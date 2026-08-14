"""The global settings overlay lives in Postgres, not in a gitignored file.

The bug this exists to prevent has already happened once: settings.local.json is
gitignored, so `git pull` never restores it and a machine move dropped every
web-edited value back to settings.toml's defaults. That surfaces as an encode
quietly running at the wrong preset, not as an error.
"""

import json

from blurayripper.persistence import MemoryStore
from blurayripper.settings_store import (
    SHARED_KEY, read_overlay, save_overlay, sync_from_cluster,
)


class Durable(MemoryStore):
    @property
    def durable(self): return True


def _overlay(cfg_dir, data):
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "settings.local.json").write_text(json.dumps(data))


def test_turning_on_shared_config_adopts_this_box_rather_than_wiping_it(tmp_path):
    """First run against a database that has no settings yet. The box already
    running in production is the one with the real config — publish it, don't
    clear it."""
    _overlay(tmp_path, {"encode": {"ffmpeg_preset": "slower"}})
    store = Durable()

    note = sync_from_cluster(tmp_path, store)

    assert "seeded" in note
    assert store.get_shared_config(SHARED_KEY) == {"encode": {"ffmpeg_preset": "slower"}}
    assert read_overlay(tmp_path) == {"encode": {"ffmpeg_preset": "slower"}}


def test_a_fresh_box_pulls_the_cluster_settings_instead_of_using_defaults(tmp_path):
    """The migration case: new machine, no local file. It must come up matching
    the cluster, not silently on settings.toml's defaults."""
    store = Durable()
    store.set_shared_config(SHARED_KEY, {"encode": {"ffmpeg_preset": "slower"}})

    note = sync_from_cluster(tmp_path, store)

    assert "pulled" in note
    assert read_overlay(tmp_path) == {"encode": {"ffmpeg_preset": "slower"}}


def test_an_unreachable_database_keeps_the_last_known_settings(tmp_path):
    """Postgres owning the settings must not mean Postgres being down reverts
    them — that would turn an outage into a silent quality regression."""
    _overlay(tmp_path, {"encode": {"ffmpeg_preset": "slower"}})

    class Broken(Durable):
        def get_shared_config(self, key):
            raise ConnectionError("no route to host")

    note = sync_from_cluster(tmp_path, Broken())

    assert "unreachable" in note and "NOT reverted" in note
    assert read_overlay(tmp_path) == {"encode": {"ffmpeg_preset": "slower"}}


def test_saving_settings_publishes_them_to_the_cluster(tmp_path):
    """A value typed on one box has to reach the others, and outlive that box."""
    store = Durable()
    save_overlay(tmp_path, {"encode": {"ffmpeg_preset": "slower"}}, store)

    assert store.get_shared_config(SHARED_KEY)["encode"]["ffmpeg_preset"] == "slower"


def test_a_save_still_works_when_the_cluster_write_fails(tmp_path):
    """Losing the database must not lose the edit the user just made."""
    class Broken(Durable):
        def set_shared_config(self, key, value):
            raise ConnectionError("no route to host")

    save_overlay(tmp_path, {"encode": {"ffmpeg_preset": "slower"}}, Broken())

    assert read_overlay(tmp_path)["encode"]["ffmpeg_preset"] == "slower"


def test_without_a_shared_store_nothing_changes(tmp_path):
    """Single-box installs keep working exactly as before."""
    _overlay(tmp_path, {"encode": {"ffmpeg_preset": "slower"}})
    assert "no shared store" in sync_from_cluster(tmp_path, MemoryStore())
    assert read_overlay(tmp_path) == {"encode": {"ffmpeg_preset": "slower"}}


def test_the_box_set_plan_also_lives_in_the_cluster(tmp_path):
    """Same class of file, same failure: the CSV is gitignored, so it never
    reached the workers and the migration lost it. A missing plan is silent —
    discs just stop being auto-named — so it must not depend on a manual copy."""
    from blurayripper import boxset_plan

    settings = type("S", (), {"config_dir": tmp_path})()
    store = Durable()
    csv = "disc_label,show,year,season,first_episode,episodes\nHAWAII_S1_D1,Hawaii Five-0,1968,1,1,3\n"
    boxset_plan.save_plan(settings, csv, store)

    fresh = type("S", (), {"config_dir": tmp_path / "newbox"})()
    note = boxset_plan.sync_from_cluster(fresh, store)

    assert "pulled 1 box-set row" in note
    assert boxset_plan.load_plan(fresh) == csv


def test_an_outage_never_clears_the_box_set_plan(tmp_path):
    from blurayripper import boxset_plan

    settings = type("S", (), {"config_dir": tmp_path})()
    boxset_plan.save_plan(settings, "disc_label,show\nX,Y\n")

    class Broken(Durable):
        def get_shared_config(self, key):
            raise ConnectionError("no route to host")

    note = boxset_plan.sync_from_cluster(settings, Broken())
    assert "NOT cleared" in note
    assert boxset_plan.load_plan(settings) == "disc_label,show\nX,Y\n"
