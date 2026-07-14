"""dovi_tool wrapper: extract a Dolby Vision RPU from an HEVC stream, convert its
profile, and inject it into a re-encoded HEVC stream.

DV survives a re-encode only if the RPU (per-scene tone-mapping metadata, indexed
by frame) is pulled off the source, then re-injected into the new encode — which
must keep the exact frame count/order (a plain CRF encode does). Disc DV is
usually profile 7 (dual-layer); we convert to profile 8.1 (single-layer,
HDR10-compatible) so Plex and TVs play it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..config import Settings
from . import process


def mode_for_profile(profile: int | None) -> int:
    """dovi_tool -m mode to normalise a source profile to 8.1:
    7 (dual-layer) -> 2, 5 (IPT-PQ) -> 3, 8/other -> 0 (keep as-is)."""
    if profile == 7:
        return 2
    if profile == 5:
        return 3
    return 0


class DoviTool:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def available(self) -> bool:
        # Verify the binary actually exists on THIS machine — a configured path is
        # trusted without a check elsewhere, so a shared config can name a
        # dovi_tool that isn't installed on this worker. Attempting DV then would
        # fail mid-encode with a WinError 2 spawn error; better to skip to HDR10.
        p = self._settings.tools.dovitool
        return bool(p) and shutil.which(p) is not None

    def _bin(self) -> str:
        return self._settings.tools.require("dovitool")

    def extract_rpu_command(self, hevc_in: Path, rpu_out: Path, profile: int | None) -> list[str]:
        cmd = [self._bin()]
        mode = mode_for_profile(profile)
        if mode:
            cmd += ["-m", str(mode)]  # convert profile to 8.1 while extracting
        cmd += ["extract-rpu", str(hevc_in), "-o", str(rpu_out)]
        return cmd

    def inject_rpu_command(self, hevc_in: Path, rpu_in: Path, hevc_out: Path) -> list[str]:
        return [self._bin(), "inject-rpu", "-i", str(hevc_in),
                "--rpu-in", str(rpu_in), "-o", str(hevc_out)]

    async def extract_rpu(self, hevc_in: Path, rpu_out: Path, profile: int | None) -> Path:
        await process.run(self.extract_rpu_command(hevc_in, rpu_out, profile))
        return rpu_out

    async def inject_rpu(self, hevc_in: Path, rpu_in: Path, hevc_out: Path) -> Path:
        await process.run(self.inject_rpu_command(hevc_in, rpu_in, hevc_out))
        return hevc_out
