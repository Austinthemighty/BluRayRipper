"""Box-set plan: a user-written CSV that names TV discs before you insert them.

Ripping a season means the same mapping form once per disc. A plan row says "discs
whose label looks like this are this show, this season", so an inserted disc names
itself and starts ripping with no form at all.

The plan is keyed by the disc's VOLUME LABEL, not by the fingerprint the existing
``services.mapping_store`` uses: a fingerprint is sha1(label + every title duration),
so it only exists once the disc has been scanned and can't be written ahead of time.
The two compose — once a planned disc finishes ripping, ``_persist_mapping`` stores
its fingerprint, and any later re-rip takes the older remembered-mapping fast path.

Everything here is pure except ``load_plan``/``save_plan``, so the matching and
inference are testable without a disc.
"""

from __future__ import annotations

import csv
import fnmatch
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from .models import DiscInfo, EpisodeAssignment, MediaKind
from .services import naming
from .tools import discinfo

PLAN_FILENAME = "boxsets.local.csv"

# Only the first two are required; the rest are inferred from the disc label when
# blank (a label like HAWAII_FIVE_0_S1_D4 already states season and disc number).
COLUMNS = ("disc_label", "show", "year", "season", "disc", "collection",
           "first_episode", "episodes")

# What people actually type in a spreadsheet, folded to the names above. Written
# out rather than guessed at because a header this file does not recognise is
# reported as unknown and its column silently ignored — and "Disk" vs "disc" is
# not a mistake worth making someone hunt for.
_HEADER_ALIASES = {
    "disc label": "disc_label", "disk label": "disc_label",
    "disc name": "disc_label", "disk name": "disc_label",
    "label": "disc_label", "volume label": "disc_label",
    "show": "show", "series": "show", "title": "show",
    "year": "year",
    "season": "season",
    "disc": "disc", "disk": "disc",
    "disc number": "disc", "disk number": "disc",
    "disc in season": "disc", "disk in season": "disc",
    "collection": "collection",
    "collection number": "collection",
    "disc collection number": "collection", "disk collection number": "collection",
    "set number": "collection", "disc of set": "collection",
    "first episode": "first_episode", "start episode": "first_episode",
    "episodes": "episodes", "episode count": "episodes", "number of episodes": "episodes",
}

EXAMPLE_CSV = """\
disc_label,show,year,season,disc,collection,first_episode,episodes
HAWAII_FIVE_0_S1_*,Hawaii Five-0,2010,,,,,
,How I Met Your Mother,2005,1,1,1,,8
,How I Met Your Mother,2005,1,2,2,,8
,How I Met Your Mother,2005,1,3,3,,6
,How I Met Your Mother,2005,2,1,4,,8
"""


@dataclass
class PlanRow:
    """One CSV line: which discs it covers, and what they are."""

    # Match pattern; a trailing "*" is a wildcard. OPTIONAL: a box set normally
    # prints one label on all of its discs ("HIMYM" on every one of 28), so for
    # most sets there is nothing here to write. Such a row cannot name a disc on
    # insertion — nothing identifies it — but it still answers "season 2, disc 3:
    # which episodes?" when the disc is named by hand.
    disc_label: str = ""
    show: str = ""
    year: int | None = None
    season: int | None = None          # blank -> read from the disc label
    disc: int | None = None            # disc WITHIN the season (S2 D3)
    collection: int | None = None      # disc within the whole set (disc 15 of 28)
    first_episode: int | None = None   # blank -> summed from earlier discs
    episodes: int | None = None        # expected count; a mismatch parks the job
    line: int = 0                      # source line, for error messages

    def as_dict(self) -> dict:
        return {
            "disc_label": self.disc_label, "show": self.show, "year": self.year,
            "season": self.season, "disc": self.disc, "collection": self.collection,
            "first_episode": self.first_episode,
            "episodes": self.episodes, "line": self.line,
        }

    @property
    def last_episode(self) -> int | None:
        if self.first_episode is None or not self.episodes:
            return None
        return self.first_episode + self.episodes - 1


@dataclass
class PlanMatch:
    """The outcome of applying a row to a scanned disc: either assignments to use,
    or a reason the job must fall back to the normal naming gate."""

    assignments: list[EpisodeAssignment] = field(default_factory=list)
    reason: str | None = None          # set when the plan must NOT be applied
    season: int | None = None
    first_episode: int | None = None

    @property
    def ok(self) -> bool:
        return bool(self.assignments) and self.reason is None


def _norm(label: str) -> str:
    """Fold a label to its comparable form: lowercase, with every run of
    punctuation (``_``, ``-``, ``.``, ``:`` …) reduced to one space.

    Disc labels are underscored (``HAWAII_FIVE_0_S1_D4``) but people write the
    show's real title (``Hawaii Five-0 S1 D4``), so matching has to ignore which
    separator was used. ``*`` and ``?`` survive — they're the wildcard syntax.
    """
    folded = re.sub(r"[^0-9a-z*?]+", " ", (label or "").casefold())
    return re.sub(r"\s{2,}", " ", folded).strip()


def _int(value: str | None, field_name: str, line: int, errors: list[str],
         minimum: int | None = None) -> int | None:
    """Parse an optional integer cell, reporting rather than raising on junk.

    ``minimum`` is only set where an out-of-range number would be ACTED ON: most
    cells are safer carried through as written (a bad ``episodes`` count parks
    the disc precisely because it doesn't match), but first_episode has no such
    backstop — it is used directly to number the files."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        number = int(text)
    except ValueError:
        errors.append(f"line {line}: {field_name} must be a whole number, got “{text}”.")
        return None
    if minimum is not None and number < minimum:
        errors.append(
            f"line {line}: {field_name} must be {minimum} or more, got “{text}”.")
        return None
    return number


def parse_csv(text: str) -> tuple[list[PlanRow], list[str]]:
    """Parse plan CSV into rows plus human-readable errors.

    Never raises: one bad line is reported and skipped so the rest of a long box-set
    file still loads. A header row is required, which also makes column order free.
    """
    rows: list[PlanRow] = []
    errors: list[str] = []
    # Excel's "CSV UTF-8" export writes a byte-order mark, which is part of the
    # first header name — so disc_label read as "﻿disc_label" and the user
    # was told the column was missing while looking straight at it.
    text = (text or "").lstrip("﻿")
    if not text.strip():
        return rows, errors
    reader = csv.DictReader(io.StringIO(text))
    try:
        fieldnames = reader.fieldnames
    except csv.Error as exc:
        return rows, [f"The CSV could not be read: {exc}."]
    if not fieldnames:
        return rows, ["The CSV needs a header row: " + ",".join(COLUMNS)]
    canon = {(h or ""): _canonical_header(h) for h in fieldnames}
    header = {name for name in canon.values() if name}
    if "show" not in header:
        errors.append("Missing required column “show”.")
    if errors:
        return rows, errors
    unknown = sorted(raw.strip() for raw, name in canon.items()
                     if raw.strip() and name is None)
    if unknown:
        errors.append("Ignoring unknown column(s): " + ", ".join(unknown))

    seen: set[str] = set()
    rest = iter(reader)
    line = 1                                       # line 1 is the header
    while True:
        line += 1
        try:
            raw = next(rest)
        except StopIteration:
            break
        except csv.Error as exc:
            # ONE unbalanced quote makes csv swallow every following line into a
            # single field, which trips its 128 KB field limit and raised out of
            # a function documented never to raise — 500ing the box-set editor
            # and, worse, the disc-scan path that reads the plan on every insert.
            # The reader can't resynchronise after this, so say where it stopped.
            errors.append(f"line {line}: unreadable ({exc}); check for an unclosed "
                          'quote (") above this line. Nothing after it was read.')
            break
        cell = {}
        for k, v in raw.items():
            name = _canonical_header(k) if k else None
            if name:
                cell[name] = v or ""
        label = cell.get("disc_label", "").strip()
        show = cell.get("show", "").strip()
        if not label and not show:
            continue  # blank line
        if not show:
            errors.append(f"line {line}: show is required.")
            continue
        season = _int(cell.get("season"), "season", line, errors)
        disc = _int(cell.get("disc"), "disc", line, errors, minimum=1)
        collection = _int(cell.get("collection"), "collection", line, errors, minimum=1)
        # A row has to be findable by SOMETHING. With a label it names a disc on
        # insertion; without one it is still a usable answer to "season 2, disc
        # 3 — which episodes?", but only if it says which disc it describes.
        if not label and disc is None and collection is None:
            errors.append(
                f"line {line}: needs a disc_label, or a season and disc number "
                f"(or a collection number) — otherwise nothing identifies it.")
            continue
        key = (_norm(label) if label
               else f"{_norm(show)}|s{season}|d{disc}|c{collection}")
        if key in seen:
            errors.append(
                f"line {line}: duplicate "
                + (f"disc_label “{label}”" if label else f"row for “{show}”")
                + " — the first one wins.")
            continue
        seen.add(key)
        rows.append(PlanRow(
            disc_label=label,
            show=show,
            year=_int(cell.get("year"), "year", line, errors),
            season=season,
            disc=disc,
            collection=collection,
            # Episodes are numbered from 1 — Plex has no E00. Anything below it
            # is a typo, and unlike a wrong `episodes` count nothing downstream
            # catches it: the number goes straight into the filename.
            first_episode=_int(cell.get("first_episode"), "first_episode", line,
                               errors, minimum=1),
            episodes=_int(cell.get("episodes"), "episodes", line, errors),
            line=line,
        ))
    _derive_first_episodes(rows)
    return rows, errors


def _canonical_header(name: str | None) -> str | None:
    """The column a header means, or None if it means nothing to us.

    Spreadsheets get typed by hand, so "Disk collection number", "disc_label"
    and "Episodes" all have to land on the same three columns.
    """
    key = re.sub(r"[\s_]+", " ", (name or "").strip().casefold())
    if key in COLUMNS:
        return key
    return _HEADER_ALIASES.get(key)


def _derive_first_episodes(rows: list[PlanRow]) -> None:
    """Fill in each disc's first episode by summing the discs before it.

    A box-set spreadsheet lists how many episodes are on each disc, because that
    is what you can see by looking at the case. Where each disc STARTS is then
    arithmetic nobody should be doing by hand: season 1 disc 3 begins at episode
    17 precisely because discs 1 and 2 held eight each.

    Only within one show and season, only for rows that give a disc number and a
    count, and never over a row that states its own first_episode — a stated
    number is a correction, and a set with a double-length opener needs one.
    Stops at the first gap: if disc 2's count is missing, disc 3 cannot be
    placed, and a guess would silently misname a whole disc.
    """
    groups: dict[tuple, list[PlanRow]] = {}
    for row in rows:
        if row.disc is None or row.season is None:
            continue
        groups.setdefault((_norm(row.show), row.season), []).append(row)
    for group in groups.values():
        group.sort(key=lambda r: r.disc or 0)
        nxt: int | None = 1
        for row in group:
            if row.first_episode is None and nxt is not None:
                row.first_episode = nxt
            if row.first_episode is not None and row.episodes:
                nxt = row.first_episode + row.episodes
            else:
                nxt = None          # a gap: everything after it is unknowable


def find(rows: list[PlanRow], show: str, season: int | None = None,
         disc: int | None = None, collection: int | None = None) -> PlanRow | None:
    """The row describing one disc of ``show``, looked up by its NUMBER.

    The other direction from ``match``: that one answers "a disc with this label
    just went in, what is it?", which needs a label the disc actually prints.
    This answers "I am holding season 2 disc 3, what is on it?", which is the
    question a box set leaves you with when all its discs are labelled alike.

    A collection number identifies a disc on its own — it counts through the
    whole set — so it is tried first; season+disc needs both to mean anything.
    """
    want = _norm(show)
    if not want:
        return None
    candidates = [r for r in rows if _norm(r.show) == want]
    if collection is not None:
        for row in candidates:
            if row.collection == collection:
                return row
    if season is not None and disc is not None:
        for row in candidates:
            if row.season == season and row.disc == disc:
                return row
    return None


def match(rows: list[PlanRow], label: str | None) -> PlanRow | None:
    """The row covering ``label``. An exact match always beats a wildcard; among
    wildcards the longest (most specific) pattern wins, so a per-disc row can
    override the season-wide row that would otherwise catch it."""
    target = _norm(label or "")
    if not target:
        return None
    wild: list[PlanRow] = []
    for row in rows:
        pattern = _norm(row.disc_label)
        if pattern == target:
            return row
        if "*" in pattern or "?" in pattern:
            wild.append(row)
    hits = [r for r in wild if fnmatch.fnmatchcase(target, _norm(r.disc_label))]
    if not hits:
        return None
    return max(hits, key=lambda r: len(_norm(r.disc_label)))


def assignments_for(row: PlanRow, disc: DiscInfo) -> PlanMatch:
    """Turn a plan row + a scanned disc into episode assignments.

    Episode-length titles (in playlist order) become consecutive episodes starting
    at the row's first episode. Anything the plan can't state confidently returns a
    reason instead, so the job parks at the normal gate rather than guessing — a
    wrong auto-rip costs a whole disc.
    """
    label = (disc.volume_label or disc.disc_name or "") if disc else ""
    titles = discinfo.episode_candidate_titles(disc) if disc else []
    count = len(titles)
    if not count:
        return PlanMatch(reason="no episode-length titles on this disc")

    if row.episodes is not None and row.episodes != count:
        return PlanMatch(
            reason=f"plan expects {row.episodes} episode(s) but the disc has {count}")

    season = row.season
    if season is None:
        _title, _year, season = naming.clean_tmdb_query(label, "tv")
    if season is None:
        return PlanMatch(reason="no season in the plan row or the disc label")

    first = row.first_episode
    if first is None:
        span = naming.episode_span(naming.disc_number(label), count)
        first = span[0] if span else None
    if first is None:
        return PlanMatch(
            reason="no first episode in the plan row and no disc number in the label")
    # Checked here as well as at parse time because this is the point of consent:
    # a matching row means "rip this disc with NO naming gate", and a first
    # episode below 1 numbers the files S01E00 / S01E-5, which Plex will not
    # match. Parking costs one mapping form; a bad auto-rip costs the disc.
    if first < 1:
        return PlanMatch(
            reason=f"plan row starts at episode {first}; episode numbers start at 1")

    assignments = [
        EpisodeAssignment(
            title_id=t.title_id,
            kind=MediaKind.TV,
            show_or_movie_title=row.show,
            year=row.year,
            season=season,
            episode=first + i,
        )
        for i, t in enumerate(titles)
    ]
    return PlanMatch(assignments=assignments, season=season, first_episode=first)


# --- storage ---------------------------------------------------------------

def plan_path(settings) -> Path:
    """Where the plan CSV lives — beside the other local, gitignored config."""
    return Path(settings.config_dir) / PLAN_FILENAME


def load_plan(settings) -> str:
    """The saved CSV text, or "" when no plan has been written yet.

    Decoded leniently: a plan saved from Excel or Notepad on Windows is
    Windows-1252, not UTF-8, the moment a show title has an accent in it — and
    the resulting UnicodeDecodeError is not an OSError, so it came out of here
    and out of every caller, including the disc-scan path that consults the plan
    on every insert. A mangled accent in one show name is a far smaller problem
    than no plan at all.
    """
    try:
        raw = plan_path(settings).read_bytes()
    except OSError:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


SHARED_KEY = "boxset.plan"


def save_plan(settings, text: str, store=None) -> Path:
    path = plan_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text or "", encoding="utf-8")
    if store is not None and getattr(store, "durable", False):
        try:
            store.set_shared_config(SHARED_KEY, {"csv": text or ""})
        except Exception:
            pass  # saved locally; the next sync publishes it
    return path


def sync_from_cluster(settings, store) -> str:
    """Refresh the plan from Postgres, which owns it. Returns a summary.

    Same reasoning as the settings overlay: the CSV is gitignored, so it never
    reached the other boxes and a machine move lost it outright. A missing plan
    is silent — discs just stop being auto-named and get queued for manual
    naming instead, which looks like the matcher having a bad day."""
    if store is None or not getattr(store, "durable", False):
        return "no shared store — this box's plan stays local to it"
    local = load_plan(settings)
    try:
        remote = store.get_shared_config(SHARED_KEY)
    except Exception as exc:
        return (f"cluster unreachable ({type(exc).__name__}) — keeping the local "
                "plan; it was NOT cleared")
    if remote is None:
        if not local.strip():
            return "nothing stored in the cluster yet"
        store.set_shared_config(SHARED_KEY, {"csv": local})
        return "seeded the cluster from this box"
    text = remote.get("csv", "")
    if text == local:
        return "in sync with the cluster"
    plan_path(settings).parent.mkdir(parents=True, exist_ok=True)
    plan_path(settings).write_text(text, encoding="utf-8")
    rows, _ = parse_csv(text)
    return f"pulled {len(rows)} box-set row(s) from the cluster"


def load_rows(settings) -> list[PlanRow]:
    """Parsed rows from the saved plan; errors are ignored here because the UI
    surfaces them at save time, and one bad line must not stop a rip."""
    rows, _errors = parse_csv(load_plan(settings))
    return rows
