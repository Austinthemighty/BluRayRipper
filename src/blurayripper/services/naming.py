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


def _normalize_separators(query: str) -> str:
    """Turn a disc volume label's separators into spaces.

    ISO9660 labels can't contain spaces, so DVDs ship names like
    ``HAWAII_FIVE_0_S1_D4``. Underscores are word characters, so every ``\\b``
    below fails on them — the season/disc go undetected and the raw label is
    handed to TMDB. Underscores are never part of a real title, so they always
    become spaces. Dots are only a separator when the label has no spaces at all
    (``Some.Show.S01.D02``); otherwise they're real punctuation (*Mrs. Doubtfire*).
    """
    q = (query or "").strip().replace("_", " ")
    if "." in q and " " not in q:
        q = q.replace(".", " ")
    return re.sub(r"\s{2,}", " ", q).strip()


# Disc/side descriptor: "Disc 2", "D2", "Disc2". The bare "d" form only counts
# when it's a standalone token, so a title word can't be read as a disc number.
_DISC_RE = re.compile(r"\b(?:disc|disk)\s*0*(\d{1,2})\b|\bd\s*0*(\d{1,2})\b", re.IGNORECASE)


def disc_number(query: str) -> int | None:
    """The disc number in a set from a disc label, e.g. ``HAWAII_FIVE_0_S1_D4``
    -> 4. None when the label doesn't say. Used to suggest which block of
    episodes a disc holds."""
    m = _DISC_RE.search(_normalize_separators(query))
    if not m:
        return None
    n = int(m.group(1) or m.group(2))
    return n or None  # "Disc 0" isn't a real disc number


def episode_span(disc: int | None, count: int) -> tuple[int, int] | None:
    """Guess the (first, last) episode numbers a disc holds, from its position in
    the set and how many episode-length titles are on it — disc 4 of 4-per-disc
    means E13-E16. A GUESS only (disc 1 often carries extras, the last disc is
    usually short), so callers must present it for confirmation, never auto-apply.
    """
    if not disc or disc < 1 or count < 1:
        return None
    first = (disc - 1) * count + 1
    return first, first + count - 1


# Straight and curly double quotes — phones autocorrect " into “ ”, and a search
# typed on one shouldn't silently behave differently. Single quotes are NOT a
# delimiter: apostrophes start real titles (*'Salem's Lot*).
_QUOTE_PAIRS = (('"', '"'), ('“', '”'), ('“', '“'), ('”', '”'))


def _unquote(query: str) -> str | None:
    """The inner text of a fully quoted query, or None if it isn't one.

    Only a query wrapped end-to-end counts, so a title containing a quoted phrase
    is still cleaned normally. Empty quotes ("") aren't a search — treated as not
    quoted, so the caller falls back to the usual handling instead of asking TMDB
    for nothing."""
    q = (query or "").strip()
    if len(q) < 2:
        return None
    for open_q, close_q in _QUOTE_PAIRS:
        if q.startswith(open_q) and q.endswith(close_q):
            inner = q[len(open_q):-len(close_q)].strip()
            return inner or None
    return None


def clean_tmdb_query(query: str, kind: str = "movie") -> tuple[str, int | None, int | None]:
    """Turn a disc name into a searchable (title, year, season) for TMDB.

    Disc names carry authoring noise TMDB can't match, e.g.
    ``"Silicon Valley Season 2 Disc 1"`` -> title ``"Silicon Valley"``, season 2.
    Peels a trailing ``(YYYY)`` as the year, the season number if present, then
    strips trailing "Season N" / "Disc N" descriptors.

    "Volume/Vol" is only stripped for TV — real movie titles use it (*Kill Bill:
    Vol. 1*, *Guardians of the Galaxy Vol. 3*), so movies keep it.

    Wrapping the whole thing in double quotes searches for it EXACTLY: ``"my pal"``
    goes to TMDB as ``my pal``, with no peeling and no separator rewriting. That's
    the escape hatch for a title the cleaning would otherwise damage — one ending
    in a word this function treats as noise ("Pal", "Vol"), or one that genuinely
    contains something like "Season".
    """
    literal = _unquote(query)
    if literal is not None:
        return literal, None, None

    q = _normalize_separators(query)

    # Year: a parenthesized (YYYY) anywhere (disc names sometimes put it before
    # "Season N", e.g. "Breaking Bad (2008) Season 4 Disc 2").
    m = re.search(r"\((\d{4})\)", q)
    year = int(m.group(1)) if m else None
    q = re.sub(r"\s*\(\d{4}\)\s*", " ", q).strip()

    season: int | None = None
    if kind == "tv":
        sm = re.search(r"\b(?:season|s)\s*0*(\d{1,2})\b", q, flags=re.IGNORECASE)
        if sm:
            season = int(sm.group(1))

    # Peel trailing authoring descriptors: "Season 2", "Disc 1", "S01", "D2".
    # "Volume/Vol" and the Sxx/Dxx short forms are TV-only — real movie titles use
    # them (*Kill Bill: Vol. 1*, *Guardians of the Galaxy Vol. 3*).
    #
    # NTSC/PAL are the TV video standard the disc was authored for, stamped on the
    # label (HAWAII_FIVE_0_S4_D5_NTSC). They apply to movie and TV discs alike, and
    # they matter beyond their own text: this loop only strips from the END, so an
    # unrecognised trailing token stops it dead — leaving "S4 D5" on the title too.
    # Tradeoff: a real title whose LAST word is "Pal" loses it ("My Pal" -> "My").
    # That's rare, it only degrades a TMDB search (naming is still confirmable by
    # hand), and every PAL-region disc gains — but it is why "Pal Joey" is safe
    # (not trailing) and a disc labelled just "PAL" survives via the fallback below.
    words = r"season|seasons|disc|disk|ntsc|pal"
    if kind == "tv":
        words += r"|volume|vol\.?|s0*\d+|d0*\d+"
    tail = re.compile(rf"[\s:_.\-]*\b(?:{words})\b\s*\d*\s*$", re.IGNORECASE)
    prev = None
    while prev != q:  # repeat for stacked descriptors ("Season 2 Disc 1")
        prev = q
        q = tail.sub("", q).strip()

    if kind == "tv":  # set-level descriptors TMDB doesn't want
        q = re.sub(r"[\s:_.\-]*\b(?:the\s+)?complete\s+series\b\s*$", "", q,
                   flags=re.IGNORECASE).strip()
        q = re.sub(r"[\s:_.\-]*\bcollection\b\s*$", "", q, flags=re.IGNORECASE).strip()

    q = re.sub(r"\s{2,}", " ", q).strip()
    return (q or query.strip()), year, season


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
