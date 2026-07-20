"""Web HandBrake preset editor: read the active preset's fields, save edits to a
gitignored presets.local.json overlay that config prefers over presets.json."""

import json
import shutil
from pathlib import Path

import pytest

from blurayripper import handbrake_store as hb
from blurayripper.config import load_settings


def _cfg(tmp_path):
    for f in Path("config").iterdir():
        if f.is_file():
            shutil.copy(f, tmp_path / f.name)
    return tmp_path


def test_payload_exposes_fields_with_help_and_current_values(tmp_path):
    _cfg(tmp_path)
    pf = tmp_path / "presets.json"
    name = json.loads(pf.read_text())["PresetList"][0]["PresetName"]
    p = hb.handbrake_ui_payload(pf, name)
    keys = {f["key"] for f in p["fields"]}
    assert {"VideoEncoder", "VideoQualitySlider", "VideoPreset", "VideoOptionExtra"} <= keys
    assert all(f["help"] for f in p["fields"])                 # every field has a tooltip
    rf = next(f for f in p["fields"] if f["key"] == "VideoQualitySlider")
    assert rf["type"] == "number" and rf["value"] == 18


def test_save_writes_local_overlay_and_coerces(tmp_path):
    _cfg(tmp_path)
    pf = tmp_path / "presets.json"
    name = json.loads(pf.read_text())["PresetList"][0]["PresetName"]
    out = hb.save_handbrake(pf, name, {"VideoQualitySlider": "20", "VideoPreset": "medium",
                                       "VideoOptionExtra": "ref=4"})
    assert out.name == "presets.local.json"
    preset = json.loads(out.read_text())["PresetList"][0]
    assert preset["VideoQualitySlider"] == 20 and preset["VideoPreset"] == "medium"
    assert preset["VideoOptionExtra"] == "ref=4"
    # The tracked presets.json is untouched.
    assert json.loads(pf.read_text())["PresetList"][0]["VideoQualitySlider"] == 18


def test_invalid_choice_rejected(tmp_path):
    _cfg(tmp_path)
    pf = tmp_path / "presets.json"
    name = json.loads(pf.read_text())["PresetList"][0]["PresetName"]
    with pytest.raises(ValueError):
        hb.save_handbrake(pf, name, {"VideoPreset": "not-a-real-preset"})


def test_config_prefers_local_preset_overlay(tmp_path):
    _cfg(tmp_path)
    pf = tmp_path / "presets.json"
    # settings.toml resolves preset_file relative to the repo root; point it at the
    # tmp copy (absolute) so the overlay check happens in tmp, as it does in prod
    # where the config dir IS the repo config dir.
    toml = tmp_path / "settings.toml"
    toml.write_text(toml.read_text().replace('preset_file = "config/presets.json"',
                                             f'preset_file = "{pf.as_posix()}"'))
    name = json.loads(pf.read_text())["PresetList"][0]["PresetName"]

    # No overlay yet -> config uses presets.json.
    s = load_settings(tmp_path)
    assert s.encode.preset_file.name == "presets.json"
    # After a web save -> config prefers presets.local.json.
    hb.save_handbrake(pf, name, {"VideoQualitySlider": "22"})
    s2 = load_settings(tmp_path)
    assert s2.encode.preset_file.name == "presets.local.json"
