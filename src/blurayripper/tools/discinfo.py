"""Disc-structure analysis: fingerprinting + movie/TV heuristics.

Disc menus do not store episode numbers, so we cannot read them. What we *can*
derive reliably from the scan is each title's duration and play order, plus a
stable fingerprint of the disc. The fingerprint lets us recall a previously
confirmed episode mapping (see services.mapping_store) and skip the manual
confirm step on a re-rip of the same disc.
"""

from __future__ import annotations

import hashlib
import re

from ..models import DiscInfo, MediaKind, Title

# Episode-length window (seconds): titles in this range on a multi-title disc
# are likely TV episodes. ~18 min to 75 min covers most broadcast formats.
_EPISODE_MIN = 18 * 60
_EPISODE_MAX = 75 * 60
# A single title longer than this is almost certainly a movie main feature.
_MOVIE_MIN = 70 * 60


def disc_fingerprint(disc: DiscInfo) -> str:
    """Stable hash of the disc identity: label + ordered title durations.

    MakeMKV reports identical durations on every scan of the same disc, so the
    exact second values are deterministic and safe to hash directly.
    """
    label = (disc.volume_label or disc.disc_name or "").strip().lower()
    durations = [str(t.duration_seconds) for t in ordered_titles(disc)]
    payload = label + "|" + ",".join(durations)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _playlist_sort_key(title: Title) -> tuple:
    """Order titles by their source playlist number when available, else title id."""
    if title.source_filename:
        match = re.search(r"(\d+)", title.source_filename)
        if match:
            return (0, int(match.group(1)), title.title_id)
    return (1, title.title_id, 0)


def ordered_titles(disc: DiscInfo) -> list[Title]:
    """Titles in best-guess play order."""
    return sorted(disc.titles, key=_playlist_sort_key)


def guess_kind(disc: DiscInfo) -> MediaKind:
    """Heuristic: one dominant long title => movie; a cluster of episode-length
    titles => TV. Falls back to TV when ambiguous (the confirm step fixes it)."""
    eps = episode_candidate_titles(disc)
    longest = max((t.duration_seconds for t in disc.titles), default=0)

    if len(eps) >= 3:
        return MediaKind.TV
    # One clearly-dominant feature with few/no episode-length siblings -> movie.
    if longest >= _MOVIE_MIN and len(eps) <= 1:
        return MediaKind.MOVIE
    return MediaKind.TV if len(eps) >= 2 else MediaKind.MOVIE


def episode_candidate_titles(disc: DiscInfo) -> list[Title]:
    """Titles whose duration falls in the episode-length window, in play order."""
    return [
        t
        for t in ordered_titles(disc)
        if _EPISODE_MIN <= t.duration_seconds <= _EPISODE_MAX
    ]


def main_feature(disc: DiscInfo) -> Title | None:
    """The longest title; the movie main feature for a movie disc."""
    if not disc.titles:
        return None
    return max(disc.titles, key=lambda t: t.duration_seconds)
