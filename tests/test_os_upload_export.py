"""OpenSubtitles manual-upload export: info sheet + folder layout."""

from blurayripper.models import EpisodeAssignment, MediaKind
from blurayripper.services import os_upload


def _movie():
    return EpisodeAssignment(0, MediaKind.MOVIE, "The Film", 1999)


def _episode():
    return EpisodeAssignment(0, MediaKind.TV, "The Show", 2015, season=2, episode=5,
                             episode_title="Pilot")


def test_sheet_maps_flags_and_fields():
    sheet = os_upload.build_info_sheet(
        srt_name="The Film (1999).en.sdh.srt", assignment=_movie(),
        language_code="eng", is_sdh=True, forced=False, height=1080, fps=23.976,
        version="v1.2.3")
    assert "English (eng)" in sheet
    assert "23.976" in sheet
    assert "hearing impaired:       YES" in sheet
    assert "high-definition movie:  YES" in sheet
    assert "Foreign Parts Only:                   no" in sheet
    assert "machine translated:     no" in sheet
    assert "The Film (1999)" in sheet


def test_forced_sd_movie_flags():
    sheet = os_upload.build_info_sheet(
        srt_name="x.forced.srt", assignment=_movie(),
        language_code="pol", is_sdh=False, forced=True, height=480, fps=None,
        version="v1")
    assert "Polish (pol)" in sheet
    assert "Foreign Parts Only:                   YES" in sheet
    assert "high-definition movie:  no" in sheet
    assert "(check the video file)" in sheet  # no fps


def test_episode_sheet_has_season_episode():
    sheet = os_upload.build_info_sheet(
        srt_name="ep.srt", assignment=_episode(), language_code="eng",
        is_sdh=False, forced=False, height=720, fps=25.0, version="v1")
    assert "S02E05" in sheet
    assert "Pilot" in sheet


def test_export_writes_srt_and_sheet(tmp_path):
    srt = tmp_path / "The Film (1999).en.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n", encoding="utf-8")
    export_dir = tmp_path / "upload"
    dest = os_upload.export_subtitle(
        export_dir, srt, assignment=_movie(), language_code="eng",
        is_sdh=False, forced=False, height=1080, fps=24.0, version="v1")
    assert dest.exists() and dest.read_text().startswith("1\n")
    assert dest.parent.name == "The Film (1999)"
    info = dest.parent / (srt.name + ".info.txt")
    assert info.exists() and "OpenSubtitles manual upload" in info.read_text()


def test_a_two_episode_title_is_named_for_both_on_the_upload_sheet():
    a = EpisodeAssignment(2, MediaKind.TV, "HIMYM", 2005, season=8, episode=11, episode_end=12)
    sheet = os_upload.build_info_sheet(
        srt_name="ep.srt", assignment=a, language_code="eng",
        is_sdh=False, forced=False, height=480, fps=29.97, version="v1")
    assert "S08E11-E12" in sheet
