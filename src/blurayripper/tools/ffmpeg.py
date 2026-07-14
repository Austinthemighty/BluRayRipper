"""ffmpeg wrapper.

Two jobs: extract still frames for the grain preview, and (optionally) encode UHD
titles with libx265 while preserving HDR10 metadata — the one thing HandBrake
does poorly. Non-UHD stays on HandBrake.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from ..config import Settings
from ..services import hdr as hdr_mod
from . import process
from .handbrake import EncodeProgress


def _parse_time(value: str) -> float:
    """ffmpeg -progress 'out_time' (HH:MM:SS.ffffff) to seconds."""
    try:
        h, m, s = value.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except (ValueError, AttributeError):
        return 0.0


def _progress_handler(duration: float, on_progress):
    """Return an on_line handler that turns ffmpeg -progress output into
    EncodeProgress callbacks (same shape the HandBrake path emits)."""
    st = {"fps": None, "speed": None, "t": 0.0}

    async def handle(line: str) -> None:
        if "=" not in line:
            return
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k == "out_time":
            st["t"] = _parse_time(v)
        elif k == "fps":
            try:
                st["fps"] = float(v)
            except ValueError:
                pass
        elif k == "speed":
            try:
                st["speed"] = float(v.rstrip("x"))
            except ValueError:
                pass
        elif k == "progress" and on_progress is not None:
            frac = min(0.999, st["t"] / duration) if duration else None
            eta = (int((duration - st["t"]) / st["speed"])
                   if (duration and st["speed"]) else None)
            on_progress(EncodeProgress(
                state="done" if v == "end" else "working",
                fraction=1.0 if v == "end" else frac,
                eta_seconds=eta, fps=st["fps"]))
    return handle


class FFmpeg:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def available(self) -> bool:
        # Confirm the binary exists on this machine (a configured path is trusted
        # without a check), so the UHD path cleanly falls back to HandBrake rather
        # than failing to spawn a missing ffmpeg.
        p = self._settings.tools.ffmpeg
        return bool(p) and shutil.which(p) is not None

    def _ffprobe_bin(self) -> str:
        ff = self._settings.tools.require("ffmpeg")
        cand = Path(ff).with_name(Path(ff).name.replace("ffmpeg", "ffprobe"))
        if cand.exists():
            return str(cand)
        return shutil.which("ffprobe") or str(cand)

    async def extract_frame(self, video: Path, out_png: Path, at_seconds: float) -> Path:
        out_png.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            self._settings.tools.require("ffmpeg"),
            "-y",
            "-ss", f"{at_seconds:.2f}",
            "-i", str(video),
            "-frames:v", "1",
            "-update", "1",   # required by ffmpeg 7.x for single fixed-name images
            str(out_png),
        ]
        await process.run(cmd)
        return out_png

    async def probe_video(self, video: Path) -> tuple[hdr_mod.HdrInfo, float]:
        """(HDR metadata, duration seconds) from ffprobe. Falls back to empty/0."""
        cmd = [
            self._ffprobe_bin(), "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", "-select_streams", "v:0",
            "-show_frames", "-read_intervals", "%+#1", str(video),
        ]
        result = await process.run(cmd, check=False)
        try:
            data = json.loads("\n".join(result.stdout_lines))
        except (ValueError, TypeError):
            return hdr_mod.HdrInfo(), 0.0
        info = hdr_mod.parse_hdr(data)
        try:
            duration = float(data.get("format", {}).get("duration") or 0)
        except (ValueError, TypeError):
            duration = 0.0
        return info, duration

    def build_encode_command(
        self, src: Path, dst: Path, *, hdr: hdr_mod.HdrInfo, crf: int, preset: str,
        audio_mode: str, tune_grain: bool,
    ) -> list[str]:
        """The ffmpeg libx265 command (pure, so it's unit-testable)."""
        extra = ["tune=grain"] if tune_grain else None
        cmd = [
            self._settings.tools.require("ffmpeg"), "-y", "-i", str(src),
            "-map", "0:v:0", "-map", "0:a?",           # video + all audio
            "-map_chapters", "0", "-map_metadata", "0",
            "-c:v", "libx265", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p10le",
        ]
        if hdr.colorprim:
            cmd += ["-color_primaries", hdr.colorprim]
        if hdr.transfer:
            cmd += ["-color_trc", hdr.transfer]
        if hdr.colormatrix:
            cmd += ["-colorspace", hdr.colormatrix]
        cmd += ["-x265-params", hdr_mod.x265_params(hdr, extra)]
        cmd += (["-c:a", "copy"] if audio_mode == "copy"
                else ["-c:a", "aac", "-b:a", "640k"])
        cmd += ["-progress", "pipe:1", "-nostats", str(dst)]
        return cmd

    async def encode(
        self, src: Path, dst: Path, *, on_progress=None, tune_grain: bool = False,
        low_priority: bool = True,
    ) -> Path:
        """Encode ``src`` -> ``dst`` (UHD/HDR path). Probes HDR metadata, encodes
        with libx265, and streams progress as EncodeProgress (same shape the
        HandBrake path uses)."""
        dst.parent.mkdir(parents=True, exist_ok=True)
        enc = self._settings.encode
        hdr, duration = await self.probe_video(src)
        cmd = self.build_encode_command(
            src, dst, hdr=hdr, crf=enc.ffmpeg_crf, preset=enc.ffmpeg_preset,
            audio_mode=enc.ffmpeg_audio, tune_grain=tune_grain)
        await process.run(cmd, on_line=_progress_handler(duration, on_progress),
                          low_priority=low_priority)
        if not dst.exists() or dst.stat().st_size == 0:
            raise RuntimeError(f"ffmpeg produced no output at {dst}")
        return dst

    async def extract_hevc(self, src: Path, out: Path) -> Path:
        """Copy the source video track to a raw Annex-B HEVC file (for dovi_tool)."""
        out.parent.mkdir(parents=True, exist_ok=True)
        cmd = [self._settings.tools.require("ffmpeg"), "-y", "-i", str(src),
               "-map", "0:v:0", "-c:v", "copy", "-f", "hevc", str(out)]
        await process.run(cmd)
        return out

    def build_hevc_encode_command(
        self, src: Path, dst: Path, *, hdr: hdr_mod.HdrInfo, crf: int, preset: str,
        tune_grain: bool,
    ) -> list[str]:
        """Video-only libx265 encode to a raw HEVC stream (for the DV pipeline)."""
        extra = ["tune=grain"] if tune_grain else None
        cmd = [
            self._settings.tools.require("ffmpeg"), "-y", "-i", str(src),
            "-map", "0:v:0", "-an", "-sn",
            "-c:v", "libx265", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p10le",
        ]
        if hdr.colorprim:
            cmd += ["-color_primaries", hdr.colorprim]
        if hdr.transfer:
            cmd += ["-color_trc", hdr.transfer]
        if hdr.colormatrix:
            cmd += ["-colorspace", hdr.colormatrix]
        cmd += ["-x265-params", hdr_mod.x265_params(hdr, extra)]
        cmd += ["-f", "hevc", "-progress", "pipe:1", "-nostats", str(dst)]
        return cmd

    async def encode_hevc(
        self, src: Path, out: Path, *, hdr: hdr_mod.HdrInfo, duration: float = 0.0,
        on_progress=None, tune_grain: bool = False, low_priority: bool = True,
    ) -> Path:
        """Re-encode video-only to raw HEVC (base for DV RPU injection)."""
        out.parent.mkdir(parents=True, exist_ok=True)
        enc = self._settings.encode
        cmd = self.build_hevc_encode_command(
            src, out, hdr=hdr, crf=enc.ffmpeg_crf, preset=enc.ffmpeg_preset,
            tune_grain=tune_grain)
        await process.run(cmd, on_line=_progress_handler(duration, on_progress),
                          low_priority=low_priority)
        if not out.exists() or out.stat().st_size == 0:
            raise RuntimeError(f"ffmpeg produced no HEVC output at {out}")
        return out
