"""Cross-platform optical-disc eject.

Used to pop the disc after ripping so the user can swap in the next one while
the (much longer) encode runs.
"""

from __future__ import annotations

import subprocess
import sys


def eject(device: str | None) -> None:
    """Eject the optical disc. ``device`` is an OS device hint:
    'F:' on Windows, '/dev/sr0' on Linux, a disk id on macOS (may be None)."""
    if sys.platform.startswith("win"):
        _eject_windows(device)
    elif sys.platform == "darwin":
        # drutil ejects the default optical tray; ignore failure if none.
        subprocess.run(["drutil", "tray", "eject"], check=False)
    else:
        cmd = ["eject"]
        if device:
            cmd.append(device)
        subprocess.run(cmd, check=False)


def _eject_windows(device: str | None) -> None:
    import ctypes

    winmm = ctypes.windll.winmm  # type: ignore[attr-defined]

    def mci(command: str) -> None:
        winmm.mciSendStringW(command, None, 0, None)

    drive = (device or "").rstrip("\\/")
    alias = "blurayripper_eject"
    if drive:
        mci(f"open {drive} type cdaudio alias {alias}")
        mci(f"set {alias} door open")
        mci(f"close {alias}")
    else:
        mci("set CDAudio door open")
