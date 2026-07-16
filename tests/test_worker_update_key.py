"""The worker's 'u'/Ctrl+U key: drain, then git-pull and restart."""

import sys

from blurayripper.cli import _KeyPress, _is_update_key


def test_update_key_recognizes_ctrl_u_and_u():
    assert _is_update_key("\x15")   # Ctrl+U
    assert _is_update_key("u")
    assert _is_update_key("U")
    assert not _is_update_key("x")
    assert not _is_update_key("\n")
    assert not _is_update_key("\x03")  # Ctrl+C stays with SIGINT, not this key


def test_keypress_is_noop_without_a_tty(monkeypatch):
    # Under pytest stdin isn't a TTY; start() must not spawn a thread or crash, so
    # a headless/service worker is unaffected.
    fired = []
    k = _KeyPress(lambda ch: fired.append(ch))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    k.start()
    k.stop()
    assert fired == []


def test_update_key_callback_requests_drain_and_flags_update():
    # Mirror the wiring in _cmd_worker: the callback sets the update flag and asks
    # the runner to drain, but only once (idempotent while already stopping).
    class _Runner:
        def __init__(self): self._stop = False
        def request_stop(self): self._stop = True

    runner = _Runner()
    update = {"do": False}

    def on_key(ch):
        if _is_update_key(ch) and not runner._stop:
            update["do"] = True
            runner.request_stop()

    on_key("x")
    assert update["do"] is False and runner._stop is False
    on_key("u")
    assert update["do"] is True and runner._stop is True
