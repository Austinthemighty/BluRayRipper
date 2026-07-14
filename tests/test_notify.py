"""Webhook notification helper: ntfy-style plain-text POST, best-effort."""

import blurayripper.services.notify as N


class _Resp:
    is_success = True


class _Client:
    last = {}

    def __init__(self, **kw): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, content=None, headers=None):
        _Client.last = {"url": url, "content": content, "headers": headers}
        return _Resp()


async def test_notify_posts_body_and_headers(monkeypatch):
    monkeypatch.setattr(N.httpx, "AsyncClient", _Client)
    ok = await N.notify("http://ntfy/mytopic", "Grain", "pick one", priority="high")
    assert ok
    assert _Client.last["url"] == "http://ntfy/mytopic"
    assert _Client.last["content"] == b"pick one"
    # Title is sent as UTF-8 bytes (so an emoji title can't crash header encoding).
    assert _Client.last["headers"]["Title"] == b"Grain"
    assert _Client.last["headers"]["Priority"] == "high"


async def test_notify_emoji_title_is_sent_as_utf8_bytes(monkeypatch):
    # An emoji in an HTTP header would raise UnicodeEncodeError (latin-1); sending
    # the Title as UTF-8 bytes avoids that and keeps the emoji.
    monkeypatch.setattr(N.httpx, "AsyncClient", _Client)
    ok = await N.notify("http://ntfy/x", "💿 Insert disc", "body 🔔", priority="high")
    assert ok is True
    assert _Client.last["headers"]["Title"] == "💿 Insert disc".encode("utf-8")
    assert _Client.last["content"] == "body 🔔".encode("utf-8")


async def test_notify_no_url_is_noop():
    assert await N.notify(None, "t", "m") is False


async def test_notify_swallows_errors(monkeypatch):
    class Boom:
        def __init__(self, **kw): pass
        async def __aenter__(self): raise RuntimeError("down")
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(N.httpx, "AsyncClient", Boom)
    assert await N.notify("http://x", "t", "m") is False  # never raises
