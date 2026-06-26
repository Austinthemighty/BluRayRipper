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
}

# The binary basename to look for on PATH for each tool key.
_TOOL_PATH_NAMES: dict[str, list[str]] = {
    "makemkvcon": ["makemkvcon", "makemkvcon64"],
    "handbrakecli": ["HandBrakeCLI"],
    "mkvmerge": ["mkvmerge"],
    "mkvextract": ["mkvextract"],
    "subtitleedit": ["seconv", "SubtitleEdit"],
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


@dataclass
class SubtitleSettings:
    fix_common_errors: bool = True
    remove_text_for_hi: bool = False


@dataclass
class Settings:
    tools: ToolPaths
    storage: StorageSettings
    encode: EncodeSettings
    languages: LanguageSettings
    subtitles: SubtitleSettings
    min_length_seconds: int = 120
    tmdb_api_key: str | None = None
    metadata_language: str = "en-US"
    config_dir: Path = DEFAULT_CONFIG_DIR


def _load_secrets(config_dir: Path) -> dict[str, str]:
    """Parse config/secrets.env into a dict; also honors process env vars."""
    secrets: dict[str, str] = {}
    env_file = config_dir / "secrets.env"
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            secrets[key.strip()] = value.strip().strip('"').strip("'")
    # Process environment overrides the file.
    for key in ("TMDB_API_KEY",):
        if os.environ.get(key):
            secrets[key] = os.environ[key]
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

    tools_cfg = data.get("tools", {})
    tools = ToolPaths(
        makemkvcon=autodetect_tool("makemkvcon", tools_cfg.get("makemkvcon") or None),
        handbrakecli=autodetect_tool("handbrakecli", tools_cfg.get("handbrakecli") or None),
        mkvmerge=autodetect_tool("mkvmerge", tools_cfg.get("mkvmerge") or None),
        mkvextract=autodetect_tool("mkvextract", tools_cfg.get("mkvextract") or None),
        subtitleedit=autodetect_tool("subtitleedit", tools_cfg.get("subtitleedit") or None),
        subtitleedit_runner=(tools_cfg.get("subtitleedit_runner") or None),
    )

    storage_cfg = data.get("storage", {})
    storage = StorageSettings(
        staging_dir=_resolve_path(storage_cfg.get("staging_dir", "./staging"), REPO_ROOT),
        output_dir=_resolve_path(storage_cfg.get("output_dir", "./output"), REPO_ROOT),
        movies_subdir=storage_cfg.get("movies_subdir", "Movies"),
        tv_subdir=storage_cfg.get("tv_subdir", "TV Shows"),
    )

    encode_cfg = data.get("encode", {})
    encode = EncodeSettings(
        container=encode_cfg.get("container", "mp4"),
        preset_file=_resolve_path(encode_cfg.get("preset_file", "config/presets.json"), REPO_ROOT),
        preset_name=encode_cfg.get("preset_name", "Plex H265 1080p"),
    )

    lang_cfg = data.get("languages", {})
    languages = LanguageSettings(
        keep=list(lang_cfg.get("keep", ["eng"])) or ["eng"],
        default=lang_cfg.get("default", "eng"),
    )

    sub_cfg = data.get("subtitles", {})
    subtitles = SubtitleSettings(
        fix_common_errors=bool(sub_cfg.get("fix_common_errors", True)),
        remove_text_for_hi=bool(sub_cfg.get("remove_text_for_hi", False)),
    )

    scan_cfg = data.get("scan", {})
    meta_cfg = data.get("metadata", {})
    secrets = _load_secrets(cfg_dir)

    return Settings(
        tools=tools,
        storage=storage,
        encode=encode,
        languages=languages,
        subtitles=subtitles,
        min_length_seconds=int(scan_cfg.get("min_length_seconds", 120)),
        tmdb_api_key=secrets.get("TMDB_API_KEY") or None,
        metadata_language=meta_cfg.get("language", "en-US"),
        config_dir=cfg_dir,
    )
