"""Regressions for JobManager defects found in the src audit.

Each test here failed before the matching fix in web/manager.py and asserts what
the OPERATOR gets — the job is parked where the controls that recover it live,
the request is refused with something the form can render, the disc that gets
ejected is the one the job is actually using — rather than that some exception
was or was not raised.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from blurayripper import boxset_plan
from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, EpisodeAssignment, MediaKind, Title
from blurayripper.persistence import (
    JobRecord, MemoryStore, assignment_to_dict, disc_to_dict)
from blurayripper.pipeline import Job, JobState
from blurayripper.tools.makemkv import Drive
from blurayripper.web.app import create_app
from blurayripper.web.manager import JobManager, WebJob, _Reporter


EPISODE_SECONDS = 47 * 60  # inside the episode-length window discinfo uses


def _settings(tmp_path, clustered=False):
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "settings.toml").write_text("", encoding="utf-8")
    s = load_settings(cfg)
    s.config_dir = cfg                       # where boxsets.local.csv is read from
    s.database_url = None                    # never reach for a real Postgres
    s.storage.output_dir = tmp_path / "library"
    s.storage.staging_dir = tmp_path / "staging"
    s.encode.preset_file = tmp_path / "presets.json"
    s.notify_url = None                      # no pushes out of a test
    s.tmdb_api_key = None                    # no network in _fill_episode_titles
    s.cluster.use_workers = clustered
    return s


def _mgr(tmp_path, clustered=False, store=None):
    return JobManager(_settings(tmp_path, clustered), store=store or MemoryStore())


def _tv_disc(label="HOUSE_S1_D1", disc_name=None, titles=3):
    return DiscInfo(
        source="disc:0", disc_name=disc_name or label, volume_label=label,
        titles=[Title(title_id=i, duration_seconds=EPISODE_SECONDS, chapter_count=6,
                      source_filename=f"{800 + i:05d}.mpls") for i in range(titles)])


def _movie_assignment():
    return assignment_to_dict(
        EpisodeAssignment(0, MediaKind.MOVIE, "WarGames", 1983))


async def _settle():
    for _ in range(60):
        await asyncio.sleep(0)


# --- recovery must not manufacture the orphan it exists to catch -------------

def test_an_interrupted_failure_with_nothing_ripped_is_parked_for_review(tmp_path):
    """A FAILED record whose error looks like a killed process was reclaimed as
    READY so a worker could pick it up again. When nothing had been ripped that
    created the exact orphan the review gate exists to prevent: a worker claims
    it, refuses it (there is no file to encode), releases it, and the card reads
    "ready" forever — until the NEXT restart finally caught it.

    The disc job must land where its recovery controls are: FAILED, flagged for
    review, and stored that way so no worker can claim it in the meantime.
    """
    store = MemoryStore()
    store.save(JobRecord(                       # killed mid-rip: no checkpoint
        id=4, source="disc:0", state="failed",
        error="ProcessError: Command failed (-9)",
        disc=disc_to_dict(_tv_disc()), selected_title_ids=[0],
        assignments=[_movie_assignment()], checkpoints={}))
    store.save(JobRecord(                       # killed mid-ENCODE: the rip is done
        id=5, source="disc:1", state="failed",
        error="ProcessError: Command failed (-9)",
        disc=disc_to_dict(_tv_disc("HOUSE_S1_D2")), selected_title_ids=[0],
        assignments=[_movie_assignment()],
        checkpoints={"0": {"ripped_path": "t000/title.mkv"}}))

    m = _mgr(tmp_path, clustered=True, store=store)
    m.recover()

    unripped, ripped = m.jobs[4], m.jobs[5]
    stored = {r.id: r.state for r in store.load_all()}

    assert unripped.state is JobState.FAILED
    assert stored[4] == "failed", "a worker could still claim a job with no rip"
    card = m._job_dict(unripped)
    assert card["needs_review"] is True and card["retry_rips"] is True
    assert any("Rip this disc" in line for line in unripped.log), unripped.log
    assert "no ripped file" in (unripped.error or "")

    # The one that HAS its rip is still handed straight back to the workers, so
    # the test above passes because of the missing rip, not because recovery now
    # parks everything it sees.
    assert ripped.state is JobState.READY and stored[5] == "ready"
    assert ripped.error is None


def test_a_dev_sourced_job_that_never_ripped_reaches_the_review_gate(tmp_path):
    """``dev:/dev/sr0`` is a real selector (change_source accepts it, makemkv
    scans it), but _rip_pending returned False for anything not starting with
    ``disc:`` — so the ``dev:`` half of the review gate, of the Retry button's
    label and of the "your disc is back" matcher was all dead code. A dev: job
    whose rip never finished stayed the orphan the gate was written to fix, and
    re-inserting its disc opened a SECOND job for it.
    """
    store = MemoryStore()
    store.save(JobRecord(
        id=9, source="dev:/dev/sr0", state="ready",
        disc=disc_to_dict(DiscInfo(
            source="dev:/dev/sr0", disc_name="HOUSE_S1_D1", volume_label="HOUSE_S1_D1",
            titles=[Title(title_id=0, duration_seconds=EPISODE_SECONDS)])),
        selected_title_ids=[0], assignments=[_movie_assignment()], checkpoints={}))

    m = _mgr(tmp_path, clustered=True, store=store)
    m.recover()

    wj = m.jobs[9]
    card = m._job_dict(wj)
    assert wj.state is JobState.FAILED
    assert {r.id: r.state for r in store.load_all()}[9] == "failed"
    assert card["needs_review"] is True
    assert card["retry_rips"] is True, "Retry would rip, but the button doesn't say so"
    assert card["needs_disc"] is True

    # …and putting that disc back in is recognised as feeding THIS job rather
    # than as a new disc to queue.
    assert [j.id for j in m._jobs_awaiting_this_disc("HOUSE_S1_D1")] == [9]


# --- changing the drive must not leave the old device behind -----------------

async def test_changing_drive_never_leaves_the_job_pointing_at_the_drive_it_left(
        tmp_path, monkeypatch):
    """change_source only wrote the new OS device when it could find the new
    drive in the cache — and that cache is seeded from a file and refreshed by a
    watcher, so right after a restart it is empty. The job then pointed at the
    NEW drive by selector and the OLD drive by device, which is the combination
    the code's own comment warns about: auto-eject and cover art would work on
    whatever disc is now in the drive this job just moved off.
    """
    m = _mgr(tmp_path)
    disc = _tv_disc()
    disc.drive_device = "D:"                 # the drive the disc has just left
    job = Job(source="disc:0")
    job.disc = disc
    wj = WebJob(id=1, source="disc:0", state=JobState.FAILED)
    wj.disc, wj._pipeline_job = disc, job
    m.jobs[1] = wj
    m._drives_cache = []                     # cold cache: nothing enumerated yet

    m.change_source(1, "disc:1")

    assert wj.source == "disc:1"
    assert wj.disc.drive_device != "D:"
    assert job.disc.drive_device != "D:"

    # What that costs in practice: the pipeline falls back to the stored device
    # when it cannot enumerate the drives — which is exactly the wedged-drive
    # situation that made you move the disc in the first place. It must not fall
    # back onto D:, where somebody else's disc is now sitting.
    async def cannot_enumerate(timeout=None):
        raise asyncio.TimeoutError("drive is not responding")

    monkeypatch.setattr(m.pipeline.makemkv, "list_drives", cannot_enumerate)
    assert await m.pipeline._eject_device_for(job) != "D:"


# --- add_source refuses what it cannot resolve -------------------------------

@pytest.fixture
def api(tmp_path):
    """(client, manager) over the real app, with no pipeline coroutines spawned."""
    app = create_app(_settings(tmp_path))
    manager = app.state.manager

    def spawn(coro):
        coro.close()
        return _Done()

    manager._spawn = spawn
    return TestClient(app), manager


class _Done:
    def done(self) -> bool:
        return True

    def cancel(self) -> None:
        pass


def test_a_blank_source_queues_nothing_from_the_servers_own_directory(
        api, tmp_path, monkeypatch):
    """``Path("")`` is ``Path(".")`` and a directory, so a blank source globbed
    the SERVE PROCESS'S working directory and queued a job per .mkv it happened
    to contain — files nobody asked to rip, named after whatever was lying next
    to the server. The refusal the API advertises has to actually happen.
    """
    client, manager = api
    work = tmp_path / "cwd"
    work.mkdir()
    (work / "Some Home Video.mkv").write_bytes(b"x")
    monkeypatch.chdir(work)

    r = client.post("/api/jobs", json={"source": "   "})

    assert r.status_code == 400, r.text[:200]
    assert r.json()["detail"].strip()
    assert manager.jobs == {}, "a blank source queued something"
    assert manager.store.load_all() == []


def test_a_source_too_long_for_the_filesystem_is_refused_not_a_traceback(api):
    """pathlib re-raises every errno outside its own small set, and
    ENAMETOOLONG is not in it — so a source with a component past the OS name
    limit came out of add_source as OSError and reached the browser as a 500
    with nothing to act on, instead of the 400 the endpoint promises for a
    source it cannot resolve.
    """
    client, manager = api

    r = client.post("/api/jobs", json={"source": "Q" * 300})

    assert r.status_code == 400, r.text[:200]
    # A 400 with a message that names the real problem. It used to be an OSError
    # escaping as a 500; the generic "check the path / mapped drive" advice that
    # first replaced it sent the user after a mount that was fine.
    detail = r.json()["detail"]
    assert "300" in detail and "255" in detail, detail
    assert manager.jobs == {}


# --- a plan that cannot be read must not fail the disc -----------------------

def _scanning_manager(tmp_path, disc, monkeypatch):
    m = _mgr(tmp_path)

    async def fake_scan(source, report):
        job = Job(source=source)
        job.state = JobState.SCANNING
        job.disc = disc
        job.fingerprint = "fp_test"
        return job

    monkeypatch.setattr(m.pipeline, "scan_disc", fake_scan)
    return m


async def test_a_plan_csv_with_a_stray_quote_names_the_disc_instead_of_failing_it(
        tmp_path, monkeypatch):
    """One unbalanced double-quote makes csv swallow the rest of the file into a
    single field, which blows Python's 128 KiB field limit and raises out of
    parse_csv — despite its "never raises" contract. That parse runs on EVERY
    scanned disc, so a stray quote in a long plan failed every disc inserted
    with a csv error and no route forward.

    A plan that cannot be read means "no plan": the disc must still reach the
    naming gate rather than failing.

    parse_csv now honours its contract and returns no rows instead of raising,
    so the outcome is what this asserts. The manager's own guard against
    load_rows raising is covered by the next test, which forces that path
    directly — belt and braces, since the two were fixed independently.
    """
    boxset_plan.save_plan(
        _settings(tmp_path),
        'disc_label,show\nHOUSE_S1_D1,"House\n' + "junk,row\n" * 40000)
    m = _scanning_manager(tmp_path, _tv_disc(), monkeypatch)

    wj = m.add_disc("disc:0")
    await _settle()

    assert wj.state is JobState.AWAITING_TITLES, wj.error
    assert wj.error is None                     # the disc is nameable, not failed
    assert any("naming this one manually" in line for line in wj.log)


async def test_a_plan_that_raises_still_lets_the_disc_be_named(tmp_path, monkeypatch):
    """The defensive half. _try_planned_tv runs on EVERY scanned disc, so if
    load_rows ever raises again — a new csv edge, an encoding we do not handle —
    it must not take every inserted disc down with it."""
    boxset_plan.save_plan(_settings(tmp_path), "disc_label,show\nHOUSE_S1_D1,House\n")
    m = _scanning_manager(tmp_path, _tv_disc(), monkeypatch)

    def boom(_settings_):
        raise UnicodeDecodeError("utf-8", b"", 0, 1, "invalid start byte")

    monkeypatch.setattr("blurayripper.web.manager.boxset_plan.load_rows", boom)

    wj = m.add_disc("disc:0")
    await _settle()

    assert wj.state is JobState.AWAITING_TITLES, wj.error
    assert wj.error is None


async def test_a_plan_year_of_zero_is_treated_as_no_year_and_warned_about(
        tmp_path, monkeypatch):
    """A "0" in the year cell parses to the integer 0: falsy, so the year-matched
    TMDB lookup was skipped, but not None, so the warning that catches a missing
    year never fired. The disc auto-ripped with year 0 on every assignment and
    nothing said the Plex folder would carry no year — which is how the 1968
    show lands in the 2010 remake's folder.
    """
    boxset_plan.save_plan(
        _settings(tmp_path),
        "disc_label,show,year,season,first_episode,episodes\n"
        "HOUSE_S1_D1,House,0,1,1,3\n")
    m = _scanning_manager(tmp_path, _tv_disc(), monkeypatch)

    async def fake_execute(job, scheduler, report, job_name=None):
        job.results = []                      # the plan starts a rip; don't run one
        return job

    monkeypatch.setattr(m.pipeline, "execute", fake_execute)

    wj = m.add_disc("disc:0")
    await _settle()

    assert wj.assignments, "the plan row did not apply at all"
    assert {a.year for a in wj.assignments.values()} == {None}
    log = "\n".join(wj.log)
    assert "ignoring the year “0”" in log, log
    assert "may match a remake" in log, log


# --- the outage line must name the call that actually failed -----------------

async def test_a_failed_heartbeat_is_not_reported_as_a_failed_read(tmp_path, capsys):
    """The serve heartbeat (a WRITE) shared a try block with list_workers (a
    READ) whose handler hardcoded "list_workers failed". A write-only outage —
    a replica still serving reads, a permissions or disk-full error on the
    workers table — therefore blamed the read, on the one line that exists to
    explain the failure. Sharing the block also meant a failed heartbeat skipped
    the read entirely, so the Workers panel froze for a reason of its own.
    """
    class WriteOnlyOutage(MemoryStore):
        """Reads answer; the heartbeat write does not."""

        durable = True

        def __init__(self, manager_box):
            super().__init__()
            self._box = manager_box

        def record_worker(self, *a, **kw):
            # Bound the poll loop to a single pass from its FIRST store call, so
            # the test never depends on which calls a pass makes.
            self._box["m"].closing = True
            raise PermissionError("permission denied for table workers")

    box = {}
    m = _mgr(tmp_path, clustered=True, store=WriteOnlyOutage(box))
    box["m"] = m
    m.settings.cluster.worker_poll_seconds = 0

    await m._refresh_from_store()

    out = capsys.readouterr().out
    assert "record_worker failed" in out, out
    assert "list_workers failed" not in out, out
    assert "permission denied" in out
    # The read still ran, rather than being skipped because the write failed.
    assert m._workers_cache == []
    assert "list_workers" not in m._last_store_error


# --- names that identify the physical disc -----------------------------------

def test_a_disc_that_spans_a_season_boundary_is_named_unambiguously(tmp_path):
    """The season span and the episode span were computed independently, so the
    last disc of a set (S01E22 + S02E01 + S02E02) came out as "S01–S02E01–E22" —
    which reads as episodes 1 to 22 of both seasons, i.e. the whole box set. The
    name exists to pick ONE disc off a shelf, so each end of the span has to
    carry its own season.
    """
    m = _mgr(tmp_path)

    def ep(tid, season, episode):
        return EpisodeAssignment(title_id=tid, kind=MediaKind.TV,
                                 show_or_movie_title="House",
                                 season=season, episode=episode)

    wj = WebJob(id=1, source="disc:0")
    wj.disc = _tv_disc()
    wj.assignments = {a.title_id: a for a in (ep(0, 1, 22), ep(1, 2, 1), ep(2, 2, 2))}

    # A disc spanning two seasons has no single season for the address, so the
    # disc number leads and both ends of the span stay season-qualified.
    assert m._display_name(wj) == "House D01 S01E22–S02E02"

    # A disc inside one season is untouched — the span there was never ambiguous.
    # Note the label (S1_D1) and the episodes (season 2) disagree here: the marker
    # follows the LABEL on purpose, because that is what is printed on the case
    # you have to find.
    wj.assignments = {a.title_id: a for a in (ep(0, 2, 3), ep(1, 2, 4))}
    assert m._display_name(wj) == "House S01 D01 E03–E04"


def test_the_mapping_form_prefills_from_the_iso_volume_label(tmp_path):
    """The season/disc hints were read off disc_name only, while the box-set plan
    matches on ``volume_label or disc_name``. On a disc where MakeMKV reports a
    friendly name next to the ISO label, the plan could infer season 1 disc 4 and
    the manual form still came up blank — on exactly the fallback path where
    retyping the season for all 20 discs hurts.
    """
    m = _mgr(tmp_path)
    wj = WebJob(id=1, source="disc:0", state=JobState.AWAITING_MAPPING)
    wj.disc = DiscInfo(
        source="disc:0", disc_name="Hawaii Five-O",
        volume_label="HAWAII_FIVE_0_S1_D4",
        titles=[Title(title_id=0, duration_seconds=EPISODE_SECONDS)])
    m.jobs[1] = wj

    card = m._job_dict(wj)

    assert card["label_season"] == 1
    assert card["label_disc"] == 4
    # The friendly name is still what identifies the disc on the card.
    assert card["disc_name"] == "Hawaii Five-O"


async def test_a_second_confirm_explains_itself_instead_of_reading_as_a_fault(
        tmp_path, monkeypatch):
    """The reported bug's tail end. The UI now blocks the double click, but a
    slow tick or a second tab can still get there, and "Job 5 is not awaiting a
    naming mapping" reads like something broke — when in fact the first click
    worked and nothing was lost."""
    from blurayripper.models import EpisodeAssignment, MediaKind

    m = _mgr(tmp_path)
    wj = WebJob(id=5, source="disc:0", state=JobState.READY)
    wj.assignments = {0: EpisodeAssignment(
        title_id=0, kind=MediaKind.MOVIE, show_or_movie_title="Heat", year=1995)}
    job = Job(source="disc:0"); job.state = JobState.READY
    wj._pipeline_job = job
    m.jobs[5] = wj

    with pytest.raises(ValueError) as exc:
        m.set_mapping(5, list(wj.assignments.values()))

    msg = str(exc.value)
    assert "already confirmed" in msg
    assert "Nothing was lost" in msg
    assert "not awaiting a naming mapping" not in msg


async def test_a_job_that_never_had_a_naming_still_says_so_plainly(tmp_path):
    """The other side: a job with no assignments genuinely is not at that gate,
    and must not be told it was 'already confirmed'."""
    from blurayripper.pipeline import Job as _Job

    m = _mgr(tmp_path)
    wj = WebJob(id=6, source="disc:0", state=JobState.RIPPING)
    wj._pipeline_job = _Job(source="disc:0")
    wj._pipeline_job.state = JobState.RIPPING
    m.jobs[6] = wj

    with pytest.raises(ValueError, match="not awaiting a naming mapping"):
        m.set_mapping(6, [])


async def test_two_jobs_on_one_drive_never_rip_at_the_same_time(tmp_path):
    """Live fault: jobs 311 and 350 were the same disc in the same reader and
    BOTH ripped it at once. prep_concurrency=2 let each take a slot and nothing
    keyed the gate to the drive, so two makemkvcon processes fought over one
    optical head — 1.6 MB/s (0.4x) instead of a normal rip."""
    import asyncio

    mgr = _mgr(tmp_path)
    a = WebJob(id=311, source="disc:1")
    b = WebJob(id=350, source="disc:1")

    overlap, live = [], 0

    async def rip(wj):
        nonlocal live
        async with mgr._prep_slot(wj):
            live += 1
            overlap.append(live)
            await asyncio.sleep(0.02)      # long enough for the other to barge in
            live -= 1

    await asyncio.gather(rip(a), rip(b))
    assert max(overlap) == 1, f"two rips ran on one drive: {overlap}"


async def test_separate_drives_still_rip_in_parallel(tmp_path):
    """The fix must not serialise the whole rip box: two drives are two rips.
    A lock on 'the drive' that actually behaved like a global lock would halve
    throughput on a two-reader box and look like a fix."""
    import asyncio

    mgr = _mgr(tmp_path)
    mgr._prep_sem = asyncio.Semaphore(2)
    a = WebJob(id=1, source="disc:0")
    b = WebJob(id=2, source="disc:1")

    live, peak = 0, 0

    async def rip(wj):
        nonlocal live, peak
        async with mgr._prep_slot(wj):
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.02)
            live -= 1

    await asyncio.gather(rip(a), rip(b))
    assert peak == 2, "different drives were serialised against each other"


async def test_file_sourced_jobs_are_not_gated_by_a_drive(tmp_path):
    """A file has no drive to contend for. Keying the lock on the source string
    without checking it names a drive would serialise every file job behind one
    lock — and most of this queue is file-sourced."""
    import asyncio

    mgr = _mgr(tmp_path)
    mgr._prep_sem = asyncio.Semaphore(2)
    a = WebJob(id=1, source=r"Z:\media\a.mkv")
    b = WebJob(id=2, source=r"Z:\media\b.mkv")
    assert mgr._drive_key(a) is None

    live, peak = 0, 0

    async def go(wj):
        nonlocal live, peak
        async with mgr._prep_slot(wj):
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.02)
            live -= 1

    await asyncio.gather(go(a), go(b))
    assert peak == 2, "file-sourced jobs were serialised behind a drive lock"


# --- the same disc must not be queued twice ---------------------------------

def _scanned(mgr, job_id, fp, state=JobState.RIPPING, name="Happy Feet 2"):
    wj = WebJob(id=job_id, source="disc:1", state=state)
    wj.fingerprint = fp
    wj.disc = DiscInfo(source="disc:1", disc_name=name, titles=[])
    mgr.jobs[wj.id] = wj
    return wj


def test_a_second_job_for_a_disc_still_being_ripped_is_caught(tmp_path):
    """Jobs 311 and 350 were the same disc in the same reader, both in flight.
    Serialising the drive stops them overlapping but still rips the disc twice
    and places the film twice — the second job should never start."""
    mgr = _mgr(tmp_path)
    first = _scanned(mgr, 311, "fp-happy-feet")
    second = _scanned(mgr, 350, "fp-happy-feet", state=JobState.SCANNING)

    dupe = mgr._active_duplicate(second)
    assert dupe is not None and dupe.id == 311


def test_re_ripping_a_disc_you_already_finished_is_allowed(tmp_path):
    """DONE is not a duplicate. Re-ripping a disc whose encode you were unhappy
    with is a legitimate request, and blocking it would make the guard worse
    than the bug."""
    mgr = _mgr(tmp_path)
    _scanned(mgr, 311, "fp-happy-feet", state=JobState.DONE)
    again = _scanned(mgr, 400, "fp-happy-feet", state=JobState.SCANNING)
    assert mgr._active_duplicate(again) is None


def test_a_failed_job_does_not_block_its_own_replacement(tmp_path):
    """The natural fix for a failed disc is to add it again. If FAILED counted
    as a duplicate, that retry would be refused by the wreck it is replacing."""
    mgr = _mgr(tmp_path)
    _scanned(mgr, 311, "fp-happy-feet", state=JobState.FAILED)
    replacement = _scanned(mgr, 400, "fp-happy-feet", state=JobState.SCANNING)
    assert mgr._active_duplicate(replacement) is None


def test_different_discs_are_not_confused(tmp_path):
    """Two discs in flight at once is normal on a two-drive box."""
    mgr = _mgr(tmp_path)
    _scanned(mgr, 311, "fp-happy-feet")
    other = _scanned(mgr, 312, "fp-elemental", state=JobState.SCANNING)
    assert mgr._active_duplicate(other) is None


def test_an_unscanned_job_matches_nothing(tmp_path):
    """No fingerprint yet means no identity. Treating empty-as-equal would make
    every freshly added disc a duplicate of every other."""
    mgr = _mgr(tmp_path)
    a = _scanned(mgr, 1, None, state=JobState.SCANNING)
    _scanned(mgr, 2, None, state=JobState.SCANNING)
    assert mgr._active_duplicate(a) is None


# --- merging duplicate jobs for one disc -------------------------------------

def test_three_failed_attempts_at_one_disc_merge_into_one(tmp_path):
    """Jumanji sat in the queue as 345, 348 and 349: a failed rip gets re-added
    rather than retried, and each re-add is a fresh job. They are one piece of
    work."""
    mgr = _mgr(tmp_path)
    keep = _scanned(mgr, 345, "fp-jumanji", state=JobState.FAILED, name="Jumanji (1995)")
    _scanned(mgr, 348, "fp-jumanji", state=JobState.FAILED, name="Jumanji (1995)")
    _scanned(mgr, 349, "fp-jumanji", state=JobState.FAILED, name="Jumanji (1995)")
    assert [o.id for o in mgr._merge_candidates(keep)] == [348, 349]


async def test_merging_keeps_the_job_you_asked_for_and_drops_the_rest(tmp_path):
    mgr = _mgr(tmp_path)
    _scanned(mgr, 345, "fp-jumanji", state=JobState.FAILED)
    keep = _scanned(mgr, 348, "fp-jumanji", state=JobState.FAILED)
    _scanned(mgr, 349, "fp-jumanji", state=JobState.FAILED)

    out = await mgr.merge_duplicates(348)
    assert out == {"kept": 348, "merged": [345, 349]}
    assert sorted(mgr.jobs) == [348]


async def test_merging_carries_over_naming_the_kept_job_never_got(tmp_path):
    """The mapping you typed is the one thing a failed attempt may actually have
    achieved. Merging must not be the way it gets thrown away."""
    mgr = _mgr(tmp_path)
    keep = _scanned(mgr, 348, "fp-jumanji", state=JobState.FAILED)
    named = _scanned(mgr, 345, "fp-jumanji", state=JobState.FAILED)
    named.assignments = {0: _movie_assignment()}
    named.selected_title_ids = [0]

    await mgr.merge_duplicates(348)
    assert keep.assignments and keep.selected_title_ids == [0]


async def test_a_job_that_already_placed_a_file_is_never_merged_away(tmp_path):
    """Merging deletes jobs. A job holding placed output is finished work, and
    losing it to a tidy-up would be the worst possible outcome of a cleanup."""
    mgr = _mgr(tmp_path)
    keep = _scanned(mgr, 348, "fp-jumanji", state=JobState.FAILED)
    done = _scanned(mgr, 345, "fp-jumanji", state=JobState.DONE)
    done.outputs = ["/mnt/TrueNas/.../Jumanji (1995)/Jumanji (1995).mkv"]

    assert [o.id for o in mgr._merge_candidates(keep)] == []
    await mgr.merge_duplicates(348)
    assert 345 in mgr.jobs


def test_a_different_resolution_of_one_film_is_not_a_duplicate(tmp_path):
    """The 1080p Blu-ray and the 4K UHD of one film are different discs and
    fingerprint differently, so they are two VERSIONS, not duplicates — merging
    them would delete one of the two files you deliberately ripped."""
    mgr = _mgr(tmp_path)
    bluray = _scanned(mgr, 400, "fp-jumanji-bd", state=JobState.FAILED)
    _scanned(mgr, 401, "fp-jumanji-uhd", state=JobState.FAILED)
    assert mgr._merge_candidates(bluray) == []


def test_an_unscanned_job_offers_no_merge(tmp_path):
    """No fingerprint means no identity; treating that as a match would offer to
    merge every freshly added disc into every other."""
    mgr = _mgr(tmp_path)
    a = _scanned(mgr, 1, None, state=JobState.SCANNING)
    _scanned(mgr, 2, None, state=JobState.SCANNING)
    assert mgr._merge_candidates(a) == []


# --- a worker's address ------------------------------------------------------

def test_a_worker_reports_the_address_that_reaches_the_cluster(monkeypatch):
    """A worker has several addresses — ZeroTier, a LAN NIC, Docker bridges,
    loopback — and only the one the cluster sees is any use for reaching it.
    Asking the routing table picks that one; resolving the local hostname picks
    whatever the resolver likes, which on these boxes is often 127.0.1.1."""
    import socket as _socket
    from blurayripper import worker as worker_mod

    asked = {}

    class _Sock:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def settimeout(self, t): pass
        def connect(self, addr): asked["to"] = addr
        def getsockname(self): return ("192.0.2.9", 51820)

    monkeypatch.setattr(_socket, "socket", lambda *a, **k: _Sock())
    got = worker_mod.local_address("postgresql://u:p@192.0.2.38:5432/brr")
    assert got == "192.0.2.9"
    assert asked["to"][0] == "192.0.2.38", "must route toward the cluster"


def test_a_hostname_database_url_is_never_resolved(monkeypatch):
    """connect() on a NAME does a DNS lookup, and settimeout does not cover
    getaddrinfo — so a hostname would block the heartbeat on the resolver for as
    long as it felt like taking. Costing a worker its heartbeat to decorate a
    panel is not a trade worth making: fall back to the default route."""
    import socket as _socket
    from blurayripper import worker as worker_mod

    asked = {}

    class _Sock:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def settimeout(self, t): pass
        def connect(self, addr): asked["to"] = addr
        def getsockname(self): return ("192.168.1.50", 51820)

    monkeypatch.setattr(_socket, "socket", lambda *a, **k: _Sock())
    worker_mod.local_address("postgresql://u:p@db.internal.example:5432/brr")
    assert asked["to"][0] == "8.8.8.8", "a hostname must not be resolved here"


def test_an_address_that_cannot_be_worked_out_is_not_fatal(monkeypatch):
    """No route, no network yet, a locked-down box: the panel loses one line of
    decoration. It must never cost the worker its heartbeat."""
    import socket as _socket
    from blurayripper import worker as worker_mod

    def boom(*a, **k): raise OSError("network is unreachable")
    monkeypatch.setattr(_socket, "socket", boom)
    assert worker_mod.local_address("postgresql://u:p@192.0.2.38:5432/brr") is None


def test_the_workers_panel_is_actually_given_each_box_s_address(tmp_path):
    """The whole point is seeing it in the UI. The store can hold the address and
    the worker can report it, and the panel still shows nothing if serve drops it
    on the way out — which is a wire with no other symptom."""
    from blurayripper.persistence import WorkerInfo

    mgr = _mgr(tmp_path)
    mgr._workers_cache = [
        WorkerInfo("handbreak01", "0.1.0", 49, "Encode: working", 3.0, 7200.0,
                   "192.0.2.9"),
        WorkerInfo("older-box", "0.1.0", None, "idle", 3.0, 60.0, None),
    ]
    panel = {w["id"]: w for w in mgr.snapshot()["workers"]}
    assert panel["handbreak01"]["address"] == "192.0.2.9"
    # A worker that predates this, or can't work its address out, still lists.
    assert panel["older-box"]["address"] is None


# --- a job name has to say WHICH DISC ----------------------------------------

def _tv_job(label, *eps, show="House"):
    from blurayripper.models import DiscInfo
    wj = WebJob(id=234, source="disc:0", state=JobState.FAILED)
    wj.disc = DiscInfo(source="disc:0", disc_name=label, volume_label=label, titles=[])
    wj.assignments = {
        i: EpisodeAssignment(title_id=i, kind=MediaKind.TV, show_or_movie_title=show,
                             season=s, episode=e)
        for i, (s, e) in enumerate(eps)}
    return wj


def test_a_tv_job_names_the_disc_it_came_off(tmp_path):
    """Re-ripping a bad rip means finding the physical disc. "House S05E06–E10"
    sends you to an episode guide to work out that those five are on disc 2 —
    while the label already said so when the disc was scanned."""
    m = _mgr(tmp_path)
    wj = _tv_job("HOUSE_S5_D2", (5, 6), (5, 7), (5, 8), (5, 9), (5, 10))
    assert m._display_name(wj) == "House S05 D02 E06–E10"


def test_a_label_with_only_a_disc_number_still_narrows_the_shelf(tmp_path):
    """Plenty of sets label discs "FIREFLY_D1" with no season at all. The disc
    number alone is still the difference between one case and five."""
    m = _mgr(tmp_path)
    # The label gives no season, so the one you entered when naming stands in.
    assert m._display_name(_tv_job("FIREFLY_D1", (1, 1), (1, 2), show="Firefly")) \
        == "Firefly S01 D01 E01–E02"


def test_a_label_that_says_nothing_leaves_the_name_alone(tmp_path):
    """Every disc in some sets reports a bare "HOUSE". Inventing a marker there
    would be worse than none — it would send you to a disc that doesn't exist."""
    m = _mgr(tmp_path)
    assert m._display_name(_tv_job("HOUSE", (5, 6), (5, 7))) == "House S05E06–E07"


def test_the_marker_follows_the_label_not_the_episodes(tmp_path):
    """A box set occasionally labels a disc with a season its contents disagree
    with. The marker exists to match the printing on the case, so the LABEL wins
    — the episode range right next to it already says what is actually on it."""
    m = _mgr(tmp_path)
    assert m._display_name(_tv_job("HOUSE_S1_D3", (2, 3), (2, 4))) \
        == "House S01 D03 E03–E04"


def test_a_movie_gets_no_disc_marker(tmp_path):
    """A film is one disc; "D1" on it is noise, and the year is what identifies
    a movie."""
    from blurayripper.models import DiscInfo
    m = _mgr(tmp_path)
    wj = WebJob(id=1, source="disc:0")
    wj.disc = DiscInfo(source="disc:0", disc_name="JUMANJI_D1",
                       volume_label="JUMANJI_D1", titles=[])
    wj.assignments = {0: EpisodeAssignment(0, MediaKind.MOVIE, "Jumanji", 1995)}
    assert m._display_name(wj) == "Jumanji (1995)"


def test_a_job_that_failed_before_naming_still_points_at_the_disc(tmp_path):
    """This is the case that needs it most: nothing was confirmed, so the
    episode range doesn't exist yet, and the label is the only thing that can
    tell you which disc to put back in."""
    from blurayripper.models import DiscInfo
    m = _mgr(tmp_path)
    wj = WebJob(id=234, source="disc:0", state=JobState.FAILED)
    wj.disc = DiscInfo(source="disc:0", disc_name="HOUSE_S5_D2",
                       volume_label="HOUSE_S5_D2", titles=[])
    wj.show_title = "House"
    wj.season = 5
    assert m._display_name(wj) == "House S05 D02"


def test_a_two_digit_disc_number_is_not_padded_further(tmp_path):
    """Box sets get past nine discs; D10 must stay D10."""
    m = _mgr(tmp_path)
    assert m._display_name(_tv_job("HOUSE_S5_D10", (5, 6), (5, 7))) \
        == "House S05 D10 E06–E07"


def test_the_label_season_wins_over_the_one_you_typed(tmp_path):
    """"S05 D02" is the disc's address on the shelf, so both halves come off the
    label when it has them. The episode numbers beside it still say what is
    actually on the disc."""
    m = _mgr(tmp_path)
    assert m._display_name(_tv_job("HOUSE_S5_D2", (9, 6), (9, 7))) \
        == "House S05 D02 E06–E07"


def test_a_single_episode_disc_reads_cleanly(tmp_path):
    m = _mgr(tmp_path)
    assert m._display_name(_tv_job("HOUSE_S5_D2", (5, 6))) == "House S05 D02 E06"


def test_an_unnamed_job_whose_label_has_only_a_disc_still_shows_it(tmp_path):
    """Nothing confirmed and no season on the label — the disc number is all
    there is, and it is still the difference between one case and five."""
    from blurayripper.models import DiscInfo
    m = _mgr(tmp_path)
    wj = WebJob(id=9, source="disc:0", state=JobState.FAILED)
    wj.disc = DiscInfo(source="disc:0", disc_name="FIREFLY_D3",
                       volume_label="FIREFLY_D3", titles=[])
    wj.show_title = "Firefly"
    assert m._display_name(wj) == "Firefly D03"


# --- a disc carrying two cuts of one film ------------------------------------

def _movie_disc(*seconds, label="FIFTY_SHADES"):
    from blurayripper.models import DiscInfo, Title
    return DiscInfo(
        source="disc:0", disc_name=label, volume_label=label,
        titles=[Title(title_id=i, duration_seconds=s, chapter_count=12,
                      source_filename=f"{i:05d}.mpls")
                for i, s in enumerate(seconds)])


def test_both_cuts_of_a_film_are_suggested_not_just_the_longest(tmp_path):
    """Taken from job #363, Fifty Shades of Grey: the disc offered 7512s and
    7711s — a theatrical and an unrated cut — and only the longer was ripped.
    The job completed, the file looked right, and one of the two films on the
    disc simply did not exist."""
    m = _mgr(tmp_path)
    disc = _movie_disc(7512, 7711, 330, 149, 150)
    assert m._suggested_titles(disc) == {0, 1}


def test_the_second_cut_is_found_however_the_disc_orders_its_titles(tmp_path):
    """Job #361, Fifty Shades Darker: the cuts were titles 0 and 3, with trailers
    in between. Nothing may assume the features come first or adjacently."""
    m = _mgr(tmp_path)
    assert m._suggested_titles(_movie_disc(7068, 325, 325, 7888)) == {0, 3}


def test_extras_are_still_left_out(tmp_path):
    """The gate only helps if it stays quiet on an ordinary disc. Norbit's
    longest bonus feature ran 1293s against a 6125s film — a fifth of it."""
    m = _mgr(tmp_path)
    assert m._suggested_titles(_movie_disc(6125, 509, 1293, 244, 290, 711)) == {0}
    assert m._suggested_titles(_movie_disc(6417, 216)) == {0}                 # Whiplash
    assert m._suggested_titles(_movie_disc(5178, 208, 135, 135, 359, 199)) == {0}  # Sherlock Gnomes


def test_a_single_cut_disc_still_goes_straight_through(tmp_path):
    """One suggested title is what keeps a normal film hands-off: auto-selected,
    and auto-ripped when TMDB matches it exactly. Opening the gate for every
    disc would be a worse bug than the one being fixed."""
    m = _mgr(tmp_path)
    assert len(m._suggested_titles(_movie_disc(6125, 509, 244))) == 1


def test_two_cuts_without_editions_are_refused_rather_than_overwritten(tmp_path):
    """Both cuts carry the same title and year, so with no Edition they compute
    the SAME path and the second replaces the first — the original loss moved one
    step later, where it would be even harder to notice."""
    import pytest as _pytest
    m = _mgr(tmp_path)
    both = [EpisodeAssignment(0, MediaKind.MOVIE, "Fifty Shades of Grey", 2015),
            EpisodeAssignment(1, MediaKind.MOVIE, "Fifty Shades of Grey", 2015)]
    with _pytest.raises(ValueError, match="overwrite"):
        m._refuse_colliding_assignments(both)


def test_two_cuts_with_distinct_editions_are_accepted(tmp_path):
    """Which is the whole point: Plex shows them as two versions of one film."""
    m = _mgr(tmp_path)
    m._refuse_colliding_assignments([
        EpisodeAssignment(0, MediaKind.MOVIE, "Fifty Shades of Grey", 2015,
                          edition="Theatrical"),
        EpisodeAssignment(1, MediaKind.MOVIE, "Fifty Shades of Grey", 2015,
                          edition="Unrated"),
    ])


def test_two_titles_mapped_to_one_episode_are_caught_too(tmp_path):
    """The same guard, for the other way a mapping silently loses a file."""
    import pytest as _pytest
    m = _mgr(tmp_path)
    with _pytest.raises(ValueError, match="overwrite"):
        m._refuse_colliding_assignments([
            EpisodeAssignment(0, MediaKind.TV, "House", 2004, season=1, episode=3),
            EpisodeAssignment(1, MediaKind.TV, "House", 2004, season=1, episode=3),
        ])


def test_a_two_parter_and_a_title_sharing_its_second_episode_are_refused(tmp_path):
    """E11-E12 and E12 save as different files, so the path check cannot see
    it — but Plex would hold two different E12s."""
    import pytest as _pytest
    m = _mgr(tmp_path)
    with _pytest.raises(ValueError, match="both include S08E12"):
        m._refuse_colliding_assignments([
            EpisodeAssignment(2, MediaKind.TV, "HIMYM", 2005, season=8, episode=11, episode_end=12),
            EpisodeAssignment(3, MediaKind.TV, "HIMYM", 2005, season=8, episode=12),
        ])
    m._refuse_colliding_assignments([                      # moved on by one: fine
        EpisodeAssignment(2, MediaKind.TV, "HIMYM", 2005, season=8, episode=11, episode_end=12),
        EpisodeAssignment(3, MediaKind.TV, "HIMYM", 2005, season=8, episode=13),
    ])


def test_an_ordinary_single_movie_mapping_is_untouched(tmp_path):
    m = _mgr(tmp_path)
    m._refuse_colliding_assignments(
        [EpisodeAssignment(0, MediaKind.MOVIE, "Whiplash", 2014)])


def test_confirming_two_colliding_cuts_is_refused_through_set_mapping(tmp_path):
    """Through the real entry point, not the helper. The guard existing is no
    use if nothing calls it, and a deleted call is invisible to a test that
    reaches past set_mapping."""
    import pytest as _pytest
    m = _mgr(tmp_path)
    wj = WebJob(id=363, source="disc:0", state=JobState.AWAITING_MAPPING)
    wj.disc = _movie_disc(7512, 7711)
    m.jobs[wj.id] = wj

    with _pytest.raises(ValueError, match="overwrite"):
        m.set_mapping(363, [
            EpisodeAssignment(0, MediaKind.MOVIE, "Fifty Shades of Grey", 2015),
            EpisodeAssignment(1, MediaKind.MOVIE, "Fifty Shades of Grey", 2015),
        ])
    # and the job stays at the gate, so the naming can simply be corrected
    assert wj.state is JobState.AWAITING_MAPPING
    assert not wj.assignments


async def test_a_movie_runtime_lookup_tolerates_tmdb_being_unhelpful(tmp_path):
    """The runtime is a hint for labelling cuts, never a dependency. TMDB
    rate-limiting, a film with no runtime recorded, or a dead network must leave
    naming working rather than failing the job."""
    from blurayripper.services.metadata import TMDBClient

    client = TMDBClient.__new__(TMDBClient)

    async def boom(path, params): raise OSError("TMDB unreachable")
    client._get = boom
    assert await client.movie_runtime(550) is None

    async def no_runtime(path, params): return {"id": 550}
    client._get = no_runtime
    assert await client.movie_runtime(550) is None

    async def zero(path, params): return {"id": 550, "runtime": 0}
    client._get = zero
    assert await client.movie_runtime(550) is None

    async def ok(path, params): return {"id": 550, "runtime": 125}
    client._get = ok
    assert await client.movie_runtime(550) == 125
