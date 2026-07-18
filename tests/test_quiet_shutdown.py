"""The Windows shutdown hook swallows only the benign proactor teardown noise."""

import sys
import types

from blurayripper.cli import _quiet_windows_shutdown


def _fire(exc):
    """Invoke whatever is currently installed as sys.unraisablehook with `exc`."""
    args = types.SimpleNamespace(
        exc_type=type(exc), exc_value=exc, exc_traceback=None, object=None)
    sys.unraisablehook(args)


def test_hook_swallows_closed_pipe_but_passes_others(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    seen = []
    monkeypatch.setattr(sys, "unraisablehook", lambda a: seen.append(a.exc_value))

    _quiet_windows_shutdown()   # wraps the (recorded) default hook

    _fire(ValueError("I/O operation on closed pipe"))   # benign -> swallowed
    _fire(RuntimeError("something real"))               # real -> passed through
    _fire(ValueError("some other value error"))         # not the pipe one -> passed

    assert len(seen) == 2
    assert isinstance(seen[0], RuntimeError)
    assert "other value error" in str(seen[1])


def test_hook_is_noop_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    sentinel = object()
    monkeypatch.setattr(sys, "unraisablehook", sentinel)
    _quiet_windows_shutdown()
    assert sys.unraisablehook is sentinel   # unchanged
