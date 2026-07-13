"""TMDB metadata client (v3 REST) for movie/TV search and episode listing.

Used to feed the naming confirm step. A free API key (config/secrets.env,
``TMDB_API_KEY``) is required; without it the pipeline still works but naming
must be entered fully by hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

_BASE = "https://api.themoviedb.org/3"
# TMDB image CDN + the sizes we pull (portrait poster, landscape backdrop).
_POSTER_URL = "https://image.tmdb.org/t/p/w780"
_BACKDROP_URL = "https://image.tmdb.org/t/p/w1280"


def _img(base: str, path: str | None) -> str | None:
    return f"{base}{path}" if path else None


@dataclass
class MovieCandidate:
    tmdb_id: int
    title: str
    year: int | None
    overview: str
    poster_path: str | None = None
    backdrop_path: str | None = None


@dataclass
class ShowCandidate:
    tmdb_id: int
    name: str
    year: int | None
    overview: str
    poster_path: str | None = None
    backdrop_path: str | None = None


@dataclass
class Episode:
    season_number: int
    episode_number: int
    name: str
    runtime: int | None  # minutes, often absent on TMDB
    overview: str


def _year_from(date: str | None) -> int | None:
    if date and len(date) >= 4 and date[:4].isdigit():
        return int(date[:4])
    return None


def _pick_art(candidates, title, year, name_attr):
    """Choose the candidate whose title (+year) best matches, preferring an exact
    title+year hit, then falling back to TMDB's own relevance order (first result)."""
    if not candidates:
        return (None, None)
    want = (title or "").strip().lower()

    def score(c):
        exact_title = getattr(c, name_attr, "").strip().lower() == want
        exact_year = year is not None and c.year == year
        return (exact_title and exact_year, exact_title, exact_year)

    best = max(candidates, key=score)
    return (_img(_POSTER_URL, best.poster_path), _img(_BACKDROP_URL, best.backdrop_path))


class MetadataError(RuntimeError):
    pass


class TMDBClient:
    def __init__(self, api_key: str | None, language: str = "en-US") -> None:
        self._api_key = api_key
        self._language = language

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    async def _get(self, path: str, params: dict) -> dict:
        if not self._api_key:
            raise MetadataError(
                "TMDB API key not configured. Set TMDB_API_KEY in config/secrets.env."
            )
        query = {"api_key": self._api_key, "language": self._language, **params}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{_BASE}{path}", params=query)
            resp.raise_for_status()
            return resp.json()

    async def search_movie(self, query: str, year: int | None = None) -> list[MovieCandidate]:
        params: dict = {"query": query}
        if year:
            params["year"] = year
        data = await self._get("/search/movie", params)
        return [
            MovieCandidate(
                tmdb_id=r["id"],
                title=r.get("title") or r.get("original_title") or "",
                year=_year_from(r.get("release_date")),
                overview=r.get("overview", ""),
                poster_path=r.get("poster_path"),
                backdrop_path=r.get("backdrop_path"),
            )
            for r in data.get("results", [])
        ]

    async def search_tv(self, query: str) -> list[ShowCandidate]:
        data = await self._get("/search/tv", {"query": query})
        return [
            ShowCandidate(
                tmdb_id=r["id"],
                name=r.get("name") or r.get("original_name") or "",
                year=_year_from(r.get("first_air_date")),
                overview=r.get("overview", ""),
                poster_path=r.get("poster_path"),
                backdrop_path=r.get("backdrop_path"),
            )
            for r in data.get("results", [])
        ]

    async def movie_artwork(self, title: str, year: int | None) -> tuple[str | None, str | None]:
        """(poster_url, backdrop_url) for the best movie match, or (None, None)."""
        return _pick_art(await self.search_movie(title, year), title, year, "title")

    async def tv_artwork(self, title: str, year: int | None) -> tuple[str | None, str | None]:
        """(poster_url, backdrop_url) for the best show match, or (None, None)."""
        return _pick_art(await self.search_tv(title), title, year, "name")

    async def save_image(self, url: str, dest: Path) -> bool:
        """Download an image URL to `dest` (creating parent dirs). No API key
        needed for the image CDN. Returns True on success."""
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            dest = Path(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(resp.content)
        return True

    async def get_season(self, tv_id: int, season_number: int) -> list[Episode]:
        data = await self._get(f"/tv/{tv_id}/season/{season_number}", {})
        return [
            Episode(
                season_number=e.get("season_number", season_number),
                episode_number=e.get("episode_number", 0),
                name=e.get("name", ""),
                runtime=e.get("runtime"),
                overview=e.get("overview", ""),
            )
            for e in data.get("episodes", [])
        ]
