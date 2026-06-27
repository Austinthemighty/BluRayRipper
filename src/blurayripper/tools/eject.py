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
    """Eject a data optical disc on Windows.

    Tries, in order: the DeviceIoControl lock/dismount/eject sequence (the
    reliable method for data Blu-ray/DVD discs), then the Shell 'Eject' verb,
    then MCI. Raises with the collected errors if all fail.
    """
    drive = (device or "").rstrip("\\/")  # e.g. "F:"
    errors: list[str] = []
    if drive and ":" in drive:
        for name, fn in (("ioctl", _eject_ioctl), ("shell", _eject_shell)):
            try:
                fn(drive)
                return
            except Exception as exc:
                errors.append(f"{name}: {exc}")
    try:
        _eject_mci(drive)
        return
    except Exception as exc:
        errors.append(f"mci: {exc}")
    raise OSError("; ".join(errors) or "no eject method available")


def _eject_ioctl(drive: str) -> None:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x1
    FILE_SHARE_WRITE = 0x2
    OPEN_EXISTING = 3
    FSCTL_LOCK_VOLUME = 0x00090018
    FSCTL_DISMOUNT_VOLUME = 0x00090020
    IOCTL_STORAGE_MEDIA_REMOVAL = 0x002D4804
    IOCTL_STORAGE_EJECT_MEDIA = 0x002D4808
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    handle = k32.CreateFileW(
        rf"\\.\{drive}", GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None,
    )
    if not handle or handle == INVALID_HANDLE_VALUE:
        raise OSError(f"open {drive} failed (err {ctypes.get_last_error()})")

    def ioctl(code: int, in_buf=None, in_len: int = 0) -> bool:
        returned = wintypes.DWORD(0)
        return bool(k32.DeviceIoControl(
            handle, code, in_buf, in_len, None, 0, ctypes.byref(returned), None))

    try:
        # Lock + dismount are best-effort (may fail if the FS isn't mounted).
        ioctl(FSCTL_LOCK_VOLUME)
        ioctl(FSCTL_DISMOUNT_VOLUME)
        # Allow media removal (PREVENT_MEDIA_REMOVAL{ BOOLEAN PreventMediaRemoval=0 }).
        ioctl(IOCTL_STORAGE_MEDIA_REMOVAL, (ctypes.c_byte * 1)(0), 1)
        if not ioctl(IOCTL_STORAGE_EJECT_MEDIA):
            raise OSError(f"eject ioctl failed on {drive} (err {ctypes.get_last_error()})")
    finally:
        k32.CloseHandle(handle)


def _eject_shell(drive: str) -> None:
    # Windows Shell 'Eject' verb (Namespace(17) == ssfDRIVES). Handles some
    # cases the IOCTL won't; fire-and-forget, so we can't fully verify it.
    ps = (
        f"$item=(New-Object -comObject Shell.Application).Namespace(17).ParseName('{drive}');"
        "if($item -eq $null){throw 'drive not found'};$item.InvokeVerb('Eject')"
    )
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise OSError(r.stderr.strip() or f"powershell exit {r.returncode}")


def _eject_mci(drive: str) -> None:
    import ctypes

    winmm = ctypes.windll.winmm  # type: ignore[attr-defined]
    alias = "blurayripper_eject"
    if drive:
        winmm.mciSendStringW(f"open {drive} type cdaudio alias {alias}", None, 0, None)
        rc = winmm.mciSendStringW(f"set {alias} door open wait", None, 0, None)
        winmm.mciSendStringW(f"close {alias}", None, 0, None)
    else:
        rc = winmm.mciSendStringW("set CDAudio door open wait", None, 0, None)
    if rc != 0:
        raise OSError(f"MCI returned {rc}")
