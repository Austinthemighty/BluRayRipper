"""Crash-recovery matrix: which persisted states a restart re-runs, and which it
must leave alone.

The rules differ between a single box (serve does everything) and a cluster
(serve rips, workers encode), so each rule below is exercised with the ONE
setting that decides it varied and everything else held still. Extends
tests/test_recovery.py, which covers the single-box happy path.
"""

import asyncio

from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, EpisodeAssignment, MediaKind, Title
from blurayripper.persistence import JobRecord, MemoryStore, assignment_to_dict, disc_to_dict
from blurayripper.pipeline import JobState
from blurayripper.web.manager import JobManager


class DurableMemoryStore(MemoryStore):
    """MemoryStore that claims to be the shared Postgres store, so the manager
    takes the clustered code paths (workers exist and can claim jobs)."""

    @property
    def durable(self) -> bool:
        return True


def _disc_dict(name="WarGames"):
    return disc_to_dict(DiscInfo(
        source="disc:0", disc_name=name,
        titles=[Title(title_id=0, duration_seconds=6000, chapter_count=12)],
    ))


def _assignment(title="WarGames", year=1983):
    return assignment_to_dict(EpisodeAssignment(0, MediaKind.MOVIE, title, year))


def _rec(job_id, state, **kw):
    """A fully-prepped disc job: titles selected, named, and already ripped
    (so `_rip_pending` is False and the orphan-review branch stays out of it)."""
    base = dict(
        id=job_id, source=f"disc:{job_id}", state=state, fingerprint="fp",
        disc=_disc_dict(), selected_title_ids=[0], assignments=[_assignment()],
        checkpoints={"0": {"ripped_path": f"job_{job_id}/rip/t000/a.mkv"}},
    )
    base.update(kw)
    return JobRecord(**base)


def _settings(tmp_path, monkeypatch, *, clustered=False, worker_does_subs=False):
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path)
    monkeypatch.setattr(settings.storage, "staging_dir", tmp_path / "staging")
    settings.cluster.use_workers = clustered
    settings.cluster.worker_does_subs = worker_does_subs
    return settings


def _manager_watching_resumes(settings, store, monkeypatch):
    """A manager whose _resume is replaced by a recorder, so recover()'s routing
    decisions are observable without any pipeline work actually starting."""
    manager = JobManager(settings, store=store)
    resumed: list[int] = []
    monkeypatch.setattr(manager, "_resume", lambda wj: resumed.append(wj.id))
    return manager, resumed


async def _settle():
    for _ in range(30):
        await asyncio.sleep(0)


# --- notifications ------------------------------------------------------

async def test_a_job_already_done_before_the_restart_is_not_notified_again(
        tmp_path, monkeypatch):
    """Restarting serve must not spray a "finished!" push for every job in the
    history — a box that has ripped 200 discs would light up the phone 200 times
    on every restart, which trains you to ignore the alerts that matter."""
    settings = _settings(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "notify_url", "https://ntfy.example/blurayripper")
    pushes = []

    async def fake_notify(url, title, message, priority=None):
        pushes.append(title)
        return True

    monkeypatch.setattr("blurayripper.web.manager.notify", fake_notify)

    store = DurableMemoryStore()
    # Old history: finished long ago, before the process died.
    store.save(_rec(1, "done", assignments=[_assignment("Alien", 1979)],
                    outputs=["/lib/Alien (1979)/Alien.mkv"]))
    # Interrupted work: finishes for real *after* the restart, so it must notify.
    store.save(_rec(2, "ready", assignments=[_assignment("WarGames", 1983)]))

    manager = JobManager(settings, store=store)

    async def fake_execute(job, scheduler, report, job_name=None):
        return job

    monkeypatch.setattr(manager.pipeline, "execute", fake_execute)
    manager.recover()
    await _settle()

    assert manager.jobs[2].state is JobState.DONE   # it really did complete here
    assert pushes == ["✅ Done: WarGames (1983)"]

    # And the suppression is permanent for the restored job: even the code path
    # that pushes on a completion refuses to fire for it a second time.
    manager._maybe_notify_done(manager.jobs[1])
    await _settle()
    assert pushes == ["✅ Done: WarGames (1983)"]


# --- interactive gates --------------------------------------------------

async def test_a_job_parked_at_the_mapping_gate_is_not_auto_resumed(tmp_path, monkeypatch):
    """The mapping gate is waiting on a human to say what the disc IS. Resuming it
    on restart would run the pipeline with no assignments and file the rip under
    the wrong name (or crash), and the person who was about to answer never gets
    asked."""
    settings = _settings(tmp_path, monkeypatch)
    store = DurableMemoryStore()
    # Identical jobs; only the persisted state differs.
    store.save(_rec(10, "awaiting_mapping", assignments=[]))
    store.save(_rec(11, "confirming", assignments=[]))

    manager, resumed = _manager_watching_resumes(settings, store, monkeypatch)
    n = manager.recover()

    assert resumed == [11]      # mid-flight work restarts...
    assert n == 1               # ...and only that one counts as recovered
    assert manager._live_state(manager.jobs[10]) is JobState.AWAITING_MAPPING
    assert manager.jobs[10].disc.disc_name == "WarGames"  # gate can still be answered


async def test_both_interactive_gates_survive_a_clustered_restart_untouched(
        tmp_path, monkeypatch):
    """Clustered, the gates are serve's own business — no worker can answer them.
    Serve must neither re-run them nor hand them to the workers by flipping them
    to READY, or a worker claims a disc that was never named."""
    settings = _settings(tmp_path, monkeypatch, clustered=True)
    store = DurableMemoryStore()
    store.save(_rec(20, "awaiting_titles", assignments=[]))
    store.save(_rec(21, "awaiting_mapping", assignments=[]))
    store.save(_rec(22, "identifying", assignments=[]))  # rip-box work: does resume

    manager, resumed = _manager_watching_resumes(settings, store, monkeypatch)
    manager.recover()

    assert resumed == [22]
    assert manager.jobs[20].state is JobState.AWAITING_TITLES
    assert manager.jobs[21].state is JobState.AWAITING_MAPPING
    # Nothing was made claimable behind the user's back.
    persisted = {r.id: r.state for r in store.load_all()}
    assert persisted[20] == "awaiting_titles" and persisted[21] == "awaiting_mapping"


# --- interrupted vs genuine failures ------------------------------------

async def test_a_clustered_kill_looking_failure_becomes_claimable_but_a_real_one_stays_failed(
        tmp_path, monkeypatch):
    """Stopping serve kills the tools it spawned, so the jobs land in the store as
    FAILED with a negative/kill return code. Those have to come back on their own —
    otherwise every restart leaves a pile of "failed" jobs needing a manual Retry.
    A genuine tool error must NOT come back, or a broken disc retries forever."""
    settings = _settings(tmp_path, monkeypatch, clustered=True)
    store = DurableMemoryStore()
    # Same job, same state, same everything — only the error text differs.
    store.save(_rec(30, "failed",
                    error="ProcessError: Command failed (-9): HandBrakeCLI ..."))
    store.save(_rec(31, "failed",
                    error="ProcessError: Command failed (2): unable to open disc"))

    manager, resumed = _manager_watching_resumes(settings, store, monkeypatch)
    manager.recover()

    # The rip box does not re-run either of them — encoding is the workers' job.
    assert resumed == []
    killed, real = manager.jobs[30], manager.jobs[31]
    assert killed.state is JobState.READY
    assert manager._live_state(killed) is JobState.READY   # pipeline state reset too
    assert killed.error is None
    assert real.state is JobState.FAILED
    assert real.error == "ProcessError: Command failed (2): unable to open disc"
    # A worker can only pick it up if the STORE says ready, not just this process.
    assert {r.id: r.state for r in store.load_all()} == {30: "ready", 31: "failed"}


# --- id allocation ------------------------------------------------------

async def test_the_next_new_job_gets_an_id_past_the_restored_ones_not_a_colliding_one(
        tmp_path, monkeypatch):
    """The counter restarts at 1 with the process. Without continuing it past the
    restored jobs, the first disc inserted after a restart is saved under an
    existing id and overwrites that job's record in Postgres — the older job's
    naming, checkpoints and outputs are simply gone."""
    settings = _settings(tmp_path, monkeypatch)
    store = DurableMemoryStore()
    store.save(_rec(4, "done", outputs=["/lib/a.mkv"]))
    store.save(_rec(9, "awaiting_mapping", assignments=[]))  # gap: ids are not dense

    manager, _resumed = _manager_watching_resumes(settings, store, monkeypatch)
    manager.recover()

    async def fake_scan(wj):
        return None

    monkeypatch.setattr(manager, "_scan_job", fake_scan)
    fresh = manager.add_disc("disc:7")
    await _settle()

    assert fresh.id == 10
    assert set(manager.jobs) == {4, 9, 10}
    rows = {r.id: r for r in store.load_all()}
    assert set(rows) == {4, 9, 10}
    assert rows[4].outputs == ["/lib/a.mkv"]   # untouched by the new job
    assert rows[9].state == "awaiting_mapping"


# --- who owns which state -----------------------------------------------

async def test_encoding_and_placing_come_back_to_serve_only_when_there_are_no_workers(
        tmp_path, monkeypatch):
    """A clustered serve box that re-ran every interrupted encode would race the
    worker that is about to claim the same job from Postgres — two machines
    encoding one title into one output path. On a single box there is nobody else
    to do it, so the very same records must resume."""
    settings = _settings(tmp_path, monkeypatch, clustered=True)
    store = DurableMemoryStore()
    store.save(_rec(40, "encoding"))
    store.save(_rec(41, "placing"))

    clustered, clustered_resumed = _manager_watching_resumes(settings, store, monkeypatch)
    clustered.recover()
    assert clustered_resumed == []
    assert clustered.jobs[40].state is JobState.ENCODING  # left as the worker's
    assert clustered.jobs[41].state is JobState.PLACING

    single_settings = _settings(tmp_path, monkeypatch, clustered=False)
    single, single_resumed = _manager_watching_resumes(single_settings, store, monkeypatch)
    single.recover()
    assert single_resumed == [40, 41]


async def test_subtitle_states_return_to_serve_only_while_serve_owns_subtitles(
        tmp_path, monkeypatch):
    """worker_does_subs moves OCR onto the workers. If serve kept reclaiming
    EXTRACTING_SUBS/OCR_SUBS after a restart it would OCR the same titles the
    worker is OCR'ing — the slow stage, done twice, on the box that should be
    ripping."""
    store = DurableMemoryStore()
    store.save(_rec(50, "extracting_subs"))
    store.save(_rec(51, "ocr_subs"))

    serve_owns = _settings(tmp_path, monkeypatch, clustered=True, worker_does_subs=False)
    m1, resumed_serve = _manager_watching_resumes(serve_owns, store, monkeypatch)
    m1.recover()
    assert resumed_serve == [50, 51]

    worker_owns = _settings(tmp_path, monkeypatch, clustered=True, worker_does_subs=True)
    m2, resumed_worker = _manager_watching_resumes(worker_owns, store, monkeypatch)
    m2.recover()
    assert resumed_worker == []
    assert m2.jobs[50].state is JobState.EXTRACTING_SUBS  # still the worker's to finish


async def test_an_unidentified_auto_identify_job_comes_back_to_the_rip_box_at_ready(
        tmp_path, monkeypatch):
    """Subtitle identification needs the OCR + OpenSubtitles matching that only the
    rip box does, so an auto-identify job that hasn't produced assignments yet
    cannot be left in the READY pool: a worker would claim it and have no idea
    which episode each title is. Once identified it is ordinary encode work."""
    settings = _settings(tmp_path, monkeypatch, clustered=True)
    store = DurableMemoryStore()
    # Both auto-identify, both READY, both ripped. Only "did the matching finish"
    # (i.e. are there assignments) differs.
    store.save(_rec(60, "ready", auto_identify=True, assignments=[]))
    store.save(_rec(61, "ready", auto_identify=True, assignments=[_assignment()]))

    manager, resumed = _manager_watching_resumes(settings, store, monkeypatch)
    manager.recover()

    assert resumed == [60]
    assert manager.jobs[61].state is JobState.READY  # left for a worker to claim


# --- what a resumed job looks like the instant recovery ends -------------

async def test_a_resumed_mid_rip_job_does_not_still_read_as_ripping(tmp_path, monkeypatch):
    """A restored job carries RIPPING on its pipeline job, and the UI's state is
    read from there in preference to the web state. Without clearing it, a
    just-recovered job shows a rip in progress — counted in "Ripping N" and
    pinning its drive in the Drives panel — before anything has actually
    restarted, so a real disc can't be started in that drive."""
    settings = _settings(tmp_path, monkeypatch)
    store = DurableMemoryStore()
    store.save(_rec(90, "ripping", checkpoints={}))

    manager = JobManager(settings, store=store)
    started = []

    async def fake_run(wj):
        started.append(wj.id)

    monkeypatch.setattr(manager, "_run_job", fake_run)
    assert manager.recover() == 1

    wj = manager.jobs[90]
    assert manager._job_dict(wj)["state"] == "ready"   # not the stale "ripping"
    assert "Recovered after restart" in "\n".join(wj.log)
    await _settle()
    assert started == [90]                              # and it really did restart


# --- the orphaned-READY review gate -------------------------------------

async def test_the_parked_orphan_is_written_back_to_the_store_not_only_in_memory(
        tmp_path, monkeypatch):
    """Parking the never-ripped job only in memory would leave Postgres still
    saying "ready", so a worker keeps claiming and releasing it forever while the
    UI shows it needs review — and a second serve restart would park it again."""
    settings = _settings(tmp_path, monkeypatch, clustered=True)
    store = DurableMemoryStore()
    store.save(_rec(70, "ready", checkpoints={}))            # rip never finished
    store.save(_rec(71, "ready"))                            # ripped: worker's job

    manager, resumed = _manager_watching_resumes(settings, store, monkeypatch)
    manager.recover()

    assert resumed == []
    persisted = {r.id: r.state for r in store.load_all()}
    assert persisted == {70: "failed", 71: "ready"}
    assert "Rip never completed" in ({r.id: r for r in store.load_all()}[70].error or "")


async def test_retrying_a_parked_orphan_checkpoints_ready_before_telling_the_ui(
        tmp_path, monkeypatch):
    """Retry is the approval for a recovery-parked job, and it announces the change
    on the event bus. If the store were written after that tick, a browser (or a
    worker polling the store) that reacts to the tick reads the job as still
    FAILED and the click looks like it did nothing."""
    settings = _settings(tmp_path, monkeypatch, clustered=True)
    store = DurableMemoryStore()
    store.save(_rec(80, "ready", checkpoints={}))

    manager = JobManager(settings, store=store)
    manager.recover()
    assert manager.jobs[80].state is JobState.FAILED  # parked for review

    async def fake_run(wj):
        return None

    monkeypatch.setattr(manager, "_run_job", fake_run)

    snapshots = []
    real_publish = manager.bus.publish

    def spy():
        snapshots.append({r.id: r.state for r in store.load_all()})
        real_publish()

    monkeypatch.setattr(manager.bus, "publish", spy)

    manager.retry(80)
    await _settle()

    assert snapshots, "retry must announce the change on the bus"
    assert snapshots[0][80] == "ready"   # store already updated at the first tick
