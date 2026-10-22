"""Identify which episode a ripped title is by comparing its OCR'd subtitles
against a small set of reference subtitles (the episodes known to be on the disc).

The candidate set is tiny — just the episodes the user says are on this disc — so
this is a permutation problem (match N ripped titles to N known episodes), not an
open-ended search. We reduce each subtitle track to a bag of word-trigram
"shingles" of its dialogue (sound cues, speaker labels, tags and punctuation
stripped) and score a rip against a reference by Jaccard overlap. The correct
episode shares many trigrams even through OCR noise; wrong episodes share almost
none, so the right match stands out sharply.

Assignment is greedy on the global score matrix (highest cell first, then remove
that title and episode, repeat). Each result carries a confidence: the assigned
score must clear ``min_score`` and beat the title's next-best episode by
``min_margin`` — otherwise it's flagged for human confirmation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .subtitles import srt_cues

_TAG = re.compile(r"<[^>]+>")
_BRACKET = re.compile(r"[\[(][^\])]*[\])]")            # [door creaks] / (sighs)
_SPEAKER = re.compile(r"^[-\s]*[A-Z][A-Z0-9 .'&]{1,24}:")  # MAN: / MRS. SMITH:
_NONWORD = re.compile(r"[^a-z0-9' ]+")
_WS = re.compile(r"\s+")


def normalize_dialogue(srt_text: str) -> list[str]:
    """SRT text -> flat list of lowercased dialogue words (cues/labels removed)."""
    words: list[str] = []
    for cue in srt_cues(srt_text):
        cue = _TAG.sub(" ", cue)
        cue = _BRACKET.sub(" ", cue)
        lines = [_SPEAKER.sub("", ln) for ln in cue.splitlines()]
        text = _NONWORD.sub(" ", " ".join(lines).lower())
        text = _WS.sub(" ", text).strip()
        if text:
            words.extend(text.split())
    return words


def shingles(words: list[str], n: int = 3) -> set[str]:
    if len(words) < n:
        return set(words)
    return {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a) + len(b) - inter
    return inter / union if union else 0.0


def similarity(a_text: str, b_text: str, n: int = 3) -> float:
    """Jaccard overlap of dialogue trigrams between two SRT texts (0..1)."""
    return _jaccard(shingles(normalize_dialogue(a_text), n), shingles(normalize_dialogue(b_text), n))


@dataclass
class Match:
    title_id: int
    episode: int | None        # assigned episode (None if no candidate left)
    score: float               # similarity of the assigned pair
    runner_up: int | None      # title's next-best episode
    runner_up_score: float
    confident: bool
    # The last episode when the title holds two (a two-parter mastered as one).
    episode_end: int | None = None


def identify_two_parts(
    rips: dict[int, str],
    refs: dict[int, str],
    candidates: set[int],
    *,
    n: int = 3,
    min_score: float = 0.10,
    min_margin: float = 0.04,
) -> list[Match]:
    """Titles in ``candidates`` that hold two consecutive episodes.

    A two-parter mastered as one title (HIMYM S8's "The Final Page", E11+E12)
    carries both halves' dialogue, so against either reference alone it scores
    about half — and one-episode matching left it forever unsure. Scored here
    against each consecutive pair merged into one reference, it matches like any
    episode does. Taken only when a pair clearly beats every single episode and
    every other pair: a long single episode scores best on its own.
    """
    ref_sh = {ep: shingles(normalize_dialogue(t), n) for ep, t in refs.items()}
    found: list[Match] = []
    for tid in candidates:
        if tid not in rips:
            continue
        rs = shingles(normalize_dialogue(rips[tid]), n)
        pairs = sorted(((_jaccard(rs, ref_sh[e] | ref_sh[e + 1]), e)
                        for e in ref_sh if e + 1 in ref_sh), reverse=True)
        if not pairs:
            continue
        best, ep = pairs[0]
        singles = [_jaccard(rs, sh) for sh in ref_sh.values()]
        rival = max(([pairs[1][0]] if len(pairs) > 1 else []) + singles, default=0.0)
        if best >= min_score and best - rival >= min_margin:
            found.append(Match(tid, ep, best, None, rival, True, episode_end=ep + 1))
    return found


def identify(
    rips: dict[int, str],
    refs: dict[int, str],
    *,
    n: int = 3,
    min_score: float = 0.10,
    min_margin: float = 0.04,
) -> list[Match]:
    """Match ripped titles to reference episodes by subtitle similarity.

    ``rips`` maps title_id -> OCR'd SRT text; ``refs`` maps episode number ->
    reference SRT text. Returns one Match per title (input order preserved).
    """
    rip_sh = {tid: shingles(normalize_dialogue(t), n) for tid, t in rips.items()}
    ref_sh = {ep: shingles(normalize_dialogue(t), n) for ep, t in refs.items()}
    score: dict[int, dict[int, float]] = {
        tid: {ep: _jaccard(rs, ref_sh[ep]) for ep in ref_sh} for tid, rs in rip_sh.items()
    }

    # Greedy global assignment: take the highest remaining (title, episode) cell.
    remaining_t = set(rips)
    remaining_e = set(refs)
    assigned: dict[int, int] = {}
    cells = sorted(
        ((s, tid, ep) for tid, row in score.items() for ep, s in row.items()),
        key=lambda c: c[0],
        reverse=True,
    )
    for s, tid, ep in cells:
        if tid in remaining_t and ep in remaining_e:
            assigned[tid] = ep
            remaining_t.discard(tid)
            remaining_e.discard(ep)

    results: list[Match] = []
    for tid in rips:
        ep = assigned.get(tid)
        row = score.get(tid, {})
        # The title's best alternative episode (for a confidence margin).
        others = sorted(((e, sc) for e, sc in row.items() if e != ep), key=lambda x: x[1], reverse=True)
        runner_up, runner_up_score = (others[0] if others else (None, 0.0))
        sc = row.get(ep, 0.0) if ep is not None else 0.0
        confident = ep is not None and sc >= min_score and (sc - runner_up_score) >= min_margin
        results.append(Match(tid, ep, sc, runner_up, runner_up_score, confident))
    return results
