"""Content-based SDH (hearing-impaired) detection for OCR'd subtitles.

Blu-ray PGS tracks ripped from disc almost never carry the MKV
``flag_hearing_impaired`` property or a descriptive track name, so the only
reliable signal for "is this SDH or a standard track?" is the subtitle text
itself. An SDH track is full of non-dialogue cues a standard track lacks:

  - bracketed sound descriptions:  ``[door creaks]``, ``(sighs)``
  - music symbols:                 ``♪ ♫``
  - speaker labels:                ``MAN:``, ``MRS. SMITH:``

We OCR every track to SRT anyway, so we scan the result and classify a full
track as SDH when a meaningful fraction of its cues carry these markers.
"""

from __future__ import annotations

import re

_TIMECODE = re.compile(r"\d\d:\d\d:\d\d[,.]\d\d\d\s*-->")
# A bracketed description of >=2 chars: [door creaks] / (gunshot). Italic/markup
# tags like <i> use angle brackets, so they don't trip this.
_BRACKET = re.compile(r"[\[(][^\])]{2,}[\])]")
_MUSIC = re.compile("[♩♪♫♬♭♮♯]")  # ♩ ♪ ♫ ♬ ♭ ♮ ♯
# Speaker label at the start of a line: optional dash, then ALL-CAPS name + colon.
_SPEAKER = re.compile(r"^[-\s]*[A-Z][A-Z0-9 .'&]{1,24}:")
_TAGS = re.compile(r"<[^>]+>")  # strip <i>, <b>, font tags before label matching


def srt_cues(srt_text: str) -> list[str]:
    """Split SRT content into per-cue text blocks (index + timecode lines removed)."""
    cues: list[str] = []
    for chunk in re.split(r"\n[ \t]*\n", srt_text.replace("\r\n", "\n")):
        lines = [ln for ln in chunk.splitlines() if ln.strip()]
        text = [ln for ln in lines if not ln.strip().isdigit() and not _TIMECODE.search(ln)]
        if text:
            cues.append("\n".join(text))
    return cues


def tidy_captions(srt_text: str) -> str:
    """A DVD's closed captions, as MakeMKV writes them, made into a plain sidecar.

    MakeMKV keeps each caption's on-screen column as padding spaces and leaves
    blank lines inside cues. Both go, and the cues are renumbered. The speaker
    names, sound cues, lyrics and italics stay: they are what makes captions
    SDH, and they are what the file is named as.

    Parsed by timecode rather than by blank lines, so a caption with a blank
    row in the middle keeps its second half.
    """
    lines = srt_text.replace("\r\n", "\n").split("\n")
    cues: list[tuple[str, list[str]]] = []
    for i, raw in enumerate(lines):
        line = raw.strip()
        if _TIMECODE.search(line):
            cues.append((line, []))
        elif not line:
            continue
        elif line.isdigit() and i + 1 < len(lines) and _TIMECODE.search(lines[i + 1]):
            continue                         # the next cue's number
        elif cues:
            cues[-1][1].append(line)
    out = [f"{n}\n{timing}\n" + "\n".join(text)
           for n, (timing, text) in enumerate((c for c in cues if c[1]), start=1)]
    return "\n\n".join(out) + "\n" if out else ""


def _is_sdh_cue(cue: str) -> bool:
    if _BRACKET.search(cue) or _MUSIC.search(cue):
        return True
    clean = _TAGS.sub("", cue)
    return any(_SPEAKER.match(line) for line in clean.splitlines())


def looks_like_sdh(srt_text: str, *, min_cues: int = 40, threshold: float = 0.06) -> bool:
    """True if the SRT text reads like an SDH/hearing-impaired track.

    Short tracks (under ``min_cues`` cues — typically forced/partial subtitles)
    are never classified as SDH; the forced flag covers those separately.
    """
    cues = srt_cues(srt_text)
    if len(cues) < min_cues:
        return False
    hits = sum(1 for c in cues if _is_sdh_cue(c))
    return hits / len(cues) >= threshold
