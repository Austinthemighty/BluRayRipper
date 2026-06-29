"""Pure Plex naming: filenames, folder structure, and sidecar subtitle names.

Plex conventions (https://support.plex.tv/articles/naming-and-organizing):
  Movies:      Movies/Title (Year)/Title (Year).ext
  TV episodes: TV Shows/Show (Year)/Season 01/Show - S01E09.ext
  Subtitles:   <video basename>.<lang>[.default].srt   (sidecar, same folder)

Everything here is a pure function so it is trivially unit-tested.
"""

from __future__ import annotations

import re
from pathlib import Path

# ISO 639-2/B (what MakeMKV/mkvmerge report) -> ISO 639-1 (what Plex prefers on
# sidecars). Unmapped codes fall through to the original 3-letter code.
_ISO_639_2B_TO_1 = {
    "eng": "en", "pol": "pl", "fre": "fr", "fra": "fr", "ger": "de", "deu": "de",
    "spa": "es", "ita": "it", "por": "pt", "rus": "ru", "jpn": "ja", "kor": "ko",
    "chi": "zh", "zho": "zh", "dut": "nl", "nld": "nl", "swe": "sv", "nor": "no",
    "dan": "da", "fin": "fi", "cze": "cs", "ces": "cs", "gre": "el", "ell": "el",
    "hun": "hu", "tur": "tr", "ara": "ar", "heb": "he", "hin": "hi", "tha": "th",
    "ukr": "uk", "rum": "ro", "ron": "ro", "slo": "sk", "slk": "sk", "slv": "sl",
    "bul": "bg", "hrv": "hr", "srp": "sr", "cat": "ca", "vie": "vi", "ind": "id",
}

# Characters illegal on Windows (the strictest common filesystem) + control chars.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize(name: str) -> str:
    """Make ``name`` safe as a single path component across Windows/Linux/macOS."""
    cleaned = _ILLEGAL.sub("", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.rstrip(". ")  # Windows forbids trailing dot/space
    if cleaned.upper() in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned or "Untitled"


def subtitle_lang_suffix(iso_639_2b: str) -> str:
    code = iso_639_2b.strip().lower()
    return _ISO_639_2B_TO_1.get(code, code)


def _title_with_year(title: str, year: int | None) -> str:
    base = sanitize(title)
    return f"{base} ({year})" if year else base


def movie_video_path(
    movies_root: Path, title: str, year: int | None, container: str, edition: str | None = None
) -> Path:
    folder = _title_with_year(title, year)
    name = folder
    if edition and edition.strip():
        # Plex edition tag: "Title (Year) {edition-Extended Cut}.ext"
        name = f"{folder} {{edition-{sanitize(edition)}}}"
    return movies_root / folder / f"{name}.{container}"


def episode_video_path(
    tv_root: Path,
    show: str,
    year: int | None,
    season: int,
    episode: int,
    container: str,
) -> Path:
    show_folder = _title_with_year(show, year)
    season_folder = f"Season {season:02d}"
    basename = f"{sanitize(show)} - S{season:02d}E{episode:02d}"
    return tv_root / show_folder / season_folder / f"{basename}.{container}"


def subtitle_sidecar_path(
    video_path: Path,
    iso_639_2b: str,
    *,
    default: bool = False,
    forced: bool = False,
    sdh: bool = False,
    index: int | None = None,
) -> Path:
    """Plex subtitle sidecar name with language + optional modifiers.

    Examples (basename ``Show - S01E09``):
      main, default lang -> ``Show - S01E09.en.default.srt``
      forced subtitles   -> ``Show - S01E09.en.forced.srt``
      SDH / hearing-imp. -> ``Show - S01E09.en.sdh.srt``
      extra plain track  -> ``Show - S01E09.en.2.srt`` (index disambiguator)

    ``forced`` and ``sdh`` are Plex-recognized keywords; ``index`` keeps any
    further same-language tracks from colliding.
    """
    suffix = subtitle_lang_suffix(iso_639_2b)
    parts = [video_path.stem, suffix]
    if forced:
        parts.append("forced")
    if sdh:
        parts.append("sdh")
    if index is not None:
        parts.append(str(index))
    if default:
        parts.append("default")
    return video_path.with_name(".".join(parts) + ".srt")
