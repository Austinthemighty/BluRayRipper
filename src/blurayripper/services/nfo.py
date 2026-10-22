"""Write a Kodi/Plex-style .nfo sidecar next to each output video.

Plex (with the XBMCnfo / Local Media Assets agent) and Kodi read a
``<video>.nfo`` XML file for local metadata — <movie> for films,
<episodedetails> for TV. Purely additive; ignored by Plex's own agents.
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

from ..models import EpisodeAssignment, MediaKind


def build_nfo(a: EpisodeAssignment) -> str:
    if a.kind is MediaKind.MOVIE:
        rows = [f"  <title>{escape(a.show_or_movie_title)}</title>"]
        if a.year:
            rows.append(f"  <year>{a.year}</year>")
        if a.edition:
            rows.append(f"  <edition>{escape(a.edition)}</edition>")
        body = "<movie>\n" + "\n".join(rows) + "\n</movie>\n"
    else:
        # One <episodedetails> per episode the file holds: that is how Kodi
        # describes a multi-episode file (S08E11-E12).
        blocks = []
        for n in a.episodes or [None]:
            title = a.episode_title or (f"Episode {n}" if n else "Episode")
            rows = [f"  <title>{escape(title)}</title>",
                    f"  <showtitle>{escape(a.show_or_movie_title)}</showtitle>"]
            if a.season is not None:
                rows.append(f"  <season>{a.season}</season>")
            if n is not None:
                rows.append(f"  <episode>{n}</episode>")
            if a.year:
                rows.append(f"  <year>{a.year}</year>")
            blocks.append("<episodedetails>\n" + "\n".join(rows) + "\n</episodedetails>\n")
        body = "".join(blocks)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body


def write_nfo(video_path: Path, a: EpisodeAssignment) -> Path:
    """Write ``<video stem>.nfo`` next to the video. Returns the .nfo path."""
    nfo = video_path.with_suffix(".nfo")
    nfo.write_text(build_nfo(a), encoding="utf-8")
    return nfo
