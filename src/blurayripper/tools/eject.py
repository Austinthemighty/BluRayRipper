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
        _run_or_raise(["drutil", "tray", "eject"], "eject")
    else:
        cmd = ["eject"]
        if device:
            cmd.append(device)
        _run_or_raise(cmd, "eject")


def _run_or_raise(cmd: list[str], what: str) -> None:
    """Run a tray command and FAIL on a non-zero exit.

    check=False discarded the exit status, so a busy drive, a wrong /dev/srN or
    a jammed tray returned normally — and the pipeline then logged "Ejected
    disc — safe to load the next one" and pushed a notification saying so, while
    the tray never moved. Only Windows could ever report a failure.
    """
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Could not {what}: '{cmd[0]}' is not installed on this machine."
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise RuntimeError(
            f"Could not {what} ({' '.join(cmd)} exited {result.returncode})"
            + (f": {detail[-1]}" if detail else "")
        )


def close_tray(device: str | None, method: str = "auto") -> None:
    """Close (load) the optical drive tray. ``method`` (Windows) forces one of
    "scsi"/"ioctl"/"mci"; "auto" tries scsi -> ioctl -> mci."""
    if sys.platform.startswith("win"):
        _close_windows(device, method)
    elif sys.platform == "darwin":
        _run_or_raise(["drutil", "tray", "close"], "close the tray")
    else:
        cmd = ["eject", "-t"]  # -t closes the tray on Linux
        if device:
            cmd.append(device)
        _run_or_raise(cmd, "close the tray")


def _close_windows(device: str | None, method: str = "auto") -> None:
    dev_path, letter = _win_paths(device)
    methods: dict[str, tuple[str | None, object]] = {
        "scsi": (dev_path, lambda d: _scsi_start_stop(d, start=True)),
        "ioctl": (dev_path, _close_ioctl),
        "mci": (letter or (device or "").strip().rstrip("\\/"), _close_mci),
    }
    if method != "auto":
        arg, fn = methods.get(method, (None, None))
        if fn is None:
            raise OSError(f"unknown close method '{method}'")
        if not arg:
            raise OSError(f"method '{method}' needs a drive/device")
        fn(arg)  # type: ignore[operator]
        return
    errors: list[str] = []
    for name in ("scsi", "ioctl", "mci"):
        arg, fn = methods[name]
        if not arg:
            continue
        try:
            fn(arg)  # type: ignore[operator]
            return
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    raise OSError("; ".join(errors) or "no close method available")


def _close_ioctl(dev_path: str) -> None:
    """Close the tray via IOCTL_STORAGE_LOAD_MEDIA (read-only handle, no admin)."""
    import ctypes
    from ctypes import wintypes

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
    IOCTL_STORAGE_LOAD_MEDIA = 0x002D480C
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    handle = k32.CreateFileW(
        dev_path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
        None, OPEN_EXISTING, 0, None,
    )
    if not handle or handle == INVALID_HANDLE_VALUE:
        raise OSError(f"open {dev_path} failed (err {ctypes.get_last_error()})")
    try:
        returned = wintypes.DWORD(0)
        if not k32.DeviceIoControl(
            handle, IOCTL_STORAGE_LOAD_MEDIA, None, 0, None, 0,
            ctypes.byref(returned), None,
        ):
            raise OSError(f"load-media ioctl failed on {dev_path} (err {ctypes.get_last_error()})")
    finally:
        k32.CloseHandle(handle)


def _close_mci(drive: str) -> None:
    import ctypes

    winmm = ctypes.windll.winmm  # type: ignore[attr-defined]
    alias = "blurayripper_close"
    winmm.mciSendStringW(f"open {drive} type cdaudio alias {alias}", None, 0, None)
    rc = winmm.mciSendStringW(f"set {alias} door closed wait", None, 0, None)
    winmm.mciSendStringW(f"close {alias}", None, 0, None)
    if rc:
        raise OSError(f"MCI returned {rc}")


def _win_paths(device: str | None) -> tuple[str | None, str | None]:
    r"""(device_path, drive_letter) from a "F", "F:" or "\\.\CdRom0" hint."""
    raw = (device or "").strip().rstrip("\\/")
    if raw.startswith("\\\\"):
        return raw, None
    if len(raw) == 1 and raw.isalpha():
        # A letter typed without its colon. Without this it matched neither
        # branch, so ioctl, shell and scsi were all skipped and the command that
        # exists to try four back-ends reported one MCI failure.
        raw = f"{raw}:"
    if ":" in raw:
        return rf"\\.\{raw}", raw
    return None, None


def _eject_windows(device: str | None, method: str = "auto") -> None:
    """Eject a data optical disc on Windows.

    "auto" tries, in order: the DeviceIoControl eject (opened read-only, so no
    admin needed), the Shell eject verb (what Explorer does), then SCSI START
    STOP UNIT and MCI as last resorts (SCSI needs admin). Forcing a single method
    is for diagnosing a stubborn drive.
    """
    # MakeMKV may report a drive letter ("F:") or a device path ("\\.\CdRom0").
    # SCSI/IOCTL take a device path; Shell/MCI need a drive letter. Shared with
    # _close_windows so a fix to the normalisation reaches both.
    dev_path, letter = _win_paths(device)

    methods: dict[str, tuple[str | None, object]] = {
        "scsi": (dev_path, _eject_scsi),
        "ioctl": (dev_path, _eject_ioctl),
        "shell": (letter, _eject_shell),
        # "" is a real argument to MCI, not a missing one: it addresses MCI's
        # built-in default CDAudio device. Passing the device path instead (what
        # this used to do) cannot work — MCI does not accept \\.\CdRom0 — and
        # skipping MCI entirely meant `blurayripper eject` with no drive
        # argument, and any rip whose drive device is unknown, could never
        # succeed on Windows even though this back-end needs no drive name.
        "mci": (letter or "", _eject_mci),
    }
    if method != "auto":
        arg, fn = methods.get(method, (None, None))
        if fn is None:
            raise OSError(f"unknown eject method '{method}'")
        if arg is None:
            raise OSError(f"method '{method}' needs a "
                          f"{'drive letter (e.g. F:)' if method in ('shell', 'mci') else 'device'}")
        fn(arg)  # type: ignore[operator]
        return

    errors: list[str] = []
    # ioctl (read-only) and shell work without admin; scsi/mci are last resorts.
    for name in ("ioctl", "shell", "scsi", "mci"):
        arg, fn = methods[name]
        if arg is None:
            continue
        try:
            fn(arg)  # type: ignore[operator]
            return
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    raise OSError("; ".join(errors) or "no eject method available")


def _eject_scsi(dev_path: str) -> None:
    """Physically eject via SCSI START STOP UNIT (LoadEject=1, Start=0)."""
    _scsi_start_stop(dev_path, start=False)


def _scsi_start_stop(dev_path: str, start: bool) -> None:
    """SCSI START STOP UNIT with LoadEject=1: start=False ejects (open tray),
    start=True loads (close tray). The most reliable way to move the tray."""
    op = "load" if start else "eject"
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
        sptd.Cdb[0] = 0x1B          # START STOP UNIT
        sptd.Cdb[4] = 0x03 if start else 0x02  # LoadEject=1; Start bit -> load/eject
        returned = wintypes.DWORD(0)
        ok = k32.DeviceIoControl(
            handle, IOCTL_SCSI_PASS_THROUGH_DIRECT,
            ctypes.byref(sptd), ctypes.sizeof(sptd),
            ctypes.byref(sptd), ctypes.sizeof(sptd),
            ctypes.byref(returned), None,
        )
        if not ok:
            raise OSError(f"scsi {op} failed on {dev_path} (err {ctypes.get_last_error()})")
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
