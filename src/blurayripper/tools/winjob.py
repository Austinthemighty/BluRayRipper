"""Windows Job Object: kill child processes when the parent dies.

serve/worker spawn MakeMKV, HandBrake and SubtitleEdit as children. On Windows a
dead parent does NOT reap its children, so a crash or power-off orphans them and
they pile up (exhausting the commit limit). Assigning the current process to a
job object with KILL_ON_JOB_CLOSE makes the OS terminate every child the instant
the parent exits — the proper fix, versus the startup best-effort reap.

Everything here is best-effort and a no-op off Windows.
"""

from __future__ import annotations

import sys

_JOB_HANDLE = None  # module-global so the handle isn't GC'd (which would fire the kill)


def enable_kill_on_close() -> bool:
    """Put this process in a kill-on-close job so its children die with it.
    Returns True if applied. No-op (False) off Windows or on any failure."""
    global _JOB_HANDLE
    if not sys.platform.startswith("win") or _JOB_HANDLE is not None:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in
                        ("ReadOperationCount", "WriteOperationCount",
                         "OtherOperationCount", "ReadTransferCount",
                         "WriteTransferCount", "OtherTransferCount")]

        class EXT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC),
                ("IoInfo", IO),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        h = k32.CreateJobObjectW(None, None)
        if not h:
            return False
        info = EXT()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(
                h, 9, ctypes.byref(info), ctypes.sizeof(info)):  # 9 = ExtendedLimitInformation
            return False
        if not k32.AssignProcessToJobObject(h, k32.GetCurrentProcess()):
            return False  # e.g. already in a non-nestable job (pre-Win8)
        _JOB_HANDLE = h  # keep alive for the process lifetime
        return True
    except Exception:
        return False
