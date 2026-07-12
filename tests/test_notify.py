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
    assert _Client.last["headers"]["Title"] == "Grain"
    assert _Client.last["headers"]["Priority"] == "high"


async def test_notify_no_url_is_noop():
    assert await N.notify(None, "t", "m") is False


async def test_notify_swallows_errors(monkeypatch):
    class Boom:
        def __init__(self, **kw): pass
        async def __aenter__(self): raise RuntimeError("down")
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(N.httpx, "AsyncClient", Boom)
    assert await N.notify("http://x", "t", "m") is False  # never raises
