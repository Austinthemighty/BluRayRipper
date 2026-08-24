"""Hostile HTTP against the web UI's API.

The dashboard is the only way in, it has no login (see the "Bind host" setting),
and it is driven by hand — so the traffic that really arrives includes a
double-clicked button, a tab left open overnight posting a job id that was
removed hours ago, and a hand-edited ``fetch()`` from the browser console. None
of that may land as a 500: a traceback tells the operator nothing, and a
half-applied mutation on a job that is mid-rip is worse than a refusal.

So these tests assert the CONTRACT OF A REFUSAL rather than "it raised":

* a 4xx the UI can render, carrying a message a human can act on;
* nothing changed by the request that was refused — no job invented, no state
  transition half-made, no cluster-wide signal queued;
* and for the settings overlay, which is a real file on disk that every box in
  the cluster syncs from, not a single byte written.

Plus the two things a public-facing surface has to get right: a download
endpoint that cannot be pointed outside the files it owns, and a mutating
endpoint that is hit twice at once.
"""

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from blurayripper.config import load_settings
from blurayripper.persistence import MemoryStore, WorkerInfo
from blurayripper.pipeline import JobState
from blurayripper.settings_store import read_overlay
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
    """Stop the manager launching real pipeline coroutines (rip/encode/OCR)."""

    def spawn(coro):
        coro.close()
        return _DeadTask()

    manager._spawn = spawn


def _build(tmp_path, store=None):
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "settings.toml").write_text("", encoding="utf-8")
    settings = load_settings(cfg)
    settings.config_dir = cfg
    settings.database_url = None            # never reach for a real Postgres
    settings.storage.output_dir = tmp_path / "out"
    settings.storage.staging_dir = tmp_path / "staging"
    # config.load_settings resolves this against the REPO, not the config dir —
    # keep every write these tests can provoke inside tmp_path.
    settings.encode.preset_file = tmp_path / "presets.json"
    settings.notify_url = None
    app = create_app(settings)
    if store is not None:
        app.state.manager.store = store
    _no_spawn(app.state.manager)
    return app


@pytest.fixture
def app(tmp_path):
    return _build(tmp_path)


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def manager(app):
    return app.state.manager


@pytest.fixture
def cluster(tmp_path):
    """(app, client, manager, store) with a store that claims to be Postgres."""
    store = DurableMem()
    app = _build(tmp_path, store)
    return app, TestClient(app), app.state.manager, store


def _async_client(app):
    """An ASGI client that runs on the test's own event loop, so two gathered
    requests interleave at every await point inside the handlers. TestClient
    serialises through a portal, which would hide any ordering bug entirely."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://brr")


def _job(manager, job_id=1, state=JobState.AWAITING_TITLES, source="disc:0", **kw):
    wj = WebJob(id=job_id, source=source, state=state, **kw)
    manager.jobs[job_id] = wj
    return wj


def _scope_suspects(cfg_dir, scope):
    """Every path a scope-joined write could plausibly land on, including ones
    that climb out of the config directory entirely."""
    out = set()
    for base in (cfg_dir, cfg_dir.parent, Path.cwd()):
        for tail in (scope, f"{scope}.json"):
            for form in (tail, tail.replace("\\", "/")):
                out.add(Path(os.path.normpath(os.path.join(str(base), form))))
    return out


def _detail(response):
    """The human-readable message the UI shows, or None if the body isn't one."""
    try:
        return response.json().get("detail")
    except (ValueError, AttributeError):
        return None


# Every endpoint that takes a job id in the path, with a body valid enough to
# reach the handler. The *id* is the hostile part in these tests.
JOB_ENDPOINTS = [
    ("GET", "/api/jobs/{i}/log", None),
    ("POST", "/api/jobs/{i}/titles", {"title_ids": [1]}),
    ("POST", "/api/jobs/{i}/mapping",
     {"assignments": [{"title_id": 1, "kind": "movie", "show_or_movie_title": "X"}]}),
    ("POST", "/api/jobs/{i}/grain", {"tune_grain": True}),
    ("POST", "/api/jobs/{i}/auto-identify",
     {"title_ids": [1], "show_title": "S", "show_tmdb_id": 1, "season": 1, "episodes": [1]}),
    ("POST", "/api/jobs/{i}/confirm-match", {"mapping": {"1": 2}}),
    ("POST", "/api/jobs/{i}/retry", {}),
    ("POST", "/api/jobs/{i}/bump", {}),
    ("POST", "/api/jobs/{i}/requeue", {}),
    ("POST", "/api/jobs/{i}/edit", {"edition": "Director's Cut"}),
    ("POST", "/api/jobs/{i}/rename", {"title": "X"}),
    ("POST", "/api/jobs/{i}/remap", {}),
    ("POST", "/api/jobs/{i}/group-season", {}),
    ("POST", "/api/jobs/{i}/change-source", {"source": "disc:1"}),
    ("POST", "/api/jobs/{i}/redo-subs", {}),
    ("POST", "/api/jobs/{i}/preview-grain", {}),
    ("GET", "/api/jobs/{i}/aacs-dump", None),
    ("GET", "/api/jobs/{i}/preview/on", None),
    ("GET", "/api/jobs/{i}/preview-clip/on", None),
    ("GET", "/api/jobs/{i}/artwork", None),
]


def _call(client, method, path, body):
    if method == "GET":
        return client.get(path)
    if method == "DELETE":
        return client.delete(path)
    return client.post(path, json=body)


# --------------------------------------------------------------------------
# job ids that don't exist, can't exist, or aren't numbers
# --------------------------------------------------------------------------

@pytest.mark.parametrize("job_id", [999, -5, 0, 2 ** 80])
def test_a_job_id_that_cannot_exist_is_refused_by_every_job_endpoint(
        client, manager, job_id):
    """A stale browser tab posts ids for jobs that were removed; a hand-typed URL
    posts negative or absurd ones. Every one of them must come back as a 4xx the
    UI can show, never a 500 — and must not conjure a job into the queue, which
    is what a plain ``self.jobs[job_id] = ...`` on the way in would do."""
    for method, template, body in JOB_ENDPOINTS:
        path = template.format(i=job_id)
        r = _call(client, method, path, body)
        assert r.status_code in (400, 404), (path, r.status_code, r.text[:200])
        assert "Traceback" not in r.text, path
        detail = _detail(r)
        assert isinstance(detail, str) and detail.strip(), (path, r.text[:200])
        # The message has to say WHICH thing was missing, not just "error".
        assert ("not found" in detail.lower() or "no such job" in detail.lower()
                or f"job {job_id}" in detail.lower()
                or "no preview" in detail.lower() or "no artwork" in detail.lower()
                or "aacs dump" in detail.lower()), (path, detail)
    assert manager.jobs == {}, "a lookup for a missing job created one"


def test_a_non_numeric_job_id_is_a_422_that_names_the_path_parameter(client, manager):
    """``/api/jobs/undefined/log`` is what a UI bug actually sends. FastAPI should
    reject it at the path parameter — the handler must never see a string where it
    expects an int, and the error must point at ``job_id`` so the cause is obvious
    in the browser's network tab."""
    for method, template, body in JOB_ENDPOINTS:
        path = template.format(i="undefined")
        r = _call(client, method, path, body)
        assert r.status_code == 422, (path, r.status_code, r.text[:200])
        locs = [tuple(e["loc"]) for e in r.json()["detail"]]
        assert ("path", "job_id") in locs, (path, locs)
    assert manager.jobs == {}


def test_deleting_a_job_that_is_already_gone_is_a_no_op_not_an_error(client, manager):
    """Two tabs both press Remove on the same finished job. The second DELETE has
    nothing to do — it must succeed quietly rather than 404 the operator, and must
    not resurrect the id in the queue."""
    r = client.delete("/api/jobs/4242")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert manager.jobs == {}
    r = client.delete("/api/jobs/-1")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert manager.jobs == {}


# --------------------------------------------------------------------------
# bodies of the wrong type, shape, or size
# --------------------------------------------------------------------------

@pytest.mark.parametrize("body", [[1, 2, 3], "just a string", 7, True])
def test_a_body_of_the_wrong_type_is_refused_without_touching_the_job(
        client, manager, body):
    """A JSON array where an object belongs (or a bare scalar) must be a 422 —
    and the job it was aimed at must be exactly as it was: still parked at the
    title gate, with no selection recorded."""
    wj = _job(manager, 1, JobState.AWAITING_TITLES)
    r = client.post("/api/jobs/1/titles", json=body)
    assert r.status_code == 422, r.text[:200]
    # Pin it to the BODY: a 422 raised for some other reason (a path parameter,
    # a query parameter) would otherwise satisfy the status check by accident.
    locs = [tuple(e["loc"]) for e in r.json()["detail"]]
    assert any(loc and loc[0] == "body" for loc in locs), locs
    assert wj.state is JobState.AWAITING_TITLES
    assert wj.selected_title_ids == []


def test_malformed_json_is_a_422_not_a_parser_traceback(client, manager):
    """A request cut off mid-flight (or a hand-written ``fetch()`` with a typo)
    arrives as bytes that aren't JSON. That has to be the same tidy 422 as any
    other bad body — a json.JSONDecodeError escaping the handler is a 500 whose
    body is a stack trace naming server paths."""
    wj = _job(manager, 1, JobState.AWAITING_TITLES)
    r = client.post("/api/jobs/1/titles", content="{not json",
                    headers={"content-type": "application/json"})
    assert r.status_code == 422
    assert "Traceback" not in r.text
    assert wj.state is JobState.AWAITING_TITLES
    assert wj.selected_title_ids == []


def test_a_missing_required_field_names_the_field_and_queues_nothing(
        client, manager):
    """``POST /api/jobs`` with no ``source`` is the classic half-filled form. The
    422 must name ``source``, and — because add_source persists as it goes — no
    job may reach either the queue or the store."""
    r = client.post("/api/jobs", json={})
    assert r.status_code == 422
    locs = [tuple(e["loc"]) for e in r.json()["detail"]]
    assert ("body", "source") in locs, locs
    assert manager.jobs == {}
    assert manager.store.load_all() == []


def test_a_field_of_the_wrong_type_inside_a_valid_body_is_refused(client, manager):
    """``title_ids: ["3"]`` — a select box that forgot to parseInt. Rejected at
    the item, not silently coerced into a title id nobody selected."""
    wj = _job(manager, 1, JobState.AWAITING_TITLES)
    r = client.post("/api/jobs/1/titles", json={"title_ids": ["not-a-number"]})
    assert r.status_code == 422
    locs = [tuple(e["loc"]) for e in r.json()["detail"]]
    assert ("body", "title_ids", 0) in locs, locs
    assert wj.selected_title_ids == []


def test_extra_unexpected_fields_are_ignored_rather_than_rejected(client, manager):
    """A newer UI sends fields this server doesn't know yet (and every fetch()
    written by hand carries junk). Unknown keys must be ignored so the request
    still does its job — a strict 422 here would break the dashboard on any
    version skew."""
    wj = _job(manager, 1, JobState.AWAITING_TITLES)
    r = client.post("/api/jobs/1/titles",
                    json={"title_ids": [3, 4], "ui_version": "next", "nonsense": {"a": 1}})
    assert r.status_code == 200, r.text[:200]
    assert wj.selected_title_ids == [3, 4]
    assert wj.state is JobState.AWAITING_MAPPING


def test_a_rejected_mapping_leaves_no_half_written_assignments(client, manager):
    """The naming form posts every title at once. If the third one is bad, the
    first two must NOT be recorded — a job that starts encoding with a partial
    mapping places some files under the right name and loses the rest."""
    wj = _job(manager, 1, JobState.AWAITING_MAPPING)
    r = client.post("/api/jobs/1/mapping", json={"assignments": [
        {"title_id": 1, "kind": "movie", "show_or_movie_title": "Good One"},
        {"title_id": 2, "kind": "movie", "show_or_movie_title": "Also Good"},
        {"title_id": 3, "kind": "opera", "show_or_movie_title": "Bad Kind"},
    ]})
    assert r.status_code == 400, r.text[:200]
    assert "opera" in _detail(r), _detail(r)
    assert wj.assignments == {}, "a rejected mapping was partly applied"
    assert wj.selected_title_ids == []
    assert wj.state is JobState.AWAITING_MAPPING, "job was started anyway"


@pytest.mark.parametrize("source", ["", "   ", "/definitely/not/here/disc.mkv"])
def test_a_source_that_does_not_resolve_is_refused_and_queues_nothing(
        client, manager, source, monkeypatch, tmp_path):
    """add_source refuses a path it can't resolve rather than handing it to
    makemkvcon. The refusal must leave nothing behind: a job row written before
    the failure would sit in the queue forever with a source nobody can rip.

    The blank sources are chdir'd onto an empty directory on purpose: ``Path("")``
    is ``Path(".")``, so a blank source is resolved against the SERVER'S working
    directory. Without the chdir this test would pass or fail on whether the
    developer happened to be sitting in a folder with a .mkv in it.
    """
    monkeypatch.chdir(tmp_path)
    r = client.post("/api/jobs", json={"source": source})
    assert r.status_code == 400, r.text[:200]
    assert isinstance(_detail(r), str) and _detail(r).strip()
    assert manager.jobs == {}
    assert manager.store.load_all() == []


def test_a_source_of_the_wrong_type_never_reaches_the_filesystem(client, manager):
    """A ``source`` that arrives as an object goes straight into ``Path(...)`` and
    a ``.strip()`` if validation lets it past — an AttributeError 500 instead of a
    422 the form can render next to the field."""
    r = client.post("/api/jobs", json={"source": {"path": "/x"}})
    assert r.status_code == 422
    locs = [tuple(e["loc"]) for e in r.json()["detail"]]
    assert ("body", "source") in locs, locs
    assert manager.jobs == {}
    assert manager.store.load_all() == []


def test_a_very_large_body_is_parsed_in_full_and_a_dry_run_writes_nothing(
        client, manager, tmp_path):
    """The box-set plan is pasted in whole — thousands of lines of CSV arrive in
    one POST. The body must be read in full (not truncated at some buffer), and
    ``dry_run`` must mean exactly that: the preview may not leave the plan file
    on disk, or "let me see what this would do" quietly rewrites the plan."""
    plan = tmp_path / "config" / "boxsets.local.csv"
    rows = 20_000
    csv = "disc_label,show,year\n" + "".join(
        f"BOXSET_D{i:05d},Show Number {i},19{i % 100:02d}\n" for i in range(rows))
    assert len(csv) > 500_000, "test body isn't actually large"

    r = client.post("/api/boxsets", json={"csv": csv, "dry_run": True})
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert body["saved"] is False
    assert len(body["rows"]) == rows, "the large body was not parsed in full"
    assert body["rows"][-1]["disc_label"] == f"BOXSET_D{rows - 1:05d}"
    assert not plan.exists(), "a dry run wrote the plan file"

    # Control: the same endpoint really can write, so the assertion above is
    # about dry_run and not about the endpoint being inert in this fixture.
    r = client.post("/api/boxsets", json={"csv": "disc_label,show\nD1,Show\n"})
    assert r.status_code == 200 and r.json()["saved"] is True
    assert plan.read_text(encoding="utf-8").startswith("disc_label,show")


def test_a_boxset_plan_that_is_not_text_is_refused_and_the_plan_survives(
        client, tmp_path):
    """``csv`` arriving as a number/object is a UI bug. It must be refused with a
    message, and the plan already on disk must be untouched — the failure mode
    that matters is a bad save wiping a plan someone typed by hand."""
    plan = tmp_path / "config" / "boxsets.local.csv"
    client.post("/api/boxsets", json={"csv": "disc_label,show\nKEEP_ME,Kept Show\n"})
    before = plan.read_bytes()

    for bad in (5, {"a": 1}, ["disc_label,show"]):
        r = client.post("/api/boxsets", json={"csv": bad})
        assert r.status_code == 400, (bad, r.status_code)
        assert _detail(r) == "csv must be text."
    assert plan.read_bytes() == before, "a refused save changed the plan on disk"


# --------------------------------------------------------------------------
# query parameters
# --------------------------------------------------------------------------

def test_query_values_of_the_wrong_type_are_refused_before_the_handler_runs(
        client):
    """``tv_id``/``season`` come from the URL, so anything can be typed there.
    They must be rejected at the boundary — reaching the handler means a TMDB
    request built from junk."""
    r = client.get("/api/tmdb/season", params={"tv_id": "abc", "season": "latest"})
    assert r.status_code == 422
    locs = [tuple(e["loc"]) for e in r.json()["detail"]]
    assert ("query", "tv_id") in locs and ("query", "season") in locs, locs


def test_a_missing_query_parameter_is_a_422_naming_it(client):
    """A search box that fires before it has any text drops ``query`` entirely. It
    must be a 422 naming the parameter, not a default-to-empty that sends a
    blank search to TMDB and comes back with "no matches" for no visible reason."""
    r = client.get("/api/tmdb/search", params={"kind": "movie"})
    assert r.status_code == 422
    locs = [tuple(e["loc"]) for e in r.json()["detail"]]
    assert ("query", "query") in locs, locs


def test_a_body_field_of_the_wrong_type_stops_short_of_the_optical_drive(client):
    """The eject endpoint drives real hardware. A ``device`` that isn't a string
    has to be refused by validation, never handed to the ctypes/MCI call."""
    r = client.post("/api/drives/eject", json={"device": ["F:", "G:"]})
    assert r.status_code == 422
    locs = [tuple(e["loc"]) for e in r.json()["detail"]]
    assert ("body", "device") in locs, locs


# --------------------------------------------------------------------------
# the settings scope — the one request that writes a file
# --------------------------------------------------------------------------

def test_an_empty_scope_reads_the_global_settings_page(client, tmp_path):
    """``?scope=`` is what a UI sends when its worker dropdown hasn't loaded. It
    must fall back to the global page rather than being treated as a worker named
    "" (which would show a stripped-down form and save to a phantom worker)."""
    r = client.get("/api/settings", params={"scope": ""})
    assert r.status_code == 200
    body = r.json()
    assert body["scope"] == "global"
    # Global-only keys — a worker payload has neither.
    assert body["overlay_file"] == str(tmp_path / "config" / "settings.local.json")
    assert "workers" in body
    # Server-only fields are present, which the worker view drops.
    keys = {(f["section"], f["key"]) for g in body["groups"] for f in g["fields"]}
    assert ("web", "port") in keys


def test_a_worker_scope_write_without_postgres_is_refused_with_the_fix(
        client, tmp_path):
    """Per-worker overrides need the shared store. Without it the write must be
    refused — and the message must say what to do, because the alternative is a
    save that appears to work and silently reaches no worker."""
    overlay = tmp_path / "config" / "settings.local.json"
    client.post("/api/settings", json={"encode": {"tune_grain": True}})
    before = overlay.read_bytes()

    r = client.post("/api/settings", params={"scope": "encoder-box"},
                    json={"encode": {"tune_grain": False}})
    assert r.status_code == 400, r.text[:200]
    assert "BLURAYRIPPER_DATABASE_URL" in _detail(r), _detail(r)
    assert overlay.read_bytes() == before, "a worker write fell back to the global file"


def test_a_rejected_settings_write_does_not_touch_the_overlay_file_at_all(cluster):
    """The overlay is a real file that every box in the cluster pulls from. A
    write carrying one bad value must be rejected WHOLE: not the valid half
    applied and the rest dropped, and not a rewritten file. Otherwise a typo in
    one field silently changes another — on every machine."""
    _app, client, _manager, store = cluster
    cfg = _manager.settings.config_dir
    overlay = cfg / "settings.local.json"

    client.post("/api/settings", json={"encode": {"tune_grain": True, "container": "mkv"}})
    before_bytes = overlay.read_bytes()
    before_shared = json.dumps(store.get_shared_config("settings.global"), sort_keys=True)

    r = client.post("/api/settings", json={
        "encode": {"low_priority": False, "container": "avi"},   # valid, then invalid
        "queue": {"encode_capacity": 9},
    })
    assert r.status_code == 400, r.text[:200]
    assert "must be one of" in _detail(r) and "container" in _detail(r)

    assert overlay.read_bytes() == before_bytes, "the overlay file was rewritten"
    saved = read_overlay(cfg)
    assert "low_priority" not in saved["encode"], "the valid half was applied anyway"
    assert "queue" not in saved, "a later section was applied anyway"
    assert saved["encode"]["container"] == "mkv", "the good value was clobbered"
    assert json.dumps(store.get_shared_config("settings.global"),
                      sort_keys=True) == before_shared, "the cluster copy changed"


def test_a_non_numeric_value_for_a_numeric_setting_is_refused_by_name(cluster):
    """Typing prose into a number box must be refused by name rather than coerced
    to some fallback: an encode capacity that silently became 0 (or 1) would
    throttle the whole cluster, and nothing in the UI would say why."""
    _app, client, manager, _store = cluster
    overlay = manager.settings.config_dir / "settings.local.json"
    client.post("/api/settings", json={"queue": {"encode_capacity": 3}})
    before = overlay.read_bytes()

    r = client.post("/api/settings", json={"queue": {"encode_capacity": "as many as it takes"}})
    assert r.status_code == 400
    assert _detail(r) == "queue.encode_capacity must be a whole number"
    assert overlay.read_bytes() == before


@pytest.mark.parametrize("scope", [
    "../../etc/passwd", "..\\..\\windows\\system32", "sub/dir/worker", "/etc/passwd",
    "{tmp}/stolen-config",   # absolute, and somewhere this process CAN write
])
def test_a_scope_containing_a_path_separator_never_becomes_a_file(cluster, scope):
    """The scope names a row in Postgres, not a file — but it arrives raw in the
    query string, so the thing to prove is that no code path joins it to a
    directory. Nothing may appear on disk beyond the one global overlay, and the
    global overlay must be untouched whatever the scope says."""
    _app, client, manager, _store = cluster
    cfg = manager.settings.config_dir
    scope = scope.format(tmp=cfg.parent)
    client.post("/api/settings", json={"encode": {"tune_grain": True}})
    before = (cfg / "settings.local.json").read_bytes()
    files_before = sorted(p.relative_to(cfg.parent).as_posix()
                          for p in cfg.parent.rglob("*") if p.is_file())
    suspects = _scope_suspects(cfg, scope)
    existed = {p for p in suspects if p.exists()}

    read = client.get("/api/settings", params={"scope": scope})
    assert read.status_code == 200 and read.json()["scope"] == "worker"

    write = client.post("/api/settings", params={"scope": scope},
                        json={"encode": {"tune_grain": False}})
    assert write.status_code < 500, write.text[:200]

    files_after = sorted(p.relative_to(cfg.parent).as_posix()
                         for p in cfg.parent.rglob("*") if p.is_file())
    assert files_after == files_before, f"scope {scope!r} created a file"
    assert {p for p in suspects if p.exists()} == existed, (
        f"scope {scope!r} was joined onto a directory and written")
    assert (cfg / "settings.local.json").read_bytes() == before
    assert read_overlay(cfg)["encode"]["tune_grain"] is True


@pytest.mark.parametrize("body,expected", [
    ({}, "worker scope"),
    ({"scope": ""}, "worker scope"),
    ({"scope": "global"}, "worker scope"),
    ({"scope": "server"}, "worker scope"),
])
def test_clearing_settings_requires_a_real_worker_scope(client, tmp_path, body,
                                                        expected):
    """"Clear overrides" with no scope (or the global one) must be refused — the
    same call pointed at "global" would wipe the settings for every box."""
    overlay = tmp_path / "config" / "settings.local.json"
    client.post("/api/settings", json={"encode": {"tune_grain": True}})
    before = overlay.read_bytes()

    r = client.post("/api/settings/clear", json=body)
    assert r.status_code == 400, r.text[:200]
    assert expected in _detail(r).lower(), _detail(r)
    assert overlay.read_bytes() == before
    assert read_overlay(tmp_path / "config")["encode"]["tune_grain"] is True


def test_clearing_settings_with_no_body_at_all_is_a_422(client):
    """No body at all is a different bug from a body with no scope, and it must
    stop at validation. A handler that defaulted the body to None would fall
    through to ``(body or {}).get("scope")`` — one refactor away from clearing
    something."""
    r = client.post("/api/settings/clear")
    assert r.status_code == 422
    assert ("body",) in [tuple(e["loc"]) for e in r.json()["detail"]]


# --------------------------------------------------------------------------
# cluster-wide mutations that must not fire on a bad request
# --------------------------------------------------------------------------

def test_an_unknown_worker_action_queues_no_cluster_signal(cluster):
    """A worker action is broadcast to every encode box. An action the workers
    don't implement must be refused BEFORE the signal is written, or the row sits
    in Postgres unread and the next legitimate action is confusing."""
    _app, client, manager, store = cluster
    manager._workers_cache = [WorkerInfo("encoder-1", "1.0", None, None, 1.0)]

    r = client.post("/api/workers/encoder-1/action", json={"action": "reformat"})
    assert r.status_code == 400
    assert "reformat" in _detail(r)
    assert store.cluster_signals() == {}

    r = client.post("/api/workers/encoder-1/action", json={})   # action missing
    assert r.status_code == 400
    assert store.cluster_signals() == {}

    # Control: a real action on a connected worker does write a signal, so the
    # emptiness above is the refusal and not a store that never records.
    r = client.post("/api/workers/encoder-1/action", json={"action": "drain"})
    assert r.status_code == 200 and r.json() == {"ok": True, "workers": 1}
    assert store.cluster_signals() == {"drain:encoder-1": 1}


def test_an_action_for_a_worker_that_is_not_connected_is_refused(cluster):
    """A typo'd (or long-gone) worker name would leave a signal nobody ever
    reads, so the operator sees "restarting" and nothing restarts."""
    _app, client, manager, store = cluster
    manager._workers_cache = [WorkerInfo("encoder-1", "1.0", None, None, 1.0)]

    r = client.post("/api/workers/encoder-2/action", json={"action": "restart"})
    assert r.status_code == 400
    assert "encoder-2" in _detail(r) and "connected" in _detail(r)
    assert store.cluster_signals() == {}


def test_controlling_workers_without_postgres_is_refused_not_silently_dropped(
        client, manager):
    """Standalone (no shared store) there are no remote workers at all. The UI
    button must come back with a reason rather than a cheerful ok."""
    r = client.post("/api/workers/*/action", json={"action": "update"})
    assert r.status_code == 400
    assert "Postgres" in _detail(r)
    assert manager.store.cluster_signals() == {}

    # Control: this store DOES record signals when one is written, so the
    # emptiness above is the refusal and not a store that can never show one.
    manager.store.request_cluster_signal("update", "*")
    assert manager.store.cluster_signals() != {}


def test_removing_by_an_unknown_state_removes_nothing(client, manager):
    """"Clear failed" posts a list of state names. An unrecognised name must
    match nothing — the dangerous bug is a filter that falls open and clears the
    whole queue, including jobs that are mid-encode."""
    _job(manager, 1, JobState.DONE)
    _job(manager, 2, JobState.ENCODING)

    r = client.post("/api/jobs/remove-by-state", json={"states": ["compleeted", "boom"]})
    assert r.status_code == 200 and r.json() == {"removed": 0}
    assert set(manager.jobs) == {1, 2}

    r = client.post("/api/jobs/remove-by-state", json={"states": []})
    assert r.json() == {"removed": 0}
    assert set(manager.jobs) == {1, 2}

    # Control: a state that IS real removes exactly that job and no other.
    r = client.post("/api/jobs/remove-by-state", json={"states": ["done"]})
    assert r.json() == {"removed": 1}
    assert set(manager.jobs) == {2}


# --------------------------------------------------------------------------
# the download endpoints
# --------------------------------------------------------------------------

def test_the_preview_download_can_only_serve_the_two_files_on_the_job(
        client, manager, tmp_path):
    """``/api/jobs/{id}/preview/{which}`` is the only download whose path carries
    user input. ``which`` selects between two paths the SERVER recorded — it is
    never joined to a directory — so no spelling of ".." can reach a file outside
    them. Anything that isn't "on" resolves to the grain-off still."""
    secret = tmp_path / "secrets.env"
    secret.write_text("BLURAYRIPPER_DATABASE_URL=postgres://hunter2", encoding="utf-8")
    on_png = tmp_path / "on.png"
    on_png.write_text("GRAIN-ON-IMAGE", encoding="utf-8")
    off_png = tmp_path / "off.png"
    off_png.write_text("GRAIN-OFF-IMAGE", encoding="utf-8")

    wj = _job(manager, 1, JobState.AWAITING_GRAIN)
    wj.preview_on, wj.preview_off = str(on_png), str(off_png)

    assert client.get("/api/jobs/1/preview/on").text == "GRAIN-ON-IMAGE"
    assert client.get("/api/jobs/1/preview/off").text == "GRAIN-OFF-IMAGE"

    for which in ["..%2F..%2Fsecrets.env", "%2e%2e%2fsecrets.env", "....//secrets.env",
                  "..%252f..%252fsecrets.env", str(secret).replace("/", "%2F")]:
        r = client.get(f"/api/jobs/1/preview/{which}")
        assert r.status_code in (200, 404), (which, r.status_code)
        assert "hunter2" not in r.text, f"{which} escaped the job's own files"
        if r.status_code == 200:
            assert r.text == "GRAIN-OFF-IMAGE", which


def test_the_preview_clip_download_is_closed_the_same_way(client, manager, tmp_path):
    """The clip endpoint is a second, separately-written copy of the same lookup —
    and it streams with Range support, so a traversal here hands out arbitrary
    bytes of an arbitrary file. ``which`` must select between the job's two
    recorded clips and nothing else."""
    secret = tmp_path / "private.mp4"
    secret.write_text("NOT-FOR-THE-BROWSER", encoding="utf-8")
    clip = tmp_path / "off.mp4"
    clip.write_text("GRAIN-OFF-CLIP", encoding="utf-8")
    wj = _job(manager, 1, JobState.AWAITING_GRAIN)
    wj.preview_off_clip = str(clip)

    for which in ["..%2Fprivate.mp4", "%2e%2e%2fprivate.mp4", str(secret).replace("/", "%2F")]:
        r = client.get(f"/api/jobs/1/preview-clip/{which}")
        assert r.status_code in (200, 404)
        assert "NOT-FOR-THE-BROWSER" not in r.text, which
    assert client.get("/api/jobs/1/preview-clip/off").text == "GRAIN-OFF-CLIP"


def test_a_missing_preview_is_a_404_rather_than_an_empty_download(client, manager):
    """A job that never previewed has no file. Serving 200 with nothing would put
    a broken <img> on the card and no explanation."""
    _job(manager, 1, JobState.AWAITING_GRAIN)
    r = client.get("/api/jobs/1/preview/on")
    assert r.status_code == 404 and _detail(r) == "No preview."
    r = client.get("/api/jobs/1/preview-clip/on")
    assert r.status_code == 404 and _detail(r) == "No preview clip."


def test_an_aacs_dump_that_has_been_moved_is_a_404_that_says_so(
        client, manager, tmp_path):
    """The dump is offered for download so it can be emailed to MakeMKV. If the
    file has since been cleaned up, the operator needs to hear that — a 500 (or a
    zero-byte file) reads as "the download is broken"."""
    dump = tmp_path / "aacs.dump.gz"
    dump.write_bytes(b"\x1f\x8b\x08KEYDUMP")
    wj = _job(manager, 1, JobState.FAILED)
    wj.aacs_dump = str(dump)
    r = client.get("/api/jobs/1/aacs-dump")
    assert r.status_code == 200 and r.content == b"\x1f\x8b\x08KEYDUMP"

    dump.unlink()
    r = client.get("/api/jobs/1/aacs-dump")
    assert r.status_code == 404
    assert "moved" in _detail(r), _detail(r)


# --------------------------------------------------------------------------
# the same mutating request, twice, at once
# --------------------------------------------------------------------------

async def test_two_concurrent_title_submissions_apply_exactly_one(app, manager):
    """A double-clicked Rip button sends the form twice. The state gate must let
    exactly one through — the second has to be refused, not merged into the
    first, or the job rips a title set nobody chose."""
    wj = _job(manager, 1, JobState.AWAITING_TITLES)
    async with _async_client(app) as ac:
        first, second = await asyncio.gather(
            ac.post("/api/jobs/1/titles", json={"title_ids": [1, 2]}),
            ac.post("/api/jobs/1/titles", json={"title_ids": [7]}),
        )

    codes = sorted(r.status_code for r in (first, second))
    assert codes == [200, 400], (first.status_code, second.status_code)
    winner = first if first.status_code == 200 else second
    loser = second if winner is first else first
    assert "not awaiting title selection" in _detail(loser)

    expected = [1, 2] if winner is first else [7]
    assert wj.selected_title_ids == expected, "the two submissions were merged"
    assert wj.state is JobState.AWAITING_MAPPING


async def test_two_concurrent_grain_decisions_do_not_wedge_the_gate(app, manager):
    """The grain gate is an asyncio Future the request resolves. Resolving it
    twice raises InvalidStateError — so the second click of an A/B choice must be
    refused at the gate, not crash the request that carries it."""
    loop = asyncio.get_running_loop()
    wj = _job(manager, 1, JobState.AWAITING_GRAIN)
    wj._grain_future = loop.create_future()

    async with _async_client(app) as ac:
        keep, drop = await asyncio.gather(
            ac.post("/api/jobs/1/grain", json={"tune_grain": True}),
            ac.post("/api/jobs/1/grain", json={"tune_grain": False}),
        )

    assert sorted(r.status_code for r in (keep, drop)) == [200, 400]
    loser = drop if keep.status_code == 200 else keep
    assert "not awaiting a grain decision" in _detail(loser)
    assert wj._grain_future.done()
    # The pipeline gets the answer of whichever request was accepted — never the
    # refused one, and never an exception in place of a value.
    assert wj._grain_future.result() is (keep.status_code == 200)


async def test_two_concurrent_deletes_remove_the_job_once_and_clean_up(
        app, manager, tmp_path):
    """Removal cleans a staging directory on the NAS, so it awaits — which is
    exactly where a second DELETE can slip in. Both must succeed, the job must
    leave the queue and the store, and the staging directory must be gone."""
    staging = tmp_path / "staging" / "job_5"
    staging.mkdir(parents=True)
    (staging / "title_00.mkv").write_bytes(b"partial rip")
    wj = _job(manager, 5, JobState.FAILED)
    manager._persist(wj)
    assert manager.store.load_all(), "fixture did not persist the job"

    async with _async_client(app) as ac:
        first, second = await asyncio.gather(
            ac.delete("/api/jobs/5"), ac.delete("/api/jobs/5"))

    assert (first.status_code, second.status_code) == (200, 200)
    assert manager.jobs == {}
    assert manager.store.load_all() == []
    assert not staging.exists(), "the removed job's staging dir was left behind"


async def test_two_concurrent_settings_writes_both_survive(app, manager):
    """Saving settings is read-modify-write on one file. Two panels saving
    different fields at the same time must not lose one of them — the second
    write must merge, not replace the file it read before the first landed."""
    cfg = manager.settings.config_dir
    async with _async_client(app) as ac:
        a, b = await asyncio.gather(
            ac.post("/api/settings", json={"encode": {"tune_grain": True}}),
            ac.post("/api/settings", json={"subtitles": {"ocr_concurrency": 8}}),
        )

    assert (a.status_code, b.status_code) == (200, 200)
    saved = read_overlay(cfg)
    assert saved["encode"]["tune_grain"] is True
    assert saved["subtitles"]["ocr_concurrency"] == 8
    assert json.loads((cfg / "settings.local.json").read_text(encoding="utf-8")) == saved


# --------------------------------------------------------------------------
# requests aimed at a job in the wrong state
# --------------------------------------------------------------------------

def test_a_gate_submitted_for_a_job_past_that_gate_is_refused_by_name(
        client, manager):
    """The tab was open while the disc finished. Posting the title form now must
    be refused with a message that says which gate — and must not drag a finished
    job back into the queue."""
    wj = _job(manager, 1, JobState.DONE)
    r = client.post("/api/jobs/1/titles", json={"title_ids": [1]})
    assert r.status_code == 400
    assert "Job 1 is not awaiting title selection." == _detail(r)
    assert wj.state is JobState.DONE
    assert wj.selected_title_ids == []


def test_an_empty_title_selection_is_refused_before_anything_moves(client, manager):
    """Submitting the form with nothing ticked would rip zero titles and report
    the job ready — this project has shipped that failure before."""
    wj = _job(manager, 1, JobState.AWAITING_TITLES)
    r = client.post("/api/jobs/1/titles", json={"title_ids": []})
    assert r.status_code == 400
    assert _detail(r) == "No titles selected."
    assert wj.state is JobState.AWAITING_TITLES
    assert wj.selected_title_ids == []


def test_requeueing_a_job_that_is_not_stalled_is_refused(client, manager):
    """Requeue restarts work from checkpoints. Aimed at a job that is genuinely
    running, it would race a live encode — so it must be refused, and must not
    have already reset the state on its way to the refusal.

    The message is pinned exactly because requeue has a second guard right behind
    this one ("no scan state"); a substring match would let the wrong guard take
    the credit and hide the removal of the stalled check."""
    wj = _job(manager, 1, JobState.AWAITING_MAPPING)
    r = client.post("/api/jobs/1/requeue")
    assert r.status_code == 400
    assert _detail(r) == "Job 1 isn't stalled — nothing to requeue.", _detail(r)
    assert wj.state is JobState.AWAITING_MAPPING
    assert wj.error is None


def test_restart_is_refused_when_the_server_was_not_launched_by_serve(client, app):
    """The dashboard offers Restart even when the process wasn't started by
    ``serve`` (a dev run, or an embedded app). It must say so rather than
    pretending to restart and leaving the operator waiting for a server that
    never went down."""
    r = client.post("/api/server/restart", json={})
    assert r.status_code == 501
    assert "serve" in _detail(r)
    assert getattr(app.state, "restart_requested", None) is None
