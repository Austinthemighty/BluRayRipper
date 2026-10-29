"""Is every episode of a show accounted for?

A box set goes in disc by disc over days, the encode queue runs for weeks, and
the only list of what a season should hold is TMDB's. This compares the two:
each episode TMDB lists against the files in the library and the jobs that name
it. Run by hand on 2026-10-28, it found three HIMYM discs never ripped
(S01 E09-E16, S02 E17-E22, S09 E17-E24), and a bonus title in job 419 named
S03E01 that would have overwritten the real episode from job 417.

Pure, apart from ``library_episodes``, which reads one folder.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

DONE, FAILED = "done", "failed"

# "Show - S08E11.mkv" or, for a two-parter mastered as one, "Show - S08E11-E12.mkv".
_EPISODE_FILE = re.compile(r" - S(\d{2,})E(\d{2,})(?:-E(\d{2,}))?\.[A-Za-z0-9]+$")


@dataclass
class Claim:
    """One job naming one episode."""
    job_id: int
    state: str          # the job's live state: "done", "ready", "failed", ...


def episode_ranges(numbers) -> list[tuple[int, int]]:
    """[9, 10, 11, 16] -> [(9, 11), (16, 16)]."""
    out: list[tuple[int, int]] = []
    for n in sorted(set(numbers)):
        if out and n == out[-1][1] + 1:
            out[-1] = (out[-1][0], n)
        else:
            out.append((n, n))
    return out


def library_episodes(show_dir: Path) -> dict[int, set[int]]:
    """{season: {episodes}} from the video files under one show's folder. A
    two-parter file counts for both of its episodes."""
    found: dict[int, set[int]] = {}
    if not show_dir.is_dir():
        return found
    for f in show_dir.rglob("*"):
        m = _EPISODE_FILE.search(f.name)
        if not m or f.suffix.lower() in (".srt", ".nfo", ".jpg", ".png"):
            continue
        season, first = int(m[1]), int(m[2])
        last = int(m[3]) if m[3] else first
        found.setdefault(season, set()).update(range(first, max(first, last) + 1))
    return found


def check_season(season: int, expected: dict[int, str], in_library: set[int],
                 claims: dict[int, list[Claim]],
                 plan: dict[int, str] | None = None) -> dict:
    """One season's verdict.

    ``expected`` is TMDB's episode list ({number: name}). ``claims`` says which
    jobs name each episode, and ``plan`` which box-set disc should hold it.

    An episode is in the library (a file is there), queued (an unfinished job
    names it), failed (only failed jobs name it), or missing (nothing does, or
    a finished job does but its file is gone).

    One episode named by two live jobs is a conflict: whichever is placed
    second overwrites the other, and nobody would know which one survived.
    The exception is a single unfinished job alongside finished ones: that is
    a deliberate re-make (a re-rip of a damaged disc) and replaces the file.
    """
    plan = plan or {}
    episodes = []
    for n in sorted(expected):
        named = claims.get(n, [])
        live = [c for c in named if c.state != FAILED]
        if n in in_library:
            state = "library"
        elif any(c.state != DONE for c in live):
            state = "queued"
        elif named and not live:
            state = "failed"
        else:
            state = "missing"
        episodes.append({"episode": n, "name": expected[n], "state": state,
                         "jobs": sorted({c.job_id for c in named})})

    conflicts, remakes = [], []
    for n in sorted(claims):
        live = [c for c in claims[n] if c.state != FAILED]
        pending = sorted({c.job_id for c in live if c.state != DONE})
        done = sorted({c.job_id for c in live if c.state == DONE})
        if len(pending) + len(done) < 2:
            continue
        if len(pending) == 1 and done:
            remakes.append({"episode": n, "jobs": done + pending, "by": pending[0]})
        else:
            conflicts.append({"episode": n, "jobs": sorted(done + pending)})

    unexpected = sorted(
        ({n for n, cs in claims.items() if any(c.state != FAILED for c in cs)}
         | set(in_library)) - set(expected))

    by_state = {s: [e["episode"] for e in episodes if e["state"] == s]
                for s in ("library", "queued", "failed", "missing")}
    missing = [
        {"first": a, "last": b,
         "discs": sorted({plan[n] for n in range(a, b + 1) if n in plan})}
        for a, b in episode_ranges(by_state["missing"])]

    if not expected:
        status = "unknown"
    elif not by_state["library"] and not by_state["queued"] and not by_state["failed"]:
        status = "not_started"
    elif by_state["missing"] or by_state["failed"]:
        status = "incomplete"
    elif by_state["queued"]:
        status = "in_progress"
    else:
        status = "complete"

    return {
        "season": season, "status": status, "total": len(expected),
        "counts": {s: len(v) for s, v in by_state.items()},
        "episodes": episodes, "missing": missing, "failed": by_state["failed"],
        "conflicts": conflicts, "remakes": remakes, "unexpected": unexpected,
    }
