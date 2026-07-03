"""OpenSubtitles.com REST client — fetch reference subtitles for an episode.

Used only to supply *reference* text for subtitle-based episode identification
(see ``sub_match``): given the show's TMDB id and a season/episode, download a
well-rated SRT to compare a rip's OCR'd subtitles against. The candidate set per
disc is tiny (just the episodes on it), so this stays well within free-tier
limits. A free API key is required (config/secrets.env, ``OPENSUBTITLES_API_KEY``);
optional username/password raise the daily download quota.
"""

from __future__ import annotations

from pathlib import Path

import httpx

_BASE = "https://api.opensubtitles.com/api/v1"
_UA = "BluRayRipper/1.0"


class OpenSubtitlesError(RuntimeError):
    pass


async def gather_references(
    client, show_tmdb_id: int, season: int, episodes: list[int], language: str = "en"
) -> dict[int, str]:
    """Fetch reference SRT text for each candidate episode. Missing/failed
    episodes are simply omitted (matching degrades gracefully without them)."""
    refs: dict[int, str] = {}
    for ep in episodes:
        try:
            text = await client.fetch_episode_srt(show_tmdb_id, season, ep, language)
        except Exception:
            text = None
        if text:
            refs[ep] = text
    return refs


def best_file_id(search_data: list[dict], *, language: str | None = None) -> int | None:
    """Pick the best subtitle file from a /subtitles response's ``data`` list.

    Prefers trusted uploads, then download count. Returns a ``file_id`` (the id
    used by /download), or None when there are no usable files.
    """
    best: tuple | None = None
    best_id: int | None = None
    for item in search_data:
        attrs = item.get("attributes", {}) or {}
        if language and (attrs.get("language") or "").lower() != language.lower():
            continue
        files = attrs.get("files") or []
        if not files:
            continue
        rank = (
            1 if attrs.get("from_trusted") else 0,
            int(attrs.get("download_count") or 0),
            float(attrs.get("ratings") or 0.0),
        )
        if best is None or rank > best:
            best = rank
            best_id = files[0].get("file_id")
    return best_id


class OpenSubtitlesClient:
    def __init__(
        self,
        api_key: str | None,
        username: str | None = None,
        password: str | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self._api_key = api_key
        self._username = username
        self._password = password
        self._token: str | None = None
        # Reference SRTs are cached to disk so re-running identification (or the
        # same episode appearing on another disc) never re-spends the daily
        # /download quota. Only the killer call — /download — is skipped on a hit.
        self._cache_dir = cache_dir

    def _cache_path(self, tmdb_id: int, season: int, episode: int, language: str) -> Path | None:
        if not self._cache_dir:
            return None
        return self._cache_dir / f"{tmdb_id}_s{season:02d}e{episode:02d}_{language}.srt"

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def _headers(self) -> dict:
        h = {
            "Api-Key": self._api_key or "",
            "User-Agent": _UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def _login(self, client: httpx.AsyncClient) -> None:
        if self._token or not (self._username and self._password):
            return
        resp = await client.post(
            f"{_BASE}/login",
            headers=self._headers(),
            json={"username": self._username, "password": self._password},
        )
        if resp.status_code == 200:
            self._token = resp.json().get("token")

    async def fetch_episode_srt(
        self,
        parent_tmdb_id: int,
        season: int,
        episode: int,
        language: str = "en",
    ) -> str | None:
        """Download the best reference SRT for one episode, or None if not found.

        A cached SRT is returned without touching the network, so it costs no
        download quota."""
        if not self._api_key:
            raise OpenSubtitlesError(
                "OpenSubtitles API key not configured. Set OPENSUBTITLES_API_KEY in "
                "config/secrets.env."
            )
        cache_path = self._cache_path(parent_tmdb_id, season, episode, language)
        if cache_path and cache_path.exists():
            return cache_path.read_text(encoding="utf-8", errors="replace")
        async with httpx.AsyncClient(timeout=30) as client:
            await self._login(client)
            search = await client.get(
                f"{_BASE}/subtitles",
                headers=self._headers(),
                params={
                    "parent_tmdb_id": parent_tmdb_id,
                    "season_number": season,
                    "episode_number": episode,
                    "languages": language,
                    "order_by": "download_count",
                },
            )
            search.raise_for_status()
            file_id = best_file_id(search.json().get("data", []), language=language)
            if file_id is None:
                return None
            dl = await client.post(
                f"{_BASE}/download", headers=self._headers(), json={"file_id": file_id}
            )
            dl.raise_for_status()
            link = dl.json().get("link")
            if not link:
                return None
            srt = await client.get(link, headers={"User-Agent": _UA})
            srt.raise_for_status()
            if cache_path:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(srt.text, encoding="utf-8")
            return srt.text
