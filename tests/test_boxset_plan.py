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
    # The label is optional now, but a row still has to be findable BY
    # something: this one gives no label, no disc number and no collection
    # number, so nothing could ever look it up.
    assert any("nothing identifies it" in e for e in errors)
    assert any("show is required" in e for e in errors)
    assert any("season must be a whole number" in e for e in errors)
    assert rows[1].season is None            # the junk cell is dropped, not fatal


def test_parse_requires_a_header():
    """Without one, the first row of data is read AS the header and silently
    disappears. "show" is the one column every row must have, so its absence is
    what catches this."""
    _rows, errors = parse_csv("HAWAII_S1_D1,Hawaii Five-0\n")
    assert errors and "show" in errors[0] and "Missing required column" in errors[0]


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


# --- a box set whose discs are all labelled alike ----------------------------

# A real spreadsheet, headers and spelling as typed. Every disc of this set
# reports the same volume label, so the "Disk name" column is empty throughout —
# which is exactly the case the plan could not express before.
HIMYM_CSV = """Disk name,Show,Season,Disk,Disk collection number,Episodes,Year
,How I Met Your Mother,1,1,1,8,2005
,How I Met Your Mother,1,2,2,8,2005
,How I Met Your Mother,1,3,3,6,2005
,How I Met Your Mother,2,1,4,8,2005
,How I Met Your Mother,2,2,5,8,2005
,How I Met Your Mother,2,3,6,6,2005
,How I Met Your Mother,3,1,7,6,2005
,How I Met Your Mother,3,2,8,7,2005
,How I Met Your Mother,3,3,9,7,2005
"""


def test_a_spreadsheets_own_headers_and_spelling_are_accepted():
    """"Disk name", "Disk collection number" — people type these by hand, and a
    header this file doesn't recognise has its whole column silently ignored.
    Making someone rename columns to match an internal schema is a bad trade for
    a file they already maintain."""
    rows, errors = parse_csv(HIMYM_CSV)
    assert errors == []
    assert len(rows) == 9
    assert rows[0].show == "How I Met Your Mother"
    assert (rows[0].season, rows[0].disc, rows[0].collection) == (1, 1, 1)
    assert rows[0].year == 2005 and rows[0].episodes == 8
    assert all(r.disc_label == "" for r in rows)      # no labels anywhere


def test_each_discs_episode_range_is_summed_from_the_discs_before_it():
    """What you can see on the case is how many episodes a disc holds. Where it
    STARTS is arithmetic — season 1 disc 3 begins at episode 17 because discs 1
    and 2 held eight each — and doing that by hand for 28 discs is the job this
    file exists to avoid."""
    rows, _ = parse_csv(HIMYM_CSV)
    ranges = [(r.season, r.disc, r.first_episode, r.last_episode) for r in rows]
    assert ranges == [
        (1, 1, 1, 8), (1, 2, 9, 16), (1, 3, 17, 22),
        (2, 1, 1, 8), (2, 2, 9, 16), (2, 3, 17, 22),   # each season restarts at 1
        (3, 1, 1, 6), (3, 2, 7, 13), (3, 3, 14, 20),
    ]


def test_a_stated_first_episode_is_a_correction_and_is_never_overwritten():
    """A set with a double-length opener, or a disc out of order, needs to say
    so — and saying so must win over the arithmetic."""
    rows, errors = parse_csv(
        "show,season,disc,episodes,first_episode\n"
        "Lost,1,1,2,1\n"        # a two-part pilot counted as one episode slot
        "Lost,1,2,6,4\n"        # stated: starts at 4, not 3
        "Lost,1,3,6,\n"         # derived, continuing from the stated row
    )
    assert errors == []
    assert [(r.disc, r.first_episode, r.last_episode) for r in rows] == [
        (1, 1, 2), (2, 4, 9), (3, 10, 15)]


def test_a_missing_count_stops_the_arithmetic_rather_than_guessing_past_it():
    """Deriving disc 3 from disc 1 while disc 2's count is unknown would misname
    a whole disc, silently and plausibly."""
    rows, _ = parse_csv(
        "show,season,disc,episodes\n"
        "Gap,1,1,8\n"
        "Gap,1,2,\n"            # unknown
        "Gap,1,3,6\n"
    )
    assert rows[0].first_episode == 1
    assert rows[1].first_episode == 9       # still placeable: disc 1 is known
    assert rows[2].first_episode is None    # ...but nothing after the gap is


def test_a_disc_is_found_by_its_number_when_no_label_can_find_it():
    """The lookup the naming form needs: all 28 discs print one label, so the
    only way to ask "what is on this one?" is by its number."""
    import blurayripper.boxset_plan as bp

    rows, _ = parse_csv(HIMYM_CSV)
    row = bp.find(rows, "How I Met Your Mother", season=3, disc=2)
    assert (row.first_episode, row.last_episode, row.collection) == (7, 13, 8)

    # A collection number identifies a disc on its own, with no season needed.
    by_set = bp.find(rows, "How I Met Your Mother", collection=8)
    assert by_set is row

    # Case and punctuation in the show name must not lose the set.
    assert bp.find(rows, "how i met your mother", season=1, disc=1).collection == 1
    assert bp.find(rows, "Some Other Show", season=1, disc=1) is None
    assert bp.find(rows, "How I Met Your Mother", season=9, disc=1) is None


def test_a_label_that_states_the_season_and_disc_needs_no_disc_label_column():
    """"HOW_I_MET_YOUR_MOTHER_S9_D1" already says which row it wants: strip the
    S9 and D1 and what remains is the show, which is the column the plan is
    keyed on anyway. Requiring a disc_label as well means filling that column in
    28 times — having first inserted each disc to find out what it says."""
    import blurayripper.boxset_plan as bp

    rows, errors = parse_csv(
        "Disk name,Show,Season,Disk,Disk collection number,Episodes,Year\n"
        ",How I Met Your Mother,9,1,25,8,2005\n"
        ",How I Met Your Mother,9,2,26,8,2005\n")
    assert errors == []

    first = bp.match_label_parts(rows, "HOW_I_MET_YOUR_MOTHER_S9_D1")
    assert (first.season, first.disc) == (9, 1)
    assert (first.first_episode, first.last_episode) == (1, 8)

    second = bp.match_label_parts(rows, "HOW_I_MET_YOUR_MOTHER_S9_D2")
    assert (second.first_episode, second.last_episode) == (9, 16)

    # Punctuation and case in the label must not lose the show.
    assert bp.match_label_parts(rows, "how.i.met.your.mother.s9.d1") is first
    assert bp.match_label_parts(rows, "How I Met Your Mother S9 D1") is first


def test_a_label_missing_the_disc_number_is_not_guessed_at():
    """A label that states only the season cannot pick between that season's
    discs, and picking wrong names a whole disc's worth of episodes as the wrong
    ones — silently, because every part of it looks plausible."""
    import blurayripper.boxset_plan as bp

    rows, _ = parse_csv(
        "show,season,disc,episodes\n"
        "How I Met Your Mother,9,1,8\n"
        "How I Met Your Mother,9,2,8\n")

    assert bp.match_label_parts(rows, "HOW_I_MET_YOUR_MOTHER_S9") is None
    assert bp.match_label_parts(rows, "HOW_I_MET_YOUR_MOTHER") is None
    assert bp.match_label_parts(rows, "") is None
    # A show the plan has never heard of stays unmatched rather than guessing.
    assert bp.match_label_parts(rows, "THE_WIRE_S1_D1") is None
