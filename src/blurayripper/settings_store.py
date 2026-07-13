"""Web-editable settings overlay: schema, load, and save.

The web UI edits a gitignored ``config/settings.local.json`` overlay that
``load_settings`` merges over ``settings.toml`` — but *under* ``secrets.env`` /
process env vars, so secrets (the database URL, API keys, per-machine tool paths
set there) stay authoritative and are never clobbered by a web edit. Changes
take effect on the next ``serve`` restart.

The database connection is intentionally NOT in the schema: it holds a secret
and lives in ``config/secrets.env``.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .config import DEFAULT_CONFIG_DIR, _load_secrets  # noqa: F401 (shared secrets parse)

OVERLAY_NAME = "settings.local.json"


@dataclass
class Field:
    section: str
    key: str
    type: str  # "bool" | "int" | "str" | "choice" | "list"
    label: str
    default: object
    help: str = ""
    choices: tuple[str, ...] = ()
    env: str | None = None  # secrets.env var that shadows this field, if any


# Editable fields grouped for the UI, mirroring the settings.toml structure.
# `default` mirrors config.load_settings' fallback so the form shows the real
# effective value when neither the TOML nor the overlay sets a key.
SCHEMA: list[tuple[str, list[Field]]] = [
    ("Tools", [
        Field("tools", "makemkvcon", "str", "MakeMKV console", "", "Blank = auto-detect.", env="BLURAYRIPPER_MAKEMKVCON"),
        Field("tools", "handbrakecli", "str", "HandBrakeCLI", "", "Blank = auto-detect.", env="BLURAYRIPPER_HANDBRAKECLI"),
        Field("tools", "mkvmerge", "str", "mkvmerge", "", "Blank = auto-detect.", env="BLURAYRIPPER_MKVMERGE"),
        Field("tools", "mkvextract", "str", "mkvextract", "", "Blank = auto-detect.", env="BLURAYRIPPER_MKVEXTRACT"),
        Field("tools", "subtitleedit", "str", "Subtitle Edit", "", "Blank = auto-detect.", env="BLURAYRIPPER_SUBTITLEEDIT"),
        Field("tools", "subtitleedit_runner", "str", "SE runner", "", "e.g. 'mono' on macOS/Linux; blank runs directly.", env="BLURAYRIPPER_SUBTITLEEDIT_RUNNER"),
        Field("tools", "ffmpeg", "str", "ffmpeg", "", "Grain preview + UHD/HDR encoding.", env="BLURAYRIPPER_FFMPEG"),
        Field("tools", "dovitool", "str", "dovi_tool", "", "Dolby Vision UHD re-encodes.", env="BLURAYRIPPER_DOVITOOL"),
    ]),
    ("Storage", [
        Field("storage", "staging_dir", "str", "Staging dir", "./staging", "Per-job temp/intermediates root.", env="BLURAYRIPPER_STAGING_DIR"),
        Field("storage", "output_dir", "str", "Output dir", "./output", "Plex library root.", env="BLURAYRIPPER_OUTPUT_DIR"),
        Field("storage", "movies_subdir", "str", "Movies subfolder", "Movies"),
        Field("storage", "tv_subdir", "str", "TV subfolder", "TV Shows"),
    ]),
    ("Encode", [
        Field("encode", "container", "choice", "Container", "mp4", "Must match the HandBrake preset's format.", choices=("mkv", "mp4")),
        Field("encode", "preset_name", "str", "HandBrake preset", "Plex H265 1080p", "Preset name inside presets.json (used for <=1080p)."),
        Field("encode", "tune_grain", "bool", "Tune grain (default)", False),
        Field("encode", "low_priority", "bool", "Low-priority encodes", True),
        Field("encode", "deinterlace", "choice", "Deinterlace", "auto", choices=("auto", "on", "off")),
        Field("encode", "deinterlace_max_height", "int", "Deinterlace max height", 576),
        Field("encode", "ffmpeg_uhd", "bool", "ffmpeg UHD path", False, "Encode >= min height with ffmpeg to preserve HDR10."),
        Field("encode", "uhd_min_height", "int", "UHD min height", 2160),
        Field("encode", "ffmpeg_crf", "int", "ffmpeg CRF", 18, "x265 quality; lower = better/bigger."),
        Field("encode", "ffmpeg_preset", "choice", "ffmpeg x265 preset", "medium", "Slower = smaller file, same quality, more time.",
              choices=("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow")),
        Field("encode", "ffmpeg_audio", "choice", "ffmpeg audio", "copy", "'copy' needs container = mkv.", choices=("copy", "aac")),
        Field("encode", "dovi", "bool", "Preserve Dolby Vision", False, "Needs dovi_tool + mkvmerge + temp space; requires ffmpeg UHD."),
    ]),
    ("Languages", [
        Field("languages", "keep", "list", "Keep languages", ["eng"], "ISO 639-2/B codes, comma-separated (e.g. eng, pol)."),
        Field("languages", "default", "str", "Default language", "eng"),
    ]),
    ("Subtitles", [
        Field("subtitles", "fix_common_errors", "bool", "Fix common OCR errors", True),
        Field("subtitles", "remove_text_for_hi", "bool", "Strip SDH (hearing-impaired)", False),
        Field("subtitles", "ocr_concurrency", "int", "OCR concurrency", 4),
        Field("subtitles", "keep_extracted", "bool", "Cache extracted subs", True),
        Field("subtitles", "extract_concurrency", "int", "Extract concurrency", 2),
        Field("subtitles", "ocr_timeout_seconds", "int", "OCR timeout (s)", 3600),
        Field("subtitles", "upload_export_dir", "str", "Subtitle upload export dir", "", "Blank = off.", env="BLURAYRIPPER_SUB_UPLOAD_DIR"),
    ]),
    ("Audio", [
        Field("audio", "tracks", "choice", "Audio tracks", "all", "'all' keeps commentaries; 'main' = first per language.", choices=("all", "main")),
    ]),
    ("Queue", [
        Field("queue", "encode_capacity", "int", "Encode capacity", 3, "UHD encodes cost 2 units, others 1."),
    ]),
    ("Cluster", [
        Field("cluster", "use_workers", "bool", "Use workers", False, "Rip box enqueues; separate workers encode.", env="BLURAYRIPPER_USE_WORKERS"),
        Field("cluster", "worker_poll_seconds", "int", "Worker poll (s)", 5),
        Field("cluster", "heartbeat_seconds", "int", "Heartbeat (s)", 15),
        Field("cluster", "stale_after_seconds", "int", "Stale reclaim (s)", 180),
        Field("cluster", "local_encode_max_height", "int", "Local encode max height", 0, "0 = rip box never encodes.", env="BLURAYRIPPER_LOCAL_ENCODE_MAX_HEIGHT"),
        Field("cluster", "worker_does_subs", "bool", "Workers do subtitles", False, env="BLURAYRIPPER_WORKER_DOES_SUBS"),
    ]),
    ("Disc", [
        Field("disc", "auto_eject", "bool", "Auto-eject after rip", True),
        Field("disc", "auto_open", "bool", "Auto-scan on insert", True),
        Field("disc", "poll_seconds", "int", "Drive poll (s)", 15),
        Field("disc", "extract_cover_art", "bool", "Extract disc cover art", True),
    ]),
    ("Serve", [
        Field("serve", "cleanup_stale_tools", "bool", "Clean up stale tools at startup", True),
        Field("serve", "prep_concurrency", "int", "Prep concurrency", 2, "How many jobs rip+subs+grain at once."),
        Field("serve", "auto_retry_max", "int", "Auto-retry max", 2),
        Field("serve", "notify_url", "str", "Notify webhook URL", "", "ntfy-style; blank = off.", env="BLURAYRIPPER_NOTIFY_URL"),
    ]),
    ("Scan", [
        Field("scan", "min_length_seconds", "int", "Min title length (s)", 120, "Drops menus/trailers below this."),
    ]),
    ("Metadata", [
        Field("metadata", "language", "str", "Metadata language", "en-US"),
        Field("metadata", "write_nfo", "bool", "Write .nfo sidecars", True),
        Field("metadata", "fetch_artwork", "bool", "Fetch poster/backdrop from TMDB", True,
              "Downloads the real poster + backdrop, overriding disc art (needs a TMDB key)."),
    ]),
    ("Web", [
        Field("web", "host", "str", "Bind host", "127.0.0.1", "0.0.0.0 = reachable on the LAN (no login!)."),
        Field("web", "port", "int", "Port", 8723),
    ]),
]

_FIELDS: dict[tuple[str, str], Field] = {
    (f.section, f.key): f for _title, group in SCHEMA for f in group
}

# Fields that make sense to override PER WORKER (encode quality/speed, the
# concurrency knobs, which jobs a worker takes, and its tool paths). Server-only
# knobs (web host/port, drive polling, disc auto-open, notify, database) are not
# here — they don't apply to an encode worker.
_WORKER_KEYS: set[tuple[str, str]] = {
    ("tools", "handbrakecli"), ("tools", "mkvmerge"), ("tools", "mkvextract"),
    ("tools", "subtitleedit"), ("tools", "subtitleedit_runner"),
    ("tools", "ffmpeg"), ("tools", "dovitool"),
    ("encode", "container"), ("encode", "preset_name"), ("encode", "tune_grain"),
    ("encode", "low_priority"), ("encode", "deinterlace"), ("encode", "deinterlace_max_height"),
    ("encode", "ffmpeg_uhd"), ("encode", "uhd_min_height"), ("encode", "ffmpeg_crf"),
    ("encode", "ffmpeg_preset"), ("encode", "ffmpeg_audio"), ("encode", "dovi"),
    ("subtitles", "ocr_concurrency"), ("subtitles", "extract_concurrency"),
    ("subtitles", "ocr_timeout_seconds"),
    ("queue", "encode_capacity"),
    ("cluster", "local_encode_max_height"), ("cluster", "worker_does_subs"),
}


def _is_worker_field(f: Field) -> bool:
    return (f.section, f.key) in _WORKER_KEYS


def _overlay_path(cfg_dir: Path) -> Path:
    return cfg_dir / OVERLAY_NAME


def read_overlay(cfg_dir: Path) -> dict:
    """Return the web-edited overlay dict (empty if absent/unreadable)."""
    path = _overlay_path(cfg_dir)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _configured(cfg_dir: Path) -> dict:
    """settings.toml deep-merged with the overlay (no secrets) — 'configured' values."""
    data: dict = {}
    toml_file = cfg_dir / "settings.toml"
    if toml_file.exists():
        try:
            with toml_file.open("rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
    for section, values in read_overlay(cfg_dir).items():
        if isinstance(values, dict):
            data.setdefault(section, {}).update(values)
    return data


def _coerce(f: Field, raw: object):
    """Validate/convert a raw form value to the field's type. Raises ValueError."""
    if f.type == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if f.type == "int":
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{f.section}.{f.key} must be a whole number")
    if f.type == "list":
        if isinstance(raw, list):
            items = [str(x).strip() for x in raw]
        else:
            items = [p.strip() for p in str(raw).split(",")]
        return [x for x in items if x]
    value = "" if raw is None else str(raw).strip()
    if f.type == "choice" and value and value not in f.choices:
        raise ValueError(f"{f.section}.{f.key} must be one of {', '.join(f.choices)}")
    return value


def save_overlay(cfg_dir: Path, updates: dict) -> dict:
    """Validate `updates` against the schema, merge into the overlay, write it.

    Unknown / non-editable keys (anything not in SCHEMA, including [database])
    are ignored, so the endpoint can't be used to smuggle in a secret.
    """
    overlay = read_overlay(cfg_dir)
    for section, kv in (updates or {}).items():
        if not isinstance(kv, dict):
            continue
        for key, raw in kv.items():
            spec = _FIELDS.get((section, key))
            if spec is None:
                continue
            overlay.setdefault(section, {})[key] = _coerce(spec, raw)
    path = _overlay_path(cfg_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(overlay, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return overlay


def _is_worker_scope(scope: str | None) -> bool:
    return bool(scope) and scope not in ("global", "server")


def save_settings(cfg_dir: Path, updates: dict, scope: str = "global", store=None) -> dict:
    """Dispatch a settings save by scope.

    scope "global" -> settings.local.json (server + defaults for all workers).
    scope <worker_id> -> the shared store, as a SPARSE overlay: only fields that
    DIFFER from the global base are kept, so a worker override stays minimal and
    unset fields keep following the global config.
    """
    if not _is_worker_scope(scope):
        return save_overlay(cfg_dir, updates)
    if store is None or not getattr(store, "durable", False):
        raise ValueError("Per-worker settings need a shared Postgres store "
                         "(set BLURAYRIPPER_DATABASE_URL and run `blurayripper db-init`).")
    base = _configured(cfg_dir)
    overlay: dict = {}
    for section, kv in (updates or {}).items():
        if not isinstance(kv, dict):
            continue
        for key, raw in kv.items():
            spec = _FIELDS.get((section, key))
            if spec is None or not _is_worker_field(spec):
                continue
            value = _coerce(spec, raw)
            base_val = base.get(section, {}).get(key, spec.default)
            if value != base_val:  # only real overrides are stored
                overlay.setdefault(section, {})[key] = value
    store.set_worker_settings(scope, overlay)
    return overlay


def settings_ui_payload(cfg_dir: Path, scope: str = "global", store=None) -> dict:
    """Grouped, typed field list + current values for the settings form.

    scope "global" shows all fields (server config). A worker id shows only the
    worker-applicable fields, with each field's current effective value and
    whether it is overridden for that worker.
    """
    data = _configured(cfg_dir)
    worker_scope = _is_worker_scope(scope)
    overlay = store.get_worker_settings(scope) if (worker_scope and store) else {}
    secrets = {} if worker_scope else _load_secrets(cfg_dir)
    groups = []
    for title, fields in SCHEMA:
        out = []
        for f in fields:
            if worker_scope and not _is_worker_field(f):
                continue
            base = data.get(f.section, {}).get(f.key, f.default)
            entry = {
                "section": f.section, "key": f.key, "type": f.type,
                "label": f.label, "help": f.help, "choices": list(f.choices),
            }
            if worker_scope:
                overridden = f.section in overlay and f.key in overlay[f.section]
                entry["value"] = overlay[f.section][f.key] if overridden else base
                entry["overridden"] = overridden
                entry["base"] = base
                entry["env"] = entry["env_value"] = None
            else:
                env_value = secrets.get(f.env) if f.env else None
                entry["value"] = base
                entry["env"] = f.env
                entry["env_value"] = env_value or None
            out.append(entry)
        if out:
            groups.append({"title": title, "fields": out})

    payload = {"groups": groups, "scope": "worker" if worker_scope else "global"}
    if not worker_scope:
        payload["overlay_file"] = str(_overlay_path(cfg_dir))
        workers: set[str] = set()
        durable = bool(store and getattr(store, "durable", False))
        if store:
            try:
                workers |= {w.worker_id for w in store.list_workers()}
                workers |= set(store.all_worker_settings().keys())
            except Exception:
                pass
        payload["workers"] = sorted(workers)
        payload["durable"] = durable
    return payload
