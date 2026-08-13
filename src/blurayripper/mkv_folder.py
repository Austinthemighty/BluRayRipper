"""Decide whether a folder of .mkv files is one show's episodes or many titles.

A folder can be either. ``To Process/MKV's`` holds unrelated movies, where one
job per file is right — each gets its own name, and separate jobs encode in
parallel across workers. A season folder holds one show's episodes, where a job
per file means naming the same show ten times over.

The tell is the filenames. MakeMKV writes a set as ``<base>_t04.mkv`` … so the
stems collapse to a single shared base once the trailing counter is removed;
unrelated movies don't collapse to anything. Everything here is pure so the
heuristic can be tested without a filesystem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .services import naming

# The trailing counter MakeMKV (and most rippers) append: _t04, .E01, - 01, _04.
# Anchored to the END, and it must be introduced by punctuation or a letter tag —
# NOT by a bare space. That exclusion is what stops a numbered movie franchise
# ("Rocky 2", "Rocky 3", "Apollo 13") collapsing to one "show", which would name
# separate films as episodes of each other.
_COUNTER_RE = re.compile(
    r"(?:[\s._-]*(?:t|track|title|part|pt|ep|episode|e)[\s._-]*|[._-]+\s*)(\d{1,3})\s*$",
    re.IGNORECASE,
)
# At least this many files before a shared base is meaningful — two movies could
# coincidentally share a prefix, a whole season won't be a coincidence.
MIN_SET = 2


@dataclass
class MkvSet:
    """A folder's .mkv files judged to be one show's episodes."""

    base: str            # the shared stem, e.g. "Rick and Morty Season 1"
    show: str            # cleaned for TMDB, e.g. "Rick and Morty"
    season: int | None   # read off the base when it says so
    files: list[Path]    # in counter order — the natural episode order


def _split_counter(stem: str) -> tuple[str, int] | None:
    """``("Rick and Morty Season 1_t04")`` -> ``("Rick and Morty Season 1", 4)``."""
    m = _COUNTER_RE.search(stem)
    if not m:
        return None
    base = stem[: m.start()].strip(" ._-")
    return (base, int(m.group(1))) if base else None


def analyse(paths: list[Path]) -> MkvSet | None:
    """The set these files form, or None if they're unrelated titles.

    None means "one job per file" — the caller's existing behaviour. A set means
    one job with a title per file, named once like a TV disc.
    """
    files = [Path(p) for p in paths]
    if len(files) < MIN_SET:
        return None
    split = [(_split_counter(p.stem), p) for p in files]
    if any(s is None for s, _ in split):
        return None                       # something has no counter -> not a set
    bases = {s[0].casefold() for s, _ in split}
    if len(bases) != 1:
        return None                       # different titles -> unrelated files
    ordered = [p for _s, p in sorted(split, key=lambda sp: sp[0][1])]
    base = split[0][0][0]
    show, _year, season = naming.clean_tmdb_query(base, "tv")
    return MkvSet(base=base, show=show or base, season=season, files=ordered)
