from pathlib import Path

from blurayripper.models import StreamType
from blurayripper.tools.makemkv import _parse_duration, parse_info

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
