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


def test_subtitle_lang_suffix_fallback():
    assert naming.subtitle_lang_suffix("eng") == "en"
    assert naming.subtitle_lang_suffix("xyz") == "xyz"  # unknown -> passthrough
