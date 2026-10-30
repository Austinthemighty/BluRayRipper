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
    # Windows resolves a device name by the part BEFORE the first dot, so CON.mkv,
    # NUL.2020 and LPT1.x are the same devices as CON, NUL and LPT1. Testing the
    # whole component missed those: the deploy target is a Windows VM, where
    # mkdir("NUL.2020") fails and a write to "NUL.mkv" goes to the bit bucket and
    # reports success. Splitting on the first dot keeps "Conan"/"Console" safe —
    # they are one dot-free part that simply isn't a device name.
    if cleaned.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
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

    A season and disc run together with no separator at all
    (``HOW_I_MET_YOUR_MOTHER_S3D1``) are split apart too. As one token, neither
    edge between them is a ``\\b``, so both were missed — no season, no disc, no
    box-set plan row — and "S3D1" stayed on the TMDB search.

    So are a descriptor word and its number (``LANDMAN_SEASON2``,
    ``BREAKING_BAD_S5_DISC1``). The season was read, but with no ``\b`` after
    the word nothing peeled it, and "LANDMAN SEASON2" went to TMDB.
    """
    q = (query or "").strip().replace("_", " ")
    if "." in q and " " not in q:
        q = q.replace(".", " ")
    q = re.sub(r"\b(s0*\d{1,2})(d0*\d{1,2})\b", r"\1 \2", q, flags=re.IGNORECASE)
    q = re.sub(r"\b(season|disc|disk)(\d{1,2})\b", r"\1 \2", q, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", q).strip()


# Disc labels spell numbers out at least as often as they use digits: "Landman:
# Season Two (Disc 1)" is a real MakeMKV disc name, and TMDB matches nothing for
# it while "Landman" matches at once. Twenty covers any real box set.
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20,
}
_ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11,
    "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15,
    "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
    "twentieth": 20,
}
_NUMBER_WORD_RE = "|".join(_NUMBER_WORDS)
_ORDINAL_WORD_RE = "|".join(_ORDINAL_WORDS)


# Disc/side descriptor: "Disc 2", "D2", "Disc2". The bare "d" form only counts
# when it's a standalone token, so a title word can't be read as a disc number.
_DISC_RE = re.compile(r"\b(?:disc|disk)\s*0*(\d{1,2})\b|\bd\s*0*(\d{1,2})\b", re.IGNORECASE)

# Market/region tags a label carries after its disc number
# (HOW_I_MET_YOUR_MOTHER_S2_D1_US): country codes and DVD region codes.
_REGION_TAGS = r"us|usa|uk|gb|ca|au|nz|eu|r[0-8]"


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
        else:
            # "Season Two" and "The Complete Fifth Season" are as common on a
            # box set's discs as "S02", and both left the season blank — so the
            # naming form had to be filled in by hand for every disc of the set.
            wm = re.search(rf"\bseasons?\s+({_NUMBER_WORD_RE})\b", q, re.IGNORECASE)
            om = re.search(rf"\b({_ORDINAL_WORD_RE})\s+seasons?\b", q, re.IGNORECASE)
            if wm:
                season = _NUMBER_WORDS[wm.group(1).lower()]
            elif om:
                season = _ORDINAL_WORDS[om.group(1).lower()]

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
    if kind == "tv":
        # Before the tail loop, and not anchored to the end: the loop peels
        # "Disc 2" off first, which takes "Season" with it and leaves "House:
        # The Complete Fifth" — a title TMDB likes no better than the original.
        q = re.sub(rf"[\s:_.\-]*\b(?:the\s+)?complete\s+"
                   rf"(?:{_ORDINAL_WORD_RE})\s+seasons?\b", " ", q,
                   flags=re.IGNORECASE).strip()

    # Brackets and number WORDS both used to defeat this. The loop only strips
    # from the END, so the ")" of "(Disc 1)" stopped it dead — and with the disc
    # descriptor left in place nothing further could be peeled either, which is
    # how "Landman: Season Two (Disc 1)" reached TMDB intact and matched nothing.
    tail = re.compile(
        rf"[\s:_.\-]*[(\[]?\s*\b(?:{words})\b\s*(?:\d+|{_NUMBER_WORD_RE})?"
        rf"\s*[)\]]?\s*$",
        re.IGNORECASE)
    # A region tag after the disc number (HOW_I_MET_YOUR_MOTHER_S2_D1_US) stopped
    # the loop the way NTSC did, so "S2 D1 US" reached TMDB and no plan row
    # matched the show. Unlike NTSC/PAL it is only peeled straight after a disc
    # descriptor: these are ordinary words at the end of a title — *This Is Us*
    # — and there they are part of the name.
    disc_words = r"disc|disk|d" if kind == "tv" else r"disc|disk"
    region = re.compile(
        rf"(\b(?:{disc_words})\s*0*\d{{1,2}})[\s:_.\-]+(?:{_REGION_TAGS})\s*$",
        re.IGNORECASE)
    prev = None
    while prev != q:  # repeat for stacked descriptors ("Season 2 Disc 1")
        prev = q
        q = region.sub(r"\1", q)
        q = tail.sub("", q).strip()

    if kind == "tv":  # set-level descriptors TMDB doesn't want
        q = re.sub(r"[\s:_.\-]*\b(?:the\s+)?complete\s+series\b\s*$", "", q,
                   flags=re.IGNORECASE).strip()
        q = re.sub(r"[\s:_.\-]*\bcollection\b\s*$", "", q, flags=re.IGNORECASE).strip()

    q = re.sub(r"\s{2,}", " ", q).strip()
    return (q or query.strip()), year, season


# What a label is made of besides the show: descriptors, their numbers, and
# separators. A label of nothing else (SEASON_1_DISC_1) names no show.
_DESCRIPTOR_TOKEN = re.compile(
    rf"^(?:season|seasons|disc|disk|vol|volume|ntsc|pal|s0*\d+|d0*\d+|\d+|"
    rf"{_NUMBER_WORD_RE}|{_ORDINAL_WORD_RE})$", re.IGNORECASE)


def label_stem(label: str | None) -> str | None:
    """The show part of a disc label, folded so every disc of one set gives the
    same answer: ``HIMYM_S1_D3`` and ``HIMYM_S2_D1`` both give ``"himym"``.

    Box-set mode uses it to find the earlier discs of a set. None when the label
    names no show at all (``SEASON_1_DISC_1``, ``DISC_1``): two such discs are
    no more likely to be one set than any other two, and treating them as one
    would name the second after the first."""
    raw = (label or "").strip()
    title, _year, _season = clean_tmdb_query(raw, "tv")
    words = re.sub(r"[^0-9a-z]+", " ",
                   _normalize_separators(title).replace("&", " and ").casefold()).split()
    # clean_tmdb_query hands back the whole label when peeling leaves nothing.
    # A show that is itself a number (24_SEASON_1_DISC_1 -> "24") was peeled to.
    if not words or (title == raw and all(_DESCRIPTOR_TOKEN.match(w) for w in words)):
        return None
    return " ".join(words)


def _title_with_year(title: str, year: int | None) -> str:
    base = sanitize(title)
    return f"{base} ({year})" if year else base


# Plex's own vocabulary for versions ("4K", not "2160p"). Ordered high to low.
_RESOLUTION_LABELS = ((2160, "4K"), (1440, "1440p"), (1080, "1080p"),
                      (720, "720p"), (576, "576p"), (480, "480p"))


def resolution_label(height: int | None) -> str | None:
    """A version label for a title's height, or None when it isn't known.

    None matters: an unknown height must not become "SD" and file a Blu-ray as
    a lesser version of itself."""
    if not height or height <= 0:
        return None
    for cutoff, label in _RESOLUTION_LABELS:
        if height >= cutoff:
            return label
    return "SD"


def movie_video_path(
    movies_root: Path, title: str, year: int | None, container: str,
    edition: str | None = None, version: str | None = None,
) -> Path:
    """Where a movie is filed.

    ``version`` is Plex's multi-version convention: the same movie folder holds
    every copy, and a " - <version>" suffix tells them apart, so a 1080p and a
    4K rip of one film show up as two versions of one movie rather than two
    movies or one silently overwriting the other."""
    folder = _title_with_year(title, year)
    name = folder
    if version and version.strip():
        name = f"{name} - {sanitize(version)}"
    if edition and edition.strip():
        # Plex edition tag: "Title (Year) {edition-Extended Cut}.ext"
        name = f"{name} {{edition-{sanitize(edition)}}}"
    return movies_root / folder / f"{name}.{container}"


def episode_code(season: int, episode: int, episode_end: int | None = None) -> str:
    """``S08E11``, or ``S08E11-E12`` for one file holding two episodes — the
    form Plex reads as a multi-episode file."""
    code = f"S{season:02d}E{episode:02d}"
    if episode_end is not None and episode_end > episode:
        code += f"-E{episode_end:02d}"
    return code


def episode_video_path(
    tv_root: Path,
    show: str,
    year: int | None,
    season: int,
    episode: int,
    container: str,
    episode_end: int | None = None,
) -> Path:
    show_folder = _title_with_year(show, year)
    season_folder = f"Season {season:02d}"
    basename = f"{sanitize(show)} - {episode_code(season, episode, episode_end)}"
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
