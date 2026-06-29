"""Subtitle-based episode identification: correct permutation through OCR noise,
and low-confidence flagging when episodes are indistinguishable."""

from blurayripper.services.sub_match import identify, normalize_dialogue, similarity


def _srt(lines: list[str]) -> str:
    blocks = []
    for i, text in enumerate(lines, 1):
        blocks.append(f"{i}\n00:00:{i:02d},000 --> 00:00:{i+1:02d},000\n{text}\n")
    return "\n".join(blocks)


# Three episodes with clearly distinct dialogue.
EP_LINES = {
    10: [f"The reactor core is overheating fast {i}" for i in range(40)],
    11: [f"She walked along the quiet beach alone {i}" for i in range(40)],
    12: [f"They argued about money at the dinner {i}" for i in range(40)],
}
REFS = {ep: _srt(lines) for ep, lines in EP_LINES.items()}


def _ocr_noise(lines: list[str]) -> list[str]:
    # Simulate OCR: swap some letters / drop a word here and there.
    out = []
    for k, ln in enumerate(lines):
        if k % 5 == 0:
            ln = ln.replace("e", "c", 1)  # a single-char OCR slip
        if k % 7 == 0:
            ln = " ".join(ln.split()[:-1])  # drop trailing token
        out.append(ln)
    return out


def test_normalize_strips_cues_and_labels():
    words = normalize_dialogue(_srt(["MAN: Run! [explosion]", "<i>Hello there.</i>"]))
    assert "explosion" not in words and "man" not in words
    assert "hello" in words and "run" in words


def test_similarity_high_for_same_episode_low_for_different():
    same = similarity(REFS[10], _srt(_ocr_noise(EP_LINES[10])))
    diff = similarity(REFS[10], REFS[11])
    assert same > 0.3
    assert diff < 0.05
    assert same > diff * 5


def test_identify_recovers_scrambled_order():
    # Titles come off the disc in a scrambled order vs episode number.
    rips = {
        0: _srt(_ocr_noise(EP_LINES[12])),  # title 0 is actually episode 12
        1: _srt(_ocr_noise(EP_LINES[10])),  # title 1 is episode 10
        2: _srt(_ocr_noise(EP_LINES[11])),  # title 2 is episode 11
    }
    matches = {m.title_id: m for m in identify(rips, REFS)}
    assert matches[0].episode == 12 and matches[0].confident
    assert matches[1].episode == 10 and matches[1].confident
    assert matches[2].episode == 11 and matches[2].confident


def test_identify_is_a_permutation():
    rips = {i: _srt(_ocr_noise(EP_LINES[ep])) for i, ep in enumerate([10, 11, 12])}
    eps = [m.episode for m in identify(rips, REFS)]
    assert sorted(eps) == [10, 11, 12]  # no episode used twice


def test_indistinguishable_episodes_flagged_low_confidence():
    # Two reference episodes with near-identical dialogue -> not confident.
    refs = {1: _srt(["the same line repeated"] * 40), 2: _srt(["the same line repeated"] * 40)}
    rips = {0: _srt(["the same line repeated"] * 40), 1: _srt(["the same line repeated"] * 40)}
    matches = identify(rips, refs)
    assert all(not m.confident for m in matches)
