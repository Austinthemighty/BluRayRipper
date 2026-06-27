"""MKVToolNix wrappers: identify tracks (mkvmerge -J) and extract PGS (mkvextract)."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from . import process


@dataclass
class SubtitleTrack:
    track_id: int
    codec: str
    language: str | None  # ISO 639-2/B 3-letter where available
    is_pgs: bool
    forced: bool = False
    sdh: bool = False  # hearing-impaired / SDH / closed-caption
    name: str | None = None
    default: bool = False


def parse_video_height(stdout: str) -> int | None:
    """Pure parser: first video track's pixel height from ``mkvmerge -J``."""
    data = json.loads(stdout)
    for track in data.get("tracks", []):
        if track.get("type") != "video":
            continue
        dims = track.get("properties", {}).get("pixel_dimensions", "")
        if "x" in dims:
            try:
                return int(dims.split("x")[1])
            except (ValueError, IndexError):
                return None
    return None


def parse_identify(stdout: str) -> list[SubtitleTrack]:
    """Pure parser for ``mkvmerge -J`` JSON output -> subtitle tracks.

    Captures forced / hearing-impaired flags (and infers them from the track
    name when the explicit flags are absent) so same-language tracks can be
    given distinct Plex sidecar names instead of colliding.
    """
    data = json.loads(stdout)
    tracks: list[SubtitleTrack] = []
    for track in data.get("tracks", []):
        if track.get("type") != "subtitles":
            continue
        props = track.get("properties", {})
        codec = track.get("codec", "") or ""
        lang = props.get("language") or props.get("language_ietf")
        if lang in (None, "und", "", "zxx"):
            lang = None
        name = props.get("track_name")
        low = (name or "").lower()
        forced = bool(props.get("forced_track", False)) or "forced" in low
        sdh = (
            bool(props.get("flag_hearing_impaired", False))
            or "sdh" in low
            or "hearing" in low
            or low.strip() in ("cc", "closed caption", "closed captions")
        )
        tracks.append(
            SubtitleTrack(
                track_id=int(track["id"]),
                codec=codec,
                language=lang.lower() if lang else None,
                is_pgs="PGS" in codec.upper(),
                forced=forced,
                sdh=sdh,
                name=name,
                default=bool(props.get("default_track", False)),
            )
        )
    return tracks


class MkvTools:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def identify_subtitles(self, mkv_path: Path) -> list[SubtitleTrack]:
        cmd = [self._settings.tools.require("mkvmerge"), "-J", str(mkv_path)]
        result = await asyncio.wait_for(process.run(cmd), timeout=120)
        return parse_identify("\n".join(result.stdout_lines))

    async def video_height(self, mkv_path: Path) -> int | None:
        cmd = [self._settings.tools.require("mkvmerge"), "-J", str(mkv_path)]
        result = await asyncio.wait_for(process.run(cmd), timeout=120)
        return parse_video_height("\n".join(result.stdout_lines))

    async def extract_track(
        self, mkv_path: Path, track_id: int, out_path: Path,
        *, on_progress: Callable[[float], None] | None = None,
    ) -> Path:
        """Extract one track to ``out_path`` (e.g. a .sup for a PGS subtitle).

        Forwards mkvextract's "Progress: N%" so callers can show movement and
        tell a slow extraction apart from a hung one."""
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            self._settings.tools.require("mkvextract"),
            "tracks",
            str(mkv_path),
            f"{track_id}:{out_path}",
        ]

        def handle(line: str) -> None:
            if on_progress is not None:
                m = re.search(r"Progress:\s*(\d+)%", line)
                if m:
                    on_progress(int(m.group(1)) / 100)

        # Bounded so a stuck extraction (e.g. a flaky NAS handle) fails instead
        # of hanging the job forever; generous enough for large UHD files.
        await asyncio.wait_for(process.run(cmd, on_line=handle), timeout=1800)
        return out_path
