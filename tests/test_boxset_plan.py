"""Box-set plan: parsing the CSV, matching disc labels, and inferring episodes."""

from blurayripper.boxset_plan import assignments_for, match, parse_csv
from blurayripper.models import DiscInfo, MediaKind, Title

SAMPLE = """\
disc_label,show,year,season,first_episode,episodes
HAWAII_FIVE_0_S1_*,Hawaii Five-0,2010,,,
THE_OFFICE_S3_D2,The Office,2005,3,9,4
"""


def _disc(label, n=4, seconds=2700):
    """A disc with n episode-length titles (the 18-75 min window)."""
    return DiscInfo(
        source="disc:0", disc_name=label, volume_label=label,
        titles=[Title(title_id=i, duration_seconds=seconds, chapter_count=6,
                      source_filename=f"{i:05d}.mpls") for i in range(n)],
    )


def test_parse_sample_csv():
    rows, errors = parse_csv(SAMPLE)
    assert errors == []
    assert [r.disc_label for r in rows] == ["HAWAII_FIVE_0_S1_*", "THE_OFFICE_S3_D2"]
    assert rows[0].show == "Hawaii Five-0" and rows[0].year == 2010
    assert rows[0].season is None            # blank -> inferred from each disc label
    assert rows[1].season == 3 and rows[1].first_episode == 9 and rows[1].episodes == 4


def test_parse_reports_bad_lines_without_raising():
    """One malformed line must not stop a long box-set file from loading."""
    rows, errors = parse_csv(
        "disc_label,show,season\n"
        "GOOD_S1_D1,Good Show,1\n"
        ",Missing Label,1\n"
        "NO_SHOW_S1_D1,,1\n"
        "BAD_SEASON_S1_D1,Bad Season,two\n"
    )
    assert [r.show for r in rows] == ["Good Show", "Bad Season"]   # the rest survive
    assert any("disc_label is required" in e for e in errors)
    assert any("show is required" in e for e in errors)
    assert any("season must be a whole number" in e for e in errors)
    assert rows[1].season is None            # the junk cell is dropped, not fatal


def test_parse_requires_a_header():
    _rows, errors = parse_csv("HAWAII_S1_D1,Hawaii Five-0\n")
    assert errors and "disc_label" in errors[0]


def test_parse_flags_duplicates_and_empty_text():
    rows, errors = parse_csv(
        "disc_label,show\nA_S1_D1,One\nA_S1_D1,Two\n")
    assert [r.show for r in rows] == ["One"]                  # first wins
    assert any("duplicate" in e for e in errors)
    assert parse_csv("") == ([], [])
    assert parse_csv("   \n") == ([], [])


def test_match_prefers_exact_over_wildcard():
    rows, _ = parse_csv(SAMPLE + "HAWAII_FIVE_0_S1_D4,Hawaii Five-0 Bonus,2010,1,99,\n")
    assert match(rows, "HAWAII_FIVE_0_S1_D4").show == "Hawaii Five-0 Bonus"
    assert match(rows, "HAWAII_FIVE_0_S1_D2").show == "Hawaii Five-0"   # wildcard
    assert match(rows, "SOME_OTHER_DISC") is None
    assert match(rows, "") is None
    assert match([], "HAWAII_FIVE_0_S1_D4") is None


def test_match_ignores_separator_style_and_case():
    """A row typed with spaces still matches the disc's underscored volume label."""
    rows, _ = parse_csv("disc_label,show\nHawaii Five-0 S1 D4,Hawaii Five-0\n")
    assert match(rows, "HAWAII_FIVE_0_S1_D4") is not None
    assert match(rows, "hawaii five 0 s1 d4") is not None


def test_match_longest_wildcard_wins():
    rows, _ = parse_csv(
        "disc_label,show\nHAWAII_*,Broad\nHAWAII_FIVE_0_S1_*,Specific\n")
    assert match(rows, "HAWAII_FIVE_0_S1_D4").show == "Specific"


def test_assignments_infer_season_and_episodes_from_the_label():
    """The common case: one wildcard row covers a whole season, because each disc's
    label states its own season and disc number."""
    rows, _ = parse_csv(SAMPLE)
    row = match(rows, "HAWAII_FIVE_0_S1_D4")
    m = assignments_for(row, _disc("HAWAII_FIVE_0_S1_D4", n=4))

    assert m.ok and m.season == 1 and m.first_episode == 13   # disc 4 x 4 episodes
    assert [(a.title_id, a.episode) for a in m.assignments] == \
        [(0, 13), (1, 14), (2, 15), (3, 16)]
    assert all(a.kind is MediaKind.TV for a in m.assignments)
    assert all(a.show_or_movie_title == "Hawaii Five-0" and a.year == 2010
               for a in m.assignments)
    assert all(a.season == 1 for a in m.assignments)


def test_assignments_use_explicit_columns_over_inference():
    rows, _ = parse_csv(SAMPLE)
    row = match(rows, "THE_OFFICE_S3_D2")
    m = assignments_for(row, _disc("THE_OFFICE_S3_D2", n=4))
    assert m.ok and m.season == 3
    assert [a.episode for a in m.assignments] == [9, 10, 11, 12]   # not disc-derived


def test_assignments_refuse_on_episode_count_mismatch():
    """The safety valve: a disc with an unexpected title count (a bonus feature in
    the episode-length window) must park rather than shift every episode number."""
    rows, _ = parse_csv(SAMPLE)
    row = match(rows, "THE_OFFICE_S3_D2")           # declares episodes=4
    m = assignments_for(row, _disc("THE_OFFICE_S3_D2", n=5))
    assert not m.ok and "5" in m.reason and not m.assignments


def test_assignments_refuse_when_nothing_says_the_season():
    rows, _ = parse_csv("disc_label,show\nSOME_BOXSET_D2,Some Show\n")
    m = assignments_for(rows[0], _disc("SOME_BOXSET_D2", n=3))
    assert not m.ok and "season" in m.reason


def test_assignments_refuse_without_a_first_episode():
    """Season known, but no disc number and no first_episode -> can't number them."""
    rows, _ = parse_csv("disc_label,show,season\nSOME_BOXSET_S2,Some Show,2\n")
    m = assignments_for(rows[0], _disc("SOME_BOXSET_S2", n=3))
    assert not m.ok and "first episode" in m.reason


def test_assignments_refuse_when_no_episode_length_titles():
    rows, _ = parse_csv(SAMPLE)
    row = match(rows, "HAWAII_FIVE_0_S1_D4")
    movie_ish = _disc("HAWAII_FIVE_0_S1_D4", n=1, seconds=7200)   # 2h, outside window
    m = assignments_for(row, movie_ish)
    assert not m.ok and "episode-length" in m.reason
