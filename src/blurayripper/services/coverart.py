"""Extract cover art / poster images that discs carry in the clear.

Blu-ray/UHD: BDMV/META/DL/*.jpg (+ bdmt_<lang>.xml naming the disc + thumbnails).
DVD:         JACKET_P/*.jpg  (jacket pictures, when present).

These files are NOT part of the AACS/CSS-encrypted video, so they read straight
off the mounted disc (or a ripped BDMV/VIDEO_TS folder) — no MakeMKV or
decryption needed. Placed as ``poster.jpg`` in the movie folder, Plex picks it
up automatically.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from xml.etree import ElementTree

from ..models import DiscInfo

_IMG_EXTS = {".jpg", ".jpeg", ".png"}
_WIN_DRIVE = re.compile(r"^[A-Za-z]:$")


def _child_ci(parent: Path, name: str) -> Path | None:
    """Case-insensitive child lookup — disc filesystems are usually UPPERCASE and
    case matters on Linux."""
    direct = parent / name
    if direct.exists():
        return direct
    try:
        for c in parent.iterdir():
            if c.name.lower() == name.lower():
                return c
    except OSError:
        pass
    return None


def _dir_path(root: Path, *parts: str) -> Path | None:
    cur = root
    for part in parts:
        nxt = _child_ci(cur, part)
        if nxt is None or not nxt.is_dir():
            return None
        cur = nxt
    return cur


def _images_in(d: Path) -> list[Path]:
    try:
        return [p for p in d.iterdir() if p.is_file() and p.suffix.lower() in _IMG_EXTS]
    except OSError:
        return []


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def disc_root(disc: DiscInfo) -> Path | None:
    """Filesystem root to read cover art from, or None if unsupported (an ISO or
    a Linux optical device that isn't a plain path)."""
    src = disc.source or ""
    if src.startswith("file:"):
        return Path(src[len("file:"):])
    if src.startswith("iso:"):
        return None  # reading inside an ISO isn't supported yet
    dev = (disc.drive_device or "").strip()
    if _WIN_DRIVE.match(dev):
        return Path(dev + "\\")
    return None


def find_cover_images(root: Path) -> list[Path]:
    """Every cover-art image the disc carries, largest (best quality) first."""
    found: list[Path] = []
    meta = _dir_path(root, "BDMV", "META", "DL")
    if meta:
        found += _images_in(meta)
    jacket = _dir_path(root, "JACKET_P")
    if jacket:
        found += _images_in(jacket)
    found.sort(key=_size, reverse=True)
    return found


def disc_title(root: Path) -> str | None:
    """Disc title from BDMV/META/DL/bdmt_<lang>.xml, best-effort."""
    meta = _dir_path(root, "BDMV", "META", "DL")
    if not meta:
        return None
    try:
        xmls = sorted(p for p in meta.iterdir()
                      if p.name.lower().startswith("bdmt_") and p.suffix.lower() == ".xml")
    except OSError:
        return None
    for xml in xmls:
        try:
            tree = ElementTree.parse(xml)
        except (ElementTree.ParseError, OSError):
            continue
        for el in tree.iter():
            if el.tag.rsplit("}", 1)[-1] == "name" and el.text and el.text.strip():
                return el.text.strip()
    return None


def extract_poster(root: Path, dest_dir: Path, *, name: str = "poster.jpg") -> Path | None:
    """Copy the largest disc cover image into ``dest_dir`` as ``name`` (converting
    the extension label only, not the bytes). Returns the written path, or None if
    the disc carries no cover art."""
    imgs = find_cover_images(root)
    if not imgs:
        return None
    src = imgs[0]
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Keep the source extension so a .png isn't mislabeled .jpg.
    dest = dest_dir / (Path(name).stem + src.suffix.lower())
    shutil.copy2(src, dest)
    return dest
