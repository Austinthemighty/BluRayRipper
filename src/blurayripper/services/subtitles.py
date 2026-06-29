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
