"""Configuration precedence, end to end, with real files on disk.

Lowest to highest: config/settings.toml, the web-edited settings.local.json
overlay, a per-worker overlay from Postgres, then config/secrets.env and real
process env vars.

This is written as integration rather than unit tests because the layers only
break in combination: the value a box actually encodes with is the product of
four files, two of which are gitignored and one of which lives on another
machine. That mixture is what once let a migration to a new box silently revert
an encode preset to settings.toml's default, with nothing failing and nobody
noticing for days.
"""

import os

import pytest

from blurayripper import settings_store as ss
from blurayripper.config import _CLUSTER_SECRETS, load_settings
from blurayripper.persistence import MemoryStore
from blurayripper.services import secretbox


class Durable(MemoryStore):
    """A shared store, as the cluster has: Postgres reachable from every box."""

    @property
    def durable(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch):
    """Real BLURAYRIPPER_*/API-key env vars would silently outrank every file
    these tests write, so drop them; and the decrypted-secrets cache is process
    wide, so it must not leak between tests."""
    for key in list(os.environ):
        if key.startswith(("BLURAYRIPPER_", "OPENSUBTITLES_")) or key == "TMDB_API_KEY":
            monkeypatch.delenv(key, raising=False)
    _CLUSTER_SECRETS.clear()
    yield
    _CLUSTER_SECRETS.clear()


def _write(path, text):
    path.write_text(text, encoding="utf-8")


def _toml(cfg_dir, text):
    _write(cfg_dir / "settings.toml", text)


def _secrets(cfg_dir, text):
    _write(cfg_dir / "secrets.env", text)


# --------------------------------------------------------------------------
# The layer stack
# --------------------------------------------------------------------------

def test_each_layer_in_turn_wins_over_the_one_below_it(tmp_path):
    """One key, carried up all four layers, adding exactly one layer at a time.

    Every layer here is edited by a different person or machine — the repo, the
    web UI, the cluster's per-worker page, and the box's own secrets.env — so an
    inversion anywhere in the order means one of them is quietly ignored.
    """
    _toml(tmp_path, "[cluster]\nlocal_encode_max_height = 720\n")
    assert load_settings(tmp_path).cluster.local_encode_max_height == 720

    # 2. the web-edited overlay, written through the real save path.
    ss.save_overlay(tmp_path, {"cluster": {"local_encode_max_height": "1080"}})
    assert load_settings(tmp_path).cluster.local_encode_max_height == 1080

    # 3. a per-worker overlay held in the shared store.
    store = Durable()
    ss.save_settings(tmp_path, {"cluster": {"local_encode_max_height": "2160"}},
                     scope="w1", store=store)
    worker_overlay = store.get_worker_settings("w1")
    assert load_settings(tmp_path, extra_overlay=worker_overlay
                         ).cluster.local_encode_max_height == 2160
    # ...and the same config dir, read WITHOUT that worker's overlay, is untouched.
    assert load_settings(tmp_path).cluster.local_encode_max_height == 1080

    # 4. this box's secrets.env beats everything, worker overlay included.
    _secrets(tmp_path, "BLURAYRIPPER_LOCAL_ENCODE_MAX_HEIGHT=480\n")
    assert load_settings(tmp_path, extra_overlay=worker_overlay
                         ).cluster.local_encode_max_height == 480


def test_a_process_env_var_outranks_the_same_key_in_secrets_env(tmp_path, monkeypatch):
    """The service wrapper on the Windows VM passes settings in as env vars; if
    the file won, a stale secrets.env on that box would beat the deployment."""
    _secrets(tmp_path, "BLURAYRIPPER_NOTIFY_URL=https://ntfy.example/from-file\n"
                       "BLURAYRIPPER_NOTIFY_PRIORITY=urgent\n")
    monkeypatch.setenv("BLURAYRIPPER_NOTIFY_URL", "https://ntfy.example/from-env")

    s = load_settings(tmp_path)

    assert s.notify_url == "https://ntfy.example/from-env"
    # The file was still read: its other key survives, so this is genuinely the
    # env var winning a collision rather than secrets.env being skipped.
    assert s.notify_priority == "urgent"


def test_a_worker_override_of_one_key_keeps_the_rest_of_the_overlay_section(tmp_path):
    """Overlays merge key by key. If a section were replaced wholesale, giving
    one worker a different CRF would also silently reset its preset and grain
    tuning back to the tracked TOML — an encode that looks fine and isn't."""
    _toml(tmp_path, '[encode]\ncontainer = "mkv"\nffmpeg_crf = 18\n')
    ss.save_overlay(tmp_path, {"encode": {"ffmpeg_preset": "slower", "tune_grain": "true"}})

    s = load_settings(tmp_path, extra_overlay={"encode": {"ffmpeg_crf": 20}})

    assert s.encode.ffmpeg_crf == 20            # the worker's own override
    assert s.encode.ffmpeg_preset == "slower"   # other overlay keys survive it
    assert s.encode.tune_grain is True
    assert s.encode.container == "mkv"          # and the TOML under both survives


# --------------------------------------------------------------------------
# Blank means "unset", not "empty"
# --------------------------------------------------------------------------

def test_a_blank_boolean_in_secrets_env_means_unset_rather_than_false(tmp_path):
    """secrets.env values are strings, and "false" is a truthy string. A commented
    -out-then-blanked line must fall through to the layer below instead of
    turning a feature off cluster-wide."""
    _toml(tmp_path, "[cluster]\nworker_does_subs = false\n")
    ss.save_overlay(tmp_path, {"cluster": {"worker_does_subs": "true"}})

    _secrets(tmp_path, "BLURAYRIPPER_WORKER_DOES_SUBS=\n")
    assert load_settings(tmp_path).cluster.worker_does_subs is True  # falls through

    _secrets(tmp_path, "BLURAYRIPPER_WORKER_DOES_SUBS=false\n")
    assert load_settings(tmp_path).cluster.worker_does_subs is False  # parsed, not truthy


def test_a_blank_string_in_secrets_env_does_not_erase_the_configured_value(tmp_path):
    """Same rule for plain strings: an empty BLURAYRIPPER_NOTIFY_URL must not
    become notify_url="" and turn every alert off without saying so."""
    ss.save_overlay(tmp_path, {"serve": {"notify_url": "https://ntfy.example/topic"}})

    _secrets(tmp_path, "BLURAYRIPPER_NOTIFY_URL=\n")
    assert load_settings(tmp_path).notify_url == "https://ntfy.example/topic"

    _secrets(tmp_path, "BLURAYRIPPER_NOTIFY_URL=https://ntfy.example/thisbox\n")
    assert load_settings(tmp_path).notify_url == "https://ntfy.example/thisbox"


# --------------------------------------------------------------------------
# Per-worker overlays
# --------------------------------------------------------------------------

def test_a_per_worker_override_reaches_only_that_worker(tmp_path):
    """Tuning the fast encoder must not re-tune the cluster. A per-worker save
    that leaked into settings.local.json would push one box's preset to every
    box, and to the server, on their next restart."""
    _toml(tmp_path, '[encode]\nffmpeg_preset = "medium"\nffmpeg_crf = 18\n')
    store = Durable()

    ss.save_settings(tmp_path, {"encode": {"ffmpeg_preset": "veryslow"}},
                     scope="fastbox", store=store)

    tuned = load_settings(tmp_path, extra_overlay=store.get_worker_settings("fastbox"))
    other = load_settings(tmp_path,
                          extra_overlay=store.get_worker_settings("slowbox") or None)
    assert tuned.encode.ffmpeg_preset == "veryslow"
    assert other.encode.ffmpeg_preset == "medium"
    assert list(store.all_worker_settings()) == ["fastbox"]
    # The global, shared-by-everyone overlay file was never written.
    assert not (tmp_path / ss.OVERLAY_NAME).exists()
    assert load_settings(tmp_path).encode.ffmpeg_preset == "medium"


def test_a_worker_left_matching_the_global_setting_keeps_following_it(tmp_path):
    """A worker override is only recorded when it differs from the CURRENT
    effective global config — overlay included, not just settings.toml. Recording
    a redundant one would freeze that box at today's value, so a later
    cluster-wide preset change would quietly skip it."""
    _toml(tmp_path, '[encode]\nffmpeg_preset = "medium"\n')
    ss.save_overlay(tmp_path, {"encode": {"ffmpeg_preset": "slower"}})
    store = Durable()

    # Saving the value it already has must not pin the worker to it.
    ss.save_settings(tmp_path, {"encode": {"ffmpeg_preset": "slower"}},
                     scope="w1", store=store)
    assert store.get_worker_settings("w1") == {}

    ss.save_overlay(tmp_path, {"encode": {"ffmpeg_preset": "veryslow"}})
    s = load_settings(tmp_path, extra_overlay=store.get_worker_settings("w1") or None)
    assert s.encode.ffmpeg_preset == "veryslow"


def test_a_fresh_box_pulls_the_clusters_overlay_before_it_encodes_anything(tmp_path):
    """The migration that started all this: settings.local.json is gitignored, so
    a new machine has none and comes up on settings.toml's defaults. Syncing from
    Postgres has to land in the file that load_settings actually reads."""
    _toml(tmp_path, '[encode]\npreset_name = "Plex H265 1080p"\nffmpeg_crf = 18\n')
    store = Durable()
    store.set_shared_config(ss.SHARED_KEY, {
        "encode": {"preset_name": "Plex H265 1080p Grain", "ffmpeg_crf": 16}})

    before = load_settings(tmp_path)
    assert before.encode.preset_name == "Plex H265 1080p" and before.encode.ffmpeg_crf == 18

    note = ss.sync_from_cluster(tmp_path, store)  # the one thing that changes

    after = load_settings(tmp_path)
    assert "pulled" in note
    assert after.encode.preset_name == "Plex H265 1080p Grain"
    assert after.encode.ffmpeg_crf == 16


# --------------------------------------------------------------------------
# Cluster secrets (encrypted, shared through Postgres)
# --------------------------------------------------------------------------

def _cluster(monkeypatch, values, passphrase="pw"):
    """Publish `values` to a shared store and point make_store at it."""
    store = Durable()
    secretbox.publish(store, passphrase, values)
    monkeypatch.setattr("blurayripper.persistence.make_store", lambda url: store)
    return store


def test_cluster_credentials_fill_gaps_but_never_replace_a_local_one(tmp_path, monkeypatch):
    """Pointing a box at the cluster must not change a credential it already had
    — that box may deliberately run its own key — while the ones it is missing
    should arrive rather than staying silently unset."""
    _cluster(monkeypatch, {
        "TMDB_API_KEY": "tmdb-from-cluster",
        "OPENSUBTITLES_API_KEY": "os-from-cluster",
        "BLURAYRIPPER_NOTIFY_URL": "https://ntfy.example/cluster",
    })
    ss.save_overlay(tmp_path, {"serve": {"notify_url": "https://ntfy.example/overlay"}})
    _secrets(tmp_path, "BLURAYRIPPER_DATABASE_URL=postgresql://db/one\n"
                       f"{secretbox.PASSPHRASE_KEY}=pw\n"
                       "TMDB_API_KEY=tmdb-local\n")

    s = load_settings(tmp_path)

    assert s.tmdb_api_key == "tmdb-local"                 # local value untouched
    assert s.opensubtitles_api_key == "os-from-cluster"   # gap filled
    # A cluster credential joins the secrets layer, so it outranks the web overlay.
    assert s.notify_url == "https://ntfy.example/cluster"
    assert s.database_url == "postgresql://db/one"


def test_a_box_with_no_passphrase_never_reaches_for_the_cluster_database(tmp_path, monkeypatch):
    """Installs that never opted in must stay on exactly the path they were on:
    no store built, no query, no new failure mode — and the same config with the
    passphrase added proves the store really is reachable in the second half."""
    store = Durable()
    secretbox.publish(store, "pw", {"OPENSUBTITLES_API_KEY": "os-from-cluster"})
    asked: list[str] = []

    def spy(url):
        asked.append(url)
        return store

    monkeypatch.setattr("blurayripper.persistence.make_store", spy)
    _secrets(tmp_path, "BLURAYRIPPER_DATABASE_URL=postgresql://db/two\n")

    s = load_settings(tmp_path)
    assert s.opensubtitles_api_key is None
    assert asked == []

    # One thing changes: the passphrase appears in secrets.env.
    _secrets(tmp_path, "BLURAYRIPPER_DATABASE_URL=postgresql://db/two\n"
                       f"{secretbox.PASSPHRASE_KEY}=pw\n")
    s = load_settings(tmp_path)
    assert s.opensubtitles_api_key == "os-from-cluster"
    assert asked == ["postgresql://db/two"]


def test_a_passphrase_without_a_database_url_is_inert(tmp_path, monkeypatch):
    """A single-box install that copied the passphrase line in has nowhere to
    fetch from; it must load normally rather than opening a store on an empty
    connection string."""
    asked: list[str] = []

    def spy(url):
        asked.append(url)
        return Durable()

    monkeypatch.setattr("blurayripper.persistence.make_store", spy)
    _secrets(tmp_path, f"{secretbox.PASSPHRASE_KEY}=pw\nTMDB_API_KEY=tmdb-local\n")

    s = load_settings(tmp_path)

    assert asked == []
    assert s.tmdb_api_key == "tmdb-local"
    assert s.database_url is None


def test_a_wrong_passphrase_stops_the_box_instead_of_booting_without_credentials(
        tmp_path, monkeypatch):
    """Degrading to "no credentials" is indistinguishable from a working cluster
    with none stored, so a mistyped passphrase would surface weeks later as
    artwork quietly not downloading. It has to fail at load."""
    _cluster(monkeypatch, {"TMDB_API_KEY": "tmdb-from-cluster"}, passphrase="right-pw")
    base = "BLURAYRIPPER_DATABASE_URL=postgresql://db/three\n"
    _secrets(tmp_path, base + f"{secretbox.PASSPHRASE_KEY}=wrong-pw\n")

    with pytest.raises(secretbox.SecretsUnavailable):
        load_settings(tmp_path)

    # Only the passphrase differs, so the raise is attributable to it.
    _CLUSTER_SECRETS.clear()
    _secrets(tmp_path, base + f"{secretbox.PASSPHRASE_KEY}=right-pw\n")
    assert load_settings(tmp_path).tmdb_api_key == "tmdb-from-cluster"


def test_an_unreachable_database_still_lets_the_box_boot_on_its_own_secrets(
        tmp_path, monkeypatch):
    """Postgres holding the shared credentials must not make Postgres being down
    a hard stop — the rip box keeps working on whatever secrets.env gives it."""
    def unreachable(url):
        raise ConnectionError("no route to host")

    monkeypatch.setattr("blurayripper.persistence.make_store", unreachable)
    _secrets(tmp_path, "BLURAYRIPPER_DATABASE_URL=postgresql://db/four\n"
                       f"{secretbox.PASSPHRASE_KEY}=pw\n"
                       "TMDB_API_KEY=tmdb-local\n")

    s = load_settings(tmp_path)

    assert s.tmdb_api_key == "tmdb-local"
    assert s.opensubtitles_api_key is None
