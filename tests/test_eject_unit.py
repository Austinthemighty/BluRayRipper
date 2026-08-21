"""Unit coverage for cross-platform disc eject / tray close.

Auto-eject is what lets the user swap the next disc in while the (much longer)
encode runs, so every decision *around* the syscalls matters: which platform
branch runs, how a MakeMKV drive hint ("F:", "F:\\", "\\\\.\\CdRom0", None) is
normalised, which fallback runs next when one method fails, and whether a total
failure is reported instead of silently doing nothing.

The syscalls themselves cannot run here (no optical drive, and no Win32 at all),
so ``ctypes``/``subprocess`` are stubbed at the module boundary and the tests
assert on what the module *asks the OS to do* — the device path it opens, the
access mask it opens with, the IOCTL codes and SCSI CDB it sends, the argv it
spawns — plus what it does with the answer.
"""

import ctypes as _ctypes
import sys
import types

import pytest

from blurayripper.tools import eject as ej

_INVALID_HANDLE = _ctypes.c_void_p(-1).value

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_DISMOUNT_VOLUME = 0x00090020
IOCTL_STORAGE_MEDIA_REMOVAL = 0x002D4804
IOCTL_STORAGE_EJECT_MEDIA = 0x002D4808
IOCTL_STORAGE_LOAD_MEDIA = 0x002D480C
IOCTL_SCSI_PASS_THROUGH_DIRECT = 0x4D014


# --- boundary stubs -----------------------------------------------------


class _FakeSubprocess:
    """Stands in for the module-global ``subprocess``; records every argv."""

    def __init__(self, result=None):
        self.runs = []
        self._result = result or types.SimpleNamespace(returncode=0, stderr="")

    def run(self, cmd, **kw):
        self.runs.append((list(cmd), kw))
        return self._result

    @property
    def argvs(self):
        return [cmd for cmd, _kw in self.runs]


class _Ref:
    """Stand-in for ``ctypes.byref`` that keeps the referent readable, so a
    test can inspect the SCSI command block the module filled in."""

    def __init__(self, obj):
        self.obj = obj


class _WinFn:
    """A stubbed WinDLL export. Real ctypes function pointers accept
    ``.restype``/``.argtypes`` assignment, so this must too."""

    def __init__(self, impl):
        self._impl = impl
        self.restype = None
        self.argtypes = None

    def __call__(self, *args):
        return self._impl(*args)


class _K32:
    """Fake kernel32: canned CreateFileW handle + per-IOCTL-code results."""

    def __init__(self, handle=0x1234, ioctl_results=None):
        self.handle = handle
        self.opens = []      # (path, desired_access, share_mode, disposition)
        self.ioctls = []     # (handle, code, in_buf, in_len)
        self.closed = []
        self._results = dict(ioctl_results or {})
        self.CreateFileW = _WinFn(self._create)
        self.DeviceIoControl = _WinFn(self._dio)
        self.CloseHandle = _WinFn(self._close)

    def _create(self, path, access, share, sec, disposition, flags, template):
        self.opens.append((path, access, share, disposition))
        return self.handle

    def _dio(self, handle, code, in_buf, in_len, out_buf, out_len, returned, ov):
        self.ioctls.append((handle, code, in_buf, in_len))
        return self._results.get(code, 1)

    def _close(self, handle):
        self.closed.append(handle)
        return 1

    @property
    def ioctl_codes(self):
        return [code for _h, code, _b, _l in self.ioctls]


class _Winmm:
    """Fake winmm: records the MCI command strings and answers with rc codes."""

    def __init__(self, results=None):
        self.commands = []
        self._results = list(results or [])

    def mciSendStringW(self, command, buf, size, cb):
        self.commands.append(command)
        if self._results:
            return self._results.pop(0)
        return 0


def _fake_ctypes(k32=None, winmm=None, last_error=1117):
    """A ``ctypes`` module clone: real types (Structure, c_ubyte, sizeof …) so
    the SCSI struct is built for real, but Win32-only bits faked. ``byref`` is
    swapped for a readable wrapper.  1117 == ERROR_IO_DEVICE."""
    mod = types.ModuleType("ctypes")
    for name in dir(_ctypes):
        if not name.startswith("__"):
            setattr(mod, name, getattr(_ctypes, name))
    mod.WinDLL = lambda name, use_last_error=False: k32
    mod.windll = types.SimpleNamespace(winmm=winmm)
    mod.get_last_error = lambda: last_error
    mod.byref = _Ref
    wt = types.ModuleType("ctypes.wintypes")
    wt.DWORD = _ctypes.c_ulong
    wt.HANDLE = _ctypes.c_void_p
    wt.LPCWSTR = _ctypes.c_wchar_p
    wt.LPVOID = _ctypes.c_void_p
    mod.wintypes = wt
    return mod, wt


def _install_ctypes(monkeypatch, k32=None, winmm=None, last_error=1117):
    """The eject helpers ``import ctypes`` lazily (they only run on Windows),
    so the fake has to be installed in sys.modules for the duration."""
    mod, wt = _fake_ctypes(k32, winmm, last_error)
    monkeypatch.setitem(sys.modules, "ctypes", mod)
    monkeypatch.setitem(sys.modules, "ctypes.wintypes", wt)


def _platform(monkeypatch, name):
    """Pin the platform eject() dispatches on, without touching the real sys."""
    monkeypatch.setattr(ej, "sys", types.SimpleNamespace(platform=name))


def _fake_sub(monkeypatch, result=None):
    fake = _FakeSubprocess(result)
    monkeypatch.setattr(ej, "subprocess", fake)
    return fake


def _stub_eject_methods(monkeypatch, fail=()):
    """Replace the four Windows eject back-ends with recorders. Names in
    ``fail`` raise, so a test can drive any point of the fallback chain."""
    calls = []

    def make(name):
        def fn(arg):
            calls.append((name, arg))
            if name in fail:
                raise OSError(f"{name} said no")
        return fn

    for name, attr in (("scsi", "_eject_scsi"), ("ioctl", "_eject_ioctl"),
                       ("shell", "_eject_shell"), ("mci", "_eject_mci")):
        monkeypatch.setattr(ej, attr, make(name))
    return calls


def _stub_close_methods(monkeypatch, fail=()):
    calls = []

    def make(name):
        def fn(arg, **kw):
            calls.append((name, arg, kw))
            if name in fail:
                raise OSError(f"{name} said no")
        return fn

    monkeypatch.setattr(ej, "_scsi_start_stop", make("scsi"))
    monkeypatch.setattr(ej, "_close_ioctl", make("ioctl"))
    monkeypatch.setattr(ej, "_close_mci", make("mci"))
    return calls


# --- platform dispatch --------------------------------------------------


def test_linux_eject_names_the_device_on_the_command_line(monkeypatch):
    """A clustered serve box may have several optical drives; ejecting without
    naming the device would pop whichever one `eject` defaults to."""
    _platform(monkeypatch, "linux")
    sub = _fake_sub(monkeypatch)
    ej.eject("/dev/sr1")
    assert sub.argvs == [["eject", "/dev/sr1"]]


def test_linux_eject_without_a_device_leaves_the_default_to_the_tool(monkeypatch):
    """device=None means "the default drive" — it must be omitted, not passed
    through as a literal None (which would blow up argv encoding)."""
    _platform(monkeypatch, "linux")
    sub = _fake_sub(monkeypatch)
    ej.eject(None)
    assert sub.argvs == [["eject"]]


def test_macos_eject_uses_the_drutil_tray_eject_verb(monkeypatch):
    """macOS has no `eject(1)` for optical trays; drutil is the one that moves
    the tray. The device hint is deliberately not forwarded."""
    _platform(monkeypatch, "darwin")
    sub = _fake_sub(monkeypatch)
    ej.eject("disk3")
    assert sub.argvs == [["drutil", "tray", "eject"]]


def test_linux_close_tray_passes_the_load_flag(monkeypatch):
    """Closing is `eject -t`; without -t the same command would *open* the tray
    the user just asked to close."""
    _platform(monkeypatch, "linux")
    sub = _fake_sub(monkeypatch)
    ej.close_tray("/dev/sr0")
    assert sub.argvs == [["eject", "-t", "/dev/sr0"]]


def test_linux_close_tray_without_a_device_omits_it(monkeypatch):
    _platform(monkeypatch, "linux")
    sub = _fake_sub(monkeypatch)
    ej.close_tray(None)
    assert sub.argvs == [["eject", "-t"]]


def test_macos_close_tray_uses_the_drutil_close_verb(monkeypatch):
    _platform(monkeypatch, "darwin")
    sub = _fake_sub(monkeypatch)
    ej.close_tray(None)
    assert sub.argvs == [["drutil", "tray", "close"]]


def test_windows_eject_never_spawns_a_unix_eject_binary(monkeypatch):
    """Windows must take the Win32 branch: falling through to the POSIX branch
    would spawn a non-existent `eject.exe` and blow up mid-pipeline."""
    _platform(monkeypatch, "win32")
    sub = _fake_sub(monkeypatch)
    seen = []
    monkeypatch.setattr(ej, "_eject_windows", lambda d, m: seen.append((d, m)))
    ej.eject("F:", "shell")
    assert sub.runs == []
    assert seen == [("F:", "shell")]     # --method is forwarded, not dropped


def test_windows_close_tray_never_spawns_a_unix_eject_binary(monkeypatch):
    _platform(monkeypatch, "win32")
    sub = _fake_sub(monkeypatch)
    seen = []
    monkeypatch.setattr(ej, "_close_windows", lambda d, m: seen.append((d, m)))
    ej.close_tray("F:", "mci")
    assert sub.runs == []
    assert seen == [("F:", "mci")]


# --- device-name normalisation -----------------------------------------


def test_drive_letter_becomes_a_device_path_and_keeps_the_letter():
    r"""SCSI/IOCTL need \\.\F:; Shell/MCI need the bare letter. Both come out
    of one hint."""
    assert ej._win_paths("F:") == (r"\\.\F:", "F:")


def test_drive_letter_hint_tolerates_a_trailing_separator_and_spaces():
    r"""MakeMKV/the web UI hand over "F:\" — \\.\F:\ is not a valid device
    path, so the separator has to be stripped."""
    assert ej._win_paths("  F:\\  ") == (r"\\.\F:", "F:")


def test_device_path_hint_is_used_as_is_and_yields_no_drive_letter():
    r"""MakeMKV can report \\.\CdRom0 instead of a letter; there is no letter to
    derive, so the letter-only back-ends must be told so."""
    assert ej._win_paths(r"\\.\CdRom0") == (r"\\.\CdRom0", None)


def test_no_device_hint_yields_neither_path_nor_letter():
    assert ej._win_paths(None) == (None, None)
    assert ej._win_paths("") == (None, None)


# --- Windows eject: fallback chain -------------------------------------


def test_windows_eject_tries_the_no_admin_ioctl_first_and_stops_there(monkeypatch):
    """ioctl is the only back-end that works unelevated *and* silently; trying
    the slower PowerShell/SCSI paths after it succeeded would eject twice."""
    calls = _stub_eject_methods(monkeypatch)
    ej._eject_windows("F:")
    assert calls == [("ioctl", r"\\.\F:")]


def test_windows_eject_falls_back_to_the_shell_verb_when_the_ioctl_fails(monkeypatch):
    """The IOCTL is refused on some drives; Explorer's own eject verb still
    works, and it is the next thing that needs no admin rights."""
    calls = _stub_eject_methods(monkeypatch, fail=("ioctl",))
    ej._eject_windows("F:")
    assert calls == [("ioctl", r"\\.\F:"), ("shell", "F:")]


def test_windows_eject_keeps_falling_back_through_scsi_to_mci(monkeypatch):
    """Last resorts, in order, each with the argument shape it needs: SCSI takes
    the device path, MCI takes the drive letter."""
    calls = _stub_eject_methods(monkeypatch, fail=("ioctl", "shell", "scsi"))
    ej._eject_windows("F:")
    assert calls == [("ioctl", r"\\.\F:"), ("shell", "F:"),
                     ("scsi", r"\\.\F:"), ("mci", "F:")]


def test_windows_eject_reports_what_every_method_answered(monkeypatch):
    """When the tray really is stuck the user gets one message naming each
    method and its error — that is the only diagnostic they have."""
    _stub_eject_methods(monkeypatch, fail=("ioctl", "shell", "scsi", "mci"))
    with pytest.raises(OSError) as exc:
        ej._eject_windows("F:")
    msg = str(exc.value)
    for name in ("ioctl", "shell", "scsi", "mci"):
        assert f"{name}: {name} said no" in msg


def test_windows_eject_of_a_device_path_skips_the_letter_only_shell_verb(monkeypatch):
    r"""With \\.\CdRom0 there is no drive letter, so the Shell verb cannot be
    attempted — it must be skipped, not called with None."""
    calls = _stub_eject_methods(monkeypatch, fail=("ioctl", "scsi"))
    ej._eject_windows(r"\\.\CdRom0")
    assert [name for name, _arg in calls] == ["ioctl", "scsi", "mci"]


def test_windows_eject_without_any_device_reports_instead_of_no_opping(monkeypatch):
    """No device hint means no method can run. Returning quietly would look
    like a successful eject and the user would wait for a tray that never
    moved."""
    calls = _stub_eject_methods(monkeypatch)
    with pytest.raises(OSError, match="no eject method available"):
        ej._eject_windows(None)
    assert calls == []


def test_windows_forced_method_runs_only_that_method(monkeypatch):
    """`--method mci` exists to diagnose a stubborn drive; silently falling
    back would hide which method actually moved the tray."""
    calls = _stub_eject_methods(monkeypatch)
    ej._eject_windows("F:", "mci")
    assert calls == [("mci", "F:")]


def test_windows_forced_method_failure_is_not_swallowed_by_a_fallback(monkeypatch):
    """A forced method that fails raises its own error, with no other method
    attempted afterwards."""
    calls = _stub_eject_methods(monkeypatch, fail=("scsi",))
    with pytest.raises(OSError, match="scsi said no"):
        ej._eject_windows("F:", "scsi")
    assert calls == [("scsi", r"\\.\F:")]


def test_windows_unknown_forced_method_is_rejected(monkeypatch):
    """A typo in --method must not be read as "nothing to do"."""
    calls = _stub_eject_methods(monkeypatch)
    with pytest.raises(OSError, match="unknown eject method 'iocl'"):
        ej._eject_windows("F:", "iocl")
    assert calls == []


def test_forcing_a_letter_only_method_without_a_letter_says_so(monkeypatch):
    r"""Forcing shell/mci against \\.\CdRom0 cannot work; the message has to
    name what is missing (a drive letter) rather than a generic failure."""
    _stub_eject_methods(monkeypatch)
    with pytest.raises(OSError, match="drive letter"):
        ej._eject_windows(r"\\.\CdRom0", "shell")


def test_forcing_a_device_method_without_a_device_says_so(monkeypatch):
    _stub_eject_methods(monkeypatch)
    with pytest.raises(OSError) as exc:
        ej._eject_windows(None, "ioctl")
    assert "needs a device" in str(exc.value)


# --- Windows close: fallback chain -------------------------------------


def test_close_tray_prefers_scsi_and_asks_it_to_load(monkeypatch):
    """Closing is the reverse of ejecting: the SCSI back-end must be told
    start=True, or the tray the user asked to close would open instead."""
    calls = _stub_close_methods(monkeypatch)
    ej._close_windows("F:")
    assert calls == [("scsi", r"\\.\F:", {"start": True})]


def test_close_tray_falls_back_from_scsi_to_ioctl_to_mci(monkeypatch):
    """SCSI pass-through needs admin, so the unelevated fallbacks must follow
    it — with the device path for the IOCTL and the letter for MCI."""
    calls = _stub_close_methods(monkeypatch, fail=("scsi", "ioctl"))
    ej._close_windows("F:")
    assert [(name, arg) for name, arg, _kw in calls] == [
        ("scsi", r"\\.\F:"), ("ioctl", r"\\.\F:"), ("mci", "F:")]


def test_close_tray_of_a_device_path_still_tries_every_method(monkeypatch):
    r"""With \\.\CdRom0 there is no drive letter, so the last-ditch MCI attempt
    is made with the raw hint rather than skipped — the SCSI/IOCTL back-ends
    still get the device path."""
    calls = _stub_close_methods(monkeypatch, fail=("scsi", "ioctl"))
    ej._close_windows(r"\\.\CdRom0")
    assert [(name, arg) for name, arg, _kw in calls] == [
        ("scsi", r"\\.\CdRom0"), ("ioctl", r"\\.\CdRom0"), ("mci", r"\\.\CdRom0")]


def test_close_tray_reports_what_every_method_answered(monkeypatch):
    _stub_close_methods(monkeypatch, fail=("scsi", "ioctl", "mci"))
    with pytest.raises(OSError) as exc:
        ej._close_windows("F:")
    msg = str(exc.value)
    for name in ("scsi", "ioctl", "mci"):
        assert f"{name}: {name} said no" in msg


def test_close_tray_without_a_device_reports_instead_of_no_opping(monkeypatch):
    calls = _stub_close_methods(monkeypatch)
    with pytest.raises(OSError, match="no close method available"):
        ej._close_windows(None)
    assert calls == []


def test_close_tray_unknown_forced_method_is_rejected(monkeypatch):
    calls = _stub_close_methods(monkeypatch)
    with pytest.raises(OSError, match="unknown close method 'shell'"):
        ej._close_windows("F:", "shell")   # shell is eject-only
    assert calls == []


def test_close_tray_forced_method_without_a_device_says_what_is_missing(monkeypatch):
    _stub_close_methods(monkeypatch)
    with pytest.raises(OSError, match="needs a drive/device"):
        ej._close_windows(None, "ioctl")


def test_close_tray_forced_method_runs_only_that_method(monkeypatch):
    calls = _stub_close_methods(monkeypatch)
    ej._close_windows("F:", "mci")
    assert calls == [("mci", "F:", {})]


# --- IOCTL back-ends ----------------------------------------------------


def test_eject_ioctl_opens_the_device_read_only_so_no_admin_is_needed(monkeypatch):
    """GENERIC_WRITE on an optical device is what forces an elevation prompt
    (err 5 on open); read-only still satisfies the eject IOCTL. This is the
    whole reason auto-eject works from a non-elevated service."""
    k32 = _K32()
    _install_ctypes(monkeypatch, k32=k32)
    ej._eject_ioctl(r"\\.\F:")
    path, access, _share, _disp = k32.opens[0]
    assert path == r"\\.\F:"
    assert access == GENERIC_READ
    assert not access & GENERIC_WRITE


def test_eject_ioctl_unlocks_dismounts_allows_removal_then_ejects(monkeypatch):
    """The eject IOCTL fails on a mounted volume, so the lock/dismount/allow-
    removal preamble has to be sent first, in that order."""
    k32 = _K32()
    _install_ctypes(monkeypatch, k32=k32)
    ej._eject_ioctl(r"\\.\F:")
    assert k32.ioctl_codes == [FSCTL_LOCK_VOLUME, FSCTL_DISMOUNT_VOLUME,
                               IOCTL_STORAGE_MEDIA_REMOVAL,
                               IOCTL_STORAGE_EJECT_MEDIA]
    assert k32.closed == [k32.handle]


def test_eject_ioctl_allows_removal_with_a_cleared_prevent_flag(monkeypatch):
    """PREVENT_MEDIA_REMOVAL carries one byte; sending a non-zero one would ask
    the drive to *lock* the disc in right before ejecting it."""
    k32 = _K32()
    _install_ctypes(monkeypatch, k32=k32)
    ej._eject_ioctl(r"\\.\F:")
    _h, _code, buf, length = next(
        rec for rec in k32.ioctls if rec[1] == IOCTL_STORAGE_MEDIA_REMOVAL)
    assert length == 1
    assert list(buf) == [0]


def test_eject_ioctl_treats_lock_and_dismount_as_best_effort(monkeypatch):
    """A disc with no mountable filesystem (or one already dismounted) fails
    lock/dismount — that must not abort an otherwise fine eject."""
    k32 = _K32(ioctl_results={FSCTL_LOCK_VOLUME: 0, FSCTL_DISMOUNT_VOLUME: 0,
                              IOCTL_STORAGE_MEDIA_REMOVAL: 0})
    _install_ctypes(monkeypatch, k32=k32)
    ej._eject_ioctl(r"\\.\F:")      # must not raise
    assert IOCTL_STORAGE_EJECT_MEDIA in k32.ioctl_codes


def test_eject_ioctl_reports_a_refused_eject_and_still_closes_the_handle(monkeypatch):
    """Leaking the device handle would keep the drive busy for the next rip,
    and a swallowed failure would look like a successful eject."""
    k32 = _K32(ioctl_results={IOCTL_STORAGE_EJECT_MEDIA: 0})
    _install_ctypes(monkeypatch, k32=k32, last_error=5)
    with pytest.raises(OSError) as exc:
        ej._eject_ioctl(r"\\.\F:")
    assert r"\\.\F:" in str(exc.value) and "err 5" in str(exc.value)
    assert k32.closed == [k32.handle]


def test_eject_ioctl_reports_a_failed_open_without_closing_a_bad_handle(monkeypatch):
    """CreateFileW answers INVALID_HANDLE_VALUE when the drive letter is wrong
    or in use; passing that to DeviceIoControl/CloseHandle is undefined."""
    k32 = _K32(handle=_INVALID_HANDLE)
    _install_ctypes(monkeypatch, k32=k32, last_error=2)
    with pytest.raises(OSError, match="err 2"):
        ej._eject_ioctl(r"\\.\Q:")
    assert k32.ioctls == [] and k32.closed == []


def test_eject_ioctl_treats_a_null_handle_as_a_failed_open(monkeypatch):
    """Some layers answer 0 rather than INVALID_HANDLE_VALUE; both mean no
    device."""
    k32 = _K32(handle=0)
    _install_ctypes(monkeypatch, k32=k32)
    with pytest.raises(OSError, match="open .* failed"):
        ej._eject_ioctl(r"\\.\F:")
    assert k32.ioctls == []


def test_close_ioctl_sends_load_media_not_eject_media(monkeypatch):
    """LOAD_MEDIA (0x2D480C) closes the tray; the neighbouring EJECT_MEDIA code
    would open the tray the user asked to close."""
    k32 = _K32()
    _install_ctypes(monkeypatch, k32=k32)
    ej._close_ioctl(r"\\.\F:")
    assert k32.ioctl_codes == [IOCTL_STORAGE_LOAD_MEDIA]
    assert k32.opens[0][1] == GENERIC_READ       # unelevated, like eject
    assert k32.closed == [k32.handle]


def test_close_ioctl_reports_a_refused_load_and_still_closes_the_handle(monkeypatch):
    k32 = _K32(ioctl_results={IOCTL_STORAGE_LOAD_MEDIA: 0})
    _install_ctypes(monkeypatch, k32=k32, last_error=21)
    with pytest.raises(OSError, match="load-media ioctl failed"):
        ej._close_ioctl(r"\\.\F:")
    assert k32.closed == [k32.handle]


def test_close_ioctl_reports_a_failed_open(monkeypatch):
    k32 = _K32(handle=_INVALID_HANDLE)
    _install_ctypes(monkeypatch, k32=k32, last_error=2)
    with pytest.raises(OSError, match="open .* failed"):
        ej._close_ioctl(r"\\.\F:")
    assert k32.ioctls == [] and k32.closed == []


# --- SCSI pass-through --------------------------------------------------


def _sent_cdb(k32):
    _h, code, ref, _len = k32.ioctls[0]
    assert code == IOCTL_SCSI_PASS_THROUGH_DIRECT
    return ref.obj


def test_scsi_eject_sends_start_stop_unit_with_the_tray_open_bits(monkeypatch):
    """CDB 1Bh with LoadEject=1/Start=0 (byte 4 == 0x02) is the command that
    physically opens the tray; byte 4 == 0x03 would suck it back in."""
    k32 = _K32()
    _install_ctypes(monkeypatch, k32=k32)
    ej._eject_scsi(r"\\.\F:")
    cdb = _sent_cdb(k32)
    assert cdb.Cdb[0] == 0x1B
    assert cdb.Cdb[4] == 0x02
    assert cdb.CdbLength == 6


def test_scsi_load_sets_the_start_bit(monkeypatch):
    """The close-tray direction shares the command; only the Start bit differs."""
    k32 = _K32()
    _install_ctypes(monkeypatch, k32=k32)
    ej._scsi_start_stop(r"\\.\F:", start=True)
    assert _sent_cdb(k32).Cdb[4] == 0x03


def test_scsi_pass_through_opens_the_device_for_write(monkeypatch):
    """Unlike the eject IOCTL, SCSI pass-through is refused on a read-only
    handle — this back-end genuinely needs read/write (and admin)."""
    k32 = _K32()
    _install_ctypes(monkeypatch, k32=k32)
    ej._eject_scsi(r"\\.\F:")
    assert k32.opens[0][1] == GENERIC_READ | GENERIC_WRITE


def test_scsi_declares_the_struct_length_and_a_bounded_timeout(monkeypatch):
    """A wrong Length makes the driver reject the request outright, and a
    missing timeout lets a wedged drive hang the rip thread."""
    k32 = _K32()
    _install_ctypes(monkeypatch, k32=k32)
    ej._eject_scsi(r"\\.\F:")
    sptd = _sent_cdb(k32)
    assert sptd.Length == _ctypes.sizeof(type(sptd))
    assert 0 < sptd.TimeOutValue <= 60


def test_scsi_failure_names_the_direction_that_failed(monkeypatch):
    """"scsi eject failed" vs "scsi load failed" is what tells the user which
    half of the tray dance the drive refused."""
    k32 = _K32(ioctl_results={IOCTL_SCSI_PASS_THROUGH_DIRECT: 0})
    _install_ctypes(monkeypatch, k32=k32, last_error=87)
    with pytest.raises(OSError) as exc:
        ej._eject_scsi(r"\\.\F:")
    assert "scsi eject failed" in str(exc.value) and "err 87" in str(exc.value)
    assert k32.closed == [k32.handle]

    k32b = _K32(ioctl_results={IOCTL_SCSI_PASS_THROUGH_DIRECT: 0})
    _install_ctypes(monkeypatch, k32=k32b)
    with pytest.raises(OSError, match="scsi load failed"):
        ej._scsi_start_stop(r"\\.\F:", start=True)


def test_scsi_reports_a_failed_open_without_closing_a_bad_handle(monkeypatch):
    k32 = _K32(handle=_INVALID_HANDLE)
    _install_ctypes(monkeypatch, k32=k32, last_error=5)
    with pytest.raises(OSError, match=r"open .* failed \(err 5\)"):
        ej._eject_scsi(r"\\.\F:")
    assert k32.ioctls == [] and k32.closed == []


# --- Shell (PowerShell) verb -------------------------------------------


def test_shell_eject_targets_the_named_drive_in_the_drives_namespace(monkeypatch):
    """Namespace(17) is ssfDRIVES; the drive letter has to reach ParseName or
    the verb fires on nothing."""
    _platform(monkeypatch, "win32")
    sub = _fake_sub(monkeypatch)
    ej._eject_shell("F:")
    argv, kwargs = sub.runs[0]
    assert argv[0] == "powershell"
    script = argv[-1]
    assert "Namespace(17)" in script
    assert "ParseName('F:')" in script
    assert kwargs["capture_output"] and kwargs["timeout"] > 0


def test_shell_eject_fires_the_enumerated_verb_rather_than_invokeverb(monkeypatch):
    """The verb is really named "E&ject", so InvokeVerb('Eject') silently
    no-ops. The script must enumerate Verbs() and DoIt() the matching one."""
    _platform(monkeypatch, "win32")
    sub = _fake_sub(monkeypatch)
    ej._eject_shell("F:")
    script = sub.runs[0][0][-1]
    assert "Verbs()" in script and "DoIt()" in script
    assert "InvokeVerb" not in script


def test_shell_eject_surfaces_powershells_own_error_text(monkeypatch):
    """'drive not found' / 'no eject verb' are thrown by the script itself and
    are the useful half of the fallback chain's final message."""
    _platform(monkeypatch, "win32")
    _fake_sub(monkeypatch, types.SimpleNamespace(
        returncode=1, stderr="  drive not found  \n"))
    with pytest.raises(OSError, match="drive not found"):
        ej._eject_shell("Q:")


def test_shell_eject_falls_back_to_the_exit_code_when_stderr_is_silent(monkeypatch):
    """A bare non-zero exit still has to become a message, not an empty one."""
    _platform(monkeypatch, "win32")
    _fake_sub(monkeypatch, types.SimpleNamespace(returncode=9, stderr=""))
    with pytest.raises(OSError, match="powershell exit 9"):
        ej._eject_shell("F:")


def test_shell_eject_is_quiet_when_powershell_succeeds(monkeypatch):
    """Exit 0 means the tray moved — raising here would send the caller down
    the slower SCSI/MCI fallbacks for nothing."""
    _platform(monkeypatch, "win32")
    _fake_sub(monkeypatch, types.SimpleNamespace(returncode=0, stderr="junk"))
    ej._eject_shell("F:")


# --- MCI ----------------------------------------------------------------


def test_mci_eject_opens_the_drive_asks_the_door_open_then_closes(monkeypatch):
    """MCI needs the device opened before the door command and closed after,
    otherwise the alias leaks and the next attempt fails to open it."""
    winmm = _Winmm()
    _install_ctypes(monkeypatch, winmm=winmm)
    ej._eject_mci("F:")
    assert len(winmm.commands) == 3
    assert winmm.commands[0].startswith("open F: type cdaudio alias ")
    assert "door open" in winmm.commands[1]
    assert winmm.commands[2].startswith("close ")


def test_mci_uses_one_alias_across_open_set_and_close(monkeypatch):
    """A mismatched alias would leave the device open forever and make the
    door command address nothing."""
    winmm = _Winmm()
    _install_ctypes(monkeypatch, winmm=winmm)
    ej._eject_mci("F:")
    alias = winmm.commands[0].rsplit(" ", 1)[1]
    assert alias in winmm.commands[1].split()
    assert winmm.commands[2] == f"close {alias}"


def test_mci_closes_the_alias_even_when_the_door_command_fails(monkeypatch):
    """The failing case is exactly when a leaked alias hurts: the next fallback
    (or the next disc) needs the device free."""
    winmm = _Winmm(results=[0, 275, 0])   # open ok, set fails, close ok
    _install_ctypes(monkeypatch, winmm=winmm)
    with pytest.raises(OSError, match="MCI returned 275"):
        ej._eject_mci("F:")
    assert winmm.commands[-1].startswith("close ")


def test_mci_eject_is_quiet_on_a_zero_return_code(monkeypatch):
    winmm = _Winmm()
    _install_ctypes(monkeypatch, winmm=winmm)
    ej._eject_mci("F:")     # rc 0 everywhere -> no raise


def test_mci_close_asks_for_the_door_closed(monkeypatch):
    """Same command family as eject with the opposite door state; sharing the
    'open' string would make close_tray eject the disc."""
    winmm = _Winmm()
    _install_ctypes(monkeypatch, winmm=winmm)
    ej._close_mci("F:")
    assert "door closed" in winmm.commands[1]
    assert "door open" not in winmm.commands[1]


def test_mci_close_reports_a_non_zero_return_code(monkeypatch):
    winmm = _Winmm(results=[0, 266, 0])
    _install_ctypes(monkeypatch, winmm=winmm)
    with pytest.raises(OSError, match="MCI returned 266"):
        ej._close_mci("F:")


def test_mci_close_uses_one_alias_across_open_set_and_close(monkeypatch):
    winmm = _Winmm()
    _install_ctypes(monkeypatch, winmm=winmm)
    ej._close_mci("F:")
    alias = winmm.commands[0].rsplit(" ", 1)[1]
    assert winmm.commands[2] == f"close {alias}"


def test_mci_without_a_drive_addresses_the_default_cdaudio_device(monkeypatch):
    """The no-drive form talks to MCI's built-in CDAudio device instead of
    opening an alias. (Reachable only by calling _eject_mci directly — the
    dispatcher skips MCI when it has no drive string; see notes.)"""
    winmm = _Winmm(results=[0])
    _install_ctypes(monkeypatch, winmm=winmm)
    ej._eject_mci("")
    assert winmm.commands == ["set CDAudio door open wait"]
