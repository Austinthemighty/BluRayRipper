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


# --- closed captions as a sidecar ------------------------------------------------------

from blurayripper.services.subtitles import tidy_captions  # noqa: E402

# The shape MakeMKV writes a DVD's captions in: each line padded to its on-screen
# column, blank lines left inside and after cues.
RAW_CAPTIONS = (
    "1\n00:00:01,869 --> 00:00:03,152\n            SAM:             \n"
    "     The lights went out again.      \n\n\n"
    "2\n00:00:03,154 --> 00:00:05,387\n             [THUNDER]              \n\n\n"
    "3\n00:00:05,389 --> 00:00:07,072\n     <i> ♪ Rain on the window ♪</i>  \n"
    "   \n"
    "     Room 20.   \n\n\n"
)


def test_captions_lose_their_padding_but_keep_what_makes_them_sdh():
    out = tidy_captions(RAW_CAPTIONS)

    assert out == (
        "1\n00:00:01,869 --> 00:00:03,152\nSAM:\nThe lights went out again.\n\n"
        "2\n00:00:03,154 --> 00:00:05,387\n[THUNDER]\n\n"
        "3\n00:00:05,389 --> 00:00:07,072\n<i> ♪ Rain on the window ♪</i>\nRoom 20.\n")


def test_a_blank_row_inside_a_caption_does_not_lose_its_second_half():
    """Splitting on blank lines would have cut "Room 20." off cue 3 and dropped
    it as a cue with no timecode. A line that is only a number stays text."""
    out = tidy_captions(RAW_CAPTIONS)
    assert "Room 20." in out
    assert tidy_captions("1\n00:00:01,000 --> 00:00:02,000\n  42  \n") == \
        "1\n00:00:01,000 --> 00:00:02,000\n42\n"


def test_captions_with_nothing_in_them_make_no_file():
    assert tidy_captions("") == ""
    assert tidy_captions("1\n00:00:01,000 --> 00:00:02,000\n     \n\n") == ""
