"""OpenSubtitles client error/quota paths, the reference-SRT cache, and the
subtitle→episode scoring that decides when a match is too weak to trust.

Everything here is offline: ``httpx.AsyncClient`` is replaced by a recorder that
replays canned responses, so a test that claims "no request was made" really does
prove the code took the short-circuit.
"""

from __future__ import annotations

import httpx
import pytest

from blurayripper.models import EpisodeAssignment, MediaKind
from blurayripper.services import opensubtitles as OS
from blurayripper.services import os_upload
from blurayripper.services.opensubtitles import (
    OpenSubtitlesClient,
    OpenSubtitlesError,
    QuotaExhausted,
    best_file_id,
    gather_references,
    quota_reset_at,
)
from blurayripper.services.sub_match import identify, similarity

# ---------------------------------------------------------------------------
# httpx stand-ins
# ---------------------------------------------------------------------------

_REQ = httpx.Request("GET", "https://api.opensubtitles.com/api/v1/x")


def _json(status: int, body) -> httpx.Response:
    return httpx.Response(status, json=body, request=_REQ)


def _text(status: int, body: str) -> httpx.Response:
    return httpx.Response(status, text=body, request=_REQ)


class _Call:
    def __init__(self, method, url, headers, params, body):
        self.method = method
        self.url = url
        self.headers = headers
        self.params = params
        self.body = body


class _FakeHTTP:
    """Replays canned responses keyed by URL fragment; records every request."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[_Call] = []

    def factory(self, *_a, **kw):
        self.kwargs = kw
        return self

    def to(self, fragment: str) -> list[_Call]:
        return [c for c in self.calls if fragment in c.url]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def get(self, url, headers=None, params=None):
        return self._reply("GET", url, headers, params, None)

    async def post(self, url, headers=None, json=None):
        return self._reply("POST", url, headers, None, json)

    def _reply(self, method, url, headers, params, body):
        call = _Call(method, url, dict(headers or {}), params, body)
        self.calls.append(call)
        for fragment, resp in self.routes.items():
            if fragment in url:
                return resp(call) if callable(resp) else resp
        raise AssertionError(f"unstubbed request: {method} {url}")


class _NoNetwork:
    def __init__(self, *_a, **_kw):
        raise AssertionError("an HTTP client was constructed; expected none")


SRT_BODY = "1\n00:00:01,000 --> 00:00:02,000\nHello there\n"
LINK = "https://cdn.example/ref.srt"


def _search_hit(file_id: int = 42, language: str = "en") -> httpx.Response:
    return _json(200, {"data": [{"attributes": {
        "language": language, "download_count": 9, "files": [{"file_id": file_id}]}}]})


def _routes(**overrides) -> dict:
    # The real API only ever returns uploads in the language that was asked for.
    routes = {
        "/subtitles": lambda call: _search_hit(language=call.params["languages"]),
        "/download": _json(200, {"link": LINK, "remaining": 96, "requests": 4}),
        LINK: _text(200, SRT_BODY),
    }
    routes.update(overrides)
    return routes


def _install(monkeypatch, routes: dict) -> _FakeHTTP:
    http = _FakeHTTP(routes)
    monkeypatch.setattr(OS.httpx, "AsyncClient", http.factory)
    return http


# ---------------------------------------------------------------------------
# best_file_id — which upload gets downloaded
# ---------------------------------------------------------------------------

def _item(file_id, *, lang="en", trusted=False, downloads=0, ratings=0.0, files=None):
    attrs = {"language": lang, "from_trusted": trusted, "download_count": downloads,
             "ratings": ratings}
    if lang is None:
        attrs.pop("language")
    attrs["files"] = [{"file_id": file_id}] if files is None else files
    return {"attributes": attrs}


def test_equal_download_counts_are_broken_by_rating():
    """Rating is the last tiebreak. Without it two equally-downloaded uploads fall
    back to list order, which is whatever the API felt like returning."""
    data = [_item(1, downloads=100, ratings=6.0), _item(2, downloads=100, ratings=9.1)]
    assert best_file_id(data) == 2
    # ...and the rating must not outrank download count, which is the primary key.
    assert best_file_id([_item(1, downloads=100, ratings=1.0),
                         _item(2, downloads=5, ratings=10.0)]) == 1


def test_entry_without_a_language_field_is_dropped_only_when_filtering():
    """An upload whose attributes omit ``language`` must not be treated as a match
    for the requested language — that is how a Polish sub ends up scored against
    an English reference."""
    data = [_item(1, lang=None, downloads=999), _item(2, lang="en", downloads=5)]
    assert best_file_id(data, language="en") == 2   # languageless entry rejected
    assert best_file_id(data) == 1                  # unfiltered, it is the best


def test_malformed_entries_do_not_abort_the_whole_search():
    """The API occasionally returns null attributes; one bad row must not lose the
    good rows behind it."""
    data = [{"attributes": None}, {}, _item(7, downloads=1)]
    assert best_file_id(data) == 7


# ---------------------------------------------------------------------------
# gather_references — per-episode failure policy
# ---------------------------------------------------------------------------

async def test_non_fatal_failure_does_not_stop_the_remaining_episodes():
    """Only a *fatal* error (bad key / quota) aborts the loop. A per-episode
    failure must leave the other episodes still identifiable."""
    seen: list[int] = []

    class Flaky:
        async def fetch_episode_srt(self, tmdb, season, ep, lang):
            seen.append(ep)
            if ep == 6:
                raise OpenSubtitlesError("transient", fatal=False)
            return f"srt-{ep}"

    refs = await gather_references(Flaky(), 1043, 1, [6, 7, 8], "en")
    assert seen == [6, 7, 8]
    assert refs == {7: "srt-7", 8: "srt-8"}


async def test_unexpected_exception_for_one_episode_keeps_the_others():
    """A non-OpenSubtitlesError (socket reset, bad JSON) must be absorbed the same
    way — otherwise one flaky episode fails identification for the whole disc."""
    class Boom:
        async def fetch_episode_srt(self, tmdb, season, ep, lang):
            if ep == 6:
                raise ValueError("Expecting value: line 1 column 1")
            return f"srt-{ep}"

    assert await gather_references(Boom(), 1043, 1, [6, 7], "en") == {7: "srt-7"}


async def test_total_failure_reports_the_real_reason_not_an_empty_dict():
    """When nothing at all could be fetched the caller must see why; an empty dict
    would look like 'this show has no subtitles'."""
    class Boom:
        async def fetch_episode_srt(self, *a):
            raise ValueError("connection reset by peer")

    with pytest.raises(OpenSubtitlesError) as ei:
        await gather_references(Boom(), 1043, 1, [6, 7], "en")
    assert "connection reset by peer" in str(ei.value)
    assert ei.value.fatal is False  # an unknown error is not assumed fatal


# ---------------------------------------------------------------------------
# login / auth headers
# ---------------------------------------------------------------------------

async def test_search_carries_the_bearer_token_obtained_at_login(monkeypatch):
    """The token from /login must be attached to later calls — it is what raises
    the account's daily download quota above the anonymous Api-Key limit."""
    http = _install(monkeypatch, _routes(**{"/login": _json(200, {"token": "tok-123"})}))
    client = OpenSubtitlesClient("key", "user", "pw")

    await client.fetch_episode_srt(1043, 2, 6, "en")

    assert http.to("/subtitles")[0].headers["Authorization"] == "Bearer tok-123"
    assert http.to("/download")[0].headers["Authorization"] == "Bearer tok-123"


async def test_anonymous_client_sends_no_authorization_header(monkeypatch):
    """Without credentials there is no token, and an empty ``Bearer`` header would
    make OpenSubtitles reject an otherwise valid Api-Key request."""
    http = _install(monkeypatch, _routes())
    await OpenSubtitlesClient("key").fetch_episode_srt(1043, 2, 6, "en")
    assert "Authorization" not in http.to("/subtitles")[0].headers


async def test_login_happens_once_across_many_episodes(monkeypatch):
    """Each episode opens a fresh HTTP client but must reuse the cached token; a
    login per episode is a wasted round-trip on every disc."""
    http = _install(monkeypatch, _routes(**{"/login": _json(200, {"token": "tok-123"})}))
    client = OpenSubtitlesClient("key", "user", "pw")

    await client.fetch_episode_srt(1043, 2, 6, "en")
    await client.fetch_episode_srt(1043, 2, 7, "en")

    assert len(http.to("/download")) == 2   # both episodes really were fetched
    assert len(http.to("/login")) == 1


async def test_login_failure_falls_back_to_the_raw_body_when_it_is_not_json():
    """A gateway/HTML error page has no ``message`` field; the operator still needs
    to see what came back instead of a JSONDecodeError traceback."""
    class Stub:
        async def post(self, *a, **k):
            return _text(502, "<html>Bad Gateway from the edge</html>" + "x" * 500)

    client = OpenSubtitlesClient("key", "user", "pw")
    with pytest.raises(OpenSubtitlesError) as ei:
        await client._login(Stub(), required=True)
    msg = str(ei.value)
    assert "HTTP 502" in msg and "Bad Gateway from the edge" in msg
    assert msg.count("x") <= 200          # body is truncated, not dumped whole
    assert ei.value.fatal is True


# ---------------------------------------------------------------------------
# fetch_episode_srt — misses, quota, caching
# ---------------------------------------------------------------------------

async def test_missing_api_key_fails_before_any_request(monkeypatch):
    """Without a key every call is guaranteed to 401; the guard names the setting
    to fix instead of spending a round-trip to find that out."""
    monkeypatch.setattr(OS.httpx, "AsyncClient", _NoNetwork)
    with pytest.raises(OpenSubtitlesError) as ei:
        await OpenSubtitlesClient(None).fetch_episode_srt(1043, 2, 6, "en")
    assert "OPENSUBTITLES_API_KEY" in str(ei.value)


async def test_episode_with_no_subtitles_returns_none_without_spending_quota(monkeypatch):
    """An episode nobody has subtitled is a miss, not an error — and it must not
    burn a /download from the daily allowance."""
    http = _install(monkeypatch, _routes(**{"/subtitles": _json(200, {"data": []})}))
    assert await OpenSubtitlesClient("key").fetch_episode_srt(1043, 2, 6, "en") is None
    assert http.to("/download") == []


async def test_search_response_missing_the_data_key_is_a_miss(monkeypatch):
    """A 200 with an unexpected shape must degrade to 'no subtitles' rather than
    raising KeyError out of the identification run."""
    http = _install(monkeypatch, _routes(**{"/subtitles": _json(200, {"status": "ok"})}))
    assert await OpenSubtitlesClient("key").fetch_episode_srt(1043, 2, 6, "en") is None
    assert http.to("/download") == []


async def test_over_quota_200_body_raises_fatally_and_records_zero_remaining(monkeypatch):
    """Exhausted quota comes back HTTP 200 with a message and no link. Treating it
    as a plain miss would silently identify every remaining episode as unknown."""
    body = {"message": "You have downloaded your allowed 100 subtitles for 24 hours.",
            "requests": 100, "reset_time_utc": "2026-08-22T04:10:00Z"}
    _install(monkeypatch, _routes(**{"/download": _json(200, body)}))
    client = OpenSubtitlesClient("key")

    with pytest.raises(OpenSubtitlesError) as ei:
        await client.fetch_episode_srt(1043, 2, 6, "en")

    assert ei.value.fatal is True
    assert "allowed 100 subtitles" in str(ei.value)
    quota = client.last_known_quota()
    assert quota["remaining"] == 0
    assert quota["reset_time_utc"] == "2026-08-22T04:10:00Z"  # UI shows when it lifts


async def test_link_less_response_without_a_quota_message_is_only_a_miss(monkeypatch):
    """Not every missing link is the quota. Anything else must stay a per-episode
    miss so the rest of the disc is still identified."""
    _install(monkeypatch, _routes(
        **{"/download": _json(200, {"message": "file temporarily unavailable"})}))
    client = OpenSubtitlesClient("key")

    assert await client.fetch_episode_srt(1043, 2, 6, "en") is None
    assert client.last_known_quota()["remaining"] is None  # not marked exhausted


async def test_downloaded_reference_is_cached_and_reused(monkeypatch, tmp_path):
    """Re-running identification (or the same episode on a second disc) must cost
    zero download quota — the cache is the only thing standing between a retry and
    the daily limit."""
    http = _install(monkeypatch, _routes())
    client = OpenSubtitlesClient("key", cache_dir=tmp_path / "refs")

    first = await client.fetch_episode_srt(1043, 2, 6, "en")
    assert first == SRT_BODY
    assert (tmp_path / "refs" / "1043_s02e06_en.srt").read_text() == SRT_BODY

    calls_after_first = len(http.calls)
    second = await OpenSubtitlesClient("key", cache_dir=tmp_path / "refs").fetch_episode_srt(
        1043, 2, 6, "en")
    assert second == SRT_BODY
    assert len(http.calls) == calls_after_first  # nothing at all went over the wire


async def test_cache_is_keyed_per_episode_and_language(monkeypatch, tmp_path):
    """A cached English S02E06 must never satisfy a request for another episode or
    another language — that would hand the matcher the wrong reference text."""
    http = _install(monkeypatch, _routes())
    client = OpenSubtitlesClient("key", cache_dir=tmp_path / "refs")

    await client.fetch_episode_srt(1043, 2, 6, "en")
    await client.fetch_episode_srt(1043, 2, 7, "en")
    await client.fetch_episode_srt(1043, 2, 6, "pol")

    assert len(http.to("/download")) == 3
    names = sorted(p.name for p in (tmp_path / "refs").iterdir())
    assert names == ["1043_s02e06_en.srt", "1043_s02e06_pl.srt", "1043_s02e07_en.srt"]


async def test_without_a_cache_dir_every_call_downloads_again(monkeypatch):
    """The cache is opt-in. An unconfigured client must not quietly park SRTs in
    some shared default location — the same repeat that is free with a cache dir
    costs quota without one, and that difference is the whole point of the setting."""
    http = _install(monkeypatch, _routes())
    client = OpenSubtitlesClient("key")

    await client.fetch_episode_srt(1043, 2, 6, "en")
    await client.fetch_episode_srt(1043, 2, 6, "en")

    assert len(http.to("/download")) == 2


async def test_http_429_captures_the_quota_reset_before_raising(monkeypatch):
    """When the API hard-rejects with 429 the reset time is in that response only.
    Losing it means the UI cannot tell the user when to retry."""
    _install(monkeypatch, _routes(**{"/download": _json(
        429, {"requests": 100, "reset_time": "5 hours",
              "reset_time_utc": "2026-08-22T04:10:00Z"})}))
    client = OpenSubtitlesClient("key")

    with pytest.raises(OpenSubtitlesError) as ei:
        await client.fetch_episode_srt(1043, 2, 6, "en")

    assert ei.value.fatal is True and "quota" in str(ei.value).lower()
    assert client.last_known_quota() == {
        "remaining": 0, "requests": 100, "reset_time": "5 hours",
        "reset_time_utc": "2026-08-22T04:10:00Z"}


async def test_http_429_with_an_unparseable_body_still_records_exhaustion(monkeypatch):
    """A 429 served by a CDN is HTML. The quota must still read as spent, else the
    UI keeps advertising downloads that will all fail."""
    _install(monkeypatch, _routes(**{"/download": _text(429, "<html>Too Many Requests</html>")}))
    client = OpenSubtitlesClient("key")

    with pytest.raises(OpenSubtitlesError):
        await client.fetch_episode_srt(1043, 2, 6, "en")
    assert client.last_known_quota() == {"remaining": 0}


# --- when the quota comes back --------------------------------------------------------

def _utc(text):
    from datetime import datetime, timezone
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def test_the_reset_time_reads_with_or_without_milliseconds():
    assert quota_reset_at({"reset_time_utc": "2026-08-22T04:10:00Z"}) == _utc("2026-08-22T04:10:00")
    assert quota_reset_at({"reset_time_utc": "2026-08-22T04:10:00.000Z"}) == _utc("2026-08-22T04:10:00")
    assert quota_reset_at({"reset_time_utc": "2026-08-22T04:10:00"}) == _utc("2026-08-22T04:10:00")
    for unreadable in (None, {}, {"reset_time_utc": None}, {"reset_time_utc": "soon"},
                       {"reset_time_utc": 5}):
        assert quota_reset_at(unreadable) is None


async def test_a_spent_quota_says_when_it_comes_back(monkeypatch):
    """Waiting is the only cure, so the error has to say until when. Without it a
    disc identified by its dialogue failed and retried every few minutes for the
    rest of the day."""
    body = {"message": "You have downloaded your allowed 100 subtitles for 24 hours.",
            "reset_time_utc": "2026-08-22T04:10:00Z"}
    _install(monkeypatch, _routes(**{"/download": _json(200, body)}))
    with pytest.raises(QuotaExhausted) as ei:
        await OpenSubtitlesClient("key").fetch_episode_srt(1043, 2, 6, "en")
    assert ei.value.reset_at == _utc("2026-08-22T04:10:00") and ei.value.fatal is True

    _install(monkeypatch, _routes(**{"/download": _json(
        406, {"reset_time_utc": "2026-08-23T01:00:00.000Z"})}))
    with pytest.raises(QuotaExhausted) as ei:
        await OpenSubtitlesClient("key").fetch_episode_srt(1043, 2, 6, "en")
    assert ei.value.reset_at == _utc("2026-08-23T01:00:00") and ei.value.status == 406


async def test_a_406_without_a_reset_time_uses_the_one_an_earlier_download_gave(monkeypatch):
    replies = iter([_json(200, {"link": LINK, "remaining": 1,
                                "reset_time_utc": "2026-08-22T04:10:00Z"}),
                    _text(406, "<html>quota</html>")])
    _install(monkeypatch, _routes(**{"/download": lambda call: next(replies)}))
    client = OpenSubtitlesClient("key")
    await client.fetch_episode_srt(1043, 2, 6, "en")
    with pytest.raises(QuotaExhausted) as ei:
        await client.fetch_episode_srt(1043, 2, 7, "en")
    assert ei.value.reset_at == _utc("2026-08-22T04:10:00")


async def test_a_bare_429_does_not_borrow_the_daily_reset(monkeypatch):
    """A 429 with no reset time can be the per-second rate limit. Taking the
    daily reset from an earlier download would wait hours for a pause of
    seconds."""
    replies = iter([_json(200, {"link": LINK, "remaining": 5,
                                "reset_time_utc": "2026-08-22T04:10:00Z"}),
                    _text(429, "<html>Too Many Requests</html>")])
    _install(monkeypatch, _routes(**{"/download": lambda call: next(replies)}))
    client = OpenSubtitlesClient("key")
    await client.fetch_episode_srt(1043, 2, 6, "en")
    with pytest.raises(QuotaExhausted) as ei:
        await client.fetch_episode_srt(1043, 2, 7, "en")
    assert ei.value.reset_at is None and ei.value.status == 429


def test_only_the_quota_statuses_are_quota_errors():
    def err(code):
        return httpx.HTTPStatusError("x", request=_REQ, response=_text(code, ""))
    assert isinstance(OpenSubtitlesClient._classify_http_error(err(406)), QuotaExhausted)
    assert isinstance(OpenSubtitlesClient._classify_http_error(err(429)), QuotaExhausted)
    for code in (401, 403, 500):
        assert not isinstance(OpenSubtitlesClient._classify_http_error(err(code)),
                              QuotaExhausted)


def test_a_cached_reference_is_read_without_any_request(monkeypatch, tmp_path):
    monkeypatch.setattr(OS.httpx, "AsyncClient", _NoNetwork)
    client = OpenSubtitlesClient("key", cache_dir=tmp_path)
    (tmp_path / "1043_s02e06_en.srt").write_text(SRT_BODY)
    assert client.cached_episode_srt(1043, 2, 6, "eng") == SRT_BODY   # 639-2 normalised
    assert client.cached_episode_srt(1043, 2, 7, "en") is None
    assert OpenSubtitlesClient("key").cached_episode_srt(1043, 2, 6, "en") is None


class _QuotaAt:
    """Downloads until episode ``spent_at``, then the quota is gone. Some later
    episodes are already in the cache from another day."""

    def __init__(self, spent_at, cached=()):
        self.spent_at, self.cached, self.asked = spent_at, set(cached), []

    async def fetch_episode_srt(self, tmdb, season, ep, lang):
        self.asked.append(ep)
        if ep in self.cached:
            return f"ref {ep}"
        if ep >= self.spent_at:
            raise QuotaExhausted("quota", reset_at=_utc("2026-08-22T04:10:00"), status=406)
        return f"ref {ep}"

    def cached_episode_srt(self, tmdb, season, ep, lang):
        return f"ref {ep}" if ep in self.cached else None


async def test_after_the_quota_runs_out_cached_references_are_still_read():
    """Stopping at the first quota error threw away references downloaded on an
    earlier day for the episodes after it."""
    client = _QuotaAt(spent_at=3, cached={5})
    refs = await gather_references(client, 1100, 1, [1, 2, 3, 4, 5, 6], "en")
    assert refs == {1: "ref 1", 2: "ref 2", 5: "ref 5"}
    assert client.asked == [1, 2, 3]          # nothing asked of the API after the cut


async def test_stop_on_quota_hands_back_what_it_got_and_what_it_could_not():
    client = _QuotaAt(spent_at=3, cached={5})
    with pytest.raises(QuotaExhausted) as ei:
        await gather_references(client, 1100, 1, [1, 2, 3, 4, 5, 6], "en",
                                stop_on_quota=True)
    assert ei.value.partial == {1: "ref 1", 2: "ref 2", 5: "ref 5"}
    assert ei.value.untried == [3, 4, 6]
    assert ei.value.reset_at == _utc("2026-08-22T04:10:00")


async def test_a_quota_cut_before_anything_was_got_still_raises_by_default():
    with pytest.raises(QuotaExhausted) as ei:
        await gather_references(_QuotaAt(spent_at=1), 1100, 1, [1, 2], "en")
    assert ei.value.fatal is True and ei.value.untried == [1, 2]


async def test_http_401_is_fatal_and_leaves_the_quota_snapshot_untouched(monkeypatch):
    """A rejected key is an auth problem, not a quota problem; reporting it as
    'quota exhausted' sends the user off to wait a day for nothing."""
    _install(monkeypatch, _routes(**{"/subtitles": _json(401, {"message": "invalid api key"})}))
    client = OpenSubtitlesClient("badkey")

    with pytest.raises(OpenSubtitlesError) as ei:
        await client.fetch_episode_srt(1043, 2, 6, "en")

    assert ei.value.fatal is True
    assert "quota" not in str(ei.value).lower()
    assert client.last_known_quota() is None


async def test_http_500_is_reported_as_retryable(monkeypatch):
    """A server-side failure must NOT be fatal, or one bad response aborts every
    remaining episode on the disc."""
    _install(monkeypatch, _routes(**{"/subtitles": _json(500, {"message": "boom"})}))
    with pytest.raises(OpenSubtitlesError) as ei:
        await OpenSubtitlesClient("key").fetch_episode_srt(1043, 2, 6, "en")
    assert ei.value.fatal is False and "500" in str(ei.value)


async def test_language_is_normalised_for_the_cache_filename(monkeypatch, tmp_path):
    """Config speaks ISO 639-2/B ('pol'); the cache key must use the same 639-1
    form the API does, or 'pol' and 'pl' cache the same file twice."""
    http = _install(monkeypatch, _routes())
    client = OpenSubtitlesClient("key", cache_dir=tmp_path / "refs")

    await client.fetch_episode_srt(1043, 2, 6, "pol")
    assert http.to("/subtitles")[0].params["languages"] == "pl"
    assert [p.name for p in (tmp_path / "refs").iterdir()] == ["1043_s02e06_pl.srt"]

    await client.fetch_episode_srt(1043, 2, 6, "pl")  # same language, other spelling
    assert len(http.to("/download")) == 1


# ---------------------------------------------------------------------------
# user_info — the quota panel
# ---------------------------------------------------------------------------

async def test_user_info_without_a_key_is_fatal_and_makes_no_request(monkeypatch):
    monkeypatch.setattr(OS.httpx, "AsyncClient", _NoNetwork)
    with pytest.raises(OpenSubtitlesError) as ei:
        await OpenSubtitlesClient(None).user_info()
    assert ei.value.fatal is True and "API key" in str(ei.value)


async def test_user_info_unwraps_the_data_envelope(monkeypatch):
    """/infos/user nests everything under ``data``; returning the envelope would
    show the quota panel a dict with no download counts in it."""
    payload = {"data": {"allowed_downloads": 100, "downloads_count": 4,
                        "remaining_downloads": 96, "level": "Sub leecher", "vip": False}}
    http = _install(monkeypatch, {"/login": _json(200, {"token": "tok-9"}),
                                  "/infos/user": _json(200, payload)})

    info = await OpenSubtitlesClient("key", "user", "pw").user_info()

    assert info == payload["data"]
    assert http.to("/infos/user")[0].headers["Authorization"] == "Bearer tok-9"


async def test_user_info_tolerates_a_null_data_envelope(monkeypatch):
    """``{"data": null}`` must become an empty dict, not None — callers index it."""
    _install(monkeypatch, {"/login": _json(200, {"token": "t"}),
                           "/infos/user": _json(200, {"data": None})})
    assert await OpenSubtitlesClient("key", "user", "pw").user_info() == {}


async def test_user_info_requires_login_and_never_falls_through_anonymously(monkeypatch):
    """The Api-Key alone cannot read /infos/user, so a failed login must surface the
    login reason rather than letting the request go out unauthenticated."""
    http = _install(monkeypatch, {
        "/login": _json(401, {"message": "wrong username or password"}),
        "/infos/user": _json(200, {"data": {"remaining_downloads": 96}})})

    with pytest.raises(OpenSubtitlesError) as ei:
        await OpenSubtitlesClient("key", "user", "pw").user_info()

    assert "wrong username or password" in str(ei.value)
    assert http.to("/infos/user") == []


async def test_user_info_http_error_is_translated(monkeypatch):
    """A 403 from /infos/user must arrive as an OpenSubtitlesError with an
    actionable message, not a raw httpx exception the web layer cannot render."""
    _install(monkeypatch, {"/login": _json(200, {"token": "t"}),
                           "/infos/user": _json(403, {"message": "forbidden"})})

    with pytest.raises(OpenSubtitlesError) as ei:
        await OpenSubtitlesClient("key", "user", "pw").user_info()
    assert ei.value.fatal is True and "OPENSUBTITLES_API_KEY" in str(ei.value)


async def test_garbage_search_body_drops_one_episode_not_the_run(monkeypatch):
    """End to end: a 200 whose body is not JSON raises out of the client, and
    gather_references turns that into a per-episode miss with the reason kept."""
    _install(monkeypatch, _routes(**{"/subtitles": _text(200, "<html>maintenance</html>")}))
    client = OpenSubtitlesClient("key")

    with pytest.raises(OpenSubtitlesError) as ei:
        await gather_references(client, 1043, 2, [6], "en")
    assert ei.value.fatal is False


# ---------------------------------------------------------------------------
# sub_match — scoring and confidence
# ---------------------------------------------------------------------------

def _srt(lines: list[str]) -> str:
    return "\n".join(
        f"{i}\n00:00:{i:02d},000 --> 00:00:{i + 1:02d},000\n{t}\n"
        for i, t in enumerate(lines, 1)
    )


SHARED = [f"the reactor core is overheating fast {i}" for i in range(20)]
EP_A = SHARED + [f"we must vent the plasma before dawn {i}" for i in range(20)]
EP_B = SHARED + [f"she walked along the quiet beach alone {i}" for i in range(20)]
EP_C = [f"they argued about money at the dinner {i}" for i in range(40)]
REFS = {10: _srt(EP_A), 11: _srt(EP_B), 12: _srt(EP_C)}


def _ocr(lines: list[str]) -> list[str]:
    out = []
    for k, ln in enumerate(lines):
        if k % 5 == 0:
            ln = ln.replace("e", "c", 1)
        if k % 7 == 0:
            ln = " ".join(ln.split()[:-1])
        out.append(ln)
    return out


def test_runner_up_is_the_second_best_episode_not_the_assigned_one():
    """The confidence margin is only meaningful if the runner-up really is the
    closest *other* episode; comparing against itself would make everything look
    certain."""
    rips = {0: _srt(_ocr(EP_A))}
    m = identify(rips, REFS)[0]
    assert m.episode == 10
    assert m.runner_up == 11             # shares half its dialogue with A
    assert 0.0 < m.runner_up_score < m.score


def test_a_weak_best_match_is_not_accepted():
    """A rip can be the best of the candidates and still be nothing like any of
    them (bad OCR, wrong disc). Below ``min_score`` it must be flagged instead of
    writing an episode number."""
    rips = {0: _srt(_ocr(EP_A))}
    assert identify(rips, REFS, min_score=0.10)[0].confident is True
    weak = identify(rips, REFS, min_score=0.95)[0]
    assert weak.episode == 10 and weak.confident is False


def test_a_best_match_that_barely_beats_the_runner_up_is_not_accepted():
    """Two episodes that score almost the same mean the assignment is a coin flip;
    the margin rule is what stops a coin flip being written to disk."""
    rips = {0: _srt(_ocr(EP_A))}
    assert identify(rips, REFS, min_margin=0.04)[0].confident is True
    tight = identify(rips, REFS, min_margin=0.95)[0]
    assert tight.episode == 10 and tight.confident is False


def test_a_title_with_no_dialogue_is_never_confidently_assigned():
    """An OCR pass that produced nothing must not be handed the leftover episode
    number just because it is the only one left."""
    rips = {0: _srt(_ocr(EP_A)), 1: ""}
    by_id = {m.title_id: m for m in identify(rips, REFS)}
    assert by_id[0].episode == 10 and by_id[0].confident
    assert by_id[1].score == 0.0 and by_id[1].confident is False


def test_extra_titles_get_no_episode_rather_than_a_wrong_one():
    """More ripped titles than known episodes (bonus features, duplicate angles):
    the surplus must come back unassigned."""
    rips = {0: _srt(_ocr(EP_A)), 1: _srt(_ocr(EP_C))}
    matches = identify(rips, {10: _srt(EP_A)})
    assigned = [m for m in matches if m.episode is not None]
    unassigned = [m for m in matches if m.episode is None]
    assert [m.title_id for m in assigned] == [0]
    assert len(unassigned) == 1
    assert unassigned[0].score == 0.0 and unassigned[0].confident is False
    assert unassigned[0].runner_up_score == 0.0


def test_results_follow_the_order_titles_were_given_in():
    """Callers zip the results against their own title list, so reordering here
    would silently pair each title with another title's episode."""
    rips = {7: _srt(_ocr(EP_C)), 3: _srt(_ocr(EP_A)), 5: _srt(_ocr(EP_B))}
    assert [m.title_id for m in identify(rips, REFS)] == [7, 3, 5]


def test_a_handful_of_words_still_scores_instead_of_silently_zeroing():
    """Forced/partial tracks can be shorter than the trigram window. Falling back
    to bare words keeps them comparable; returning nothing would score every short
    title 0 and flag the whole disc for manual mapping."""
    assert similarity(_srt(["Hello there"]), _srt(["Hello there"])) == 1.0
    assert similarity(_srt(["Hello there"]), _srt(["Goodbye now"])) == 0.0


def test_bracketed_sound_cues_do_not_contribute_to_the_score():
    """SDH sound cues describe what is heard, not what is said. Counting them lets
    an episode full of ``[gunshot]`` look like any other action episode."""
    cues = _srt([f"[a door creaks open in the distance {i}]" for i in range(20)])
    spoken = _srt([f"a door creaks open in the distance {i}" for i in range(20)])
    assert similarity(cues, spoken) == 0.0


def test_speaker_labels_are_stripped_before_comparing():
    """A rip's SDH track names the speaker; the OpenSubtitles reference usually
    does not. Left in, the labels drag every real match's score down."""
    labelled = _srt([f"CAPTAIN JANE: fire the thrusters now {i}" for i in range(20)])
    plain = _srt([f"fire the thrusters now {i}" for i in range(20)])
    assert similarity(labelled, plain) == 1.0


def test_markup_tags_never_become_dialogue_words():
    """OpenSubtitles SRTs are full of ``<font color=...>``; treating the markup as
    dialogue makes an identical script score as a partial match."""
    tagged = _srt([f'<font color="#00ff00">run for the exit now {i}</font>'
                   for i in range(20)])
    plain = _srt([f"run for the exit now {i}" for i in range(20)])
    assert similarity(tagged, plain) == 1.0


def test_empty_subtitle_text_scores_zero_without_dividing_by_zero():
    """Two empty tracks have no overlap to speak of; the score must be 0.0, not a
    crash in the middle of identifying a disc."""
    assert similarity("", REFS[10]) == 0.0
    assert similarity("", "") == 0.0


# ---------------------------------------------------------------------------
# os_upload — the manual upload sheet
# ---------------------------------------------------------------------------

def _movie(year=1999, title="The Film"):
    return EpisodeAssignment(0, MediaKind.MOVIE, title, year)


def _episode(season=2, episode=5):
    return EpisodeAssignment(0, MediaKind.TV, "The Show", 2015, season=season,
                             episode=episode, episode_title="Pilot")


def test_language_name_falls_back_to_the_raw_code():
    """An unlisted language must still produce a usable sheet rather than a blank
    or crashing field."""
    assert os_upload.language_name(None) == "Undetermined"
    assert os_upload.language_name("") == "Undetermined"
    assert os_upload.language_name("qaa") == "qaa"
    assert os_upload.language_name("ENG") == "English"   # code case is irrelevant


def test_episodes_are_grouped_by_season_and_movies_by_year():
    """One upload folder per season keeps a 24-episode box set from becoming 24
    sibling folders in the export dir."""
    assert os_upload.movie_folder(_episode()) == "The Show - Season 2"
    assert os_upload.movie_folder(_movie()) == "The Film (1999)"
    assert os_upload.movie_folder(_movie(year=None)) == "The Film"


def test_release_name_encodes_season_episode_for_tv_and_year_for_films():
    assert os_upload.suggested_release_name(_episode(), 1080) == "The.Show.S02E05.1080p.BluRay"
    assert os_upload.suggested_release_name(_movie(), None) == "The.Film.1999.BluRay"
    # A TV title whose episode is not known yet falls back to the show's year.
    assert os_upload.suggested_release_name(_episode(episode=None), 720) == \
        "The.Show.2015.720p.BluRay"


def test_sheet_for_a_film_without_a_year_omits_the_empty_parentheses():
    """``The Film ()`` would be pasted straight into the OpenSubtitles title search
    and find nothing."""
    sheet = os_upload.build_info_sheet(
        srt_name="x.srt", assignment=_movie(year=None), language_code="eng",
        is_sdh=False, forced=False, height=1080, fps=24.0, version="v1")
    assert "Movie name:            The Film\n" in sheet
    assert "()" not in sheet


def test_sheet_for_an_unidentified_episode_omits_the_season_episode_code():
    """Season/episode may still be unknown at export time; formatting None would
    raise, and inventing S00E00 would be uploaded as fact."""
    sheet = os_upload.build_info_sheet(
        srt_name="x.srt", assignment=_episode(season=None, episode=None),
        language_code="eng", is_sdh=False, forced=False, height=1080, fps=24.0,
        version="v1")
    episode_line = [ln for ln in sheet.splitlines() if ln.startswith("Episode:")][0]
    assert "S" not in episode_line.replace("Episode:", "")
    assert "Pilot" in episode_line


def test_export_folder_name_cannot_escape_the_export_dir():
    """A title with a slash (``Face/Off``) would otherwise create a nested
    directory — or on Windows fail the mkdir outright."""
    assert "/" not in os_upload._safe(os_upload.movie_folder(
        _movie(title="Face/Off: Part 2")))


def test_export_places_the_srt_and_sheet_in_one_sanitized_folder(tmp_path):
    """The sheet must land beside the exact SRT it describes; a mismatched pair is
    worse than no export at all."""
    srt = tmp_path / "Face-Off (1997).en.srt"
    srt.write_text(SRT_BODY, encoding="utf-8")
    dest = os_upload.export_subtitle(
        tmp_path / "upload", srt, assignment=_movie(year=1997, title="Face/Off"),
        language_code="eng", is_sdh=False, forced=False, height=1080, fps=23.976,
        version="v1")

    assert dest.parent.parent == tmp_path / "upload"   # not nested via the slash
    assert dest.read_text() == SRT_BODY
    sheet = (dest.parent / (srt.name + ".info.txt")).read_text()
    assert f"Subtitle file to upload:  {srt.name}" in sheet
