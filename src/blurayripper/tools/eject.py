"""Cross-platform optical-disc eject.

Used to pop the disc after ripping so the user can swap in the next one while
the (much longer) encode runs.
"""

from __future__ import annotations

import subprocess
import sys


def eject(device: str | None, method: str = "auto") -> None:
    """Eject the optical disc. ``device`` is an OS device hint:
    'F:' on Windows, '/dev/sr0' on Linux, a disk id on macOS (may be None).

    ``method`` (Windows only): "auto" tries scsi -> ioctl -> shell -> mci; or
    force one of "scsi"/"ioctl"/"shell"/"mci" (for diagnosing a stubborn drive).
    """
    if sys.platform.startswith("win"):
        _eject_windows(device, method)
    elif sys.platform == "darwin":
        # drutil ejects the default optical tray; ignore failure if none.
        subprocess.run(["drutil", "tray", "eject"], check=False)
    else:
        cmd = ["eject"]
        if device:
            cmd.append(device)
        subprocess.run(cmd, check=False)


def _eject_windows(device: str | None, method: str = "auto") -> None:
    """Eject a data optical disc on Windows.

    "auto" tries, in order: the DeviceIoControl eject (opened read-only, so no
    admin needed), the Shell eject verb (what Explorer does), then SCSI START
    STOP UNIT and MCI as last resorts (SCSI needs admin). Forcing a single method
    is for diagnosing a stubborn drive.
    """
    raw = (device or "").strip().rstrip("\\/")  # "F:", "F:\", or "\\.\CdRom0"
    # MakeMKV may report a drive letter ("F:") or a device path ("\\.\CdRom0").
    # SCSI/IOCTL take a device path; Shell/MCI need a drive letter.
    if raw.startswith("\\\\"):
        dev_path: str | None = raw
        letter: str | None = None
    elif ":" in raw:
        dev_path = rf"\\.\{raw}"
        letter = raw
    else:
        dev_path = letter = None

    methods: dict[str, tuple[str | None, object]] = {
        "scsi": (dev_path, _eject_scsi),
        "ioctl": (dev_path, _eject_ioctl),
        "shell": (letter, _eject_shell),
        "mci": (letter or raw, _eject_mci),
    }
    if method != "auto":
        arg, fn = methods.get(method, (None, None))
        if fn is None:
            raise OSError(f"unknown eject method '{method}'")
        if not arg:
            raise OSError(f"method '{method}' needs a "
                          f"{'drive letter (e.g. F:)' if method in ('shell', 'mci') else 'device'}")
        fn(arg)  # type: ignore[operator]
        return

    errors: list[str] = []
    # ioctl (read-only) and shell work without admin; scsi/mci are last resorts.
    for name in ("ioctl", "shell", "scsi", "mci"):
        arg, fn = methods[name]
        if not arg:
            continue
        try:
            fn(arg)  # type: ignore[operator]
            return
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    raise OSError("; ".join(errors) or "no eject method available")


def _eject_scsi(dev_path: str) -> None:
    """Physically eject via the SCSI START STOP UNIT command (LoadEject=1,
    Start=0). The most reliable way to actually move the tray."""
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
    IOCTL_SCSI_PASS_THROUGH_DIRECT = 0x4D014
    SCSI_IOCTL_DATA_UNSPECIFIED = 2
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class SPTD(ctypes.Structure):
        _fields_ = [
            ("Length", ctypes.c_ushort),
            ("ScsiStatus", ctypes.c_ubyte),
            ("PathId", ctypes.c_ubyte),
            ("TargetId", ctypes.c_ubyte),
            ("Lun", ctypes.c_ubyte),
            ("CdbLength", ctypes.c_ubyte),
            ("SenseInfoLength", ctypes.c_ubyte),
            ("DataIn", ctypes.c_ubyte),
            ("DataTransferLength", ctypes.c_ulong),
            ("TimeOutValue", ctypes.c_ulong),
            ("DataBuffer", ctypes.c_void_p),
            ("SenseInfoOffset", ctypes.c_ulong),
            ("Cdb", ctypes.c_ubyte * 16),
        ]

    handle = k32.CreateFileW(
        dev_path, GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None,
    )
    if not handle or handle == INVALID_HANDLE_VALUE:
        raise OSError(f"open {dev_path} failed (err {ctypes.get_last_error()})")
    try:
        sptd = SPTD()
        sptd.Length = ctypes.sizeof(SPTD)
        sptd.CdbLength = 6
        sptd.DataIn = SCSI_IOCTL_DATA_UNSPECIFIED
        sptd.TimeOutValue = 15
        sptd.Cdb[0] = 0x1B  # START STOP UNIT
        sptd.Cdb[4] = 0x02  # LoadEject=1, Start=0 -> eject
        returned = wintypes.DWORD(0)
        ok = k32.DeviceIoControl(
            handle, IOCTL_SCSI_PASS_THROUGH_DIRECT,
            ctypes.byref(sptd), ctypes.sizeof(sptd),
            ctypes.byref(sptd), ctypes.sizeof(sptd),
            ctypes.byref(returned), None,
        )
        if not ok:
            raise OSError(f"scsi eject failed on {dev_path} (err {ctypes.get_last_error()})")
    finally:
        k32.CloseHandle(handle)


def _eject_ioctl(dev_path: str) -> None:
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

    # Open with GENERIC_READ only: IOCTL_STORAGE_EJECT_MEDIA needs FILE_READ_ACCESS
    # (a 0-access handle gets ACCESS_DENIED on the IOCTL), but GENERIC_WRITE is
    # what forces an elevation requirement (err 5 on open). Read-only is the
    # no-admin sweet spot. Lock/dismount below need write and just no-op.
    handle = k32.CreateFileW(
        dev_path, GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None,
    )
    if not handle or handle == INVALID_HANDLE_VALUE:
        raise OSError(f"open {dev_path} failed (err {ctypes.get_last_error()})")

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
            raise OSError(f"eject ioctl failed on {dev_path} (err {ctypes.get_last_error()})")
    finally:
        k32.CloseHandle(handle)


def _eject_shell(drive: str) -> None:
    # Windows Shell eject verb (Namespace(17) == ssfDRIVES) — exactly what
    # Explorer's right-click "Eject" does, and works without admin. The verb is
    # actually named "E&ject" (with the menu accelerator), so InvokeVerb('Eject')
    # silently no-ops; enumerate the verbs and fire the eject one via DoIt().
    ps = (
        f"$i=(New-Object -comObject Shell.Application).Namespace(17).ParseName('{drive}');"
        "if($i -eq $null){throw 'drive not found'};"
        "$v=$i.Verbs()|?{($_.Name -replace '&','') -match 'Eject'};"
        "if($v){$v.DoIt();Start-Sleep -Milliseconds 1800}else{throw 'no eject verb'}"
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
