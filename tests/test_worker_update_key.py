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


def test_find_git_env_override_wins(monkeypatch, tmp_path):
    from blurayripper.cli import _find_git
    fake = tmp_path / "mygit.exe"
    fake.write_text("")
    monkeypatch.setenv("BLURAYRIPPER_GIT", str(fake))
    assert _find_git() == str(fake)


def test_find_git_prefers_path(monkeypatch):
    import shutil

    from blurayripper.cli import _find_git
    monkeypatch.delenv("BLURAYRIPPER_GIT", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/git")
    assert _find_git() == "/usr/bin/git"


def test_find_git_locates_github_desktop_bundle(monkeypatch, tmp_path):
    import shutil

    from blurayripper.cli import _find_git
    monkeypatch.delenv("BLURAYRIPPER_GIT", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)   # not on PATH
    monkeypatch.setattr(sys, "platform", "win32")
    # Two GitHub Desktop versions installed — the newest wins.
    la = tmp_path / "LocalAppData"
    for ver in ("app-3.4.1", "app-3.4.3"):
        p = la / "GitHubDesktop" / ver / "resources" / "app" / "git" / "cmd" / "git.exe"
        p.parent.mkdir(parents=True)
        p.write_text("")
    monkeypatch.setenv("LOCALAPPDATA", str(la))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "nope"))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "nope2"))
    got = _find_git()
    assert got is not None and got.endswith("git.exe") and "app-3.4.3" in got


def test_stop_handlers_cover_unattended_shutdown():
    """A worker running as a scheduled task/service must DRAIN when something
    tells it to stop — not just on an interactive Ctrl+C — or an hours-long
    encode is lost every time the box shuts down."""
    import signal

    from blurayripper.cli import _STOP_SIGNALS, _install_stop_handlers

    saved = {n: signal.getsignal(getattr(signal, n))
             for n in _STOP_SIGNALS if hasattr(signal, n)}
    calls = []
    try:
        installed = _install_stop_handlers(lambda signum, frame: calls.append(signum))

        assert "SIGINT" in installed              # interactive Ctrl+C
        # The supervisor signal for this platform is covered: SIGTERM on POSIX,
        # SIGBREAK (Ctrl+Break) on Windows.
        assert ("SIGTERM" in installed) or ("SIGBREAK" in installed)
        for name in installed:
            assert signal.getsignal(getattr(signal, name)) is not None
        # Only signals this platform actually has get installed.
        assert all(hasattr(signal, n) for n in installed)

        # The handler really is wired up — raising the signal drains, not dies.
        signal.raise_signal(signal.SIGINT)
        assert calls == [int(signal.SIGINT)]
    finally:
        for name, prev in saved.items():
            signal.signal(getattr(signal, name), prev)
