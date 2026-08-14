from pathlib import Path

from blurayripper.models import StreamType
from blurayripper.tools.makemkv import _parse_duration, parse_drives, parse_info

FIXTURE = Path(__file__).parent / "fixtures" / "makemkv_info_tv.txt"


def _load() -> list[str]:
    return FIXTURE.read_text(encoding="utf-8").splitlines()


def test_parse_duration():
    assert _parse_duration("0:22:05") == 22 * 60 + 5
    assert _parse_duration("1:07:03") == 3600 + 7 * 60 + 3
    assert _parse_duration("23:10") == 23 * 60 + 10
    assert _parse_duration("garbage") == 0


def test_disc_and_title_counts():
    disc = parse_info(_load(), "file:/fake")
    assert disc.disc_name == "Sample Show Season 1 Disc 1"
    assert disc.volume_label == "SAMPLE_SHOW_S01_D1"
    assert len(disc.titles) == 4


def test_title_attributes():
    disc = parse_info(_load(), "file:/fake")
    t0 = disc.title(0)
    assert t0 is not None
    assert t0.duration_seconds == 22 * 60 + 5
    assert t0.chapter_count == 6
    assert t0.size_bytes == 1503238553
    assert t0.source_filename == "00001.mpls"
    assert t0.output_filename == "title_t00.mkv"


def test_stream_languages():
    disc = parse_info(_load(), "file:/fake")
    t0 = disc.title(0)
    assert t0.audio_languages == ["eng", "pol"]
    assert t0.subtitle_languages == ["eng"]
    # Subtitle stream is PGS.
    subs = [s for s in t0.streams if s.stream_type is StreamType.SUBTITLE]
    assert len(subs) == 1
    assert "PGS" in (subs[0].codec or "")


def test_a_repeated_drv_block_lists_each_drive_once():
    """makemkvcon re-enumerates after LibreDrive init, emitting the DRV block
    twice in a single run. Both lines are the same physical drive, so appending
    them showed every drive twice in the panel — each with its own Add button
    that queued the same disc into a second job."""
    line0 = ('DRV:0,2,999,12,"BD-RE ASUS BW-16D1HT 3.01 KLUL75I4959",'
             '"The Nightmare Before Christmas","/dev/sr0"')
    line1 = ('DRV:1,2,999,12,"BD-RE ASUS BW-16D1HT 3.10 KX1P71E4206",'
             '"","/dev/sr1"')

    drives = parse_drives([line0, line1, line0, line1])

    assert [d.index for d in drives] == [0, 1]
    assert drives[0].device == "/dev/sr0"
    assert drives[0].disc_name == "The Nightmare Before Christmas"
    assert drives[1].disc_name is None


def test_a_second_enumeration_does_not_blank_a_disc_name_the_first_had():
    """The re-enumeration is sometimes the less complete one. Later wins, but it
    must not lose information the earlier line carried."""
    full = 'DRV:0,2,999,12,"ASUS BW-16D1HT","Some Disc","/dev/sr0"'
    empty = 'DRV:0,2,999,12,"ASUS BW-16D1HT","",""'

    drives = parse_drives([full, empty])

    assert len(drives) == 1
    assert drives[0].disc_name == "Some Disc"
    assert drives[0].device == "/dev/sr0"
