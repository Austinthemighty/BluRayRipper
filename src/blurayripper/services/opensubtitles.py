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
    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        # A "fatal" error (bad key / exhausted quota) fails EVERY episode the same
        # way, so there's no point trying the rest — surface it and stop.
        self.fatal = fatal


async def gather_references(
    client, show_tmdb_id: int, season: int, episodes: list[int], language: str = "en"
) -> dict[int, str]:
    """Fetch reference SRT text for each candidate episode.

    A per-episode miss (no subs for that episode) is omitted so matching degrades
    gracefully. But if NOTHING could be fetched, raise the real reason (quota /
    bad key / no results) instead of returning an empty dict that hides why."""
    refs: dict[int, str] = {}
    last_error: Exception | None = None
    for ep in episodes:
        try:
            text = await client.fetch_episode_srt(show_tmdb_id, season, ep, language)
        except OpenSubtitlesError as exc:
            last_error = exc
            text = None
            if exc.fatal:  # same failure for every episode — don't hammer the API
                break
        except Exception as exc:
            last_error = exc
            text = None
        if text:
            refs[ep] = text
    if not refs and last_error is not None:
        raise OpenSubtitlesError(str(last_error), fatal=getattr(last_error, "fatal", False))
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

    async def _login(self, client: httpx.AsyncClient, *, required: bool = False) -> None:
        """Exchange username+password for a bearer token (raises the API quota and
        enables /infos/user). Best-effort by default — a failed login still leaves
        the Api-Key working for search/download. Pass required=True (e.g. for
        /infos/user) to surface WHY it failed instead of silently continuing."""
        if self._token:
            return
        if not (self._username and self._password):
            if required:
                raise OpenSubtitlesError(
                    "OPENSUBTITLES_USERNAME / OPENSUBTITLES_PASSWORD are not set in "
                    "the running server. Add both to config/secrets.env and RESTART "
                    "(a value added while the server is running isn't picked up).",
                    fatal=True)
            return
        resp = await client.post(
            f"{_BASE}/login",
            headers=self._headers(),
            json={"username": self._username, "password": self._password},
        )
        if resp.status_code == 200:
            self._token = resp.json().get("token")
            return
        if required:
            detail = ""
            try:
                detail = resp.json().get("message") or ""
            except Exception:
                detail = (resp.text or "")[:200]
            raise OpenSubtitlesError(
                f"OpenSubtitles login failed (HTTP {resp.status_code})"
                + (f": {detail}" if detail else "")
                + " — check OPENSUBTITLES_USERNAME/PASSWORD and the API key.",
                fatal=True)

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
        try:
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
                    return None  # no subs for this episode — not an error
                dl = await client.post(
                    f"{_BASE}/download", headers=self._headers(), json={"file_id": file_id}
                )
                dl.raise_for_status()
                body = dl.json()
                link = body.get("link")
                if not link:
                    # Over-quota can come back 200 with a message and no link.
                    msg = (body.get("message") or "").lower()
                    if "download" in msg or "quota" in msg:
                        raise OpenSubtitlesError(
                            f"OpenSubtitles download quota reached: {body.get('message')}",
                            fatal=True)
                    return None
                srt = await client.get(link, headers={"User-Agent": _UA})
                srt.raise_for_status()
                if cache_path:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(srt.text, encoding="utf-8")
                return srt.text
        except httpx.HTTPStatusError as exc:
            raise self._classify_http_error(exc) from exc

    async def user_info(self) -> dict:
        """Fetch the account's download-quota info from /infos/user.

        Reads the daily quota: allowed_downloads, downloads_count,
        remaining_downloads, level, vip. Needs a username+password (a bearer
        token) — the Api-Key alone can't read /infos/user."""
        if not self._api_key:
            raise OpenSubtitlesError("OpenSubtitles API key not configured.", fatal=True)
        async with httpx.AsyncClient(timeout=20) as client:
            await self._login(client, required=True)  # raises a specific reason on failure
            try:
                resp = await client.get(f"{_BASE}/infos/user", headers=self._headers())
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise self._classify_http_error(exc) from exc
            return resp.json().get("data", {}) or {}

    @staticmethod
    def _classify_http_error(exc: "httpx.HTTPStatusError") -> "OpenSubtitlesError":
        code = exc.response.status_code
        if code in (401, 403):
            return OpenSubtitlesError(
                f"OpenSubtitles rejected the request (HTTP {code}) — check "
                "OPENSUBTITLES_API_KEY (and username/password) in secrets.env.",
                fatal=True)
        if code in (406, 429):
            return OpenSubtitlesError(
                f"OpenSubtitles daily download quota exhausted (HTTP {code}) — it "
                "resets each day. The titles are ripped; retry later or map episodes "
                "manually.", fatal=True)
        return OpenSubtitlesError(f"OpenSubtitles request failed (HTTP {code}).")
