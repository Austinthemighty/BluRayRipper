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
