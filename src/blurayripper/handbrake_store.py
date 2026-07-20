"""Web-editable HandBrake preset: expose the useful encode knobs of the active
preset (in config/presets.json) as a typed, tooltip'd field list, and save edits
to a gitignored config/presets.local.json overlay (preferred over presets.json,
so web edits survive `git pull` — same pattern as settings.local.json).

Only <=1080p titles use HandBrake; UHD uses the ffmpeg path (its knobs live in the
regular [encode] settings). Changes apply on the next encode after a restart
(the resolved preset_file is picked at startup).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

LOCAL_PRESET_NAME = "presets.local.json"


@dataclass
class HbField:
    key: str          # HandBrake preset JSON key
    type: str         # "choice" | "int" | "number" | "str"
    label: str
    help: str = ""
    choices: tuple[str, ...] = ()


# The subset worth exposing. Everything here is a top-level key on a HandBrake
# preset object, so we can read/write it directly.
FIELDS: list[HbField] = [
    HbField("VideoEncoder", "choice", "Video encoder",
            "H.265 (x265) is the default; a *_10bit encoder keeps 10-bit/HDR sources 10-bit. "
            "x264 is H.264 — more compatible but larger. nvenc/qsv use the GPU (much faster, "
            "lower quality per bit).",
            ("x265_10bit", "x265", "x264_10bit", "x264",
             "nvenc_h265", "nvenc_h265_10bit", "qsv_h265", "qsv_h265_10bit")),
    HbField("VideoQualitySlider", "number", "Quality (RF / CRF)",
            "Constant-quality target — lower = better quality and bigger files. "
            "~18-20 for UHD, ~20-22 for 1080p, ~19-21 for DVD. 0 is lossless (huge)."),
    HbField("VideoPreset", "choice", "Encoder preset (speed)",
            "Speed vs efficiency, NOT quality. Slower = a smaller file for the same quality, "
            "but much more time. 'medium' is near-transparent; 'slower' is ~1 fps at 4K.",
            ("ultrafast", "superfast", "veryfast", "faster", "fast",
             "medium", "slow", "slower", "veryslow")),
    HbField("VideoTune", "choice", "Tune",
            "x265 tuning. 'grain' preserves film grain (bigger files); 'animation' suits "
            "cartoons; blank = none. Leave blank unless you know you want one.",
            ("", "grain", "animation", "fastdecode", "zerolatency")),
    HbField("VideoProfile", "str", "Profile",
            "Encoder profile — 'auto', or e.g. 'main10' for 10-bit. Leave 'auto' unless a "
            "device needs a specific profile."),
    HbField("VideoLevel", "str", "Level",
            "Encoder level — 'auto' is almost always right."),
    HbField("VideoOptionExtra", "str", "Advanced x265 options",
            "Extra x265 parameters, colon-separated (e.g. ref=5:bframes=5:aq-mode=3). For "
            "fine-tuning; an invalid option fails the encode, so leave as-is if unsure."),
    HbField("VideoFramerateMode", "choice", "Frame-rate mode",
            "'vfr' keeps the source's (variable) rate — best for discs; 'cfr' forces a "
            "constant rate; 'pfr' caps the peak.",
            ("vfr", "cfr", "pfr")),
    HbField("AudioEncoderFallback", "choice", "Audio fallback codec",
            "Codec used when a track can't be passed through untouched (the preset copies "
            "originals when it can). av_aac is a safe, compatible default.",
            ("av_aac", "copy", "ac3", "eac3", "av_opus")),
]

_BY_KEY = {f.key: f for f in FIELDS}


def _load_file(preset_file: Path) -> dict:
    return json.loads(Path(preset_file).read_text(encoding="utf-8"))


def _find_preset(data: dict, preset_name: str) -> dict:
    presets = data.get("PresetList", [])
    if not presets:
        raise ValueError("No presets found in the preset file.")
    return next((p for p in presets if p.get("PresetName") == preset_name), presets[0])


def _coerce(field: HbField, raw):
    if field.type == "int":
        return int(raw)
    if field.type == "number":
        f = float(raw)
        return int(f) if f == int(f) else f
    val = str(raw)
    if field.type == "choice" and field.choices and val not in field.choices:
        raise ValueError(f"{field.label}: '{val}' is not one of {', '.join(field.choices)}.")
    return val


def handbrake_ui_payload(preset_file: Path, preset_name: str) -> dict:
    """Grouped field list + current values for the HandBrake editor."""
    data = _load_file(preset_file)
    preset = _find_preset(data, preset_name)
    fields = []
    for f in FIELDS:
        fields.append({
            "key": f.key, "type": f.type, "label": f.label, "help": f.help,
            "choices": list(f.choices), "value": preset.get(f.key, ""),
        })
    return {"preset_name": preset.get("PresetName", preset_name), "fields": fields}


def save_handbrake(preset_file: Path, preset_name: str, updates: dict) -> Path:
    """Apply updates to the active preset and write config/presets.local.json.
    Returns the written path. Only known fields are accepted."""
    data = _load_file(preset_file)
    preset = _find_preset(data, preset_name)
    for key, raw in (updates or {}).items():
        field = _BY_KEY.get(key)
        if field is None:
            continue  # ignore unknown keys
        preset[key] = _coerce(field, raw)
    out = Path(preset_file).parent / LOCAL_PRESET_NAME
    out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return out
