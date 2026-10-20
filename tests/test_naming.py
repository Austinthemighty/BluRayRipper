from pathlib import Path

from blurayripper.services import naming


def test_sanitize_illegal_chars():
    assert naming.sanitize('A: B/C?<>|*"D') == "A BCD"
    assert naming.sanitize("trailing dot.") == "trailing dot"
    assert naming.sanitize("   ") == "Untitled"


def test_sanitize_reserved():
    assert naming.sanitize("CON") == "_CON"
    assert naming.sanitize("com1") == "_com1"


def test_movie_path():
    p = naming.movie_video_path(Path("/out/Movies"), "Blade Runner", 1982, "mp4")
    assert p == Path("/out/Movies/Blade Runner (1982)/Blade Runner (1982).mp4")


def test_movie_path_with_edition():
    p = naming.movie_video_path(Path("/out/Movies"), "Blade Runner", 1982, "mkv", "Final Cut")
    assert p == Path("/out/Movies/Blade Runner (1982)/Blade Runner (1982) {edition-Final Cut}.mkv")


def test_movie_path_blank_edition_ignored():
    p = naming.movie_video_path(Path("/out/Movies"), "Blade Runner", 1982, "mkv", "  ")
    assert p == Path("/out/Movies/Blade Runner (1982)/Blade Runner (1982).mkv")


def test_movie_path_no_year():
    p = naming.movie_video_path(Path("/out/Movies"), "Mystery", None, "mkv")
    assert p == Path("/out/Movies/Mystery/Mystery.mkv")


def test_episode_path():
    p = naming.episode_video_path(Path("/out/TV"), "The Show", 2010, 1, 9, "mp4")
    assert p == Path("/out/TV/The Show (2010)/Season 01/The Show - S01E09.mp4")


def test_subtitle_sidecar_default_and_nondefault():
    video = Path("/out/TV/The Show (2010)/Season 01/The Show - S01E09.mp4")
    en = naming.subtitle_sidecar_path(video, "eng", default=True)
    pl = naming.subtitle_sidecar_path(video, "pol", default=False)
    assert en.name == "The Show - S01E09.en.default.srt"
    assert pl.name == "The Show - S01E09.pl.srt"


def test_subtitle_sidecar_forced_sdh_and_index_dont_collide():
    video = Path("/out/Movies/WarGames (1983)/WarGames (1983).mkv")
    main = naming.subtitle_sidecar_path(video, "eng", default=True)
    forced = naming.subtitle_sidecar_path(video, "eng", forced=True)
    sdh = naming.subtitle_sidecar_path(video, "eng", sdh=True)
    extra = naming.subtitle_sidecar_path(video, "eng", index=2)
    names = {main.name, forced.name, sdh.name, extra.name}
    assert names == {
        "WarGames (1983).en.default.srt",
        "WarGames (1983).en.forced.srt",
        "WarGames (1983).en.sdh.srt",
        "WarGames (1983).en.2.srt",
    }
    assert len(names) == 4  # all distinct


def test_subtitle_lang_suffix_fallback():
    assert naming.subtitle_lang_suffix("eng") == "en"
    assert naming.subtitle_lang_suffix("xyz") == "xyz"  # unknown -> passthrough


def test_clean_tmdb_query_strips_tv_disc_noise():
    from blurayripper.services.naming import clean_tmdb_query
    assert clean_tmdb_query("Silicon Valley Season 2 Disc 1", "tv") == ("Silicon Valley", None, 2)
    assert clean_tmdb_query("Game of Thrones S01 Disc 3", "tv") == ("Game of Thrones", None, 1)
    assert clean_tmdb_query("Friends Season 10 Volume 2", "tv") == ("Friends", None, 10)
    assert clean_tmdb_query("The Office Complete Series Disc 5", "tv") == ("The Office", None, None)
    # Year anywhere in the string is pulled out, not just trailing.
    assert clean_tmdb_query("Breaking Bad (2008) Season 4 Disc 2", "tv") == ("Breaking Bad", 2008, 4)
    # A clean show name is left alone.
    assert clean_tmdb_query("Silicon Valley", "tv") == ("Silicon Valley", None, None)


def test_clean_tmdb_query_preserves_movie_titles():
    from blurayripper.services.naming import clean_tmdb_query
    # "Vol"/"Part" are real movie-title words — must NOT be stripped.
    assert clean_tmdb_query("Guardians of the Galaxy Vol. 3", "movie") == (
        "Guardians of the Galaxy Vol. 3", None, None)
    assert clean_tmdb_query("Kill Bill: Vol. 1", "movie") == ("Kill Bill: Vol. 1", None, None)
    assert clean_tmdb_query("Dune: Part Two (2024)", "movie") == ("Dune: Part Two", 2024, None)
    assert clean_tmdb_query("WarGames (1983)", "movie") == ("WarGames", 1983, None)
    # A trailing number that isn't a descriptor stays put.
    assert clean_tmdb_query("Ocean's 11", "movie") == ("Ocean's 11", None, None)


def test_clean_tmdb_query_handles_underscore_disc_labels():
    """ISO9660 labels can't hold spaces, so DVDs ship "HAWAII_FIVE_0_S1_D4".
    Underscores are word chars, so every \\b failed and nothing was detected —
    the raw label even went to TMDB as the search query."""
    from blurayripper.services.naming import clean_tmdb_query

    assert clean_tmdb_query("HAWAII_FIVE_0_S1_D4", "tv") == ("HAWAII FIVE 0", None, 1)
    assert clean_tmdb_query("HAWAII_FIVE_0_S01_D04", "tv") == ("HAWAII FIVE 0", None, 1)
    assert clean_tmdb_query("THE_OFFICE_S3_D2", "tv") == ("THE OFFICE", None, 3)
    # Dot-separated (no spaces anywhere) is the same shape.
    assert clean_tmdb_query("Breaking.Bad.S04.D2", "tv") == ("Breaking Bad", None, 4)
    # Spaced labels keep working.
    assert clean_tmdb_query("Silicon Valley Season 2 Disc 1", "tv") == ("Silicon Valley", None, 2)


def test_clean_tmdb_query_keeps_real_punctuation_in_titles():
    """Dots are only separators when the label has no spaces — a real title's
    punctuation must survive, or the TMDB search gets worse, not better."""
    from blurayripper.services.naming import clean_tmdb_query

    assert clean_tmdb_query("Mrs. Doubtfire", "movie")[0] == "Mrs. Doubtfire"
    assert clean_tmdb_query("Kill Bill: Vol. 1", "movie")[0] == "Kill Bill: Vol. 1"
    assert clean_tmdb_query("Guardians of the Galaxy Vol. 3", "movie")[0] == \
        "Guardians of the Galaxy Vol. 3"


def test_disc_number_from_label():
    from blurayripper.services.naming import disc_number

    assert disc_number("HAWAII_FIVE_0_S1_D4") == 4
    assert disc_number("HAWAII_FIVE_0_S01_D04") == 4
    assert disc_number("Silicon Valley Season 2 Disc 1") == 1
    assert disc_number("Breaking.Bad.S04.D2") == 2
    assert disc_number("SOME_MOVIE") is None
    assert disc_number("") is None
    # A trailing number that isn't a disc descriptor must not be read as one.
    assert disc_number("OCEANS_11") is None
    assert disc_number("WALL_E") is None


def test_episode_span_guesses_the_block_a_disc_holds():
    from blurayripper.services.naming import episode_span

    assert episode_span(1, 4) == (1, 4)      # first disc starts at E01
    assert episode_span(4, 4) == (13, 16)    # disc 4 of 4-per-disc
    assert episode_span(2, 3) == (4, 6)
    assert episode_span(None, 4) is None     # unknown disc -> no guess
    assert episode_span(0, 4) is None
    assert episode_span(3, 0) is None        # no titles -> nothing to span


def test_strips_the_ntsc_pal_video_standard_from_disc_labels():
    """DVDs stamp the video standard on the label (HAWAII_FIVE_0_S4_D5_NTSC).
    It blocked the whole tail-stripping loop, which only peels from the END — so
    an unrecognised trailing token left "S4 D5" on the title too."""
    from blurayripper.services.naming import clean_tmdb_query, disc_number

    assert clean_tmdb_query("HAWAII_FIVE_0_S4_D5_NTSC", "tv") == ("HAWAII FIVE 0", None, 4)
    assert clean_tmdb_query("HAWAII_FIVE_0_S4_D5_PAL", "tv") == ("HAWAII FIVE 0", None, 4)
    assert clean_tmdb_query("THE_OFFICE_S3_D2_NTSC", "tv")[0] == "THE OFFICE"
    assert disc_number("HAWAII_FIVE_0_S4_D5_NTSC") == 5      # still finds the disc

    # Movie discs carry it too.
    assert clean_tmdb_query("SOME_MOVIE_NTSC", "movie")[0] == "SOME MOVIE"
    assert clean_tmdb_query("SOME_MOVIE_PAL", "movie")[0] == "SOME MOVIE"


def test_ntsc_pal_stripping_does_not_eat_real_titles():
    """Only a TRAILING standard is peeled, and a label that is nothing else keeps
    its text — so the common cases can't be destroyed by the rule above."""
    from blurayripper.services.naming import clean_tmdb_query

    assert clean_tmdb_query("Pal Joey", "movie")[0] == "Pal Joey"   # not trailing
    assert clean_tmdb_query("PAL", "movie")[0] == "PAL"             # nothing left -> keep
    assert clean_tmdb_query("Kill Bill: Vol. 1", "movie")[0] == "Kill Bill: Vol. 1"


def test_quoted_query_is_searched_exactly():
    """The escape hatch for a title the cleaning would damage: quote it and nothing
    is peeled or rewritten."""
    from blurayripper.services.naming import clean_tmdb_query

    assert clean_tmdb_query('"my pal"', "movie") == ("my pal", None, None)
    assert clean_tmdb_query('  "My Pal"  ', "movie")[0] == "My Pal"      # outer space ok
    # Nothing is stripped inside the quotes — that's the whole point.
    assert clean_tmdb_query('"Silicon Valley Season 2"', "tv")[0] == "Silicon Valley Season 2"
    assert clean_tmdb_query('"HAWAII_FIVE_0_S4_D5_NTSC"', "tv")[0] == "HAWAII_FIVE_0_S4_D5_NTSC"
    assert clean_tmdb_query('"Kill Bill: Vol. 1"', "tv")[0] == "Kill Bill: Vol. 1"


def test_quoting_rescues_a_title_the_cleaner_would_damage():
    """Unquoted, a trailing "Pal" is treated as the PAL video standard. Quoting is
    how you say "no, that's the title"."""
    from blurayripper.services.naming import clean_tmdb_query

    assert clean_tmdb_query("My Pal", "movie")[0] == "My"       # stripped as noise
    assert clean_tmdb_query('"My Pal"', "movie")[0] == "My Pal"  # ...unless quoted


def test_curly_quotes_work_too():
    """Phones autocorrect " into “ ”; a search typed on one must behave the same."""
    from blurayripper.services.naming import clean_tmdb_query

    assert clean_tmdb_query('“my pal”', "movie") == ("my pal", None, None)


def test_only_a_fully_wrapped_query_counts_as_quoted():
    """A stray or embedded quote must not turn cleaning off by accident, and an
    apostrophe-led title can't be mistaken for a quoted one."""
    from blurayripper.services.naming import clean_tmdb_query

    assert clean_tmdb_query("'Salem's Lot", "tv")[0] == "'Salem's Lot"   # single quotes
    assert clean_tmdb_query('He said "hi" loudly', "tv")[0] == 'He said "hi" loudly'
    # Unclosed quote -> normal cleaning still applies.
    assert clean_tmdb_query('"Breaking Bad Season 4', "tv")[2] == 4


def test_a_spelled_out_season_in_brackets_still_finds_the_show():
    """"Landman: Season Two (Disc 1)" is a real MakeMKV disc name, and TMDB
    returned nothing for it until it was retyped as "Landman" by hand.

    Two separate gaps, and the first one caused the second. The tail stripper
    only peels from the END, so the ")" of "(Disc 1)" stopped it dead — and with
    the disc descriptor still attached, nothing behind it could be peeled
    either. Then "Season Two" would have survived anyway, because only digits
    were accepted after "Season"."""
    from blurayripper.services.naming import clean_tmdb_query

    assert clean_tmdb_query("Landman: Season Two (Disc 1)", "tv") == ("Landman", None, 2)
    assert clean_tmdb_query("Landman: Season Two", "tv") == ("Landman", None, 2)
    assert clean_tmdb_query("Landman [Disc 1]", "tv") == ("Landman", None, None)

    # The season comes back too, so the naming form stops needing it typed for
    # every disc of the set.
    assert clean_tmdb_query("House: The Complete Fifth Season Disc 2", "tv") \
        == ("House", None, 5)
    assert clean_tmdb_query("Fargo Season Three", "tv") == ("Fargo", None, 3)

    # Still no false positives: a number word is only noise after a descriptor.
    assert clean_tmdb_query("Ocean's Eleven", "movie") == ("Ocean's Eleven", None, None)
    assert clean_tmdb_query("Mission: Impossible", "movie") \
        == ("Mission: Impossible", None, None)
    assert clean_tmdb_query("Kill Bill: Vol. 1", "movie") \
        == ("Kill Bill: Vol. 1", None, None)
    # Stripping everything leaves the original rather than an empty search.
    assert clean_tmdb_query("The Complete Second Season", "tv")[0] \
        == "The Complete Second Season"
