"""TMDB client unit tests: what goes on the wire, what comes back off it.

This module decides WHICH film or show a disc becomes, so every test here stubs
HTTP at the transport layer and lets real httpx build the URL/query string and do
``raise_for_status`` / ``.json()`` — the assertions are about the request TMDB
would actually receive and the objects the pipeline actually gets back.
"""

import stat
from pathlib import Path

import httpx
import pytest

from blurayripper.services import metadata
from blurayripper.services.metadata import MetadataError, TMDBClient


def _stub_tmdb(monkeypatch, handler):
    """Route every httpx.AsyncClient the module builds through an in-process
    transport. Returns the list of captured httpx.Request objects."""
    seen: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    real_client = httpx.AsyncClient

    class _StubClient(real_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_record)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(metadata.httpx, "AsyncClient", _StubClient)
    return seen


def _json(payload, status: int = 200):
    """A handler that answers every request with the same JSON body."""
    return lambda request: httpx.Response(status, json=payload)


def _movie(tmdb_id, title, date, **extra):
    return {"id": tmdb_id, "title": title, "release_date": date,
            "overview": f"{title} overview", **extra}


def _show(tmdb_id, name, date, **extra):
    return {"id": tmdb_id, "name": name, "first_air_date": date,
            "overview": f"{name} overview", **extra}


# --------------------------------------------------------------------------
# request building
# --------------------------------------------------------------------------

async def test_a_movie_search_carries_the_key_language_title_and_year(monkeypatch):
    """Every one of these has to reach TMDB: no key is a 401, no language gives
    German overviews to an English library, and no year makes "Dumbo" ambiguous."""
    seen = _stub_tmdb(monkeypatch, _json({"results": []}))

    await TMDBClient("secret-key", "fr-FR").search_movie("Dumbo", year=1941)

    req = seen[0]
    assert req.url.host == "api.themoviedb.org"
    assert req.url.path == "/3/search/movie"
    assert dict(req.url.params) == {
        "api_key": "secret-key", "language": "fr-FR", "query": "Dumbo", "year": "1941"}


async def test_a_search_without_a_year_omits_the_filter_rather_than_sending_a_blank(
        monkeypatch):
    """TMDB treats ``year=`` / ``year=None`` as a literal filter and returns
    nothing, so an unknown year has to leave the parameter off entirely."""
    seen = _stub_tmdb(monkeypatch, _json({"results": []}))
    client = TMDBClient("k")

    await client.search_movie("WarGames")
    await client.search_movie("WarGames", year=None)

    assert [("year" in r.url.params) for r in seen] == [False, False]
    assert all(r.url.params["query"] == "WarGames" for r in seen)


async def test_a_show_search_hits_the_tv_endpoint_and_not_the_movie_one(monkeypatch):
    """/search/movie answers 200 with an empty result list for a show title, so a
    wrong endpoint here looks exactly like "TMDB doesn't know this series"."""
    seen = _stub_tmdb(monkeypatch, _json({"results": []}))

    await TMDBClient("k").search_tv("Silicon Valley")

    assert seen[0].url.path == "/3/search/tv"
    assert seen[0].url.params["query"] == "Silicon Valley"
    assert seen[0].url.params["api_key"] == "k"


async def test_a_season_request_addresses_the_show_and_season_by_number(monkeypatch):
    """Episode titles come from /tv/{id}/season/{n}; get the path wrong and every
    placed file is named after the wrong season."""
    seen = _stub_tmdb(monkeypatch, _json({"episodes": []}))

    await TMDBClient("k").get_season(4556, 2)

    assert seen[0].url.path == "/3/tv/4556/season/2"
    assert seen[0].url.params["api_key"] == "k"


# --------------------------------------------------------------------------
# response parsing
# --------------------------------------------------------------------------

async def test_movie_results_keep_tmdb_relevance_order_and_their_release_year(
        monkeypatch):
    """The confirm UI lists these in order and disambiguates on the year, so the
    year has to be parsed out of release_date rather than left to the caller."""
    seen = _stub_tmdb(monkeypatch, _json({"results": [
        _movie(11, "Dumbo", "2019-03-27", poster_path="/p2019.jpg"),
        _movie(22, "Dumbo", "1941-10-23", poster_path="/p1941.jpg",
               backdrop_path="/b1941.jpg"),
    ]}))

    got = await TMDBClient("k").search_movie("Dumbo")

    assert [(c.tmdb_id, c.title, c.year) for c in got] == [
        (11, "Dumbo", 2019), (22, "Dumbo", 1941)]
    assert got[1].overview == "Dumbo overview"
    assert (got[1].poster_path, got[1].backdrop_path) == ("/p1941.jpg", "/b1941.jpg")
    assert got[0].backdrop_path is None      # absent in the payload, not ""
    assert len(seen) == 1


async def test_an_unreleased_or_undated_result_has_no_year_instead_of_crashing(
        monkeypatch):
    """TMDB sends release_date "" for announced-but-undated films. Parsing that as
    an int would take down the whole search on one junk row."""
    _stub_tmdb(monkeypatch, _json({"results": [
        _movie(1, "Undated", ""),
        _movie(2, "Missing", None),
        _movie(3, "Partial", "19"),
        _movie(4, "Fine", "1983-06-03"),
    ]}))

    got = await TMDBClient("k").search_movie("x")

    assert [c.year for c in got] == [None, None, None, 1983]


async def test_a_result_with_a_blank_title_falls_back_to_its_original_title(
        monkeypatch):
    """Foreign-language entries often carry no localised title; showing a blank row
    in the confirm list is unpickable."""
    _stub_tmdb(monkeypatch, _json({"results": [
        _movie(1, "", "1967-10-25", original_title="Le Samouraï"),
        _movie(2, None, "1954-04-26", original_title="七人の侍"),
        _movie(3, "", "2000-01-01"),
    ]}))

    got = await TMDBClient("k").search_movie("x")

    assert [c.title for c in got] == ["Le Samouraï", "七人の侍", ""]


async def test_a_show_with_a_blank_name_falls_back_to_its_original_name(monkeypatch):
    """Same hole on the TV side — an unnamed candidate can never be matched to a
    box-set row, so the disc silently loses its episode titles."""
    _stub_tmdb(monkeypatch, _json({"results": [
        _show(1, "", "2015-05-13", original_name="Dark", poster_path="/d.jpg"),
        _show(2, "Hawaii Five-O", "1968-09-20"),
    ]}))

    got = await TMDBClient("k").search_tv("x")

    assert [(c.tmdb_id, c.name, c.year) for c in got] == [
        (1, "Dark", 2015), (2, "Hawaii Five-O", 1968)]
    assert got[0].poster_path == "/d.jpg"


async def test_an_episode_without_a_runtime_or_season_still_parses(monkeypatch):
    """TMDB omits runtime on most episodes and season_number on some; a KeyError
    here would abort episode naming for a whole box-set disc."""
    _stub_tmdb(monkeypatch, _json({"episodes": [
        {"episode_number": 1, "name": "Pilot", "runtime": 58,
         "season_number": 2, "overview": "first"},
        {"episode_number": 2, "name": "Cat's in the Bag..."},
    ]}))

    eps = await TMDBClient("k").get_season(4556, 2)

    assert [(e.episode_number, e.name) for e in eps] == [
        (1, "Pilot"), (2, "Cat's in the Bag...")]
    assert [e.runtime for e in eps] == [58, None]
    assert [e.season_number for e in eps] == [2, 2]   # defaulted from the request
    assert eps[1].overview == ""


async def test_a_payload_with_no_results_key_means_no_matches_not_a_crash(monkeypatch):
    """TMDB's own error/empty bodies carry no "results" (or "episodes") key at all;
    indexing it blindly turns "nothing found" into a 500 in the naming UI."""
    _stub_tmdb(monkeypatch, _json({"page": 1, "total_results": 0}))
    client = TMDBClient("k")

    assert await client.search_movie("nothing at all") == []
    assert await client.search_tv("nothing at all") == []
    assert await client.get_season(1, 1) == []


# --------------------------------------------------------------------------
# failure modes
# --------------------------------------------------------------------------

async def test_searching_without_an_api_key_fails_before_any_http_call(monkeypatch):
    """Without a key TMDB answers 401 for every request; failing up front keeps the
    error actionable ("set TMDB_API_KEY") instead of a bare HTTP status."""
    seen = _stub_tmdb(monkeypatch, _json({"results": [_movie(1, "Dumbo", "1941-10-23")]}))
    client = TMDBClient(None)

    assert client.configured is False
    assert TMDBClient("k").configured is True

    for call in (client.search_movie("Dumbo"), client.search_tv("Dumbo"),
                 client.get_season(1, 1)):
        with pytest.raises(MetadataError) as exc:
            await call
        assert "TMDB_API_KEY" in str(exc.value)

    assert seen == []   # nothing was sent, key or no key


async def test_an_http_error_from_tmdb_is_raised_rather_than_read_as_no_matches(
        monkeypatch):
    """An invalid key answers 401 with a body that has no "results". Swallowing
    that reports "no matches" forever and the user retypes the title instead of
    fixing the key."""
    _stub_tmdb(monkeypatch, _json(
        {"status_code": 7, "status_message": "Invalid API key"}, status=401))

    with pytest.raises((MetadataError, httpx.HTTPStatusError)) as exc:
        await TMDBClient("stale-key").search_movie("Dumbo")

    if isinstance(exc.value, httpx.HTTPStatusError):
        assert exc.value.response.status_code == 401


async def test_a_non_json_body_from_tmdb_is_raised_rather_than_read_as_no_matches(
        monkeypatch):
    """A captive portal / proxy / CDN error page answers 200 with HTML. Treating
    that as an empty result set is indistinguishable from a genuine miss."""
    _stub_tmdb(monkeypatch, lambda request: httpx.Response(
        200, content=b"<html>504 Gateway Time-out</html>",
        headers={"content-type": "text/html"}))

    # json.JSONDecodeError is a ValueError; what matters is that it does not
    # return an empty candidate list.
    with pytest.raises((MetadataError, ValueError)):
        await TMDBClient("k").search_tv("Hawaii Five-O")


# --------------------------------------------------------------------------
# artwork selection, end to end through the client
# --------------------------------------------------------------------------

async def test_tv_artwork_resolves_the_year_itself_because_the_search_cannot(
        monkeypatch):
    """The past bug: /search/tv takes no year filter, so TMDB returns the popular
    2010 remake first — and "Hawaii Five-0" (zero) IS the remake's exact title,
    while the 1968 original is spelled with a letter O. Ranking an exact title
    above the caller's explicit year handed back the remake's poster for a disc
    the box-set row said was 1968."""
    seen = _stub_tmdb(monkeypatch, _json({"results": [
        _show(4602, "Hawaii Five-0", "2010-09-20", poster_path="/remake2010.jpg",
              backdrop_path="/remake-bd.jpg"),
        _show(1932, "Hawaii Five-O", "1968-09-20", poster_path="/original1968.jpg",
              backdrop_path="/original-bd.jpg"),
    ]}))

    poster, backdrop = await TMDBClient("k").tv_artwork("Hawaii Five-0", 1968)

    assert poster.endswith("/original1968.jpg")
    assert backdrop.endswith("/original-bd.jpg")
    # ...and it cannot have been TMDB doing the filtering for us.
    assert "year" not in seen[0].url.params
    assert "first_air_date_year" not in seen[0].url.params

    # The same query with the remake's year still reaches the remake.
    poster, _ = await TMDBClient("k").tv_artwork("Hawaii Five-0", 2010)
    assert poster.endswith("/remake2010.jpg")


async def test_movie_artwork_prefers_the_asked_for_year_when_tmdb_leads_with_another(
        monkeypatch):
    """TMDB's year filter is advisory — it still returns the popular remake first.
    The wanted year has to survive all the way into the ranking or the 1941 disc
    gets the 2019 poster."""
    seen = _stub_tmdb(monkeypatch, _json({"results": [
        _movie(19, "Dumbo", "2019-03-27", poster_path="/p2019.jpg"),
        _movie(41, "Dumbo", "1941-10-23", poster_path="/p1941.jpg"),
    ]}))

    poster, _ = await TMDBClient("k").movie_artwork("Dumbo", 1941)

    assert poster.endswith("/p1941.jpg")
    assert seen[0].url.params["year"] == "1941"   # asked TMDB too, belt and braces


async def test_artwork_urls_pair_each_image_with_its_own_cdn_size(monkeypatch):
    """poster.jpg is portrait at w780 and fanart.jpg landscape at w1280; crossing
    them puts a stretched backdrop where Plex expects a poster."""
    _stub_tmdb(monkeypatch, _json({"results": [
        _movie(1, "WarGames", "1983-06-03", poster_path="/p.jpg",
               backdrop_path="/b.jpg"),
    ]}))

    poster, backdrop = await TMDBClient("k").movie_artwork("WarGames", 1983)

    assert poster == "https://image.tmdb.org/t/p/w780/p.jpg"
    assert backdrop == "https://image.tmdb.org/t/p/w1280/b.jpg"


async def test_artwork_for_an_unknown_title_is_absent_rather_than_wrong(monkeypatch):
    """No candidates at all must yield no URLs — a placeholder or an exception here
    would either misfile art or abort the placement step."""
    _stub_tmdb(monkeypatch, _json({"results": []}))
    client = TMDBClient("k")

    assert await client.movie_artwork("No Such Film", 1999) == (None, None)
    assert await client.tv_artwork("No Such Show", 1999) == (None, None)


async def test_a_match_with_no_images_yields_no_urls_not_a_broken_path(monkeypatch):
    """TMDB regularly has a match with poster_path null; "…/w780None" would be
    downloaded as a 404 body and written over good disc art."""
    _stub_tmdb(monkeypatch, _json({"results": [
        _movie(1, "Obscure", "1974-01-01", poster_path=None, backdrop_path=None),
    ]}))

    assert await TMDBClient("k").movie_artwork("Obscure", 1974) == (None, None)


# --------------------------------------------------------------------------
# image download
# --------------------------------------------------------------------------

async def test_saving_an_image_needs_no_api_key_and_follows_the_cdn_redirect(
        tmp_path, monkeypatch):
    """image.tmdb.org is a plain CDN that redirects and wants no key: sending the
    search client's auth gate or refusing the 3xx means no artwork at all."""
    def handler(request):
        if request.url.path.endswith("/moved.jpg"):
            return httpx.Response(302, headers={"location": "https://cdn.example/real.jpg"})
        return httpx.Response(200, content=b"\xff\xd8JPEGDATA")

    seen = _stub_tmdb(monkeypatch, handler)
    dest = tmp_path / "Movies" / "WarGames (1983)" / "poster.jpg"

    ok = await TMDBClient(None).save_image("https://image.tmdb.org/t/p/w780/moved.jpg", dest)

    assert ok is True
    assert dest.read_bytes() == b"\xff\xd8JPEGDATA"    # followed the redirect
    assert [r.url.path for r in seen] == ["/t/p/w780/moved.jpg", "/real.jpg"]
    assert "api_key" not in seen[0].url.params


async def test_a_failed_image_download_leaves_no_placeholder_behind(
        tmp_path, monkeypatch):
    """A truncated/zero-byte poster.jpg reads as "art already present" on the next
    pass, so a 404 must leave the folder exactly as it was."""
    _stub_tmdb(monkeypatch, lambda request: httpx.Response(404, content=b"not found"))
    dest = tmp_path / "Movies" / "WarGames (1983)" / "poster.jpg"

    with pytest.raises(httpx.HTTPStatusError):
        await TMDBClient("k").save_image("https://image.tmdb.org/t/p/w780/gone.jpg", dest)

    assert not dest.exists()
    assert not dest.parent.exists()


def test_a_readonly_temp_from_an_interrupted_run_does_not_block_the_next_write(
        tmp_path, monkeypatch):
    """A killed run can leave poster.jpg.tmp behind, and on a Windows/SMB share it
    carries the read-only attribute — which denies writing it AND deleting it
    until the attribute itself is cleared. Retrying without clearing it just
    raises PermissionError again and wedges artwork for that folder permanently.

    The stale temp here really is read-only, and both refusals are keyed off the
    live mode bits, so the retry only gets through if the code actually cleared
    them."""
    dest = tmp_path / "poster.jpg"
    dest.write_bytes(b"old-art")
    stale = tmp_path / "poster.jpg.tmp"
    stale.write_bytes(b"half-written-junk")
    stale.chmod(stat.S_IREAD)          # the read-only attribute, for real

    real_write_bytes = Path.write_bytes
    real_unlink = Path.unlink
    denied: list[str] = []

    def _is_readonly(path):
        return path.exists() and not path.stat().st_mode & stat.S_IWUSR

    def win_write_bytes(self, data):
        if _is_readonly(self):
            denied.append(f"write {self.name}")
            raise PermissionError(13, "Permission denied")
        return real_write_bytes(self, data)

    def win_unlink(self, missing_ok=False):
        # POSIX lets you unlink a read-only file; Windows/SMB does not, and that
        # is the platform this retry exists for.
        if _is_readonly(self):
            denied.append(f"unlink {self.name}")
            raise PermissionError(13, "Permission denied")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "write_bytes", win_write_bytes)
    monkeypatch.setattr(Path, "unlink", win_unlink)

    metadata._replace_bytes(dest, b"new-art")

    # Refused once on the write, then cleared — never refused a second time.
    assert denied == ["write poster.jpg.tmp"]
    assert dest.read_bytes() == b"new-art"
    assert not stale.exists()
