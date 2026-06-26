import json

from blurayripper.tools.mkvtools import parse_identify

SAMPLE = json.dumps(
    {
        "tracks": [
            {"id": 0, "type": "video", "codec": "MPEG-4p10/AVC/H.264"},
            {"id": 1, "type": "audio", "codec": "DTS-HD Master Audio",
             "properties": {"language": "eng"}},
            {"id": 2, "type": "subtitles", "codec": "HDMV PGS",
             "properties": {"language": "eng"}},
            {"id": 3, "type": "subtitles", "codec": "HDMV PGS",
             "properties": {"language": "pol"}},
            {"id": 4, "type": "subtitles", "codec": "SubRip/SRT",
             "properties": {"language": "und"}},
        ]
    }
)


def test_parse_identify_filters_to_subtitles():
    tracks = parse_identify(SAMPLE)
    assert [t.track_id for t in tracks] == [2, 3, 4]


def test_pgs_detection_and_language():
    tracks = {t.track_id: t for t in parse_identify(SAMPLE)}
    assert tracks[2].is_pgs and tracks[2].language == "eng"
    assert tracks[3].is_pgs and tracks[3].language == "pol"
    # SRT is not PGS; 'und' language becomes None.
    assert not tracks[4].is_pgs
    assert tracks[4].language is None
