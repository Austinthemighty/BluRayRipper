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


def test_claimable_states_excludes_subs_unless_worker_does_subs():
    from blurayripper.worker import _SUBS_STATES
    s = load_settings()
    s.cluster.worker_does_subs = False
    assert set(WorkerRunner(s, worker_id="w1")._claimable_states()) == set(CLAIMABLE_STATES)
    assert "extracting_subs" not in WorkerRunner(s, worker_id="w1")._claimable_states()
    s.cluster.worker_does_subs = True
    assert set(WorkerRunner(s, worker_id="w1")._claimable_states()) == (
        set(CLAIMABLE_STATES) | set(_SUBS_STATES))


def test_fully_ripped_guard(tmp_path, monkeypatch):
    s = load_settings()
    monkeypatch.setattr(s.storage, "staging_dir", tmp_path)
    r = WorkerRunner(s, worker_id="w1")

    placed = JobRecord(id=1, source="disc:0", state="ready", selected_title_ids=[0],
                       checkpoints={"0": {"placed": True}})
    assert r._fully_ripped(placed) is True             # already placed -> ready

    missing = JobRecord(id=2, source="disc:0", state="ready", selected_title_ids=[0],
                        checkpoints={"0": {"ripped_path": "job_2/rip/t000/x.mkv"}})
    assert r._fully_ripped(missing) is False           # rip file absent -> serve still ripping

    f = tmp_path / "job_2" / "rip" / "t000" / "x.mkv"
    f.parent.mkdir(parents=True)
    f.write_text("x")
    assert r._fully_ripped(missing) is True             # now present -> ready


async def test_worker_releases_job_still_being_ripped(tmp_path, monkeypatch):
    """A worker that claims a job the serve box is still ripping (claimable READY,
    but no rip file yet) must release it back, not fail it or steal it."""
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "staging_dir", tmp_path / "stage")
    monkeypatch.setattr(settings, "database_url", "memory://test")
    settings.cluster.worker_poll_seconds = 0
    # _apply_worker_settings reloads settings from disk + the store overlay, which
    # throws the line above away and leaves this test sleeping the default 5s on
    # every run of the suite. Pin the reload to these settings; the overlay
    # behaviour is covered elsewhere and is not what this test is about.
    monkeypatch.setattr("blurayripper.worker.load_settings",
                        lambda *a, **k: settings)
    runner = WorkerRunner(settings, worker_id="w1")
    runner.store = MemoryStore()
    runner.store.save(JobRecord(
        id=9, source="disc:0", state="ready", selected_title_ids=[0],
        assignments=[assignment_to_dict(EpisodeAssignment(0, MediaKind.MOVIE, "X", 2000))],
        checkpoints={"0": {"ripped_path": "job_9/rip/t000/x.mkv"}}))  # file doesn't exist

    processed = []

    async def fake_process(rec):
        processed.append(rec.id)
    monkeypatch.setattr(runner, "_process", fake_process)

    orig_release = runner.store.release

    def release_and_stop(jid, owner=None):
        orig_release(jid, owner)
        runner.request_stop()
    monkeypatch.setattr(runner.store, "release", release_and_stop)

    await runner.run()
    assert processed == []                                  # never handed to _process
    assert runner.store.claim_next("w2", ["ready"]).id == 9  # released -> claimable again
