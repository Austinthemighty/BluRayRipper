"""HandBrakeCLI wrapper: H.265 transcode with preset import, language-filtered
audio, and ``--json`` progress parsing.

HandBrake's ``--json`` mode prints labelled JSON blocks on stdout, e.g.::

    Progress: {
     "State": "WORKING",
     "Working": { "Progress": 0.42, "ETASeconds": 90 }
    }

A block can span many lines, so we accumulate from ``Label: {`` until the
braces balance (string-aware), then parse.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from . import process

ProgressHandler = Callable[["EncodeProgress"], None] | Callable[["EncodeProgress"], Awaitable[None]]


@dataclass
class EncodeProgress:
    state: str
    fraction: float | None = None
    eta_seconds: int | None = None
    fps: float | None = None  # average encode rate (RateAvg) in frames/sec


class HandBrakeJsonParser:
    """Stateful, string-aware accumulator for HandBrake's labelled JSON blocks."""

    def __init__(self) -> None:
        self._label: str | None = None
        self._buf: list[str] = []
        self._depth = 0
        self._in_string = False
        self._escape = False

    def feed_line(self, line: str) -> tuple[str, str] | None:
        """Feed one stdout line. Returns (label, json_text) when a block completes."""
        if self._label is None:
            # Look for a "Label: {" block start.
            stripped = line.strip()
            if ": {" in stripped and stripped.endswith("{"):
                label, _, _rest = stripped.partition(": {")
                self._label = label.strip()
                self._buf = ["{"]
                self._depth = 0
                self._in_string = False
                self._escape = False
                self._count_braces("{")
            return None

        self._buf.append(line)
        self._count_braces(line)
        if self._depth <= 0:
            label = self._label
            text = "\n".join(self._buf)
            self._label = None
            self._buf = []
            return (label, text)
        return None

    def _count_braces(self, text: str) -> None:
        for ch in text:
            if self._escape:
                self._escape = False
                continue
            if ch == "\\":
                self._escape = True
                continue
            if ch == '"':
                self._in_string = not self._in_string
                continue
            if self._in_string:
                continue
            if ch == "{":
                self._depth += 1
            elif ch == "}":
                self._depth -= 1


def _progress_from_block(obj: dict) -> EncodeProgress:
    state = obj.get("State", "")
    fraction = None
    eta = None
    fps = None
    for key in ("Working", "Muxing", "Scanning", "WorkDone"):
        sub = obj.get(key)
        if isinstance(sub, dict):
            if "Progress" in sub:
                fraction = sub.get("Progress")
            if "ETASeconds" in sub:
                eta = sub.get("ETASeconds")
            if "RateAvg" in sub:
                fps = sub.get("RateAvg")
            break
    return EncodeProgress(state=state, fraction=fraction, eta_seconds=eta, fps=fps)


class HandBrake:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _effective_grain(self, tune_grain: bool | None) -> bool:
        return self._settings.encode.tune_grain if tune_grain is None else tune_grain

    def build_command(
        self, src: Path, dst: Path, *, tune_grain: bool | None = None,
        start: float | None = None, duration: float | None = None,
    ) -> list[str]:
        enc = self._settings.encode
        langs = self._settings.languages
        # Put the default language first so HandBrake flags it as default.
        ordered = [langs.default] + [l for l in langs.keep if l != langs.default]
        cmd = [
            self._settings.tools.require("handbrakecli"),
            "--preset-import-file",
            str(enc.preset_file),
            "-Z",
            enc.preset_name,
            "-i",
            str(src),
            "-o",
            str(dst),
            "--json",
            "--audio-lang-list",
            ",".join(ordered),
            # "main": only the first (primary) track per language; "all": every
            # kept-language track including commentaries.
            "--first-audio" if self._settings.audio.main_only else "--all-audio",
            # Override any subtitle handling in the preset. We extract PGS from
            # the ripped MKV and OCR to .srt sidecars separately, so HandBrake
            # must not select, embed, or burn-in subtitles (a preset with
            # SubtitleBurnBehavior would otherwise hard-burn foreign subs, which
            # cannot be turned off in Plex and breaks the sidecar workflow).
            "--subtitle",
            "none",
        ]
        if self._effective_grain(tune_grain):
            cmd += ["--encoder-tune", "grain"]
        if start is not None:
            cmd += ["--start-at", f"seconds:{start:.0f}"]
        if duration is not None:
            cmd += ["--stop-at", f"seconds:{duration:.0f}"]
        return cmd

    async def encode(
        self,
        src: Path,
        dst: Path,
        *,
        on_progress: ProgressHandler | None = None,
        tune_grain: bool | None = None,
    ) -> Path:
        import json as _json

        dst.parent.mkdir(parents=True, exist_ok=True)
        parser = HandBrakeJsonParser()

        async def handle(line: str) -> None:
            block = parser.feed_line(line)
            if block is None:
                return
            label, text = block
            if label != "Progress":
                return
            try:
                obj = _json.loads(text)
            except _json.JSONDecodeError:
                return
            if on_progress is not None:
                prog = _progress_from_block(obj)
                res = on_progress(prog)
                if res is not None and hasattr(res, "__await__"):
                    await res

        await process.run(self.build_command(src, dst, tune_grain=tune_grain), on_line=handle)
        return dst

    async def encode_clip(
        self, src: Path, dst: Path, *, start: float, duration: float, tune_grain: bool
    ) -> Path:
        """Encode a short segment (no audio/subs) for grain previews."""
        dst.parent.mkdir(parents=True, exist_ok=True)
        cmd = self.build_command(src, dst, tune_grain=tune_grain, start=start, duration=duration)
        # Drop audio for a quick, small preview clip.
        cmd += ["-a", "none"]
        await process.run(cmd)
        return dst
