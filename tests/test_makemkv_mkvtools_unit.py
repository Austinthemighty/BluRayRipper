"""Unit tests for the two robot/JSON parsers a scan depends on.

makemkvcon's ``-r`` output and mkvmerge's ``-J`` JSON are the only things that
tell the app what is on a disc. Everything downstream (which title to rip, which
audio to keep, which subtitle track to OCR, which encoder to pick) is built from
what these parsers return, so a silent mis-parse becomes a wrong rip rather than
an error. These cover the ragged edges: truncated lines, absent fields, values
in an unexpected order, and log noise mixed into the stream.

``tests/test_makemkv_parser.py`` and ``tests/test_mkvtools.py`` cover the happy
paths; this file deliberately does not repeat them.
"""

import json
from pathlib import Path

from blurayripper.config import load_settings
from blurayripper.models import StreamType
from blurayripper.tools.makemkv import (
    _parse_duration,
    _split_robot_line,
    _to_int,
    parse_drives,
    parse_info,
)
from blurayripper.tools.mkvtools import (
    MkvTools,
    parse_identify,
    parse_probe,
    parse_video_height,
)

FIXTURE = Path(__file__).parent / "fixtures" / "makemkv_info_tv.txt"


def _fixture_lines() -> list[str]:
    return FIXTURE.read_text(encoding="utf-8").splitlines()


def _subs(tracks: list) -> dict:
    return {t.track_id: t for t in tracks}


# --------------------------------------------------------------------------
# makemkv: robot line splitting
# --------------------------------------------------------------------------


def test_a_comma_inside_a_quoted_field_stays_part_of_the_message():
    """MakeMKV's MSG text routinely contains commas ("...22 seconds, which is
    less than..."). Splitting on commas would chop the message in half and shift
    every field after it, so the reason a scan found no titles would reach the
    UI as a truncated fragment — or as the wrong field entirely."""
    payload = '5021,0,3,"Title #3 has length 22 seconds, which is too short","%1"'

    fields = _split_robot_line(payload)

    assert len(fields) == 5
    assert fields[3] == "Title #3 has length 22 seconds, which is too short"
    assert fields[4] == "%1"


def test_doubled_quotes_collapse_to_one_inside_a_field():
    """Disc labels and MakeMKV messages quote things ("Backup" folder, movie
    titles with quotes). Robot mode escapes them by doubling; leaving them
    doubled would put a stray `""` into a disc name that becomes a folder name."""
    fields = _split_robot_line('2,0,"He said ""hi"" loudly"')

    assert fields[2] == 'He said "hi" loudly'


def test_an_empty_payload_yields_no_fields_instead_of_raising():
    """A bare `PRGV:` or a truncated final line at the end of a killed
    makemkvcon run has nothing after the colon. Letting the reader's
    StopIteration escape would blow up the whole scan over one junk line."""
    assert _split_robot_line("") == []


# --------------------------------------------------------------------------
# makemkv: scalar conversions
# --------------------------------------------------------------------------


def test_a_bare_seconds_duration_is_not_read_as_minutes():
    """Durations gate everything: min-length filtering, main-feature picking and
    the episode/extra split. The rightmost component is always seconds, so a
    two-part value is mm:ss and a one-part value is plain seconds; reading them
    from the left would inflate a 45 second trailer into a 45 minute feature."""
    assert _parse_duration("45") == 45
    assert _parse_duration("45:00") == 45 * 60
    assert _parse_duration("2:03:04") == 2 * 3600 + 3 * 60 + 4


def test_a_duration_with_a_non_numeric_part_is_zero_not_partially_parsed():
    """A garbled duration must read as "unknown" (0), not as whatever prefix
    happened to parse. Half-parsing "1:xx:30" into 30 seconds would quietly
    demote a feature-length title below the min-length cut-off and drop it."""
    assert _parse_duration("1:xx:30") == 0
    assert _parse_duration("") == 0


def test_a_human_readable_size_is_not_mistaken_for_a_byte_count():
    """TINFO carries the size twice: attr 10 as "1.4 GB" and attr 11 as bytes.
    Anything non-numeric has to come back 0 so a display string can never be
    used as a byte count in the free-space check before a rip."""
    assert _to_int("1.4 GB") == 0
    assert _to_int(" 1503238553 ") == 1503238553


def test_a_missing_field_is_zero_rather_than_an_exception():
    """Robot lines lose fields when makemkvcon is killed mid-write, so _to_int
    gets None/"" and must absorb it — an AttributeError here aborts the parse of
    an otherwise complete scan. A negative value is real, though: index -1 marks
    a drive the OS sees but MakeMKV cannot open, and must survive."""
    assert _to_int(None) == 0
    assert _to_int("") == 0
    assert _to_int("-1") == -1


# --------------------------------------------------------------------------
# makemkv: parse_info
# --------------------------------------------------------------------------


def test_the_friendly_cinfo_name_beats_the_drv_label_whichever_line_came_first():
    """The DRV label is the raw volume id ("SAMPLE_SHOW_S01_D1"); CINFO Name is
    the human title used for the library folder. makemkvcon does not guarantee
    the order it emits them in, and the DRV line must never overwrite a name
    CINFO already supplied or discs land in an ALL_CAPS_UNDERSCORE folder."""
    lines = [
        'CINFO:2,0,"Sample Show Season 1 Disc 1"',
        'DRV:0,2,999,12,"BD-RE","SAMPLE_SHOW_S01_D1","/dev/sr0"',
    ]

    assert parse_info(lines, "disc:0").disc_name == "Sample Show Season 1 Disc 1"


def test_a_disc_with_no_cinfo_name_still_gets_the_drv_label():
    """Plenty of DVDs report no CINFO Name at all. Falling back to the DRV label
    is what keeps such a disc from arriving in the queue nameless."""
    lines = ['DRV:0,2,999,12,"BD-RE","UNTITLED_DVD","/dev/sr0"']

    assert parse_info(lines, "disc:0").disc_name == "UNTITLED_DVD"


def test_an_unrelated_cinfo_attribute_does_not_overwrite_the_disc_name():
    """CINFO carries a dozen attributes (type, language, comment...) keyed only
    by id. Only 2 is the name and only 32 is the volume label; treating a
    neighbour like 30 as a name would let an arbitrary string win just because
    it happened to be printed last."""
    lines = [
        'CINFO:2,0,"Sample Show Season 1 Disc 1"',
        'CINFO:32,0,"SAMPLE_SHOW_S01_D1"',
        'CINFO:30,0,"something else entirely"',
    ]

    disc = parse_info(lines, "file:/fake")

    assert disc.disc_name == "Sample Show Season 1 Disc 1"
    assert disc.volume_label == "SAMPLE_SHOW_S01_D1"


def test_truncated_robot_lines_are_skipped_instead_of_aborting_the_scan():
    """A killed or crashing makemkvcon leaves half-written lines in the log, and
    plain text (banners, MSG noise) is interleaved throughout. Every one of them
    is short of the fields its branch indexes, so an unguarded parse raises
    IndexError and the entire scan is lost over one bad line."""
    lines = [
        "TINFO:0,9",                     # missing the value fields
        "SINFO:0,0",                     # missing attr id and value
        "CINFO:2",                       # missing the value
        "DRV:0,2",                       # missing name/disc/device
        "MakeMKV v1.17.7 started",       # no colon at all
        "",
        'TINFO:0,9,0,"0:10:00"',         # the one good line
    ]

    disc = parse_info(lines, "file:/fake")

    assert [t.title_id for t in disc.titles] == [0]
    assert disc.titles[0].duration_seconds == 600


def test_a_title_seen_only_in_stream_lines_still_appears():
    """SINFO can reach us for a title whose TINFO block was lost (truncated log,
    or attributes the scan never emitted). The stream still has to be attachable
    to something, so the title is created on demand — otherwise the attach pass
    raises KeyError and no titles at all come back from the disc."""
    disc = parse_info(['SINFO:7,0,1,6201,"Video"'], "file:/fake")

    assert [t.title_id for t in disc.titles] == [7]
    assert [s.stream_type for s in disc.titles[0].streams] == [StreamType.VIDEO]


def test_streams_land_on_their_own_title_in_stream_id_order():
    """Track order is how the app names sidecars and picks the default audio, and
    makemkvcon interleaves titles and emits attributes out of order. Streams must
    be grouped by (title, stream) and sorted by stream id, not by arrival — a
    title 0 stream appended to title 1 would OCR the wrong episode's subtitles."""
    lines = [
        'SINFO:0,2,1,6203,"Subtitles"',
        'SINFO:1,0,1,6201,"Video"',
        'SINFO:0,0,1,6201,"Video"',
        'SINFO:0,1,1,6202,"Audio"',
    ]

    disc = parse_info(lines, "file:/fake")

    assert [t.title_id for t in disc.titles] == [0, 1]
    assert [s.stream_id for s in disc.titles[0].streams] == [0, 1, 2]
    assert [s.stream_type for s in disc.titles[0].streams] == [
        StreamType.VIDEO,
        StreamType.AUDIO,
        StreamType.SUBTITLE,
    ]
    assert [s.stream_id for s in disc.titles[1].streams] == [0]


def test_titles_are_ordered_numerically_not_by_line_order():
    """The UI lists titles in this order and users pick episodes by position. A
    lexicographic or arrival ordering would put title 10 ahead of title 2."""
    lines = ['TINFO:10,9,0,"0:01:00"', 'TINFO:2,9,0,"0:01:00"']

    assert [t.title_id for t in parse_info(lines, "file:/fake").titles] == [2, 10]


def test_the_full_codec_id_wins_over_the_short_name():
    """MakeMKV sends the codec id (attr 5, "V_MPEG4/ISO/AVC") and then a short
    label (attr 6, "Mpeg4") for the same stream. The id is what the PGS check and
    the HEVC/UHD routing match on, so the first value must stick; letting the
    later short label overwrite it turns "S_HDMV/PGS" into a name nothing
    recognises. Where only the short label is sent it is still used."""
    disc = parse_info(_fixture_lines(), "file:/fake")
    streams = {s.stream_id: s for s in disc.title(0).streams}

    assert streams[0].codec == "V_MPEG4/ISO/AVC"   # attr 5 arrives before attr 6
    assert streams[1].codec == "DTS-HD MA"         # attr 6 only


def test_a_stream_language_is_normalised_and_a_blank_one_becomes_none():
    """Language codes are compared against the configured keep-list and used to
    name .srt sidecars, so casing and padding have to be stripped. A blank code
    must become None rather than "", which would produce a sidecar named
    `episode..srt` and a keep-list entry that matches nothing."""
    lines = [
        'SINFO:0,1,1,6202,"Audio"',
        'SINFO:0,1,3,0," ENG "',
        'SINFO:0,2,1,6202,"Audio"',
        'SINFO:0,2,3,0,""',
    ]

    streams = {s.stream_id: s for s in parse_info(lines, "file:/fake").title(0).streams}

    assert streams[1].language == "eng"
    assert streams[2].language is None


def test_the_human_language_name_is_not_used_as_the_track_name():
    """SINFO attr 4 is the spelled-out language ("English") and sits right next
    to attr 2, the real track name ("Director's Commentary"). Reading 4 as the
    name would label every track after its language and destroy the only signal
    that separates a commentary track from the feature audio."""
    lines = [
        'SINFO:0,1,1,6202,"Audio"',
        'SINFO:0,1,4,0,"English"',
        'SINFO:0,2,1,6202,"Audio"',
        'SINFO:0,2,4,0,"English"',
        'SINFO:0,2,2,0,"Director\'s Commentary"',
    ]

    streams = {s.stream_id: s for s in parse_info(lines, "file:/fake").title(0).streams}

    assert streams[1].name is None
    assert streams[2].name == "Director's Commentary"


def test_a_stream_with_no_type_line_is_not_counted_as_audio():
    """A stream whose Type attribute never arrived is unknown, not audio. If the
    default leaned audio, its language would join the keep-list decision and the
    encoder would be told to preserve a track that may not be audio at all."""
    lines = ['SINFO:0,1,3,0,"deu"']

    title = parse_info(lines, "file:/fake").title(0)

    assert title.streams[0].stream_type is StreamType.UNKNOWN
    assert title.audio_languages == []


def test_the_drive_device_comes_from_the_selected_disc_index():
    """Auto-eject sends the tray command to disc.drive_device. With two drives
    present, taking the first DRV line rather than the one matching the ripped
    `disc:N` selector would eject the other drive — potentially mid-rip."""
    lines = [
        'DRV:0,2,999,12,"BD-RE A","Disc A","/dev/sr0"',
        'DRV:1,2,999,12,"BD-RE B","Disc B","/dev/sr1"',
    ]

    assert parse_info(lines, "disc:1").drive_device == "/dev/sr1"
    assert parse_info(lines, "disc:0").drive_device == "/dev/sr0"
    # A selector with no matching DRV line leaves it unknown rather than guessing.
    assert parse_info(lines, "disc:5").drive_device is None


def test_only_disc_and_dev_sources_carry_a_drive_to_eject():
    """`dev:` names the device outright, while `file:`/`iso:` sources are a
    folder or an image on disk with no tray behind them. Treating those paths as
    a device would hand the eject call a filesystem path."""
    lines = ['DRV:0,2,999,12,"BD-RE A","Disc A","/dev/sr0"']

    assert parse_info(lines, "dev:/dev/sr9").drive_device == "/dev/sr9"
    assert parse_info(lines, "file:/mnt/rips/BDMV_parent").drive_device is None
    assert parse_info(lines, "iso:/mnt/rips/movie.iso").drive_device is None


# --------------------------------------------------------------------------
# makemkv: parse_drives
# --------------------------------------------------------------------------


def test_an_empty_drive_bay_line_is_not_listed_as_a_drive():
    """makemkvcon pads its enumeration with placeholder DRV lines for bays that
    hold no drive (`DRV:1,256,999,0,"","",""`). They have no drive name, and
    listing them puts phantom entries with an Add button in the drives panel."""
    drives = parse_drives(_fixture_lines())

    assert [d.index for d in drives] == [0]
    assert drives[0].name == "BD-RE HL-DT-ST BD-RE WH16NS40"


def test_a_drv_line_without_a_device_column_still_lists_the_drive():
    """Older makemkvcon builds omit the trailing OS-device field. The drive is
    still rippable via its disc:N index, so it must appear (with no device to
    eject) instead of being dropped or raising IndexError. A line too short to
    even carry a drive name is junk and is dropped."""
    six_fields = 'DRV:0,2,999,12,"BD-RE HL-DT-ST","Some Disc"'
    too_short = 'DRV:1,2,999'

    drives = parse_drives([six_fields, too_short])

    assert [d.index for d in drives] == [0]
    assert drives[0].disc_name == "Some Disc"
    assert drives[0].device is None


# --------------------------------------------------------------------------
# mkvtools: parse_identify
# --------------------------------------------------------------------------


def test_a_subtitle_track_with_no_properties_block_is_still_listed():
    """mkvmerge omits `properties` entirely for a track carrying no metadata.
    The track is still extractable and OCR-able, so it has to be listed with an
    unknown language rather than crashing the identify of the whole file."""
    stdout = json.dumps({"tracks": [{"id": 4, "type": "subtitles", "codec": "HDMV PGS"}]})

    tracks = parse_identify(stdout)

    assert [t.track_id for t in tracks] == [4]
    assert tracks[0].language is None
    assert tracks[0].is_pgs


def test_a_null_codec_does_not_sink_the_identify():
    """A JSON null codec (seen on damaged/unusual muxes) reaches the PGS check as
    None. Uppercasing None raises, which would lose every subtitle on the file —
    including the good tracks listed after it."""
    stdout = json.dumps(
        {"tracks": [{"id": 1, "type": "subtitles", "codec": None,
                     "properties": {"language": "eng"}}]}
    )

    track = parse_identify(stdout)[0]

    assert track.codec == ""
    assert track.is_pgs is False
    assert track.language == "eng"


def test_pgs_is_detected_whatever_the_case_of_the_codec_name():
    """PGS is the image-based format that has to be OCR'd; a text format must not
    be. mkvmerge spells the codec differently across versions ("HDMV PGS",
    "S_HDMV/PGS"), so the match is case-insensitive — and must stay narrow enough
    that VobSub, which our PGS pipeline cannot read, is not swept in."""
    stdout = json.dumps(
        {
            "tracks": [
                {"id": 0, "type": "subtitles", "codec": "S_HDMV/PGS"},
                {"id": 1, "type": "subtitles", "codec": "hdmv pgs"},
                {"id": 2, "type": "subtitles", "codec": "VobSub"},
            ]
        }
    )

    tracks = _subs(parse_identify(stdout))

    assert tracks[0].is_pgs
    assert tracks[1].is_pgs
    assert not tracks[2].is_pgs


def test_a_track_tagged_only_with_a_bcp47_language_keeps_it():
    """Some remuxes set only `language_ietf`. Without the fallback the track
    reads as unknown and its sidecar loses the language suffix Plex needs, so a
    Brazilian Portuguese track would be indistinguishable from any other."""
    stdout = json.dumps(
        {"tracks": [{"id": 2, "type": "subtitles", "codec": "HDMV PGS",
                     "properties": {"language_ietf": "pt-BR"}}]}
    )

    assert parse_identify(stdout)[0].language == "pt-br"


def test_zxx_and_blank_languages_are_dropped_like_und():
    """"zxx" (no linguistic content) and an empty string are placeholders, not
    languages. Keeping them produces sidecars named `.zxx.srt` that Plex offers
    the viewer as a real subtitle option."""
    stdout = json.dumps(
        {
            "tracks": [
                {"id": 0, "type": "subtitles", "codec": "HDMV PGS",
                 "properties": {"language": "zxx"}},
                {"id": 1, "type": "subtitles", "codec": "HDMV PGS",
                 "properties": {"language": ""}},
            ]
        }
    )

    assert [t.language for t in parse_identify(stdout)] == [None, None]


def test_forced_is_inferred_from_the_track_name():
    """Forced subtitles (alien dialogue only) must not be shipped as the full
    subtitle track. Many discs set no forced flag and only say so in the track
    name, and without the name check both tracks claim the same sidecar name and
    one silently overwrites the other."""
    stdout = json.dumps(
        {"tracks": [{"id": 3, "type": "subtitles", "codec": "HDMV PGS",
                     "properties": {"language": "eng", "track_name": "English Forced"}}]}
    )

    track = parse_identify(stdout)[0]

    assert track.forced is True
    assert track.sdh is False


def test_a_language_name_containing_cc_is_not_treated_as_closed_captions():
    """"CC" as a whole track name means closed captions, but the letters can also
    fall inside an ordinary name. The name match has to be the whole string —
    a substring test would tag Occitan subtitles as hearing-impaired and file
    them under the wrong sidecar."""
    stdout = json.dumps(
        {
            "tracks": [
                {"id": 0, "type": "subtitles", "codec": "HDMV PGS",
                 "properties": {"track_name": "Occitan"}},
                {"id": 1, "type": "subtitles", "codec": "HDMV PGS",
                 "properties": {"track_name": "CC"}},
            ]
        }
    )

    tracks = _subs(parse_identify(stdout))

    assert tracks[0].sdh is False
    assert tracks[1].sdh is True


# --------------------------------------------------------------------------
# mkvtools: parse_probe / parse_video_height / video_dims
# --------------------------------------------------------------------------


def test_container_duration_becomes_whole_seconds_and_missing_means_zero():
    """The probe duration is compared against the source title's runtime to
    verify an encode did not truncate. mkvmerge reports nanoseconds, and files
    written by an interrupted tool have no container duration at all — that has
    to read as 0 ("unknown"), not raise, or the verify step never runs."""
    assert parse_probe(json.dumps({"tracks": []})).duration_seconds == 0
    with_duration = json.dumps(
        {"container": {"properties": {"duration": 5_999_999_999}}, "tracks": []}
    )
    assert parse_probe(with_duration).duration_seconds == 5


def test_undecided_audio_languages_are_left_out_of_the_probe():
    """The probed audio languages drive the keep-list check on an already-encoded
    file. "und"/"zxx"/absent are placeholders and must not be counted as real
    languages, and casing has to be normalised or an "ENG" track fails to match
    the configured "eng"."""
    stdout = json.dumps(
        {
            "tracks": [
                {"type": "audio", "properties": {"language": "ENG"}},
                {"type": "audio", "properties": {"language": "und"}},
                {"type": "audio", "properties": {}},
                {"type": "audio", "properties": {"language": "zxx"}},
                {"type": "audio", "properties": {"language": "fre"}},
            ]
        }
    )

    assert parse_probe(stdout).audio_languages == ["eng", "fre"]


def test_the_height_comes_from_the_first_video_track_listed():
    """Height picks the encoder path (UHD/HDR goes to ffmpeg, everything else to
    HandBrake). Audio tracks are frequently listed ahead of the video and carry
    no dimensions, and in a stitched remux with a second video track the main
    feature is the first one — letting a later track win would route a 1080p
    encode down the UHD path on the strength of a tacked-on intro."""
    stdout = json.dumps(
        {
            "tracks": [
                {"type": "audio", "properties": {"language": "eng"}},
                {"type": "video", "properties": {"pixel_dimensions": "1920x1080"}},
                {"type": "video", "properties": {"pixel_dimensions": "3840x2160"}},
            ]
        }
    )

    assert parse_video_height(stdout) == 1080


def test_a_malformed_pixel_dimensions_yields_no_height_rather_than_an_error():
    """A dimensions string that is not WxH (truncated or non-numeric) means the
    height is unknown. Raising here would fail the job outright instead of
    letting it fall back to the default encoder path."""
    assert parse_video_height(
        json.dumps({"tracks": [{"type": "video", "properties": {"pixel_dimensions": "1920"}}]})
    ) is None
    assert parse_video_height(
        json.dumps({"tracks": [{"type": "video", "properties": {"pixel_dimensions": "axb"}}]})
    ) is None


def _mkvtools(monkeypatch) -> MkvTools:
    settings = load_settings()
    monkeypatch.setattr(settings.tools, "mkvmerge", "/usr/bin/mkvmerge")
    return MkvTools(settings)


async def test_video_dims_reads_height_and_fps_from_a_single_probe(monkeypatch):
    """Height and frame rate are both needed before an encode and both come out
    of the same mkvmerge JSON. Probing twice doubles a network read of a large
    remux on the mapped drive for information already in hand — and the pair is
    returned (height, fps), which the callers unpack positionally.

    The audio track here is listed first and carries its own default_duration
    (mkvmerge reports one for AC3 and friends), so a frame rate taken from
    whichever track happens to be first would come back as audio frame timing."""
    calls = []

    class _Result:
        stdout_lines = [
            json.dumps(
                {
                    "tracks": [
                        {"type": "audio", "properties": {
                            "language": "eng",
                            "default_duration": 32_000_000,
                        }},
                        {"type": "video", "properties": {
                            "pixel_dimensions": "3840x2160",
                            "default_duration": 41_708_333,
                        }},
                    ]
                }
            )
        ]
        stderr = ""
        returncode = 0

    async def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _Result()

    monkeypatch.setattr("blurayripper.tools.mkvtools.process.run", fake_run)

    height, fps = await _mkvtools(monkeypatch).video_dims(Path("/z/movie.mkv"))

    assert height == 2160
    assert fps == 23.976
    assert len(calls) == 1
    assert "-J" in calls[0]      # JSON output, not the human-readable listing


async def test_a_missing_frame_rate_does_not_cost_us_the_height(monkeypatch):
    """Not every file reports a default_duration. An unknown frame rate is
    survivable — an unknown height is not, because it decides the encoder — so
    the two must be resolved independently of each other."""
    class _Result:
        stdout_lines = [
            json.dumps({"tracks": [{"type": "video",
                                    "properties": {"pixel_dimensions": "1920x1080"}}]})
        ]
        stderr = ""
        returncode = 0

    async def fake_run(cmd, **kwargs):
        return _Result()

    monkeypatch.setattr("blurayripper.tools.mkvtools.process.run", fake_run)

    assert await _mkvtools(monkeypatch).video_dims(Path("/z/movie.mkv")) == (1080, None)
