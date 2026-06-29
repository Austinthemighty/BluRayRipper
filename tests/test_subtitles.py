"""Content-based SDH detection on OCR'd SRT text."""

from blurayripper.services.subtitles import looks_like_sdh, srt_cues


def _srt(cues: list[str]) -> str:
    blocks = []
    for i, text in enumerate(cues, 1):
        t0, t1 = i * 2, i * 2 + 1
        blocks.append(
            f"{i}\n00:00:{t0:02d},000 --> 00:00:{t1:02d},000\n{text}\n"
        )
    return "\n".join(blocks)


def test_srt_cues_strips_index_and_timecodes():
    cues = srt_cues(_srt(["Hello there.", "[door creaks]"]))
    assert cues == ["Hello there.", "[door creaks]"]


def test_standard_track_not_sdh():
    # Plain dialogue, only a stray parenthetical — below threshold.
    lines = [f"Line of dialogue number {i}." for i in range(80)]
    lines[3] = "(quietly) I'll be back."
    assert looks_like_sdh(_srt(lines)) is False


def test_sdh_track_detected_from_sound_cues():
    lines = [f"Line of dialogue number {i}." for i in range(80)]
    for i in range(0, 80, 8):  # ~12% sound cues / labels
        lines[i] = "[explosion]"
    lines[1] = "MAN: Get down!"
    lines[9] = "♪ ominous music ♪"
    assert looks_like_sdh(_srt(lines)) is True


def test_short_forced_track_never_sdh():
    # A handful of cues (forced subs) — even if bracketed, not classified SDH.
    assert looks_like_sdh(_srt(["[in Russian]", "[in French]"])) is False


def test_speaker_labels_drive_detection():
    lines = [f"Some spoken line {i}." for i in range(60)]
    for i in range(0, 60, 6):
        lines[i] = "WOMAN: Over here!"
    assert looks_like_sdh(_srt(lines)) is True
