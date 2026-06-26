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
    """Eject a data optical disc via DeviceIoControl(IOCTL_STORAGE_EJECT_MEDIA).

    This works for Blu-ray/DVD data discs, unlike the MCI ``cdaudio`` API which
    targets audio CDs and silently fails on data drives. Falls back to MCI if a
    drive letter isn't available.
    """
    import ctypes
    from ctypes import wintypes

    drive = (device or "").rstrip("\\/")  # e.g. "F:"
    if not drive or ":" not in drive:
        # No drive letter — best-effort MCI on the default optical drive.
        winmm = ctypes.windll.winmm  # type: ignore[attr-defined]
        if winmm.mciSendStringW("set CDAudio door open wait", None, 0, None) != 0:
            raise OSError("MCI eject failed and no drive letter was available.")
        return

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x1
    FILE_SHARE_WRITE = 0x2
    OPEN_EXISTING = 3
    IOCTL_STORAGE_EJECT_MEDIA = 0x2D4808
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    handle = k32.CreateFileW(
        rf"\\.\{drive}", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
        None, OPEN_EXISTING, 0, None,
    )
    if not handle or handle == INVALID_HANDLE_VALUE:
        raise OSError(f"Could not open {drive} to eject (error {ctypes.get_last_error()}).")
    try:
        returned = wintypes.DWORD(0)
        ok = k32.DeviceIoControl(
            handle, IOCTL_STORAGE_EJECT_MEDIA, None, 0, None, 0,
            ctypes.byref(returned), None,
        )
        if not ok:
            raise OSError(f"Eject IOCTL failed on {drive} (error {ctypes.get_last_error()}).")
    finally:
        k32.CloseHandle(handle)
