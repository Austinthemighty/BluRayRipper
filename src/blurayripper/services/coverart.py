"""Extract cover art / poster images that discs carry in the clear.

Blu-ray/UHD: BDMV/META/DL/*.jpg (+ bdmt_<lang>.xml naming the disc + thumbnails).
DVD:         JACKET_P/*.jpg  (jacket pictures, when present).

These files are NOT part of the AACS/CSS-encrypted video, so they read straight
off the mounted disc (or a ripped BDMV/VIDEO_TS folder) — no MakeMKV or
decryption needed. Placed as ``poster.jpg`` in the movie folder, Plex picks it
up automatically.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import struct
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from ..models import DiscInfo

_IMG_EXTS = {".jpg", ".jpeg", ".png"}
_WIN_DRIVE = re.compile(r"^[A-Za-z]:$")


def _child_ci(parent: Path, name: str) -> Path | None:
    """Case-insensitive child lookup — disc filesystems are usually UPPERCASE and
    case matters on Linux."""
    # exists() re-raises PermissionError (it only swallows ENOENT and friends),
    # so an intermediate directory without traverse rights — an SMB share, a
    # restrictively mounted disc — escaped from what is meant to be a lookup
    # that answers "no".
    try:
        direct = parent / name
        if direct.exists():
            return direct
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


def image_size(path: Path) -> tuple[int, int] | None:
    """(width, height) from a JPEG/PNG/GIF header, no Pillow dependency. None if
    it can't be determined."""
    try:
        with path.open("rb") as f:
            return _image_size_stream(f)
    except OSError:
        return None


def image_size_bytes(data: bytes) -> tuple[int, int] | None:
    import io
    return _image_size_stream(io.BytesIO(data))


def _image_size_stream(f) -> tuple[int, int] | None:
    try:
        head = f.read(24)
        if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24:
            w, h = struct.unpack(">II", head[16:24])
            return int(w), int(h)
        if head[:6] in (b"GIF87a", b"GIF89a"):
            w, h = struct.unpack("<HH", head[6:10])
            return int(w), int(h)
        if head[:2] == b"\xff\xd8":  # JPEG — scan for the SOF marker
            f.seek(2)
            while True:
                b = f.read(1)
                if not b:
                    return None
                if b != b"\xff":
                    continue
                marker = f.read(1)
                while marker == b"\xff":
                    marker = f.read(1)
                if not marker:
                    return None
                m = marker[0]
                if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7:
                    continue  # standalone markers carry no length
                ln = f.read(2)
                if len(ln) < 2:
                    return None
                seg = struct.unpack(">H", ln)[0]
                if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):  # SOFn
                    data = f.read(5)
                    if len(data) < 5:
                        return None
                    h, w = struct.unpack(">HH", data[1:5])
                    return int(w), int(h)
                f.seek(seg - 2, 1)
    except (OSError, struct.error):
        return None
    return None


def classify_images(root: Path) -> tuple[list[Path], list[Path]]:
    """Split disc images into (portraits, landscapes), each largest-file first.
    Portrait (h >= w) makes a poster; landscape (w > h) makes a backdrop/fanart.
    Images whose size can't be read are treated as landscape (disc thumbnails
    almost always are)."""
    portraits: list[Path] = []
    landscapes: list[Path] = []
    for p in find_cover_images(root):
        size = image_size(p)
        if size and size[1] >= size[0] and size[0] > 0:
            portraits.append(p)
        else:
            landscapes.append(p)
    return portraits, landscapes


@dataclass
class DiscArt:
    poster: Path | None = None   # best portrait, written as poster.<ext>
    fanart: Path | None = None   # best landscape, written as fanart.<ext>


def _make_writable(dest: Path) -> bool:
    """Add owner-write to ``dest`` (on Windows that clears the read-only
    attribute), keeping its other permission bits. True if the caller should
    retry a write that just failed.

    Overwriting art raises PermissionError even in a writable folder when the
    existing poster.jpg/fanart.jpg is read-only, and that is the normal case here
    rather than an exotic one: disc files are read-only and ``shutil.copy2``
    copies the mode across, so the FIRST extraction leaves art that no later run
    can replace. Plex and other taggers set the attribute on the Z: share too.
    ``metadata._replace_bytes`` already does this for TMDB-sourced art.
    """
    try:
        os.chmod(dest, os.stat(dest).st_mode | stat.S_IWUSR)
        return True
    except OSError:
        return False


def _copy_as(src: Path, dest_dir: Path, stem: str) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (stem + src.suffix.lower())  # keep real ext (.png vs .jpg)
    try:
        shutil.copy2(src, dest)
    except PermissionError:
        if not _make_writable(dest):
            raise
        shutil.copy2(src, dest)
    _make_writable(dest)  # don't hand the disc's read-only mode to the next run
    return dest


def _iso_images(iso_path: Path) -> list[tuple[str, bytes]]:
    """(name, bytes) for every cover image inside an ISO — best-effort via the
    optional ``pycdlib``. Returns [] if pycdlib isn't installed or the ISO can't
    be read (so callers degrade to no-art rather than failing)."""
    try:
        import pycdlib  # optional: pip install -e ".[iso]"
    except ImportError:
        return []
    import io

    out: list[tuple[str, bytes]] = []
    iso = pycdlib.PyCdlib()
    try:
        iso.open(str(iso_path))
    except Exception:
        return []
    try:
        # Blu-ray art lives on the UDF side; DVD JACKET_P may be on either.
        for kind, dirs in (("udf", ["/BDMV/META/DL", "/JACKET_P"]),
                           ("iso", ["/JACKET_P"])):
            for d in dirs:
                try:
                    children = list(iso.list_children(**{f"{kind}_path": d}))
                except Exception:
                    continue
                for child in children:
                    try:
                        if child.is_dir():
                            continue
                        ident = child.file_identifier().decode("utf-8", "replace")
                    except Exception:
                        continue
                    name = ident.split(";")[0]  # strip ISO9660 ";1" version
                    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                    if ext not in ("jpg", "jpeg", "png"):
                        continue
                    buf = io.BytesIO()
                    try:
                        iso.get_file_from_iso_fp(
                            buf, **{f"{kind}_path": d.rstrip("/") + "/" + ident})
                    except Exception:
                        continue
                    if buf.getvalue():
                        out.append((name, buf.getvalue()))
    finally:
        try:
            iso.close()
        except Exception:
            pass
    return out


def _write_bytes(item: tuple[str, bytes], dest_dir: Path, stem: str) -> Path:
    name, data = item
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (stem + (Path(name).suffix.lower() or ".jpg"))
    try:
        dest.write_bytes(data)
    except PermissionError:  # same read-only destination as _copy_as
        if not _make_writable(dest):
            raise
        dest.write_bytes(data)
    return dest


def extract_art_from_iso(iso_path: Path, dest_dir: Path, *, poster: bool = True,
                         fanart: bool = True) -> DiscArt:
    """Cover art from inside an ISO (best-effort; needs pycdlib). Same
    poster/fanart split as extract_art."""
    portraits: list[tuple[str, bytes]] = []
    landscapes: list[tuple[str, bytes]] = []
    for name, data in _iso_images(Path(iso_path)):
        size = image_size_bytes(data)
        (portraits if (size and size[1] >= size[0] and size[0] > 0)
         else landscapes).append((name, data))
    portraits.sort(key=lambda x: len(x[1]), reverse=True)
    landscapes.sort(key=lambda x: len(x[1]), reverse=True)
    art = DiscArt()
    if poster and portraits:
        art.poster = _write_bytes(portraits[0], dest_dir, "poster")
    if fanart and landscapes:
        art.fanart = _write_bytes(landscapes[0], dest_dir, "fanart")
    return art


def extract_art(root: Path, dest_dir: Path, *, poster: bool = True,
                fanart: bool = True) -> DiscArt:
    """Copy the disc's best portrait to poster.jpg and best landscape to
    fanart.jpg (Plex's backdrop). Most Blu-ray disc thumbnails are landscape, so
    typically only fanart is produced. Returns what was written."""
    portraits, landscapes = classify_images(root)
    art = DiscArt()
    if poster and portraits:
        art.poster = _copy_as(portraits[0], dest_dir, "poster")
    if fanart and landscapes:
        art.fanart = _copy_as(landscapes[0], dest_dir, "fanart")
    return art
