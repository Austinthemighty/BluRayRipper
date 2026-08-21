"""Story: "it says ready but nothing is happening, and putting the disc back in
must not make a second job".

A clustered job at READY with no ripped file belongs to nobody: serve skips
READY as worker-owned, and a worker claims it then hands it straight back
(_fully_ripped is False), so it sits there for ever looking queued. Recovery
parks it as a review item WITHOUT touching the drive, and the disc going back in
later has to land on that parked job rather than start a duplicate that then
fights it for the same drive.

tests/test_recovery.py pins the individual steps. This file walks the whole
narrative end to end and pins the discriminations that keep it honest: which
job the disc wakes, which jobs it must leave alone, and what still has to be an
explicit click.
"""

import asyncio

from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, EpisodeAssignment, MediaKind, Title
from blurayripper.persistence import (
    JobRecord,
    MemoryStore,
    assignment_to_dict,
    disc_to_dict,
)
from blurayripper.pipeline import JobState
from blurayripper.tools.makemkv import Drive
from blurayripper.web.manager import JobManager
from blurayripper.worker import CLAIMABLE_STATES, WorkerRunner

# A real encode-side failure: NOT one of the killed-process return codes that
# recovery treats as "interrupted, resume me".
ENCODE_FAILURE = "ProcessError: Command failed (7): HandBrakeCLI.exe ..."


class ClusteredStore(MemoryStore):
    """Stands in for the shared Postgres every box in the cluster talks to."""

    @property
    def durable(self) -> bool:
        return True


def _cluster(tmp_path, monkeypatch, *, use_workers: bool = True):
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path / "out")
    monkeypatch.setattr(settings.storage, "staging_dir", tmp_path / "stage")
    monkeypatch.setattr(settings.cluster, "use_workers", use_workers)
    monkeypatch.setattr(settings, "auto_rip", False)
    monkeypatch.setattr(settings, "notify_url", None)  # no real pushes from tests
    return settings, ClusteredStore()


def _record(job_id, *, disc_name, source="disc:0", state="ready",
            titles=(0,), ripped=(), error=None):
    """A persisted job for ``disc_name``; ``ripped`` lists the titles that have a
    rip checkpoint (so () is the orphan case: queued but never actually ripped)."""
    return JobRecord(
        id=job_id, source=source, state=state, fingerprint=f"fp-{job_id}",
        disc=disc_to_dict(DiscInfo(
            source=source, disc_name=disc_name, drive_device="E:",
            titles=[Title(title_id=t, duration_seconds=3000 + t) for t in titles])),
        selected_title_ids=list(titles),
        assignments=[
            assignment_to_dict(EpisodeAssignment(
                title_id=t, kind=MediaKind.TV, show_or_movie_title="House",
                year=2004, season=1, episode=t + 1))
            for t in titles
        ],
        checkpoints={str(t): {"ripped_path": f"job_{job_id}/rip/t{t:03d}/a.mkv"}
                     for t in ripped},
        error=error,
    )


def _drive(index, disc_name, device):
    return Drive(index=index, name=f"Model-{index}", device=device,
                 disc_name=disc_name)


def _capture_rips(manager, monkeypatch):
    """Record every job that actually starts running (rip included).

    Both routes into a running job — recovery's _resume and the UI's _start_job —
    end in _spawn(self._run_job(wj)), so stubbing that one coroutine catches any
    of them while leaving persistence and state transitions real."""
    ran = []

    async def fake_run_job(wj):
        ran.append(wj.id)

    monkeypatch.setattr(manager, "_run_job", fake_run_job)
    return ran


def _capture_pushes(settings, monkeypatch):
    monkeypatch.setattr(settings, "notify_url", "http://ntfy.example/blurayripper")
    pushed = []

    async def fake_notify(url, title, message, priority=None):
        pushed.append((title, message))
        return True

    monkeypatch.setattr("blurayripper.web.manager.notify", fake_notify)
    return pushed


def _stub_add_disc(manager, monkeypatch):
    added = []
    monkeypatch.setattr(manager, "add_disc", lambda source: added.append(source))
    return added


def _record_for(store, job_id):
    return {r.id: r for r in store.load_all()}[job_id]


def _count(wj, needle):
    return sum(1 for line in wj.log if needle in line)


async def _settle():
    for _ in range(20):
        await asyncio.sleep(0)


async def test_the_orphaned_ready_job_is_parked_and_then_reclaimed_by_its_own_disc(
        tmp_path, monkeypatch):
    """The whole story in one run, because neither half is any use alone: parking
    without matching means feeding the disc back spawns a rival job for the same
    drive, and matching without parking means the job either idles for ever or
    spins up a drive nobody asked it to touch. Also checks the two things that
    must survive a restart — the parked state and the drive it was repointed at."""
    settings, store = _cluster(tmp_path, monkeypatch)
    store.save(_record(300, disc_name="HOUSE_S1_D3", ripped=()))

    # Premise: as persisted, this job is claimable by a worker that can then do
    # nothing with it — the deadlock the review gate exists to break.
    claimed = store.claim_next("worker-1", CLAIMABLE_STATES)
    assert claimed is not None and claimed.id == 300
    assert WorkerRunner(settings, worker_id="worker-1")._fully_ripped(claimed) is False
    store.release(300)

    m = JobManager(settings, store=store)
    ran = _capture_rips(m, monkeypatch)
    pushed = _capture_pushes(settings, monkeypatch)
    added = _stub_add_disc(m, monkeypatch)

    # --- serve restarts: the orphan is parked, and NOTHING is re-ripped --------
    assert m.recover() == 0
    await _settle()
    wj = m.jobs[300]
    assert ran == []                                  # no drive was touched
    assert wj.state is JobState.FAILED
    assert "Rip never completed" in (wj.error or "")
    assert _count(wj, "Nothing has been read or re-ripped") == 1

    card = m._job_dict(wj)
    assert card["needs_review"] is True    # its own queue: a physical decision
    assert card["retry_rips"] is True      # the button must say RIP, not resume
    assert card["needs_disc"] is True
    assert card["disc_loaded"] is False    # the disc isn't in the machine yet

    # It is out of the workers' queue now, so nothing claims-and-releases it.
    assert store.claim_next("worker-2", CLAIMABLE_STATES) is None
    saved = _record_for(store, 300)
    assert saved.state == "failed"
    assert "Rip never completed" in (saved.error or "")

    # --- serve restarts again while the disc is still out ---------------------
    # The parking is durable, so this is not a loop that re-parks (and re-logs)
    # the same job on every restart.
    m2 = JobManager(settings, store=store)
    ran2 = _capture_rips(m2, monkeypatch)
    assert m2.recover() == 0
    await _settle()
    assert ran2 == []
    assert m2.jobs[300].state is JobState.FAILED
    assert _count(m2.jobs[300], "Nothing has been read or re-ripped") == 1

    # --- the user finds the disc and puts it in a DIFFERENT drive -------------
    m.drive_names.set("F:", "Top drive")
    m._drives_cache = [_drive(1, "HOUSE_S1_D3", "F:")]
    m._auto_open(m._drives_cache)
    await _settle()

    assert added == []                        # no duplicate job for this disc
    assert wj.source == "disc:1"              # repointed at the drive it is in
    assert _record_for(store, 300).source == "disc:1"   # …and that survives a restart
    assert _count(wj, "Disc is back in Top drive") == 1   # named drive, not "disc:1"
    assert _count(wj, "No new job was created for this disc") == 1
    assert m._job_dict(wj)["disc_loaded"] is True   # tells you WHICH parked job

    # Still parked: inserting a disc is not approval.
    assert ran == []
    assert m._live_state(wj) is JobState.FAILED
    assert m._job_dict(wj)["needs_review"] is True
    assert len(pushed) == 1
    title, message = pushed[0]
    assert "HOUSE_S1_D3" in title and "review" in title.lower()
    assert "#300" in message

    # --- the drive watcher keeps polling with the disc still sitting there ----
    m._auto_open(m._drives_cache)
    m._auto_open(m._drives_cache)
    await _settle()
    assert added == []
    assert _count(wj, "Disc is back in Top drive") == 1   # not once per poll
    assert len(pushed) == 1                               # nor a push per poll

    # --- approval is the click ------------------------------------------------
    m.retry(300)
    await _settle()
    assert ran == [300]
    assert m.jobs[300].state is JobState.READY
    assert m.jobs[300].source == "disc:1"     # rips from where the disc actually is


async def test_the_returning_disc_wakes_only_the_job_that_is_waiting_for_it(
        tmp_path, monkeypatch):
    """With a shelf of parked discs the match has to be on the label, not merely
    on "something failed" — otherwise one disc going in repoints an unrelated job
    at a drive holding the wrong disc, and the UI tells you the wrong card is the
    one you just fed."""
    settings, store = _cluster(tmp_path, monkeypatch)
    store.save(_record(300, disc_name="HOUSE_S1_D3", ripped=()))
    store.save(_record(301, disc_name="WARGAMES", ripped=()))

    m = JobManager(settings, store=store)
    _capture_rips(m, monkeypatch)
    added = _stub_add_disc(m, monkeypatch)
    m.recover()
    await _settle()
    assert m.jobs[300].state is m.jobs[301].state is JobState.FAILED

    # One drive holds a parked job's disc; the other holds something new entirely.
    drives = [_drive(1, "HOUSE_S1_D3", "F:"), _drive(2, "TOY_STORY", "G:")]
    m._drives_cache = drives
    m._auto_open(drives)
    await _settle()

    # Only the unrecognised disc becomes a new job.
    assert added == ["disc:2"]
    assert m.jobs[300].source == "disc:1"
    assert m.jobs[301].source == "disc:0"          # untouched
    assert _count(m.jobs[301], "Disc is back") == 0
    # Two drives are loaded, but only the job whose label matches says so.
    assert m._job_dict(m.jobs[300])["disc_loaded"] is True
    assert m._job_dict(m.jobs[301])["disc_loaded"] is False


async def test_only_a_job_with_a_title_left_to_rip_lays_claim_to_the_disc(
        tmp_path, monkeypatch):
    """A job that failed AFTER ripping already has its file: it must let the disc
    start a new job and stop offering disc controls, or you lose the rip and the
    drive. But "ripped" means every selected title — a TV disc that died halfway
    through the episodes is still waiting for that disc."""
    settings, store = _cluster(tmp_path, monkeypatch)
    store.save(_record(400, disc_name="FINISHED_RIP", titles=(0, 1), ripped=(0, 1),
                       state="failed", error=ENCODE_FAILURE))
    store.save(_record(401, disc_name="HALF_RIPPED", titles=(0, 1), ripped=(0,),
                       state="failed", error=ENCODE_FAILURE))

    m = JobManager(settings, store=store)
    _capture_rips(m, monkeypatch)
    added = _stub_add_disc(m, monkeypatch)
    m.recover()
    await _settle()

    drives = [_drive(1, "FINISHED_RIP", "F:"), _drive(2, "HALF_RIPPED", "G:")]
    m._drives_cache = drives
    m._auto_open(drives)
    await _settle()

    # The fully ripped job keeps its hands off: its disc is free for a new job.
    assert added == ["disc:1"]
    assert m.jobs[400].source == "disc:0"
    done_card = m._job_dict(m.jobs[400])
    assert done_card["needs_disc"] is False
    assert done_card["retry_rips"] is False      # Retry resumes from checkpoints
    assert done_card["needs_review"] is False    # an ordinary failure, not a hunt

    # The half-ripped one still needs the disc, so it takes the reinsertion.
    assert m.jobs[401].source == "disc:2"
    assert _count(m.jobs[401], "Disc is back") == 1
    half_card = m._job_dict(m.jobs[401])
    assert half_card["needs_disc"] is True
    assert half_card["needs_review"] is True


async def test_a_box_set_label_that_matches_several_parked_jobs_says_how_many(
        tmp_path, monkeypatch):
    """Every disc in a box set reports the same volume label, so the disc you put
    back can legitimately match more than one parked job. Guessing one would
    repoint the wrong job at the drive, so all of them follow the disc and the
    push says how many — the review still picks."""
    settings, store = _cluster(tmp_path, monkeypatch)
    store.save(_record(500, disc_name="HOUSE", ripped=()))
    store.save(_record(501, disc_name="HOUSE", ripped=()))

    m = JobManager(settings, store=store)
    ran = _capture_rips(m, monkeypatch)
    pushed = _capture_pushes(settings, monkeypatch)
    added = _stub_add_disc(m, monkeypatch)
    m.recover()
    await _settle()

    m._drives_cache = [_drive(3, "HOUSE", "H:")]
    m._auto_open(m._drives_cache)
    await _settle()

    assert added == []
    assert m.jobs[500].source == m.jobs[501].source == "disc:3"
    assert ran == []
    assert len(pushed) == 1
    assert "2 parked jobs match this disc" in pushed[0][1]


async def test_a_single_box_resumes_the_rip_instead_of_asking_for_review(
        tmp_path, monkeypatch):
    """The gate is about ownership, not about the missing rip: on one box the
    same process rips AND encodes, so an unripped READY job is its own to finish.
    Parking it there would strand work that used to just resume after a restart."""
    settings, store = _cluster(tmp_path, monkeypatch, use_workers=False)
    store.save(_record(300, disc_name="HOUSE_S1_D3", ripped=()))

    m = JobManager(settings, store=store)
    ran = _capture_rips(m, monkeypatch)

    assert m.recover() == 1
    await _settle()

    wj = m.jobs[300]
    assert ran == [300]                       # picked straight back up
    assert wj.state is JobState.READY
    assert _count(wj, "Nothing has been read or re-ripped") == 0
    assert m._job_dict(wj)["needs_review"] is False
