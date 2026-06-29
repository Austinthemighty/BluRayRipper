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
    for i in range(1, 6):
        store.save(JobRecord(id=i, source=f"disc:{i}", state="ready", disc=_disc_dict(),
                             selected_title_ids=[0]))
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
    assert rec.state == "awaiting_titles"
    assert rec.disc and rec.disc["disc_name"] == "WarGames"
