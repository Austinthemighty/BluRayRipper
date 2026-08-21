"""JobManager edge cases: the untaken side of its guards.

test_web_manager.py walks the happy paths. This file pushes the conditionals the
other way — the drive watcher's failure/blind-drive handling, the store poll's
error de-duplication, the command guards that must reject instead of crashing,
and the naming/lane/artwork helpers' fallbacks.
"""

import asyncio
import time

import pytest

import blurayripper.web.manager as mgr
from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, EpisodeAssignment, MediaKind, Title
from blurayripper.persistence import JobRecord, MemoryStore
from blurayripper.pipeline import Job, JobState, TitleResult
from blurayripper.tools.makemkv import Drive
from blurayripper.web.manager import EventBus, JobManager, WebJob, _Reporter


def _tv_disc(disc_name="HOUSE"):
    return DiscInfo(
        source="disc:0",
        disc_name=disc_name,
        titles=[
            Title(title_id=0, duration_seconds=2700, chapter_count=8, source_filename="00001.mpls"),
            Title(title_id=1, duration_seconds=2700, chapter_count=8, source_filename="00002.mpls"),
            Title(title_id=2, duration_seconds=2700, chapter_count=8, source_filename="00003.mpls"),
        ],
    )


def _movie_disc(disc_name="WarGames"):
    return DiscInfo(
        source="disc:0", disc_name=disc_name,
        titles=[Title(title_id=0, duration_seconds=6762, chapter_count=12,
                      source_filename="00002.mpls")],
    )


@pytest.fixture
def manager(tmp_path, monkeypatch):
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path / "library")
    monkeypatch.setattr(settings.storage, "staging_dir", tmp_path / "staging")
    m = JobManager(settings, store=MemoryStore())

    async def fake_scan(source, report):
        job = Job(source=source)
        job.disc = _tv_disc()
        job.fingerprint = "fp_test"
        return job

    monkeypatch.setattr(m.pipeline, "scan_disc", fake_scan)
    return m


async def _settle():
    for _ in range(30):
        await asyncio.sleep(0)


def _job(manager, job_id, source="disc:0", state=JobState.QUEUED, disc=None):
    wj = WebJob(id=job_id, source=source, state=state)
    wj.disc = disc
    job = Job(source=source)
    job.disc = disc
    job.state = state
    wj._pipeline_job = job
    manager.jobs[job_id] = wj
    return wj


# --- event bus ------------------------------------------------------------


def test_publish_ticks_every_live_subscriber_and_stops_after_unsubscribe():
    """The SSE handler re-reads the snapshot only when its queue gets a tick, so a
    publish that reaches nobody is a UI that silently stops updating."""
    bus = EventBus()
    a, b = bus.subscribe(), bus.subscribe()

    bus.publish()
    assert a.qsize() == 1 and b.qsize() == 1   # both clients woken

    bus.unsubscribe(a)
    bus.publish()
    assert a.qsize() == 1                      # a disconnected client gets nothing more
    assert b.qsize() == 2                      # the remaining one still ticks


# --- reporter -------------------------------------------------------------


def test_stage_only_progress_keeps_the_bar_where_it_was(manager):
    """The pipeline reports a stage change with no fraction ("placing"). Treating
    that as 0 would slam a nearly-finished bar back to the start."""
    wj = _job(manager, 1)
    rep = _Reporter(manager, wj)

    rep.on_progress("encoding", 0.75)
    rep.on_progress("placing", None)

    assert wj.stage == "placing"
    assert wj.progress == 0.75


# --- drive watcher --------------------------------------------------------


def _one_watch_pass(manager, monkeypatch):
    """Run exactly one iteration of _watch_drives."""
    async def stop(timeout):
        manager.closing = True
    monkeypatch.setattr(manager, "_wait_for_disc_change", stop)


async def test_drive_watch_publishes_the_scan_and_auto_opens(manager, monkeypatch):
    """One good watcher pass must fill the cache, mark the box as having looked,
    and hand the drives to auto-open — that is the whole insert-a-disc path."""
    monkeypatch.setattr(mgr, "os_media_snapshot", lambda: {"F:": "HOUSE"})
    drives = [Drive(index=0, name="ASUS", device="F:", disc_name="HOUSE")]

    async def list_drives():
        return drives
    monkeypatch.setattr(manager, "list_drives", list_drives)
    opened = []
    monkeypatch.setattr(manager, "_auto_open", lambda d: opened.append(d))
    manager.settings.auto_open = True
    _one_watch_pass(manager, monkeypatch)

    await manager._watch_drives()

    assert manager._drives_cache == drives
    assert manager._drives_scanned is True
    assert opened == [drives]
    assert manager._drives_cache_file.exists()   # panel stays warm across a restart


async def test_drive_watch_leaves_discs_alone_when_auto_open_is_off(manager, monkeypatch):
    """auto_open=false is how you stop the server grabbing every disc you insert;
    the watcher must still refresh the panel."""
    monkeypatch.setattr(mgr, "os_media_snapshot", lambda: None)
    drives = [Drive(index=0, name="ASUS", device="F:", disc_name="HOUSE")]

    async def list_drives():
        return drives
    monkeypatch.setattr(manager, "list_drives", list_drives)
    opened = []
    monkeypatch.setattr(manager, "_auto_open", lambda d: opened.append(d))
    manager.settings.auto_open = False
    _one_watch_pass(manager, monkeypatch)

    await manager._watch_drives()

    assert opened == []                          # no job was started for the disc
    assert manager._drives_cache == drives       # but the panel still updated


async def test_drive_scan_failure_keeps_the_last_cache_and_logs_once(manager, monkeypatch, capsys):
    """An unresponsive drive raises every poll. Blanking the panel loses the only
    record of what was attached, and logging each tick buries the real cause."""
    warm = [Drive(index=0, name="ASUS", device="F:", disc_name="HOUSE")]
    manager._drives_cache = list(warm)
    monkeypatch.setattr(mgr, "os_media_snapshot", lambda: None)

    async def boom():
        raise OSError("drive not ready")
    monkeypatch.setattr(manager, "list_drives", boom)

    passes = {"n": 0}

    async def two_passes(timeout):
        passes["n"] += 1
        if passes["n"] >= 2:
            manager.closing = True
    monkeypatch.setattr(manager, "_wait_for_disc_change", two_passes)

    await manager._watch_drives()

    assert manager._drives_cache == warm         # last known drives survive
    assert manager._drives_scanned is False      # never enumerated successfully
    out = capsys.readouterr().out
    assert out.count("[drives] scan failed") == 1   # same cause, said once
    assert "drive not ready" in out


async def test_a_different_drive_error_is_reported_again(manager, monkeypatch, capsys):
    """The de-duplication is per message: a NEW failure mode must not be swallowed
    just because the drive was already failing for another reason."""
    monkeypatch.setattr(mgr, "os_media_snapshot", lambda: None)
    errors = [OSError("drive not ready"), RuntimeError("makemkvcon missing")]

    async def boom():
        raise errors[min(passes["n"], 1)]
    monkeypatch.setattr(manager, "list_drives", boom)

    passes = {"n": 0}

    async def two_passes(timeout):
        passes["n"] += 1
        if passes["n"] >= 2:
            manager.closing = True
    monkeypatch.setattr(manager, "_wait_for_disc_change", two_passes)

    await manager._watch_drives()

    out = capsys.readouterr().out
    assert "drive not ready" in out and "makemkvcon missing" in out


async def test_an_unchanged_tray_waits_out_the_poll_interval(manager, monkeypatch):
    """The early return exists for a tray that just closed. If an UNCHANGED probe
    also returned early, the watcher would re-run makemkvcon in a tight loop."""
    monkeypatch.setattr(mgr.JobManager, "_MEDIA_PROBE_SECONDS", 0)
    manager._media_snapshot = {"F:": "HOUSE"}
    probes = {"n": 0}

    def probe():
        probes["n"] += 1
        if probes["n"] > 50:
            raise AssertionError("probe loop never reached its deadline")
        return {"F:": "HOUSE"}                   # same media, every time
    monkeypatch.setattr(mgr, "os_media_snapshot", probe)

    started = time.monotonic()
    await manager._wait_for_disc_change(0.05)

    assert probes["n"] >= 2                      # kept probing rather than returning
    assert time.monotonic() - started >= 0.05    # waited the whole interval


async def test_disc_wait_gives_up_immediately_on_shutdown(manager, monkeypatch):
    """Ctrl+C must not be held up for the rest of a 15s poll interval."""
    monkeypatch.setattr(mgr.JobManager, "_MEDIA_PROBE_SECONDS", 0)
    manager._media_snapshot = {"F:": "HOUSE"}
    probes = {"n": 0}

    def probe():
        probes["n"] += 1
        if probes["n"] > 5:
            raise AssertionError("kept waiting after the manager started closing")
        manager.closing = True
        return {"F:": "HOUSE"}
    monkeypatch.setattr(mgr, "os_media_snapshot", probe)

    await manager._wait_for_disc_change(3600)    # would hang if closing were ignored

    assert probes["n"] == 1


def test_ejecting_one_drive_leaves_the_other_drives_alone(manager):
    """Two-drive boxes are normal. Clearing every cached disc on one eject would
    blank the panel for a disc that is still sitting in the other drive."""
    manager._drives_cache = [
        Drive(index=0, name="ASUS", device="F:", disc_name="HOUSE_S1_D1"),
        Drive(index=1, name="LG", device="G:", disc_name="WarGames"),
    ]
    manager._last_disc = {0: "HOUSE_S1_D1", 1: "WarGames"}

    manager.note_disc_ejected(device="F:")

    assert manager._drives_cache[0].disc_name is None
    assert manager._drives_cache[1].disc_name == "WarGames"
    assert manager._last_disc == {1: "WarGames"}


def test_a_drive_makemkv_cannot_open_is_never_auto_ripped(manager, monkeypatch):
    """The OS can see a disc in a drive makemkvcon can't open. Queuing it starts a
    job that can only fail, and buries the real problem in a failed-job card."""
    added = []
    monkeypatch.setattr(manager, "add_disc", lambda src: added.append(src))

    blind = Drive(index=-1, name="ASUS", device="F:", disc_name="HOUSE_S1_D1",
                  makemkv_blind=True)
    manager._auto_open([blind])

    assert added == []
    assert manager._last_disc == {}   # not remembered either, so a fix re-triggers


def test_a_failed_scan_does_not_block_the_next_disc_in_that_drive(manager, monkeypatch):
    """The "scan still running" guard matches on the drive selector and ignores the
    disc name (there isn't one yet). Without the done/failed exemption, one failed
    scan would stop that drive auto-opening anything, forever."""
    added = []
    monkeypatch.setattr(manager, "add_disc", lambda src: added.append(src))

    dead = _job(manager, 7, source="disc:1", state=JobState.FAILED)
    dead.disc = None                       # it failed before producing disc info
    dead._pipeline_job.state = JobState.FAILED

    manager._auto_open([Drive(index=1, name="ASUS", device="G:", disc_name="WarGames")])

    assert added == ["disc:1"]


# --- store poll -----------------------------------------------------------


def _poll_for(manager, ticks):
    """Stop _refresh_from_store after ``ticks`` iterations."""
    manager.settings.cluster.worker_poll_seconds = 0
    seen = {"n": 0}
    real = manager.store.list_workers

    def counted():
        seen["n"] += 1
        if seen["n"] >= ticks:
            manager.closing = True
        return real()
    manager.store.list_workers = counted
    return seen


async def test_store_read_failures_are_logged_once_and_the_poll_recovers(manager, capsys):
    """A store outage otherwise makes the UI silently stop reflecting worker
    progress. Say why once — then say when it comes back, so you know it's over."""
    calls = {"n": 0}

    def load_all():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("connection refused")
        return []
    manager.store.load_all = load_all
    _poll_for(manager, 3)

    await manager._refresh_from_store()

    out = capsys.readouterr().out
    assert out.count("[store] load_all failed") == 1     # two failures, one message
    assert "connection refused" in out
    assert out.count("reconnected") == 1                 # and the recovery is announced


async def test_worker_progress_lands_on_the_job_and_pushes_the_done_alert(manager):
    """Encoding happens on another box; the store is the only channel that carries
    its progress back. Missing the DONE transition means no "finished" push."""
    wj = _job(manager, 3, state=JobState.ENCODING)
    wj._pipeline_job.state = JobState.ENCODING
    rec = JobRecord(id=3, source="disc:0", state="done", stage="placing",
                    progress=1.0, log=["worker: placed"],
                    outputs=["/library/WarGames (1983).mkv"])
    manager.store.load_all = lambda: [rec]
    sent = []
    manager._notify = lambda t, m, priority=None: sent.append(t)
    _poll_for(manager, 3)

    await manager._refresh_from_store()

    assert wj.state is JobState.DONE
    assert wj._pipeline_job.state is JobState.DONE   # so _live_state agrees
    assert wj.outputs == ["/library/WarGames (1983).mkv"]
    assert wj.log == ["worker: placed"]
    assert len([t for t in sent if t.startswith("✅ Done")]) == 1  # not once per poll


async def test_a_state_this_version_does_not_know_is_skipped_quietly(manager, capsys):
    """A newer worker (or a hand-edited row) can write a state this build has no
    member for. Letting it escape aborts the whole tick — every other job's progress
    stops too — and reports a store outage that isn't happening."""
    wj = _job(manager, 4, state=JobState.ENCODING)
    wj._pipeline_job.state = JobState.ENCODING
    wj.stage = "encode"
    manager.store.load_all = lambda: [
        JobRecord(id=4, source="disc:0", state="transcoding_v2", stage="clobbered")]
    _poll_for(manager, 2)

    await manager._refresh_from_store()

    assert wj.state is JobState.ENCODING     # left exactly as it was
    assert wj.stage == "encode"
    assert "load_all failed" not in capsys.readouterr().out


async def test_a_locally_owned_job_is_not_clobbered_by_the_store(manager):
    """The rip box owns ripping/awaiting jobs. Letting a stale store row write over
    them would drag a job that is mid-rip here back to whatever a worker last saw."""
    wj = _job(manager, 5, state=JobState.RIPPING)
    wj._pipeline_job.state = JobState.RIPPING
    manager.store.load_all = lambda: [
        JobRecord(id=5, source="disc:0", state="failed", error="stale row")]
    _poll_for(manager, 2)

    await manager._refresh_from_store()

    assert wj.state is JobState.RIPPING
    assert wj.error is None


# --- job lookup / naming helpers -----------------------------------------


def test_a_job_with_no_disc_label_is_not_reported_as_loaded(manager):
    """Empty drives report no disc name. Comparing blank-to-blank would mark a
    label-less job "disc in the drive" on a box with no disc in it at all."""
    wj = _job(manager, 8, disc=DiscInfo(source="disc:0", disc_name="", titles=[]))
    manager._drives_cache = [Drive(index=0, name="ASUS", device="F:", disc_name=None)]

    assert manager._disc_is_loaded(wj) is False

    wj.disc.disc_name = "HOUSE"
    manager._drives_cache = [Drive(index=0, name="ASUS", device="F:", disc_name="HOUSE")]
    assert manager._disc_is_loaded(wj) is True


def test_display_name_falls_back_to_the_label_when_the_mapping_has_no_title(manager):
    """confirm_match builds assignments from ``wj.show_title or ""``, so an
    unnamed show yields blank-titled assignments — the notification would just
    read " S01E03" with nothing to identify the disc by."""
    wj = _job(manager, 9, disc=_tv_disc("HOUSE_S1_D1"))
    wj.assignments = {0: EpisodeAssignment(title_id=0, kind=MediaKind.TV,
                                           show_or_movie_title="  ", season=1, episode=3)}

    assert manager._display_name(wj) == "HOUSE_S1_D1"


def test_display_name_spans_seasons_for_a_mixed_disc(manager):
    """The last disc of a box set often carries the end of one season and the start
    of the next. One season number would name it wrong on the shelf."""
    wj = _job(manager, 10, disc=_tv_disc())

    def ep(tid, season, episode):
        return EpisodeAssignment(title_id=tid, kind=MediaKind.TV,
                                 show_or_movie_title="House", season=season, episode=episode)

    wj.assignments = {a.title_id: a for a in (ep(0, 1, 22), ep(1, 2, 1), ep(2, 2, 2))}
    assert manager._display_name(wj) == "House S01–S02E01–E22"


def test_display_name_omits_the_season_when_the_mapping_has_none(manager):
    """A TV assignment can reach here with episode numbers but no season (an
    identify run that never resolved one). It must still name the disc, not crash."""
    wj = _job(manager, 11, disc=_tv_disc())
    wj.assignments = {0: EpisodeAssignment(title_id=0, kind=MediaKind.TV,
                                           show_or_movie_title="House", episode=3)}

    assert manager._display_name(wj) == "House E03"


def test_lane_follows_who_actually_does_the_subtitles(manager):
    """The queue split is by machine, not by stage: with worker_does_subs on, OCR
    runs on the worker, so showing it in the rip queue misreports what the rip box
    is busy with."""
    wj = _job(manager, 12, state=JobState.OCR_SUBS)

    manager.settings.cluster.worker_does_subs = False
    assert manager._lane(wj, JobState.OCR_SUBS) == "rip"

    manager.settings.cluster.worker_does_subs = True
    assert manager._lane(wj, JobState.OCR_SUBS) == "encode"
    assert manager._lane(wj, JobState.EXTRACTING_SUBS) == "encode"


# --- artwork -------------------------------------------------------------


def test_artwork_prefers_the_poster_and_falls_back_to_fanart(manager, monkeypatch, tmp_path):
    """The card thumbnail should be the poster; fanart is a wide backdrop and only
    stands in when no poster was extracted."""
    folder = tmp_path / "House (2004)"
    folder.mkdir()
    monkeypatch.setattr(manager.pipeline, "_poster_folder", lambda a: folder)
    wj = _job(manager, 13)
    wj.assignments = {0: EpisodeAssignment(title_id=0, kind=MediaKind.MOVIE,
                                           show_or_movie_title="House", year=2004)}

    assert manager.artwork_path(13) is None          # nothing extracted yet

    (folder / "fanart.png").write_bytes(b"x")
    assert manager.artwork_path(13) == folder / "fanart.png"

    (folder / "poster.jpg").write_bytes(b"x")
    assert manager.artwork_path(13) == folder / "poster.jpg"


def test_artwork_is_none_for_a_job_that_was_never_named(manager):
    """The card asks for a thumbnail before the naming gate is answered; there is
    no library folder to look in yet, so this must answer, not raise."""
    _job(manager, 14)                                # exists, but has no assignments

    assert manager.artwork_path(14) is None
    assert manager.artwork_path(9999) is None        # and a removed job is fine too


async def test_refresh_artwork_is_a_no_op_for_a_job_with_no_naming(manager, monkeypatch):
    """The card offers "refresh art" before the naming gate is answered; there is
    no TMDB identity to fetch for yet, so it must answer 0, not crash."""
    calls = []

    async def art(assignments, report):
        calls.append(assignments)
        return 1
    monkeypatch.setattr(manager.pipeline, "place_tmdb_art", art)
    _job(manager, 20)                                     # no assignments

    assert await manager.refresh_artwork(20) == 0
    assert await manager.refresh_artwork(9999) == 0       # and a removed job
    assert calls == []


async def test_refresh_all_artwork_only_touches_named_jobs(manager, monkeypatch):
    """"Refresh all art" runs across the whole queue. Handing TMDB an empty
    assignment list for every unnamed job is a wasted round trip per job."""
    calls = []

    async def art(assignments, report):
        calls.append([a.show_or_movie_title for a in assignments])
        return 2
    monkeypatch.setattr(manager.pipeline, "place_tmdb_art", art)

    named = _job(manager, 21)
    named.assignments = {0: EpisodeAssignment(title_id=0, kind=MediaKind.MOVIE,
                                              show_or_movie_title="WarGames", year=1983)}
    _job(manager, 22)                                     # never named

    total = await manager.refresh_all_artwork()

    assert calls == [["WarGames"]]
    assert total == 2


# --- add / group ----------------------------------------------------------


async def test_add_source_accepts_a_bare_drive_selector(manager):
    """The Add box takes what the Drives panel shows ("disc:1"). Falling through to
    the path branch turns that into "Nothing at disc:1"."""
    ids = manager.add_source("disc:1")
    await _settle()

    assert len(ids) == 1
    assert manager.jobs[ids[0]].source == "disc:1"


async def test_a_folder_with_nothing_rippable_names_the_folder(manager, tmp_path):
    """A folder with no .mkv and no BDMV/VIDEO_TS is a typo'd path or the wrong
    level of the tree — say which folder was searched, not "nothing at ..."."""
    empty = tmp_path / "Season 1"
    empty.mkdir()

    with pytest.raises(ValueError) as exc:
        manager.add_source(str(empty))

    assert "No .mkv files found" in str(exc.value)
    assert str(empty) in str(exc.value)


def _mkv_jobs(manager, monkeypatch, folder, names):
    from blurayripper.tools.mkvtools import ProbedFile

    folder.mkdir(parents=True, exist_ok=True)
    for n in names:
        (folder / n).write_bytes(b"x" * 8)

    async def probe(path):
        return ProbedFile(duration_seconds=1320, video_height=1080,
                          audio_languages=["eng"], subtitles=[])
    monkeypatch.setattr(manager.pipeline.mkvtools, "probe", probe)
    return [manager._add_mkv(folder / n).id for n in names]


async def test_group_season_folds_sibling_episode_jobs_into_one(manager, monkeypatch, tmp_path):
    """Files added one at a time sit as N jobs all wanting the same show name typed
    N times. Grouping must actually retire the originals, or you name them twice."""
    ids = _mkv_jobs(manager, monkeypatch, tmp_path / "s1",
                    ["Poirot Series 1_t01.mkv", "Poirot Series 1_t02.mkv"])
    await _settle()

    new_id = await manager.group_season(ids[0])
    await _settle()

    assert set(manager.jobs) == {new_id}                 # the originals are gone
    grouped = manager.jobs[new_id]
    assert len(grouped.disc.titles) == 2
    assert grouped.state is JobState.AWAITING_MAPPING
    assert all(str(i) in "\n".join(grouped.log) for i in ids)   # says what it folded
    # The files themselves are untouched — grouping is a queue operation.
    assert (tmp_path / "s1" / "Poirot Series 1_t01.mkv").exists()


async def test_group_season_refuses_files_that_are_not_one_show(manager, monkeypatch, tmp_path):
    """Grouping unrelated films would name each as an episode of the others. The
    heuristic must refuse rather than guess."""
    ids = _mkv_jobs(manager, monkeypatch, tmp_path / "To Process",
                    ["Black Hawk Down (2001).mkv", "Bumblebee (2018).mkv"])
    await _settle()

    with pytest.raises(ValueError) as exc:
        await manager.group_season(ids[0])

    assert "one show's episodes" in str(exc.value)
    assert set(manager.jobs) == set(ids)                 # nothing was removed


async def test_group_season_on_a_removed_job_says_so(manager):
    """The button can be clicked from a stale page after the job was removed."""
    with pytest.raises(ValueError, match="No such job"):
        await manager.group_season(9999)


# --- requeue / retry ------------------------------------------------------


async def test_requeue_kills_the_wedged_rip_before_re_running(manager, monkeypatch):
    """A stalled rip is usually a wedged makemkvcon still holding the drive. Starting
    the replacement rip first makes the two fight over the same drive."""
    order = []

    async def wedged():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            order.append("old-rip-cancelled")
            raise

    async def fake_run(wj):
        order.append("re-ran")
    monkeypatch.setattr(manager, "_run_job", fake_run)

    wj = _job(manager, 30, state=JobState.RIPPING)
    wj._pipeline_job.state = JobState.RIPPING
    wj._activity_at = time.monotonic() - (manager.settings.rip_stall_after_seconds + 60)
    wj._task = asyncio.create_task(wedged())
    await _settle()

    manager.requeue(30)
    await _settle()

    assert order == ["old-rip-cancelled", "re-ran"]
    assert wj.state is JobState.READY


def test_requeue_refuses_a_job_it_cannot_resume(manager):
    """A job restored without scan state has no checkpoints to resume from, so
    "requeue" would only produce an unhandled error on the button."""
    with pytest.raises(ValueError, match="No job"):
        manager.requeue(9999)

    wj = WebJob(id=31, source="disc:0", state=JobState.ENCODING)   # no _pipeline_job
    manager.jobs[31] = wj
    assert manager._is_stalled(wj, manager._live_state(wj)) is True

    with pytest.raises(ValueError, match="no scan state"):
        manager.requeue(31)


def test_retry_refuses_a_failed_job_with_no_scan_state(manager):
    """Same for Retry: without a pipeline job there is nothing to re-run."""
    wj = WebJob(id=32, source="disc:0", state=JobState.FAILED)
    manager.jobs[32] = wj

    with pytest.raises(ValueError, match="no scan state"):
        manager.retry(32)

    assert wj.state is JobState.FAILED


# --- gate commands --------------------------------------------------------


def test_confirming_with_nothing_ticked_is_rejected(manager):
    """An empty selection would advance the job to naming with no titles to name,
    and then rip nothing at all."""
    wj = _job(manager, 33, state=JobState.AWAITING_TITLES, disc=_tv_disc())

    with pytest.raises(ValueError, match="No titles selected"):
        manager.set_titles(33, [])

    assert wj.state is JobState.AWAITING_TITLES


def test_set_mapping_refuses_a_job_that_is_not_at_a_gate(manager):
    """A stale browser tab can post the naming form for a job that has since
    started encoding; applying it would restart the job under a new name."""
    wj = _job(manager, 34, state=JobState.ENCODING, disc=_movie_disc())

    with pytest.raises(ValueError, match="not awaiting a naming mapping"):
        manager.set_mapping(34, [EpisodeAssignment(title_id=0, kind=MediaKind.MOVIE,
                                                   show_or_movie_title="WarGames", year=1983)])

    assert wj.state is JobState.ENCODING and wj.assignments == {}


def test_rename_refuses_a_blank_title(manager):
    """An empty title would place the file into a library folder called "" —
    invisible in Plex and awkward to undo."""
    wj = _job(manager, 35, state=JobState.DONE, disc=_movie_disc())
    original = EpisodeAssignment(title_id=0, kind=MediaKind.MOVIE,
                                 show_or_movie_title="WarGames", year=1983)
    wj.assignments = {0: original}

    with pytest.raises(ValueError, match="A title is required"):
        manager.rename_job(35, "   ", 1983)

    assert wj.assignments[0] is original

    with pytest.raises(ValueError, match="No job"):
        manager.rename_job(9999, "WarGames", 1983)


async def test_remap_refuses_a_ready_job_whose_prep_is_still_running(manager):
    """READY spans "queued for a worker" and "still prepping here". Re-opening the
    naming form for the second kind edits a job that is mid-rip."""
    release = asyncio.Event()
    wj = _job(manager, 36, state=JobState.READY, disc=_movie_disc())
    wj.selected_title_ids = [0]
    wj._task = asyncio.create_task(release.wait())
    await _settle()

    with pytest.raises(ValueError, match="still running"):
        manager.remap(36)

    assert wj.state is JobState.READY
    release.set()
    await _settle()


def test_remap_refuses_a_job_that_never_finished_scanning(manager):
    """A scan that failed leaves no disc, so there are no titles to re-map — the
    fix is Retry or Remove, not the naming form."""
    wj = WebJob(id=37, source="disc:0", state=JobState.FAILED)
    manager.jobs[37] = wj

    with pytest.raises(ValueError, match="no scanned disc"):
        manager.remap(37)

    assert wj.state is JobState.FAILED


def test_edit_refuses_a_finished_job(manager):
    """Grain and edition only affect an encode that hasn't happened. Accepting them
    on a placed title silently promises a change that will never be made."""
    wj = _job(manager, 38, state=JobState.DONE, disc=_movie_disc())
    wj.grain_mode = "auto"

    with pytest.raises(ValueError, match="already finished"):
        manager.edit_job(38, grain_mode="on", tune_grain=True)

    assert wj.grain_mode == "auto"


def test_an_edition_is_only_stamped_on_movies(manager):
    """"Edition" is a Plex movie concept — "Show S01E02 {edition-...}" is not a
    filename Plex understands, so a TV assignment must ignore it."""
    wj = _job(manager, 39, state=JobState.READY, disc=_tv_disc())
    tv = EpisodeAssignment(title_id=0, kind=MediaKind.TV,
                           show_or_movie_title="House", season=1, episode=1)
    movie = EpisodeAssignment(title_id=1, kind=MediaKind.MOVIE,
                              show_or_movie_title="WarGames", year=1983)
    wj.assignments = {0: tv, 1: movie}

    manager.edit_job(39, edition="Director's Cut")

    assert tv.edition is None
    assert movie.edition == "Director's Cut"


def test_choosing_auto_grain_keeps_the_value_prep_already_resolved(manager):
    """"Auto" means "leave it to the pipeline". Writing None over the per-title
    checkpoint would throw away the grain decision prep already made."""
    wj = _job(manager, 40, state=JobState.READY, disc=_movie_disc())
    wj._pipeline_job.checkpoints = {0: {"ripped_path": "job_40/t0.mkv", "tune_grain": True}}

    manager.edit_job(40, grain_mode="auto", tune_grain=None)

    assert wj._pipeline_job.checkpoints[0]["tune_grain"] is True
    assert wj.grain_mode == "auto"


def test_auto_identify_needs_a_key_and_candidate_episodes(manager):
    """Both are hard prerequisites: without a key nothing can be fetched, and with
    no candidates every title matches nothing. Starting the rip anyway wastes a
    whole disc before failing."""
    wj = _job(manager, 41, state=JobState.AWAITING_MAPPING, disc=_tv_disc())

    manager.osub._api_key = None
    with pytest.raises(ValueError, match="OPENSUBTITLES_API_KEY"):
        manager.set_auto_identify(41, "House", 2004, 1408, 1, [1, 2], [0, 1])
    assert wj.auto_identify is False

    manager.osub._api_key = "key"
    with pytest.raises(ValueError, match="No candidate episodes"):
        manager.set_auto_identify(41, "House", 2004, 1408, 1, [], [0, 1])
    assert wj.auto_identify is False
    assert wj.state is JobState.AWAITING_MAPPING


def test_auto_identify_refuses_a_job_that_is_not_at_a_gate(manager):
    """Subtitle identification re-rips the selected titles; running it against a
    job already in flight would fight the rip that is happening now."""
    wj = _job(manager, 42, state=JobState.RIPPING, disc=_tv_disc())
    manager.osub._api_key = "key"

    with pytest.raises(ValueError, match="not awaiting a naming mapping"):
        manager.set_auto_identify(42, "House", 2004, 1408, 1, [1, 2], [0, 1])

    assert wj.auto_identify is False


def test_confirm_match_refuses_a_job_that_is_not_awaiting_one(manager):
    """The confirm-episodes form is posted from a page that may be minutes old."""
    wj = _job(manager, 43, state=JobState.ENCODING, disc=_tv_disc())

    with pytest.raises(ValueError, match="not awaiting match confirmation"):
        manager.confirm_match(43, {0: 1})

    assert wj.assignments == {}


async def test_identify_says_when_opensubtitles_has_nothing_for_the_season(manager, monkeypatch):
    """An empty reference set silently matches every title against nothing. The
    real causes (wrong show/season, episodes not on OpenSubtitles) are worth naming."""
    wj = _job(manager, 44, state=JobState.IDENTIFYING, disc=_tv_disc())
    wj.show_title, wj.show_tmdb_id, wj.season = "House", 1408, 3
    wj.candidate_episodes = [1, 2]

    async def phase(job, root, report):
        return {0: "some ocr text"}
    monkeypatch.setattr(manager.pipeline, "identify_phase", phase)

    async def nothing(*a, **k):
        return {}
    monkeypatch.setattr(mgr, "gather_references", nothing)

    with pytest.raises(RuntimeError) as exc:
        await manager._auto_identify(wj, _Reporter(manager, wj))

    assert "no subtitles" in str(exc.value)
    assert "House" in str(exc.value) and "[1, 2]" in str(exc.value)


# --- re-OCR ---------------------------------------------------------------


def _named(manager, job_id, state=JobState.READY):
    wj = _job(manager, job_id, state=state, disc=_movie_disc())
    wj._pipeline_job.state = state
    wj.assignments = {0: EpisodeAssignment(title_id=0, kind=MediaKind.MOVIE,
                                           show_or_movie_title="WarGames", year=1983)}
    wj.selected_title_ids = [0]
    return wj


async def test_reocr_hands_the_job_back_its_resting_state(manager, monkeypatch):
    """Re-OCR is maintenance on a job that is otherwise waiting for a worker. The
    pipeline flips the job to OCR_SUBS for progress; leaving it there makes the card
    read "ocr subs" forever and blocks the next re-OCR as "busy"."""
    wj = _named(manager, 50, state=JobState.READY)

    async def redo(job, staging_root, report):
        job.state = JobState.OCR_SUBS
        return [staging_root / "WarGames.eng.srt"]
    monkeypatch.setattr(manager.pipeline, "redo_subtitles", redo)

    manager.redo_subtitles(50)
    await _settle()

    assert manager._live_state(wj) is JobState.READY
    assert "placed 1 subtitle sidecar" in "\n".join(wj.log)


async def test_a_failed_reocr_is_logged_and_does_not_fail_the_job(manager, monkeypatch):
    """Re-OCR is background maintenance on an already-placed title. A crash in it
    must not turn a finished job into a failed one."""
    wj = _named(manager, 51, state=JobState.READY)

    async def redo(job, staging_root, report):
        job.state = JobState.OCR_SUBS
        raise RuntimeError("subtitleedit exploded")
    monkeypatch.setattr(manager.pipeline, "redo_subtitles", redo)

    manager.redo_subtitles(51)
    await _settle()

    assert "Re-OCR failed: RuntimeError: subtitleedit exploded" in "\n".join(wj.log)
    assert manager._live_state(wj) is JobState.READY


async def test_reocr_all_works_through_every_resting_job(manager, monkeypatch):
    """The bulk button has to actually run them — and one at a time, or a big
    library starts dozens of OCR processes at once."""
    a, b = _named(manager, 52), _named(manager, 53)
    _job(manager, 54, state=JobState.READY)                 # never named -> skipped
    busy = _named(manager, 55, state=JobState.ENCODING)
    concurrent, peak = 0, 0

    async def redo(job, staging_root, report):
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0)
        concurrent -= 1
        return []
    monkeypatch.setattr(manager.pipeline, "redo_subtitles", redo)

    queued = manager.redo_subtitles_all()
    await _settle()

    assert queued == 2
    assert peak == 1                                        # sequential, not a stampede
    for wj in (a, b):
        assert "Re-OCR found no staged or cached subtitles" in "\n".join(wj.log)
    assert busy.log == []                                   # a worker owns this one


# --- grain preview / gate -------------------------------------------------


async def test_preview_refuses_a_job_with_no_ripped_file(manager):
    """The A/B clips are cut from the ripped .mkv. Asked before the rip lands, the
    button must explain itself instead of erroring deep in HandBrake."""
    wj = _job(manager, 60, state=JobState.READY, disc=_movie_disc())

    with pytest.raises(ValueError, match="not ready for a preview"):
        await manager.generate_preview(60)                  # no selected titles yet

    wj.selected_title_ids = [0]
    with pytest.raises(ValueError, match="Ripped file not available"):
        await manager.generate_preview(60)                  # selected, but never ripped


async def test_preview_attaches_the_clips_to_the_job(manager, monkeypatch):
    """The card renders the A/B players straight off these fields, so a preview
    that isn't stored on the job is one you can never watch."""
    from blurayripper.pipeline import GrainPreview

    staging = manager.settings.storage.staging_dir
    (staging / "job_61").mkdir(parents=True)
    ripped = staging / "job_61" / "t0.mkv"
    ripped.write_bytes(b"x")

    wj = _job(manager, 61, state=JobState.READY, disc=_movie_disc())
    wj.selected_title_ids = [0]
    wj._pipeline_job.checkpoints = {0: {"ripped_path": "job_61/t0.mkv"}}

    async def gen(job, src, title_id, preview_dir, report):
        assert src == ripped
        return GrainPreview(off_clip=preview_dir / "off.mp4", on_clip=preview_dir / "on.mp4",
                            off_png=preview_dir / "off.png", on_png=None)
    monkeypatch.setattr(manager.pipeline, "_generate_grain_preview", gen)

    await manager.generate_preview(61)

    assert wj.preview_off_clip.name == "off.mp4"
    assert wj.preview_on_clip.name == "on.mp4"
    assert wj.preview_off.name == "off.png"
    assert wj.preview_on is None


def test_grain_pick_is_rejected_when_the_job_is_not_at_the_gate(manager):
    """The grain buttons stay on screen after the pick resolves; a second click
    must be refused rather than resolving an already-finished future."""
    _job(manager, 62, state=JobState.ENCODING, disc=_movie_disc())

    with pytest.raises(ValueError, match="not awaiting a grain decision"):
        manager.set_grain(62, True)


# --- removal --------------------------------------------------------------


async def test_removing_a_job_that_is_already_gone_is_a_no_op(manager):
    """Two browser tabs, one Remove each — the second must not 500."""
    await manager.remove(9999)


async def test_remove_does_not_swallow_its_own_cancellation(manager, monkeypatch, tmp_path):
    """remove() cancels the job's task and waits for it. On shutdown remove() is
    itself cancelled while waiting — catching that CancelledError as "the job's"
    would leave the server hanging on a task that refuses to die."""
    monkeypatch.setattr(manager.settings.storage, "staging_dir", tmp_path)
    finish = asyncio.Event()

    async def stubborn():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await finish.wait()          # a teardown that outlives the cancel

    wj = _job(manager, 63, state=JobState.RIPPING)
    wj._task = asyncio.create_task(stubborn())
    await _settle()

    remover = asyncio.create_task(manager.remove(63))
    await _settle()
    remover.cancel()

    with pytest.raises(asyncio.CancelledError):
        await remover

    finish.set()
    await _settle()


# --- scan / source --------------------------------------------------------


async def test_an_auto_mapped_disc_skips_both_gates(manager, monkeypatch, tmp_path):
    """A disc the mapping DB already knows needs no human input — parking it at a
    gate is exactly the interruption the remembered mapping exists to avoid."""
    assignment = EpisodeAssignment(title_id=0, kind=MediaKind.MOVIE,
                                   show_or_movie_title="WarGames", year=1983)

    async def scan(source, report):
        job = Job(source=source)
        job.disc = _movie_disc()
        job.auto_mapped = True
        job.assignments = {0: assignment}
        job.selected_title_ids = [0]
        return job
    monkeypatch.setattr(manager.pipeline, "scan_disc", scan)

    async def execute(job, scheduler, report, job_name=None):
        job.results = [TitleResult(title_id=0, video_path=tmp_path / "WarGames (1983).mkv")]
        return job
    monkeypatch.setattr(manager.pipeline, "execute", execute)

    wj = manager.add_disc("disc:0")
    await _settle()

    assert wj.state is JobState.DONE
    assert wj.assignments == {0: assignment}
    assert wj.outputs == [str(tmp_path / "WarGames (1983).mkv")]


def test_change_source_works_on_a_job_that_never_scanned(manager):
    """"Rip from another drive" is exactly what you click when the FIRST drive
    couldn't even scan the disc — so the job has no pipeline state at all."""
    wj = WebJob(id=64, source="disc:0", state=JobState.FAILED)
    manager.jobs[64] = wj

    manager.change_source(64, "disc:2")

    assert wj.source == "disc:2"
    assert "disc:2" in "\n".join(wj.log)


# --- auto-retry -----------------------------------------------------------


def test_with_retries_disabled_the_failure_is_still_announced(manager, monkeypatch, tmp_path):
    """auto_retry_max=0 means "never retry", not "say nothing" — and it must not
    log a nonsense "auto-retry exhausted (0)" for retries that never existed."""
    src = tmp_path / "Movie (2022).mkv"
    src.write_bytes(b"x")
    sent = []
    monkeypatch.setattr(manager, "_notify",
                        lambda t, m, priority=None: sent.append((t, priority)))
    spawned = []
    monkeypatch.setattr(manager, "_spawn",
                        lambda coro: (spawned.append(coro), coro.close())[1])
    manager.settings.auto_retry_max = 0

    wj = WebJob(id=65, source=str(src), state=JobState.FAILED)
    wj.error = "RuntimeError: transient encode blip"
    manager.jobs[65] = wj

    manager._schedule_auto_retry(wj)

    assert spawned == []
    assert len(sent) == 1 and sent[0][1] == "high"
    assert "Auto-retry exhausted" not in "\n".join(wj.log)


async def test_a_pending_auto_retry_does_not_fire_during_shutdown(manager, monkeypatch):
    """The backoff can be minutes long. Re-queuing a job while the process is
    stopping starts a rip that is about to be killed anyway."""
    ran = []
    monkeypatch.setattr(manager, "_run_job", lambda wj: ran.append(wj))
    wj = _job(manager, 66, state=JobState.FAILED)
    wj._pipeline_job.state = JobState.FAILED
    manager.closing = True

    await manager._auto_retry_later(66, 0)

    assert wj.state is JobState.FAILED
    assert ran == []


# --- box-set episode titles ----------------------------------------------


async def test_one_show_by_that_name_still_gets_episode_titles(manager, monkeypatch):
    """The remake guard must be narrow: when only ONE show carries the name, a plan
    row without a year is unambiguous and should still name the episodes."""
    from blurayripper.boxset_plan import PlanRow
    import blurayripper.services.metadata as md

    class Show:
        def __init__(self, tmdb_id, name, year):
            self.tmdb_id, self.name, self.year = tmdb_id, name, year

    class Ep:
        def __init__(self, episode_number, name):
            self.episode_number, self.name = episode_number, name

    async def search_tv(self, q):
        return [Show(1408, "House", 2004)]

    async def get_season(self, tv_id, season):
        assert (tv_id, season) == (1408, 1)
        return [Ep(1, "Pilot"), Ep(2, "Paternity")]

    monkeypatch.setattr(md.TMDBClient, "search_tv", search_tv)
    monkeypatch.setattr(md.TMDBClient, "get_season", get_season)
    manager.settings.tmdb_api_key = "x"

    assigns = [EpisodeAssignment(0, MediaKind.TV, "House", season=1, episode=1),
               EpisodeAssignment(1, MediaKind.TV, "House", season=1, episode=2)]
    got = await manager._fill_episode_titles(assigns, PlanRow(disc_label="X", show="House"), 1)

    assert got == 1408
    assert [a.episode_title for a in assigns] == ["Pilot", "Paternity"]


async def test_a_tmdb_failure_never_blocks_the_box_set_rip(manager, monkeypatch):
    """Episode titles are a nicety — the numbers already name the file correctly.
    Letting a TMDB outage escape would abort a rip the plan row fully described."""
    from blurayripper.boxset_plan import PlanRow
    import blurayripper.services.metadata as md

    async def search_tv(self, q):
        raise OSError("tmdb unreachable")
    monkeypatch.setattr(md.TMDBClient, "search_tv", search_tv)
    manager.settings.tmdb_api_key = "x"

    assigns = [EpisodeAssignment(0, MediaKind.TV, "House", season=1, episode=1)]
    got = await manager._fill_episode_titles(assigns, PlanRow(disc_label="X", show="House"), 1)

    assert got is None
    assert assigns[0].episode_title is None      # numbered, not named — still correct


# --- auto-rip -------------------------------------------------------------


async def test_auto_rip_will_not_search_tmdb_for_an_unlabelled_disc(manager, monkeypatch):
    """A disc with no volume label would send an empty query to TMDB, whose first
    "exact match" is whatever it feels like returning — and that would rip a whole
    disc under someone else's name."""
    import blurayripper.services.metadata as md

    searched = []

    async def search_movie(self, q, year=None):
        searched.append(q)
        return []
    monkeypatch.setattr(md.TMDBClient, "search_movie", search_movie)
    manager.settings.auto_rip = True
    manager.settings.tmdb_api_key = "x"

    async def scan(source, report):
        job = Job(source=source)
        job.disc = _movie_disc(disc_name="")
        return job
    monkeypatch.setattr(manager.pipeline, "scan_disc", scan)

    wj = manager.add_disc("disc:0")
    await _settle()

    assert searched == []
    assert wj.state is JobState.AWAITING_MAPPING     # parked for you to name


async def test_auto_rip_parks_the_disc_when_tmdb_is_unreachable(manager, monkeypatch):
    """No answer from TMDB is not "no match" — it must fall back to the naming gate
    and say why, not rip the disc under a guessed name or fail the job."""
    import blurayripper.services.metadata as md

    async def search_movie(self, q, year=None):
        raise OSError("tmdb unreachable")
    monkeypatch.setattr(md.TMDBClient, "search_movie", search_movie)
    manager.settings.auto_rip = True
    manager.settings.tmdb_api_key = "x"

    async def scan(source, report):
        job = Job(source=source)
        job.disc = _movie_disc()
        return job
    monkeypatch.setattr(manager.pipeline, "scan_disc", scan)

    wj = manager.add_disc("disc:0")
    await _settle()

    assert wj.state is JobState.AWAITING_MAPPING
    assert "TMDB lookup failed (OSError)" in "\n".join(wj.log)


# --- snapshot resilience --------------------------------------------------


def test_one_unrenderable_job_does_not_break_the_whole_snapshot(manager):
    """The snapshot feeds every SSE client. One malformed job (a bad store row)
    must not blank the queue for every other job on the page."""
    good = _job(manager, 70, state=JobState.DONE, disc=_movie_disc())
    bad = _job(manager, 71, state=JobState.READY,
               disc=DiscInfo(source="disc:0", disc_name="BROKEN", titles=None))

    jobs = {j["id"]: j for j in manager.snapshot()["jobs"]}

    assert set(jobs) == {good.id, bad.id}
    assert jobs[70]["state"] == "done"                  # the healthy job is intact
    assert jobs[71]["state"] == "failed"                # the bad one is flagged
    assert "render error" in jobs[71]["error"]


# --- rename ---------------------------------------------------------------


async def test_renaming_a_show_whose_episodes_have_no_numbers_still_corrects_the_name(manager):
    """An auto-identify job can hold TV assignments with no season/episode yet —
    they resolve to no path at all. The correction must still land on them instead
    of aborting the whole rename."""
    wj = _job(manager, 72, state=JobState.FAILED, disc=_tv_disc())
    wj.assignments = {0: EpisodeAssignment(title_id=0, kind=MediaKind.TV,
                                           show_or_movie_title="Hawaii Five-0", year=2010)}

    manager.rename_job(72, "Hawaii Five-O", 1968)

    assert wj.assignments[0].show_or_movie_title == "Hawaii Five-O"
    assert wj.assignments[0].year == 1968
    assert wj.show_tmdb_id is None       # the old show's id must not carry over


def test_rename_reports_a_filesystem_failure_instead_of_going_quiet(manager, monkeypatch):
    """Renaming placed files can fail (Plex holding a handle, a read-only share).
    Silently swallowing it leaves the library and the queue disagreeing."""
    a = EpisodeAssignment(title_id=0, kind=MediaKind.MOVIE,
                          show_or_movie_title="WarGames", year=1983)
    old_video = manager.pipeline._video_path_for(a)
    old_video.parent.mkdir(parents=True, exist_ok=True)
    old_video.write_bytes(b"x")

    wj = _job(manager, 73, state=JobState.DONE, disc=_movie_disc())
    wj.assignments = {0: a}
    wj.outputs = [str(old_video)]

    def boom(src, dst):
        raise OSError("file in use by Plex")
    monkeypatch.setattr(mgr.library, "rename_movie", boom)

    with pytest.raises(ValueError, match="Couldn't rename the placed files"):
        manager.rename_job(73, "War Games", 1983)

    assert wj.outputs == [str(old_video)]   # the queue still points at the real file


# --- edit / re-OCR guards -------------------------------------------------


def test_an_explicit_grain_pick_overrides_what_prep_already_decided(manager):
    """Prep resolves grain per title and stores it in the checkpoint. Editing the
    job afterwards has to reach that checkpoint, or the encode ignores your pick."""
    wj = _job(manager, 74, state=JobState.READY, disc=_movie_disc())
    wj._pipeline_job.checkpoints = {0: {"ripped_path": "job_74/t0.mkv", "tune_grain": False}}

    manager.edit_job(74, grain_mode="on", tune_grain=True)

    assert wj._pipeline_job.checkpoints[0]["tune_grain"] is True


def test_reocr_refuses_a_job_that_was_never_named(manager):
    """Subtitles are placed next to the named video file. With no assignment there
    is nowhere to put them, so the button must say so rather than no-op silently."""
    _job(manager, 75, state=JobState.READY, disc=_movie_disc())

    with pytest.raises(ValueError, match="no naming assignments"):
        manager.redo_subtitles(75)


def test_remap_on_a_removed_job_says_so(manager):
    with pytest.raises(ValueError, match="No job"):
        manager.remap(9999)


# --- removal / store resilience ------------------------------------------


async def test_a_removed_job_stays_removed_even_if_the_database_is_down(manager, capsys):
    """Postgres is on another box and does go away. If the delete failing aborted
    the removal, the job would come straight back on the next snapshot."""
    def boom(job_id):
        raise RuntimeError("connection refused")
    manager.store.delete = boom
    _job(manager, 76, state=JobState.DONE)

    await manager.remove(76)

    assert 76 not in manager.jobs
    assert "delete job 76 failed" in capsys.readouterr().out


# --- .mkv ingestion -------------------------------------------------------


async def test_an_unprobeable_mkv_still_becomes_a_nameable_job(manager, monkeypatch, tmp_path):
    """mkvmerge can fail on an odd file. Refusing the job leaves you no way to
    encode it at all; a job with no stream detail is still fully nameable."""
    mkv = tmp_path / "WarGames.mkv"
    mkv.write_bytes(b"data")

    async def probe(path):
        raise RuntimeError("mkvmerge: unsupported")
    monkeypatch.setattr(manager.pipeline.mkvtools, "probe", probe)

    wj = manager._add_mkv(mkv)
    await _settle()

    assert wj.state is JobState.AWAITING_MAPPING
    assert wj.disc.disc_name == "WarGames"
    assert wj._pipeline_job.checkpoints[0]["ripped_path"] == str(mkv)   # no re-rip
    assert "Probe failed" in "\n".join(wj.log)


async def test_a_season_folder_keeps_each_files_own_tracks(manager, monkeypatch, tmp_path):
    """The set's titles drive the naming form and the encode. One file failing to
    probe must not lose the other files' audio/subtitle tracks — or drop the set."""
    from blurayripper.tools.mkvtools import ProbedFile, SubtitleTrack

    folder = tmp_path / "s1"
    folder.mkdir()
    good = folder / "Poirot Series 1_t01.mkv"
    bad = folder / "Poirot Series 1_t02.mkv"
    for f in (good, bad):
        f.write_bytes(b"x" * 8)

    async def probe(path):
        if path == bad:
            raise RuntimeError("mkvmerge: unsupported")
        return ProbedFile(duration_seconds=3000, video_height=1080,
                          audio_languages=["eng", "fra"],
                          subtitles=[SubtitleTrack(track_id=3, codec="PGS",
                                                   language="eng", is_pgs=True)])
    monkeypatch.setattr(manager.pipeline.mkvtools, "probe", probe)

    ids = manager.add_source(str(folder))
    await _settle()
    wj = manager.jobs[ids[0]]

    assert len(wj.disc.titles) == 2                       # the set survived
    by_name = {t.source_filename: t for t in wj.disc.titles}
    assert by_name[good.name].audio_languages == ["eng", "fra"]
    assert by_name[good.name].subtitle_languages == ["eng"]
    assert by_name[bad.name].audio_languages == []        # unknown, not invented
    assert by_name[bad.name].duration_seconds == 0
    assert "Probe failed for Poirot Series 1_t02.mkv" in "\n".join(wj.log)


# --- auto-retry follow-through -------------------------------------------


async def test_the_auto_retry_actually_requeues_when_the_backoff_expires(manager, monkeypatch):
    """The backoff is the whole point of auto-retry; if the delayed half never
    re-queues, a transient encode failure just sits there failed forever."""
    ran = []

    async def fake_run(wj):
        ran.append(wj.id)
    monkeypatch.setattr(manager, "_run_job", fake_run)
    wj = _job(manager, 77, state=JobState.FAILED)
    wj._pipeline_job.state = JobState.FAILED
    wj.error = "RuntimeError: transient encode blip"

    await manager._auto_retry_later(77, 0)
    await _settle()

    assert wj.state is JobState.READY
    assert wj.error is None
    assert ran == [77]


def test_display_name_without_episode_numbers_still_names_the_season(manager):
    """A TV disc identified down to the season but not the episodes (the state an
    auto-identify job sits in) still has to be findable on the shelf."""
    wj = _job(manager, 78, state=JobState.FAILED, disc=_tv_disc())
    wj.assignments = {0: EpisodeAssignment(title_id=0, kind=MediaKind.TV,
                                           show_or_movie_title="House", season=2)}

    assert manager._display_name(wj) == "House S02"
