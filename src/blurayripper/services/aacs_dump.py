"""Find and keep the AACS dump MakeMKV writes for a disc it can't decrypt.

When MakeMKV meets a (usually UHD) disc whose volume key it doesn't know, it
writes a ``.tgz`` dump into its own data directory and the disc fails to open.
Emailing that dump to svq@makemkv.com is how the key gets added — but the file
lands in a directory most people never look in, and the next failing disc writes
another one beside it.

So: after a failed scan, look for a dump that appeared *during* that scan and
copy it somewhere the user actually goes, named after the disc.

Detection is by timestamp, not by matching MakeMKV's message text — the wording
varies between versions and isn't something this project can pin down.

Nothing here sends anything. The dump is the user's to email.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

SUBMIT_TO = "svq@makemkv.com"

# app_DataDir = "..."  in MakeMKV's settings.conf overrides the default location.
_DATA_DIR_RE = re.compile(r'^\s*app_DataDir\s*=\s*"?([^"\r\n]+)"?\s*$', re.MULTILINE)


def default_data_dir() -> Path:
    """MakeMKV's per-user data directory for this OS."""
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA")
        return Path(base) / "MakeMKV" if base else Path.home() / "AppData/Roaming/MakeMKV"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "MakeMKV"
    return Path.home() / ".MakeMKV"


def data_dir(settings_conf: Path | None = None) -> Path:
    """Where MakeMKV keeps its data, honouring an app_DataDir override.

    That override is why the forum thread's "it's in the data directory" isn't
    enough on its own — the directory is configurable."""
    conf = settings_conf or (default_data_dir() / "settings.conf")
    try:
        m = _DATA_DIR_RE.search(Path(conf).read_text(encoding="utf-8", errors="replace"))
        if m and m.group(1).strip():
            return Path(m.group(1).strip()).expanduser()
    except OSError:
        pass
    return default_data_dir()


def find_new_dump(directory: Path, since: float) -> Path | None:
    """The newest ``.tgz`` in ``directory`` modified at/after ``since``.

    Bounded by the scan's own start time so an old dump from a previous disc is
    never mistaken for this one's."""
    try:
        dumps = [p for p in Path(directory).glob("*.tgz")
                 if p.is_file() and p.stat().st_mtime >= since - 1]
    except OSError:
        return None
    return max(dumps, key=lambda p: p.stat().st_mtime) if dumps else None


def _safe_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", (name or "").strip())
    return cleaned.strip(". ") or "disc"


def collect(dump: Path, dest_dir: Path, disc_name: str | None = None) -> Path:
    """Copy the dump somewhere findable, named after the disc.

    Copied, not moved: MakeMKV may still want its own copy, and a failed copy
    must not destroy the only record of an unreadable disc."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_name(disc_name) if disc_name else Path(dump).stem
    dest = dest_dir / f"{stem}_{Path(dump).name}"
    shutil.copy2(dump, dest)
    return dest


def advice(dest: Path) -> str:
    """What to tell the user, once a dump has been kept."""
    return (
        f"AACS dump saved: {dest} — MakeMKV doesn't know this disc's volume key. "
        f"Email that file to {SUBMIT_TO} and the key is usually added within "
        "12-48h, after which the disc rips normally. Nothing has been sent."
    )
