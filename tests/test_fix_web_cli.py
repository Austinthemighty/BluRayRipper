"""Regressions for the web API and the CLI: refusals that used to be crashes,
and messages that used to arrive with their point removed.

Two failure shapes are covered here.

The HTTP half: an endpoint that answers 500 tells the operator nothing and
leaves the UI with no message to render, so a request the server will not honour
has to come back as a 4xx that says what is wrong. Four of these got there by an
exception the handler did not expect (``int([])``, ENAMETOOLONG, ``csv.Error``)
and one by a scope test that disagreed with the module that acts on the scope —
the reply described a per-worker change while the server's own config was what
had been edited.

The CLI half: exit codes and messages are the whole interface. A `queue` that
exits 0 having skipped a disc, a `check` that prints "All checks passed" on a box
that cannot encode UHD, and a "set its path under [tools]" that rich renders as
"set its path under " are each a silent failure — the command reports success,
or reports a fix the user cannot follow.
"""

import argparse
import asyncio
import json
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from blurayripper import cli
from blurayripper.config import ToolNotFoundError, load_settings
from blurayripper.models import DiscInfo, EpisodeAssignment, MediaKind
from blurayripper.persistence import MemoryStore, WorkerInfo
from blurayripper.pipeline import Job, JobState, TitleResult
from blurayripper.web.app import create_app
from blurayripper.web.manager import WebJob


# --------------------------------------------------------------------------
# web helpers
# --------------------------------------------------------------------------

class DurableMem(MemoryStore):
    """A MemoryStore that claims to be the shared Postgres store."""
    durable = True


class _DeadTask:
    def done(self) -> bool:
        return True

    def cancel(self) -> None:
        pass


def _build(tmp_path, store=None):
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "settings.toml").write_text("", encoding="utf-8")
    settings = load_settings(cfg)
    settings.config_dir = cfg
    settings.database_url = None              # never reach for a real Postgres
    settings.storage.output_dir = tmp_path / "out"
    settings.storage.staging_dir = tmp_path / "staging"
    settings.encode.preset_file = tmp_path / "presets.json"
    settings.notify_url = None
    app = create_app(settings)
    if store is not None:
        app.state.manager.store = store

    def _spawn(coro):                          # no real rip/encode coroutines
        coro.close()
        return _DeadTask()

    app.state.manager._spawn = _spawn
    return app


def _client(app):
    """raise_server_exceptions=False so an unhandled exception arrives as the 500
    it would be in production. Without it these tests would "fail" by erroring
    out, and could not tell a 500 apart from a 400."""
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def app(tmp_path):
    return _build(tmp_path)


@pytest.fixture
def client(app):
    return _client(app)


@pytest.fixture
def manager(app):
    return app.state.manager


def _detail(response):
    try:
        return response.json().get("detail")
    except (ValueError, AttributeError):
        return None


# --------------------------------------------------------------------------
# cli helpers
# --------------------------------------------------------------------------

def _cli_settings(tmp_path, **over):
    """A configuration where everything else already passes, so a non-zero exit
    is attributable to the one thing under test."""
    s = load_settings()
    s.config_dir = tmp_path
    s.storage.staging_dir = tmp_path / "staging"
    s.storage.output_dir = tmp_path / "out"
    s.database_url = None
    s.tmdb_api_key = "tmdb-key"
    s.languages.keep = ["eng"]
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    for name in ("makemkvcon", "handbrakecli", "mkvmerge", "mkvextract",
                 "subtitleedit"):
        exe = tools / name
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)                       # _on_disk uses shutil.which
        setattr(s.tools, name, str(exe))
    s.tools.subtitleedit_runner = None
    s.tools.ffmpeg = s.tools.dovitool = ""
    preset = tmp_path / "presets.json"
    preset.write_text(json.dumps({"PresetList": [
        {"PresetName": "P", "FileFormat": "av_mkv", "VideoEncoder": "x265",
         "VideoQualitySlider": 20}]}))
    s.encode.preset_file, s.encode.preset_name = preset, "P"
    s.encode.container = "mkv"
    s.encode.ffmpeg_uhd = s.encode.dovi = False
    s.subtitles.fix_common_errors = False
    s.queue.encode_capacity = 3
    for key, value in over.items():
        setattr(s, key, value)
    return s


def _use(monkeypatch, settings):
    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: settings)
    return settings


def _ns(**kw):
    kw.setdefault("config", None)
    return argparse.Namespace(**kw)


def _flat(captured) -> str:
    """Rich word-wraps to 80 columns off a terminal, so one sentence arrives
    split across lines. Collapse it back before matching."""
    return " ".join(captured.out.split())


def _run_check(monkeypatch, settings):
    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: settings)

    async def _help(self):
        return ""

    monkeypatch.setattr(
        "blurayripper.tools.subtitleedit.SubtitleEdit.help_text", _help)
    real_which = __import__("shutil").which
    monkeypatch.setattr("shutil.which", lambda n, *a, **k:
                        "/usr/bin/java" if n == "java" else real_which(n, *a, **k))
    return asyncio.run(cli._cmd_check(_ns()))


def _fake_pipeline(*, prepare=None, execute=None):
    class _FakePipeline:
        def __init__(self, settings, callbacks=None):
            pass

        async def prepare(self, source):
            return await prepare(self, source)

        async def execute(self, job, scheduler, report, job_name=None):
            return await execute(self, job, scheduler, report, job_name)

    return _FakePipeline


# --------------------------------------------------------------------------
# POST /api/jobs/{id}/rename — a year that is not a number
# --------------------------------------------------------------------------

def test_a_year_that_is_a_json_list_is_refused_by_name_not_by_traceback(
        client, manager):
    """The rename box posts whatever the page holds, and a stale/hand-edited
    fetch() can send `year: []`. `int([])` is a TypeError, which the job-error
    wrapper does not map, so the request came back 500: no message for the UI to
    show, and no way to tell it apart from the server being broken.

    A string year like "abc" was always a 400 — the two must not differ."""
    manager.jobs[7] = WebJob(
        id=7, source="disc:0", state=JobState.DONE,
        assignments={1: EpisodeAssignment(
            title_id=1, kind=MediaKind.MOVIE,
            show_or_movie_title="Dune", year=1984)})

    bad = client.post("/api/jobs/7/rename", json={"title": "Dune", "year": []})

    assert bad.status_code == 400, bad.text[:300]
    assert "year" in str(_detail(bad)).lower(), _detail(bad)
    # Refused whole: the job still carries the identity it had.
    kept = manager.jobs[7].assignments[1]
    assert (kept.show_or_movie_title, kept.year) == ("Dune", 1984)
    # And the refusal did not wedge the endpoint — a real year still lands.
    good = client.post("/api/jobs/7/rename", json={"title": "Dune", "year": 2021})
    assert good.status_code == 200, good.text[:300]
    assert manager.jobs[7].assignments[1].year == 2021


# --------------------------------------------------------------------------
# POST /api/jobs — a source the OS itself refuses to look at
# --------------------------------------------------------------------------

def test_a_source_past_the_os_name_limit_is_refused_and_adds_no_job(
        client, manager):
    """Pasting a mangled path into "add a folder" is ordinary. A path component
    past ~255 characters makes pathlib re-raise ENAMETOOLONG out of is_dir(),
    which is an OSError — not the ValueError add_source promises — so the box
    answered 500 and the UI showed nothing at all."""
    before = dict(manager.jobs)

    r = client.post("/api/jobs", json={"source": "Q" * 300})

    assert r.status_code == 400, r.text[:300]
    # Assert the substance, not one phrasing: it must name the length problem
    # rather than sending the user after a mount that is fine.
    detail = str(_detail(r)).lower()
    assert "300" in detail and "255" in detail, detail
    assert "mapped drive" not in detail, detail
    assert manager.jobs == before, "a refused source still created a job"


# --------------------------------------------------------------------------
# /api/boxsets — an unmatched quote in the plan
# --------------------------------------------------------------------------

def _plan_with_a_stray_quote() -> str:
    """One unterminated double quote makes csv read every following line as a
    single field; past 128 KB that is a csv.Error, not a bad row."""
    return ('disc_label,show\nA_S1_D1,"Unterminated\n'
            + "".join(f"B_S1_D{i},Show {i}\n" for i in range(20000)))


def test_a_plan_with_a_stray_quote_is_reported_not_500ed(client):
    """The preview button is exactly where a bad paste is meant to be caught.
    parse_csv documents "never raises" and the csv module raises anyway, so the
    preview took the editor down instead of naming the problem."""
    r = client.post("/api/boxsets",
                    json={"csv": _plan_with_a_stray_quote(), "dry_run": True})

    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["errors"], "the unparseable plan was reported as clean"
    assert 'quote' in " ".join(body["errors"]).lower(), body["errors"]
    assert body["rows"] == []          # nothing invented out of a broken parse
    assert body["saved"] is False      # a dry run still wrote nothing


def test_the_boxset_editor_still_opens_on_a_saved_plan_it_cannot_parse(
        app, client, manager):
    """The editor is the only place the stray quote can be FIXED, so it has to
    load — with the raw text and the reason — rather than 500 and leave the
    operator with a plan file no page will show them."""
    from blurayripper import boxset_plan

    text = _plan_with_a_stray_quote()
    boxset_plan.save_plan(manager.settings, text)

    r = client.get("/api/boxsets")

    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["csv"] == text, "the text needing repair was not handed back"
    assert body["errors"], "the editor claimed the broken plan was fine"


# --------------------------------------------------------------------------
# POST /api/settings — what a scope means
# --------------------------------------------------------------------------

def test_an_empty_scope_edits_the_server_config_and_says_that_is_what_it_did(
        client, tmp_path):
    """`?scope=` is what the page sends before its worker dropdown has loaded.
    settings_store routes that to the GLOBAL overlay — the server's own config,
    which needs a restart — while the endpoint used to answer "scope: '',
    applies: next job". The operator then waited for a change that had already
    been written to a file nothing was going to re-read."""
    r = client.post("/api/settings", params={"scope": ""},
                    json={"encode": {"ffmpeg_preset": "slow"}})

    assert r.status_code == 200, r.text[:300]
    overlay = json.loads((tmp_path / "config" / "settings.local.json").read_text())
    assert overlay["encode"]["ffmpeg_preset"] == "slow"   # the server's own config
    body = r.json()
    assert body["scope"] == "global", body
    assert body["restart_required"] is True, body
    assert body["applies"] == "restart", body


def test_a_typo_in_a_worker_name_is_refused_instead_of_inventing_that_worker(
        tmp_path):
    """A worker_settings row is permanent and the Settings page lists every row
    it finds, so one typo'd scope leaves a machine in the dropdown forever that
    no worker will ever read. request_worker_action already refuses an unknown
    target for this reason; a save that reaches nobody is no better.

    The pair is the point: the same request against the correctly spelled worker
    must still be saved, or this would only prove the endpoint stopped working.
    """
    store = DurableMem()
    store.list_workers = lambda: [WorkerInfo("SteamMachine", "v1", None, "idle", 1.0)]
    client = _client(_build(tmp_path, store))

    typo = client.post("/api/settings", params={"scope": "Steammachine"},
                       json={"encode": {"ffmpeg_preset": "veryslow"}})

    assert typo.status_code == 400, typo.text[:300]
    assert "Steammachine" in str(_detail(typo)), _detail(typo)
    assert store.all_worker_settings() == {}, "the typo became a worker row"
    listed = client.get("/api/settings").json()["workers"]
    assert listed == ["SteamMachine"], listed

    real = client.post("/api/settings", params={"scope": "SteamMachine"},
                       json={"encode": {"ffmpeg_preset": "veryslow"}})
    assert real.status_code == 200, real.text[:300]
    assert store.get_worker_settings("SteamMachine") == {
        "encode": {"ffmpeg_preset": "veryslow"}}


# --------------------------------------------------------------------------
# `check` — the UHD row
# --------------------------------------------------------------------------

def test_check_fails_a_uhd_box_whose_ffmpeg_is_configured_but_not_installed(
        tmp_path, monkeypatch, capsys):
    """The shared cluster config names one ffmpeg path for every box, so a
    Windows worker routinely carries the Linux serve box's path. Gating the UHD
    row on truthiness passed that box green: "UHD via ffmpeg ... All checks
    passed", exit 0 — and then every UHD title failed to spawn mid-encode, with
    uhd_require_ffmpeg keeping HandBrake off them too, so the disc parked.

    Both runs are identical but for whether ffmpeg exists, so the exit code is
    actually attributable to it."""
    absent = _cli_settings(tmp_path)
    absent.encode.ffmpeg_uhd = True
    absent.tools.ffmpeg = str(tmp_path / "not-installed" / "ffmpeg")
    rc_absent = _run_check(monkeypatch, absent)
    out = _flat(capsys.readouterr())

    installed = _cli_settings(tmp_path)
    installed.encode.ffmpeg_uhd = True
    ff = tmp_path / "tools" / "ffmpeg"
    ff.write_text("#!/bin/sh\n")
    ff.chmod(0o755)
    installed.tools.ffmpeg = str(ff)
    rc_installed = _run_check(monkeypatch, installed)
    ok_out = _flat(capsys.readouterr())

    assert rc_installed == 0, ok_out[-400:]      # fixture otherwise passes
    assert "UHD via ffmpeg" in ok_out
    assert rc_absent != 0, "check passed a box that cannot encode UHD"
    assert "All checks passed" not in out
    assert "UHD via ffmpeg" not in out, "the green UHD row was printed anyway"
    assert "UHD titles can't encode" in out
    assert "not-installed" in out, "the message never names the path to fix"


# --------------------------------------------------------------------------
# `queue` — the exit code after a skipped disc
# --------------------------------------------------------------------------

def test_queue_exits_nonzero_when_a_disc_never_got_past_setup(
        tmp_path, monkeypatch, capsys):
    """A disc dropped during setup used to vanish from the verdict entirely: the
    tally compared the finished jobs against the SHRUNKEN prepared list, so
    `queue` exited 0 having ripped two discs of three. The exit code is the only
    thing an overnight batch reads, so the skip has to reach it — while the
    discs that did work still finish and still get reported."""
    _use(monkeypatch, _cli_settings(tmp_path))

    async def _prepare(self, source):
        if source == "disc:9":
            raise RuntimeError("no disc in drive")
        return Job(source=source, disc=DiscInfo(source=source, disc_name="OK"))

    async def _execute(self, job, scheduler, report, job_name=None):
        job.results = [TitleResult(title_id=1,
                                   video_path=tmp_path / f"{job.source[-1]}.mkv")]
        return job

    monkeypatch.setattr(cli, "Pipeline",
                        _fake_pipeline(prepare=_prepare, execute=_execute))

    rc = asyncio.run(cli._cmd_queue(_ns(sources=["disc:0", "disc:9", "disc:1"])))

    out = _flat(capsys.readouterr())
    assert rc == 1, "a skipped disc left the run looking successful"
    assert "2/3 succeeded" in out, out[-300:]
    assert "Skipping disc:9" in out           # named, so it can be re-run
    assert "0.mkv" in out and "1.mkv" in out  # the good discs still placed


# --------------------------------------------------------------------------
# messages rich used to eat
# --------------------------------------------------------------------------

def test_the_tools_section_survives_into_the_missing_tool_message(
        tmp_path, monkeypatch, capsys):
    """"[tools]" is the one thing a ToolNotFoundError exists to say: WHERE to
    put the path. Printed inside a markup string, rich read it as a style tag and
    dropped it, so the user was told to "set its path under  in
    config/settings.toml" and had nothing to search settings.toml for."""
    _use(monkeypatch, _cli_settings(tmp_path))

    class _Missing:
        def __init__(self, settings):
            pass

        async def scan(self, source, *, on_message=None):
            raise ToolNotFoundError("makemkvcon")

    monkeypatch.setattr(cli, "MakeMKV", _Missing)

    rc = cli.main(["scan", "disc:0"])

    out = _flat(capsys.readouterr())
    assert rc == 2
    assert "[tools]" in out, out
    assert "under [tools] in config/settings.toml" in out, out


def test_the_web_extra_survives_into_the_install_command(
        tmp_path, monkeypatch, capsys):
    """This message is the only guidance shown when uvicorn is missing, and rich
    ate the "[web]" out of it — leaving `pip install -e "."`, a command that
    installs successfully and leaves `serve` broken in exactly the same way."""
    _use(monkeypatch, _cli_settings(tmp_path))
    monkeypatch.setitem(sys.modules, "uvicorn", None)

    rc = asyncio.run(cli._cmd_serve(_ns(host=None, port=None)))

    out = _flat(capsys.readouterr())
    assert rc == 2
    assert '".[web]"' in out, out


# --------------------------------------------------------------------------
# TMDB failures during interactive naming
# --------------------------------------------------------------------------

def test_a_tmdb_http_error_falls_back_to_typing_the_name_by_hand(
        tmp_path, monkeypatch, capsys):
    """Only a MISSING key raises MetadataError. A revoked key answers 401, a
    proxy answers HTML, a sleeping DNS answers nothing — all of which escaped the
    `except MetadataError` and aborted the whole command with a traceback, AFTER
    the disc had been scanned and the titles chosen. That is the same fallback
    the code already gives you when the key is simply absent, so it has to be
    reached the same way: the naming gate continues, by hand."""
    cb = cli.CliCallbacks(_cli_settings(tmp_path))

    async def _revoked(query, year=None):
        raise httpx.HTTPStatusError(
            "401 Unauthorized",
            request=httpx.Request("GET", "https://api.themoviedb.org/3/search/movie"),
            response=httpx.Response(401))

    monkeypatch.setattr(cb.tmdb, "search_movie", _revoked)
    answers = iter(["Dune", "1984"])
    monkeypatch.setattr(cli.Prompt, "ask",
                        staticmethod(lambda *a, **k: next(answers)))

    disc = DiscInfo(source="disc:0", disc_name="DUNE")
    assigns = asyncio.run(cb._confirm_movie(disc, [1]))

    out = _flat(capsys.readouterr())
    assert assigns[1].show_or_movie_title == "Dune"
    assert assigns[1].year == 1984, "the hand-typed year was lost"
    assert "TMDB" in out and "by hand" in out, out
    assert "401" in out, "the reason the lookup failed was not reported"


def test_a_tmdb_outage_between_show_and_season_still_names_the_episodes(
        tmp_path, monkeypatch, capsys):
    """The season lookup is the second TMDB call in the TV gate, and it fails on
    its own: the show search worked, the operator has already picked the show and
    typed a season, and only the episode-title lookup is unreachable. Losing the
    whole command there throws away a scan and every answer given so far, for a
    field (the episode title) that is optional."""
    from blurayripper.services.metadata import ShowCandidate

    cb = cli.CliCallbacks(_cli_settings(tmp_path))

    async def _found(query):
        return [ShowCandidate(tmdb_id=1234, name="Hawaii Five-O", year=1968,
                              overview="")]

    async def _proxy_html(tv_id, season):
        raise json.JSONDecodeError("Expecting value", "<html>504</html>", 0)

    monkeypatch.setattr(cb.tmdb, "search_tv", _found)
    monkeypatch.setattr(cb.tmdb, "get_season", _proxy_html)
    monkeypatch.setattr(cli.Prompt, "ask",
                        staticmethod(lambda *a, **k: "Hawaii Five-0"))
    numbers = iter([0, 2, 5, 5])   # pick #, season, first episode, title 1 -> ep
    monkeypatch.setattr(cli.IntPrompt, "ask",
                        staticmethod(lambda *a, **k: next(numbers)))

    disc = DiscInfo(source="disc:0", disc_name="HAWAII_FIVE_0_S2")
    assigns = asyncio.run(cb._confirm_tv(disc, [1]))

    out = _flat(capsys.readouterr())
    got = assigns[1]
    assert (got.show_or_movie_title, got.season, got.episode) == \
        ("Hawaii Five-O", 2, 5)
    assert got.episode_title is None       # the only thing the outage cost
    assert "TMDB" in out and "by hand" in out, out


# --- an encode-only worker is not a broken rip box ---------------------------

def test_check_does_not_fail_a_worker_for_having_no_makemkv(tmp_path, monkeypatch, capsys):
    """setup-linux.sh --no-makemkv builds an encode-only worker on purpose, then
    `check` ended with "Some checks failed" because the ripper it was told not to
    install was not installed. That reads as broken and sends the operator
    looking for a problem they deliberately chose."""
    import asyncio
    from argparse import Namespace

    from blurayripper.cli import _cmd_check
    from blurayripper.config import load_settings

    # Rich elides table cells at the terminal width, so a narrow console turns
    # the status into "…" and the assertion below would be testing formatting.
    monkeypatch.setenv("COLUMNS", "220")
    s_ = load_settings()
    s_.storage.staging_dir = tmp_path / "stage"
    s_.storage.output_dir = tmp_path / "out"
    s_.tools.makemkvcon = ""            # the only tool a worker does not need
    monkeypatch.setattr("blurayripper.cli.load_settings", lambda *a, **k: s_)

    asyncio.run(_cmd_check(Namespace(encode_only=True, config=None)))
    worker_out = capsys.readouterr().out
    assert "encode-only worker" in worker_out

    # ...and the SAME box checked as a rip box still reports the ripper missing,
    # which is the whole point of not just deleting the check.
    asyncio.run(_cmd_check(Namespace(encode_only=False, config=None)))
    rip_out = capsys.readouterr().out
    assert "MISSING" in rip_out
    assert "encode-only worker" not in rip_out
