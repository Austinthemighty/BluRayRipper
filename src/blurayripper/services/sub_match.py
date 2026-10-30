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
from dataclasses import dataclass, field

from .subtitles import srt_cues

# A match needs this much overlap, and this much lead over the title's next
# best episode, to be named without asking.
MIN_SCORE, MIN_MARGIN = 0.10, 0.04

# Below this many dialogue trigrams a title can't be said to match NOTHING: an
# OCR that failed, or a track of sound cues, reads the same as an extra.
MIN_DIALOGUE = 100

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


def dialogue_size(srt_text: str, n: int = 3) -> int:
    """How much dialogue a subtitle text holds, in trigrams."""
    return len(shingles(normalize_dialogue(srt_text), n))


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
    min_score: float = MIN_SCORE,
    min_margin: float = MIN_MARGIN,
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
    min_score: float = MIN_SCORE,
    min_margin: float = MIN_MARGIN,
) -> list[Match]:
    """Match ripped titles to reference episodes by subtitle similarity.

    ``rips`` maps title_id -> OCR'd SRT text; ``refs`` maps episode number ->
    reference SRT text. Returns one Match per title (input order preserved).
    """
    score = score_matrix(rips, refs, n)

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


def score_matrix(rips: dict[int, str], refs: dict[int, str],
                 n: int = 3) -> dict[int, dict[int, float]]:
    """{title: {episode: similarity}} for every title against every reference."""
    rip_sh = {tid: shingles(normalize_dialogue(t), n) for tid, t in rips.items()}
    ref_sh = {ep: shingles(normalize_dialogue(t), n) for ep, t in refs.items()}
    return {tid: {ep: _jaccard(rs, ref_sh[ep]) for ep in ref_sh} for tid, rs in rip_sh.items()}


@dataclass
class Verdict:
    """What to do with each title of a disc after matching."""
    named: list[Match] = field(default_factory=list)       # confident: name these
    skipped: dict[int, str] = field(default_factory=dict)  # extras, and why
    unsure: dict[int, str] = field(default_factory=dict)   # ask, and why
    propose_skip: set[int] = field(default_factory=set)    # ask, with Skip preselected

    @property
    def clean(self) -> bool:
        """Nothing to ask: the disc can be named as it stands."""
        return bool(self.named) and not self.unsure


def _code(ep: int) -> str:
    return f"E{ep:02d}"


def judge(wanted: list[int], matches: list[Match], scores: dict[int, dict[int, float]],
          *, dialogue: dict[int, int], skip_unmatched: bool = False,
          missing: list[int] = (), taken_elsewhere: dict[int, int] | None = None,
          season: int | None = None) -> Verdict:
    """Decide, title by title, whether a disc can be named without asking.

    ``scores`` are each title's similarity to EVERY reference fetched
    (score_matrix), not just the ones identify() had left to give it, and
    ``dialogue`` how much dialogue each title has (dialogue_size).

    - A confident match is named, unless another job already names that
      episode (``taken_elsewhere``: episode -> job): then it asks rather than
      replace that job's file.
    - A title matches NOTHING when it has real dialogue and its best score
      against every episode is under MIN_SCORE. With ``skip_unmatched`` (box-set
      mode) such a title is an extra and is skipped, but only when every other
      title was named confidently and no candidate episode is ``missing`` a
      reference (one could be the very episode the title is). Otherwise it is
      asked about, with Skip proposed.
    - Anything else asks. A title closest to an episode another title on the
      disc took (an alternate cut of a finale, say) says so.
    """
    taken_elsewhere = taken_elsewhere or {}
    by_title = {m.title_id: m for m in matches}
    v = Verdict()
    for tid in wanted:
        m = by_title.get(tid)
        if m is not None and m.confident and m.episode is not None:
            eps = range(m.episode, (m.episode_end or m.episode) + 1)
            clash = [e for e in eps if e in taken_elsewhere]
            if clash:
                v.unsure[tid] = (f"{_code(clash[0])} ({m.score:.2f}), but job "
                                 f"#{taken_elsewhere[clash[0]]} already names "
                                 f"{_code(clash[0])} — asking rather than replace it")
            else:
                v.named.append(m)
    holder = {e: m for m in v.named for e in range(m.episode, (m.episode_end or m.episode) + 1)}
    nothing: dict[int, tuple[int, float]] = {}
    for tid in wanted:
        if tid in v.unsure or any(m.title_id == tid for m in v.named):
            continue
        m, row = by_title.get(tid), scores.get(tid) or {}
        if m is None or not row:
            v.unsure[tid] = ("no subtitles to match" if m is None
                             else "no reference subtitles to match against")
            continue
        best = max(row, key=row.get)
        if row[best] < MIN_SCORE and dialogue.get(tid, 0) >= MIN_DIALOGUE:
            nothing[tid] = (best, row[best])
            continue
        if dialogue.get(tid, 0) < MIN_DIALOGUE:
            v.unsure[tid] = "too little dialogue to tell"
        elif best in holder and holder[best].title_id != tid:
            other = holder[best]
            v.unsure[tid] = (f"closest to {_code(best)} ({row[best]:.2f}), which title "
                             f"{other.title_id} took ({other.score:.2f}) — an alternate cut?")
        else:
            v.unsure[tid] = (f"closest to {_code(best)} ({row[best]:.2f})"
                             + (f", then {_code(m.runner_up)} ({m.runner_up_score:.2f})"
                                if m.runner_up is not None and m.runner_up != best else "")
                             + " — too close to call")
    of_season = f" of season {season}" if season is not None else ""
    blockers = sorted(v.unsure)          # decided before any extra is looked at
    for tid, (best, score) in nothing.items():
        why = f"matches no episode{of_season} (best {_code(best)} at {score:.2f}, under {MIN_SCORE:.2f})"
        if not v.named:
            v.unsure[tid] = (f"{why}; no title on this disc matched any episode — "
                             "the wrong season, or a disc of extras?")
            continue
        if skip_unmatched and not blockers and not missing:
            v.skipped[tid] = why
            continue
        if not skip_unmatched:
            reason = "an extra?"
        elif blockers:
            reason = (f"not skipped while title{'s' if len(blockers) > 1 else ''} "
                      f"{', '.join(map(str, blockers))} "
                      f"{'are' if len(blockers) > 1 else 'is'} unsure")
        else:
            reason = ("not skipped: OpenSubtitles has no reference for "
                      + ", ".join(_code(e) for e in sorted(missing)[:6])
                      + (" …" if len(missing) > 6 else "") + ", which it could be")
        v.unsure[tid] = f"{why} — {reason}"
        v.propose_skip.add(tid)
    return v
