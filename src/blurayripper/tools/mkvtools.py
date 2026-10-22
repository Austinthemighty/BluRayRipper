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
class ProbedFile:
    duration_seconds: int
    video_height: int | None
    audio_languages: list[str]
    subtitles: list["SubtitleTrack"]


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
    # Already text (SubRip), so readable without OCR. On a DVD this is the
    # closed captions, which MakeMKV converts to an SRT track.
    is_text: bool = False


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


def parse_video_fps(stdout: str) -> float | None:
    """First video track's frame rate from ``mkvmerge -J``, derived from its
    ``default_duration`` (nanoseconds per frame). Rounded to 3 dp so common
    rates read cleanly (23.976, 25.0, 29.97)."""
    data = json.loads(stdout)
    for track in data.get("tracks", []):
        if track.get("type") != "video":
            continue
        dd = track.get("properties", {}).get("default_duration")
        if dd:
            try:
                return round(1_000_000_000 / int(dd), 3)
            except (ValueError, ZeroDivisionError):
                return None
    return None


def parse_probe(stdout: str) -> ProbedFile:
    """Pure parser for ``mkvmerge -J`` -> duration, resolution, audio langs, subs."""
    data = json.loads(stdout)
    dur_ns = data.get("container", {}).get("properties", {}).get("duration")
    duration = int(dur_ns / 1_000_000_000) if dur_ns else 0
    audio: list[str] = []
    for track in data.get("tracks", []):
        if track.get("type") == "audio":
            lang = track.get("properties", {}).get("language")
            if lang and lang not in ("und", "zxx"):
                audio.append(lang.lower())
    return ProbedFile(
        duration_seconds=duration,
        video_height=parse_video_height(stdout),
        audio_languages=audio,
        subtitles=parse_identify(stdout),
    )


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
                is_text=(props.get("codec_id") == "S_TEXT/UTF8"
                         or "SUBRIP" in codec.upper()),
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

    async def mux_dovi(self, video_hevc: Path, audio_src: Path, out: Path) -> Path:
        """Mux a raw (DV-injected) HEVC video with the audio (+ chapters) from the
        original rip into an MKV. mkvmerge is DV-aware, so it signals the profile
        8.1 track correctly for Plex."""
        out.parent.mkdir(parents=True, exist_ok=True)
        cmd = [self._settings.tools.require("mkvmerge"), "-o", str(out),
               str(video_hevc), "--no-video", "--no-subtitles", str(audio_src)]
        await asyncio.wait_for(process.run(cmd), timeout=3600)
        # The last step of the Dolby Vision path. mkvmerge can exit 0 having
        # written a header-only file when the RPU-injected stream is truncated,
        # and the caller then MOVES that into the Plex library and marks the
        # title placed — a green job with no video. The DV path already falls
        # back to plain HDR10 on an exception, so raising here recovers the job
        # rather than failing it.
        if not out.exists() or out.stat().st_size == 0:
            raise RuntimeError(
                f"mkvmerge reported success but produced no usable file at {out}")
        return out

    async def video_dims(self, mkv_path: Path) -> tuple[int | None, float | None]:
        """(height, fps) from a single mkvmerge probe."""
        cmd = [self._settings.tools.require("mkvmerge"), "-J", str(mkv_path)]
        result = await asyncio.wait_for(process.run(cmd), timeout=120)
        stdout = "\n".join(result.stdout_lines)
        return parse_video_height(stdout), parse_video_fps(stdout)

    async def probe(self, mkv_path: Path) -> ProbedFile:
        cmd = [self._settings.tools.require("mkvmerge"), "-J", str(mkv_path)]
        result = await asyncio.wait_for(process.run(cmd), timeout=120)
        return parse_probe("\n".join(result.stdout_lines))

    async def extract_tracks(
        self, mkv_path: Path, specs: list[tuple[int, Path]],
        *, on_progress: Callable[[float], None] | None = None,
    ) -> None:
        """Extract several tracks in a SINGLE mkvextract pass (one read of the
        source file, not one per track). Forwards "Progress: N%"."""
        if not specs:
            return
        for _, out in specs:
            out.parent.mkdir(parents=True, exist_ok=True)
        cmd = [self._settings.tools.require("mkvextract"), "tracks", str(mkv_path)]
        cmd += [f"{tid}:{out}" for tid, out in specs]

        def handle(line: str) -> None:
            if on_progress is not None:
                m = re.search(r"Progress:\s*(\d+)%", line)
                if m:
                    on_progress(int(m.group(1)) / 100)

        # Bounded so a stuck extraction (e.g. a flaky NAS handle) fails instead
        # of hanging the job forever; generous enough for large UHD files.
        await asyncio.wait_for(process.run(cmd, on_line=handle), timeout=1800)

    async def extract_track(self, mkv_path: Path, track_id: int, out_path: Path) -> Path:
        await self.extract_tracks(mkv_path, [(track_id, out_path)])
        return out_path
