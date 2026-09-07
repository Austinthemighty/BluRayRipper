"""Serve -> worker handoff through the shared store: who owns a job, and when.

Integration tests for the ownership protocol only — no HandBrake, no ffmpeg, no
Postgres. What they exercise is the contract between JobManager (the rip box),
WorkerRunner (an encode box) and the Store that sits between them: a prepped job
becomes claimable, exactly one worker holds it, a worker refuses work the rip box
hasn't finished, a silent worker loses its claim, and serve mirrors a worker's
progress back onto the job without ever stepping on work it owns itself.
"""

from __future__ import annotations

import asyncio
import time

from blurayripper import version as version_mod
from blurayripper import worker as worker_mod
from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, EpisodeAssignment, MediaKind, Title
from blurayripper.persistence import (
    JobRecord,
    MemoryStore,
    WorkerInfo,
    assignment_to_dict,
)
from blurayripper.pipeline import Job, JobState, TitleResult
from blurayripper.web import manager as manager_mod
from blurayripper.web.manager import JobManager, WebJob
from blurayripper.worker import (
    CLAIMABLE_STATES,
    WorkerReporter,
    WorkerRunner,
    job_from_record,
)


# --- stores -------------------------------------------------------------

class SharedStore(MemoryStore):
    """A MemoryStore that says it is shared.

    ``durable`` is the flag that puts serve into clustered mode (it starts the
    store-refresh loop and leaves worker-owned states alone). Claim/release keep
    MemoryStore's own semantics, so tests using this one pin the real thing.
    """

    @property
    def durable(self) -> bool:
        return True


class ClusterStore(SharedStore):
    """SharedStore plus the two things only a real (Postgres) store provides: a
    per-job heartbeat that a claim expires against, and a workers table.

    This mirrors the SQL in ``PostgresStore.claim_next`` / ``heartbeat`` /
    ``record_worker``. ``now`` is a fake clock the test advances, so staleness is
    exercised without sleeping. Tests built on it vary only what the *worker* or
    *serve* code does against a fixed store contract.
    """

    def __init__(self) -> None:
        super().__init__()
        self.now = 0.0
        self._owner: dict[int, str] = {}
        self._beat: dict[int, float] = {}
        self._workers: dict[str, tuple] = {}

    def claim_next(self, worker_id, states, stale_after_seconds=180):
        for job_id, data in sorted(self._rows.items()):
            if data.get("state") not in states:
                continue
            held = self._owner.get(job_id)
            if held is not None and (self.now - self._beat.get(job_id, 0.0)) <= stale_after_seconds:
                continue  # someone holds it and is still proving they're alive
            self._owner[job_id] = worker_id
            self._beat[job_id] = self.now
            return JobRecord.from_json(data)
        return None

    def heartbeat(self, job_id, worker_id):
        if self._owner.get(job_id) == worker_id:
            self._beat[job_id] = self.now

    def release(self, job_id, owner=None):
        self._owner.pop(job_id, None)
        self._beat.pop(job_id, None)

    def owner_of(self, job_id):
        return self._owner.get(job_id)

    def record_worker(self, worker_id, version, job_id, stage, run_token=None, address=None):
        self._workers[worker_id] = (version, job_id, stage, self.now)

    def list_workers(self):
        return [WorkerInfo(wid, ver, job_id, stage, self.now - seen)
                for wid, (ver, job_id, stage, seen) in sorted(self._workers.items())]


# --- fixtures / helpers -------------------------------------------------

def _settings(tmp_path, monkeypatch):
    """Clustered settings pointed entirely at tmp_path, with every side effect
    (pushes, stale-tool kills, real Postgres) switched off."""
    s = load_settings()
    monkeypatch.setattr(s.storage, "output_dir", tmp_path / "library")
    monkeypatch.setattr(s.storage, "staging_dir", tmp_path / "staging")
    s.notify_url = None            # no pushes out of a test
    s.cleanup_stale_tools = False  # don't pkill anything on this machine
    s.database_url = "postgresql://cluster/test"  # "a shared store exists"
    s.cluster.use_workers = True
    s.cluster.worker_poll_seconds = 0
    s.cluster.offline_alert_seconds = 0  # presence alerts are covered elsewhere
    return s


def _worker(settings, store, worker_id, monkeypatch):
    """A WorkerRunner wired to `store`, with the two things `run()` reaches out
    to the machine for (git version, config reload) neutralised."""
    monkeypatch.setattr(version_mod, "get_version", lambda: "0.1.0+999.testbld")
    monkeypatch.setattr(worker_mod, "load_settings", lambda *a, **kw: settings)
    runner = WorkerRunner(settings, worker_id=worker_id)
    runner.store = store
    return runner


def _capture_process(runner, monkeypatch):
    """Replace the encode with a recorder; returns the list of job ids reaching it."""
    processed: list[int] = []

    async def fake_process(rec):
        processed.append(rec.id)

    monkeypatch.setattr(runner, "_process", fake_process)
    return processed


def _stop_after(runner, monkeypatch, polls=2):
    """Let the worker's real claim loop run `polls` times, then drain it — so
    ``run()`` terminates in a test without waiting on a poll interval."""
    store = runner.store
    # Several runners share one store across a test; remember the store's own
    # claim_next the first time so each wrapper wraps it, not the last wrapper.
    if not hasattr(store, "_pristine_claim"):
        store._pristine_claim = store.claim_next
    real_claim = store._pristine_claim
    seen = {"n": 0}

    def counted(worker_id, states, stale_after_seconds=180):
        seen["n"] += 1
        if seen["n"] > polls:
            runner.request_stop()
            return None
        return real_claim(worker_id, states, stale_after_seconds)

    monkeypatch.setattr(runner.store, "claim_next", counted)
    return seen


def _ready_record(job_id, **kw):
    base = dict(
        id=job_id, source="disc:0", state="ready", selected_title_ids=[0],
        assignments=[assignment_to_dict(
            EpisodeAssignment(0, MediaKind.MOVIE, "WarGames", 1983))],
        # Already placed, so _fully_ripped is satisfied without touching disk;
        # the tests that care about the rip file set their own checkpoints.
        checkpoints={"0": {"placed": True}},
    )
    base.update(kw)
    return JobRecord(**base)


def _webjob(manager, job_id, state=JobState.QUEUED):
    """A WebJob already past the scan/mapping gates, registered with the manager."""
    disc = DiscInfo(source="disc:0", disc_name="WARGAMES", titles=[
        Title(title_id=0, duration_seconds=6762, chapter_count=12,
              source_filename="00002.mpls")])
    wj = WebJob(id=job_id, source="disc:0", state=state)
    wj.disc = disc
    wj.selected_title_ids = [0]
    wj.assignments = {0: EpisodeAssignment(0, MediaKind.MOVIE, "WarGames", 1983)}
    job = Job(source="disc:0", state=state)
    job.disc = disc
    job.selected_title_ids = [0]
    job.assignments = dict(wj.assignments)
    wj._pipeline_job = job
    manager.jobs[job_id] = wj
    return wj


def _returns(value):
    async def _f(*a, **kw):
        return value
    return _f


async def _refresh_until(manager, ready, timeout=2.0):
    """Run serve's REAL store-refresh loop until `ready()` holds, then stop it.

    The loop's poll interval is zeroed and each pass is in-memory, so this settles
    in a few milliseconds; the timeout only exists to fail loudly rather than hang.
    """
    manager.settings.cluster.worker_poll_seconds = 0
    manager.closing = False
    task = asyncio.ensure_future(manager._refresh_from_store())
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(0.005)
            if ready():
                return
        raise AssertionError("serve's refresh loop never reached the expected state")
    finally:
        manager.closing = True
        await asyncio.sleep(0)
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _serve_prep(tmp_path, monkeypatch, *, height):
    """Run serve's clustered _run_job for one job whose only title is `height`
    pixels tall. Returns (settings, store, encoded_locally)."""
    settings = _settings(tmp_path, monkeypatch)
    settings.cluster.local_encode_max_height = 1080
    monkeypatch.setattr(manager_mod, "get_version", lambda: "0.1.0+999.testbld")
    store = SharedStore()
    manager = JobManager(settings, store=store)
    wj = _webjob(manager, 41)
    rel = "job_41/rip/t000/title.mkv"

    async def fake_prep(job, staging_root, report, include_subs=True):
        # Stand in for MakeMKV: leave a real ripped file where the checkpoint
        # says it is, so a worker's _fully_ripped check has something to find.
        ripped = settings.storage.staging_dir / rel
        ripped.parent.mkdir(parents=True, exist_ok=True)
        ripped.write_text("ripped bits")
        job.checkpoints[0] = {"ripped_path": rel}
        report.on_event("Rip finished.")

    encoded_locally: list[str] = []

    async def fake_encode(job, staging_root, scheduler, report):
        encoded_locally.append(job.source)
        return [TitleResult(0, video_path=tmp_path / "WarGames (1983).mkv")]

    monkeypatch.setattr(manager.pipeline, "prep_phase", fake_prep)
    monkeypatch.setattr(manager.pipeline, "_persist_mapping", lambda job, report: None)
    monkeypatch.setattr(manager.pipeline, "title_heights", _returns([height]))
    monkeypatch.setattr(manager.pipeline, "encode_phase", fake_encode)

    await manager._run_job(wj)
    return settings, store, encoded_locally


def _record(store, job_id):
    return {r.id: r for r in store.load_all()}[job_id]


# --- the handoff --------------------------------------------------------

async def test_a_uhd_job_serve_cannot_encode_is_left_in_the_queue_for_a_worker(
        tmp_path, monkeypatch):
    """The whole reason workers exist: a 2160p job must leave the rip box as a
    claimable queue entry, not be encoded on the machine that has to keep ripping.
    If the handoff state were anything but READY no worker would ever take it and
    the disc would sit finished-but-unencoded forever."""
    settings, store, encoded_locally = await _serve_prep(tmp_path, monkeypatch, height=2160)

    assert encoded_locally == []                 # the rip box did not encode it
    assert _record(store, 41).state == "ready"   # ...it queued it instead

    # And a real worker on the other box picks it straight out of that store.
    runner = _worker(settings, store, "encoder-A", monkeypatch)
    processed = _capture_process(runner, monkeypatch)
    _stop_after(runner, monkeypatch)
    await runner.run()
    assert processed == [41]


async def test_a_1080p_job_is_encoded_on_the_rip_box_and_never_offered_to_a_worker(
        tmp_path, monkeypatch):
    """Same job, same store, only the title height differs: under
    local_encode_max_height serve finishes it itself. A worker must find nothing —
    otherwise a job would be encoded twice, once on each box, racing over the
    same output file."""
    settings, store, encoded_locally = await _serve_prep(tmp_path, monkeypatch, height=1080)

    assert encoded_locally == ["disc:0"]         # serve did the encode
    assert _record(store, 41).state == "done"    # ...and nothing is claimable

    # Ask the store directly, BEFORE a worker is built: a finished row is not
    # offered to a claimer at all. Without this the test below would also pass
    # for an unrelated reason — the local encode deletes the job's staging dir,
    # so even a wrongly-claimable row is dropped again by the _fully_ripped
    # guard, and "the worker encoded nothing" would prove nothing about state.
    assert store.claim_next("encoder-Z", CLAIMABLE_STATES, 180) is None

    runner = _worker(settings, store, "encoder-A", monkeypatch)
    processed = _capture_process(runner, monkeypatch)
    _stop_after(runner, monkeypatch)
    await runner.run()
    assert processed == []


async def test_two_workers_cannot_hold_the_same_job_at_once(tmp_path, monkeypatch):
    """Claiming has to be exclusive. Two workers encoding one job would both
    write the same title into the library and both mark it done — the classic
    duplicate-output failure a shared queue exists to prevent."""
    settings = _settings(tmp_path, monkeypatch)
    store = SharedStore()
    store.save(_ready_record(50))

    first = _worker(settings, store, "encoder-A", monkeypatch)
    got_first = _capture_process(first, monkeypatch)
    _stop_after(first, monkeypatch)
    await first.run()
    assert got_first == [50]

    second = _worker(settings, store, "encoder-B", monkeypatch)
    got_second = _capture_process(second, monkeypatch)
    _stop_after(second, monkeypatch)
    await second.run()
    assert got_second == []          # A still holds it

    # Once A's claim is dropped the very same job is available again — proving
    # the empty result above was the claim, not the job being unclaimable.
    store.release(50)
    third = _worker(settings, store, "encoder-B", monkeypatch)
    got_third = _capture_process(third, monkeypatch)
    _stop_after(third, monkeypatch)
    await third.run()
    assert got_third == [50]


async def test_a_worker_hands_back_a_job_whose_rip_has_not_landed_yet(
        tmp_path, monkeypatch):
    """A job goes READY in the store before its rip file exists on the NAS, so a
    worker can claim one serve is still ripping. It must put the claim back and
    keep polling — failing it as "nothing to encode" would kill a live rip, and
    sitting on it would deadlock the job (serve treats READY as worker-owned)."""
    settings = _settings(tmp_path, monkeypatch)
    store = SharedStore()
    rel = "job_60/rip/t000/title.mkv"
    store.save(_ready_record(60, checkpoints={"0": {"ripped_path": rel}}))

    too_early = _worker(settings, store, "encoder-A", monkeypatch)
    got_early = _capture_process(too_early, monkeypatch)
    _stop_after(too_early, monkeypatch)
    await too_early.run()
    assert got_early == []           # refused: no ripped file to encode

    # Serve finishes the rip. Nothing else changes — same store, same record.
    ripped = settings.storage.staging_dir / rel
    ripped.parent.mkdir(parents=True, exist_ok=True)
    ripped.write_text("ripped bits")

    now_ready = _worker(settings, store, "encoder-B", monkeypatch)
    got_ready = _capture_process(now_ready, monkeypatch)
    _stop_after(now_ready, monkeypatch)
    await now_ready.run()
    # Also proves the first worker RELEASED it: an unreleased claim would still
    # be held and this second worker would come away empty.
    assert got_ready == [60]


async def test_a_silent_workers_job_is_reclaimed_only_after_the_configured_window(
        tmp_path, monkeypatch):
    """Reclaim has to be driven by cluster.stale_after_seconds, not a hardcoded
    default. Too eager and a slow-but-alive encode gets duplicated; too patient
    and a dead worker's job is stuck for the default 3 minutes regardless of what
    the operator configured."""
    async def attempt(stale_after):
        settings = _settings(tmp_path, monkeypatch)
        settings.cluster.stale_after_seconds = stale_after
        store = ClusterStore()
        store.save(_ready_record(70))
        store.claim_next("encoder-A", CLAIMABLE_STATES, 180)  # A takes it at t=0
        store.now = 100                                       # ...then goes quiet
        runner = _worker(settings, store, "encoder-B", monkeypatch)
        processed = _capture_process(runner, monkeypatch)
        _stop_after(runner, monkeypatch)
        await runner.run()
        return store, processed

    patient, not_taken = await attempt(600)    # 100s of silence is fine
    impatient, taken = await attempt(30)       # 100s of silence is too long

    assert not_taken == [] and patient.owner_of(70) == "encoder-A"
    assert taken == [70] and impatient.owner_of(70) == "encoder-B"


async def test_a_heartbeating_worker_keeps_its_claim_through_the_stale_window(
        tmp_path, monkeypatch):
    """A UHD encode runs far longer than the stale window, so the only thing
    stopping another worker from stealing it mid-encode is the heartbeat the
    reporter sends while it works. Lose that and every long job gets encoded
    twice."""
    store = ClusterStore()
    store.save(_ready_record(80))
    rec = store.claim_next("encoder-A", CLAIMABLE_STATES, 180)
    job = job_from_record(rec)
    job.state = JobState.ENCODING          # what the pipeline sets while encoding
    reporter = WorkerReporter(store, "encoder-A", 80, job)

    for _ in range(6):                     # 300s of encoding, alive throughout
        store.now += 50
        reporter.on_progress("encoding title 1", 0.5)
        reporter.touch()

    # Guard against passing for the wrong reason: the row IS in a claimable state.
    assert _record(store, 80).state == "encoding"
    assert "encoding" in CLAIMABLE_STATES
    assert store.claim_next("encoder-B", CLAIMABLE_STATES, 180) is None
    assert store.owner_of(80) == "encoder-A"
    # touch() also mirrors progress out, which is what serve's UI reads.
    assert _record(store, 80).progress == 0.5
    assert _record(store, 80).worker == "encoder-A"

    store.now += 200                       # the worker dies: no more touches
    assert store.claim_next("encoder-B", CLAIMABLE_STATES, 180).id == 80
    assert store.owner_of(80) == "encoder-B"


async def test_serve_mirrors_a_workers_progress_but_not_onto_a_job_it_is_ripping(
        tmp_path, monkeypatch):
    """Serve's refresh is how a remote encode shows up in the UI at all — and it
    is also the one place serve can overwrite a job from the outside. It must
    copy worker-owned jobs and skip locally-owned ones, or a stale row would drag
    a disc that is physically ripping right now into "done"."""
    settings = _settings(tmp_path, monkeypatch)
    monkeypatch.setattr(manager_mod, "get_version", lambda: "0.1.0+999.testbld")
    store = ClusterStore()
    manager = JobManager(settings, store=store)

    handed_off = _webjob(manager, 90, state=JobState.ENCODING)
    still_ripping = _webjob(manager, 91, state=JobState.RIPPING)
    still_ripping.stage = "ripping title 1"
    still_ripping.progress = 0.25
    still_ripping.log = ["serve: ripping title 1"]

    # A worker writes an IDENTICAL kind of record for both ids.
    for job_id in (90, 91):
        job = Job(source="disc:0", state=JobState.ENCODING)
        reporter = WorkerReporter(store, "encoder-A", job_id, job)
        reporter.on_event("Claimed by encoder-A.")
        reporter.on_progress("encoding title 1", 0.66)
        reporter.finish(JobState.DONE, [str(tmp_path / "WarGames (1983).mkv")])

    await _refresh_until(manager, lambda: handed_off.state is JobState.DONE)

    # The worker-owned job took everything the worker reported.
    assert handed_off.state is JobState.DONE
    assert handed_off._pipeline_job.state is JobState.DONE
    assert handed_off.stage == "encoding title 1"
    assert handed_off.progress == 0.66
    assert any("Claimed by encoder-A." in line for line in handed_off.log)
    assert handed_off.outputs == [str(tmp_path / "WarGames (1983).mkv")]

    # The job serve is ripping kept every one of its own fields.
    assert still_ripping.state is JobState.RIPPING
    assert still_ripping._pipeline_job.state is JobState.RIPPING
    assert still_ripping.stage == "ripping title 1"
    assert still_ripping.progress == 0.25
    assert still_ripping.log == ["serve: ripping title 1"]
    assert still_ripping.outputs == []


async def test_serve_flags_an_encode_as_stalled_only_when_no_live_worker_claims_it(
        tmp_path, monkeypatch):
    """A worker that dies mid-encode leaves the row saying "encoding" forever.
    Serve decides a job is frozen by asking which worker is heart-beating it, so
    the answer must follow both WHICH job the worker reports and HOW long ago it
    reported — otherwise the UI either never surfaces a dead encode, or flags
    healthy ones and invites you to requeue work that is running fine."""
    settings = _settings(tmp_path, monkeypatch)
    settings.cluster.stale_after_seconds = 60
    monkeypatch.setattr(manager_mod, "get_version", lambda: "0.1.0+999.testbld")
    store = ClusterStore()
    manager = JobManager(settings, store=store)

    claimed = _webjob(manager, 100, state=JobState.ENCODING)
    orphaned = _webjob(manager, 101, state=JobState.ENCODING)

    runner = _worker(settings, store, "encoder-A", monkeypatch)
    runner._version = "0.1.0+999.testbld"
    runner._register(100, "encoding title 1")   # the worker says it is on 100

    await _refresh_until(
        manager, lambda: any(w.job_id == 100 for w in manager._workers_cache))
    assert manager._is_stalled(claimed, manager._live_state(claimed)) is False
    assert manager._is_stalled(orphaned, manager._live_state(orphaned)) is True

    # encoder-A stops reporting. 100s is past THIS cluster's 60s window but
    # inside the 180s default, so a stall window read from anywhere but
    # cluster.stale_after_seconds still calls the dead encode healthy.
    store.now += 100
    await _refresh_until(
        manager,
        lambda: any(w.worker_id == "encoder-A" and w.seconds_since_seen > 60
                    for w in manager._workers_cache))
    assert manager._is_stalled(claimed, manager._live_state(claimed)) is True


async def test_a_finished_job_is_released_so_an_immediate_retry_is_claimable(
        tmp_path, monkeypatch):
    """When an encode ends the worker must drop its claim. If it didn't, hitting
    Retry on a just-failed job would leave it queued but unclaimable until the
    claim aged out — the job would look ready and simply never start."""
    settings = _settings(tmp_path, monkeypatch)
    store = SharedStore()
    store.save(_ready_record(110))

    first = _worker(settings, store, "encoder-A", monkeypatch)

    async def fake_encode(job, staging_root, scheduler, report):
        report.on_progress("encoding", 1.0)
        # A real encode leaves a real file behind: the worker now looks at the
        # disk before reporting DONE, so a stub that promises a path it never
        # wrote is a failed job, not a finished one.
        placed = tmp_path / "WarGames (1983).mkv"
        placed.write_text("h265 bytes")
        return [TitleResult(0, video_path=placed)]

    monkeypatch.setattr(first.pipeline, "encode_phase", fake_encode)
    rec = store.claim_next("encoder-A", CLAIMABLE_STATES, 180)
    assert rec.id == 110
    await first._process(rec)
    assert _record(store, 110).state == "done"

    # The operator hits Retry: serve flips the row back to READY.
    retried = _record(store, 110)
    retried.state = "ready"
    retried.error = None
    store.save(retried)

    second = _worker(settings, store, "encoder-B", monkeypatch)
    picked_up = _capture_process(second, monkeypatch)
    _stop_after(second, monkeypatch)
    await second.run()
    assert picked_up == [110]
