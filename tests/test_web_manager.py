"""JobManager state-machine flow, with the pipeline's tool calls stubbed out."""

import asyncio

import pytest

from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, EpisodeAssignment, MediaKind, Title
from blurayripper.pipeline import Job, JobState, TitleResult
from blurayripper.web.manager import JobManager, WebJob


def _fake_disc():
    # Three episode-length titles -> classified TV, so the job stops at the
    # titles gate (a single-title movie disc now auto-advances past it).
    return DiscInfo(
        source="disc:0",
        disc_name="WarGames",
        titles=[
            Title(title_id=0, duration_seconds=2700, chapter_count=12, source_filename="00001.mpls"),
            Title(title_id=1, duration_seconds=2700, chapter_count=12, source_filename="00002.mpls"),
            Title(title_id=2, duration_seconds=2700, chapter_count=12, source_filename="00003.mpls"),
        ],
    )


def _single_title_disc():
    return DiscInfo(
        source="disc:0", disc_name="WarGames",
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


async def test_retry_clears_failed_live_state(manager, monkeypatch, tmp_path):
    """Retry must move the job out of 'failed' in the LIVE state (not just
    wj.state), or the UI keeps showing it failed and a re-click double-spawns."""
    calls = {"n": 0}

    async def flaky_execute(job, scheduler, report, job_name=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        from blurayripper.pipeline import TitleResult
        job.results = [TitleResult(title_id=0, video_path=tmp_path / "x.mkv")]
        return job

    monkeypatch.setattr(manager.pipeline, "execute", flaky_execute)
    wj = manager.add_disc("disc:0")
    await _settle()
    manager.set_titles(wj.id, [0])
    manager.set_mapping(wj.id, [EpisodeAssignment(0, MediaKind.MOVIE, "WarGames", 1983)])
    await _settle()
    assert manager.snapshot()["jobs"][0]["state"] == "failed"  # first run failed

    manager.retry(wj.id)
    # immediately after retry the live state must no longer read "failed"
    assert manager.snapshot()["jobs"][0]["state"] != "failed"
    # a second retry now is rejected (not failed) instead of double-spawning
    with pytest.raises(ValueError):
        manager.retry(wj.id)
    await _settle()
    assert manager.snapshot()["jobs"][0]["state"] == "done"  # retry re-ran and succeeded


async def test_prep_concurrency_throttles_recovery_stampede(manager, monkeypatch, tmp_path):
    """With prep_concurrency=1, jobs prep one at a time — so a restart that
    resumes many at once can't stampede the box."""
    manager._prep_sem = asyncio.Semaphore(1)
    active = 0
    peak = 0

    async def fake_execute(job, scheduler, report, job_name=None):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        job.results = [TitleResult(title_id=0, video_path=tmp_path / "x.mkv")]
        return job

    monkeypatch.setattr(manager.pipeline, "execute", fake_execute)

    jobs = []
    for jid in (101, 102, 103):
        wj = WebJob(id=jid, source="disc:0", state=JobState.READY)
        wj._pipeline_job = Job(source="disc:0")
        wj.selected_title_ids = [0]
        wj.assignments = {0: EpisodeAssignment(0, MediaKind.MOVIE, f"M{jid}", 2000)}
        manager.jobs[jid] = wj
        jobs.append(wj)

    await asyncio.gather(*(manager._run_job(wj) for wj in jobs))
    assert peak == 1                                  # never two prepping at once
    assert all(wj.state is JobState.DONE for wj in jobs)


async def test_auto_retry_recovers_transient_failure(manager, monkeypatch, tmp_path):
    """A job that fails once is auto-re-queued and succeeds on the retry."""
    manager.settings.auto_retry_max = 2

    async def instant(job_id, delay):  # skip the real backoff sleep
        if job_id in manager.jobs:
            manager.retry(job_id)
    monkeypatch.setattr(manager, "_auto_retry_later", instant)

    calls = {"n": 0}

    async def flaky(job, scheduler, report, job_name=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        job.results = [TitleResult(title_id=0, video_path=tmp_path / "x.mkv")]
        return job
    monkeypatch.setattr(manager.pipeline, "execute", flaky)

    wj = manager.add_disc("disc:0")
    await _settle()
    manager.set_titles(wj.id, [0])
    manager.set_mapping(wj.id, [EpisodeAssignment(0, MediaKind.MOVIE, "M", 2000)])
    await _settle()

    assert calls["n"] == 2                                    # ran again after failing
    assert manager.snapshot()["jobs"][0]["state"] == "done"   # and succeeded


async def test_auto_retry_gives_up_after_max(manager, monkeypatch):
    manager.settings.auto_retry_max = 2

    async def instant(job_id, delay):
        if job_id in manager.jobs:
            manager.retry(job_id)
    monkeypatch.setattr(manager, "_auto_retry_later", instant)

    calls = {"n": 0}

    async def always_fail(job, scheduler, report, job_name=None):
        calls["n"] += 1
        raise RuntimeError("nope")
    monkeypatch.setattr(manager.pipeline, "execute", always_fail)

    wj = manager.add_disc("disc:0")
    await _settle()
    manager.set_titles(wj.id, [0])
    manager.set_mapping(wj.id, [EpisodeAssignment(0, MediaKind.MOVIE, "M", 2000)])
    await _settle()

    assert calls["n"] == 3                                    # initial + 2 auto-retries
    assert manager.snapshot()["jobs"][0]["state"] == "failed"


def test_drives_cache_survives_restart(tmp_path, monkeypatch):
    """The last-known drive list is persisted, so a fresh JobManager (restart)
    shows it immediately instead of a blank panel / blocking scan."""
    from blurayripper.persistence import MemoryStore
    from blurayripper.tools.makemkv import Drive

    settings = load_settings()
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path)
    m1 = JobManager(settings, store=MemoryStore())
    m1._drives_cache = [Drive(0, "ASUS BW-16D1HT", "E:", "SOME_MOVIE")]
    m1._save_drives_cache()

    m2 = JobManager(settings, store=MemoryStore())  # simulate a restart
    assert [(d.index, d.device, d.disc_name) for d in m2.drives()] == [(0, "E:", "SOME_MOVIE")]


async def test_remove_many_by_state(manager):
    for jid, st in [(201, JobState.FAILED), (202, JobState.FAILED), (203, JobState.DONE)]:
        manager.jobs[jid] = WebJob(id=jid, source="disc:0", state=st)
    n = await manager.remove_many(lambda wj: manager._live_state(wj).value == "failed")
    assert n == 2
    assert set(manager.jobs) == {203}


def test_snapshot_includes_workers(manager):
    from blurayripper.persistence import WorkerInfo
    manager._workers_cache = [
        WorkerInfo("worker-A", "0.1.0+g1", 5, "Encode: working · 30 fps", 3.0),
        WorkerInfo("worker-B", "0.1.0+g1", None, "idle", 500.0),
    ]
    snap = manager.snapshot()
    w = {x["id"]: x for x in snap["workers"]}
    assert w["worker-A"]["job_id"] == 5 and w["worker-A"]["stale"] is False
    assert w["worker-B"]["job_id"] is None and w["worker-B"]["stale"] is True  # 500s > 180s


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


async def test_single_title_disc_auto_advances_to_mapping(manager, monkeypatch):
    async def fake_scan(source, report):
        from blurayripper.pipeline import Job
        job = Job(source=source)
        job.disc = _single_title_disc()
        job.fingerprint = "fp_single"
        return job

    monkeypatch.setattr(manager.pipeline, "scan_disc", fake_scan)
    wj = manager.add_disc("disc:0")
    await _settle()
    # One main-feature title — skipped the titles gate.
    assert wj.state is JobState.AWAITING_MAPPING
    assert wj.selected_title_ids == [0]


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


async def test_clustered_mode_rips_then_enqueues(manager, monkeypatch):
    manager.settings.cluster.use_workers = True
    ripped, executed = [], []

    async def fake_rip(job, staging_root, report, include_subs=True):
        ripped.append(job)

    async def fake_execute(*a, **k):
        executed.append(1)

    monkeypatch.setattr(manager.pipeline, "prep_phase", fake_rip)
    monkeypatch.setattr(manager.pipeline, "_persist_mapping", lambda job, report: None)
    monkeypatch.setattr(manager.pipeline, "execute", fake_execute)

    wj = manager.add_disc("disc:0")
    await _settle()
    manager.set_titles(wj.id, [0])
    manager.set_mapping(wj.id, [EpisodeAssignment(0, MediaKind.MOVIE, "WarGames", 1983)])
    await _settle()

    assert ripped and not executed         # ripped locally, did NOT encode inline
    assert wj.state is JobState.READY       # left queued for a worker to claim


def _patch_cluster_encode(manager, monkeypatch, heights):
    from blurayripper.pipeline import TitleResult
    encoded = []

    async def fake_prep(job, staging_root, report, include_subs=True):
        pass

    async def fake_heights(job, staging_root, report):
        return heights

    async def fake_encode(job, staging_root, scheduler, report):
        encoded.append(1)
        return [TitleResult(0, video_path=manager.settings.storage.staging_dir / "x.mkv")]

    monkeypatch.setattr(manager.pipeline, "prep_phase", fake_prep)
    monkeypatch.setattr(manager.pipeline, "title_heights", fake_heights)
    monkeypatch.setattr(manager.pipeline, "encode_phase", fake_encode)
    monkeypatch.setattr(manager.pipeline, "_persist_mapping", lambda job, report: None)
    monkeypatch.setattr("blurayripper.web.manager.library.cleanup", lambda p: None)
    return encoded


async def test_clustered_local_encode_for_sub_1080_titles(manager, monkeypatch):
    manager.settings.cluster.use_workers = True
    manager.settings.cluster.local_encode_max_height = 1080
    encoded = _patch_cluster_encode(manager, monkeypatch, [1080])

    wj = manager.add_disc("disc:0")
    await _settle()
    manager.set_titles(wj.id, [0])
    manager.set_mapping(wj.id, [EpisodeAssignment(0, MediaKind.MOVIE, "WarGames", 1983)])
    await _settle()

    assert encoded                          # encoded on the rip box
    assert wj.state is JobState.DONE


async def test_clustered_uhd_still_enqueues_for_worker(manager, monkeypatch):
    manager.settings.cluster.use_workers = True
    manager.settings.cluster.local_encode_max_height = 1080
    encoded = _patch_cluster_encode(manager, monkeypatch, [2160])

    wj = manager.add_disc("disc:0")
    await _settle()
    manager.set_titles(wj.id, [0])
    manager.set_mapping(wj.id, [EpisodeAssignment(0, MediaKind.MOVIE, "WarGames", 1983)])
    await _settle()

    assert not encoded                      # too tall — left for a worker
    assert wj.state is JobState.READY


async def test_edit_job_updates_grain_and_edition(manager, monkeypatch):
    manager.settings.cluster.use_workers = True

    async def fake_rip(job, staging_root, report, include_subs=True):
        pass

    monkeypatch.setattr(manager.pipeline, "prep_phase", fake_rip)
    monkeypatch.setattr(manager.pipeline, "_persist_mapping", lambda job, report: None)

    wj = manager.add_disc("disc:0")
    await _settle()
    manager.set_titles(wj.id, [0])
    manager.set_mapping(wj.id, [EpisodeAssignment(0, MediaKind.MOVIE, "WarGames", 1983)])
    await _settle()
    assert wj.state is JobState.READY

    manager.edit_job(wj.id, grain_mode="on", tune_grain=True, edition="Director's Cut")
    assert wj.grain_mode == "on" and wj.tune_grain is True
    assert wj._pipeline_job.grain_mode == "on"
    assert wj.assignments[0].edition == "Director's Cut"
    # the persisted record carries it for a worker to pick up
    rec = {r.id: r for r in manager.store.load_all()}[wj.id]
    assert rec.grain_mode == "on" and rec.assignments[0]["edition"] == "Director's Cut"


async def test_redo_subs_skips_busy_jobs(manager, monkeypatch):
    manager.settings.cluster.use_workers = True

    async def fake_rip(job, staging_root, report, include_subs=True):
        pass

    monkeypatch.setattr(manager.pipeline, "prep_phase", fake_rip)
    monkeypatch.setattr(manager.pipeline, "_persist_mapping", lambda job, report: None)

    wj = manager.add_disc("disc:0")
    await _settle()
    manager.set_titles(wj.id, [0])
    manager.set_mapping(wj.id, [EpisodeAssignment(0, MediaKind.MOVIE, "WarGames", 1983)])
    await _settle()
    assert wj.state is JobState.READY

    # A worker is now encoding it (live state mirrored from the store).
    wj._pipeline_job.state = JobState.ENCODING

    with pytest.raises(ValueError):
        manager.redo_subtitles(wj.id)          # single re-OCR refused while busy
    assert manager.redo_subtitles_all() == 0   # bulk re-OCR skips it


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


async def test_shutdown_cancels_running_jobs(manager, monkeypatch):
    started = asyncio.Event()

    async def blocking_execute(job, scheduler, report, job_name=None):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(manager.pipeline, "execute", blocking_execute)
    wj = manager.add_disc("disc:0")
    await _settle()
    manager.set_titles(wj.id, [0])
    manager.set_mapping(wj.id, [EpisodeAssignment(0, MediaKind.MOVIE, "WarGames", 1983)])
    await asyncio.wait_for(started.wait(), timeout=1)

    await asyncio.wait_for(manager.shutdown(), timeout=2)  # must not hang
    assert manager.closing is True
    assert all(t.done() for t in manager._tasks)


def test_auto_open_detects_new_discs(manager, monkeypatch):
    from blurayripper.tools.makemkv import Drive

    opened = []
    monkeypatch.setattr(manager, "add_disc", lambda src: opened.append(src))

    d0 = Drive(index=0, name="ASUS", device="F:", disc_name="WarGames")
    d1 = Drive(index=1, name="ASUS", device="E:", disc_name=None)  # empty drive

    manager._auto_open([d0, d1])
    assert opened == ["disc:0"]               # only the drive with a disc

    manager._auto_open([d0, d1])              # same disc still in -> no re-open
    assert opened == ["disc:0"]

    # Swap a new disc into drive 0 -> opens again.
    d0b = Drive(index=0, name="ASUS", device="F:", disc_name="2001 A Space Odyssey")
    manager._auto_open([d0b, d1])
    assert opened == ["disc:0", "disc:0"]


def test_auto_open_reinsert_same_disc_after_eject(manager, monkeypatch):
    from blurayripper.tools.makemkv import Drive

    opened = []
    monkeypatch.setattr(manager, "add_disc", lambda src: opened.append(src))
    full = Drive(index=0, name="ASUS", device="F:", disc_name="WarGames")
    empty = Drive(index=0, name="ASUS", device="F:", disc_name=None)

    manager._auto_open([full])
    manager._auto_open([empty])      # ejected -> forget it
    manager._auto_open([full])       # reinserted same disc -> opens again
    assert opened == ["disc:0", "disc:0"]


async def test_add_mkv_file_skips_rip(manager, monkeypatch, tmp_path):
    from blurayripper.tools.mkvtools import ProbedFile, SubtitleTrack

    mkv = tmp_path / "WarGames.mkv"
    mkv.write_bytes(b"data")

    async def fake_probe(path):
        return ProbedFile(
            duration_seconds=6700, video_height=2160, audio_languages=["eng"],
            subtitles=[SubtitleTrack(2, "HDMV PGS", "eng", True)],
        )

    monkeypatch.setattr(manager.pipeline.mkvtools, "probe", fake_probe)
    ids = manager.add_source(str(mkv))
    await _settle()

    assert len(ids) == 1
    wj = manager.jobs[ids[0]]
    assert wj.state is JobState.AWAITING_MAPPING       # one title -> straight to naming
    assert wj.selected_title_ids == [0]
    assert wj.disc.disc_name == "WarGames"
    # The provided file is pre-set as the "ripped" file so the rip is skipped.
    assert wj._pipeline_job.checkpoints[0]["ripped_path"] == str(mkv)


async def test_add_mkv_folder_creates_a_job_each(manager, monkeypatch, tmp_path):
    from blurayripper.tools.mkvtools import ProbedFile

    for n in ("a.mkv", "b.mkv"):
        (tmp_path / n).write_bytes(b"x")

    async def fake_probe(path):
        return ProbedFile(0, None, [], [])

    monkeypatch.setattr(manager.pipeline.mkvtools, "probe", fake_probe)
    ids = manager.add_source(str(tmp_path))
    await _settle()
    assert len(ids) == 2
    assert all(manager.jobs[i].state is JobState.AWAITING_MAPPING for i in ids)


async def test_grain_preview_gate(manager, monkeypatch, tmp_path):
    async def exec_with_grain(job, scheduler, report, job_name=None):
        from blurayripper.pipeline import GrainPreview
        preview = GrainPreview(off_png=tmp_path / "off.png", on_png=tmp_path / "on.png",
                               off_clip=tmp_path / "off.mp4", on_clip=tmp_path / "on.mp4")
        chosen = await report.resolve_grain(job, preview)
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


async def test_grain_gate_shows_without_ffmpeg_posters(manager, monkeypatch, tmp_path):
    """No ffmpeg -> no poster stills, but the clips (real preview) still gate the
    grain UI, so the preview appears instead of being silently skipped."""
    async def exec_with_grain(job, scheduler, report, job_name=None):
        from blurayripper.pipeline import GrainPreview, TitleResult
        preview = GrainPreview(off_clip=tmp_path / "off.mp4", on_clip=tmp_path / "on.mp4")
        await report.resolve_grain(job, preview)   # off_png/on_png default to None
        job.results = [TitleResult(title_id=0, video_path=tmp_path / "x.mkv")]
        return job

    monkeypatch.setattr(manager.pipeline, "execute", exec_with_grain)
    wj = manager.add_disc("disc:0")
    await _settle()
    manager.set_titles(wj.id, [0])
    manager.set_mapping(
        wj.id, [EpisodeAssignment(0, MediaKind.MOVIE, "WarGames", 1983)], grain_mode="preview")
    await _settle()
    assert wj.state is JobState.AWAITING_GRAIN
    p = manager.snapshot()["jobs"][0]["preview"]
    assert p is not None                                    # not silently skipped
    assert p["off_clip"].endswith("/preview-clip/off")      # clip is present
    assert p["off"] is None and p["on"] is None             # no posters without ffmpeg


def test_should_deinterlace_auto_targets_sd(manager):
    pipe = manager.pipeline
    pipe.settings.encode.deinterlace = "auto"
    pipe.settings.encode.deinterlace_max_height = 576
    assert pipe._should_deinterlace(480) is True    # NTSC DVD
    assert pipe._should_deinterlace(576) is True     # PAL DVD
    assert pipe._should_deinterlace(1080) is False   # HD source
    assert pipe._should_deinterlace(0) is False      # unknown -> don't
    pipe.settings.encode.deinterlace = "off"
    assert pipe._should_deinterlace(480) is False
    pipe.settings.encode.deinterlace = "on"
    assert pipe._should_deinterlace(1080) is True
