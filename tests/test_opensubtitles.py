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
