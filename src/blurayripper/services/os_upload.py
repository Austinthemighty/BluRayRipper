"""Export OCR'd subtitles for MANUAL upload to OpenSubtitles.

The app can't upload via the API (that needs the legacy XML-RPC endpoint plus an
OpenSubtitles-approved User-Agent). Instead, when ``subtitles.upload_export_dir``
is set, each placed SRT is copied into that folder next to a plain-text sheet
that pre-fills every field of the OpenSubtitles manual-upload form
(opensubtitles.org/upload). The fields we can determine exactly — language,
hearing-impaired (SDH), foreign-parts-only (forced), FPS, HD flag — are filled
in; IMDB ID is left to the form's built-in title search.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..models import EpisodeAssignment, MediaKind
from .naming import episode_code

# ISO 639-2/B -> English name for the languages we're likely to keep. Falls back
# to the raw code so an unlisted language still yields a usable sheet.
_LANG_NAMES = {
    "eng": "English", "pol": "Polish", "spa": "Spanish", "fre": "French",
    "fra": "French", "ger": "German", "deu": "German", "ita": "Italian",
    "por": "Portuguese", "rus": "Russian", "jpn": "Japanese", "kor": "Korean",
    "chi": "Chinese", "zho": "Chinese", "dut": "Dutch", "nld": "Dutch",
    "swe": "Swedish", "nor": "Norwegian", "dan": "Danish", "fin": "Finnish",
    "cze": "Czech", "ces": "Czech", "hun": "Hungarian", "gre": "Greek",
    "ell": "Greek", "tur": "Turkish", "ara": "Arabic", "heb": "Hebrew",
    "hin": "Hindi", "tha": "Thai", "vie": "Vietnamese", "ukr": "Ukrainian",
    "ron": "Romanian", "rum": "Romanian", "bul": "Bulgarian", "hrv": "Croatian",
    "srp": "Serbian", "slo": "Slovak", "slk": "Slovak", "slv": "Slovenian",
    "est": "Estonian", "lav": "Latvian", "lit": "Lithuanian",
    "ind": "Indonesian", "may": "Malay", "msa": "Malay", "fil": "Filipino",
}


def language_name(code: str | None) -> str:
    if not code:
        return "Undetermined"
    return _LANG_NAMES.get(code.lower(), code)


def _yn(value: bool) -> str:
    return "YES" if value else "no"


def _safe(name: str) -> str:
    keep = " ._-()"
    cleaned = "".join(c if (c.isalnum() or c in keep) else "_" for c in name).strip()
    # '.' is a kept character, so a title of ".." survived intact and
    # export_dir/".." is the export folder's PARENT — mkdir(exist_ok=True) then
    # succeeds and the .srt plus its sheet are written OUTSIDE the export dir.
    # Trailing dots/spaces are also illegal on Windows, so strip both (this is
    # what naming.sanitize already does, and why it answers "Untitled" for "..").
    cleaned = cleaned.rstrip(". ")
    return cleaned or "subtitle"


def movie_folder(assignment: EpisodeAssignment) -> str:
    title = assignment.show_or_movie_title
    if assignment.kind is MediaKind.TV and assignment.season is not None:
        return f"{title} - Season {assignment.season}"
    if assignment.year:
        return f"{title} ({assignment.year})"
    return title


def suggested_release_name(assignment: EpisodeAssignment, height: int | None) -> str:
    parts = [assignment.show_or_movie_title.replace(" ", ".")]
    if (assignment.kind is MediaKind.TV and assignment.season is not None
            and assignment.episode is not None):
        parts.append(episode_code(assignment.season, assignment.episode,
                                  assignment.episode_end))
    elif assignment.year:
        parts.append(str(assignment.year))
    if height:
        parts.append(f"{height}p")
    parts.append("BluRay")
    return ".".join(parts)


def build_info_sheet(
    *, srt_name: str, assignment: EpisodeAssignment, language_code: str | None,
    is_sdh: bool, forced: bool, height: int | None, fps: float | None, version: str,
) -> str:
    """Render the human-readable upload sheet mirroring the OpenSubtitles form."""
    title = assignment.show_or_movie_title
    if assignment.year:
        title += f" ({assignment.year})"
    fps_str = f"{fps:.3f}" if fps else "(check the video file)"
    is_hd = bool(height) and height >= 720

    lines = [
        f"OpenSubtitles manual upload — {srt_name}",
        "Upload at: https://www.opensubtitles.org/upload",
        "",
        "── Movie Information ──────────────────────────────",
        f"Movie name:            {title}",
    ]
    if assignment.kind is MediaKind.TV:
        se = ""
        if assignment.season is not None and assignment.episode is not None:
            se = episode_code(assignment.season, assignment.episode,
                              assignment.episode_end)
        et = f" - {assignment.episode_title}" if assignment.episode_title else ""
        lines.append(f"Episode:               {se}{et}")
    lines += [
        f"Language:              {language_name(language_code)} ({language_code or 'und'})",
        f'IMDB ID:               (use the form\'s title search: type "{assignment.show_or_movie_title}")',
        f"FPS:                   {fps_str}",
        f"Release name:          {suggested_release_name(assignment, height)}",
        "Movie AKA:             ",
        f"Comment:               OCR'd from disc PGS subtitles by BluRayRipper {version}",
        "Translator:            ",
        "",
        "── Checkboxes ─────────────────────────────────────",
        f"Subtitles for hearing impaired:       {_yn(is_sdh)}",
        f"Subtitles for high-definition movie:  {_yn(is_hd)}"
        + (f"   (video is {height}p)" if height else ""),
        "Subtitles are machine translated:     no",
        f"Foreign Parts Only:                   {_yn(forced)}",
        "",
        f"Subtitle file to upload:  {srt_name}  (in this folder)",
    ]
    return "\n".join(lines) + "\n"


def export_subtitle(
    export_dir: Path, srt_path: Path, *, assignment: EpisodeAssignment,
    language_code: str | None, is_sdh: bool, forced: bool,
    height: int | None, fps: float | None, version: str,
) -> Path:
    """Copy ``srt_path`` into a per-title subfolder of ``export_dir`` and write a
    ``<name>.info.txt`` upload sheet beside it. Returns the copied SRT path."""
    folder = Path(export_dir) / _safe(movie_folder(assignment))
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / srt_path.name
    shutil.copy2(srt_path, dest)
    sheet = build_info_sheet(
        srt_name=srt_path.name, assignment=assignment, language_code=language_code,
        is_sdh=is_sdh, forced=forced, height=height, fps=fps, version=version)
    (folder / (srt_path.name + ".info.txt")).write_text(sheet, encoding="utf-8")
    return dest
