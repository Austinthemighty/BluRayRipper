"""Audit finished jobs for missing subtitle sidecars.

A DONE job whose disc carried a kept-language subtitle track should have at least
one ``.srt`` sidecar next to its placed video. If it doesn't, subtitle extraction
or OCR failed at some point (older builds could proceed past that) — this flags
those movies so they can be re-run.
"""

from __future__ import annotations

from typing import Callable

from .models import StreamType
from .persistence import JobRecord, assignment_from_dict, disc_from_dict


def _expects_subs(disc, selected_title_ids, is_kept: Callable[[str | None], bool]) -> bool:
    """True if any selected title has a subtitle stream in a kept language."""
    if disc is None:
        return False
    for tid in selected_title_ids:
        title = disc.title(tid)
        if title and any(s.stream_type is StreamType.SUBTITLE and is_kept(s.language)
                         for s in title.streams):
            return True
    return False


def _name(rec: JobRecord, disc) -> str:
    if rec.assignments:
        title = getattr(assignment_from_dict(rec.assignments[0]), "show_or_movie_title", None)
        if title:
            return title
    return (disc.disc_name if disc else None) or rec.source


def audit_done_jobs(
    records: list[JobRecord],
    is_kept: Callable[[str | None], bool],
    list_sidecars: Callable[[str], list | None],
) -> list[dict]:
    """One result per DONE job. ``list_sidecars(video_path)`` returns the .srt
    files next to that video, or None if the folder is unreachable (e.g. the NAS
    isn't mapped in this session — then we can't verify it).

    status: 'ok' (has subs, or none were expected), 'missing' (expected but none
    found), or 'unreachable' (couldn't check any of its outputs)."""
    out = []
    for rec in records:
        if rec.state != "done":
            continue
        disc = disc_from_dict(rec.disc)
        expected = _expects_subs(disc, rec.selected_title_ids, is_kept)
        srt_count = 0
        reachable = False
        for video in rec.outputs:
            found = list_sidecars(video)
            if found is None:
                continue
            reachable = True
            srt_count += len(found)
        if not rec.outputs or not reachable:
            status = "unreachable"
        elif expected and srt_count == 0:
            status = "missing"
        else:
            status = "ok"
        out.append({
            "id": rec.id, "name": _name(rec, disc), "outputs": len(rec.outputs),
            "srts": srt_count, "expected": expected, "status": status,
        })
    return out
