"""Worker claim+process flow with the pipeline stubbed (no tools/Postgres)."""

from blurayripper.config import load_settings
from blurayripper.models import EpisodeAssignment, MediaKind
from blurayripper.persistence import JobRecord, MemoryStore, assignment_to_dict
from blurayripper.pipeline import TitleResult
from blurayripper.worker import CLAIMABLE_STATES, WorkerRunner, job_from_record


def _ready_record():
    return JobRecord(
        id=7, source="disc:0", state="ready",
        assignments=[assignment_to_dict(EpisodeAssignment(0, MediaKind.MOVIE, "WarGames", 1983))],
        selected_title_ids=[0],
        checkpoints={"0": {"ripped_path": "job_7/rip/t000/x.mkv", "placed": True}},
    )


def test_job_from_record_roundtrip():
    job = job_from_record(_ready_record())
    assert job.source == "disc:0"
    assert job.selected_title_ids == [0]
    assert job.checkpoints == {0: {"ripped_path": "job_7/rip/t000/x.mkv", "placed": True}}
    assert job.assignments[0].show_or_movie_title == "WarGames"


async def test_drain_stops_claiming_after_current_job(tmp_path, monkeypatch):
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path)
    monkeypatch.setattr(settings, "database_url", "memory://test")
    runner = WorkerRunner(settings, worker_id="w1")
    runner.store = MemoryStore()
    runner.store.save(_ready_record())

    processed = []

    async def fake_process(rec):
        processed.append(rec.id)
        runner.request_stop()  # a drain signal arrives while this job runs

    monkeypatch.setattr(runner, "_process", fake_process)
    await runner.run()

    # The claimed job ran to completion, then the loop exited without polling more.
    assert processed == [7]


async def test_worker_processes_claimed_job(tmp_path, monkeypatch):
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path)
    monkeypatch.setattr(settings.storage, "staging_dir", tmp_path / "stage")

    runner = WorkerRunner(settings, worker_id="w1")
    runner.store = MemoryStore()
    runner.store.save(_ready_record())

    async def fake_process(job, staging_root, scheduler, report):
        report.on_progress("encoding", 1.0)
        return [TitleResult(0, video_path=tmp_path / "WarGames (1983).mkv")]

    monkeypatch.setattr(runner.pipeline, "encode_phase", fake_process)

    rec = runner.store.claim_next("w1", CLAIMABLE_STATES)
    assert rec.id == 7
    await runner._process(rec)

    done = {r.id: r for r in runner.store.load_all()}[7]
    assert done.state == "done"
    assert done.outputs and "WarGames" in done.outputs[0]
    assert done.worker == "w1"


def test_worker_id_is_stable_hostname_by_default(monkeypatch):
    """Default worker id is the hostname (stable across restarts), not host:pid —
    so a restart doesn't spawn a duplicate worker or orphan its settings."""
    import socket
    from blurayripper.config import load_settings
    from blurayripper.worker import WorkerRunner
    monkeypatch.setattr(socket, "gethostname", lambda: "SteamMachine")
    assert WorkerRunner(load_settings()).worker_id == "SteamMachine"
    # An explicit --name still wins (for >1 worker per host).
    assert WorkerRunner(load_settings(), worker_id="encoder-A").worker_id == "encoder-A"
