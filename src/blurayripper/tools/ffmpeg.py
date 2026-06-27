"""ffmpeg wrapper — used only to extract still frames for the grain preview."""

from __future__ import annotations

from pathlib import Path

from ..config import Settings
from . import process


class FFmpeg:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def available(self) -> bool:
        return bool(self._settings.tools.ffmpeg)

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
