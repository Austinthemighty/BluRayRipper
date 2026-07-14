"""OpenSubtitles reference-subtitle selection (pure parsing) and config wiring."""

from blurayripper.config import load_settings
from blurayripper.services.opensubtitles import OpenSubtitlesClient, best_file_id


def _item(file_id, *, lang="en", trusted=False, downloads=0):
    return {
        "attributes": {
            "language": lang,
            "from_trusted": trusted,
            "download_count": downloads,
            "files": [{"file_id": file_id, "file_name": f"{file_id}.srt"}],
        }
    }


def test_best_file_prefers_trusted_then_downloads():
    data = [
        _item(1, downloads=500),
        _item(2, trusted=True, downloads=10),  # trusted wins despite fewer downloads
        _item(3, downloads=900),
    ]
    assert best_file_id(data) == 2


def test_best_file_filters_language():
    data = [_item(1, lang="es", downloads=999), _item(2, lang="en", downloads=5)]
    assert best_file_id(data, language="en") == 2


def test_best_file_none_when_no_files():
    assert best_file_id([{"attributes": {"language": "en", "files": []}}]) is None
    assert best_file_id([]) is None


def test_client_unconfigured_without_key():
    assert OpenSubtitlesClient(None).configured is False
    assert OpenSubtitlesClient("k").configured is True


def test_settings_load_opensubtitles_from_env(monkeypatch):
    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "abc123")
    monkeypatch.setenv("OPENSUBTITLES_USERNAME", "me")
    s = load_settings()
    assert s.opensubtitles_api_key == "abc123"
    assert s.opensubtitles_username == "me"


def _http_error(code):
    import httpx
    req = httpx.Request("POST", "https://api.opensubtitles.com/api/v1/download")
    return httpx.HTTPStatusError(str(code), request=req, response=httpx.Response(code, request=req))


def test_classify_http_error_flags_quota_and_auth_as_fatal():
    q = OpenSubtitlesClient._classify_http_error(_http_error(406))
    assert q.fatal and "quota" in str(q).lower()
    r = OpenSubtitlesClient._classify_http_error(_http_error(429))
    assert r.fatal and "quota" in str(r).lower()
    a = OpenSubtitlesClient._classify_http_error(_http_error(401))
    assert a.fatal and "api_key" in str(a).lower().replace(" ", "_") or "key" in str(a).lower()
    other = OpenSubtitlesClient._classify_http_error(_http_error(500))
    assert not other.fatal


async def test_gather_references_surfaces_fatal_and_stops_early():
    import pytest
    from blurayripper.services.opensubtitles import gather_references, OpenSubtitlesError

    class Quota:
        calls = 0
        async def fetch_episode_srt(self, *a):
            Quota.calls += 1
            raise OpenSubtitlesError("quota", fatal=True)
    q = Quota()
    with pytest.raises(OpenSubtitlesError) as ei:
        await gather_references(q, 1043, 1, [6, 7, 8], "en")
    assert ei.value.fatal and q.calls == 1  # fatal -> stop after the first episode


async def test_gather_references_empty_when_no_subs_no_error():
    from blurayripper.services.opensubtitles import gather_references

    class Miss:
        async def fetch_episode_srt(self, *a):
            return None
    assert await gather_references(Miss(), 1043, 1, [6, 7, 8], "en") == {}


async def test_gather_references_returns_partial_hits():
    from blurayripper.services.opensubtitles import gather_references

    class Partial:
        async def fetch_episode_srt(self, tmdb, season, ep, lang):
            return "SRT" if ep == 7 else None
    assert await gather_references(Partial(), 1043, 1, [6, 7, 8], "en") == {7: "SRT"}


class _Resp:
    def __init__(self, status, js=None, text=""):
        self.status_code = status; self._js = js or {}; self.text = text
    def json(self): return self._js


class _Client:
    def __init__(self, resp): self.resp = resp
    async def post(self, *a, **k): return self.resp


async def test_login_missing_creds_tells_you_to_restart():
    import pytest
    from blurayripper.services.opensubtitles import OpenSubtitlesClient, OpenSubtitlesError
    c = OpenSubtitlesClient("key")  # no username/password
    with pytest.raises(OpenSubtitlesError) as ei:
        await c._login(_Client(_Resp(200)), required=True)
    assert "RESTART" in str(ei.value) and ei.value.fatal


async def test_login_failure_surfaces_http_reason():
    import pytest
    from blurayripper.services.opensubtitles import OpenSubtitlesClient, OpenSubtitlesError
    c = OpenSubtitlesClient("key", "user", "pw")
    with pytest.raises(OpenSubtitlesError) as ei:
        await c._login(_Client(_Resp(401, {"message": "wrong password"})), required=True)
    assert "HTTP 401" in str(ei.value) and "wrong password" in str(ei.value)


async def test_login_best_effort_never_raises():
    from blurayripper.services.opensubtitles import OpenSubtitlesClient
    # required=False (the search/download path): a failed login must not raise —
    # the Api-Key alone still works for downloads.
    c = OpenSubtitlesClient("key", "user", "pw")
    await c._login(_Client(_Resp(403)))   # no exception
    assert c._token is None


async def test_login_stores_token_on_success():
    from blurayripper.services.opensubtitles import OpenSubtitlesClient
    c = OpenSubtitlesClient("key", "user", "pw")
    await c._login(_Client(_Resp(200, {"token": "abc"})), required=True)
    assert c._token == "abc"


def test_remember_quota_captures_download_response():
    from blurayripper.services.opensubtitles import OpenSubtitlesClient
    c = OpenSubtitlesClient("key")
    assert c.last_known_quota() is None
    c._remember_quota({"remaining": 87, "requests": 13, "reset_time_utc": "2026-07-14T04:10:00Z"})
    assert c.last_known_quota()["remaining"] == 87
    # over-quota records 0 remaining but keeps the reset time
    c._remember_quota({"requests": 100, "reset_time_utc": "2026-07-14T04:10:00Z"}, exhausted=True)
    q = c.last_known_quota()
    assert q["remaining"] == 0 and q["reset_time_utc"] == "2026-07-14T04:10:00Z"


async def test_fetch_normalizes_language_to_iso1(monkeypatch, tmp_path):
    """The app passes 'eng' (639-2/B); OpenSubtitles wants 'en'. fetch must
    normalise it for the search param AND the response-language filter, else a
    valid 'en' result gets filtered out and nothing is found."""
    import blurayripper.services.opensubtitles as OS

    captured = {}

    class Resp:
        def __init__(self, js=None, text=""):
            self._js = js or {}; self.text = text; self.status_code = 200
        def raise_for_status(self): pass
        def json(self): return self._js

    class Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None, params=None):
            if url.endswith("/subtitles"):
                captured["languages"] = params.get("languages")
                return Resp(js={"data": [{"attributes": {
                    "language": "en", "download_count": 5,
                    "files": [{"file_id": 42}]}}]})
            return Resp(text="1\n00:00:01,000 --> 00:00:02,000\nHi\n")  # the SRT link
        async def post(self, url, headers=None, json=None):
            return Resp(js={"link": "http://cdn/x.srt"})

    monkeypatch.setattr(OS.httpx, "AsyncClient", Client)
    c = OS.OpenSubtitlesClient("key", cache_dir=tmp_path)
    srt = await c.fetch_episode_srt(1043, 2, 6, "eng")   # pass 639-2/B
    assert captured["languages"] == "en"                 # normalised for the API
    assert srt and "Hi" in srt                           # the 'en' result was accepted
