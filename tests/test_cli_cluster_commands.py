"""The cluster/admin half of the CLI: serve, worker, db-init, secrets, main().

Every one of these commands does a pile of setup and then hands control to a
loop that never returns, so the setup is the only part a test can hold — and it
is the part whose mistakes are expensive and quiet:

  * a worker that starts without a database looks healthy and claims nothing;
  * a box that skips the cluster config pull encodes at whatever preset that
    machine was last set to, and the output only looks wrong next to another
    worker's;
  * db-init reporting "ready" against an in-memory store means believing there
    is crash recovery when there is none;
  * `secrets` echoing a value would put every shared credential into shell
    history and terminal scrollback, which is the one thing it exists to stop.

Nothing here starts a server, a worker loop, or a subprocess.
"""

import argparse
import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

from blurayripper import boxset_plan, handbrake_store, settings_store
from blurayripper.cli import (
    _build_parser,
    _cmd_db_init,
    _cmd_secrets,
    _cmd_serve,
    _cmd_worker,
    _storage_remedy,
    main,
)
from blurayripper.config import ToolNotFoundError, load_settings
from blurayripper.persistence import MemoryStore
from blurayripper.services import secretbox


# --------------------------------------------------------------------------
# fixtures / doubles
# --------------------------------------------------------------------------


class Durable(MemoryStore):
    """The in-memory store advertising itself as clustered.

    Every shared-config path (secretbox, settings_store, boxset_plan) bails out
    early on a non-durable store, so the clustered behaviour is unreachable
    without this."""

    @property
    def durable(self):
        return True


class FakeDurableStore:
    """A durable store that is NOT a MemoryStore — db-init branches on the type."""

    durable = True

    def __init__(self, records=(), init_error=None):
        self.records = list(records)
        self.init_calls = 0
        self._init_error = init_error

    def init(self):
        self.init_calls += 1
        if self._init_error is not None:
            raise self._init_error

    def load_all(self):
        return list(self.records)


def _settings(tmp_path, **over):
    """A Settings whose every machine-specific path points into tmp_path."""
    s = load_settings()
    s.config_dir = tmp_path
    s.storage.staging_dir = tmp_path / "staging"
    s.storage.output_dir = tmp_path / "out"
    s.database_url = None
    s.cleanup_stale_tools = False
    for key, value in over.items():
        setattr(s, key, value)
    return s


def _prose(out: str) -> str:
    """Console output with rich's 80-column hard wrapping undone.

    Asserting on a sentence otherwise depends on where the terminal happened to
    break it, which differs between a developer's terminal and CI."""
    return " ".join(out.split())


def _unwrapped(out: str) -> str:
    """Console output with the newlines removed, for long unbroken tokens (paths,
    URLs) that rich splits mid-word rather than at a space."""
    return out.replace("\n", "")


def _use_settings(monkeypatch, *settings):
    """Serve ``settings`` to successive load_settings() calls (last one repeats).

    The commands reload settings after a cluster pull changes them; handing out
    distinct objects is how a test can tell a reload happened."""
    calls = []

    def _load(*a, **k):
        calls.append(a[0] if a else None)
        return settings[min(len(calls) - 1, len(settings) - 1)]

    monkeypatch.setattr("blurayripper.cli.load_settings", _load)
    return calls


# --------------------------------------------------------------------------
# db-init
# --------------------------------------------------------------------------


def _run_db_init(monkeypatch, settings, store=None):
    _use_settings(monkeypatch, settings)
    if store is not None:
        monkeypatch.setattr("blurayripper.persistence.make_store", lambda url: store)
    return asyncio.run(_cmd_db_init(argparse.Namespace(config=None)))


def test_db_init_names_an_in_memory_store_instead_of_reporting_success(tmp_path, monkeypatch, capsys):
    """Running db-init on a box with no [database].url must not look like it
    provisioned anything. MemoryStore.init() is a no-op that succeeds, so a
    "Database ready" here would leave the operator believing crash recovery is
    on — and the belief only breaks when a power cut loses an eight-hour rip."""
    rc = _run_db_init(monkeypatch, _settings(tmp_path))

    out = _prose(capsys.readouterr().out)
    assert rc == 1
    assert "No database configured" in out
    assert "Database ready" not in out


def test_db_init_creates_the_schema_and_reports_what_is_already_stored(tmp_path, monkeypatch, capsys):
    """The count is the confirmation that it reached the real database rather
    than a fresh one: pointing a box at the wrong Postgres otherwise looks
    exactly like a successful init."""
    store = FakeDurableStore(records=["job-a", "job-b"])
    rc = _run_db_init(
        monkeypatch, _settings(tmp_path, database_url="postgresql://db/brr"), store)

    out = _prose(capsys.readouterr().out)
    assert rc == 0
    assert store.init_calls == 1, "the schema must actually be ensured"
    assert "2 job(s)" in out


def test_db_init_surfaces_a_failed_schema_creation(tmp_path, monkeypatch, capsys):
    """An unreachable Postgres is the normal failure (the NAS and the database
    go down together here). The exit code has to differ from the healthy run or
    a provisioning script cannot branch on it."""
    store = FakeDurableStore(init_error=RuntimeError("could not connect to host"))
    rc = _run_db_init(
        monkeypatch, _settings(tmp_path, database_url="postgresql://db/brr"), store)

    out = _prose(capsys.readouterr().out)
    assert rc == 2
    assert "Database init failed" in out and "could not connect to host" in out
    assert "Database ready" not in out


# --------------------------------------------------------------------------
# secrets
# --------------------------------------------------------------------------


def _run_secrets(monkeypatch, settings, action, raw, store):
    _use_settings(monkeypatch, settings)
    monkeypatch.setattr("blurayripper.config._load_secrets", lambda cfg: dict(raw))
    monkeypatch.setattr("blurayripper.persistence.make_store", lambda url: store)
    return asyncio.run(_cmd_secrets(argparse.Namespace(config=None, action=action)))


PASSPHRASE = secretbox.PASSPHRASE_KEY


def test_secrets_refuses_to_run_without_a_database(tmp_path, monkeypatch, capsys):
    """There is nowhere to share with. Reaching the store anyway would push the
    box's credentials into whatever make_store() falls back to."""
    store = Durable()
    rc = _run_secrets(
        monkeypatch, _settings(tmp_path), "push",
        {PASSPHRASE: "pw", "TMDB_API_KEY": "abc"}, store)

    assert rc == 2
    assert "No database configured" in _prose(capsys.readouterr().out)
    assert store.get_shared_config(secretbox.SHARED_KEY) is None


def test_secrets_refuses_without_a_passphrase_and_says_which_key_is_missing(
        tmp_path, monkeypatch, capsys):
    """Without the passphrase there is no key to encrypt under, so pushing would
    either crash deep in secretbox or (worse) store something readable. The two
    runs differ only in that one line of secrets.env, so the verdict is
    attributable to it and not to anything else in the config."""
    clustered = dict(database_url="postgresql://db/brr")
    credentials = {"TMDB_API_KEY": "abc"}

    store = Durable()
    without = _run_secrets(
        monkeypatch, _settings(tmp_path, **clustered), "push", credentials, store)
    out = _prose(capsys.readouterr().out)
    stored_after_refusal = store.get_shared_config(secretbox.SHARED_KEY)

    with_key = _run_secrets(
        monkeypatch, _settings(tmp_path, **clustered), "push",
        {PASSPHRASE: "pw", **credentials}, store)

    assert without == 2
    assert PASSPHRASE in out, "the operator has to be told which key to generate"
    assert stored_after_refusal is None, "nothing may reach the cluster"
    assert with_key == 0                        # the passphrase alone flipped it
    assert store.get_shared_config(secretbox.SHARED_KEY) is not None


def test_secrets_push_stores_ciphertext_and_never_echoes_a_value(
        tmp_path, monkeypatch, capsys):
    """Two separate promises. The terminal one keeps credentials out of shell
    history and scrollback; the database one is the whole reason this exists —
    a dump of the shared Postgres has to be inert."""
    store = Durable()
    rc = _run_secrets(
        monkeypatch, _settings(tmp_path, database_url="postgresql://db/brr"), "push",
        {PASSPHRASE: "pw",
         "TMDB_API_KEY": "tmdb-SECRET-value",
         "OPENSUBTITLES_API_KEY": "os-SECRET-value"},
        store)

    out = _unwrapped(capsys.readouterr().out)
    blob = json.dumps(store.get_shared_config(secretbox.SHARED_KEY))
    assert rc == 0
    assert "Stored 2 credential(s)" in out
    assert "TMDB_API_KEY" in out and "OPENSUBTITLES_API_KEY" in out
    for value in ("tmdb-SECRET-value", "os-SECRET-value"):
        assert value not in out, "a value was printed to the terminal"
        assert value not in blob, "the cluster copy is not encrypted"
    assert secretbox.decrypt("pw", store.get_shared_config(secretbox.SHARED_KEY)) == {
        "TMDB_API_KEY": "tmdb-SECRET-value",
        "OPENSUBTITLES_API_KEY": "os-SECRET-value",
    }


def test_secrets_push_from_a_box_that_has_none_stores_nothing(tmp_path, monkeypatch, capsys):
    """Pushing an empty set would overwrite the cluster's real credentials with
    nothing, and every other box would lose them at its next restart."""
    store = Durable()
    store.set_shared_config(
        secretbox.SHARED_KEY, secretbox.encrypt("pw", {"TMDB_API_KEY": "keepme"}))
    before = store.get_shared_config(secretbox.SHARED_KEY)

    rc = _run_secrets(
        monkeypatch, _settings(tmp_path, database_url="postgresql://db/brr"), "push",
        {PASSPHRASE: "pw", "BLURAYRIPPER_DATABASE_URL": "postgresql://db/brr"}, store)

    out = _prose(capsys.readouterr().out)
    assert rc == 1
    assert "Nothing to push" in out
    assert store.get_shared_config(secretbox.SHARED_KEY) == before


def test_secrets_status_shows_names_only_and_flags_a_local_override(
        tmp_path, monkeypatch, capsys):
    """`raw` already has the cluster values merged in, so the only way to tell
    "this box overrides it" from "this box received it" is to read secrets.env
    off the disk. Without that distinction a box quietly running its own stale
    key looks identical to one on the cluster's."""
    store = Durable()
    store.set_shared_config(secretbox.SHARED_KEY, secretbox.encrypt(
        "pw", {"TMDB_API_KEY": "cluster-tmdb", "OPENSUBTITLES_API_KEY": "cluster-os"}))
    (tmp_path / "secrets.env").write_text(
        "# a comment\nTMDB_API_KEY=local-override-value\n", encoding="utf-8")

    rc = _run_secrets(
        monkeypatch, _settings(tmp_path, database_url="postgresql://db/brr"), "status",
        {PASSPHRASE: "pw",
         "TMDB_API_KEY": "local-override-value",
         "OPENSUBTITLES_API_KEY": "cluster-os"},
        store)

    out = capsys.readouterr().out
    rows = {name: next(line for line in out.splitlines() if name in line)
            for name in ("TMDB_API_KEY", "OPENSUBTITLES_API_KEY", "OPENSUBTITLES_PASSWORD")}
    assert rc == 0
    # in cluster + overridden here
    assert rows["TMDB_API_KEY"].count("yes") == 2
    # in cluster, not overridden here
    assert rows["OPENSUBTITLES_API_KEY"].count("yes") == 1
    # neither
    assert rows["OPENSUBTITLES_PASSWORD"].count("yes") == 0
    for value in ("cluster-tmdb", "cluster-os", "local-override-value"):
        assert value not in _unwrapped(out)


def test_secrets_status_reports_a_wrong_passphrase_rather_than_an_empty_cluster(
        tmp_path, monkeypatch, capsys):
    """Degrading to "nothing stored" is indistinguishable from a healthy cluster
    that has never been pushed to — the mismatch would surface weeks later as
    artwork quietly not downloading, not as a setup error."""
    store = Durable()
    store.set_shared_config(
        secretbox.SHARED_KEY, secretbox.encrypt("right-key", {"TMDB_API_KEY": "abc"}))

    rc = _run_secrets(
        monkeypatch, _settings(tmp_path, database_url="postgresql://db/brr"), "status",
        {PASSPHRASE: "wrong-key"}, store)

    out = _prose(capsys.readouterr().out)
    assert rc == 2
    assert "No credentials stored" not in out
    assert "must be identical everywhere" in out


def test_secrets_clear_removes_the_cluster_copy(tmp_path, monkeypatch, capsys):
    """Rotating a leaked key starts here; leaving the old blob behind would keep
    every box picking it back up on its next restart."""
    store = Durable()
    store.set_shared_config(
        secretbox.SHARED_KEY, secretbox.encrypt("pw", {"TMDB_API_KEY": "abc"}))

    rc = _run_secrets(
        monkeypatch, _settings(tmp_path, database_url="postgresql://db/brr"), "clear",
        {PASSPHRASE: "pw"}, store)

    assert rc == 0
    assert "Cleared" in _prose(capsys.readouterr().out)
    assert store.get_shared_config(secretbox.SHARED_KEY) == {}


# --------------------------------------------------------------------------
# worker
# --------------------------------------------------------------------------


def _fake_keypress(monkeypatch):
    """Stand in for the console key reader and record start/stop.

    On POSIX the real one puts the terminal into cbreak mode from a daemon
    thread, so leaving it in a test would depend on whether stdin happened to be
    a TTY and could hand the runner back a mangled shell."""
    log = []

    class _Keys:
        def __init__(self, on_key):
            self.on_key = on_key
            log.append(self)
            self.started = self.stopped = False

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

    monkeypatch.setattr("blurayripper.cli._KeyPress", _Keys)
    return log


def _fake_worker_runner(monkeypatch, on_run=None, raises=None,
                        restart_requested=False, update_requested=False):
    """Install a WorkerRunner double and return the list it records itself into."""
    made = []

    class _Runner:
        def __init__(self, settings, worker_id=None):
            self.settings = settings
            self.worker_id = worker_id or "test-host"
            self._stop = False
            self.ran = False
            self.restart_requested = restart_requested
            self.update_requested = update_requested
            made.append(self)

        def request_stop(self):
            self._stop = True

        def request_update(self):
            self.update_requested = True

        async def run(self):
            self.ran = True
            if on_run is not None:
                on_run(self)
            if raises is not None:
                raise raises

    monkeypatch.setattr("blurayripper.worker.WorkerRunner", _Runner)
    # Never leave pytest's own SIGINT handler replaced by the worker's drain hook.
    monkeypatch.setattr("blurayripper.cli._install_stop_handlers", lambda h: [])
    _fake_keypress(monkeypatch)
    return made


def _run_worker(name=None):
    return asyncio.run(_cmd_worker(argparse.Namespace(config=None, name=name)))


def test_a_worker_refuses_to_start_without_a_shared_database(tmp_path, monkeypatch, capsys):
    """A worker's entire job is claiming from the shared queue. Started against
    the in-memory fallback it would sit there looking healthy and never claim
    anything, which reads as "no work available" rather than as misconfiguration."""
    made = _fake_worker_runner(monkeypatch)
    _use_settings(monkeypatch, _settings(tmp_path))

    rc = _run_worker()

    out = _prose(capsys.readouterr().out)
    assert rc == 2
    assert made == [], "the run loop must not be reached"
    assert "BLURAYRIPPER_DATABASE_URL" in out


def test_a_worker_pulls_the_cluster_config_before_the_run_loop(tmp_path, monkeypatch, capsys):
    """A worker box keeps its own gitignored config, so without this pull it
    encodes at whatever preset that box was last set to. The drift is silent and
    the output looks fine until you compare it with another worker's — so the
    pull has to be finished before the first job can be claimed, not alongside."""
    store = Durable()
    store.set_shared_config(
        handbrake_store.SHARED_KEY, {"PresetList": [{"PresetName": "ClusterPreset"}]})
    store.set_shared_config(settings_store.SHARED_KEY, {"encode": {"quality": 19}})
    store.set_shared_config(boxset_plan.SHARED_KEY, {"csv": "show,season\nX,1\n"})
    monkeypatch.setattr("blurayripper.persistence.make_store", lambda url: store)

    seen = {}
    made = _fake_worker_runner(
        monkeypatch,
        on_run=lambda r: seen.update(
            preset=(tmp_path / handbrake_store.LOCAL_PRESET_NAME).exists(),
            boxsets=boxset_plan.plan_path(r.settings).exists()))
    _use_settings(monkeypatch, _settings(tmp_path, database_url="postgresql://db/brr"))

    rc = _run_worker()

    assert rc == 0 and made[0].ran
    assert seen == {"preset": True, "boxsets": True}, \
        "the cluster config must be on disk before the run loop starts"
    assert json.loads((tmp_path / handbrake_store.LOCAL_PRESET_NAME).read_text()) == {
        "PresetList": [{"PresetName": "ClusterPreset"}]}
    assert "Cluster config" in _prose(capsys.readouterr().out)


def test_a_worker_runs_on_the_settings_reloaded_after_the_cluster_pull(
        tmp_path, monkeypatch, capsys):
    """Writing presets.local.json is only half the job: the Settings already in
    hand were built before that file existed, so a worker that keeps them
    encodes with the old preset for its whole lifetime despite having pulled
    the new one."""
    store = Durable()
    store.set_shared_config(
        handbrake_store.SHARED_KEY, {"PresetList": [{"PresetName": "ClusterPreset"}]})
    monkeypatch.setattr("blurayripper.persistence.make_store", lambda url: store)

    made = _fake_worker_runner(monkeypatch)
    before = _settings(tmp_path, database_url="postgresql://db/brr")
    after = _settings(tmp_path, database_url="postgresql://db/brr")
    _use_settings(monkeypatch, before, after)

    rc = _run_worker()

    assert rc == 0
    assert made[0].settings is after, "the worker kept the pre-pull settings"


def test_a_worker_still_starts_when_the_cluster_config_is_unreachable(
        tmp_path, monkeypatch, capsys):
    """The database going down must not take the encode fleet with it — a worker
    falls back to its local config cache and keeps working the jobs it can see."""
    def _explode(url):
        raise ConnectionError("could not connect to server")

    monkeypatch.setattr("blurayripper.persistence.make_store", _explode)
    made = _fake_worker_runner(monkeypatch)
    _use_settings(monkeypatch, _settings(tmp_path, database_url="postgresql://db/brr"))

    rc = _run_worker()

    assert rc == 0
    assert made and made[0].ran, "a config-pull failure must not stop the worker"


def test_a_worker_aborted_mid_encode_says_the_job_will_be_reclaimed(
        tmp_path, monkeypatch, capsys):
    """The second Ctrl+C throws away hours of encoding. The operator needs to
    know the job is not lost — it goes back to the queue and is re-encoded from
    the rip — or the natural next move is to re-rip the disc.

    The key reader must be stopped on this path too: it leaves the terminal in
    cbreak mode, so an abort that skipped it would hand back a broken shell."""
    made = _fake_worker_runner(monkeypatch, raises=KeyboardInterrupt())
    keys = _fake_keypress(monkeypatch)
    _use_settings(monkeypatch, _settings(tmp_path, database_url="postgresql://db/brr"))

    rc = _run_worker()

    out = _prose(capsys.readouterr().out)
    assert rc == 130
    assert made[0].ran
    assert "reclaimed" in out
    assert keys[0].started and keys[0].stopped, "the terminal was left in cbreak mode"


def test_a_worker_missing_a_tool_exits_with_the_configuration_code(
        tmp_path, monkeypatch, capsys):
    """Distinct from the drain exit: a supervising launcher should not treat a
    box with no HandBrake installed as a worker that finished cleanly."""
    made = _fake_worker_runner(
        monkeypatch, raises=ToolNotFoundError("HandBrakeCLI not found"))
    _use_settings(monkeypatch, _settings(tmp_path, database_url="postgresql://db/brr"))

    rc = _run_worker()

    assert rc == 2
    assert made[0].ran
    assert "HandBrakeCLI not found" in _prose(capsys.readouterr().out)


def test_a_drained_worker_pulls_only_when_the_drain_was_an_update(
        tmp_path, monkeypatch, capsys):
    """'Update' and 'Restart' are different buttons in the web UI. A plain
    restart must come back on the code already on disk — pulling there would
    silently ship whatever is on main to a box someone restarted to clear a
    stuck encode. And when the re-exec fails, the exit code must be non-zero so
    the launcher brings the worker back up instead of reading a clean drain."""
    calls = []
    monkeypatch.setattr(
        "blurayripper.cli._pull_and_restart",
        lambda console, pull=True: calls.append(pull))  # returns == re-exec failed

    _fake_worker_runner(monkeypatch, restart_requested=True, update_requested=True)
    _use_settings(monkeypatch, _settings(tmp_path, database_url="postgresql://db/brr"))
    update_rc = _run_worker()

    _fake_worker_runner(monkeypatch, restart_requested=True, update_requested=False)
    _use_settings(monkeypatch, _settings(tmp_path, database_url="postgresql://db/brr"))
    restart_rc = _run_worker()

    assert calls == [True, False]
    assert update_rc == 75 and restart_rc == 75


# --------------------------------------------------------------------------
# serve
# --------------------------------------------------------------------------


class _FakeApp:
    def __init__(self):
        self.state = types.SimpleNamespace()


def _fake_uvicorn(monkeypatch, serve_impl=None):
    """Install a uvicorn double; the returned dict records what serve() built."""
    made = {}
    mod = types.ModuleType("uvicorn")

    class Config:
        def __init__(self, app, host=None, port=None, **kw):
            self.app, self.host, self.port, self.kw = app, host, port, kw
            made["config"] = self

    class Server:
        def __init__(self, config):
            self.config = config
            self.should_exit = False
            made["server"] = self

        async def serve(self):
            made["served"] = True
            if serve_impl is not None:
                await serve_impl(self)
            else:
                await asyncio.sleep(0)

    mod.Config, mod.Server = Config, Server
    monkeypatch.setitem(sys.modules, "uvicorn", mod)
    return made


def _serve_env(monkeypatch, app=None, serve_impl=None):
    """Everything _cmd_serve reaches outside itself: uvicorn, the app factory,
    the Windows job object, and the stale-tool reaper."""
    made = _fake_uvicorn(monkeypatch, serve_impl)
    app = app if app is not None else _FakeApp()
    made["app"] = app
    made["create_app_calls"] = []

    def _create_app(settings):
        made["create_app_calls"].append(settings)
        return app

    reaped = []

    async def _kill_stale_tools(names):
        reaped.append(list(names))
        return []

    monkeypatch.setattr("blurayripper.web.app.create_app", _create_app)
    monkeypatch.setattr("blurayripper.tools.winjob.enable_kill_on_close", lambda: None)
    monkeypatch.setattr("blurayripper.tools.process.kill_stale_tools", _kill_stale_tools)
    made["reaped"] = reaped
    made["keys"] = _fake_keypress(monkeypatch)
    return made


def _run_serve(host=None, port=None):
    return asyncio.run(_cmd_serve(argparse.Namespace(config=None, host=host, port=port)))


def test_serve_without_the_web_extra_fails_with_an_explanation(tmp_path, monkeypatch, capsys):
    """uvicorn is an optional extra, so a plain `pip install -e .` box hits this.
    A bare ImportError traceback would not say that the *web* deps are what is
    missing, or that starting the server is what needed them.

    (The install command in that message is currently mangled — see the bug
    report; this pins the part that does work: it fails, and it says why.)"""
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    _use_settings(monkeypatch, _settings(tmp_path))

    rc = _run_serve()

    out = _prose(capsys.readouterr().out)
    assert rc == 2
    assert "Web UI needs extra deps" in out and "pip install" in out


def test_serve_applies_the_cluster_preset_before_it_builds_the_app(
        tmp_path, monkeypatch, capsys):
    """Same drift as the worker, from the other end: a rebuilt serve box would
    otherwise run and hand out a different preset than the workers it feeds.
    The reload matters as much as the write — create_app captures the settings,
    so a stale object here is stale for the whole process lifetime."""
    store = Durable()
    store.set_shared_config(
        handbrake_store.SHARED_KEY, {"PresetList": [{"PresetName": "ClusterPreset"}]})
    monkeypatch.setattr("blurayripper.persistence.make_store", lambda url: store)

    made = _serve_env(monkeypatch)
    before = _settings(tmp_path, database_url="postgresql://db/brr")
    after = _settings(tmp_path, database_url="postgresql://db/brr")
    _use_settings(monkeypatch, before, after)

    rc = _run_serve()

    assert rc == 0
    assert (tmp_path / handbrake_store.LOCAL_PRESET_NAME).exists()
    assert made["create_app_calls"] == [after], "the app was built on pre-pull settings"
    assert "shared HandBrake preset" in _prose(capsys.readouterr().out)


def test_serve_stops_before_starting_when_a_storage_root_is_unreachable(
        tmp_path, monkeypatch, capsys):
    """The output root is usually a mapped NAS drive. Left to fail later this
    surfaces as a bare WinError 3 from a mkdir deep inside a rip; here it names
    the folder, the drive, and the three things that actually cause it."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    settings = _settings(tmp_path)
    settings.storage.output_dir = blocker / "out"
    _use_settings(monkeypatch, settings)
    made = _serve_env(monkeypatch)

    rc = _run_serve()

    out = capsys.readouterr().out
    assert rc == 2
    assert "Cannot access output_dir" in _prose(out)
    assert str(blocker / "out") in _unwrapped(out), "the failing folder must be named"
    # Platform-appropriate: this suite runs on macOS/Linux, so the POSIX remedy
    # is the correct one to print. _storage_remedy covers both wordings.
    assert "findmnt" in _prose(out), "the message has to say what to check"
    assert "server" not in made, "uvicorn must never be started"
    assert made["create_app_calls"] == []


def test_serve_lets_the_command_line_override_the_configured_host_and_port(
        tmp_path, monkeypatch, capsys):
    """`serve --port` is how you run a second instance or dodge a port clash
    without editing the shared settings that every other box reads."""
    settings = _settings(tmp_path)
    settings.web_host, settings.web_port = "127.0.0.1", 8723

    _use_settings(monkeypatch, settings)
    configured = _serve_env(monkeypatch)
    _run_serve()

    _use_settings(monkeypatch, settings)
    overridden = _serve_env(monkeypatch)
    _run_serve(host="10.0.0.5", port=9999)

    assert (configured["config"].host, configured["config"].port) == ("127.0.0.1", 8723)
    assert (overridden["config"].host, overridden["config"].port) == ("10.0.0.5", 9999)


def test_serve_warns_about_the_open_lan_bind_only_when_it_is_open(
        tmp_path, monkeypatch, capsys):
    """There is no login on this UI, so 0.0.0.0 exposes the whole queue to the
    LAN. Printing the warning unconditionally would train everyone to skip it."""
    _use_settings(monkeypatch, _settings(tmp_path))
    _serve_env(monkeypatch)
    _run_serve(host="0.0.0.0")
    open_out = _prose(capsys.readouterr().out)

    _use_settings(monkeypatch, _settings(tmp_path))
    _serve_env(monkeypatch)
    _run_serve(host="127.0.0.1")
    local_out = _prose(capsys.readouterr().out)

    assert "trusted network" in open_out and "all interfaces" in open_out
    assert "trusted network" not in local_out


def test_serve_reaps_only_the_tools_it_will_run_itself(tmp_path, monkeypatch, capsys):
    """Reaping is by process NAME, so a worker running on the same machine would
    have its live encode and OCR killed every time serve restarts. Which tools
    serve owns depends entirely on how the cluster is split."""
    def _configured(**cluster):
        s = _settings(tmp_path, cleanup_stale_tools=True)
        s.tools.makemkvcon = "/tools/makemkvcon"
        s.tools.handbrakecli = "/tools/HandBrakeCLI"
        s.tools.subtitleedit = "/tools/seconv"
        s.tools.mkvextract = "/tools/mkvextract"
        for key, value in cluster.items():
            setattr(s.cluster, key, value)
        return s

    _use_settings(monkeypatch, _configured(
        use_workers=True, local_encode_max_height=0, worker_does_subs=True))
    delegated = _serve_env(monkeypatch)
    _run_serve()

    _use_settings(monkeypatch, _configured(
        use_workers=False, local_encode_max_height=0, worker_does_subs=False))
    standalone = _serve_env(monkeypatch)
    _run_serve()

    assert delegated["reaped"] == [["makemkvcon"]]
    assert standalone["reaped"] == [
        ["HandBrakeCLI", "makemkvcon", "mkvextract", "seconv"]]


def test_a_serve_that_cannot_start_does_not_reap_anything(tmp_path, monkeypatch, capsys):
    """A rip box whose NAS was unreachable crash-looped on 10-second restarts,
    and the stale-tool sweep ran BEFORE the pre-flight that aborted it. So every
    10 seconds something SIGKILLed every makemkvcon on the machine — including
    the operator's own `makemkvcon info disc:1`, run by hand to find out why a
    drive wasn't reading. Forty-two restarts of a diagnostic dying instantly for
    no visible reason.

    A process that is about to exit 2 has no business reaping other people's."""
    s = _settings(tmp_path, cleanup_stale_tools=True)
    s.tools.makemkvcon = "/tools/makemkvcon"   # the one it would have SIGKILLed
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("the NAS mount point, with the share not mounted")
    s.storage.staging_dir = blocker / "MakeMKV" / "staging"
    _use_settings(monkeypatch, s)
    env = _serve_env(monkeypatch)

    rc = _run_serve()

    assert rc == 2
    assert env["reaped"] == [], "aborting serve still killed the box's tools"
    assert env["create_app_calls"] == []           # and never got as far as the app
    assert "Cannot access staging_dir" in _prose(capsys.readouterr().out)


def test_the_unreachable_storage_remedy_fits_the_os_it_prints_on():
    """The advice was Windows-only. A Linux box spent a night telling its
    operator to "run: net use" and to check they were not in an elevated prompt,
    for a CIFS share that was mounted and healthy but owned by root — which is
    the one cause the message never mentioned."""
    folder = Path("/mnt/TrueNas/share/MakeMKV/staging")

    posix = _storage_remedy(folder, windows=False)
    assert "net use" not in posix and "elevated" not in posix
    assert f"findmnt -T {folder}" in posix
    assert "uid=" in posix and "root" in posix     # names the actual cause

    windows = _storage_remedy(folder, windows=True)
    assert "net use" in windows and "elevated" in windows
    assert "findmnt" not in windows


def test_serve_closes_the_event_streams_the_moment_shutdown_begins(
        tmp_path, monkeypatch, capsys):
    """uvicorn waits for the open SSE connection before it runs the lifespan
    shutdown that would end it — a deadlock that hangs Ctrl+C for the full grace
    period and then logs a CancelledError. Flipping `closing` as soon as
    should_exit is set is what breaks the cycle."""
    closed = asyncio.Event()

    class _Bus:
        def publish(self):
            closed.set()

    class _Manager:
        def __init__(self):
            self.closing = False
            self.bus = _Bus()

    app = _FakeApp()
    manager = _Manager()
    app.state.manager = manager

    async def _serve_impl(server):
        server.should_exit = True                    # shutdown starts here
        await asyncio.wait_for(closed.wait(), 2)     # ... uvicorn is still holding on

    _use_settings(monkeypatch, _settings(tmp_path))
    _serve_env(monkeypatch, app=app, serve_impl=_serve_impl)

    rc = _run_serve()

    assert rc == 0
    assert manager.closing is True


def test_serve_reexecs_itself_for_a_web_triggered_restart(tmp_path, monkeypatch, capsys):
    """The UI's Restart button can only free the port by ending this process, so
    something has to bring it back. Handing the server object to the app is what
    lets the button reach it in the first place, and the console key reader has
    to be stopped before the re-exec or the shell is left in cbreak mode with no
    process left to restore it."""
    execs = []
    monkeypatch.setattr("os.execv", lambda exe, argv: execs.append((exe, argv)))
    monkeypatch.setattr("sys.argv", ["blurayripper", "serve", "--port", "8080"])

    app = _FakeApp()

    async def _serve_impl(server):
        app.state.restart_requested = True
        await asyncio.sleep(0)

    _use_settings(monkeypatch, _settings(tmp_path))
    made = _serve_env(monkeypatch, app=app, serve_impl=_serve_impl)

    _run_serve()

    assert app.state.uvicorn_server is made["server"]
    assert execs == [(sys.executable,
                      [sys.executable, "-m", "blurayripper", "serve", "--port", "8080"])]
    assert made["keys"][0].stopped, "the terminal was not restored before the re-exec"


def test_serve_pulls_first_when_the_ui_asked_to_update_rather_than_restart(
        tmp_path, monkeypatch, capsys):
    """"Update server" and "Restart server" are separate buttons. A restart that
    silently pulled would ship whatever is on main to a box someone restarted
    just to clear a stuck job."""
    calls = []
    monkeypatch.setattr("blurayripper.cli._pull_and_restart",
                        lambda console, pull=True: calls.append(pull))
    monkeypatch.setattr("os.execv",
                        lambda *a: pytest.fail("an update must not re-exec directly"))

    app = _FakeApp()

    async def _serve_impl(server):
        app.state.restart_requested = True
        app.state.restart_pull = True
        await asyncio.sleep(0)

    _use_settings(monkeypatch, _settings(tmp_path))
    _serve_env(monkeypatch, app=app, serve_impl=_serve_impl)

    rc = _run_serve()

    assert calls == [True]
    assert rc == 1, "reaching this line at all means the re-exec failed"


def test_serve_swallows_dropped_browser_connections_but_not_real_errors(
        tmp_path, monkeypatch, capsys):
    """A browser closing an SSE tab logs a WinError 10054 from a teardown
    callback on Windows. Left in, the console fills with tracebacks nobody reads
    — which is how an actual loop error gets lost in the noise."""
    captured = {}

    async def _serve_impl(server):
        captured["handler"] = asyncio.get_running_loop().get_exception_handler()
        await asyncio.sleep(0)

    _use_settings(monkeypatch, _settings(tmp_path))
    _serve_env(monkeypatch, serve_impl=_serve_impl)
    _run_serve()

    forwarded = []
    loop = types.SimpleNamespace(default_exception_handler=forwarded.append)
    handler = captured["handler"]
    assert handler is not None

    handler(loop, {"exception": ConnectionResetError(10054, "forcibly closed")})
    handler(loop, {"message": "socket.send() raised exception."})
    assert forwarded == [], "benign disconnect noise reached the console"

    real = {"exception": ValueError("boom"), "message": "Task exception was never retrieved"}
    handler(loop, real)
    assert forwarded == [real], "a real loop error was silenced"


# --------------------------------------------------------------------------
# main() and the argument parser
# --------------------------------------------------------------------------


def _stub_command(monkeypatch, result=0, raises=None):
    """Replace db-init's handler; _build_parser() reads the global when it runs,
    so the parser wires up the stub."""
    seen = []

    async def _handler(args):
        seen.append(args)
        if raises is not None:
            raise raises
        return result

    monkeypatch.setattr("blurayripper.cli._cmd_db_init", _handler)
    return seen


def test_main_hands_the_command_a_path_not_a_string(tmp_path, monkeypatch):
    """Every command does `settings.config_dir / "something"`. A str there fails
    with an unhelpful TypeError the moment --config is used, which is exactly
    when someone is pointing a box at an alternate config directory."""
    seen = _stub_command(monkeypatch)

    assert main(["--config", str(tmp_path), "db-init"]) == 0
    assert main(["db-init"]) == 0
    assert seen[0].config == Path(tmp_path) and isinstance(seen[0].config, Path)
    assert seen[1].config is None, "an absent --config must stay None, not Path('')"


def test_main_returns_the_subcommands_exit_code(monkeypatch):
    """Shell scripts and the Windows launcher branch on this; collapsing it to 0
    would make every failure look like a success."""
    _stub_command(monkeypatch, result=7)

    assert main(["db-init"]) == 7


def test_main_reports_a_missing_tool_instead_of_a_traceback(monkeypatch, capsys):
    """ToolNotFoundError already carries the path it looked for and how to fix
    it; a stack trace buries that under frames nobody needs."""
    _stub_command(monkeypatch, raises=ToolNotFoundError("HandBrakeCLI not found at Z:\\hb"))

    rc = main(["db-init"])

    out = _prose(capsys.readouterr().out)
    assert rc == 2
    assert "HandBrakeCLI not found at Z:\\hb" in out
    assert "Traceback" not in out


def test_main_treats_ctrl_c_as_an_interrupt_rather_than_a_crash(monkeypatch, capsys):
    """Ctrl+C is the normal way to stop `serve`. A KeyboardInterrupt traceback
    on every shutdown reads as a bug and hides the ones that are."""
    _stub_command(monkeypatch, raises=KeyboardInterrupt())

    rc = main(["db-init"])

    out = _prose(capsys.readouterr().out)
    assert rc == 130
    assert "Interrupted" in out
    assert "Traceback" not in out


def test_a_bare_invocation_is_rejected_rather_than_defaulting_to_something(capsys):
    """There is no safe default here — silently picking one would mean typing
    `blurayripper` alone could start a server or a worker."""
    with pytest.raises(SystemExit) as exc:
        main([])

    assert exc.value.code == 2
    assert "command" in capsys.readouterr().err


def test_each_cluster_subcommand_reaches_its_own_handler():
    """The dispatch table itself: a subcommand wired to the wrong handler (or to
    none) is the kind of mistake that only shows up in production."""
    from blurayripper import cli

    parser = _build_parser()
    for argv, func in (
        (["serve"], cli._cmd_serve),
        (["worker"], cli._cmd_worker),
        (["db-init"], cli._cmd_db_init),
        (["secrets", "status"], cli._cmd_secrets),
    ):
        assert parser.parse_args(argv).func is func, argv


def test_serve_and_worker_options_arrive_in_the_shape_the_commands_expect():
    """--port must be an int (uvicorn rejects a string) and the unset overrides
    must be None, because that is what makes the configured value win."""
    parser = _build_parser()

    explicit = parser.parse_args(["serve", "--host", "0.0.0.0", "--port", "9000"])
    default = parser.parse_args(["serve"])
    named = parser.parse_args(["worker", "--name", "encoder-2"])

    assert explicit.host == "0.0.0.0" and explicit.port == 9000
    assert isinstance(explicit.port, int)
    assert default.host is None and default.port is None
    assert named.name == "encoder-2"
    assert parser.parse_args(["worker"]).name is None


def test_secrets_rejects_an_action_it_does_not_have(capsys):
    """`secrets clean` (for `clear`) must not be silently accepted and then fall
    through to the status branch — the operator would believe they rotated a
    credential that is still sitting in the cluster."""
    parser = _build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["secrets", "clean"])

    err = capsys.readouterr().err
    assert exc.value.code == 2
    assert "clear" in err and "push" in err


def test_an_unreachable_storage_root_is_reported_not_waited_on(
        tmp_path, monkeypatch, capsys):
    """The roots are normally a network share mounted `hard`, where an
    unreachable server makes mkdir block in the kernel forever — no error, no
    timeout. This runs before serve prints anything, so the symptom is a server
    that never starts and a log that ends mid-restart saying nothing at all.
    That is exactly how a box looked after an update: pulled, re-exec'd, and
    then silence for forty minutes."""
    import threading

    from blurayripper import cli as cli_mod

    never = threading.Event()
    monkeypatch.setattr(cli_mod, "_make_storage_root", lambda folder: never.wait(30))
    monkeypatch.setattr(cli_mod, "STORAGE_PREFLIGHT_TIMEOUT", 0.1)
    _use_settings(monkeypatch, _settings(tmp_path))
    env = _serve_env(monkeypatch)

    rc = _run_serve()

    assert rc == 2
    out = _prose(capsys.readouterr().out)
    assert "did not respond within" in out
    assert "hard" in out and "NAS" in out
    assert env["create_app_calls"] == []       # never got as far as the app
    # ...and the thread it gave up on must not hold the process open.
    stuck = [t for t in threading.enumerate() if t.name.startswith("blurayripper-")]
    assert all(t.daemon for t in stuck), f"a hung NAS must not block exit: {stuck}"
    never.set()
