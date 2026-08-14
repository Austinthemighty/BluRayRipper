"""JobManager recovery: persisted jobs are reloaded; in-flight jobs resume,
parked/terminal jobs are restored as-is, and the id counter continues."""

import asyncio

from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, EpisodeAssignment, MediaKind, Title
from blurayripper.persistence import JobRecord, MemoryStore, assignment_to_dict, disc_to_dict
from blurayripper.pipeline import JobState
from blurayripper.web.manager import JobManager


def _disc_dict():
    return disc_to_dict(DiscInfo(
        source="disc:0", disc_name="WarGames",
        titles=[Title(title_id=0, duration_seconds=6000, chapter_count=12)],
    ))


async def _settle():
    for _ in range(20):
        await asyncio.sleep(0)


async def test_recover_resumes_running_and_restores_others(tmp_path, monkeypatch):
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path)
    store = MemoryStore()
    store.save(JobRecord(
        id=5, source="disc:0", state="ready", fingerprint="fp", disc=_disc_dict(),
        selected_title_ids=[0],
        assignments=[assignment_to_dict(EpisodeAssignment(0, MediaKind.MOVIE, "WarGames", 1983))],
        checkpoints={"0": {"ripped_path": "/x.mkv"}},
    ))
    store.save(JobRecord(id=6, source="disc:1", state="done", outputs=["/out/x.mkv"]))
    store.save(JobRecord(id=7, source="disc:2", state="awaiting_titles", disc=_disc_dict()))

    manager = JobManager(settings, store=store)
    resumed_jobs = []

    async def fake_execute(job, scheduler, report, job_name=None):
        resumed_jobs.append(job)
        return job

    monkeypatch.setattr(manager.pipeline, "execute", fake_execute)

    n = manager.recover()
    await _settle()

    assert n == 1  # only the "ready" job is resumed
    assert set(manager.jobs) == {5, 6, 7}
    assert manager.jobs[6].state is JobState.DONE         # terminal, untouched
    assert manager.jobs[7].state is JobState.AWAITING_TITLES  # parked, waits for UI
    assert manager.jobs[7].disc.disc_name == "WarGames"   # disc restored for the gate

    # The resumed job carried its checkpoints (with int keys) into the pipeline.
    assert resumed_jobs and resumed_jobs[0].checkpoints == {0: {"ripped_path": "/x.mkv"}}
    assert manager.jobs[5].state is JobState.DONE          # fake execute completed it

    # New job ids continue after the highest recovered id.
    assert next(manager._ids) == 8


async def test_clustered_recovery_leaves_ready_jobs_for_workers(tmp_path, monkeypatch):
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path)
    settings.cluster.use_workers = True
    store = MemoryStore()
    # Many READY (prepped, queued) jobs must NOT be re-run by the rip box.
    # "Prepped" means the rip finished, so they carry a rip checkpoint — that's
    # what distinguishes them from a READY job whose rip never completed, which
    # no worker can take and which serve therefore has to reclaim.
    for i in range(1, 6):
        store.save(JobRecord(id=i, source=f"disc:{i}", state="ready", disc=_disc_dict(),
                             selected_title_ids=[0],
                             checkpoints={"0": {"ripped_path": f"job_{i}/rip/t000/a.mkv"}}))
    # A job still mid-prep should be resumed by the rip box.
    store.save(JobRecord(id=9, source="disc:9", state="ripping", disc=_disc_dict(),
                         selected_title_ids=[0]))

    manager = JobManager(settings, store=store)
    resumed = []
    monkeypatch.setattr(manager.pipeline, "execute", lambda *a, **k: resumed.append(1))

    async def fake_prep(job, staging_root, report):
        resumed.append(job)

    monkeypatch.setattr(manager.pipeline, "prep_phase", fake_prep)
    monkeypatch.setattr(manager.pipeline, "_persist_mapping", lambda job, report: None)

    n = manager.recover()
    await _settle()

    assert n == 1                                  # only the mid-prep job resumed
    assert set(manager.jobs) == {1, 2, 3, 4, 5, 9}  # all loaded for display
    assert manager.jobs[1].state is JobState.READY  # left claimable for a worker


async def test_recover_resumes_interrupted_failed_job(tmp_path, monkeypatch):
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path)
    store = MemoryStore()
    # A job persisted FAILED by old code, but the error is a killed process.
    store.save(JobRecord(
        id=6, source="disc:0", state="failed", disc=_disc_dict(),
        selected_title_ids=[0],
        assignments=[assignment_to_dict(EpisodeAssignment(0, MediaKind.MOVIE, "WarGames", 1983))],
        checkpoints={"0": {"ripped_path": "/x.mkv"}},
        error="ProcessError: Command failed (3221225786): HandBrakeCLI.exe ...",
    ))
    # A genuinely failed job should NOT be resumed.
    store.save(JobRecord(id=9, source="disc:1", state="failed",
                         error="ProcessError: Command failed (2): bad disc"))

    manager = JobManager(settings, store=store)
    resumed = []

    async def fake_execute(job, scheduler, report, job_name=None):
        resumed.append(job)
        return job

    monkeypatch.setattr(manager.pipeline, "execute", fake_execute)
    n = manager.recover()
    await _settle()

    assert n == 1
    assert manager.jobs[6].state is JobState.DONE      # interrupted -> resumed -> done
    assert manager.jobs[9].state is JobState.FAILED    # real failure stays failed


async def test_jobs_persist_to_store_on_transitions(tmp_path, monkeypatch):
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path)
    store = MemoryStore()
    manager = JobManager(settings, store=store)

    async def fake_scan(source, report):
        from blurayripper.pipeline import Job
        job = Job(source=source)
        job.disc = DiscInfo(source=source, disc_name="WarGames",
                            titles=[Title(title_id=0, duration_seconds=6000)])
        job.fingerprint = "fp"
        return job

    monkeypatch.setattr(manager.pipeline, "scan_disc", fake_scan)

    wj = manager.add_disc("disc:0")
    await _settle()
    # After scan, the job is persisted and recoverable.
    rec = {r.id: r for r in store.load_all()}[wj.id]
    assert rec.source == "disc:0"
    assert rec.state == "awaiting_mapping"  # single-title disc auto-advances past titles
    assert rec.disc and rec.disc["disc_name"] == "WarGames"


def _rec(**kw):
    from blurayripper.persistence import JobRecord
    base = dict(id=1, source="disc:0", state="ready", selected_title_ids=[0],
                assignments=[{"title_id": 0, "kind": "movie",
                              "show_or_movie_title": "X", "year": 2000}])
    base.update(kw)
    return JobRecord(**base)


def test_an_unripped_ready_disc_job_is_parked_for_review(tmp_path, monkeypatch):
    """Orphan case: serve skips READY as worker-owned, but a worker refuses a job
    with no ripped file (_fully_ripped), so it was claimed and released forever
    while showing 'ready'. It's now surfaced as FAILED for review rather than
    re-ripped unasked — the disc may not even be in the drive. Approving it is an
    explicit click, and the button says which of the two things it does."""
    from blurayripper.config import load_settings
    from blurayripper.persistence import MemoryStore
    from blurayripper.web.manager import JobManager

    class Durable(MemoryStore):
        @property
        def durable(self): return True

    settings = load_settings()
    settings.cluster.use_workers = True
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path)
    store = Durable()
    store.save(_rec(id=300, checkpoints={}))          # no rip checkpoint at all

    m = JobManager(settings, store=store)
    resumed = []
    monkeypatch.setattr(m, "_resume", lambda wj: resumed.append(wj.id))

    m.recover()
    wj = m.jobs[300]
    assert resumed == []                       # nothing re-ripped on its own
    assert wj.state is JobState.FAILED         # surfaced for review instead
    assert "Rip never completed" in (wj.error or "")
    log = "\n".join(wj.log)
    assert "Rip this disc" in log and "Nothing has been read or re-ripped" in log
    # The card must say Retry RIPS here, not that it resumes an existing rip.
    assert m._job_dict(wj)["retry_rips"] is True


def test_recovery_still_leaves_a_ripped_ready_job_for_the_workers(tmp_path, monkeypatch):
    """The guard must be narrow — a job whose rip finished is exactly what the
    workers are for, and serve must not steal it back."""
    from blurayripper.config import load_settings
    from blurayripper.persistence import MemoryStore
    from blurayripper.web.manager import JobManager

    class Durable(MemoryStore):
        @property
        def durable(self): return True

    settings = load_settings()
    settings.cluster.use_workers = True
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path)
    store = Durable()
    store.save(_rec(id=301, checkpoints={"0": {"ripped_path": "job_301/rip/t000/a.mkv"}}))

    m = JobManager(settings, store=store)
    resumed = []
    monkeypatch.setattr(m, "_resume", lambda wj: resumed.append(wj.id))

    m.recover()
    assert resumed == []


def test_a_ready_file_job_is_not_taken_back(tmp_path, monkeypatch):
    """Only disc jobs need serve — a file job has nothing to rip."""
    from blurayripper.config import load_settings
    from blurayripper.persistence import MemoryStore
    from blurayripper.web.manager import JobManager

    class Durable(MemoryStore):
        @property
        def durable(self): return True

    settings = load_settings()
    settings.cluster.use_workers = True
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path)
    store = Durable()
    store.save(_rec(id=302, source=r"Z:\x\Movie.mkv", checkpoints={}))

    m = JobManager(settings, store=store)
    resumed = []
    monkeypatch.setattr(m, "_resume", lambda wj: resumed.append(wj.id))
    m.recover()
    assert resumed == []


def _drive(index, disc_name):
    from blurayripper.tools.makemkv import Drive
    return Drive(index=index, name="Test Drive", device=f"/dev/sr{index}",
                 disc_name=disc_name)


def _mgr_with_failed_disc(tmp_path, monkeypatch, *, ripped: bool):
    from blurayripper.config import load_settings
    from blurayripper.models import DiscInfo
    from blurayripper.persistence import MemoryStore
    from blurayripper.web.manager import JobManager, WebJob

    settings = load_settings()
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path)
    m = JobManager(settings, store=MemoryStore())
    wj = WebJob(id=300, source="disc:0", state=JobState.FAILED)
    wj.disc = DiscInfo(source="disc:0", disc_name="HOUSE_S1_D3", titles=[])
    wj.selected_title_ids = [0]
    if ripped:
        from blurayripper.pipeline import Job

        f = tmp_path / "ripped.mkv"
        f.write_bytes(b"x")
        job = Job(source="disc:0")
        job.disc = wj.disc
        job.selected_title_ids = [0]
        job.checkpoints = {0: {"ripped_path": str(f)}}
        wj._pipeline_job = job
    m.jobs[300] = wj
    return m, wj


def test_reinserting_a_parked_jobs_disc_matches_it_instead_of_starting_a_new_job(
        tmp_path, monkeypatch):
    """The review gate parks jobs until the disc goes back in — so the disc going
    back in must not spawn a SECOND job for it, which would then compete with the
    original for the same drive and leave the review undone."""
    m, wj = _mgr_with_failed_disc(tmp_path, monkeypatch, ripped=False)
    added = []
    monkeypatch.setattr(m, "add_disc", lambda src: added.append(src))
    m._drives_cache = [_drive(1, "HOUSE_S1_D3")]

    m._auto_open(m._drives_cache)

    assert added == []                       # no duplicate job
    assert wj.source == "disc:1"             # repointed at the drive it is in
    assert "Disc is back" in "\n".join(wj.log)
    assert m._job_dict(wj)["disc_loaded"] is True


def test_a_job_that_already_ripped_does_not_grab_the_disc(tmp_path, monkeypatch):
    """It failed after ripping, so its file is on disk. Re-ripping because the
    disc happens to be loaded would throw away work and take the drive."""
    m, wj = _mgr_with_failed_disc(tmp_path, monkeypatch, ripped=True)
    added = []
    monkeypatch.setattr(m, "add_disc", lambda src: added.append(src))

    m._auto_open([_drive(1, "HOUSE_S1_D3")])

    assert added == ["disc:1"]               # a genuinely new job, as before
    assert wj.source == "disc:0"             # untouched


def test_an_unrelated_disc_still_starts_a_new_job(tmp_path, monkeypatch):
    """Matching must be on the disc, not merely on something having failed."""
    m, _ = _mgr_with_failed_disc(tmp_path, monkeypatch, ripped=False)
    added = []
    monkeypatch.setattr(m, "add_disc", lambda src: added.append(src))

    m._auto_open([_drive(1, "SOME_OTHER_MOVIE")])

    assert added == ["disc:1"]
