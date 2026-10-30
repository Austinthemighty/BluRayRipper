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


# --- tapping a notification opens the web UI ---------------------------------

def _settings(**kw):
    from types import SimpleNamespace
    base = dict(notify_click_url=None, web_host="0.0.0.0", web_port=8723,
                database_url="postgresql://u:p@192.168.1.10:5432/brr")
    base.update(kw)
    return SimpleNamespace(**base)


async def test_a_click_link_rides_in_the_click_header(monkeypatch):
    monkeypatch.setattr(N.httpx, "AsyncClient", _Client)
    await N.notify("http://ntfy/x", "✅ Done", "m", click="http://10.147.17.38:8723/#job-7")
    assert _Client.last["headers"]["Click"] == b"http://10.147.17.38:8723/#job-7"


async def test_no_click_link_sends_no_click_header(monkeypatch):
    monkeypatch.setattr(N.httpx, "AsyncClient", _Client)
    await N.notify("http://ntfy/x", "t", "m")
    assert "Click" not in _Client.last["headers"]


def test_the_configured_link_wins_and_a_job_alert_opens_at_its_job():
    s = _settings(notify_click_url="http://10.147.17.38:8723/")
    assert N.click_link(s) == "http://10.147.17.38:8723"
    assert N.click_link(s, 431) == "http://10.147.17.38:8723/#job-431"


def test_with_no_link_set_serve_offers_its_own_lan_address(monkeypatch):
    """serve listens on every interface, so the phone can reach it at the
    address this box routes to the cluster on."""
    import blurayripper.worker as worker_mod
    monkeypatch.setattr(worker_mod, "local_address", lambda db: "192.168.1.188")
    assert N.click_link(_settings(), 5) == "http://192.168.1.188:8723/#job-5"
    assert N.click_link(_settings(web_host="192.168.1.188", web_port=9000)) == \
        "http://192.168.1.188:9000"


def test_no_link_when_nothing_a_phone_could_reach(monkeypatch):
    import blurayripper.worker as worker_mod
    monkeypatch.setattr(worker_mod, "local_address", lambda db: None)
    assert N.click_link(_settings()) is None                       # address unknown
    assert N.click_link(_settings(web_host="127.0.0.1")) is None    # localhost only


def test_a_worker_never_links_to_its_own_address(monkeypatch):
    """The UI is on serve. A worker's own address would open nothing, so only a
    configured link is used there."""
    import blurayripper.worker as worker_mod
    monkeypatch.setattr(worker_mod, "local_address", lambda db: "192.168.1.112")
    assert N.click_link(_settings(), derive=False) is None
    assert N.click_link(_settings(notify_click_url="http://10.147.17.38:8723"),
                        derive=False) == "http://10.147.17.38:8723"
