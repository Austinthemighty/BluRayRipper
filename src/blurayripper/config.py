"""Configuration loading and external-tool auto-detection.

Settings come from ``config/settings.toml`` (non-secret) and ``config/secrets.env``
(API keys). Any tool path left blank is auto-detected from PATH and common
per-OS install locations. This module is the cross-platform seam: everything
else in the app receives resolved absolute paths and never guesses.
"""

from __future__ import annotations

import os
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Repo root = two levels up from this file (src/blurayripper/config.py).
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = REPO_ROOT / "config"


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _is_macos() -> bool:
    return sys.platform == "darwin"


# Common install locations to probe when a tool is not on PATH. Each entry is a
# list of candidate absolute paths, tried in order, per platform.
_TOOL_CANDIDATES: dict[str, dict[str, list[str]]] = {
    "makemkvcon": {
        "win32": [
            r"C:\Program Files (x86)\MakeMKV\makemkvcon64.exe",
            r"C:\Program Files\MakeMKV\makemkvcon64.exe",
        ],
        "darwin": ["/Applications/MakeMKV.app/Contents/MacOS/makemkvcon"],
        "linux": ["/usr/bin/makemkvcon", "/usr/local/bin/makemkvcon"],
    },
    "handbrakecli": {
        "win32": [
            r"C:\Program Files\HandBrake\HandBrakeCLI.exe",
            r"C:\Program Files (x86)\HandBrake\HandBrakeCLI.exe",
        ],
        "darwin": ["/usr/local/bin/HandBrakeCLI", "/opt/homebrew/bin/HandBrakeCLI"],
        "linux": ["/usr/bin/HandBrakeCLI", "/usr/local/bin/HandBrakeCLI"],
    },
    "mkvmerge": {
        "win32": [r"C:\Program Files\MKVToolNix\mkvmerge.exe"],
        "darwin": [
            "/Applications/MKVToolNix.app/Contents/MacOS/mkvmerge",
            "/opt/homebrew/bin/mkvmerge",
        ],
        "linux": ["/usr/bin/mkvmerge", "/usr/local/bin/mkvmerge"],
    },
    "mkvextract": {
        "win32": [r"C:\Program Files\MKVToolNix\mkvextract.exe"],
        "darwin": [
            "/Applications/MKVToolNix.app/Contents/MacOS/mkvextract",
            "/opt/homebrew/bin/mkvextract",
        ],
        "linux": ["/usr/bin/mkvextract", "/usr/local/bin/mkvextract"],
    },
    "subtitleedit": {
        "win32": [
            r"C:\Program Files\Subtitle Edit\SubtitleEdit.exe",
            r"C:\Program Files (x86)\Subtitle Edit\SubtitleEdit.exe",
        ],
        "darwin": ["/Applications/Subtitle Edit.app/Contents/MacOS/SubtitleEdit"],
        "linux": ["/usr/bin/SubtitleEdit", "/usr/local/bin/SubtitleEdit"],
    },
    "ffmpeg": {
        "win32": [r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"],
        "darwin": ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"],
        "linux": ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"],
    },
}

# The binary basename to look for on PATH for each tool key.
_TOOL_PATH_NAMES: dict[str, list[str]] = {
    "makemkvcon": ["makemkvcon", "makemkvcon64"],
    "handbrakecli": ["HandBrakeCLI"],
    "mkvmerge": ["mkvmerge"],
    "mkvextract": ["mkvextract"],
    "subtitleedit": ["seconv", "SubtitleEdit"],
    "ffmpeg": ["ffmpeg"],
}


def _platform_key() -> str:
    if _is_windows():
        return "win32"
    if _is_macos():
        return "darwin"
    return "linux"


def autodetect_tool(tool_key: str, configured: str | None) -> str | None:
    """Resolve a tool to an absolute path.

    Order: explicit config value -> PATH lookup -> known install locations.
    Returns ``None`` if the tool cannot be found (the caller decides whether
    that tool is required for the requested operation).
    """
    if configured:
        return configured
    for name in _TOOL_PATH_NAMES.get(tool_key, []):
        found = shutil.which(name)
        if found:
            return found
    for candidate in _TOOL_CANDIDATES.get(tool_key, {}).get(_platform_key(), []):
        if Path(candidate).exists():
            return candidate
    return None


@dataclass
class ToolPaths:
    makemkvcon: str | None = None
    handbrakecli: str | None = None
    mkvmerge: str | None = None
    mkvextract: str | None = None
    subtitleedit: str | None = None
    subtitleedit_runner: str | None = None
    ffmpeg: str | None = None

    def require(self, tool_key: str) -> str:
        value = getattr(self, tool_key, None)
        if not value:
            raise ToolNotFoundError(tool_key)
        return value


class ToolNotFoundError(RuntimeError):
    """Raised when a required external tool cannot be located."""

    def __init__(self, tool_key: str) -> None:
        super().__init__(
            f"Required tool '{tool_key}' was not found. Install it or set its path "
            f"under [tools] in config/settings.toml."
        )
        self.tool_key = tool_key


@dataclass
class LanguageSettings:
    keep: list[str] = field(default_factory=lambda: ["eng"])
    default: str = "eng"

    def is_kept(self, lang: str | None) -> bool:
        if lang is None:
            return False
        return lang.lower() in {l.lower() for l in self.keep}


@dataclass
class StorageSettings:
    staging_dir: Path
    output_dir: Path
    movies_subdir: str = "Movies"
    tv_subdir: str = "TV Shows"

    @property
    def movies_root(self) -> Path:
        return self.output_dir / self.movies_subdir

    @property
    def tv_root(self) -> Path:
        return self.output_dir / self.tv_subdir


@dataclass
class EncodeSettings:
    container: str = "mp4"  # "mp4" or "mkv"
    preset_file: Path = DEFAULT_CONFIG_DIR / "presets.json"
    preset_name: str = "Plex H265 1080p"
    tune_grain: bool = False  # default; can be overridden per job in the UI
    low_priority: bool = True  # run encodes below normal priority so rips stay fast
    # Deinterlace: "auto" applies HandBrake's adaptive decomb to SD/DVD sources
    # (<= deinterlace_max_height); "on" forces it; "off" never. Decomb only
    # touches frames that are actually combed, so it's safe on mixed content.
    deinterlace: str = "auto"
    deinterlace_max_height: int = 576  # NTSC 480 / PAL 576 -> DVD-era SD
    # UHD via ffmpeg+libx265 (preserves HDR10 metadata HandBrake mangles). When
    # True, titles >= uhd_min_height encode with ffmpeg; everything else stays on
    # HandBrake. Needs ffmpeg installed.
    ffmpeg_uhd: bool = False
    uhd_min_height: int = 2160
    ffmpeg_crf: int = 18            # x265 quality (lower = better/bigger); ~18 for UHD
    ffmpeg_preset: str = "medium"  # x265 preset
    ffmpeg_audio: str = "copy"     # "copy" (keep originals, incl. TrueHD/DTS-HD) or "aac"


@dataclass
class SubtitleSettings:
    fix_common_errors: bool = True
    remove_text_for_hi: bool = False
    ocr_concurrency: int = 4  # how many subtitle tracks to OCR at once
    keep_extracted: bool = True  # cache extracted .sup subs so re-OCR needs no re-rip
    extract_concurrency: int = 2  # how many titles may read the rip + extract subs at once
    # Kill a single track's OCR if it runs longer than this (SubtitleEdit can hang
    # on an error dialog and otherwise pile up holding memory). Per track.
    ocr_timeout_seconds: int = 3600
    # When set, each placed SRT is also copied here with a text sheet pre-filling
    # the OpenSubtitles manual-upload form. Blank/None = off.
    upload_export_dir: Path | None = None


@dataclass
class QueueSettings:
    # Total encode "budget". UHD encodes cost 2 units, others 1, so the default
    # of 3 allows 1 UHD + 1 lighter disc, or up to 3 DVDs, but never 2 UHDs.
    encode_capacity: int = 3


@dataclass
class ClusterSettings:
    # When True, the rip machine rips then ENQUEUES jobs (state "ready") for
    # `blurayripper worker` processes to encode, instead of encoding inline.
    use_workers: bool = False
    worker_poll_seconds: int = 5      # how often a worker checks for a job
    heartbeat_seconds: int = 15       # how often a busy worker proves it's alive
    stale_after_seconds: int = 180    # reclaim a job if its worker goes quiet
    # When clustered, the rip box encodes a job itself (instead of enqueuing it)
    # if every title is <= this height; taller (e.g. UHD 2160p) jobs go to the
    # workers. 0 = never encode locally (rip box only rips). 1080 = do <=1080p.
    local_encode_max_height: int = 0
    # When True, subtitle extraction + OCR move OFF the rip box onto the workers
    # (they run right before the encode). The rip box then only rips + resolves
    # grain, so restarting `serve` never kills in-flight extraction/OCR/encode.
    # Requires use_workers. Best paired with local_encode_max_height=0.
    worker_does_subs: bool = False


@dataclass
class AudioSettings:
    # "all" keeps every track in the kept languages (incl. commentaries);
    # "main" keeps only the first (primary) track per language.
    tracks: str = "all"

    @property
    def main_only(self) -> bool:
        return self.tracks.strip().lower() == "main"


@dataclass
class Settings:
    tools: ToolPaths
    storage: StorageSettings
    encode: EncodeSettings
    languages: LanguageSettings
    subtitles: SubtitleSettings
    audio: AudioSettings = field(default_factory=AudioSettings)
    queue: QueueSettings = field(default_factory=QueueSettings)
    cluster: ClusterSettings = field(default_factory=ClusterSettings)
    auto_eject: bool = True
    auto_open: bool = True          # auto-scan a disc when one is inserted
    drive_poll_seconds: int = 15    # how often the drive watcher checks
    # Copy the disc's own cover art (BDMV/META or DVD JACKET_P) to the output
    # folder as poster.jpg for Plex. Read before ripping (disc still in the drive).
    extract_cover_art: bool = True
    # At serve startup, kill leftover tool processes (SubtitleEdit/MakeMKV/
    # HandBrake) orphaned by a prior crash or power-off, before any jobs run.
    cleanup_stale_tools: bool = True
    # How many jobs may prep (rip+subs+grain) concurrently. Caps the recovery
    # stampede when a restart resumes many in-flight jobs at once.
    prep_concurrency: int = 2
    # Automatically re-queue a failed job up to this many times (exponential
    # backoff) before leaving it FAILED for a manual retry. 0 = never.
    auto_retry_max: int = 2
    # Webhook URL (ntfy-style plain-text POST) to push a notification when a job
    # needs input or fails. Blank = off. Put it in secrets.env if it has a token.
    notify_url: str | None = None
    min_length_seconds: int = 120
    web_host: str = "127.0.0.1"  # set to 0.0.0.0 to allow LAN access (no auth!)
    web_port: int = 8723
    database_url: str | None = None  # Postgres URL for crash recovery; None = in-memory
    tmdb_api_key: str | None = None
    metadata_language: str = "en-US"
    write_nfo: bool = True  # write a Kodi/Plex .nfo sidecar next to each output
    opensubtitles_api_key: str | None = None
    opensubtitles_username: str | None = None
    opensubtitles_password: str | None = None
    config_dir: Path = DEFAULT_CONFIG_DIR


def _load_secrets(config_dir: Path) -> dict[str, str]:
    """Parse config/secrets.env into a dict; also honors process env vars.

    secrets.env is gitignored, so machine-specific values (API keys, tool paths,
    storage folders) placed here survive `git pull` instead of being clobbered
    by the tracked settings.toml.
    """
    secrets: dict[str, str] = {}
    env_file = config_dir / "secrets.env"
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            secrets[key.strip()] = value.strip().strip('"').strip("'")
    # Process environment overrides the file (any BLURAYRIPPER_* var + API keys).
    for key, value in os.environ.items():
        if key == "TMDB_API_KEY" or key.startswith(("BLURAYRIPPER_", "OPENSUBTITLES_")):
            secrets[key] = value
    return secrets


def _resolve_path(value: str, base: Path) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p)


def load_settings(config_dir: Path | None = None) -> Settings:
    """Load and resolve all settings, auto-detecting tool paths."""
    cfg_dir = (config_dir or DEFAULT_CONFIG_DIR).resolve()
    settings_file = cfg_dir / "settings.toml"
    data: dict = {}
    if settings_file.exists():
        with settings_file.open("rb") as fh:
            data = tomllib.load(fh)

    # Load gitignored env overrides first so they win over the tracked TOML.
    secrets = _load_secrets(cfg_dir)

    def _override(env_key: str, toml_value) -> str | None:
        return secrets.get(env_key) or (toml_value or None)

    def _bool_override(env_key: str, toml_value, default: bool) -> bool:
        # secrets.env values are strings, so parse them explicitly ("false" is a
        # truthy string!). Precedence: secrets.env/env > settings.toml > default.
        raw = secrets.get(env_key)
        if raw is not None and raw.strip() != "":  # blank env = unset, fall through
            return raw.strip().lower() in ("1", "true", "yes", "on")
        if toml_value is not None:
            return bool(toml_value)
        return default

    tools_cfg = data.get("tools", {})
    tools = ToolPaths(
        makemkvcon=autodetect_tool(
            "makemkvcon", _override("BLURAYRIPPER_MAKEMKVCON", tools_cfg.get("makemkvcon"))),
        handbrakecli=autodetect_tool(
            "handbrakecli", _override("BLURAYRIPPER_HANDBRAKECLI", tools_cfg.get("handbrakecli"))),
        mkvmerge=autodetect_tool(
            "mkvmerge", _override("BLURAYRIPPER_MKVMERGE", tools_cfg.get("mkvmerge"))),
        mkvextract=autodetect_tool(
            "mkvextract", _override("BLURAYRIPPER_MKVEXTRACT", tools_cfg.get("mkvextract"))),
        subtitleedit=autodetect_tool(
            "subtitleedit", _override("BLURAYRIPPER_SUBTITLEEDIT", tools_cfg.get("subtitleedit"))),
        subtitleedit_runner=_override(
            "BLURAYRIPPER_SUBTITLEEDIT_RUNNER", tools_cfg.get("subtitleedit_runner")),
        ffmpeg=autodetect_tool("ffmpeg", _override("BLURAYRIPPER_FFMPEG", tools_cfg.get("ffmpeg"))),
    )

    storage_cfg = data.get("storage", {})
    storage = StorageSettings(
        staging_dir=_resolve_path(
            _override("BLURAYRIPPER_STAGING_DIR", storage_cfg.get("staging_dir")) or "./staging",
            REPO_ROOT),
        output_dir=_resolve_path(
            _override("BLURAYRIPPER_OUTPUT_DIR", storage_cfg.get("output_dir")) or "./output",
            REPO_ROOT),
        movies_subdir=storage_cfg.get("movies_subdir", "Movies"),
        tv_subdir=storage_cfg.get("tv_subdir", "TV Shows"),
    )

    encode_cfg = data.get("encode", {})
    encode = EncodeSettings(
        container=encode_cfg.get("container", "mp4"),
        preset_file=_resolve_path(encode_cfg.get("preset_file", "config/presets.json"), REPO_ROOT),
        preset_name=encode_cfg.get("preset_name", "Plex H265 1080p"),
        tune_grain=bool(encode_cfg.get("tune_grain", False)),
        low_priority=bool(encode_cfg.get("low_priority", True)),
        deinterlace=str(encode_cfg.get("deinterlace", "auto")),
        deinterlace_max_height=int(encode_cfg.get("deinterlace_max_height", 576)),
        ffmpeg_uhd=bool(encode_cfg.get("ffmpeg_uhd", False)),
        uhd_min_height=int(encode_cfg.get("uhd_min_height", 2160)),
        ffmpeg_crf=int(encode_cfg.get("ffmpeg_crf", 18)),
        ffmpeg_preset=str(encode_cfg.get("ffmpeg_preset", "medium")),
        ffmpeg_audio=str(encode_cfg.get("ffmpeg_audio", "copy")),
    )

    lang_cfg = data.get("languages", {})
    languages = LanguageSettings(
        keep=list(lang_cfg.get("keep", ["eng"])) or ["eng"],
        default=lang_cfg.get("default", "eng"),
    )

    sub_cfg = data.get("subtitles", {})
    _upload_dir = _override("BLURAYRIPPER_SUB_UPLOAD_DIR", sub_cfg.get("upload_export_dir"))
    subtitles = SubtitleSettings(
        fix_common_errors=bool(sub_cfg.get("fix_common_errors", True)),
        remove_text_for_hi=bool(sub_cfg.get("remove_text_for_hi", False)),
        ocr_concurrency=int(sub_cfg.get("ocr_concurrency", 4)),
        keep_extracted=bool(sub_cfg.get("keep_extracted", True)),
        extract_concurrency=int(sub_cfg.get("extract_concurrency", 2)),
        ocr_timeout_seconds=int(sub_cfg.get("ocr_timeout_seconds", 3600)),
        upload_export_dir=_resolve_path(_upload_dir, REPO_ROOT) if _upload_dir else None,
    )

    audio_cfg = data.get("audio", {})
    audio = AudioSettings(tracks=audio_cfg.get("tracks", "all"))

    queue_cfg = data.get("queue", {})
    queue = QueueSettings(encode_capacity=int(queue_cfg.get("encode_capacity", 3)))

    cluster_cfg = data.get("cluster", {})
    cluster = ClusterSettings(
        use_workers=_bool_override(
            "BLURAYRIPPER_USE_WORKERS", cluster_cfg.get("use_workers"), False),
        worker_poll_seconds=int(cluster_cfg.get("worker_poll_seconds", 5)),
        heartbeat_seconds=int(cluster_cfg.get("heartbeat_seconds", 15)),
        stale_after_seconds=int(cluster_cfg.get("stale_after_seconds", 180)),
        local_encode_max_height=int(
            _override("BLURAYRIPPER_LOCAL_ENCODE_MAX_HEIGHT",
                      cluster_cfg.get("local_encode_max_height")) or 0),
        worker_does_subs=_bool_override(
            "BLURAYRIPPER_WORKER_DOES_SUBS", cluster_cfg.get("worker_does_subs"), False),
    )

    scan_cfg = data.get("scan", {})
    disc_cfg = data.get("disc", {})
    serve_cfg = data.get("serve", {})
    meta_cfg = data.get("metadata", {})
    db_cfg = data.get("database", {})
    database_url = _override("BLURAYRIPPER_DATABASE_URL", db_cfg.get("url"))
    web_cfg = data.get("web", {})

    return Settings(
        tools=tools,
        storage=storage,
        encode=encode,
        languages=languages,
        subtitles=subtitles,
        audio=audio,
        queue=queue,
        cluster=cluster,
        auto_eject=bool(disc_cfg.get("auto_eject", True)),
        auto_open=bool(disc_cfg.get("auto_open", True)),
        drive_poll_seconds=int(disc_cfg.get("poll_seconds", 15)),
        extract_cover_art=bool(disc_cfg.get("extract_cover_art", True)),
        cleanup_stale_tools=bool(serve_cfg.get("cleanup_stale_tools", True)),
        prep_concurrency=int(serve_cfg.get("prep_concurrency", 2)),
        auto_retry_max=int(serve_cfg.get("auto_retry_max", 2)),
        notify_url=_override("BLURAYRIPPER_NOTIFY_URL", serve_cfg.get("notify_url")),
        min_length_seconds=int(scan_cfg.get("min_length_seconds", 120)),
        web_host=web_cfg.get("host", "127.0.0.1"),
        web_port=int(web_cfg.get("port", 8723)),
        database_url=database_url,
        tmdb_api_key=secrets.get("TMDB_API_KEY") or None,
        metadata_language=meta_cfg.get("language", "en-US"),
        write_nfo=bool(meta_cfg.get("write_nfo", True)),
        opensubtitles_api_key=secrets.get("OPENSUBTITLES_API_KEY") or None,
        opensubtitles_username=secrets.get("OPENSUBTITLES_USERNAME") or None,
        opensubtitles_password=secrets.get("OPENSUBTITLES_PASSWORD") or None,
        config_dir=cfg_dir,
    )
