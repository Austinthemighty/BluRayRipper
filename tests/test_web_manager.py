"""JobManager state-machine flow, with the pipeline's tool calls stubbed out."""

import asyncio

import pytest

from blurayripper import presence
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
        WorkerInfo("worker-A", "0.1.0+g1", 5, "Encode: working · 30 fps", 3.0, 7200.0),
        WorkerInfo("worker-B", "0.1.0+g1", None, "idle", 500.0, 42.0),
    ]
    snap = manager.snapshot()
    w = {x["id"]: x for x in snap["workers"]}
    assert w["worker-A"]["job_id"] == 5 and w["worker-A"]["stale"] is False
    assert w["worker-B"]["job_id"] is None and w["worker-B"]["stale"] is True  # 500s > 180s
    # Per-worker uptime (since the worker process restarted) is surfaced.
    assert w["worker-A"]["uptime"] == 7200 and w["worker-B"]["uptime"] == 42


def test_snapshot_reports_server_uptime(manager):
    snap = manager.snapshot()
    assert "uptime" in snap and isinstance(snap["uptime"], int) and snap["uptime"] >= 0


def test_stamp_log_prefixes_time_and_is_idempotent():
    import re

    from blurayripper.persistence import stamp_log
    stamped = stamp_log("Scanning disc:0 ...")
    assert re.match(r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] Scanning disc:0 \.\.\.$", stamped)
    # Re-stamping an already-stamped line (e.g. reloaded from the store) is a no-op.
    assert stamp_log(stamped) == stamped
    # An older time-only stamp is also recognized and not re-stamped.
    assert stamp_log("[12:15:07] old line") == "[12:15:07] old line"


def test_reporter_timestamps_job_log(manager):
    import re

    from blurayripper.web.manager import _Reporter
    wj = WebJob(id=1, source="disc:0")
    _Reporter(manager, wj).on_event("Found 3 titles.")
    assert wj.log and wj.log[-1].endswith("Found 3 titles.")
    assert re.match(r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] ", wj.log[-1])


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
        job.state = JobState.SCANNING  # real scan_disc leaves it here
        job.disc = _single_title_disc()
        job.fingerprint = "fp_single"
        return job

    monkeypatch.setattr(manager.pipeline, "scan_disc", fake_scan)
    wj = manager.add_disc("disc:0")
    await _settle()
    # One main-feature title — skipped the titles gate.
    assert wj.state is JobState.AWAITING_MAPPING
    assert wj.selected_title_ids == [0]
    # The API-facing state must NOT be a stale "scanning": scan_disc left the
    # pipeline job at SCANNING, and _live_state falls through to it unless the
    # parked gate is synced. Otherwise the UI shows a phantom, unclickable scan.
    assert manager._live_state(wj) is JobState.AWAITING_MAPPING
    assert wj._pipeline_job.state is JobState.AWAITING_MAPPING


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


def test_eject_clears_cached_disc_and_rearms_auto_open(manager, monkeypatch):
    """Ejecting must forget the disc, or the panel keeps showing it and putting the
    SAME disc back in looks unchanged to auto-open, so it never re-scans."""
    from blurayripper.tools.makemkv import Drive

    opened = []
    monkeypatch.setattr(manager, "add_disc", lambda src: opened.append(src))

    # Each poll re-enumerates, so the watcher hands _auto_open fresh Drive objects.
    def loaded():
        return Drive(index=0, name="ASUS", device="F:", disc_name="WarGames")

    manager._drives_cache = [loaded()]
    manager._media_snapshot = {"F:": "WarGames"}

    manager._auto_open([loaded()])
    assert opened == ["disc:0"]

    # Same disc still reported -> no duplicate scan.
    manager._auto_open([loaded()])
    assert opened == ["disc:0"]

    manager.note_disc_ejected(device="F:")
    assert manager._drives_cache[0].disc_name is None    # panel no longer shows it
    assert manager._media_snapshot == {}                  # re-insert reads as a change

    # Re-inserting the very same disc now re-triggers a scan.
    manager._auto_open([loaded()])
    assert opened == ["disc:0", "disc:0"]


def test_eject_by_index_clears_auto_open_memory(manager):
    from blurayripper.tools.makemkv import Drive

    manager._drives_cache = [Drive(index=1, name="ASUS", device="E:", disc_name="Dumbo")]
    manager._last_disc = {1: "Dumbo"}
    manager.note_disc_ejected(index=1)
    assert manager._drives_cache[0].disc_name is None
    assert 1 not in manager._last_disc


async def test_wait_for_disc_change_returns_early_on_new_media(manager, monkeypatch):
    """Closing the tray on a new disc should cut the poll wait short."""
    import blurayripper.web.manager as mgr

    manager._media_snapshot = {}
    monkeypatch.setattr(manager, "_MEDIA_PROBE_SECONDS", 0.01)
    monkeypatch.setattr(mgr, "os_media_snapshot", lambda: {"F:": "Yogi Bear"})

    await asyncio.wait_for(manager._wait_for_disc_change(30), timeout=2)
    assert manager._media_snapshot == {"F:": "Yogi Bear"}


async def test_wait_for_disc_change_sleeps_when_os_cannot_tell(manager, monkeypatch):
    """Where the OS probe is unavailable (returns None) it must not spin or bail —
    it just waits out the interval so the regular poll cadence is preserved."""
    import blurayripper.web.manager as mgr

    monkeypatch.setattr(manager, "_MEDIA_PROBE_SECONDS", 0.01)
    monkeypatch.setattr(mgr, "os_media_snapshot", lambda: None)
    await asyncio.wait_for(manager._wait_for_disc_change(0.05), timeout=2)


def test_drives_overlay_active_rip_name(manager):
    """While a rip holds a drive makemkvcon can't re-probe it, so the cached
    enumeration shows the previous disc. The active job is authoritative."""
    from blurayripper.tools.makemkv import Drive

    # Cache still shows the disc that was ripped before (stale).
    manager._drives_cache = [Drive(index=1, name="ASUS", device="E:", disc_name="Dumbo")]

    # A job is actively ripping a different disc in that same drive now.
    wj = WebJob(id=99, source="disc:1", state=JobState.RIPPING)
    wj.disc = _fake_disc()
    wj.disc.disc_name = "Disneynature Everest"
    manager.jobs[99] = wj

    (drive,) = manager.drives()
    assert drive.disc_name == "Disneynature Everest"   # live job wins over stale cache
    assert drive.index == 1 and drive.device == "E:"    # other fields preserved

    # Once the rip finishes (disc released), the cached enumeration stands again.
    wj.state = JobState.ENCODING
    (drive,) = manager.drives()
    assert drive.disc_name == "Dumbo"

    # A ripped job parked at READY for a worker has ALREADY been auto-ejected, so it
    # must not keep pinning its disc name to the drive (encode lane, not rip lane).
    wj.state = JobState.READY
    (drive,) = manager.drives()
    assert drive.disc_name == "Dumbo"


def test_display_name_identifies_which_boxset_disc(manager):
    """Every disc in a box set reports the same label ("HOUSE"), so a failed job
    can't be found on the shelf. The mapping says which season/episodes it is."""
    def job_with(*assigns, disc_name="HOUSE"):
        wj = WebJob(id=1, source="disc:1", state=JobState.FAILED)
        wj.disc = _fake_disc()
        wj.disc.disc_name = disc_name
        wj.assignments = {a.title_id: a for a in assigns}
        return wj

    def ep(tid, season, episode):
        return EpisodeAssignment(title_id=tid, kind=MediaKind.TV,
                                 show_or_movie_title="House", season=season, episode=episode)

    # A whole disc of one season -> season + episode range.
    wj = job_with(ep(1, 1, 3), ep(2, 1, 4), ep(3, 1, 5), ep(4, 1, 6))
    assert manager._display_name(wj) == "House S01E03–E06"

    # A single episode.
    assert manager._display_name(job_with(ep(1, 2, 7))) == "House S02E07"

    # Movies keep title + year.
    movie = EpisodeAssignment(title_id=0, kind=MediaKind.MOVIE,
                              show_or_movie_title="Yogi Bear", year=2010)
    assert manager._display_name(job_with(movie)) == "Yogi Bear (2010)"

    # Unmapped but auto-identified -> show + season is still enough to find it.
    wj = job_with(disc_name="HOUSE")
    wj.show_title, wj.season = "House", 4
    assert manager._display_name(wj) == "House S04"

    # Nothing known -> fall back to the disc label.
    assert manager._display_name(job_with()) == "HOUSE"


def test_lane_splits_rip_from_encode(manager):
    """READY is overloaded: a job with a live local prep task is queued to RIP;
    one without is ripped and queued to ENCODE. The lane drives the UI split."""

    class _Task:  # _lane only inspects .done()
        def __init__(self, done): self._d = done
        def done(self): return self._d

    # Ripping-side states -> rip lane.
    for st in (JobState.QUEUED, JobState.SCANNING, JobState.RIPPING,
               JobState.AWAITING_MAPPING, JobState.CONFIRMING):
        assert manager._lane(WebJob(id=1, source="disc:0", state=st), st) == "rip"

    # Encode-side states -> encode lane.
    for st in (JobState.ENCODING, JobState.PLACING):
        assert manager._lane(WebJob(id=2, source="disc:0", state=st), st) == "encode"

    # READY with a live local task = still prepping (rip); without = awaiting worker.
    ripping = WebJob(id=3, source="disc:0", state=JobState.READY)
    ripping._task = _Task(done=False)
    assert manager._lane(ripping, JobState.READY) == "rip"

    idle = WebJob(id=4, source="disc:0", state=JobState.READY)
    idle._task = _Task(done=True)  # prep finished; now waiting for a worker
    assert manager._lane(idle, JobState.READY) == "encode"
    assert manager._lane(WebJob(id=5, source="disc:0", state=JobState.READY),
                         JobState.READY) == "encode"  # no task at all

    # Resting jobs are laneless (shown in their own groups).
    assert manager._lane(WebJob(id=6, source="disc:0", state=JobState.DONE), JobState.DONE) is None


def test_rip_stall_watchdog(manager):
    """A rip that stops emitting progress/log lines for the window is a wedged
    makemkvcon and gets flagged stalled — even though its task is still 'running'."""
    import time as _time

    from blurayripper.pipeline import Job

    wj = WebJob(id=42, source="disc:0", state=JobState.RIPPING)
    wj._pipeline_job = Job(source="disc:0")
    wj._pipeline_job.state = JobState.RIPPING

    # No activity baseline recorded yet -> don't flag (rip hasn't reported at all).
    assert manager._is_stalled(wj, JobState.RIPPING) is False

    # Recent activity -> healthy.
    wj._activity_at = _time.monotonic()
    assert manager._is_stalled(wj, JobState.RIPPING) is False

    # Silent past the window -> stalled.
    wj._activity_at = _time.monotonic() - (manager.settings.rip_stall_after_seconds + 5)
    assert manager._is_stalled(wj, JobState.RIPPING) is True

    # A queued/ready job is never a rip stall regardless of activity age.
    assert manager._is_stalled(wj, JobState.READY) is False

    # Watchdog disabled -> never flags.
    manager.settings.rip_stall_after_seconds = 0
    assert manager._is_stalled(wj, JobState.RIPPING) is False


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


async def test_active_jobs_flags_only_live_local_tasks(manager):
    """active_jobs keys off a live local task, not the state — so a worker-owned
    or stale-ENCODING job (no running task here) does NOT block a restart."""
    async def _forever():
        await asyncio.sleep(3600)

    async def _noop():
        return

    live = []
    # Jobs THIS process is actively running (live task) -> flagged.
    for jid, st in [(301, JobState.ENCODING), (302, JobState.RIPPING)]:
        wj = WebJob(id=jid, source="disc:0", state=st)
        wj._task = asyncio.ensure_future(_forever())
        live.append(wj._task)
        manager.jobs[jid] = wj

    # ENCODING state but its local task already finished (delegated to a worker,
    # or orphaned by a prior crash) -> NOT flagged.
    finished = asyncio.ensure_future(_noop())
    await finished
    wj_deleg = WebJob(id=303, source="disc:0", state=JobState.ENCODING)
    wj_deleg._task = finished
    manager.jobs[303] = wj_deleg

    # Parked at a gate with no task, and a plain queued job -> NOT flagged.
    manager.jobs[304] = WebJob(id=304, source="disc:0", state=JobState.AWAITING_GRAIN)
    manager.jobs[305] = WebJob(id=305, source="disc:0", state=JobState.QUEUED)

    try:
        active = manager.active_jobs()
        assert {a["id"] for a in active} == {301, 302}
        assert all("state" in a and "name" in a for a in active)
    finally:
        for t in live:
            t.cancel()


async def test_resume_clears_stale_pipeline_state(manager):
    """_resume must reset the restored pipeline-job state (e.g. RIPPING) so the
    recovered job doesn't report a phantom in-flight state before its task runs
    (same reason retry() resets it)."""
    wj = WebJob(id=42, source="disc:0", state=JobState.RIPPING)
    job = Job(source="disc:0")
    job.state = JobState.RIPPING
    wj._pipeline_job = job
    manager.jobs[42] = wj

    manager._resume(wj)
    # Synchronously after resume (the spawned task hasn't advanced yet): the live
    # state is READY, not the stale RIPPING _live_state would otherwise surface.
    assert wj.state is JobState.READY
    assert wj._pipeline_job.state is JobState.READY
    assert manager._live_state(wj) is JobState.READY
    await _settle()


def test_is_stalled_detects_orphaned_worker_job(manager):
    from blurayripper.persistence import WorkerInfo
    wj = WebJob(id=13, source="file:x.mkv", state=JobState.ENCODING)
    manager.jobs[13] = wj

    # Store says ENCODING, but the only live worker is on a DIFFERENT job -> stalled.
    manager._workers_cache = [WorkerInfo("W1", "v", 14, "Encode", 5.0, 100.0)]
    assert manager._is_stalled(wj, JobState.ENCODING) is True

    # A live worker heartbeating THIS job -> not stalled.
    manager._workers_cache = [WorkerInfo("W1", "v", 13, "Encode", 5.0, 100.0)]
    assert manager._is_stalled(wj, JobState.ENCODING) is False

    # That worker's heartbeat has gone stale -> stalled again.
    stale = manager.settings.cluster.stale_after_seconds
    manager._workers_cache = [WorkerInfo("W1", "v", 13, "Encode", stale + 50, 100.0)]
    assert manager._is_stalled(wj, JobState.ENCODING) is True

    # A live LOCAL task on this box (e.g. rip-box local encode) -> not stalled.
    class _LiveTask:
        def done(self): return False
    wj._task = _LiveTask()
    manager._workers_cache = []
    assert manager._is_stalled(wj, JobState.ENCODING) is False
    wj._task = None

    # READY is a claimable queue slot, not active work -> never "stalled".
    assert manager._is_stalled(wj, JobState.READY) is False


def test_source_disc_missing_uses_drive_cache(manager):
    from blurayripper.tools.makemkv import Drive
    wj = WebJob(id=401, source="disc:0", state=JobState.FAILED)
    # No disc in the drive -> missing
    manager._drives_cache = [Drive(0, "ASUS", "E:", None)]
    assert manager._source_disc_missing(wj) is True
    # Disc present (even if only the OS saw it) -> not missing
    manager._drives_cache = [Drive(0, "ASUS", "E:", "SILICON_VALLEY_S2_DISC1", os_detected=True)]
    assert manager._source_disc_missing(wj) is False
    # Non-disc sources (file:/iso:) never count as "disc missing"
    assert manager._source_disc_missing(WebJob(id=402, source="file:/x.mkv")) is False


async def test_auto_retry_skipped_when_disc_missing(manager):
    from blurayripper.tools.makemkv import Drive
    manager.settings.auto_retry_max = 2
    wj = WebJob(id=403, source="disc:0", state=JobState.FAILED, error="rip failed")
    manager.jobs[403] = wj
    manager._drives_cache = [Drive(0, "ASUS", "E:", None)]   # empty drive
    manager._schedule_auto_retry(wj)
    assert manager._auto_retries.get(403, 0) == 0            # no retry scheduled
    assert any("reinsert" in l.lower() for l in wj.log)

    # With the disc present, a retry IS scheduled.
    wj2 = WebJob(id=404, source="disc:0", state=JobState.FAILED, error="rip failed")
    manager.jobs[404] = wj2
    manager._drives_cache = [Drive(0, "ASUS", "E:", "DISC")]
    manager._schedule_auto_retry(wj2)
    assert manager._auto_retries.get(404, 0) == 1


def test_rip_failure_hint_classifies_drive_vs_read_vs_disc(manager):
    from blurayripper.tools.makemkv import Drive
    # Drive dropped off the bus -> drive hint, retry skipped
    wj = WebJob(id=501, source="disc:0", state=JobState.FAILED,
                error="ProcessError: Command failed (11)")
    wj.log = ["Error 'OS error - This device does not exist' occurred while reading",
              "0 titles saved, 1 failed"]
    hint = manager._rip_failure_hint(wj)
    assert hint and "dropped" in hint.lower()

    # SCSI read error -> read hint
    wj2 = WebJob(id=502, source="disc:0", state=JobState.FAILED, error="ProcessError")
    wj2.log = ["Error 'Scsi error - ILLEGAL REQUEST' occurred while reading", "1 failed"]
    assert "read error" in manager._rip_failure_hint(wj2).lower()

    # No signature but drive cache shows no disc -> the reinsert message (via schedule)
    wj3 = WebJob(id=503, source="disc:0", state=JobState.FAILED, error="something else")
    wj3.log = ["Ripping title 0 ..."]
    manager.jobs[503] = wj3
    manager._drives_cache = [Drive(0, "ASUS", "E:", None)]  # empty drive
    manager.settings.auto_retry_max = 2
    assert manager._rip_failure_hint(wj3) is None
    manager._schedule_auto_retry(wj3)
    assert manager._auto_retries.get(503, 0) == 0  # skipped
    assert any("reinsert" in l.lower() for l in wj3.log)


def test_change_source_repoints_job_to_another_drive(manager):
    from blurayripper.pipeline import Job
    from blurayripper.models import DiscInfo
    wj = WebJob(id=601, source="disc:0", state=JobState.FAILED)
    wj._pipeline_job = Job(source="disc:0")
    wj._pipeline_job.disc = DiscInfo(source="disc:0", disc_name="X", titles=[])
    manager.jobs[601] = wj

    manager.change_source(601, "disc:1")
    assert wj.source == "disc:1"
    assert wj._pipeline_job.source == "disc:1"          # rip reads this
    assert wj._pipeline_job.disc.source == "disc:1"      # cover art / eject read this

    # Bad selector rejected
    with pytest.raises(ValueError):
        manager.change_source(601, "E:")
    # Not allowed mid-rip
    wj.state = JobState.RIPPING
    wj._pipeline_job.state = JobState.RIPPING
    with pytest.raises(ValueError):
        manager.change_source(601, "disc:0")
    # Unknown job
    with pytest.raises(KeyError):
        manager.change_source(999, "disc:1")


def test_change_source_updates_drive_device(manager):
    from blurayripper.pipeline import Job
    from blurayripper.models import DiscInfo
    from blurayripper.tools.makemkv import Drive
    manager._drives_cache = [Drive(0, "E", "E:", "MINORITY_REPORT"),
                             Drive(1, "F", "F:", "SV_S2_D1")]
    wj = WebJob(id=602, source="disc:0", state=JobState.FAILED)
    wj._pipeline_job = Job(source="disc:0")
    wj._pipeline_job.disc = DiscInfo(source="disc:0", drive_device="E:", titles=[])
    wj.disc = wj._pipeline_job.disc
    manager.jobs[602] = wj

    manager.change_source(602, "disc:1")
    # drive_device follows the new drive, so auto-eject won't eject E:'s disc
    assert wj.disc.drive_device == "F:"
    assert wj._pipeline_job.disc.drive_device == "F:"


def test_rip_pending_gates_change_drive(manager):
    from blurayripper.pipeline import Job
    def mk(source, selected, cps):
        wj = WebJob(id=700, source=source, state=JobState.FAILED)
        wj.selected_title_ids = selected
        wj._pipeline_job = Job(source=source)
        wj._pipeline_job.checkpoints = cps
        return wj
    # rip incomplete -> disc still needed (show change-drive)
    assert manager._rip_pending(mk("disc:0", [1, 5], {1: {"ripped_path": "a"}})) is True
    # every title ripped -> encode/subtitle failure, disc not needed (hide it)
    assert manager._rip_pending(
        mk("disc:0", [1, 5], {1: {"ripped_path": "a"}, 5: {"ripped_path": "b"}})) is False
    # non-disc source never offers change-drive
    assert manager._rip_pending(mk("file:/x.mkv", [0], {})) is False
    # nothing selected/ripped yet -> pending
    assert manager._rip_pending(mk("disc:0", [], {})) is True


def test_bump_job_makes_it_next_to_encode(manager):
    from blurayripper.pipeline import Job
    # Three queued jobs; bumping #3 raises its priority above the others.
    for jid in (1, 2, 3):
        wj = WebJob(id=jid, source=f"disc:{jid}", state=JobState.READY)
        wj._pipeline_job = Job(source=f"disc:{jid}")
        manager.jobs[jid] = wj

    manager.bump_job(3)
    assert manager.jobs[3].priority > manager.jobs[1].priority
    assert manager.jobs[3].priority > manager.jobs[2].priority

    # Bumping #2 now puts it ahead of the previously-bumped #3.
    manager.bump_job(2)
    assert manager.jobs[2].priority > manager.jobs[3].priority

    # Surfaced in the snapshot for the UI to order by.
    d = {j["id"]: j for j in manager.snapshot()["jobs"]}
    assert d[2]["priority"] > d[3]["priority"] > d[1]["priority"]


def test_bump_unknown_job_raises(manager):
    with pytest.raises(ValueError):
        manager.bump_job(999)


def test_done_notification_fires_once(manager):
    notes = []
    manager._notify = lambda title, msg, priority=None: notes.append((title, msg))
    wj = WebJob(id=1, source="disc:0", state=JobState.DONE, outputs=["/lib/WALL-E.mkv"])
    wj.disc = DiscInfo(source="disc:0", disc_name="WALL-E", titles=[])

    manager._maybe_notify_done(wj)
    manager._maybe_notify_done(wj)  # second call is a no-op (guarded)

    assert len(notes) == 1
    title, msg = notes[0]
    assert "Done" in title and "WALL-E" in title
    assert "1 file placed" in msg


def test_done_notification_not_refired_for_startup_history(manager):
    notes = []
    manager._notify = lambda title, msg, priority=None: notes.append(title)
    # A job already DONE at startup is pre-seeded so recovery never re-notifies.
    manager._done_notified.add(7)
    wj = WebJob(id=7, source="disc:0", state=JobState.DONE, outputs=["/lib/x.mkv"])
    manager._maybe_notify_done(wj)
    assert notes == []


async def test_requeue_recovers_a_stalled_job(manager):
    from blurayripper.persistence import WorkerInfo
    from blurayripper.pipeline import Job
    # A job stuck in a worker state with no live worker and no local task -> stalled.
    wj = WebJob(id=50, source="disc:0", state=JobState.EXTRACTING_SUBS)
    wj._pipeline_job = Job(source="disc:0")
    wj._pipeline_job.state = JobState.EXTRACTING_SUBS
    manager.jobs[50] = wj
    manager._workers_cache = [WorkerInfo("W1", "v", 99, "Encode", 5.0, 100.0)]  # on a diff job
    assert manager._is_stalled(wj, manager._live_state(wj)) is True

    manager.requeue(50)
    assert wj.state is JobState.READY
    assert wj._pipeline_job.state is JobState.READY
    assert manager._live_state(wj) is JobState.READY   # no longer stalled/leaking
    await _settle()


def test_requeue_rejects_a_non_stalled_job(manager):
    from blurayripper.pipeline import Job
    wj = WebJob(id=51, source="disc:0", state=JobState.READY)
    wj._pipeline_job = Job(source="disc:0")
    manager.jobs[51] = wj
    with pytest.raises(ValueError):
        manager.requeue(51)   # READY isn't stalled


async def test_requeue_stalled_bulk_counts(manager):
    from blurayripper.persistence import WorkerInfo
    from blurayripper.pipeline import Job
    for jid in (60, 61, 62):
        wj = WebJob(id=jid, source="disc:0", state=JobState.OCR_SUBS)
        wj._pipeline_job = Job(source="disc:0")
        wj._pipeline_job.state = JobState.OCR_SUBS
        manager.jobs[jid] = wj
    # A healthy encoding job (a live worker on it) must NOT be requeued.
    healthy = WebJob(id=63, source="disc:0", state=JobState.ENCODING)
    healthy._pipeline_job = Job(source="disc:0")
    healthy._pipeline_job.state = JobState.ENCODING
    manager.jobs[63] = healthy
    manager._workers_cache = [WorkerInfo("W1", "v", 63, "Encode", 5.0, 100.0)]

    n = manager.requeue_stalled()
    assert n == 3
    assert all(manager.jobs[j].state is JobState.READY for j in (60, 61, 62))
    assert manager.jobs[63].state is JobState.ENCODING   # untouched
    await _settle()


def test_auto_open_not_blocked_by_old_queued_job_same_selector(manager, monkeypatch):
    """A queued job from a PREVIOUS disc in the same drive (source disc:0) must not
    block auto-scanning a new disc — the selector is reused across discs."""
    from blurayripper.models import DiscInfo
    from blurayripper.tools.makemkv import Drive

    # An old, still-queued job that was ripped from drive 0 (source disc:0).
    old = WebJob(id=1, source="disc:0", state=JobState.READY)
    old.disc = DiscInfo(source="disc:0", disc_name="Jaws")
    old._pipeline_job = Job(source="disc:0")
    old._pipeline_job.state = JobState.READY
    manager.jobs[1] = old

    opened = []
    monkeypatch.setattr(manager, "add_disc", lambda src: opened.append(src))

    # A DIFFERENT disc is now in drive 0 -> must be auto-scanned despite job #1.
    manager._auto_open([Drive(index=0, name="ASUS", device="F:", disc_name="Everest")])
    assert opened == ["disc:0"]

    # But a disc matching an active job (same name) is NOT re-scanned.
    manager._last_disc.clear()
    opened.clear()
    manager._auto_open([Drive(index=0, name="ASUS", device="F:", disc_name="Jaws")])
    assert opened == []


async def test_auto_rip_confirms_single_exact_match(manager, monkeypatch):
    from blurayripper.services import metadata
    from blurayripper.web.manager import _Reporter
    manager.settings.auto_rip = True
    monkeypatch.setattr(manager.settings, "tmdb_api_key", "key")

    async def one(self, query, year=None):
        return [metadata.MovieCandidate(tmdb_id=1, title="Everest", year=2015, overview="")]
    monkeypatch.setattr(metadata.TMDBClient, "search_movie", one)

    wj = WebJob(id=1, source="disc:0")
    job = Job(source="disc:0"); job.state = JobState.SCANNING
    job.disc = DiscInfo(source="disc:0", disc_name="Everest", titles=[Title(title_id=0)])
    wj._pipeline_job = job
    manager.jobs[1] = wj

    ok = await manager._try_auto_rip(wj, job, 0, _Reporter(manager, wj))
    assert ok is True
    assert wj.state is JobState.READY
    a = wj.assignments[0]
    assert a.kind is MediaKind.MOVIE and a.show_or_movie_title == "Everest" and a.year == 2015
    await _settle()


async def test_auto_rip_parks_on_ambiguous_name(manager, monkeypatch):
    from blurayripper.services import metadata
    from blurayripper.web.manager import _Reporter
    manager.settings.auto_rip = True
    monkeypatch.setattr(manager.settings, "tmdb_api_key", "key")

    async def two(self, query, year=None):
        return [metadata.MovieCandidate(1, "Dumbo", 1941, ""),
                metadata.MovieCandidate(2, "Dumbo", 2019, "")]
    monkeypatch.setattr(metadata.TMDBClient, "search_movie", two)

    wj = WebJob(id=2, source="disc:1")
    job = Job(source="disc:1"); job.state = JobState.SCANNING
    job.disc = DiscInfo(source="disc:1", disc_name="Dumbo", titles=[Title(title_id=0)])
    wj._pipeline_job = job
    manager.jobs[2] = wj

    ok = await manager._try_auto_rip(wj, job, 0, _Reporter(manager, wj))
    assert ok is False               # ambiguous -> don't auto-pick
    assert wj.assignments == {}      # left for the user to confirm


async def test_auto_rip_off_by_default(manager):
    from blurayripper.web.manager import _Reporter
    wj = WebJob(id=3, source="disc:0")
    job = Job(source="disc:0")
    job.disc = DiscInfo(source="disc:0", disc_name="Everest", titles=[Title(title_id=0)])
    wj._pipeline_job = job
    manager.jobs[3] = wj
    assert manager.settings.auto_rip is False
    assert await manager._try_auto_rip(wj, job, 0, _Reporter(manager, wj)) is False


async def test_rename_job_moves_placed_files(manager, monkeypatch, tmp_path):
    async def _noop_art(job_id): return 0
    monkeypatch.setattr(manager, "refresh_artwork", _noop_art)
    wj = WebJob(id=1, source="disc:0", state=JobState.DONE)
    wj.assignments = {0: EpisodeAssignment(0, MediaKind.MOVIE, "Dumbo", 1941)}
    manager.jobs[1] = wj
    old = manager.pipeline._video_path_for(wj.assignments[0])
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text("v")
    (old.parent / (old.stem + ".en.srt")).write_text("s")
    (old.parent / "poster.jpg").write_text("p")
    wj.outputs = [str(old)]

    manager.rename_job(1, "Dumbo", 2019)

    new = manager.pipeline._video_path_for(wj.assignments[0])
    assert new != old and new.exists()
    assert (new.parent / (new.stem + ".en.srt")).exists()
    assert (new.parent / "poster.jpg").exists()
    assert not old.exists() and not old.parent.exists()   # old folder removed
    assert wj.outputs == [str(new)]
    assert wj.assignments[0].year == 2019
    await _settle()


async def test_rename_job_fixes_a_show_keeping_episode_numbers(manager, monkeypatch, tmp_path):
    """A show mapped to the wrong series (the 1968 'Hawaii Five-O' vs the 2010
    reboot) must be fixable: every episode gets the corrected show/year while
    keeping its season/episode numbers."""
    async def _noop_art(job_id): return 0
    monkeypatch.setattr(manager, "refresh_artwork", _noop_art)

    wj = WebJob(id=5, source="disc:1", state=JobState.READY)
    wj.assignments = {
        t: EpisodeAssignment(t, MediaKind.TV, "Hawaii Five-0", year=2010,
                             season=1, episode=8 + t)
        for t in range(3)
    }
    wj.show_title, wj.show_year, wj.show_tmdb_id = "Hawaii Five-0", 2010, 46952
    manager.jobs[5] = wj

    manager.rename_job(5, "Hawaii Five-O", 1968)

    for t in range(3):
        a = wj.assignments[t]
        assert a.show_or_movie_title == "Hawaii Five-O"
        assert a.year == 1968
        assert (a.season, a.episode) == (1, 8 + t)   # episode numbering preserved
        assert a.kind is MediaKind.TV
    # The stale TMDB id belongs to the WRONG show — dropped, so a later
    # auto-identify can't fetch reference subtitles for it again.
    assert (wj.show_title, wj.show_year, wj.show_tmdb_id) == ("Hawaii Five-O", 1968, None)
    await _settle()


async def test_rename_job_moves_placed_episode_files(manager, monkeypatch, tmp_path):
    """Renaming a show that's already placed moves each episode + its sidecars."""
    async def _noop_art(job_id): return 0
    monkeypatch.setattr(manager, "refresh_artwork", _noop_art)

    wj = WebJob(id=6, source="disc:1", state=JobState.DONE)
    wj.assignments = {
        0: EpisodeAssignment(0, MediaKind.TV, "Hawaii Five-0", year=2010, season=1, episode=8),
        1: EpisodeAssignment(1, MediaKind.TV, "Hawaii Five-0", year=2010, season=1, episode=9),
    }
    manager.jobs[6] = wj
    olds = []
    for a in wj.assignments.values():
        p = manager.pipeline._video_path_for(a)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("v")
        (p.parent / (p.stem + ".en.srt")).write_text("s")
        olds.append(p)
    wj.outputs = [str(p) for p in olds]

    manager.rename_job(6, "Hawaii Five-O", 1968)

    for a, old in zip(wj.assignments.values(), olds):
        new = manager.pipeline._video_path_for(a)
        assert new != old and new.exists()
        assert (new.parent / (new.stem + ".en.srt")).exists()
        assert not old.exists()
    assert wj.outputs == [str(manager.pipeline._video_path_for(a))
                          for a in wj.assignments.values()]
    await _settle()


def test_label_hints_prefill_season_and_disc(manager):
    """The mapping form pre-fills from the disc label instead of making you retype
    the season for every disc in a box set."""
    wj = WebJob(id=9, source="disc:1", state=JobState.AWAITING_MAPPING)
    wj.disc = _fake_disc()
    wj.disc.disc_name = "HAWAII_FIVE_0_S1_D4"
    assert manager._label_hints(wj) == {"label_season": 1, "label_disc": 4}

    wj.disc.disc_name = "Silicon Valley Season 2 Disc 1"
    assert manager._label_hints(wj) == {"label_season": 2, "label_disc": 1}

    # A movie disc says neither.
    wj.disc.disc_name = "WALL_E"
    assert manager._label_hints(wj) == {"label_season": None, "label_disc": None}

    # No disc scanned yet -> no hints, no crash.
    wj.disc = None
    assert manager._label_hints(wj) == {"label_season": None, "label_disc": None}


def test_remap_reopens_naming_for_a_job_that_was_never_named(manager):
    """A job with no naming assignment can't be renamed ("no naming assignment to
    rename") — e.g. an auto-identify disc whose subtitle match failed. remap()
    re-opens the naming form so it can be set."""
    from blurayripper.pipeline import Job

    wj = WebJob(id=247, source="disc:1", state=JobState.FAILED)
    wj.disc = _fake_disc()
    wj._pipeline_job = Job(source="disc:1")
    wj._pipeline_job.state = JobState.FAILED
    wj.selected_title_ids = [0, 1]
    wj.auto_identify = True          # identification already ran and didn't match
    wj.error = "no confident match"
    manager.jobs[247] = wj

    # Rename is impossible — that's the reported error.
    with pytest.raises(ValueError, match="no naming assignment"):
        manager.rename_job(247, "Hawaii Five-O", 1968)

    manager.remap(247)
    assert wj.state is JobState.AWAITING_MAPPING     # titles known -> straight to naming
    assert wj._pipeline_job.state is JobState.AWAITING_MAPPING
    assert wj.auto_identify is False                 # don't re-run the failed matcher
    assert wj.error is None

    # With no titles selected it goes back a step, to the title gate.
    wj2 = WebJob(id=248, source="disc:1", state=JobState.FAILED)
    wj2.disc = _fake_disc()
    wj2._pipeline_job = Job(source="disc:1")
    manager.jobs[248] = wj2
    manager.remap(248)
    assert wj2.state is JobState.AWAITING_TITLES


def test_remap_refuses_a_running_job(manager):
    """Re-opening naming mid-run would race the pipeline."""
    from blurayripper.pipeline import Job

    wj = WebJob(id=249, source="disc:0", state=JobState.ENCODING)
    wj.disc = _fake_disc()
    wj._pipeline_job = Job(source="disc:0")
    wj._pipeline_job.state = JobState.ENCODING
    manager.jobs[249] = wj
    with pytest.raises(ValueError, match="being worked on"):
        manager.remap(249)


def test_remap_works_on_a_ready_job_waiting_for_a_worker(manager):
    """The case that actually bites: an auto-identify disc finishes ripping and
    sits at READY with no assignments. It isn't done or failed, so it must still
    be nameable — and moving it off READY stops a worker claiming it meanwhile."""
    from blurayripper.pipeline import Job

    wj = WebJob(id=247, source="disc:1", state=JobState.READY)
    wj.disc = _fake_disc()
    wj._pipeline_job = Job(source="disc:1")
    wj._pipeline_job.state = JobState.READY
    wj.selected_title_ids = [0, 1]
    wj.auto_identify = True
    manager.jobs[247] = wj

    with pytest.raises(ValueError, match="no naming assignment"):
        manager.rename_job(247, "Hawaii Five-O", 1968)

    manager.remap(247)
    assert wj.state is JobState.AWAITING_MAPPING      # off READY -> unclaimable
    assert wj._pipeline_job.state is JobState.AWAITING_MAPPING
    assert wj.auto_identify is False


def test_has_naming_flag_drives_the_right_button(manager):
    """The UI picks Rename vs 'Set naming' from this flag — if it were wrong, the
    dead Rename button comes back."""
    from blurayripper.pipeline import Job

    wj = WebJob(id=246, source="disc:1", state=JobState.READY)
    wj.disc = _fake_disc()
    wj._pipeline_job = Job(source="disc:1")
    manager.jobs[246] = wj
    assert manager._job_dict(wj)["has_naming"] is False

    wj.assignments = {0: EpisodeAssignment(0, MediaKind.TV, "House", season=1, episode=1)}
    assert manager._job_dict(wj)["has_naming"] is True


def _plan(manager, monkeypatch, tmp_path, csv_text):
    """Point the manager at a throwaway config dir holding this plan CSV."""
    monkeypatch.setattr(manager.settings, "config_dir", tmp_path)
    (tmp_path / "boxsets.local.csv").write_text(csv_text, encoding="utf-8")


def _labelled_scan(manager, monkeypatch, label):
    """Make scans return a 3-episode disc carrying the given volume label."""
    async def fake_scan(source, report):
        job = Job(source=source)
        job.disc = _fake_disc()
        job.disc.disc_name = job.disc.volume_label = label
        job.fingerprint = "fp_plan"
        return job
    monkeypatch.setattr(manager.pipeline, "scan_disc", fake_scan)


async def test_boxset_plan_names_a_disc_and_skips_the_gates(manager, monkeypatch, tmp_path):
    """The whole point: a disc covered by the plan names itself and starts ripping,
    with no title gate and no mapping form."""
    _plan(manager, monkeypatch, tmp_path,
          "disc_label,show,year\nHAWAII_FIVE_0_S1_*,Hawaii Five-0,2010\n")
    _labelled_scan(manager, monkeypatch, "HAWAII_FIVE_0_S1_D4")

    wj = manager.add_disc("disc:0")
    await _settle()

    assert wj.state is not JobState.AWAITING_TITLES     # never parked
    assert wj.state is not JobState.AWAITING_MAPPING
    # Disc 4 x 3 episode titles -> E10, E11, E12 of season 1 (both off the label).
    assert [(a.title_id, a.season, a.episode) for a in wj.assignments.values()] == \
        [(0, 1, 10), (1, 1, 11), (2, 1, 12)]
    assert all(a.kind is MediaKind.TV for a in wj.assignments.values())
    assert all(a.show_or_movie_title == "Hawaii Five-0" and a.year == 2010
               for a in wj.assignments.values())
    assert wj.selected_title_ids == [0, 1, 2]
    assert any("Box-set plan" in line for line in wj.log)


async def test_boxset_plan_ignores_a_disc_it_does_not_cover(manager, monkeypatch, tmp_path):
    """A disc outside the plan must behave exactly as before — park at its gate."""
    _plan(manager, monkeypatch, tmp_path,
          "disc_label,show\nHAWAII_FIVE_0_S1_*,Hawaii Five-0\n")
    _labelled_scan(manager, monkeypatch, "SOME_OTHER_SHOW_S2_D1")

    wj = manager.add_disc("disc:0")
    await _settle()

    assert wj.state is JobState.AWAITING_TITLES
    assert not wj.assignments


async def test_boxset_plan_parks_on_an_unexpected_episode_count(manager, monkeypatch, tmp_path):
    """The safety valve: the plan declares 2 episodes but the disc has 3, so it
    stops and says why rather than shifting every episode number."""
    _plan(manager, monkeypatch, tmp_path,
          "disc_label,show,season,first_episode,episodes\n"
          "HAWAII_FIVE_0_S1_D4,Hawaii Five-0,1,13,2\n")
    _labelled_scan(manager, monkeypatch, "HAWAII_FIVE_0_S1_D4")

    wj = manager.add_disc("disc:0")
    await _settle()

    assert wj.state is JobState.AWAITING_TITLES        # parked for a human
    assert not wj.assignments
    assert any("didn't apply" in line and "3" in line for line in wj.log)


async def test_no_boxset_plan_file_changes_nothing(manager, monkeypatch, tmp_path):
    """With no plan saved, scanning behaves exactly as it did before this feature."""
    monkeypatch.setattr(manager.settings, "config_dir", tmp_path)   # empty dir
    _labelled_scan(manager, monkeypatch, "HAWAII_FIVE_0_S1_D4")

    wj = manager.add_disc("disc:0")
    await _settle()
    assert wj.state is JobState.AWAITING_TITLES


def _wi(worker_id, age, **kw):
    from blurayripper.persistence import WorkerInfo
    return WorkerInfo(worker_id=worker_id, version="v1", job_id=None,
                      stage="idle", seconds_since_seen=age, **kw)


def test_notifies_when_a_worker_goes_offline_and_returns(manager, monkeypatch):
    """Encoding silently stops when a worker dies; the point of the alert is that
    you find out without watching the panel."""
    manager.settings.cluster.offline_alert_seconds = 300
    manager._worker_watch = presence.PresenceWatch(threshold_seconds=300)
    sent = []
    monkeypatch.setattr(manager, "_notify",
                        lambda t, m, priority=None: sent.append((t, m, priority)))

    manager._workers_cache = [_wi("SteamMachine", 5)]
    manager._check_worker_presence()
    assert sent == []                                    # healthy -> silence

    manager._workers_cache = [_wi("SteamMachine", 400)]
    manager._check_worker_presence()
    assert len(sent) == 1
    assert "Worker offline" in sent[0][0] and "SteamMachine" in sent[0][0]
    assert sent[0][2] == "high"

    manager._check_worker_presence()                     # still gone
    assert len(sent) == 1                                # ...but not nagged about

    manager._workers_cache = [_wi("SteamMachine", 2)]
    manager._check_worker_presence()
    assert len(sent) == 2 and "Worker back" in sent[1][0]


def test_worker_presence_ignores_the_servers_own_heartbeat(manager, monkeypatch):
    """The rip box shares the workers table; it must not alert about itself."""
    manager.settings.cluster.offline_alert_seconds = 300
    manager._worker_watch = presence.PresenceWatch(threshold_seconds=300)
    sent = []
    monkeypatch.setattr(manager, "_notify",
                        lambda t, m, priority=None: sent.append((t, m, priority)))

    manager._workers_cache = [_wi(presence.serve_id("RIPBOX"), 5)]
    manager._check_worker_presence()
    manager._workers_cache = [_wi(presence.serve_id("RIPBOX"), 9999)]
    manager._check_worker_presence()
    assert sent == []


def test_snapshot_hides_the_servers_presence_row(manager):
    manager._workers_cache = [_wi(presence.serve_id("RIPBOX"), 1), _wi("SteamMachine", 1)]
    assert [w["id"] for w in manager.snapshot()["workers"]] == ["SteamMachine"]


def test_offline_alerts_can_be_switched_off(manager, monkeypatch):
    manager.settings.cluster.offline_alert_seconds = 0
    manager._worker_watch = presence.PresenceWatch(threshold_seconds=0)
    sent = []
    monkeypatch.setattr(manager, "_notify",
                        lambda t, m, priority=None: sent.append(t))
    manager._workers_cache = [_wi("SteamMachine", 5)]
    manager._check_worker_presence()
    manager._workers_cache = [_wi("SteamMachine", 99999)]
    manager._check_worker_presence()
    assert sent == []


def test_server_registers_its_own_presence_under_a_reserved_id(manager):
    """Workers can only spot the rip box going down if it heartbeats somewhere
    they can see — the same table, under an id the panel filters out."""
    calls = []
    manager.store.record_worker = lambda *a, **k: calls.append(a)

    manager._record_serve_presence()

    assert len(calls) == 1
    worker_id, version, job_id, stage = calls[0][:4]
    assert presence.is_serve(worker_id)          # reserved prefix -> hidden in the UI
    assert manager._hostname in worker_id
    assert job_id is None and stage == "serving"
    assert version                                # so the panel/logs show what's running


def test_disc_is_not_queued_twice_while_its_scan_is_running(manager, monkeypatch):
    """Regression: makemkvcon holds the drive during a scan, so the watcher's probe
    reads that drive as EMPTY. That made auto-open forget the disc, and the
    half-scanned job has no disc name yet to recognise it by — so the moment the
    name came back the same disc was queued a second time."""
    from blurayripper.tools.makemkv import Drive

    added = []

    def fake_add(src):
        wj = WebJob(id=263 + len(added), source=src, state=JobState.SCANNING)
        wj.disc = None                      # the scan hasn't produced a disc yet
        manager.jobs[wj.id] = wj
        added.append(src)
        return wj
    monkeypatch.setattr(manager, "add_disc", fake_add)

    loaded = Drive(index=1, name="ASUS", device="F:", disc_name="HAWAII_FIVE_0_S1_D6")
    busy = Drive(index=1, name="ASUS", device="F:", disc_name=None)  # busy scanning

    manager._auto_open([loaded])
    manager._auto_open([busy])
    manager._auto_open([loaded])
    manager._auto_open([busy])
    manager._auto_open([loaded])
    assert added == ["disc:1"]               # queued exactly once

    # Once the scan finishes, the disc name takes over as the guard.
    manager.jobs[263].disc = _fake_disc()
    manager.jobs[263].disc.disc_name = "HAWAII_FIVE_0_S1_D6"
    manager.jobs[263].state = JobState.RIPPING
    manager._auto_open([loaded])
    assert added == ["disc:1"]


def test_ejecting_still_lets_the_same_disc_be_reinserted(manager, monkeypatch):
    """The dedupe must not overshoot: a genuinely removed-and-reinserted disc has
    to queue again (that's what note_disc_ejected is for)."""
    from blurayripper.tools.makemkv import Drive

    added = []
    monkeypatch.setattr(manager, "add_disc", lambda src: added.append(src))

    def loaded():
        return Drive(index=1, name="ASUS", device="F:", disc_name="WarGames")
    empty = Drive(index=1, name="ASUS", device="F:", disc_name=None)

    manager._drives_cache = [loaded()]
    manager._auto_open([loaded()])
    assert added == ["disc:1"]

    manager.note_disc_ejected(device="F:")   # popped out
    manager._auto_open([empty])              # drive genuinely empty, no pending scan
    manager._auto_open([loaded()])           # same disc back in -> scan it again
    assert added == ["disc:1", "disc:1"]


async def test_boxset_plan_says_why_it_did_not_apply(manager, monkeypatch, tmp_path):
    """"The CSV isn't working" was indistinguishable from "no row matched this
    disc" — both fell through silently. The log now names the label and the count."""
    _plan(manager, monkeypatch, tmp_path,
          "disc_label,show\nHAWAII_FIVE_0_S1_*,Hawaii Five-0\n")
    _labelled_scan(manager, monkeypatch, "THE_SOPRANOS_S2_D3")

    wj = manager.add_disc("disc:0")
    await _settle()

    log = "\n".join(wj.log)
    assert "1 row(s) loaded but none match" in log
    assert "THE_SOPRANOS_S2_D3" in log          # so you can fix the pattern
    assert wj.state is JobState.AWAITING_TITLES  # still parks normally


async def test_boxset_plan_says_when_no_rows_loaded(manager, monkeypatch, tmp_path):
    """An empty/unparseable plan must say where it looked, not stay quiet."""
    _plan(manager, monkeypatch, tmp_path, "")     # file exists but has no rows
    _labelled_scan(manager, monkeypatch, "HAWAII_FIVE_0_S1_D4")

    wj = manager.add_disc("disc:0")
    await _settle()

    log = "\n".join(wj.log)
    assert "no rows loaded from" in log and "boxsets.local.csv" in log
