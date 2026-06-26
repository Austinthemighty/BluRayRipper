"""TMDB metadata client (v3 REST) for movie/TV search and episode listing.

Used to feed the naming confirm step. A free API key (config/secrets.env,
``TMDB_API_KEY``) is required; without it the pipeline still works but naming
must be entered fully by hand.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

_BASE = "https://api.themoviedb.org/3"


@dataclass
class MovieCandidate:
    tmdb_id: int
    title: str
    year: int | None
    overview: str


@dataclass
class ShowCandidate:
    tmdb_id: int
    name: str
    year: int | None
    overview: str


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
            )
            for r in data.get("results", [])
        ]

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
