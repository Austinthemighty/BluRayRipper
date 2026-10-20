"""The HTTP layer of the web UI: routing, parsing, status codes, response shapes.

Everything the browser talks to goes through these endpoints, so a broken
contract here (a 500 where the UI expects a 400, a renamed response key, a path
parameter that stops selecting the right thing) breaks the UI even when the
manager underneath is perfectly healthy. These tests drive `create_app` with
FastAPI's TestClient and assert the *contract*, not the manager's state machine.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from blurayripper.config import load_settings
from blurayripper.models import EpisodeAssignment, MediaKind
from blurayripper.persistence import MemoryStore, WorkerInfo, signal_key
from blurayripper.pipeline import Job, JobState
from blurayripper.tools.makemkv import Drive
from blurayripper.web.app import create_app
from blurayripper.web.manager import WebJob


class DurableMem(MemoryStore):
    """A MemoryStore that claims to be the shared Postgres store."""
    durable = True


class _DeadTask:
    """Stands in for the asyncio.Task a spawned job would own — already done, so
    nothing reads it as live work."""

    def done(self) -> bool:
        return True

    def cancel(self) -> None:
        pass


def _no_spawn(manager):
    """Stop the manager launching real pipeline coroutines (rip/encode/OCR).

    The endpoints under test hand off to the manager, which spawns background
    work; we want the state changes, not a makemkvcon subprocess.
    """
    def spawn(coro):
        coro.close()
        return _DeadTask()

    manager._spawn = spawn


def _app(tmp_path, toml: str = ""):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "settings.toml").write_text(toml, encoding="utf-8")
    settings = load_settings(tmp_path)
    settings.config_dir = tmp_path
    settings.database_url = None          # never reach for a real Postgres
    settings.storage.output_dir = tmp_path / "out"
    settings.storage.staging_dir = tmp_path / "staging"
    settings.notify_url = None
    app = create_app(settings)
    _no_spawn(app.state.manager)
    return app


@pytest.fixture
def app(tmp_path):
    return _app(tmp_path)


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def manager(app):
    return app.state.manager


def _job(manager, job_id=1, state=JobState.AWAITING_TITLES, source="disc:0", **kw):
    wj = WebJob(id=job_id, source=source, state=state, **kw)
    manager.jobs[job_id] = wj
    return wj


def _scanned_job(manager, job_id=1, state=JobState.AWAITING_TITLES, source="disc:0"):
    wj = _job(manager, job_id, state, source)
    wj._pipeline_job = Job(source=source)
    wj._pipeline_job.state = state
    return wj


# --------------------------------------------------------------------------
# static shell
# --------------------------------------------------------------------------

def test_index_is_served_with_cache_defeating_headers(client):
    """The UI is a single static page. Without no-store the browser keeps running
    the OLD page after a pull+restart, so a shipped fix appears not to work."""
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "no-store" in r.headers["cache-control"]


def test_manifest_describes_an_installable_standalone_app(client):
    """'Add to Home Screen' only gives a fullscreen app if the manifest says
    standalone and points at an icon that exists."""
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    body = r.json()
    assert body["display"] == "standalone" and body["start_url"] == "/"
    assert body["icons"][0]["src"] == "/icon.svg"
    assert client.get(body["icons"][0]["src"]).status_code == 200


def test_icon_is_served_as_cacheable_svg(client):
    """The icon is generated inline, not a file — it has to arrive with the SVG
    media type or the browser/home-screen install silently ignores it."""
    r = client.get("/icon.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert r.text.startswith("<svg") and "</svg>" in r.text
    assert "max-age" in r.headers.get("cache-control", "")


# --------------------------------------------------------------------------
# /api/drives
# --------------------------------------------------------------------------

def test_drives_serves_the_watcher_cache_without_rescanning(client, manager):
    """A per-request MakeMKV scan can block for seconds on a wedged drive; the
    panel must render from the watcher's cache instead."""
    manager._drives_cache = [Drive(index=0, name="BD-RE", device="F:", disc_name="WARGAMES")]

    async def boom():
        raise AssertionError("must not rescan when the cache is populated")
    manager.list_drives = boom

    r = client.get("/api/drives")
    assert r.status_code == 200
    assert r.json() == [{"index": 0, "name": "BD-RE", "device": "F:",
                         "disc_name": "WARGAMES", "label": None,
                         "os_detected": False, "makemkv_blind": False,
                         # Unknown until a disc has been opened in that drive,
                         # and no capacity to read on a fake 'F:' off Windows.
                         "libredrive": None, "disc_bytes": None}]


def test_drives_falls_back_to_a_live_scan_and_keeps_the_result(client, manager):
    """Right after startup the watcher hasn't run yet — a one-off scan stops the
    panel being blank, and caching it stops the NEXT request scanning again."""
    scans = []

    async def scan():
        scans.append(1)
        return [Drive(index=2, name="HL-DT-ST", device="/dev/sr0", disc_name=None,
                      os_detected=True, makemkv_blind=True)]
    manager.list_drives = scan

    body = client.get("/api/drives").json()
    assert body[0]["index"] == 2 and body[0]["device"] == "/dev/sr0"
    assert body[0]["os_detected"] is True and body[0]["makemkv_blind"] is True
    client.get("/api/drives")
    assert len(scans) == 1          # second request came from the cache


def test_drives_without_makemkv_is_a_400_carrying_the_reason(client, manager):
    """A missing makemkvcon is a configuration problem the user can fix — it has
    to surface as the tool's own message, not a blank 500."""
    from blurayripper.config import ToolNotFoundError

    async def scan():
        raise ToolNotFoundError("makemkvcon not found; set BLURAYRIPPER_MAKEMKVCON")
    manager.list_drives = scan

    r = client.get("/api/drives")
    assert r.status_code == 400
    assert "makemkvcon not found" in r.json()["detail"]


def test_drives_scan_timeout_is_a_503_that_says_it_will_retry(client, manager):
    """A drive that stops answering must not read as 'no drives' or as a client
    error — 503 tells the UI to keep the panel and wait for the watcher."""
    async def scan():
        raise TimeoutError

    manager.list_drives = scan
    r = client.get("/api/drives")
    assert r.status_code == 503
    assert "timed out" in r.json()["detail"]


def test_drive_custom_name_round_trips_into_the_drive_list(client, manager):
    """Naming a drive ('Top drive') is how you tell two identical BD-REs apart;
    the label has to come back on the very next /api/drives read."""
    manager._drives_cache = [Drive(index=0, name="BD-RE", device="F:", disc_name=None)]

    assert client.post("/api/drives/name", json={"device": "F:", "name": "Top"}).json() == {"ok": True}
    assert client.get("/api/drives").json()[0]["label"] == "Top"

    client.post("/api/drives/name", json={"device": "F:", "name": None})
    assert client.get("/api/drives").json()[0]["label"] is None


# --------------------------------------------------------------------------
# eject / close tray
# --------------------------------------------------------------------------

def test_eject_forgets_the_disc_that_came_out(client, manager, monkeypatch):
    """The cached disc name otherwise keeps describing the ejected disc, so the
    panel lies and re-inserting the same disc never re-triggers a scan."""
    import blurayripper.web.app as app_mod

    monkeypatch.setattr(app_mod, "eject", lambda device: None)
    manager._drives_cache = [Drive(index=0, name="BD-RE", device="F:", disc_name="WARGAMES")]

    assert client.post("/api/drives/eject", json={"device": "F:"}).json() == {"ok": True}
    assert client.get("/api/drives").json()[0]["disc_name"] is None


def test_failed_eject_is_a_400_and_leaves_the_disc_on_record(client, manager, monkeypatch):
    """If the tray never opened the disc is still in there — reporting success (or
    clearing the cached name) would tell the user to swap a disc that never came
    out."""
    import blurayripper.web.app as app_mod

    def stuck(device):
        raise OSError("device busy")
    monkeypatch.setattr(app_mod, "eject", stuck)
    manager._drives_cache = [Drive(index=0, name="BD-RE", device="F:", disc_name="WARGAMES")]

    r = client.post("/api/drives/eject", json={"device": "F:"})
    assert r.status_code == 400
    assert "Eject failed" in r.json()["detail"] and "device busy" in r.json()["detail"]
    assert client.get("/api/drives").json()[0]["disc_name"] == "WARGAMES"


def test_close_tray_reports_success_and_maps_failure_to_400(client, monkeypatch):
    """Closing the tray is how the UI loads the next disc remotely; a failure has
    to say so rather than pretend the disc is in."""
    import blurayripper.web.app as app_mod
    calls = []

    monkeypatch.setattr(app_mod, "close_tray", lambda device: calls.append(device))
    assert client.post("/api/drives/close", json={"device": "F:"}).json() == {"ok": True}
    assert calls == ["F:"]

    def stuck(device):
        raise OSError("no tray")
    monkeypatch.setattr(app_mod, "close_tray", stuck)
    r = client.post("/api/drives/close", json={})
    assert r.status_code == 400 and "Close tray failed" in r.json()["detail"]


# --------------------------------------------------------------------------
# adding + listing jobs
# --------------------------------------------------------------------------

def test_jobs_snapshot_is_served_as_the_queue_payload(client, manager):
    """Every poll of the queue goes through here; the UI reads jobs/version/
    workers off the top level."""
    _job(manager, 3, JobState.FAILED, source="disc:1")
    body = client.get("/api/jobs").json()
    assert {"jobs", "version", "workers", "uptime"} <= set(body)
    assert [j["id"] for j in body["jobs"]] == [3]
    assert body["jobs"][0]["state"] == "failed"


def test_add_job_returns_the_created_ids(client, manager, tmp_path):
    """The UI navigates straight to the new job, so the POST must hand back the
    id(s) the manager minted."""
    mkv = tmp_path / "Blade Runner.mkv"
    mkv.write_bytes(b"")

    r = client.post("/api/jobs", json={"source": str(mkv)})
    assert r.status_code == 200
    ids = r.json()["ids"]
    assert len(ids) == 1
    assert manager.jobs[ids[0]].source == str(mkv)


def test_add_job_with_a_path_that_isnt_there_is_a_400(client, tmp_path):
    """Typing a mapped drive letter the server can't see is the single most common
    mistake — it must come back as the manager's explanation, not a 500."""
    r = client.post("/api/jobs", json={"source": str(tmp_path / "nope.mkv")})
    assert r.status_code == 400
    assert "Nothing at" in r.json()["detail"]


def test_add_job_requires_a_source_field(client):
    """The body is a typed model: a missing source is rejected before any manager
    call, so a malformed request can't create a half-built job."""
    r = client.post("/api/jobs", json={})
    assert r.status_code == 422


# --------------------------------------------------------------------------
# the gates: titles / mapping / grain / match
# --------------------------------------------------------------------------

def test_submit_titles_parses_the_id_list_and_the_path_id(client, manager):
    """The title gate is the first human decision; the ids in the body and the job
    id in the URL both have to arrive as ints."""
    wj = _job(manager, 4, JobState.AWAITING_TITLES)
    r = client.post("/api/jobs/4/titles", json={"title_ids": [2, 5]})
    assert r.json() == {"ok": True}
    assert wj.selected_title_ids == [2, 5]
    assert wj.state is JobState.AWAITING_MAPPING


def test_submit_titles_for_an_unknown_job_is_404(client):
    """A stale browser tab posting to a removed job must get 404, not a 500 from
    the manager's KeyError."""
    r = client.post("/api/jobs/999/titles", json={"title_ids": [1]})
    assert r.status_code == 404 and r.json()["detail"] == "Job not found."


def test_submit_titles_rejects_an_empty_selection_with_the_reason(client, manager):
    """Confirming nothing would queue a job with no titles to rip; the manager's
    complaint has to reach the user."""
    _job(manager, 4, JobState.AWAITING_TITLES)
    r = client.post("/api/jobs/4/titles", json={"title_ids": []})
    assert r.status_code == 400 and "No titles selected" in r.json()["detail"]


def test_submit_mapping_builds_assignments_from_the_posted_fields(client, manager):
    """The naming form is the whole identity of the output file — every field it
    sends (kind, title, year, edition, grain) has to land on the assignment."""
    wj = _scanned_job(manager, 6, JobState.AWAITING_MAPPING)
    r = client.post("/api/jobs/6/mapping", json={
        "assignments": [{"title_id": 1, "kind": "movie",
                         "show_or_movie_title": "Blade Runner", "year": 1982,
                         "edition": "Final Cut"}],
        "grain_mode": "on", "tune_grain": True,
    })
    assert r.json() == {"ok": True}
    a = wj.assignments[1]
    assert a.kind is MediaKind.MOVIE and a.show_or_movie_title == "Blade Runner"
    assert a.year == 1982 and a.edition == "Final Cut"
    assert wj.grain_mode == "on" and wj.tune_grain is True


def test_submit_mapping_carries_tv_season_and_episode(client, manager):
    """A TV assignment without season/episode can't resolve a path — those two
    optional fields must survive the trip through the request model."""
    wj = _scanned_job(manager, 6, JobState.AWAITING_MAPPING)
    client.post("/api/jobs/6/mapping", json={"assignments": [
        {"title_id": 0, "kind": "tv", "show_or_movie_title": "WarGames",
         "season": 2, "episode": 7, "episode_title": "Pilot"}]})
    a = wj.assignments[0]
    assert a.kind is MediaKind.TV and a.season == 2 and a.episode == 7
    assert a.episode_title == "Pilot"


def test_submit_mapping_rejects_an_unknown_media_kind_with_400(client, manager):
    """'movie'/'tv' come from the UI, but a stale client (or a hand-rolled POST)
    can send anything — an unknown kind is a bad request, not a crash, and must
    leave the job unnamed."""
    wj = _scanned_job(manager, 6, JobState.AWAITING_MAPPING)
    r = client.post("/api/jobs/6/mapping", json={"assignments": [
        {"title_id": 0, "kind": "documentary", "show_or_movie_title": "X"}]})
    assert r.status_code == 400 and "documentary" in r.json()["detail"]
    assert wj.assignments == {} and wj.state is JobState.AWAITING_MAPPING


def test_grain_endpoint_resolves_the_parked_gate(client, manager):
    """A job parked at AWAITING_GRAIN is blocked on a future; the POST is the only
    thing that ever completes it."""
    import asyncio

    wj = _job(manager, 8, JobState.AWAITING_GRAIN)
    loop = asyncio.new_event_loop()
    try:
        wj._grain_future = loop.create_future()
        assert client.post("/api/jobs/8/grain", json={"tune_grain": True}).json() == {"ok": True}
        assert wj._grain_future.result() is True
        # A second submit has nothing left to resolve.
        assert client.post("/api/jobs/8/grain", json={"tune_grain": False}).status_code == 400
    finally:
        loop.close()


def test_auto_identify_endpoint_passes_the_whole_form_through(client, manager):
    """Auto-identify needs the show, season and candidate episode list together —
    a field dropped in transit sends the OCR matcher after the wrong episodes."""
    wj = _scanned_job(manager, 9, JobState.AWAITING_MAPPING)
    manager.osub._api_key = "key"
    r = client.post("/api/jobs/9/auto-identify", json={
        "title_ids": [1, 2], "show_title": "WarGames", "show_year": 1983,
        "show_tmdb_id": 771, "season": 3, "episodes": [4, 5], "grain_mode": "off"})
    assert r.json() == {"ok": True}
    assert wj.auto_identify is True and wj.show_tmdb_id == 771
    assert wj.season == 3 and wj.candidate_episodes == [4, 5]
    assert wj.selected_title_ids == [1, 2] and wj.grain_mode == "off"


def test_auto_identify_without_an_opensubtitles_key_is_400(client, manager):
    """Subtitle matching can't work without the API key; saying so beats parking
    the disc in IDENTIFYING forever."""
    _scanned_job(manager, 9, JobState.AWAITING_MAPPING)
    manager.osub._api_key = None
    r = client.post("/api/jobs/9/auto-identify", json={
        "title_ids": [1], "show_title": "X", "show_tmdb_id": 1,
        "season": 1, "episodes": [1]})
    assert r.status_code == 400 and "OpenSubtitles" in r.json()["detail"]


def test_confirm_match_turns_json_string_keys_back_into_title_ids(client, manager):
    """JSON object keys are always strings; the confirmed title->episode map has
    to come back as ints or every assignment lands on the wrong title."""
    wj = _scanned_job(manager, 10, JobState.AWAITING_MATCH)
    wj.show_title, wj.season = "WarGames", 2
    r = client.post("/api/jobs/10/confirm-match", json={"mapping": {"3": 11, "4": 12}})
    assert r.json() == {"ok": True}
    assert sorted(wj.assignments) == [3, 4]
    assert wj.assignments[3].episode == 11 and wj.assignments[3].season == 2
    assert wj.assignments[4].kind is MediaKind.TV


# --------------------------------------------------------------------------
# job actions
# --------------------------------------------------------------------------

def test_retry_a_failed_job_reports_ok(client, manager):
    """Retry is the recovery button on every failed card — a 200 is what tells
    the UI to flip the card out of the failed state."""
    wj = _scanned_job(manager, 11, JobState.FAILED)
    wj.error = "boom"
    assert client.post("/api/jobs/11/retry").json() == {"ok": True}
    assert wj.state is JobState.READY and wj.error is None


def test_bump_raises_the_job_above_everything_queued(client, manager):
    """'Encode next' must actually reorder the queue — the endpoint is the only
    caller, so a routing slip silently does nothing."""
    _job(manager, 12, JobState.READY)
    other = _job(manager, 13, JobState.READY)
    other.priority = 5
    assert client.post("/api/jobs/12/bump").json() == {"ok": True}
    assert manager.jobs[12].priority > other.priority


def test_requeue_recovers_a_stalled_job(client, manager):
    """A job left ENCODING by a dead worker has nothing working it; requeue is the
    only way back to READY without re-ripping."""
    wj = _scanned_job(manager, 14, JobState.ENCODING)
    assert client.post("/api/jobs/14/requeue").json() == {"ok": True}
    assert wj.state is JobState.READY


def test_requeue_a_healthy_job_is_rejected_with_400(client, manager):
    """Requeueing live work would interrupt an encode a worker is really doing."""
    _scanned_job(manager, 14, JobState.READY)
    r = client.post("/api/jobs/14/requeue")
    assert r.status_code == 400 and "isn't stalled" in r.json()["detail"]


def test_requeue_stalled_reports_how_many_it_recovered(client, manager):
    """The bulk recovery button shows the count it gets back; a wrong count reads
    as 'nothing was broken' after a cluster-wide power cut."""
    _scanned_job(manager, 15, JobState.ENCODING)
    _scanned_job(manager, 16, JobState.ENCODING)
    _scanned_job(manager, 17, JobState.READY)      # healthy — must not be touched
    r = client.post("/api/jobs/requeue-stalled")
    assert r.json() == {"ok": True, "requeued": 2}
    assert manager.jobs[17].state is JobState.READY


def test_edit_job_applies_grain_and_edition_before_the_encode(client, manager):
    """The edit form sends three optional fields; each has to reach the manager in
    its own argument slot, or 'Director's Cut' lands on the grain setting."""
    wj = _scanned_job(manager, 18, JobState.READY)
    wj.assignments = {0: EpisodeAssignment(0, MediaKind.MOVIE, "Blade Runner", 1982)}
    r = client.post("/api/jobs/18/edit",
                    json={"grain_mode": "on", "tune_grain": True, "edition": "Director's Cut"})
    assert r.json() == {"ok": True}
    assert wj.grain_mode == "on" and wj.tune_grain is True
    assert wj.assignments[0].edition == "Director's Cut"


def test_edit_a_finished_job_is_rejected(client, manager):
    """Nothing left to re-encode — accepting the edit would show a change that
    never reaches a file."""
    _scanned_job(manager, 18, JobState.DONE)
    r = client.post("/api/jobs/18/edit", json={"grain_mode": "on"})
    assert r.status_code == 400 and "already finished" in r.json()["detail"]


def test_rename_coerces_the_year_the_form_sends_as_text(client, manager):
    """The year input posts a string; the assignment needs an int or the placed
    folder ends up named "Movie ('1982')"."""
    wj = _scanned_job(manager, 19, JobState.DONE)
    wj.assignments = {0: EpisodeAssignment(0, MediaKind.MOVIE, "Bladerunner", 1990)}
    r = client.post("/api/jobs/19/rename", json={"title": "Blade Runner", "year": "1982"})
    assert r.json() == {"ok": True}
    assert wj.assignments[0].show_or_movie_title == "Blade Runner"
    assert wj.assignments[0].year == 1982


def test_rename_treats_a_blank_year_as_no_year(client, manager):
    """Clearing the year field must mean 'unknown', not int('') blowing up."""
    wj = _scanned_job(manager, 19, JobState.DONE)
    wj.assignments = {0: EpisodeAssignment(0, MediaKind.MOVIE, "Bladerunner", 1990)}
    assert client.post("/api/jobs/19/rename", json={"title": "Blade Runner", "year": ""}).json() == {"ok": True}
    assert wj.assignments[0].year is None


def test_rename_with_a_non_numeric_year_is_a_400(client, manager):
    """Typed junk in the year box is a bad request; it must not escape as a 500
    from int()."""
    wj = _scanned_job(manager, 19, JobState.DONE)
    wj.assignments = {0: EpisodeAssignment(0, MediaKind.MOVIE, "Bladerunner", 1990)}
    r = client.post("/api/jobs/19/rename", json={"title": "Blade Runner", "year": "nineteen"})
    assert r.status_code == 400
    assert wj.assignments[0].show_or_movie_title == "Bladerunner"   # nothing applied


def test_remap_reopens_the_naming_gate(client, manager):
    """A disc whose auto-identify never matched has no assignment to rename —
    remap is the only route back to a naming form."""
    from blurayripper.models import DiscInfo

    wj = _scanned_job(manager, 20, JobState.FAILED)
    wj.disc = DiscInfo(source="disc:0", disc_name="WARGAMES", titles=[])
    wj.selected_title_ids = [1]
    assert client.post("/api/jobs/20/remap").json() == {"ok": True}
    assert wj.state is JobState.AWAITING_MAPPING


def test_remap_a_job_with_no_scan_is_rejected(client, manager):
    """Without a scanned disc there is nothing to map; a 400 explains that instead
    of re-opening an empty form."""
    _job(manager, 20, JobState.FAILED)
    r = client.post("/api/jobs/20/remap")
    assert r.status_code == 400 and "no scanned disc" in r.json()["detail"]


def test_group_season_folds_the_sibling_jobs_and_returns_the_new_id(client, manager, tmp_path):
    """Episodes added one at a time are N jobs wanting the same name typed N
    times; the response id is what the UI opens next."""
    folder = tmp_path / "S1"
    _job(manager, 21, JobState.AWAITING_MAPPING, source=str(folder / "Show Season 1_t00.mkv"))
    _job(manager, 22, JobState.AWAITING_MAPPING, source=str(folder / "Show Season 1_t01.mkv"))

    r = client.post("/api/jobs/21/group-season")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert 21 not in manager.jobs and 22 not in manager.jobs
    assert manager.jobs[body["id"]].source == str(folder)


def test_group_season_refuses_unrelated_files(client, manager, tmp_path):
    """Grouping two separate films would name them as episodes of each other."""
    _job(manager, 21, JobState.AWAITING_MAPPING, source=str(tmp_path / "Rocky.mkv"))
    _job(manager, 22, JobState.AWAITING_MAPPING, source=str(tmp_path / "Alien.mkv"))
    r = client.post("/api/jobs/21/group-season")
    assert r.status_code == 400 and "one show's episodes" in r.json()["detail"]


def test_change_source_repoints_the_job_at_another_drive(client, manager):
    """Moving a disc to a drive that can read the bad sector is a two-step: change
    the source here, then Retry."""
    wj = _scanned_job(manager, 23, JobState.FAILED)
    assert client.post("/api/jobs/23/change-source", json={"source": "disc:1"}).json() == {"ok": True}
    assert wj.source == "disc:1" and wj._pipeline_job.source == "disc:1"


def test_change_source_rejects_something_that_isnt_a_selector(client, manager):
    """A bare path can't be a rip source; accepting it hands makemkvcon garbage."""
    _scanned_job(manager, 23, JobState.FAILED)
    r = client.post("/api/jobs/23/change-source", json={"source": "/mnt/disc"})
    assert r.status_code == 400 and "selector" in r.json()["detail"]


def test_change_source_on_an_unknown_job_is_404(client):
    """change_source signals a missing job with KeyError — the error mapper has to
    turn that into 404 rather than letting it 500."""
    r = client.post("/api/jobs/999/change-source", json={"source": "disc:1"})
    assert r.status_code == 404 and r.json()["detail"] == "Job not found."


def test_redo_subs_endpoints_report_per_job_and_bulk_counts(client, manager):
    """Re-OCR is offered both per card and for the whole library; the bulk form
    answers with how many it queued so the UI can say so."""
    for jid in (24, 25):
        wj = _scanned_job(manager, jid, JobState.DONE)
        wj.assignments = {0: EpisodeAssignment(0, MediaKind.MOVIE, "X", 2000)}
    _scanned_job(manager, 26, JobState.DONE)          # no assignments -> not eligible

    assert client.post("/api/jobs/24/redo-subs").json() == {"ok": True}
    assert manager.jobs[24]._task is not None      # the re-OCR was actually queued
    assert client.post("/api/jobs/redo-subs-all").json() == {"jobs": 2}


def test_redo_subs_without_a_naming_assignment_is_400(client, manager):
    """There is nowhere to place a .srt for an unnamed job."""
    _scanned_job(manager, 26, JobState.DONE)
    r = client.post("/api/jobs/26/redo-subs")
    assert r.status_code == 400 and "no naming assignments" in r.json()["detail"]


def test_preview_grain_is_rejected_before_the_rip_exists(client, manager):
    """The A/B preview encodes a slice of the ripped file — asking early is a bad
    request, and the awaiting endpoint has to map it like the sync ones do."""
    _scanned_job(manager, 27, JobState.READY)
    r = client.post("/api/jobs/27/preview-grain")
    assert r.status_code == 400 and "not ready for a preview" in r.json()["detail"]


def test_preview_grain_answers_only_once_the_clips_exist(client, manager):
    """The UI shows the A/B panel as soon as the POST returns, so the response must
    wait for the render — a fire-and-forget call would show an empty panel."""
    import asyncio

    rendered = []

    async def fake_preview(job_id):
        await asyncio.sleep(0)          # a real suspension point mid-render
        rendered.append(job_id)
    manager.generate_preview = fake_preview

    assert client.post("/api/jobs/27/preview-grain").json() == {"ok": True}
    assert rendered == [27]


# --------------------------------------------------------------------------
# file-serving endpoints
# --------------------------------------------------------------------------

def test_preview_stills_are_selected_by_the_path_segment(client, manager, tmp_path):
    """on/off is the whole point of an A/B preview — serving the same still for
    both makes grain look like it changes nothing."""
    on, off = tmp_path / "on.png", tmp_path / "off.png"
    on.write_bytes(b"GRAIN-ON")
    off.write_bytes(b"GRAIN-OFF")
    wj = _job(manager, 28, JobState.READY)
    wj.preview_on, wj.preview_off = on, off

    assert client.get("/api/jobs/28/preview/on").content == b"GRAIN-ON"
    assert client.get("/api/jobs/28/preview/off").content == b"GRAIN-OFF"


def test_missing_preview_is_404_not_a_broken_image(client, manager):
    """A job with no preview yet (or one whose staging was cleaned) must answer
    404 so the UI hides the panel."""
    _job(manager, 28, JobState.READY)
    assert client.get("/api/jobs/28/preview/on").status_code == 404
    assert client.get("/api/jobs/999/preview/on").status_code == 404


def test_preview_clips_are_served_as_mp4_per_side(client, manager, tmp_path):
    """The <video> element needs the mp4 media type to play (and to honour Range
    seeks); the on/off segment picks which clip."""
    on, off = tmp_path / "on.mp4", tmp_path / "off.mp4"
    on.write_bytes(b"CLIP-ON")
    off.write_bytes(b"CLIP-OFF")
    wj = _job(manager, 29, JobState.READY)
    wj.preview_on_clip, wj.preview_off_clip = on, off

    r = client.get("/api/jobs/29/preview-clip/on")
    assert r.content == b"CLIP-ON" and r.headers["content-type"] == "video/mp4"
    assert client.get("/api/jobs/29/preview-clip/off").content == b"CLIP-OFF"
    wj.preview_on_clip = None
    assert client.get("/api/jobs/29/preview-clip/on").status_code == 404


def test_artwork_is_served_from_the_placed_poster(client, manager):
    """The card thumbnail reads the poster that was placed next to the video; a
    poster that has since been deleted must 404, not 500 on a missing file."""
    a = EpisodeAssignment(0, MediaKind.MOVIE, "Blade Runner", 1982)
    wj = _job(manager, 30, JobState.DONE)
    wj.assignments = {0: a}
    folder = manager.pipeline._poster_folder(a)
    folder.mkdir(parents=True, exist_ok=True)

    assert client.get("/api/jobs/30/artwork").status_code == 404   # nothing placed yet
    (folder / "poster.jpg").write_bytes(b"JPEGDATA")
    r = client.get("/api/jobs/30/artwork")
    assert r.status_code == 200 and r.content == b"JPEGDATA"


def test_artwork_deleted_after_the_lookup_is_404(client, manager, tmp_path):
    """Art can be cleaned up between the manager naming the file and the response
    reading it; the endpoint re-checks rather than 500-ing on every card."""
    ghost = tmp_path / "poster.jpg"
    manager.artwork_path = lambda job_id: ghost
    assert client.get("/api/jobs/30/artwork").status_code == 404


def test_aacs_dump_downloads_with_a_filename_and_gzip_type(client, manager, tmp_path):
    """The dump is emailed to MakeMKV to get a key added; it has to arrive as a
    named .gz attachment rather than rendering as text."""
    dump = tmp_path / "disc_key.tar.gz"
    dump.write_bytes(b"\x1f\x8bDUMP")
    wj = _job(manager, 31, JobState.FAILED)
    wj.aacs_dump = str(dump)

    r = client.get("/api/jobs/31/aacs-dump")
    assert r.status_code == 200 and r.content == b"\x1f\x8bDUMP"
    assert r.headers["content-type"] == "application/gzip"
    assert "disc_key.tar.gz" in r.headers["content-disposition"]


def test_aacs_dump_404s_for_no_dump_and_for_a_moved_one(client, manager, tmp_path):
    """Most jobs never keep a dump, and a kept one can be moved away — both are a
    plain 404 with an explanation, not a stack trace."""
    wj = _job(manager, 31, JobState.FAILED)
    assert client.get("/api/jobs/31/aacs-dump").status_code == 404
    wj.aacs_dump = str(tmp_path / "gone.tar.gz")
    r = client.get("/api/jobs/31/aacs-dump")
    assert r.status_code == 404 and "No AACS dump" in r.json()["detail"]
    assert client.get("/api/jobs/999/aacs-dump").json()["detail"] == "Job not found."


# --------------------------------------------------------------------------
# removal + artwork refresh
# --------------------------------------------------------------------------

def test_delete_removes_the_job_and_is_idempotent(client, manager):
    """Double-clicking Remove (or removing from two tabs) must not 404 the second
    time — the card is already gone, which is what was asked for."""
    _job(manager, 32, JobState.DONE)
    assert client.delete("/api/jobs/32").json() == {"ok": True}
    assert 32 not in manager.jobs
    assert client.delete("/api/jobs/32").status_code == 200


def test_remove_by_state_only_clears_the_listed_states(client, manager):
    """'Clear failed' must not take the finished jobs with it — the states list in
    the body is the whole filter."""
    _job(manager, 33, JobState.FAILED)
    _job(manager, 34, JobState.DONE)
    _job(manager, 35, JobState.FAILED)
    r = client.post("/api/jobs/remove-by-state", json={"states": ["failed"]})
    assert r.json() == {"removed": 2}
    assert set(manager.jobs) == {34}


def test_remove_by_state_uses_the_live_state_not_the_stored_one(client, manager):
    """A job's pipeline state moves on without the WebJob copy; filtering on the
    stale field would leave a finished job behind."""
    wj = _scanned_job(manager, 36, JobState.READY)
    wj._pipeline_job.state = JobState.DONE     # live state says done
    r = client.post("/api/jobs/remove-by-state", json={"states": ["done"]})
    assert r.json() == {"removed": 1} and manager.jobs == {}


def test_artwork_refresh_endpoints_answer_with_their_own_counts(client, manager):
    """Bulk refresh reports jobs touched; the per-job form reports images placed —
    the UI shows both numbers, so the keys must not be swapped."""
    async def fake_art(assignments, report):
        return len(assignments)
    manager.pipeline.place_tmdb_art = fake_art

    for jid in (37, 38):
        wj = _job(manager, jid, JobState.DONE)
        wj.assignments = {0: EpisodeAssignment(0, MediaKind.MOVIE, "X", 2000)}
    manager.jobs[38].assignments[1] = EpisodeAssignment(1, MediaKind.MOVIE, "Y", 2001)
    _job(manager, 39, JobState.DONE)      # no assignments -> skipped

    assert client.post("/api/jobs/37/refresh-artwork").json() == {"ok": True, "placed": 1}
    assert client.post("/api/jobs/refresh-artwork").json() == {"jobs": 3}


# --------------------------------------------------------------------------
# worker + server control
# --------------------------------------------------------------------------

def test_update_workers_signals_every_connected_worker(client, manager):
    """The 'Update all workers' button has to reach the cluster-wide signal, and
    report the number of boxes it addressed."""
    manager.store = DurableMem()
    manager._workers_cache = [WorkerInfo("box1", "v1", None, "idle", 1.0),
                              WorkerInfo("box2", "v1", None, "idle", 1.0)]
    r = client.post("/api/workers/update")
    assert r.json() == {"ok": True, "workers": 2}
    assert manager.store.cluster_signals()[signal_key("update", "*")] == 1


def test_update_workers_without_a_shared_store_is_400(client, manager):
    """Standalone there are no workers to update; the button must explain that."""
    r = client.post("/api/workers/update")
    assert r.status_code == 400 and "Postgres" in r.json()["detail"]


def test_restart_is_501_when_the_server_was_not_launched_by_serve(client):
    """Under `uvicorn app:app` (or a test) there is no server handle to stop —
    the UI needs a distinct code so it can hide the button instead of retrying."""
    r = client.post("/api/server/restart", json={})
    assert r.status_code == 501 and "not launched via" in r.json()["detail"]


def test_restart_with_running_jobs_is_409_listing_them(client, app, manager):
    """Restarting kills in-flight rips. The 409 body carries the job list so the
    UI can name what would be interrupted before offering Force."""
    class _Srv:
        should_exit = False

    app.state.uvicorn_server = _Srv()

    class _LiveTask:
        def done(self):
            return False
    wj = _scanned_job(manager, 40, JobState.RIPPING)
    wj._task = _LiveTask()

    r = client.post("/api/server/restart", json={})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert [j["id"] for j in detail["active"]] == [40]
    assert "1 job(s) are still running" in detail["message"]


def test_forced_restart_records_the_pull_flag(client, app, manager):
    """'Update' and 'Restart' are the same endpoint — only `pull` distinguishes
    them, and serve reads it off app.state after the loop stops."""
    class _Srv:
        should_exit = False

    app.state.uvicorn_server = _Srv()
    r = client.post("/api/server/restart", json={"force": True, "pull": True})
    assert r.json() == {"ok": True, "restarting": True}
    assert app.state.restart_requested is True and app.state.restart_pull is True


# --------------------------------------------------------------------------
# notifications
# --------------------------------------------------------------------------

def test_notify_test_says_unconfigured_without_sending(client, manager, monkeypatch):
    """With no ntfy URL there is nowhere to POST; answering 'configured: false' is
    what makes the UI show the setup hint instead of a failure."""
    import blurayripper.web.app as app_mod
    sent = []

    async def fake_notify(url, title, message, priority):
        sent.append(url)
        return True
    monkeypatch.setattr(app_mod, "send_notify", fake_notify)

    assert client.post("/api/notify/test").json() == {"configured": False}
    assert sent == []


def test_notify_test_sends_at_the_attention_priority(client, app, monkeypatch):
    """The test alert exists to prove a phone actually rings, so it goes out at a
    high priority regardless of the configured default."""
    import blurayripper.web.app as app_mod
    calls = []

    async def fake_notify(url, title, message, priority):
        calls.append((url, title, priority))
        return True
    monkeypatch.setattr(app_mod, "send_notify", fake_notify)
    app.state.manager.settings.notify_url = "https://ntfy.example/brr"

    body = client.post("/api/notify/test").json()
    assert body == {"configured": True, "ok": True, "url": "https://ntfy.example/brr"}
    assert calls[0][0] == "https://ntfy.example/brr" and calls[0][2] == "3"


def test_settings_notify_test_uses_the_settings_just_saved(client, monkeypatch, tmp_path):
    """Saved settings only apply on restart — but the point of this button is to
    test what you JUST typed, so it reloads from disk rather than using the
    process's own (here: empty) config."""
    import blurayripper.config as config_mod
    import blurayripper.services.notify as notify_mod

    fresh = load_settings(tmp_path)
    fresh.notify_url = "https://ntfy.example/fresh"
    fresh.notify_priority = "urgent"
    monkeypatch.setattr(config_mod, "load_settings", lambda *a, **k: fresh)

    seen = {}

    async def fake_notify(url, title, message, priority):
        seen["url"], seen["priority"] = url, priority
        return True
    monkeypatch.setattr(notify_mod, "notify", fake_notify)

    body = client.post("/api/settings/notify-test").json()
    assert body["ok"] is True
    assert seen == {"url": "https://ntfy.example/fresh", "priority": "urgent"}
    assert "https://ntfy.example/fresh" in body["detail"] and "urgent" in body["detail"]


def test_settings_notify_test_reports_an_unreachable_url(client, monkeypatch, tmp_path):
    """A POST that doesn't land must not read as success — the whole point is to
    catch a wrong URL before relying on the alerts."""
    import blurayripper.config as config_mod
    import blurayripper.services.notify as notify_mod

    fresh = load_settings(tmp_path)
    fresh.notify_url = "https://ntfy.example/bad"
    monkeypatch.setattr(config_mod, "load_settings", lambda *a, **k: fresh)

    async def fake_notify(url, title, message, priority):
        return False
    monkeypatch.setattr(notify_mod, "notify", fake_notify)

    body = client.post("/api/settings/notify-test").json()
    assert body["ok"] is False and "did not succeed" in body["detail"]


def test_settings_notify_test_with_no_url_asks_you_to_save_one(client, monkeypatch, tmp_path):
    """Before anything is configured the button explains the next step instead of
    reporting a delivery failure."""
    import blurayripper.config as config_mod
    import blurayripper.services.notify as notify_mod

    fresh = load_settings(tmp_path)
    fresh.notify_url = None
    monkeypatch.setattr(config_mod, "load_settings", lambda *a, **k: fresh)

    async def unexpected(url, title, message, priority):
        return True                     # would look like success if it were called
    monkeypatch.setattr(notify_mod, "notify", unexpected)

    body = client.post("/api/settings/notify-test").json()
    assert body["ok"] is False and "No ntfy URL" in body["detail"]


# --------------------------------------------------------------------------
# OpenSubtitles quota
# --------------------------------------------------------------------------

def test_quota_reports_unconfigured_without_calling_the_api(client, manager):
    """No API key means no account to ask — the panel hides itself off this."""
    manager.osub._api_key = None

    async def boom():
        raise AssertionError("must not call the API without a key")
    manager.osub.user_info = boom

    assert client.get("/api/opensubtitles/quota").json() == {"configured": False}


def test_quota_renames_the_account_fields_for_the_ui(client, manager):
    """The API's remaining_downloads/allowed_downloads/downloads_count are
    re-keyed here; a rename slip shows a blank quota panel."""
    manager.osub._api_key = "key"

    async def info():
        return {"remaining_downloads": 17, "allowed_downloads": 20,
                "downloads_count": 3, "level": "Sub leecher", "vip": False}
    manager.osub.user_info = info

    body = client.get("/api/opensubtitles/quota").json()
    assert body == {"configured": True, "source": "account", "remaining": 17,
                    "allowed": 20, "used": 3, "level": "Sub leecher", "vip": False}


def test_quota_falls_back_to_the_last_download_when_login_fails(client, manager):
    """Many API keys can't use /login at all; the quota captured from the last
    /download response is the only number available then."""
    manager.osub._api_key = "key"

    async def info():
        raise RuntimeError("401 login not permitted")
    manager.osub.user_info = info
    manager.osub._last_quota = {"remaining": 4, "reset_time_utc": "2026-08-22T00:00:00Z"}

    body = client.get("/api/opensubtitles/quota").json()
    assert body["source"] == "last-download" and body["remaining"] == 4
    assert body["reset_time"] == "2026-08-22T00:00:00Z"
    assert "login not permitted" in body["note"]


def test_quota_reports_the_error_when_nothing_is_known_yet(client, manager):
    """Before the first reference download there is no fallback — the reason has
    to reach the panel rather than showing an empty quota."""
    manager.osub._api_key = "key"

    async def info():
        raise RuntimeError("network unreachable")
    manager.osub.user_info = info
    manager.osub._last_quota = None

    body = client.get("/api/opensubtitles/quota").json()
    assert body == {"configured": True, "error": "network unreachable"}


# --------------------------------------------------------------------------
# HandBrake preset editor
# --------------------------------------------------------------------------

def _preset_file(tmp_path, **over):
    preset = {"PresetName": "Plex H265 1080p", "VideoEncoder": "x265_10bit",
              "VideoQualitySlider": 18, "VideoPreset": "medium", "VideoTune": "",
              "VideoProfile": "auto", "VideoLevel": "auto", "VideoOptionExtra": "",
              "VideoFramerateMode": "vfr", "AudioEncoderFallback": "av_aac"}
    preset.update(over)
    path = tmp_path / "presets.json"
    path.write_text(json.dumps({"PresetList": [preset]}), encoding="utf-8")
    return path


def test_handbrake_payload_reports_the_active_presets_values(client, manager, tmp_path):
    """The encode editor renders straight off this payload — the preset name and
    each field's current value have to be the ones actually in use."""
    manager.settings.encode.preset_file = _preset_file(tmp_path, VideoPreset="slower")
    manager.settings.encode.preset_name = "Plex H265 1080p"

    body = client.get("/api/handbrake").json()
    assert body["preset_name"] == "Plex H265 1080p"
    fields = {f["key"]: f["value"] for f in body["fields"]}
    assert fields["VideoPreset"] == "slower" and fields["VideoQualitySlider"] == 18


def test_handbrake_payload_with_an_unreadable_preset_file_is_400(client, manager, tmp_path):
    """A preset path that points at nothing is a config error to show, not a 500
    on every load of the encode page."""
    manager.settings.encode.preset_file = tmp_path / "missing.json"
    r = client.get("/api/handbrake")
    assert r.status_code == 400


def test_handbrake_save_writes_the_overlay_and_asks_for_a_restart(client, manager, tmp_path):
    """The resolved preset is read at startup, so the UI must be told the change
    only lands after a restart."""
    manager.settings.encode.preset_file = _preset_file(tmp_path)

    r = client.post("/api/handbrake", json={"VideoQualitySlider": "20"})
    assert r.json() == {"ok": True, "restart_required": True, "shared": False}
    local = json.loads((tmp_path / "presets.local.json").read_text())
    assert local["PresetList"][0]["VideoQualitySlider"] == 20


def test_handbrake_save_shares_the_preset_cluster_wide(client, manager, tmp_path):
    """Every worker must encode with the same preset — saving publishes it to the
    shared store so no box drifts onto an older one."""
    from blurayripper.handbrake_store import SHARED_KEY

    manager.settings.encode.preset_file = _preset_file(tmp_path)
    manager.store = DurableMem()

    assert client.post("/api/handbrake", json={"VideoPreset": "slow"}).json()["shared"] is True
    shared = manager.store.get_shared_config(SHARED_KEY)
    assert shared["PresetList"][0]["VideoPreset"] == "slow"


def test_handbrake_save_keeps_the_local_write_when_sharing_fails(client, manager, tmp_path):
    """A Postgres hiccup must not lose the edit — the local overlay still stands
    and the response says the cluster didn't get it."""
    manager.settings.encode.preset_file = _preset_file(tmp_path)
    store = DurableMem()

    def down(key, value):
        raise RuntimeError("connection refused")
    store.set_shared_config = down
    manager.store = store

    r = client.post("/api/handbrake", json={"VideoPreset": "slow"})
    assert r.json() == {"ok": True, "restart_required": True, "shared": False}
    local = json.loads((tmp_path / "presets.local.json").read_text())
    assert local["PresetList"][0]["VideoPreset"] == "slow"


def test_handbrake_save_rejects_a_value_the_encoder_would_choke_on(client, manager, tmp_path):
    """An invalid preset name only fails at encode time, hours later — reject it
    at save time with the list of valid choices."""
    manager.settings.encode.preset_file = _preset_file(tmp_path)
    r = client.post("/api/handbrake", json={"VideoPreset": "turbo"})
    assert r.status_code == 400 and "turbo" in r.json()["detail"]
    assert not (tmp_path / "presets.local.json").exists()


# --------------------------------------------------------------------------
# settings (global vs per-worker scope)
# --------------------------------------------------------------------------

def test_settings_get_defaults_to_the_global_scope(client, tmp_path):
    """Opening Settings with no scope must show the server config, including where
    the overlay file lives."""
    body = client.get("/api/settings").json()
    assert body["scope"] == "global"
    assert body["overlay_file"].endswith("settings.local.json")
    assert any(g["title"] == "Web" for g in body["groups"])


def test_settings_get_with_a_worker_scope_shows_that_workers_overrides(client, manager):
    """Selecting a worker in the settings dropdown has to read THAT worker's
    overlay — the scope query parameter is the only thing carrying it."""
    manager.store = DurableMem()
    manager.store.set_worker_settings("SteamMachine", {"encode": {"ffmpeg_preset": "veryslow"}})

    body = client.get("/api/settings", params={"scope": "SteamMachine"}).json()
    assert body["scope"] == "worker"
    assert not any(g["title"] == "Web" for g in body["groups"])   # server-only fields hidden
    enc = {f["key"]: f for g in body["groups"] if g["title"] == "Encode" for f in g["fields"]}
    assert enc["ffmpeg_preset"]["value"] == "veryslow"
    assert enc["ffmpeg_preset"]["overridden"] is True


def test_saving_global_settings_writes_the_overlay_and_needs_a_restart(client, tmp_path):
    """The server reads its config once at startup, so the response has to tell the
    UI the change is pending a restart."""
    r = client.post("/api/settings", json={"encode": {"ffmpeg_preset": "slow"}})
    assert r.json() == {"ok": True, "scope": "global", "restart_required": True,
                        "applies": "restart"}
    overlay = json.loads((tmp_path / "settings.local.json").read_text())
    assert overlay["encode"]["ffmpeg_preset"] == "slow"


def test_saving_a_worker_scope_applies_on_its_next_job(client, manager):
    """A worker picks up its override when it claims work — telling the user to
    restart the server would be wrong (and pointless).

    The box has to be one the cluster has actually seen: a scope naming a worker
    that never checked in is refused now (tests/test_fix_web_cli.py), so this
    registers it first rather than relying on any string being accepted."""
    manager.store = DurableMem()
    manager.store.list_workers = lambda: [
        WorkerInfo("SteamMachine", "v1", None, "idle", 1.0)]
    r = client.post("/api/settings", params={"scope": "SteamMachine"},
                    json={"encode": {"ffmpeg_preset": "veryslow"}})
    assert r.json() == {"ok": True, "scope": "SteamMachine", "restart_required": False,
                        "applies": "next job"}
    assert manager.store.get_worker_settings("SteamMachine") == {
        "encode": {"ffmpeg_preset": "veryslow"}}


def test_saving_a_worker_scope_without_postgres_is_400(client):
    """Per-worker overrides live in the shared store; standalone there is nowhere
    to put them and the UI must say so instead of silently dropping the edit."""
    r = client.post("/api/settings", params={"scope": "SteamMachine"},
                    json={"encode": {"ffmpeg_preset": "veryslow"}})
    assert r.status_code == 400 and "Postgres" in r.json()["detail"]


def test_saving_an_invalid_setting_value_is_400(client, tmp_path):
    """A bad value must be refused at save time — writing it would make the next
    start fail to load its config at all."""
    r = client.post("/api/settings", json={"queue": {"encode_capacity": "lots"}})
    assert r.status_code == 400
    assert not (tmp_path / "settings.local.json").exists()


# --------------------------------------------------------------------------
# box-set plan editor
# --------------------------------------------------------------------------

def test_boxset_get_exposes_the_columns_and_example_for_the_editor(client, tmp_path):
    """The plan editor renders its header row and placeholder from this payload,
    and shows the path so the file can be edited by hand too."""
    body = client.get("/api/boxsets").json()
    assert "disc_label" in body["columns"] and body["example"].strip()
    assert body["path"].endswith("boxsets.local.csv")
    assert body["rows"] == [] and body["errors"] == []


def test_boxset_save_failure_is_a_400_that_names_the_problem(client, monkeypatch):
    """A read-only config dir must not 500 — the editor shows the OS error so the
    paste isn't silently lost."""
    from blurayripper import boxset_plan

    def cannot_write(settings, text, store=None):
        raise OSError("read-only file system")
    monkeypatch.setattr(boxset_plan, "save_plan", cannot_write)

    r = client.post("/api/boxsets", json={"csv": "disc_label,show\nA_S1_D1,Show\n"})
    assert r.status_code == 400
    assert "Couldn't save the plan" in r.json()["detail"]
    assert "read-only file system" in r.json()["detail"]


# --------------------------------------------------------------------------
# TMDB proxy
# --------------------------------------------------------------------------

def test_tmdb_search_without_an_api_key_is_400_before_any_lookup(client, monkeypatch):
    """Search is dead without a key; a clear 400 beats an empty result list that
    looks like 'TMDB has never heard of this film' — and it is decided up front,
    without a request going out."""
    import blurayripper.services.metadata as md
    calls = []

    async def fake_movie(self, query, year=None):
        calls.append(query)
        return []
    monkeypatch.setattr(md.TMDBClient, "search_movie", fake_movie)

    r = client.get("/api/tmdb/search", params={"kind": "movie", "query": "Alien"})
    assert r.status_code == 400 and "TMDB API key" in r.json()["detail"]
    assert calls == []


def test_tmdb_tv_search_is_serialised_with_id_title_year(client, app, monkeypatch):
    """The picker binds to id/title/year for both kinds, even though the TV
    candidate calls its title `name`."""
    import blurayripper.services.metadata as md

    class _Cand:
        def __init__(self, tmdb_id, name, year):
            self.tmdb_id, self.name, self.year = tmdb_id, name, year

    async def fake_tv(self, query):
        return [_Cand(771, "WarGames", 1983)]
    monkeypatch.setattr(md.TMDBClient, "search_tv", fake_tv)
    app.state.manager.settings.tmdb_api_key = "x"
    monkeypatch.setattr(md.TMDBClient, "configured", property(lambda self: True))

    body = client.get("/api/tmdb/search", params={"kind": "tv", "query": "WarGames"}).json()
    assert body == [{"id": 771, "title": "WarGames", "year": 1983}]


def test_tmdb_movie_search_is_serialised_from_the_candidate_title(client, monkeypatch):
    """Movie candidates carry `title`, TV ones carry `name` — the two branches must
    not be crossed, or the movie picker renders a list of blanks."""
    import blurayripper.services.metadata as md

    class _Cand:
        def __init__(self, tmdb_id, title, year):
            self.tmdb_id, self.title, self.year = tmdb_id, title, year

    async def fake_movie(self, query, year=None):
        return [_Cand(78, "Blade Runner", 1982)]
    monkeypatch.setattr(md.TMDBClient, "search_movie", fake_movie)
    monkeypatch.setattr(md.TMDBClient, "configured", property(lambda self: True))

    body = client.get("/api/tmdb/search",
                      params={"kind": "movie", "query": "Blade Runner"}).json()
    assert body == [{"id": 78, "title": "Blade Runner", "year": 1982}]


def test_tmdb_network_failure_is_a_502_naming_the_error(client, monkeypatch):
    """A DNS/HTTP failure reaching TMDB is not the client's fault — 502 with the
    exception type is what makes 'no results' distinguishable from 'offline'."""
    import blurayripper.services.metadata as md

    async def boom(self, query, year=None):
        raise ConnectionError("name resolution failed")
    monkeypatch.setattr(md.TMDBClient, "search_movie", boom)
    monkeypatch.setattr(md.TMDBClient, "configured", property(lambda self: True))

    r = client.get("/api/tmdb/search", params={"kind": "movie", "query": "Alien"})
    assert r.status_code == 502
    assert "ConnectionError" in r.json()["detail"]


def test_tmdb_metadata_error_is_a_400(client, monkeypatch):
    """A rejected key or a bad query is a request problem, and TMDB's own message
    is the useful thing to show."""
    import blurayripper.services.metadata as md
    from blurayripper.services.metadata import MetadataError

    async def bad(self, query, year=None):
        raise MetadataError("Invalid API key")
    monkeypatch.setattr(md.TMDBClient, "search_movie", bad)
    monkeypatch.setattr(md.TMDBClient, "configured", property(lambda self: True))

    r = client.get("/api/tmdb/search", params={"kind": "movie", "query": "Alien"})
    assert r.status_code == 400 and r.json()["detail"] == "Invalid API key"


def test_tmdb_season_serialises_episode_number_name_and_runtime(client, monkeypatch):
    """The episode picker matches titles by runtime, so runtime has to survive the
    proxy alongside the number and name."""
    import blurayripper.services.metadata as md

    class _Ep:
        def __init__(self, n, name, runtime):
            self.episode_number, self.name, self.runtime = n, name, runtime

    async def fake_season(self, tv_id, season):
        assert (tv_id, season) == (771, 2)
        return [_Ep(1, "Pilot", 44), _Ep(2, "Second", 43)]
    monkeypatch.setattr(md.TMDBClient, "get_season", fake_season)

    body = client.get("/api/tmdb/season", params={"tv_id": 771, "season": 2}).json()
    assert body == [{"episode": 1, "name": "Pilot", "runtime": 44},
                    {"episode": 2, "name": "Second", "runtime": 43}]


def test_tmdb_season_requires_numeric_query_parameters(client):
    """tv_id/season are typed — a non-numeric one is rejected before a request
    goes out to TMDB."""
    r = client.get("/api/tmdb/season", params={"tv_id": "abc", "season": 1})
    assert r.status_code == 422


def test_tmdb_season_failure_maps_to_400_and_502(client, monkeypatch):
    """The season lookup has the same two failure modes as search and must grade
    them the same way."""
    import blurayripper.services.metadata as md
    from blurayripper.services.metadata import MetadataError

    async def bad(self, tv_id, season):
        raise MetadataError("season not found")
    monkeypatch.setattr(md.TMDBClient, "get_season", bad)
    assert client.get("/api/tmdb/season", params={"tv_id": 1, "season": 9}).status_code == 400

    async def boom(self, tv_id, season):
        raise TimeoutError("read timeout")
    monkeypatch.setattr(md.TMDBClient, "get_season", boom)
    r = client.get("/api/tmdb/season", params={"tv_id": 1, "season": 9})
    assert r.status_code == 502 and "TimeoutError" in r.json()["detail"]


# --------------------------------------------------------------------------
# SSE stream
# --------------------------------------------------------------------------

def test_events_opens_with_a_full_snapshot_frame(client, manager):
    """A newly-opened tab renders from the first frame alone — without the
    immediate snapshot the queue stays blank until something changes."""
    _job(manager, 41, JobState.DONE)
    manager.closing = True          # end the stream after the opening frame

    r = client.get("/api/events")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.text.startswith("data: ") and r.text.endswith("\n\n")
    payload = json.loads(r.text[len("data: "):])
    assert [j["id"] for j in payload["jobs"]] == [41]


def test_events_drops_a_frame_it_cannot_serialise_instead_of_truncating(client, manager):
    """A half-written frame makes every client throw a JSON parse error and lose
    the stream; skipping the frame keeps the connection usable."""
    def broken():
        raise TypeError("not JSON serialisable")
    manager.snapshot = broken
    manager.closing = True

    r = client.get("/api/events")
    assert r.status_code == 200 and r.text == ""


def test_safe_sse_returns_none_rather_than_raising(manager):
    """The guard that the stream relies on, at its source."""
    from blurayripper.web.app import _safe_sse

    assert json.loads(_safe_sse(manager)[len("data: "):])["jobs"] == []

    def broken():
        raise ValueError("boom")
    manager.snapshot = broken
    assert _safe_sse(manager) is None


class _FakeRequest:
    """Enough of a Request for the SSE generator: it only ever asks whether the
    client has gone away."""

    def __init__(self, disconnect_after: int | None = None) -> None:
        self.checks = 0
        self._after = disconnect_after

    async def is_disconnected(self) -> bool:
        self.checks += 1
        return self._after is not None and self.checks > self._after


async def _open_stream(app, request):
    """The raw SSE generator, so the loop can be driven a frame at a time (the
    TestClient can only read a stream that ends by itself)."""
    endpoint = next(r for r in app.routes if getattr(r, "path", "") == "/api/events").endpoint
    response = await endpoint(request)
    return response.body_iterator


async def test_sse_collapses_a_burst_of_ticks_into_one_frame(app, manager):
    """Every job event ticks the bus; sending a frame per tick would flood the
    browser during a rip. One frame per wake-up, carrying the latest snapshot."""
    _job(manager, 42, JobState.DONE)
    gen = await _open_stream(app, _FakeRequest())

    assert json.loads((await gen.__anext__())[6:])["jobs"][0]["id"] == 42
    manager.bus.publish()
    manager.bus.publish()
    manager.bus.publish()
    frame = json.loads((await gen.__anext__())[6:])
    assert [j["id"] for j in frame["jobs"]] == [42]
    assert all(q.empty() for q in manager.bus._subs)   # the extra ticks were drained

    await gen.aclose()
    assert manager.bus._subs == set()                  # unsubscribed on close


async def test_sse_stops_when_the_client_has_gone_away(app, manager):
    """Writing to a dead socket raises inside the generator and leaks its queue
    subscription; the loop checks for the disconnect before every frame."""
    gen = await _open_stream(app, _FakeRequest(disconnect_after=0))

    await gen.__anext__()                       # opening snapshot
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()
    assert manager.bus._subs == set()


async def test_sse_stops_if_the_client_leaves_while_waiting(app, manager):
    """The wait for the next tick can outlast the client — re-checking after it
    returns is what stops a frame being written to a closed connection."""
    gen = await _open_stream(app, _FakeRequest(disconnect_after=1))

    await gen.__anext__()
    manager.bus.publish()                       # wake the loop
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()
    assert manager.bus._subs == set()


async def test_sse_heartbeats_when_nothing_has_happened(app, manager, monkeypatch):
    """An idle connection with no traffic gets culled by proxies (and the tab then
    shows a frozen queue), so a quiet period still sends a frame."""
    import asyncio

    async def instant_timeout(awaitable, timeout=None):
        awaitable.close()
        raise asyncio.TimeoutError
    monkeypatch.setattr(asyncio, "wait_for", instant_timeout)

    _job(manager, 43, JobState.DONE)
    gen = await _open_stream(app, _FakeRequest())
    await gen.__anext__()
    frame = json.loads((await gen.__anext__())[6:])   # no publish happened at all
    assert [j["id"] for j in frame["jobs"]] == [43]
    assert json.loads((await gen.__anext__())[6:])["jobs"]   # and it keeps beating
    await gen.aclose()


async def test_sse_skips_one_unserialisable_frame_and_keeps_streaming(app, manager, monkeypatch):
    """One job that won't serialise must cost one frame, not the connection — the
    stream recovers on the next tick instead of the tab going dead."""
    import asyncio

    async def instant_timeout(awaitable, timeout=None):
        awaitable.close()
        raise asyncio.TimeoutError
    monkeypatch.setattr(asyncio, "wait_for", instant_timeout)

    _job(manager, 46, JobState.DONE)
    real, calls = manager.snapshot, {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 2:
            raise TypeError("not JSON serialisable")
        return real()
    manager.snapshot = flaky

    gen = await _open_stream(app, _FakeRequest())
    assert json.loads((await gen.__anext__())[6:])["jobs"][0]["id"] == 46   # frame 1
    frame = json.loads((await gen.__anext__())[6:])   # frame 2 was dropped; 3 arrives
    assert [j["id"] for j in frame["jobs"]] == [46] and calls["n"] == 3
    await gen.aclose()


# --------------------------------------------------------------------------
# startup / shutdown
# --------------------------------------------------------------------------

def test_startup_restores_the_queue_from_the_store(tmp_path):
    """A restart mid-queue must not lose the queue — the persisted jobs are back
    in /api/jobs before the first request is served."""
    store = MemoryStore()
    seeder = _app(tmp_path / "a")
    seeder.state.manager.store = store
    for jid, state in ((44, JobState.DONE), (45, JobState.ENCODING)):
        seeder.state.manager._persist(_job(seeder.state.manager, jid, state, source="disc:2"))

    app = _app(tmp_path / "b")
    app.state.manager.store = store
    with TestClient(app) as c:
        body = c.get("/api/jobs").json()
    # The finished job is left alone; the interrupted one is picked back up.
    assert [(j["id"], j["state"]) for j in body["jobs"]] == [(44, "done"), (45, "ready")]


def test_startup_survives_a_store_that_cannot_be_read(tmp_path):
    """A database hiccup at boot must not brick the box: the server still comes up
    (with an empty queue) and still starts watching the drives."""
    app = _app(tmp_path)
    watching = []

    def boom():
        raise RuntimeError("could not connect to server")
    app.state.manager.recover = boom
    app.state.manager.start_drive_watch = lambda: watching.append(1)

    with TestClient(app) as c:
        assert c.get("/api/jobs").json()["jobs"] == []
    assert watching == [1]


def test_shutdown_closes_the_manager_so_the_process_can_exit(tmp_path):
    """Ctrl+C has to reach the manager: it cancels running rips and wakes the SSE
    streams. Without it the port stays held and serve can't re-exec."""
    app = _app(tmp_path)
    with TestClient(app):
        assert app.state.manager.closing is False
    assert app.state.manager.closing is True
