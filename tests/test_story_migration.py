"""Journey: "I moved the server to a new machine and I do not want to lose my setup."

The setup that matters is exactly the part `git pull` cannot bring: the web-edited
settings overlay, the tuned HandBrake preset, the box-set CSV and the API keys are
all gitignored, so a brand-new box has a config directory containing only what the
repo tracks. That is not a loud failure — the box starts, rips, encodes, and the
files look fine; the encode simply runs at the tracked default preset instead of
the one that was tuned, and nothing anywhere says so. It happened once already.

So this walks the whole move as one story: an old box publishes what it is running
to the cluster, a new box with an EMPTY config dir boots and comes back with all of
it, credentials cross encrypted, the new box's own values still win, the jobs whose
paths were written on the old machine still resolve — and a database outage on the
way leaves the new box on what it migrated rather than reverting it.

Two config dirs, one durable store, and the real load/save/sync functions between
them. Nothing external runs: no disc, no HandBrake, no Postgres.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from blurayripper import boxset_plan
from blurayripper import handbrake_store as hb
from blurayripper import pipeline as pl
from blurayripper import settings_store as ss
from blurayripper.config import _CLUSTER_SECRETS, _load_secrets, load_settings
from blurayripper.models import DiscInfo, Title
from blurayripper.persistence import MemoryStore
from blurayripper.pipeline import Job, Pipeline
from blurayripper.services import secretbox
from blurayripper.tools.handbrake import HandBrake

DB_URL = "postgresql://nas/blurayripper"
PASSPHRASE = "the-one-cluster-passphrase"
PRESET_NAME = "Plex H265 1080p"

# What a fresh `git clone` gives a new box: the tracked preset file and the tracked
# settings. Every value the operator changed lives somewhere else.
TRACKED_PRESET = {"PresetList": [{
    "PresetName": PRESET_NAME, "VideoEncoder": "x265_10bit",
    "VideoQualitySlider": 22, "VideoPreset": "medium", "VideoFramerateMode": "vfr",
}]}
TRACKED_TOML = ('ffmpeg_preset = "medium"\n'
                '[queue]\nencode_capacity = 3\n'
                '[subtitles]\nocr_concurrency = 4\n')

# The Settings page as the operator left it on the old box.
WEB_EDITS = {
    "encode": {"ffmpeg_preset": "slower", "tune_grain": "true",
               "preset_name": PRESET_NAME},
    "queue": {"encode_capacity": "7"},
    "subtitles": {"ocr_concurrency": "8"},
}
PLAN_CSV = ("disc_label,show,year,season,first_episode,episodes\n"
            "THE_OFFICE_S3_D2,The Office,2005,3,9,4\n")


class SharedPostgres(MemoryStore):
    """The one database both boxes can reach — it lives on the NAS, not on either
    of them, which is the whole reason it survives the move."""

    @property
    def durable(self):
        return True


class Unreachable(SharedPostgres):
    """That NAS, mid-reboot."""

    def get_shared_config(self, key):
        raise ConnectionError("no route to host")


class _Report:
    """The job log the operator watches."""

    def __init__(self):
        self.events = []

    def on_event(self, message):
        self.events.append(message)

    def on_progress(self, *a, **k):
        pass

    def checkpoint(self):
        pass

    def notify(self, *a, **k):
        pass

    def ejected(self, *a, **k):
        pass


@pytest.fixture(autouse=True)
def _one_machine_at_a_time(monkeypatch):
    """Each box is its own process in production: an ambient BLURAYRIPPER_* var
    would silently outrank every file written here, the decrypted-credentials
    cache is process-wide, and the path map is installed globally by Pipeline."""
    for key in list(os.environ):
        if key.startswith(("BLURAYRIPPER_", "OPENSUBTITLES_")) or key == "TMDB_API_KEY":
            monkeypatch.delenv(key, raising=False)
    _CLUSTER_SECRETS.clear()
    yield
    _CLUSTER_SECRETS.clear()
    pl.configure_path_map([])


def _fresh_checkout(tmp_path, name) -> Path:
    """A config dir holding only the two files git tracks."""
    box = tmp_path / name
    box.mkdir(parents=True, exist_ok=True)
    (box / "settings.toml").write_text(
        "[encode]\n"
        f'preset_file = "{(box / "presets.json").as_posix()}"\n'
        f'preset_name = "{PRESET_NAME}"\n' + TRACKED_TOML,
        encoding="utf-8")
    (box / "presets.json").write_text(json.dumps(TRACKED_PRESET), encoding="utf-8")
    return box


def _secrets_env(box: Path, **values) -> None:
    (box / "secrets.env").write_text(
        "\n".join([f"BLURAYRIPPER_DATABASE_URL={DB_URL}"]
                  + [f"{k}={v}" for k, v in values.items()]) + "\n",
        encoding="utf-8")


def _operator_configures(box: Path, store, extra: dict | None = None) -> None:
    """The three web pages the operator used on the old box, through the same
    functions the endpoints call: Settings, HandBrake, Box-sets."""
    ss.save_overlay(box, {**WEB_EDITS, **(extra or {})}, store)
    data = hb.save_handbrake(box / "presets.json", PRESET_NAME,
                             {"VideoQualitySlider": "19", "VideoPreset": "slow"})
    store.set_shared_config(hb.SHARED_KEY, data)          # what POST /api/handbrake does
    boxset_plan.save_plan(SimpleNamespace(config_dir=box), PLAN_CSV, store)


def _boot(box: Path, store):
    """What `blurayripper serve` does before it starts serving (cli._cmd_serve):
    pull the cluster's preset, settings overlay and box-set plan, THEN load
    settings — in that order, because both the preset file and the overlay are
    resolved at load time. Returns (settings, notes)."""
    notes = []
    try:
        shared = store.get_shared_config(hb.SHARED_KEY)
        notes.append("preset pulled" if (shared and hb.apply_shared_preset(box, shared))
                     else "preset in sync")
    except Exception as exc:
        notes.append(f"preset unreachable ({type(exc).__name__})")
    notes.append(ss.sync_from_cluster(box, store))
    notes.append(boxset_plan.sync_from_cluster(SimpleNamespace(config_dir=box), store))
    return load_settings(box), notes


def _summary(settings) -> str:
    """The 'what am I actually encoding with' line the job log prints."""
    return HandBrake(settings).speed_summary()


# --- the move itself: an empty box comes back as the old one ----------------

def test_the_new_box_boots_on_the_migrated_settings_not_the_tracked_defaults(tmp_path):
    """The whole Settings page is a gitignored file, so a fresh checkout silently
    starts from settings.toml — a slower, better preset reverts to the tracked
    default and the only symptom is that new encodes look slightly worse than the
    ones already in the library. The 'before' half here is that exact regression:
    it is what the new box would run if the pull did nothing."""
    store = SharedPostgres()
    _operator_configures(_fresh_checkout(tmp_path, "old"), store)
    new = _fresh_checkout(tmp_path, "new")

    would_have_run = load_settings(new)          # a boot that skipped the cluster
    assert would_have_run.encode.ffmpeg_preset == "medium"
    assert would_have_run.queue.encode_capacity == 3
    assert would_have_run.encode.tune_grain is False

    settings, notes = _boot(new, store)

    assert any(n.startswith("pulled") for n in notes), notes
    assert settings.encode.ffmpeg_preset == "slower"
    assert settings.queue.encode_capacity == 7
    assert settings.subtitles.ocr_concurrency == 8
    assert settings.encode.tune_grain is True


def test_the_tuned_handbrake_preset_is_what_the_new_box_will_actually_encode_with(tmp_path):
    """The preset holds the encoder, the quality and the speed — the settings page
    only names it. A new box keeps its tracked copy of that file, so without the
    pull it encodes at the tracked CQ, produces a perfectly playable file, and the
    difference is only visible by comparing bitrates months later."""
    store = SharedPostgres()
    _operator_configures(_fresh_checkout(tmp_path, "old"), store)
    new = _fresh_checkout(tmp_path, "new")
    _secrets_env(new, BLURAYRIPPER_HANDBRAKECLI="/opt/handbrake/HandBrakeCLI")

    assert "CQ 22" in _summary(load_settings(new))       # the tracked file it shipped with

    settings, _notes = _boot(new, store)

    line = _summary(settings)
    assert "CQ 19" in line and "preset slow" in line, line
    assert line.endswith("from presets.local.json")
    # ...and that is the file the encode command hands to HandBrake, not the
    # tracked one still sitting next to it.
    cmd = HandBrake(settings).build_command(Path("/in.mkv"), Path("/out.mkv"))
    assert cmd[cmd.index("--preset-import-file") + 1] == str(new / "presets.local.json")
    assert cmd[cmd.index("-Z") + 1] == PRESET_NAME


def test_a_preset_that_lands_after_settings_are_loaded_does_not_apply_until_a_reload(tmp_path):
    """Order, not just content: which preset file a box uses is decided once, when
    settings are loaded. Pull the preset after that and this whole session keeps
    encoding with the old one — the boot sequence has to re-load, and this pins
    why. Both halves are asserted so a reload that never happens can't pass."""
    store = SharedPostgres()
    _operator_configures(_fresh_checkout(tmp_path, "old"), store)
    new = _fresh_checkout(tmp_path, "new")

    early = load_settings(new)                    # settings read before the pull
    assert hb.apply_shared_preset(new, store.get_shared_config(hb.SHARED_KEY)) is True

    assert early.encode.preset_file.name == "presets.json"
    assert "CQ 22" in _summary(early), "the already-loaded settings changed under us"

    reloaded = load_settings(new)
    assert reloaded.encode.preset_file.name == "presets.local.json"
    assert "CQ 19" in _summary(reloaded)


def test_the_box_set_plan_still_names_the_first_disc_put_in_the_new_machine(tmp_path):
    """A lost plan is the quietest loss of all: the disc simply parks for manual
    naming, which looks like the matcher having a bad day rather than like a file
    that never came across. So assert the disc gets named, not just that a CSV
    arrived."""
    store = SharedPostgres()
    _operator_configures(_fresh_checkout(tmp_path, "old"), store)
    new = _fresh_checkout(tmp_path, "new")
    disc = DiscInfo(source="disc:0", volume_label="THE_OFFICE_S3_D2", titles=[
        Title(title_id=i, duration_seconds=2600, source_filename=f"0080{i}.mpls")
        for i in range(4)])

    _settings, notes = _boot(new, store)

    assert "pulled 1 box-set row(s) from the cluster" in notes
    row = boxset_plan.match(boxset_plan.load_rows(SimpleNamespace(config_dir=new)),
                            disc.volume_label)
    assert row is not None, "the plan did not survive the move"
    plan = boxset_plan.assignments_for(row, disc)
    assert plan.ok, plan.reason
    assert [(a.show_or_movie_title, a.season, a.episode) for a in plan.assignments] == [
        ("The Office", 3, 9), ("The Office", 3, 10),
        ("The Office", 3, 11), ("The Office", 3, 12)]


# --- the credentials --------------------------------------------------------

def test_the_api_keys_cross_as_ciphertext_and_open_on_the_box_that_has_the_passphrase(
        tmp_path, monkeypatch):
    """Credentials are the thing every migration lost, but moving them into a
    database everyone can read would trade one problem for a worse one: a dump or
    a replica would hand over every key. So the row has to be inert on its own and
    still open unattended on a box that has the passphrase in its secrets.env."""
    store = SharedPostgres()
    old = _fresh_checkout(tmp_path, "old")
    _secrets_env(old, **{secretbox.PASSPHRASE_KEY: PASSPHRASE,
                         "TMDB_API_KEY": "tmdb-live-key",
                         "OPENSUBTITLES_USERNAME": "media"})
    new = _fresh_checkout(tmp_path, "new")
    _secrets_env(new, **{secretbox.PASSPHRASE_KEY: PASSPHRASE})
    monkeypatch.setattr("blurayripper.persistence.make_store", lambda url: store)

    raw = _load_secrets(old)                       # `blurayripper secrets push`
    secretbox.publish(store, raw[secretbox.PASSPHRASE_KEY], secretbox.movable(raw))
    _CLUSTER_SECRETS.clear()                       # the new box is another process

    blob = json.dumps(store.get_shared_config(secretbox.SHARED_KEY))
    assert "tmdb-live-key" not in blob and "media" not in blob
    with pytest.raises(secretbox.SecretsUnavailable, match="identical everywhere"):
        secretbox.decrypt("what-i-thought-it-was", json.loads(blob))

    settings = load_settings(new)
    assert settings.tmdb_api_key == "tmdb-live-key"
    assert settings.opensubtitles_username == "media"


def test_a_new_box_without_the_passphrase_gets_no_keys_rather_than_the_keys(
        tmp_path, monkeypatch):
    """The passphrase line is the one thing the operator must copy by hand, so the
    forgetting case has to be safe: the cluster's row stays shut, and the box runs
    on its own secrets.env exactly as an unclustered box always did."""
    store = SharedPostgres()
    secretbox.publish(store, PASSPHRASE, {"TMDB_API_KEY": "tmdb-live-key"})
    new = _fresh_checkout(tmp_path, "new")
    _secrets_env(new, **{"OPENSUBTITLES_API_KEY": "typed-on-this-box"})
    monkeypatch.setattr("blurayripper.persistence.make_store", lambda url: store)

    settings = load_settings(new)

    assert settings.tmdb_api_key is None
    assert settings.opensubtitles_api_key == "typed-on-this-box"


def test_what_the_new_box_sets_for_itself_outranks_what_the_cluster_says(
        tmp_path, monkeypatch):
    """The new machine is not the old one: its library is on a different mount and
    its own OpenSubtitles account is already configured. Cluster config filling
    those in would point the migrated box at a drive letter that does not exist
    there — a whole library written into nowhere — so a local value has to win on
    both layers, the settings overlay and the shared credentials."""
    store = SharedPostgres()
    old = _fresh_checkout(tmp_path, "old")
    _secrets_env(old, **{secretbox.PASSPHRASE_KEY: PASSPHRASE,
                         "TMDB_API_KEY": "tmdb-live-key",
                         "OPENSUBTITLES_API_KEY": "old-box-os-key"})
    _operator_configures(old, store, extra={"storage": {"output_dir": r"Z:\Media"}})
    raw = _load_secrets(old)
    secretbox.publish(store, PASSPHRASE, secretbox.movable(raw))
    _CLUSTER_SECRETS.clear()

    new = _fresh_checkout(tmp_path, "new")
    _secrets_env(new, **{secretbox.PASSPHRASE_KEY: PASSPHRASE,
                         "OPENSUBTITLES_API_KEY": "new-box-os-key",
                         "BLURAYRIPPER_OUTPUT_DIR": str(tmp_path / "mnt" / "media")})
    monkeypatch.setattr("blurayripper.persistence.make_store", lambda url: store)

    settings, _notes = _boot(new, store)

    assert settings.storage.output_dir == tmp_path / "mnt" / "media"
    assert settings.opensubtitles_api_key == "new-box-os-key"
    # The cluster's values did arrive — this is a collision being won locally, not
    # the pull being skipped: the overlay is on disk and the gap-filling worked.
    assert ss.read_overlay(new)["storage"]["output_dir"] == r"Z:\Media"
    assert settings.encode.ffmpeg_preset == "slower"
    assert settings.tmdb_api_key == "tmdb-live-key"


# --- the paths those jobs were queued with ----------------------------------

async def test_a_job_queued_on_the_old_windows_box_resumes_through_the_migrated_map(
        tmp_path):
    """Rip checkpoints under staging float by themselves, but a job added from a
    file elsewhere stores the path verbatim — 131 of them, all naming a drive
    letter that does not exist on the new box. Each one fails with "the source
    file is gone" until the map arrives; the map is a setting, so it migrates with
    the rest and the queue drains instead of being re-added by hand."""
    store = SharedPostgres()
    old = _fresh_checkout(tmp_path, "old")
    nas = tmp_path / "mnt" / "TrueNas" / "media"
    ripped = nas / "MakeMKV" / "To Process" / "Cars 3 (2017).mkv"
    ripped.parent.mkdir(parents=True)
    ripped.write_bytes(b"the file, on the NAS, where it always was")
    _operator_configures(old, store, extra={"storage": {
        "path_map": [rf"Z:\media = {nas}"]}})

    new = _fresh_checkout(tmp_path, "new")
    _secrets_env(new, BLURAYRIPPER_STAGING_DIR=str(tmp_path / "staging"),
                 BLURAYRIPPER_OUTPUT_DIR=str(tmp_path / "output"))
    stored = r"Z:\media\MakeMKV\To Process\Cars 3 (2017).mkv"
    job = Job(source=stored)
    job.selected_title_ids = [0]
    job.checkpoints = {0: {"ripped_path": stored}}
    report = _Report()

    before = Pipeline(load_settings(new))          # the box as it arrived
    with pytest.raises(RuntimeError, match="The source file is gone"):
        await before._ensure_ripped(job, 0, tmp_path / "staging" / "job_1", report)

    settings, _notes = _boot(new, store)
    assert settings.storage.path_map == [rf"Z:\media = {nas}"]
    got = await Pipeline(settings)._ensure_ripped(
        job, 0, tmp_path / "staging" / "job_1", report)

    assert got == ripped and got.read_bytes().startswith(b"the file")
    assert any("Resuming title 0" in e for e in report.events), report.events


async def test_a_path_that_still_works_on_this_box_is_never_rewritten(tmp_path):
    """The rule is one shared setting, so it lands on every box including the
    Windows worker still in the cluster, where those Z: paths are real. Rewriting
    there would send it to a mount it cannot see — or, worse, to a stale copy of
    the same filename. Existing wins, always."""
    here = tmp_path / "Z_media"
    here.mkdir()
    (here / "Cars 3 (2017).mkv").write_bytes(b"the real one, still on this box")
    elsewhere = tmp_path / "mnt" / "TrueNas" / "media"
    elsewhere.mkdir(parents=True)
    (elsewhere / "Cars 3 (2017).mkv").write_bytes(b"a stale copy of the same name")

    box = _fresh_checkout(tmp_path, "mixed")
    _secrets_env(box, BLURAYRIPPER_STAGING_DIR=str(tmp_path / "staging"),
                 BLURAYRIPPER_OUTPUT_DIR=str(tmp_path / "output"))
    ss.save_overlay(box, {"storage": {"path_map": [f"{here} = {elsewhere}"]}})
    settings = load_settings(box)
    stored = str(here / "Cars 3 (2017).mkv")
    job = Job(source=stored)
    job.selected_title_ids = [0]
    job.checkpoints = {0: {"ripped_path": stored}}

    got = await Pipeline(settings)._ensure_ripped(
        job, 0, tmp_path / "staging" / "job_1", _Report())

    assert got.read_bytes() == b"the real one, still on this box"
    assert got == here / "Cars 3 (2017).mkv"


# --- and the database goes down halfway through the move --------------------

def test_the_database_going_down_leaves_the_new_box_on_everything_it_migrated(tmp_path):
    """Postgres owning the config must not mean Postgres being down erasing it.
    Every local file is a cache, so the box that already migrated has to restart
    into the same config during an outage — reverting to the tracked defaults
    would turn somebody else's NAS reboot into a silent quality regression here."""
    store = SharedPostgres()
    _operator_configures(_fresh_checkout(tmp_path, "old"), store)
    new = _fresh_checkout(tmp_path, "new")
    _boot(new, store)
    cached = {p.name: p.read_text() for p in sorted(new.iterdir())}

    settings, notes = _boot(new, Unreachable())

    assert [n for n in notes if "unreachable" in n], notes
    assert "settings were NOT reverted" in " ".join(notes)
    assert {p.name: p.read_text() for p in sorted(new.iterdir())} == cached
    assert settings.encode.ffmpeg_preset == "slower"
    assert settings.queue.encode_capacity == 7
    assert "CQ 19" in _summary(settings)
    assert boxset_plan.match(
        boxset_plan.load_rows(SimpleNamespace(config_dir=new)),
        "THE_OFFICE_S3_D2") is not None
