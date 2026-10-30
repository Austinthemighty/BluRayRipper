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


# --- a two-parter mastered as one title --------------------------------------------

from blurayripper.services.sub_match import identify_two_parts  # noqa: E402

EP_LINES[13] = [f"The old lighthouse keeper sang at night {i}" for i in range(40)]
REFS[13] = _srt(EP_LINES[13])


def test_a_title_holding_two_episodes_matches_the_pair():
    """HIMYM S8 D2's "The Final Page" is E11+E12 in one title. Against either
    reference alone it scores about half, so one-episode matching never trusted
    it. On the real captions: 0.825 for the pair against 0.496 for E11 alone."""
    rips = {5: _srt(_ocr_noise(EP_LINES[11] + EP_LINES[12]))}

    (m,) = identify_two_parts(rips, REFS, {5})

    assert (m.title_id, m.episode, m.episode_end, m.confident) == (5, 11, 12, True)
    assert m.score - m.runner_up_score >= 0.04


def test_a_long_single_episode_is_not_mistaken_for_a_pair():
    """Twice the usual length is only a hint. A long episode scores best on its
    own, and must be left to the ordinary matching."""
    rips = {5: _srt(EP_LINES[12])}
    assert identify_two_parts(rips, REFS, {5}) == []
    # At the end of the range only one pair can contain it, so no other pair
    # competes — the title's own single-episode score is what says no.
    assert identify_two_parts({5: _srt(EP_LINES[13])}, REFS, {5}) == []
    assert identify_two_parts({5: _srt(EP_LINES[11] + EP_LINES[12])}, REFS, set()) == []


# --- judge: name it, skip it, or ask ---------------------------------------------------

from blurayripper.services.sub_match import (  # noqa: E402
    MIN_DIALOGUE, Match, judge, score_matrix,
)

TALKS = MIN_DIALOGUE        # enough dialogue to say a title matches nothing


def _sure(tid, ep, score=0.6, end=None):
    return Match(tid, ep, score, None, 0.02, True, episode_end=end)


def _unsure(tid, ep, score=0.3, runner=None, runner_score=0.28):
    return Match(tid, ep, score, runner, runner_score, False)


def _disc_of_three_and_an_extra(extra_best=0.03):
    """Titles 0-2 are E4-E6, matched surely; title 9 is an extra."""
    matches = [_sure(0, 4), _sure(1, 5), _sure(2, 6), _unsure(9, 7, extra_best, 4, 0.01)]
    scores = {0: {4: 0.6, 5: 0.01, 6: 0.01, 7: 0.0}, 1: {4: 0.0, 5: 0.6, 6: 0.0, 7: 0.0},
              2: {4: 0.0, 5: 0.0, 6: 0.6, 7: 0.0}, 9: {4: 0.01, 5: 0.0, 6: 0.0, 7: extra_best}}
    return [0, 1, 2, 9], matches, scores


def _judge(wanted, matches, scores, **kw):
    kw.setdefault("dialogue", {t: 5 * TALKS for t in wanted})
    return judge(wanted, matches, scores, season=1, **kw)


def test_an_extra_that_matches_nothing_is_skipped_beside_sure_episodes():
    v = _judge(*_disc_of_three_and_an_extra(), skip_unmatched=True)
    assert v.clean
    assert [m.title_id for m in v.named] == [0, 1, 2]
    assert v.skipped == {9: "matches no episode of season 1 (best E07 at 0.03, under 0.10)"}


def test_a_score_of_exactly_the_minimum_is_a_match_not_nothing():
    """0.10 is where a match starts counting; a title right there resembles
    something and has to be looked at, not thrown away."""
    v = _judge(*_disc_of_three_and_an_extra(extra_best=0.10), skip_unmatched=True)
    assert 9 in v.unsure and not v.skipped and not v.clean
    v = _judge(*_disc_of_three_and_an_extra(extra_best=0.0999), skip_unmatched=True)
    assert 9 in v.skipped and v.clean


def test_nothing_is_skipped_while_another_title_is_unsure():
    wanted, matches, scores = _disc_of_three_and_an_extra()
    matches[1] = _unsure(1, 5, 0.31, 6, 0.29)
    v = _judge(wanted, matches, scores, skip_unmatched=True)
    assert not v.clean and not v.skipped
    assert v.propose_skip == {9}
    assert v.unsure[9].endswith("— not skipped while title 1 is unsure")


def test_nothing_is_skipped_while_a_candidate_episode_has_no_reference():
    """The extra could be the very episode OpenSubtitles had nothing for (or
    that the quota stopped short of)."""
    v = _judge(*_disc_of_three_and_an_extra(), skip_unmatched=True, missing=[11, 12])
    assert not v.clean and v.propose_skip == {9}
    assert "OpenSubtitles has no reference for E11, E12" in v.unsure[9]


def test_outside_box_set_mode_an_unmatched_title_is_asked_about():
    """Picked by hand, against episodes picked by hand: a title matching
    none of them is likelier a wrong pick than an extra."""
    v = _judge(*_disc_of_three_and_an_extra())
    assert not v.clean and v.propose_skip == {9}
    assert v.unsure[9].endswith("— an extra?")


def test_when_no_title_matches_anything_it_asks_and_proposes_no_skips():
    matches = [_unsure(0, 1, 0.02), _unsure(1, 2, 0.01)]
    scores = {0: {1: 0.02, 2: 0.01}, 1: {1: 0.0, 2: 0.01}}
    v = _judge([0, 1], matches, scores, skip_unmatched=True)
    assert not v.clean and not v.skipped and not v.propose_skip
    assert "the wrong season, or a disc of extras?" in v.unsure[0]


def test_a_title_with_too_little_dialogue_is_never_called_an_extra():
    """A failed OCR reads exactly like an extra: almost no overlap with
    anything. Skipping it would lose an episode."""
    wanted, matches, scores = _disc_of_three_and_an_extra()
    v = _judge(wanted, matches, scores, skip_unmatched=True,
               dialogue={0: TALKS, 1: TALKS, 2: TALKS, 9: TALKS - 1})
    assert v.unsure[9] == "too little dialogue to tell" and not v.skipped


def test_two_extras_are_both_skipped():
    wanted, matches, scores = _disc_of_three_and_an_extra()
    matches.append(_unsure(8, 7, 0.02, 4, 0.0))
    scores[8] = {4: 0.0, 5: 0.0, 6: 0.0, 7: 0.02}
    v = _judge(wanted + [8], matches, scores, skip_unmatched=True)
    assert set(v.skipped) == {8, 9} and v.clean


def test_an_episode_another_job_already_names_is_asked_about_not_replaced():
    """A re-rip of a damaged disc, or two discs labelled alike: either way a
    file in the library would be replaced without anyone saying so."""
    wanted, matches, scores = _disc_of_three_and_an_extra()
    v = _judge(wanted, matches, scores, skip_unmatched=True, taken_elsewhere={5: 412})
    assert not v.clean
    assert v.unsure[1] == "E05 (0.60), but job #412 already names E05 — asking rather than replace it"
    assert [m.title_id for m in v.named] == [0, 2]


def test_a_title_closest_to_an_episode_another_title_took_says_so():
    """An alternate cut of the finale resembles the finale. That is a question
    for you, never an extra to throw away."""
    matches = [_sure(0, 21), _sure(1, 22), _unsure(2, 20, 0.05, 22, 0.62)]
    scores = {0: {20: 0.0, 21: 0.7, 22: 0.0}, 1: {20: 0.0, 21: 0.0, 22: 0.81},
              2: {20: 0.05, 21: 0.0, 22: 0.62}}
    v = _judge([0, 1, 2], matches, scores, skip_unmatched=True)
    assert v.unsure[2] == "closest to E22 (0.62), which title 1 took (0.60) — an alternate cut?"
    assert not v.skipped and not v.clean


def test_a_title_with_no_subtitles_is_asked_about():
    wanted, matches, scores = _disc_of_three_and_an_extra()
    v = _judge(wanted, [m for m in matches if m.title_id != 9],
               {t: r for t, r in scores.items() if t != 9}, skip_unmatched=True)
    assert v.unsure == {9: "no subtitles to match"}


def test_the_score_matrix_is_what_identify_matches_on():
    refs = REFS
    rips = {0: _srt(EP_LINES[11]), 1: _srt(_ocr_noise(EP_LINES[10]))}
    scores = score_matrix(rips, refs)
    for m in identify(rips, refs):
        assert m.score == scores[m.title_id][m.episode]


def test_a_disc_with_nothing_named_is_never_clean():
    """Clean means "name these and go on". With nothing to name that would
    send a job on with no titles at all."""
    assert judge([], [], {}, dialogue={}).clean is False


def test_every_extra_gives_the_real_reason_it_was_not_skipped():
    wanted, matches, scores = _disc_of_three_and_an_extra()
    matches.append(_unsure(8, 7, 0.02, 4, 0.0))
    scores[8] = {4: 0.0, 5: 0.0, 6: 0.0, 7: 0.02}
    v = _judge(wanted + [8], matches, scores, skip_unmatched=True, missing=[12])
    assert all("no reference for E12" in v.unsure[t] for t in (8, 9))


def test_with_no_references_at_all_a_title_is_asked_about():
    v = _judge([0], [Match(0, None, 0.0, None, 0.0, False)], {0: {}}, skip_unmatched=True)
    assert v.unsure == {0: "no reference subtitles to match against"}
