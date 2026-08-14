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
COLUMNS = ("disc_label", "show", "year", "season", "first_episode", "episodes")

EXAMPLE_CSV = """\
disc_label,show,year,season,first_episode,episodes
HAWAII_FIVE_0_S1_*,Hawaii Five-0,2010,,,
THE_OFFICE_S3_D2,The Office,2005,3,9,4
"""


@dataclass
class PlanRow:
    """One CSV line: which discs it covers, and what they are."""

    disc_label: str                    # match pattern; a trailing "*" is a wildcard
    show: str
    year: int | None = None
    season: int | None = None          # blank -> read from the disc label
    first_episode: int | None = None   # blank -> derived from the disc number
    episodes: int | None = None        # expected count; a mismatch parks the job
    line: int = 0                      # source line, for error messages

    def as_dict(self) -> dict:
        return {
            "disc_label": self.disc_label, "show": self.show, "year": self.year,
            "season": self.season, "first_episode": self.first_episode,
            "episodes": self.episodes, "line": self.line,
        }


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


def _int(value: str | None, field_name: str, line: int, errors: list[str]) -> int | None:
    """Parse an optional integer cell, reporting rather than raising on junk."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        errors.append(f"line {line}: {field_name} must be a whole number, got “{text}”.")
        return None


def parse_csv(text: str) -> tuple[list[PlanRow], list[str]]:
    """Parse plan CSV into rows plus human-readable errors.

    Never raises: one bad line is reported and skipped so the rest of a long box-set
    file still loads. A header row is required, which also makes column order free.
    """
    rows: list[PlanRow] = []
    errors: list[str] = []
    if not (text or "").strip():
        return rows, errors
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return rows, ["The CSV needs a header row: " + ",".join(COLUMNS)]
    header = {(h or "").strip().lower() for h in reader.fieldnames}
    for required in ("disc_label", "show"):
        if required not in header:
            errors.append(f"Missing required column “{required}”.")
    if errors:
        return rows, errors
    unknown = header - set(COLUMNS)
    if unknown:
        errors.append("Ignoring unknown column(s): " + ", ".join(sorted(unknown)))

    seen: set[str] = set()
    for line, raw in enumerate(reader, start=2):  # line 1 is the header
        cell = {(k or "").strip().lower(): (v or "") for k, v in raw.items() if k}
        label = cell.get("disc_label", "").strip()
        show = cell.get("show", "").strip()
        if not label and not show:
            continue  # blank line
        if not label:
            errors.append(f"line {line}: disc_label is required.")
            continue
        if not show:
            errors.append(f"line {line}: show is required.")
            continue
        key = _norm(label)
        if key in seen:
            errors.append(f"line {line}: duplicate disc_label “{label}” — the first one wins.")
            continue
        seen.add(key)
        rows.append(PlanRow(
            disc_label=label,
            show=show,
            year=_int(cell.get("year"), "year", line, errors),
            season=_int(cell.get("season"), "season", line, errors),
            first_episode=_int(cell.get("first_episode"), "first_episode", line, errors),
            episodes=_int(cell.get("episodes"), "episodes", line, errors),
            line=line,
        ))
    return rows, errors


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
    """The saved CSV text, or "" when no plan has been written yet."""
    try:
        return plan_path(settings).read_text(encoding="utf-8")
    except OSError:
        return ""


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
