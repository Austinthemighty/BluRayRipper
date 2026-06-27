"""JobManager state-machine flow, with the pipeline's tool calls stubbed out."""

import asyncio

import pytest

from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, EpisodeAssignment, MediaKind, Title
from blurayripper.pipeline import Job, JobState
from blurayripper.web.manager import JobManager


def _fake_disc():
    return DiscInfo(
        source="disc:0",
        disc_name="WarGames",
        titles=[Title(title_id=0, duration_seconds=6762, chapter_count=12, source_filename="00002.mpls")],
    )


@pytest.fixture
def manager(tmp_path, monkeypatch):
    settings = load_settings()
    # Keep the mapping DB + any output under tmp.
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path)
    m = JobManager(settings)

    async def fake_scan(source, report):
        job = Job(source=source)
        job.disc = _fake_disc()
        job.fingerprint = "fp_test"
        report.on_event("scanned")
        return job

    executed = {}

    async def fake_execute(job, scheduler, report, job_name=None):
        executed["job"] = job
        report.on_progress("encoding", 1.0)
        from blurayripper.pipeline import TitleResult
        job.results = [TitleResult(title_id=0, video_path=tmp_path / "WarGames (1983).mkv")]
        return job

    monkeypatch.setattr(m.pipeline, "scan_disc", fake_scan)
    monkeypatch.setattr(m.pipeline, "execute", fake_execute)
    m._executed = executed
    return m


async def _settle():
    for _ in range(20):
        await asyncio.sleep(0)


async def test_full_gate_flow(manager):
    wj = manager.add_disc("disc:0")
    await _settle()
    assert wj.state is JobState.AWAITING_TITLES
    assert wj.disc.disc_name == "WarGames"

    manager.set_titles(wj.id, [0])
    assert wj.state is JobState.AWAITING_MAPPING

    manager.set_mapping(wj.id, [
        EpisodeAssignment(0, MediaKind.MOVIE, "WarGames", 1983),
    ])
    await _settle()
    assert wj.state is JobState.DONE
    assert wj.outputs and "WarGames" in wj.outputs[0]
    assert manager._executed["job"].assignments[0].show_or_movie_title == "WarGames"


async def test_set_titles_rejected_in_wrong_state(manager):
    wj = manager.add_disc("disc:0")
    await _settle()
    manager.set_titles(wj.id, [0])  # -> AWAITING_MAPPING
    with pytest.raises(ValueError):
        manager.set_titles(wj.id, [0])  # no longer awaiting titles


async def test_snapshot_shape(manager):
    wj = manager.add_disc("disc:0")
    await _settle()
    snap = manager.snapshot()
    assert snap["jobs"][0]["disc_name"] == "WarGames"
    assert snap["jobs"][0]["titles"][0]["id"] == 0


async def test_remove_parked_job(manager):
    wj = manager.add_disc("disc:0")
    await _settle()
    assert wj.state is JobState.AWAITING_TITLES
    await manager.remove(wj.id)
    assert wj.id not in manager.jobs
    assert wj.id not in {r.id for r in manager.store.load_all()}


async def test_remove_cancels_running_job(manager, monkeypatch):
    started = asyncio.Event()

    async def blocking_execute(job, scheduler, report, job_name=None):
        started.set()
        await asyncio.sleep(3600)  # hang until cancelled by remove()

    monkeypatch.setattr(manager.pipeline, "execute", blocking_execute)
    wj = manager.add_disc("disc:0")
    await _settle()
    manager.set_titles(wj.id, [0])
    manager.set_mapping(wj.id, [EpisodeAssignment(0, MediaKind.MOVIE, "WarGames", 1983)])
    await asyncio.wait_for(started.wait(), timeout=1)

    assert wj.id in manager.jobs
    await manager.remove(wj.id)
    assert wj.id not in manager.jobs
    assert wj.id not in {r.id for r in manager.store.load_all()}


async def test_retry_failed_job_resumes(manager, monkeypatch):
    calls = {"n": 0}

    async def fail_then_ok(job, scheduler, report, job_name=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        job.results = []
        return job

    monkeypatch.setattr(manager.pipeline, "execute", fail_then_ok)
    wj = manager.add_disc("disc:0")
    await _settle()
    manager.set_titles(wj.id, [0])
    manager.set_mapping(wj.id, [EpisodeAssignment(0, MediaKind.MOVIE, "WarGames", 1983)])
    await _settle()
    assert wj.state is JobState.FAILED

    manager.retry(wj.id)
    await _settle()
    assert wj.state is JobState.DONE
    assert calls["n"] == 2


async def test_interrupted_encode_becomes_ready_not_failed(manager, monkeypatch):
    class _Result:
        returncode = 3221225786  # 0xC000013A — terminated by shutdown

    class _Killed(Exception):
        result = _Result()

    async def interrupted(job, scheduler, report, job_name=None):
        raise _Killed("terminated")

    monkeypatch.setattr(manager.pipeline, "execute", interrupted)
    wj = manager.add_disc("disc:0")
    await _settle()
    manager.set_titles(wj.id, [0])
    manager.set_mapping(wj.id, [EpisodeAssignment(0, MediaKind.MOVIE, "WarGames", 1983)])
    await _settle()
    assert wj.state is JobState.READY  # resumable, not FAILED
    assert wj.error is None


async def test_grain_preview_gate(manager, monkeypatch, tmp_path):
    async def exec_with_grain(job, scheduler, report, job_name=None):
        chosen = await report.resolve_grain(job, tmp_path / "off.png", tmp_path / "on.png")
        manager._executed["grain"] = chosen
        from blurayripper.pipeline import TitleResult
        job.results = [TitleResult(title_id=0, video_path=tmp_path / "x.mkv")]
        return job

    monkeypatch.setattr(manager.pipeline, "execute", exec_with_grain)
    wj = manager.add_disc("disc:0")
    await _settle()
    manager.set_titles(wj.id, [0])
    manager.set_mapping(
        wj.id, [EpisodeAssignment(0, MediaKind.MOVIE, "WarGames", 1983)], grain_mode="preview"
    )
    await _settle()
    assert wj.state is JobState.AWAITING_GRAIN
    assert manager.snapshot()["jobs"][0]["preview"]["on"].endswith("/preview/on")

    manager.set_grain(wj.id, True)
    await _settle()
    assert wj.state is JobState.DONE
    assert manager._executed["grain"] is True
