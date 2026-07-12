"""Kodi/Plex .nfo sidecar generation."""

from xml.etree import ElementTree

from blurayripper.models import EpisodeAssignment, MediaKind
from blurayripper.services import nfo


def test_movie_nfo():
    a = EpisodeAssignment(0, MediaKind.MOVIE, "The Film & Co", 1999, edition="Director's Cut")
    root = ElementTree.fromstring(nfo.build_nfo(a))
    assert root.tag == "movie"
    assert root.findtext("title") == "The Film & Co"      # & is escaped/parsed
    assert root.findtext("year") == "1999"
    assert root.findtext("edition") == "Director's Cut"


def test_episode_nfo():
    a = EpisodeAssignment(0, MediaKind.TV, "The Show", 2015, season=2, episode=5,
                          episode_title="Pilot")
    root = ElementTree.fromstring(nfo.build_nfo(a))
    assert root.tag == "episodedetails"
    assert root.findtext("showtitle") == "The Show"
    assert root.findtext("season") == "2"
    assert root.findtext("episode") == "5"
    assert root.findtext("title") == "Pilot"


def test_write_nfo_next_to_video(tmp_path):
    a = EpisodeAssignment(0, MediaKind.MOVIE, "M", 2000)
    video = tmp_path / "M (2000).mkv"
    out = nfo.write_nfo(video, a)
    assert out == tmp_path / "M (2000).nfo"
    assert out.read_text().startswith("<?xml")
