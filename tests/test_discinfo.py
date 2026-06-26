from pathlib import Path

from blurayripper.models import DiscInfo, MediaKind, Title
from blurayripper.tools import discinfo
from blurayripper.tools.makemkv import parse_info

FIXTURE = Path(__file__).parent / "fixtures" / "makemkv_info_tv.txt"


def _tv_disc() -> DiscInfo:
    return parse_info(FIXTURE.read_text(encoding="utf-8").splitlines(), "file:/fake")


def test_tv_disc_detected_as_tv():
    disc = _tv_disc()
    assert discinfo.guess_kind(disc) is MediaKind.TV
    eps = discinfo.episode_candidate_titles(disc)
    assert len(eps) >= 3


def test_movie_disc_detected_as_movie():
    disc = DiscInfo(
        source="file:/fake",
        disc_name="Some Movie",
        titles=[
            Title(title_id=0, duration_seconds=2 * 3600, source_filename="00800.mpls"),
            Title(title_id=1, duration_seconds=120, source_filename="00801.mpls"),
        ],
    )
    assert discinfo.guess_kind(disc) is MediaKind.MOVIE
    assert discinfo.main_feature(disc).title_id == 0


def test_ordered_titles_by_playlist():
    disc = DiscInfo(
        source="x",
        titles=[
            Title(title_id=5, duration_seconds=100, source_filename="00010.mpls"),
            Title(title_id=2, duration_seconds=100, source_filename="00002.mpls"),
        ],
    )
    ordered = discinfo.ordered_titles(disc)
    assert [t.title_id for t in ordered] == [2, 5]


def test_fingerprint_is_stable_across_identical_scans():
    disc_a = _tv_disc()
    disc_b = _tv_disc()
    assert discinfo.disc_fingerprint(disc_a) == discinfo.disc_fingerprint(disc_b)


def test_fingerprint_changes_with_different_durations():
    disc_a = _tv_disc()
    disc_b = _tv_disc()
    disc_b.titles[0].duration_seconds += 600
    assert discinfo.disc_fingerprint(disc_a) != discinfo.disc_fingerprint(disc_b)
