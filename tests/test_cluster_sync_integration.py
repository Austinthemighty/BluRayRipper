"""Shared config has to reach every box in the cluster, through Postgres.

Four different files are gitignored and therefore invisible to `git pull`: the
web-edited settings overlay, the box-set CSV, the HandBrake preset overlay, and
secrets.env. Each one is a way for a worker to quietly run a *different*
configuration than the server — wrong preset, unnamed discs, no TMDB key — with
nothing failing to say so. These tests drive TWO boxes (two config dirs, one
database) so the crossing is real, and lean hardest on the failure directions:
an outage must not revert a box to defaults, and an empty database must not let
a blank box wipe the one that has the config.
"""

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from blurayripper import boxset_plan, handbrake_store as hb
from blurayripper.config import _CLUSTER_SECRETS, _load_secrets, load_settings
from blurayripper.persistence import MemoryStore
from blurayripper.services import secretbox
from blurayripper.settings_store import (
    SHARED_KEY, read_overlay, save_overlay, sync_from_cluster as sync_settings,
)
from blurayripper.worker import WorkerRunner

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"
DB_URL = "postgresql://db/brr"
SECRET_ENV_VARS = (
    "TMDB_API_KEY", "OPENSUBTITLES_API_KEY", "OPENSUBTITLES_USERNAME",
    "OPENSUBTITLES_PASSWORD", "BLURAYRIPPER_DATABASE_URL",
    "BLURAYRIPPER_HANDBRAKECLI", "BLURAYRIPPER_NOTIFY_URL",
    secretbox.PASSPHRASE_KEY,
)


class SharedPostgres(MemoryStore):
    """The one database both boxes can reach."""

    @property
    def durable(self): return True


class Unreachable(SharedPostgres):
    """Postgres is down (it lives on a third machine that can be rebooting)."""

    def get_shared_config(self, key):
        raise ConnectionError("no route to host")


def _box(tmp_path, name):
    """A box's config dir — the per-machine home of every gitignored file."""
    path = tmp_path / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _settings_for(config_dir):
    """boxset_plan and the worker's preset sync only ever read .config_dir."""
    return SimpleNamespace(config_dir=config_dir)


def _write_secrets(config_dir, **values):
    lines = [f"BLURAYRIPPER_DATABASE_URL={DB_URL}"]
    lines += [f"{k}={v}" for k, v in values.items()]
    (config_dir / "secrets.env").write_text("\n".join(lines) + "\n", encoding="utf-8")


PLAN_CSV = ("disc_label,show,year,season,first_episode,episodes\n"
            "THE_OFFICE_S3_D2,The Office,2005,3,9,4\n")


@pytest.fixture
def clean_secrets_env(monkeypatch):
    """Each box is its own process in production: no decrypted-secrets cache
    carries between them, and no ambient env var stands in for secrets.env."""
    _CLUSTER_SECRETS.clear()
    for var in SECRET_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield
    _CLUSTER_SECRETS.clear()


# --- a value saved on box A reaching box B ---------------------------------

def test_a_preset_typed_on_the_server_is_what_the_worker_actually_boots_with(tmp_path):
    """The overlay is the whole Settings page. A worker left on its own stale
    copy encodes at a different quality than everything else in the library and
    nothing anywhere reports a problem."""
    store = SharedPostgres()
    server, worker = _box(tmp_path, "serve"), _box(tmp_path, "worker")
    save_overlay(worker, {"encode": {"ffmpeg_preset": "veryfast"}})  # stale, local only

    save_overlay(server, {"encode": {"ffmpeg_preset": "slower"},
                          "queue": {"encode_capacity": 7}}, store)
    note = sync_settings(worker, store)

    assert "pulled" in note
    running = load_settings(worker)          # what this box would encode with
    assert running.encode.ffmpeg_preset == "slower"
    assert running.queue.encode_capacity == 7


def test_an_edit_on_the_worker_does_not_drop_what_the_server_had_saved(tmp_path):
    """Settings are published as the whole merged overlay, not as the diff that
    was typed. Publishing only the edited field would make every save on one box
    silently delete every field the other box had set."""
    store = SharedPostgres()
    server, worker = _box(tmp_path, "serve"), _box(tmp_path, "worker")
    save_overlay(server, {"encode": {"ffmpeg_preset": "slower"}}, store)
    sync_settings(worker, store)                       # worker caches the server's value

    save_overlay(worker, {"queue": {"encode_capacity": 5}}, store)
    sync_settings(server, store)

    assert read_overlay(server) == {"encode": {"ffmpeg_preset": "slower"},
                                    "queue": {"encode_capacity": 5}}


def test_a_worker_holding_last_seasons_plan_gets_the_current_one(tmp_path):
    """A stale plan is worse than no plan: the disc in the drive matches nothing,
    so it parks for manual naming and looks like the matcher misbehaving."""
    store = SharedPostgres()
    server = _settings_for(_box(tmp_path, "serve"))
    worker = _settings_for(_box(tmp_path, "worker"))
    boxset_plan.save_plan(worker, "disc_label,show\nOLD_S1_D1,Old Show\n")

    boxset_plan.save_plan(server, PLAN_CSV, store)
    note = boxset_plan.sync_from_cluster(worker, store)

    assert "pulled 1 box-set row" in note
    rows = boxset_plan.load_rows(worker)
    hit = boxset_plan.match(rows, "THE_OFFICE_S3_D2")
    assert hit is not None and hit.show == "The Office" and hit.season == 3
    # replaced, not merged — the superseded row must stop claiming discs
    assert boxset_plan.match(rows, "OLD_S1_D1") is None


def test_a_handbrake_edit_on_the_server_is_the_preset_the_worker_encodes_with(tmp_path):
    """Every box has its own checkout of presets.json, so without sharing, two
    workers encoding the same season produce visibly different files."""
    store = SharedPostgres()
    server, worker = _box(tmp_path, "serve"), _box(tmp_path, "worker")
    shutil.copy(REPO_CONFIG / "presets.json", server / "presets.json")
    preset_name = json.loads(
        (server / "presets.json").read_text())["PresetList"][0]["PresetName"]
    # This worker diverged: a different encoder and quality in its own overlay.
    diverged = json.dumps({"PresetList": [
        {"PresetName": preset_name, "VideoEncoder": "x264",
         "VideoQualitySlider": 30, "VideoPreset": "veryfast"}]})
    (worker / "presets.json").write_text(diverged)
    (worker / hb.LOCAL_PRESET_NAME).write_text(diverged)
    (worker / "settings.toml").write_text(
        "[encode]\n"
        f'preset_file = "{(worker / "presets.json").as_posix()}"\n'
        f'preset_name = "{preset_name}"\n')

    data = hb.save_handbrake(server / "presets.json", preset_name,
                             {"VideoQualitySlider": "23", "VideoPreset": "fast"})
    store.set_shared_config(hb.SHARED_KEY, data)       # what POST /api/handbrake does

    assert hb.apply_shared_preset(worker, store.get_shared_config(hb.SHARED_KEY)) is True
    running = load_settings(worker)
    assert running.encode.preset_file.name == "presets.local.json"
    values = {f["key"]: f["value"]
              for f in hb.handbrake_ui_payload(running.encode.preset_file,
                                               running.encode.preset_name)["fields"]}
    assert values["VideoQualitySlider"] == 23 and values["VideoPreset"] == "fast"
    # The whole preset travels, not just the edited keys: the encoder is the
    # server's, which this test never typed anywhere.
    assert values["VideoEncoder"] == "x265_10bit"


def test_a_worker_already_on_the_clusters_preset_does_not_keep_rehashing_it(tmp_path):
    """The worker rebuilds its pipeline whenever this reports a change, so a
    sync that always claims one would rebuild between every single job for a file
    that never moved — while a real change still has to be noticed."""
    store = SharedPostgres()
    settings = _settings_for(_box(tmp_path, "worker"))
    runner = SimpleNamespace(store=store, settings=settings)
    store.set_shared_config(hb.SHARED_KEY, {"PresetList": [
        {"PresetName": "Plex H265 1080p", "VideoQualitySlider": 21}]})

    assert WorkerRunner._sync_shared_preset(runner) is True    # first poll: adopt it
    assert WorkerRunner._sync_shared_preset(runner) is False   # unchanged: no churn

    store.set_shared_config(hb.SHARED_KEY, {"PresetList": [
        {"PresetName": "Plex H265 1080p", "VideoQualitySlider": 19}]})
    assert WorkerRunner._sync_shared_preset(runner) is True    # a real edit still lands


def test_credentials_pushed_from_the_server_reach_a_worker_that_never_had_them(
        tmp_path, monkeypatch, clean_secrets_env):
    """API keys were the thing every migration lost. They now ride in Postgres
    encrypted — but only the shareable ones: the server's Windows tool paths and
    its mapped drive would break a Linux worker that adopted them."""
    store = SharedPostgres()
    server, worker = _box(tmp_path, "serve"), _box(tmp_path, "worker")
    _write_secrets(server, **{secretbox.PASSPHRASE_KEY: "cluster-pw",
                              "TMDB_API_KEY": "server-tmdb",
                              "OPENSUBTITLES_API_KEY": "server-os",
                              "BLURAYRIPPER_HANDBRAKECLI": r"C:\Tools\HandBrakeCLI.exe",
                              "BLURAYRIPPER_STAGING_DIR": "Z:/staging"})
    _write_secrets(worker, **{secretbox.PASSPHRASE_KEY: "cluster-pw",
                              "OPENSUBTITLES_API_KEY": "worker-os"})
    monkeypatch.setattr("blurayripper.persistence.make_store", lambda url: store)

    raw = _load_secrets(server)                        # `blurayripper secrets push`
    secretbox.publish(store, raw[secretbox.PASSPHRASE_KEY], secretbox.movable(raw))
    _CLUSTER_SECRETS.clear()                           # the worker is another process

    running = load_settings(worker)
    assert running.tmdb_api_key == "server-tmdb"           # gap filled from the cluster
    assert running.opensubtitles_api_key == "worker-os"    # a local value still wins
    # per-box values stay put even though the worker has none of its own
    assert running.tools.handbrakecli != r"C:\Tools\HandBrakeCLI.exe"
    assert "Z:" not in str(running.storage.staging_dir)
    assert "server-tmdb" not in json.dumps(store.get_shared_config(secretbox.SHARED_KEY))


# --- what an outage must NOT do --------------------------------------------

def test_a_database_outage_never_clears_a_boxs_cached_cluster_config(tmp_path):
    """Postgres owning the config must not mean Postgres being down erases it.
    Every local file here is a CACHE — a box whose database is unreachable has to
    come up on its last known config, not on settings.toml's defaults."""
    box = _box(tmp_path, "worker")
    settings = _settings_for(box)
    save_overlay(box, {"encode": {"ffmpeg_preset": "slower"}})
    boxset_plan.save_plan(settings, PLAN_CSV)
    hb.apply_shared_preset(box, {"PresetList": [{"PresetName": "X",
                                                 "VideoQualitySlider": 21}]})
    cached = {p.name: p.read_text() for p in box.iterdir()}
    store = Unreachable()

    notes = [sync_settings(box, store), boxset_plan.sync_from_cluster(settings, store)]
    # the worker's preset sync is the one that reads the store itself
    preset_changed = WorkerRunner._sync_shared_preset(
        SimpleNamespace(store=store, settings=settings))

    assert all("unreachable" in n for n in notes)
    assert preset_changed is False
    assert {p.name: p.read_text() for p in box.iterdir()} == cached
    assert load_settings(box).encode.ffmpeg_preset == "slower"


def test_one_unreadable_key_does_not_take_the_other_shared_config_down(tmp_path):
    """The syncs are independent. A single failing read must not cost a box the
    settings AND the plan AND the preset in one go."""
    store = SharedPostgres()
    server = _settings_for(_box(tmp_path, "serve"))
    boxset_plan.save_plan(server, PLAN_CSV, store)
    save_overlay(server.config_dir, {"encode": {"ffmpeg_preset": "slower"}}, store)

    class OnlySettingsBroken(SharedPostgres):
        def get_shared_config(self, key):
            if key == SHARED_KEY:
                raise ConnectionError("no route to host")
            return super().get_shared_config(key)

    half = OnlySettingsBroken()
    half.set_shared_config(boxset_plan.SHARED_KEY,
                           store.get_shared_config(boxset_plan.SHARED_KEY))
    worker = _settings_for(_box(tmp_path, "worker"))

    assert "unreachable" in sync_settings(worker.config_dir, half)
    assert "pulled 1 box-set row" in boxset_plan.sync_from_cluster(worker, half)
    assert boxset_plan.load_plan(worker) == PLAN_CSV


def test_an_unreachable_database_leaves_a_box_on_its_own_credentials(
        tmp_path, monkeypatch, clean_secrets_env):
    """An outage must not stop a box working. Its own secrets.env is still there,
    so it runs on that rather than refusing to start."""
    box = _box(tmp_path, "worker")
    _write_secrets(box, **{secretbox.PASSPHRASE_KEY: "cluster-pw",
                           "TMDB_API_KEY": "worker-tmdb"})
    attempts = []

    def boom(url):
        attempts.append(url)
        raise ConnectionError("no route to host")

    monkeypatch.setattr("blurayripper.persistence.make_store", boom)

    running = load_settings(box)

    assert attempts, "the cluster lookup never happened, so nothing was tolerated"
    assert running.tmdb_api_key == "worker-tmdb"


def test_a_worker_with_a_mismatched_passphrase_says_so_instead_of_running_blind(
        tmp_path, monkeypatch, clean_secrets_env):
    """Treating an un-openable blob as 'no credentials' is indistinguishable from
    a correctly configured cluster with none stored — the typo would surface
    weeks later as artwork quietly not downloading."""
    store = SharedPostgres()
    secretbox.publish(store, "cluster-pw", {"TMDB_API_KEY": "server-tmdb"})
    box = _box(tmp_path, "worker")
    _write_secrets(box, **{secretbox.PASSPHRASE_KEY: "typo-pw"})
    monkeypatch.setattr("blurayripper.persistence.make_store", lambda url: store)

    with pytest.raises(secretbox.SecretsUnavailable, match="identical everywhere"):
        load_settings(box)


# --- the first run against an empty database -------------------------------

def test_a_blank_worker_syncing_first_does_not_wipe_the_servers_settings(tmp_path):
    """Turning on shared config: whichever box starts first wins the race to the
    empty database. A fresh worker starting first must publish nothing, or the
    server would pull that emptiness back over the config it is running on."""
    store = SharedPostgres()
    server, worker = _box(tmp_path, "serve"), _box(tmp_path, "worker")
    save_overlay(server, {"encode": {"ffmpeg_preset": "slower"}})  # local only, not published

    assert "nothing stored" in sync_settings(worker, store)
    assert store.get_shared_config(SHARED_KEY) is None

    assert "seeded" in sync_settings(server, store)
    assert "pulled" in sync_settings(worker, store)
    assert read_overlay(server) == {"encode": {"ffmpeg_preset": "slower"}}
    assert read_overlay(worker) == read_overlay(server)


def test_a_worker_with_an_empty_plan_file_does_not_erase_the_servers_plan(tmp_path):
    """Same race for the box-set CSV, with the extra trap that a worker's plan
    file can exist and be blank. Seeding that blankness would silently stop every
    planned disc from auto-naming, cluster-wide."""
    store = SharedPostgres()
    server = _settings_for(_box(tmp_path, "serve"))
    worker = _settings_for(_box(tmp_path, "worker"))
    (worker.config_dir / boxset_plan.PLAN_FILENAME).write_text("\n")
    boxset_plan.save_plan(server, PLAN_CSV)                # local only, not published

    assert "nothing stored" in boxset_plan.sync_from_cluster(worker, store)
    assert store.get_shared_config(boxset_plan.SHARED_KEY) is None

    assert "seeded" in boxset_plan.sync_from_cluster(server, store)
    assert boxset_plan.load_plan(server) == PLAN_CSV
    assert "pulled" in boxset_plan.sync_from_cluster(worker, store)
    assert boxset_plan.load_plan(worker) == PLAN_CSV
