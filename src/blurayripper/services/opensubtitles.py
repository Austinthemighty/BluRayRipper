"""OpenSubtitles.com REST client — fetch reference subtitles for an episode.

Used only to supply *reference* text for subtitle-based episode identification
(see ``sub_match``): given the show's TMDB id and a season/episode, download a
well-rated SRT to compare a rip's OCR'd subtitles against. The candidate set per
disc is tiny (just the episodes on it), so this stays well within free-tier
limits. A free API key is required (config/secrets.env, ``OPENSUBTITLES_API_KEY``);
optional username/password raise the daily download quota.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx

from .naming import subtitle_lang_suffix

_BASE = "https://api.opensubtitles.com/api/v1"
_UA = "BluRayRipper/1.0"


class OpenSubtitlesError(RuntimeError):
    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        # A "fatal" error (bad key / exhausted quota) fails EVERY episode the same
        # way, so there's no point trying the rest — surface it and stop.
        self.fatal = fatal


class QuotaExhausted(OpenSubtitlesError):
    """The download quota ran out (or the request rate did).

    Nothing cures that but waiting, so it says how long: ``reset_at`` is when
    OpenSubtitles said the quota resets (aware UTC), or None when the reply
    didn't say. ``status`` is the HTTP status; a 429 with no reset time is the
    per-second rate limit, over in moments rather than hours.

    Raised from gather_references it also carries the references fetched or
    read from the cache (``partial``) and the episodes it could not get
    (``untried``)."""

    def __init__(self, message: str, *, reset_at: datetime | None = None,
                 status: int | None = None) -> None:
        super().__init__(message, fatal=True)
        self.reset_at = reset_at
        self.status = status
        self.partial: dict[int, str] = {}
        self.untried: list[int] = []


def quota_reset_at(snapshot: dict | None) -> datetime | None:
    """When the quota resets, from a /download reply or a quota snapshot:
    ``reset_time_utc`` ("2026-08-22T04:10:00Z" or "…:00.000Z"), as aware UTC."""
    value = (snapshot or {}).get("reset_time_utc")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        when = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


async def gather_references(
    client, show_tmdb_id: int, season: int, episodes: list[int], language: str = "en",
    *, stop_on_quota: bool = False,
) -> dict[int, str]:
    """Fetch reference SRT text for each candidate episode.

    A per-episode miss (no subs for that episode) is omitted so matching degrades
    gracefully. But if NOTHING could be fetched, raise the real reason (quota /
    bad key / no results) instead of returning an empty dict that hides why.

    When the quota runs out the rest are still read from the cache, which costs
    nothing; stopping outright there threw away references downloaded on an
    earlier day. With ``stop_on_quota`` a quota cut that left episodes untried
    raises QuotaExhausted carrying what was got, so the caller can tell "not on
    OpenSubtitles" from "not fetched yet" and wait for the reset."""
    refs: dict[int, str] = {}
    last_error: Exception | None = None
    quota: QuotaExhausted | None = None
    untried: list[int] = []
    for ep in episodes:
        if quota is not None:
            cached = getattr(client, "cached_episode_srt", None)
            text = cached(show_tmdb_id, season, ep, language) if cached else None
            if text:
                refs[ep] = text
            else:
                untried.append(ep)
            continue
        try:
            text = await client.fetch_episode_srt(show_tmdb_id, season, ep, language)
        except QuotaExhausted as exc:
            last_error = quota = exc
            untried.append(ep)
            continue
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
    if quota is not None and (stop_on_quota or not refs):
        quota.partial, quota.untried = refs, untried
        raise quota
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
        self._last_quota: dict | None = None  # last quota snapshot from /download
        # Reference SRTs are cached to disk so re-running identification (or the
        # same episode appearing on another disc) never re-spends the daily
        # /download quota. Only the killer call — /download — is skipped on a hit.
        self._cache_dir = cache_dir

    def _cache_path(self, tmdb_id: int, season: int, episode: int, language: str) -> Path | None:
        if not self._cache_dir:
            return None
        return self._cache_dir / f"{tmdb_id}_s{season:02d}e{episode:02d}_{language}.srt"

    def cached_episode_srt(self, parent_tmdb_id: int, season: int, episode: int,
                           language: str = "en") -> str | None:
        """The reference already downloaded for this episode, or None. Never
        touches the network, so it works with the quota spent."""
        path = self._cache_path(parent_tmdb_id, season, episode,
                                subtitle_lang_suffix(language))
        if path and path.exists():
            return path.read_text(encoding="utf-8", errors="replace")
        return None

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
            # fatal: a missing key fails EVERY episode identically, which is this
            # flag's definition. Without it gather_references skipped its early
            # break and re-raised once per candidate episode, then reported the
            # whole run as a non-fatal (i.e. retryable) failure. user_info()
            # already marks the same condition fatal.
            raise OpenSubtitlesError(
                "OpenSubtitles API key not configured. Set OPENSUBTITLES_API_KEY in "
                "config/secrets.env.",
                fatal=True,
            )
        # OpenSubtitles uses ISO 639-1 (en, pl) for both the search filter and the
        # response's language field; our config uses 639-2/B (eng, pol). Passing
        # "eng" returns nothing / filters everything out. Normalise to "en".
        language = subtitle_lang_suffix(language)
        cached = self.cached_episode_srt(parent_tmdb_id, season, episode, language)
        if cached is not None:
            return cached
        cache_path = self._cache_path(parent_tmdb_id, season, episode, language)
        try:
            # follow_redirects: OpenSubtitles' download link (and some endpoints)
            # 301/302-redirect; without this httpx raises instead of following.
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
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
                self._remember_quota(body)  # /download reports remaining + reset
                link = body.get("link")
                if not link:
                    # Over-quota can come back 200 with a message and no link.
                    msg = (body.get("message") or "").lower()
                    if "download" in msg or "quota" in msg:
                        self._remember_quota(body, exhausted=True)
                        raise QuotaExhausted(
                            f"OpenSubtitles download quota reached: {body.get('message')}",
                            reset_at=quota_reset_at(self._last_quota), status=200)
                    return None
                srt = await client.get(link, headers={"User-Agent": _UA})
                srt.raise_for_status()
                if cache_path:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(srt.text, encoding="utf-8")
                return srt.text
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            reset_at, earlier = None, quota_reset_at(self._last_quota)
            if code in (406, 429):  # quota — capture the reset time
                try:
                    body = exc.response.json()
                    self._remember_quota(body, exhausted=True)
                    reset_at = quota_reset_at(body)
                except Exception:
                    self._last_quota = {"remaining": 0}
                # A 406 is the daily quota, so the reset an earlier download
                # reported still holds. A bare 429 may be the rate limit, which
                # resets in moments; borrowing the daily reset would wait hours.
                if reset_at is None and code == 406:
                    reset_at = quota_reset_at(self._last_quota) or earlier
            err = self._classify_http_error(exc)
            if isinstance(err, QuotaExhausted):
                err.reset_at = reset_at
            raise err from exc

    def _remember_quota(self, body: dict, *, exhausted: bool = False) -> None:
        """Record the download-quota fields OpenSubtitles returns on /download, so
        the UI can show them without needing /login (which some API keys can't)."""
        # MERGE onto the last snapshot instead of rebuilding it. Not every reply
        # carries every field, and rebuilding meant one link-less or partial
        # /download reply wiped a good snapshot to all-None — the quota panel then
        # showed "remaining: null" from source 'last-download' instead of the last
        # real figure. A field is only overwritten when the reply actually states
        # it (0 is a statement; absent is not).
        snap = dict(self._last_quota or {
            "remaining": None, "requests": None,
            "reset_time": None, "reset_time_utc": None,
        })
        for key in ("remaining", "requests", "reset_time", "reset_time_utc"):
            value = body.get(key)
            if value is not None:
                snap[key] = value
        if exhausted:
            snap["remaining"] = 0
        self._last_quota = snap

    def last_known_quota(self) -> dict | None:
        """The most recent quota snapshot captured from a /download response, or
        None if no download has happened this run."""
        return self._last_quota

    async def user_info(self) -> dict:
        """Fetch the account's download-quota info from /infos/user.

        Reads the daily quota: allowed_downloads, downloads_count,
        remaining_downloads, level, vip. Needs a username+password (a bearer
        token) — the Api-Key alone can't read /infos/user."""
        if not self._api_key:
            raise OpenSubtitlesError("OpenSubtitles API key not configured.", fatal=True)
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
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
            return QuotaExhausted(
                f"OpenSubtitles daily download quota exhausted (HTTP {code}) — it "
                "resets each day. The titles are ripped; retry later or map episodes "
                "manually.", status=code)
        return OpenSubtitlesError(f"OpenSubtitles request failed (HTTP {code}).")
