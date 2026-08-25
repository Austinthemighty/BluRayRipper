"""The rejected half of configuration: values that must NOT be accepted, files
that are missing or half-written, and the guards that keep a worker from
restart-looping.

Every case here is a branch this project can actually reach in production — a
gitignored overlay that never arrived, a power cut during a web save, a
per-worker row written by another box, a database that blinks. The shared theme
is that a wrong answer is silent: an encode quietly runs at the wrong preset, a
language list quietly gains an empty entry, a worker quietly stops encoding.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from blurayripper import config, handbrake_store as hb, settings_store as ss
from blurayripper.config import _load_secrets, autodetect_tool, load_settings
from blurayripper.persistence import MemoryStore, WorkerInfo, signal_key
from blurayripper.presence import serve_id
from blurayripper.services import secretbox
from blurayripper.worker import WorkerRunner


class Durable(MemoryStore):
    durable = True   # subclass attr shadows the base read-only property


def _toml(cfg_dir, text=""):
    (cfg_dir / "settings.toml").write_text(text, encoding="utf-8")
    return cfg_dir


def _fields(payload, title):
    return {f["key"]: f for g in payload["groups"] if g["title"] == title
            for f in g["fields"]}


# --- settings overlay: files that are missing or malformed ----------------

def test_a_half_written_overlay_is_ignored_rather_than_breaking_startup(tmp_path):
    """settings.local.json is rewritten in place on every web save, so a power
    cut mid-save leaves a truncated file. The box must still come up on
    settings.toml instead of refusing to start."""
    _toml(tmp_path, '[encode]\nffmpeg_preset = "slow"\n')
    (tmp_path / "settings.local.json").write_text('{"encode": {"ffmpeg_pre',
                                                  encoding="utf-8")

    assert ss.read_overlay(tmp_path) == {}
    assert load_settings(tmp_path).encode.ffmpeg_preset == "slow"


def test_a_malformed_settings_toml_does_not_hide_the_web_overlay(tmp_path):
    """A typo in the tracked TOML would otherwise take the settings page down
    with it — and that page is where the mistake gets corrected."""
    _toml(tmp_path, '[encode\nffmpeg_preset = "slow"\n')        # unclosed header
    (tmp_path / "settings.local.json").write_text(
        json.dumps({"encode": {"ffmpeg_preset": "veryslow"}}), encoding="utf-8")

    enc = _fields(ss.settings_ui_payload(tmp_path), "Encode")
    assert enc["ffmpeg_preset"]["value"] == "veryslow"    # the overlay survived
    assert enc["ffmpeg_crf"]["value"] == 18               # schema default for the rest


def test_the_settings_page_works_with_no_settings_toml_at_all(tmp_path):
    """settings.toml is optional: a box configured entirely from the web (or one
    that pulled its overlay from the cluster before its first checkout) has
    only the overlay."""
    (tmp_path / "settings.local.json").write_text(
        json.dumps({"queue": {"encode_capacity": 9}}), encoding="utf-8")

    assert _fields(ss.settings_ui_payload(tmp_path), "Queue")["encode_capacity"]["value"] == 9


def test_one_corrupt_section_in_the_overlay_does_not_lose_the_others(tmp_path):
    """The overlay can arrive from another box's Postgres row, so it is not
    trusted to be well-formed."""
    _toml(tmp_path)
    (tmp_path / "settings.local.json").write_text(
        json.dumps({"encode": "veryslow", "queue": {"encode_capacity": 9}}),
        encoding="utf-8")

    payload = ss.settings_ui_payload(tmp_path)
    assert _fields(payload, "Queue")["encode_capacity"]["value"] == 9
    # The junk section falls back to the schema default, not the stray string.
    assert _fields(payload, "Encode")["ffmpeg_preset"]["value"] == "medium"


def test_an_unchanged_cluster_overlay_is_not_rewritten(tmp_path):
    """Every box syncs at startup. When the cluster already matches, the local
    cache must be left exactly as it is — rewriting it churns the file for no
    reason and makes a real pull indistinguishable from a no-op."""
    data = {"encode": {"ffmpeg_preset": "slower"}}
    (tmp_path / "settings.local.json").write_text(json.dumps(data), encoding="utf-8")
    store = Durable()
    store.set_shared_config(ss.SHARED_KEY, data)
    before = (tmp_path / "settings.local.json").read_text(encoding="utf-8")

    note = ss.sync_from_cluster(tmp_path, store)

    assert "in sync" in note
    assert (tmp_path / "settings.local.json").read_text(encoding="utf-8") == before


# --- settings overlay: field coercion and validation ---------------------

def test_a_checkbox_is_stored_as_a_real_bool_whatever_the_form_sent(tmp_path):
    """HTML forms post strings and the JSON API posts real bools. "false" is a
    truthy string, so storing the raw value is how a box ends up with Dolby
    Vision "off" that every later `bool()` reads as on."""
    _toml(tmp_path)
    for raw in (True, "true", "TRUE", " on ", "yes", "1"):
        saved = ss.save_overlay(tmp_path, {"encode": {"dovi": raw}})
        assert saved["encode"]["dovi"] is True, raw
    for raw in (False, "false", "off", "no", "0", "", "banana"):
        saved = ss.save_overlay(tmp_path, {"encode": {"dovi": raw}})
        assert saved["encode"]["dovi"] is False, raw
    assert load_settings(tmp_path).encode.dovi is False


def test_a_non_numeric_number_is_rejected_and_the_whole_save_is_dropped(tmp_path):
    """One typo'd box must not half-apply the form: the fields before it in the
    payload would land and the ones after it would not, leaving settings the
    user never chose."""
    _toml(tmp_path)
    ss.save_overlay(tmp_path, {"encode": {"ffmpeg_crf": "16"}})

    with pytest.raises(ValueError, match="whole number"):
        ss.save_overlay(tmp_path, {"encode": {"ffmpeg_crf": "18",
                                              "uhd_min_height": "2160p"}})

    assert ss.read_overlay(tmp_path)["encode"] == {"ffmpeg_crf": 16}


def test_a_list_field_takes_a_comma_string_or_a_real_list_and_drops_blanks(tmp_path):
    """The form posts "eng, pol"; the JSON API posts ["eng", "pol"]. A trailing
    comma must not add an empty language, which would then match no track and
    quietly change what gets kept."""
    _toml(tmp_path)
    assert ss.save_overlay(
        tmp_path, {"languages": {"keep": "eng, pol, "}})["languages"]["keep"] == ["eng", "pol"]
    assert ss.save_overlay(
        tmp_path, {"languages": {"keep": [" eng ", "", "pol"]}})["languages"]["keep"] == ["eng", "pol"]
    assert load_settings(tmp_path).languages.keep == ["eng", "pol"]


def test_a_choice_field_rejects_anything_off_the_list_and_names_the_options(tmp_path):
    """An unknown x265 preset fails hours into the encode, not at save time —
    unless it is rejected here, with the valid values in the message."""
    _toml(tmp_path)
    with pytest.raises(ValueError, match="ultrafast"):
        ss.save_overlay(tmp_path, {"encode": {"ffmpeg_preset": "insane"}})
    assert ss.read_overlay(tmp_path) == {}          # nothing was written

    # Blank is rejected too. None of these fields has a blank option, and a
    # stored blank does NOT mean "not set": load_settings reads the overlay with
    # .get(key, default) and finds the key present, so the default never applies.
    with pytest.raises(ValueError, match="ultrafast"):
        ss.save_overlay(tmp_path, {"encode": {"ffmpeg_preset": ""}})
    assert ss.read_overlay(tmp_path) == {}


def test_a_pasted_tool_path_is_stripped_and_a_missing_value_becomes_blank(tmp_path):
    """A path pasted with a trailing space is stored verbatim otherwise, and
    then fails to launch with an error naming a path that looks correct."""
    _toml(tmp_path)
    saved = ss.save_overlay(tmp_path, {"tools": {"ffmpeg": "  /opt/ffmpeg  ",
                                                 "dovitool": None}})
    assert saved["tools"] == {"ffmpeg": "/opt/ffmpeg", "dovitool": ""}


def test_a_junk_section_in_a_save_is_skipped_not_crashed(tmp_path):
    """The save endpoint takes arbitrary JSON from the browser."""
    _toml(tmp_path)
    assert ss.save_overlay(tmp_path, {"encode": "veryslow",
                                      "queue": {"encode_capacity": "5"}}) == {
        "queue": {"encode_capacity": 5}}


# --- settings scope: global vs per-worker --------------------------------

def test_saving_at_a_server_scope_writes_the_shared_overlay_file(tmp_path):
    """"global", "server", and an absent scope all mean the same thing: the
    settings.local.json overlay every box syncs from. Treating any of them as a
    worker id would file the server's own settings under a worker that does not
    exist, where nothing would ever read them."""
    _toml(tmp_path)
    store = Durable()
    overlay_file = tmp_path / "settings.local.json"

    for scope in ("global", "server", "", None):
        ss.save_settings(tmp_path, {"queue": {"encode_capacity": "7"}},
                         scope=scope, store=store)
        assert json.loads(overlay_file.read_text(encoding="utf-8"))["queue"][
            "encode_capacity"] == 7, scope
        assert store.all_worker_settings() == {}, scope   # no worker row created
        overlay_file.unlink()


def test_a_worker_override_equal_to_the_effective_base_is_not_stored(tmp_path):
    """"Only real overrides are stored" has to compare against the EFFECTIVE
    base — settings.toml merged with the web overlay — not the TOML alone.
    Comparing against the TOML would pin every field the web UI had ever
    changed onto every worker, and those workers would then stop following
    later global edits."""
    _toml(tmp_path, '[encode]\nffmpeg_preset = "medium"\n')
    (tmp_path / "settings.local.json").write_text(
        json.dumps({"encode": {"ffmpeg_preset": "slower"}}), encoding="utf-8")
    store = Durable()

    # "slower" is what this worker already gets from the global overlay.
    assert ss.save_settings(tmp_path, {"encode": {"ffmpeg_preset": "slower"}},
                            scope="w1", store=store) == {}
    assert ss.save_settings(tmp_path, {"encode": {"ffmpeg_preset": "veryslow"}},
                            scope="w1", store=store) == {
        "encode": {"ffmpeg_preset": "veryslow"}}


def test_a_junk_section_in_a_worker_save_is_skipped(tmp_path):
    _toml(tmp_path)
    store = Durable()
    assert ss.save_settings(tmp_path, {"encode": ["nope"],
                                       "queue": {"encode_capacity": "5"}},
                            scope="w1", store=store) == {"queue": {"encode_capacity": 5}}


def test_the_settings_page_still_renders_when_the_cluster_is_unreachable(tmp_path):
    """The worker list is a nicety; the fields are the point. An outage must not
    turn the settings page into a 500."""
    class Broken(Durable):
        def list_workers(self):
            raise ConnectionError("no route to host")

    _toml(tmp_path)
    payload = ss.settings_ui_payload(tmp_path, "global", Broken())

    assert payload["workers"] == []
    assert payload["durable"] is True
    assert _fields(payload, "Encode")["ffmpeg_crf"]["value"] == 18


# --- config: tool auto-detection across platforms ------------------------

def _missing_from_path(monkeypatch, platform, installed_at):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(config.shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "exists", lambda self: str(self) == installed_at)


def test_a_tool_missing_from_path_is_found_at_its_platform_install_location(monkeypatch):
    """The Windows rip box has no PATH entry for HandBrake — the installer does
    not add one. Probing the known install directories is what makes a fresh
    box work without hand-editing settings.toml, and each platform has to probe
    its OWN list."""
    win = r"C:\Program Files\HandBrake\HandBrakeCLI.exe"
    _missing_from_path(monkeypatch, "win32", win)
    assert autodetect_tool("handbrakecli", None) == win

    mac = "/opt/homebrew/bin/HandBrakeCLI"
    _missing_from_path(monkeypatch, "darwin", mac)
    assert autodetect_tool("handbrakecli", None) == mac

    lin = "/usr/bin/HandBrakeCLI"
    _missing_from_path(monkeypatch, "linux", lin)
    assert autodetect_tool("handbrakecli", None) == lin

    # Installed nowhere -> None, so the caller reports which tool is missing
    # rather than launching a path that does not exist.
    monkeypatch.setattr(Path, "exists", lambda self: False)
    assert autodetect_tool("handbrakecli", None) is None


# --- config: secrets.env parsing -----------------------------------------

def test_secrets_env_keeps_a_value_containing_equals_and_only_strips_leading_hashes(
        tmp_path, monkeypatch):
    """ntfy webhook URLs carry tokens with '=' and fragments with '#'.
    Splitting on every '=' or treating '#' anywhere as a comment truncates the
    URL, and the push then 404s silently instead of failing visibly."""
    monkeypatch.setattr(os, "environ", {})
    (tmp_path / "secrets.env").write_text(
        "# BLURAYRIPPER_NOTIFY_URL=https://commented/out\n"
        "\n"
        "   \n"
        "not-a-setting-line\n"
        'BLURAYRIPPER_NOTIFY_URL="https://ntfy.example.com/t?tok=a=b#frag"\n'
        "TMDB_API_KEY = abc123 \n", encoding="utf-8")

    assert _load_secrets(tmp_path) == {
        "BLURAYRIPPER_NOTIFY_URL": "https://ntfy.example.com/t?tok=a=b#frag",
        "TMDB_API_KEY": "abc123",
    }


def test_path_map_rules_share_one_env_line_separated_by_semicolons(tmp_path, monkeypatch):
    """secrets.env is one value per line, so several rewrite rules have to fit
    on a single line. The env form is the per-machine one, so it also has to
    beat the tracked settings.toml."""
    monkeypatch.delenv("BLURAYRIPPER_PATH_MAP", raising=False)
    _toml(tmp_path, '[storage]\npath_map = ["from = /toml"]\n')
    (tmp_path / "secrets.env").write_text(
        "BLURAYRIPPER_PATH_MAP=Z:\\media = /mnt/a ; ; Y:\\b = /mnt/b\n",
        encoding="utf-8")

    assert load_settings(tmp_path).storage.path_map == [
        "Z:\\media = /mnt/a", "Y:\\b = /mnt/b"]


def test_a_per_worker_overlay_with_a_junk_section_still_applies_the_rest(tmp_path):
    """This overlay is a row another box wrote into Postgres. One bad section
    must not stop the worker from loading its settings at all."""
    _toml(tmp_path)
    s = load_settings(tmp_path, extra_overlay={"encode": "veryslow",
                                               "queue": {"encode_capacity": 9}})
    assert s.queue.encode_capacity == 9
    assert s.encode.ffmpeg_preset == "medium"


def test_cluster_credentials_are_decrypted_once_per_passphrase(tmp_path, monkeypatch):
    """scrypt is deliberately slow and load_settings is called freely, so an
    uncached derivation is a visible stall on every call. The cache is keyed by
    (database, passphrase) so rotating either is still picked up instead of
    being served a stale decryption."""
    from blurayripper.config import _CLUSTER_SECRETS

    store = Durable()
    secretbox.publish(store, "pw-one", {"TMDB_API_KEY": "one"})
    opened = []

    def fake_make_store(url):
        opened.append(url)
        return store

    monkeypatch.setattr("blurayripper.persistence.make_store", fake_make_store)
    monkeypatch.setattr(os, "environ", {})
    _CLUSTER_SECRETS.clear()
    try:
        (tmp_path / "secrets.env").write_text(
            "BLURAYRIPPER_DATABASE_URL=postgresql://x/y\n"
            f"{secretbox.PASSPHRASE_KEY}=pw-one\n", encoding="utf-8")
        assert _load_secrets(tmp_path)["TMDB_API_KEY"] == "one"
        assert _load_secrets(tmp_path)["TMDB_API_KEY"] == "one"
        assert opened == ["postgresql://x/y"]      # decrypted once, not twice

        # A rotated passphrase is a different key, so it is read again.
        secretbox.publish(store, "pw-two", {"TMDB_API_KEY": "two"})
        (tmp_path / "secrets.env").write_text(
            "BLURAYRIPPER_DATABASE_URL=postgresql://x/y\n"
            f"{secretbox.PASSPHRASE_KEY}=pw-two\n", encoding="utf-8")
        assert _load_secrets(tmp_path)["TMDB_API_KEY"] == "two"
        assert len(opened) == 2
    finally:
        _CLUSTER_SECRETS.clear()


# --- secretbox: passphrases and blob versioning --------------------------

def test_encrypting_without_a_passphrase_refuses_rather_than_using_an_empty_key():
    """An empty passphrase still derives a perfectly valid scrypt key, so the
    blob would look encrypted while being openable by anyone who tried "". It
    has to be refused where it is noticed, not stored."""
    store = Durable()
    with pytest.raises(secretbox.SecretsUnavailable, match=secretbox.PASSPHRASE_KEY):
        secretbox.encrypt("", {"TMDB_API_KEY": "abc"})
    with pytest.raises(secretbox.SecretsUnavailable):
        secretbox.publish(store, "", {"TMDB_API_KEY": "abc"})
    assert store.get_shared_config(secretbox.SHARED_KEY) is None


def test_an_unrecognised_blob_is_named_as_such_not_blamed_on_the_passphrase():
    """The two failures need opposite fixes. "your passphrase differs from the
    box that stored them" would send someone rotating keys across the whole
    cluster over what is really a version mismatch."""
    blob = secretbox.encrypt("pw", {"TMDB_API_KEY": "abc"})
    blob["v"] = 2
    with pytest.raises(secretbox.SecretsUnavailable, match="Unrecognised"):
        secretbox.decrypt("pw", blob)
    with pytest.raises(secretbox.SecretsUnavailable, match="Unrecognised"):
        secretbox.decrypt("pw", ["not", "a", "blob"])


def test_a_box_with_no_shared_database_never_looks_for_cluster_credentials():
    """A single-box install has nothing to fetch: the store is private to this
    process, so anything found in it is not the cluster's and must not be
    decrypted with this box's passphrase (which would then fail loudly and stop
    a setup that has no cluster at all)."""
    class NotShared(MemoryStore):
        reads: list = []

        def get_shared_config(self, key):
            self.reads.append(key)
            return secretbox.encrypt("some-other-passphrase", {"TMDB_API_KEY": "abc"})

    store = NotShared()
    store.reads = []

    assert secretbox.fetch(store, "pw") == {}
    assert store.reads == []          # the store was never consulted at all


def test_publishing_credentials_needs_a_shared_database():
    """Encrypting into an in-memory store would report success and lose every
    credential when the process exits."""
    with pytest.raises(secretbox.SecretsUnavailable, match="Postgres"):
        secretbox.publish(MemoryStore(), "pw", {"TMDB_API_KEY": "abc"})


def test_a_specific_secrets_failure_is_not_masked_by_the_generic_one(monkeypatch):
    """The generic message says the passphrase must be identical everywhere.
    Wrapping a more specific failure (e.g. the crypto library missing) in that
    text sends the user to change a passphrase that was never the problem."""
    blob = secretbox.encrypt("pw", {"TMDB_API_KEY": "abc"})

    def unavailable(*args, **kwargs):
        raise secretbox.SecretsUnavailable("the cryptography package is missing")

    monkeypatch.setattr(secretbox, "derive_key", unavailable)
    with pytest.raises(secretbox.SecretsUnavailable, match="cryptography package"):
        secretbox.decrypt("pw", blob)


# --- handbrake preset: bad files, unknown keys, shared copies -------------

def _preset_file(tmp_path, **extra):
    pf = tmp_path / "presets.json"
    preset = {"PresetName": "P", "VideoPreset": "medium", "VideoQualitySlider": 20}
    preset.update(extra)
    pf.write_text(json.dumps({"PresetList": [preset]}), encoding="utf-8")
    return pf


def test_a_preset_file_with_no_presets_is_reported_instead_of_falling_back(tmp_path):
    """HandBrake exports a PresetList; a truncated or hand-edited file with none
    must fail loudly rather than silently encoding with something else."""
    pf = tmp_path / "presets.json"
    pf.write_text(json.dumps({"PresetList": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="No presets"):
        hb.handbrake_ui_payload(pf, "P")
    with pytest.raises(ValueError, match="No presets"):
        hb.save_handbrake(pf, "P", {"VideoQualitySlider": "20"})
    assert not (tmp_path / "presets.local.json").exists()


def test_an_unknown_preset_key_is_ignored_rather_than_written_through(tmp_path):
    """The editor posts the whole form. A field name left over from an older UI
    must not inject an unvalidated key that HandBrake then rejects at encode
    time, hours after the save that caused it."""
    pf = _preset_file(tmp_path)
    preset = hb.save_handbrake(pf, "P", {"VideoPreset": "slow",
                                         "Bogus": "x"})["PresetList"][0]
    assert preset["VideoPreset"] == "slow"
    assert "Bogus" not in preset


def test_a_fractional_quality_target_is_not_rounded_to_a_whole_number(tmp_path):
    """RF 20.5 is a real HandBrake setting; storing 20 changes the encode
    quality behind the user's back. Whole numbers still store as ints, since
    HandBrake writes them that way."""
    pf = _preset_file(tmp_path)
    assert hb.save_handbrake(pf, "P", {"VideoQualitySlider": "20.5"})[
        "PresetList"][0]["VideoQualitySlider"] == 20.5
    whole = hb.save_handbrake(pf, "P", {"VideoQualitySlider": "21.0"})[
        "PresetList"][0]["VideoQualitySlider"]
    assert whole == 21 and isinstance(whole, int)


def test_the_shared_preset_replaces_an_unreadable_local_copy(tmp_path):
    """A worker whose presets.local.json was truncated by a power cut has to
    take the cluster's copy. The alternative is that box encoding with a
    different preset from the rest of the cluster, which only ever shows up as
    an odd file size."""
    local = tmp_path / "presets.local.json"
    local.write_text("{not json", encoding="utf-8")
    content = {"PresetList": [{"PresetName": "Cluster", "VideoQualitySlider": 21}]}

    assert hb.apply_shared_preset(tmp_path, content) is True
    assert json.loads(local.read_text(encoding="utf-8")) == content


# --- worker: drain / restart / update guards ------------------------------

def _runner(up_seconds=0.0, worker_id="w1", **cluster):
    s = load_settings()
    for key, value in cluster.items():
        setattr(s.cluster, key, value)
    runner = WorkerRunner(s, worker_id=worker_id)
    runner.store = Durable()
    runner._started_monotonic = time.monotonic() - up_seconds
    return runner


def test_a_database_blip_never_triggers_an_auto_update():
    """The worker cannot see the rip box's version through a failed read, so it
    must assume nothing. Updating on an unreadable store would restart every
    worker in the cluster on each outage."""
    class Blip(Durable):
        def list_workers(self):
            raise ConnectionError("no route to host")

    runner = _runner(up_seconds=3600, worker_auto_update=True)
    runner._version = "0.1.0+240.aaaaaaa"
    runner.store = Blip()

    runner._maybe_auto_update()

    assert runner._stop is False and runner.update_requested is False


def test_a_database_blip_does_not_consume_a_pending_signal():
    """The watermark advances only on a successful read. If a failed poll
    consumed it, the drain the server just asked for would be swallowed and
    never fire."""
    class FlakyOnce(Durable):
        failing = True

        def cluster_signals(self):
            if self.failing:
                self.failing = False
                raise ConnectionError("no route to host")
            return super().cluster_signals()

    store = FlakyOnce()
    runner = _runner()
    runner.store = store
    runner._signal_tokens = {}
    store.request_cluster_signal("drain", "w1")

    runner._check_update_signal()          # the blip: nothing acted on
    assert runner._stop is False

    runner._check_update_signal()          # next poll: the drain still arrives
    assert runner._stop is True


def test_a_drain_in_progress_is_never_upgraded_into_a_restart():
    """A drain means finish up and STAY down (that is how a box gets taken out
    of the cluster for maintenance). Both self-maintenance paths have to notice
    the drain, or the worker would come straight back up."""
    runner = _runner(up_seconds=10 * 86400, worker_restart_after_days=1,
                     worker_auto_update=True)
    runner._version = "0.1.0+240.aaaaaaa"
    runner.store.workers = [WorkerInfo(serve_id("RIPBOX"), "0.1.0+250.bbbbbbb",
                                       None, "serving", 1.0)]
    runner.store.list_workers = lambda: list(runner.store.workers)
    runner.request_stop()

    runner._maybe_periodic_restart()
    runner._maybe_auto_update()

    assert runner.restart_requested is False
    assert runner.update_requested is False


async def test_a_worker_without_a_shared_database_says_so_immediately():
    """A worker pointed at no database would poll a private in-memory queue
    forever, showing as idle while jobs pile up on the rip box."""
    settings = load_settings()
    settings.database_url = None
    runner = WorkerRunner(settings, worker_id="w1")

    with pytest.raises(RuntimeError, match="BLURAYRIPPER_DATABASE_URL"):
        await runner.run()


class _DrainOnFirstPoll(Durable):
    """Reports a drain signal the moment the worker starts polling — run()'s
    startup snapshot sees none — so run() exits after a single pass."""

    def __init__(self):
        super().__init__()
        self.polls = 0
        self.claims = 0

    def cluster_signals(self):
        self.polls += 1              # the first call is run()'s startup snapshot
        return {} if self.polls == 1 else {signal_key("drain", "w1"): 1}

    def claim_next(self, *args, **kwargs):
        self.claims += 1
        return None


def _worker_run_setup(tmp_path, monkeypatch, toml):
    _toml(tmp_path, toml)
    monkeypatch.setattr("blurayripper.version.get_version", lambda: "0.1.0+1.abcdef1")
    settings = load_settings(tmp_path)
    settings.database_url = "postgresql://unused/by-this-test"
    return WorkerRunner(settings, worker_id="w1")


async def test_a_drain_arriving_while_polling_stops_before_the_next_claim(
        tmp_path, monkeypatch):
    """The stop flag is re-checked after the signal poll. Without that check the
    worker would claim one more job on its way out and then hold that claim
    until the heartbeat aged past stale_after_seconds (180s by default), with
    nothing running it."""
    runner = _worker_run_setup(tmp_path, monkeypatch,
                               "[serve]\ncleanup_stale_tools = false\n")
    store = _DrainOnFirstPoll()
    runner.store = store

    await runner.run()

    assert runner._stop is True
    assert store.claims == 0


async def test_a_worker_reaps_its_own_stale_tools_but_never_makemkvcon(
        tmp_path, monkeypatch, capsys):
    """A worker shares the box with a co-located `serve` often enough that this
    matters: HandBrake and the OCR tool are its own to reap after a crash, but
    makemkvcon belongs to serve's LIVE rip, and killing it would abort a disc
    that is halfway through."""
    reaped = []

    async def fake_kill(names):
        reaped.extend(names)
        return ["HandBrakeCLI"]

    monkeypatch.setattr("blurayripper.tools.process.kill_stale_tools", fake_kill)
    runner = _worker_run_setup(tmp_path, monkeypatch, """
[serve]
cleanup_stale_tools = true
[tools]
makemkvcon = "/x/makemkvcon"
handbrakecli = "/x/HandBrakeCLI"
subtitleedit = "/x/seconv"
mkvextract = "/x/mkvextract"
""")
    runner.store = _DrainOnFirstPoll()

    await runner.run()

    assert reaped == ["HandBrakeCLI", "mkvextract", "seconv"]
    assert "Cleaned up stale tool processes: HandBrakeCLI" in capsys.readouterr().out


async def test_a_queue_outage_is_reported_once_and_retried_rather_than_fatal(
        tmp_path, monkeypatch, capsys):
    """A storage blip on the Postgres box must not take every worker down with
    it — they come back on their own. And it must not flood the console either:
    the retry runs every poll interval, so an unchanged error is printed once,
    and a NEW error still gets through."""
    class Down(Durable):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def claim_next(self, *args, **kwargs):
            self.attempts += 1
            if self.attempts >= 5:
                runner.request_stop()
            raise ConnectionError("no route to host" if self.attempts <= 3
                                  else "connection reset")

    runner = _worker_run_setup(tmp_path, monkeypatch, """
[serve]
cleanup_stale_tools = false
[cluster]
worker_poll_seconds = 0
""")
    store = Down()
    runner.store = store

    await runner.run()

    out = capsys.readouterr().out
    assert store.attempts == 5                       # kept retrying, never exited
    assert out.count("no route to host") == 1        # 3 identical failures, 1 line
    assert out.count("connection reset") == 1        # a different error still reported


# --- worker: applying central per-worker settings -------------------------

def _worker_at(tmp_path, store, toml='[encode]\nffmpeg_preset = "medium"\n'):
    _toml(tmp_path, toml)
    runner = WorkerRunner(load_settings(tmp_path), worker_id="w1")
    runner.store = store
    return runner


def test_a_worker_keeps_its_settings_when_the_store_goes_unreachable(tmp_path):
    """An outage must not silently revert a worker to the tracked defaults —
    that turns a database blip into every following encode running at the wrong
    preset, with nothing in the log to say why."""
    class Blip(Durable):
        def get_worker_settings(self, worker_id):
            raise ConnectionError("no route to host")

    store = Durable()
    store.set_worker_settings("w1", {"encode": {"ffmpeg_preset": "veryslow"}})
    runner = _worker_at(tmp_path, store)

    runner._apply_worker_settings()
    assert runner.settings.encode.ffmpeg_preset == "veryslow"

    runner.store = Blip()
    runner._apply_worker_settings()
    assert runner.settings.encode.ffmpeg_preset == "veryslow"   # not reverted


def test_applying_a_central_override_reports_what_it_changed(tmp_path, capsys):
    """The worker's console is the only place this is visible. A silent override
    is how one box ends up encoding differently from the rest of the cluster
    with no trace of where the difference came from."""
    store = Durable()
    store.set_worker_settings("w1", {"encode": {"ffmpeg_preset": "veryslow"}})
    runner = _worker_at(tmp_path, store)

    runner._apply_worker_settings()
    assert "encode.ffmpeg_preset=veryslow" in capsys.readouterr().out

    store.set_worker_settings("w1", {})          # override cleared centrally
    runner._apply_worker_settings()
    out = capsys.readouterr().out
    assert "Applied per-worker settings" not in out
    assert runner.settings.encode.ffmpeg_preset == "medium"


async def test_an_offline_alert_is_skipped_when_no_notify_url_is_set(tmp_path, monkeypatch):
    """Blank means notifications off. Posting to a blank URL would raise inside
    a fire-and-forget task, where nothing reports it."""
    sent = []

    async def fake_notify(url, title, message, priority=None):
        sent.append((url, title))

    monkeypatch.setattr("blurayripper.services.notify.notify", fake_notify)
    runner = _worker_at(tmp_path, Durable())

    runner.settings.notify_url = "https://ntfy.example.com/topic"
    runner._notify("Server offline", "no heartbeat", priority="high")
    await asyncio.sleep(0)
    assert sent == [("https://ntfy.example.com/topic", "Server offline")]

    runner.settings.notify_url = None
    runner._notify("Server offline", "no heartbeat", priority="high")
    await asyncio.sleep(0)
    assert len(sent) == 1                    # nothing was sent the second time
