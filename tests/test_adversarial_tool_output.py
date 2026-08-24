"""What the parsers do when the tools hand back garbage.

Every decision this app makes about a disc comes out of two text formats:
makemkvcon's ``-r`` robot lines and mkvmerge/ffprobe JSON. Neither is validated
by anything upstream of these parsers, and both arrive from processes that get
killed mid-write, run out of NAS, spew a re-enumeration twice, print a Windows
codepage into a UTF-8 pipe, or — through a caching proxy — answer with an HTML
error page instead of JSON at all.

The failure mode that matters here is not a crash. It is a parser that answers
confidently with a value it made up: a height of ``None`` that reads as "not
UHD" and routes an HDR remux to HandBrake, an empty track list that reads as
"this disc has no subtitles" and lands the job in the library green, a duration
of ``0`` that makes the post-rip length check return early on a truncated file.
So each test below pins the parser against BOTH edges: the garbage must be
refused, and the neighbouring legitimate input (a file that genuinely has no
video track, a disc that genuinely has no subtitles) must still be answered —
otherwise "refused" is indistinguishable from "broken".

Fixtures marked REAL are copied verbatim from production tool output.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blurayripper.config import (
    AudioSettings,
    EncodeSettings,
    LanguageSettings,
    Settings,
    StorageSettings,
    SubtitleSettings,
    ToolPaths,
)
from blurayripper.services import hdr as hdr_mod
from blurayripper.tools import ffmpeg as ffmpeg_mod
from blurayripper.tools import process as process_mod
from blurayripper.tools.ffmpeg import FFmpeg
from blurayripper.tools.makemkv import (
    _parse_duration,
    _split_robot_line,
    parse_drives,
    parse_info,
)
from blurayripper.tools.mkvtools import (
    parse_identify,
    parse_probe,
    parse_video_height,
)

# REAL: one makemkvcon run re-enumerated its drives after LibreDrive init and
# emitted this block twice. Both copies are the same two physical drives.
DRV_0 = ('DRV:0,2,999,12,"BD-RE ASUS BW-16D1HT 3.01 KLUL75I4959",'
         '"The Nightmare Before Christmas","/dev/sr0"')
DRV_1 = ('DRV:1,2,999,12,"BD-RE ASUS BW-16D1HT 3.10 KX1P71E4206",'
         '"","/dev/sr1"')

# REAL: a healthy scan/rip log. Plain text, but several lines carry a colon and
# so partition into a prefix and a payload exactly like a robot line does.
HEALTHY_LOG = [
    "MakeMKV v1.18.3 linux(x64-release) started",
    "Using LibreDrive mode (v06.3 id=E064BA3E3F81)",
    "Loaded content hash table, will verify integrity of M2TS files.",
    "File 00260.mpls was added as title #0",
    "Title #00107.mpls has length of 17 seconds which is less than minimum "
    "title length of 120 seconds and was therefore skipped",
    "Operation successfully completed",
    "Saving 1 titles into directory file:///mnt/TrueNas/.../rip/t000",
    "AV sync issue in stream 5 at 0:00:00 : Forced subtitles track #5 turned "
    "out to be empty and was removed from output file",
    "1 titles saved",
    "Copy complete. 1 titles saved.",
]

# The same announcements as they arrive in ``-r`` mode — wrapped in a MSG
# record. This is the dangerous form: unlike the plain-text lines above they DO
# split into 6 and 4 comma-separated fields, so a parser matching on field count
# instead of on the record prefix reads them as disc structure. The 3307 line is
# REAL (it recurs verbatim in the progress-noise fixture below); the second is
# the same wrapper around the failed-rip summary.
ROBOT_MSG_LOG = [
    'MSG:3307,0,3,"File 00260.mpls was added as title #0","%1","%2"',
    'MSG:5005,0,0,"0 titles saved, 1 failed"',
]

# REAL: the NAS write died mid-rip and makemkvcon still exited cleanly.
FAILED_RIP_LOG = [
    "Program reads data faster than it can write to disk, consider upgrading "
    "your hard drive if you see many of these messages.",
    "Error 'Posix error - Resource temporarily unavailable' occurred while "
    "writing data to '/mnt/TrueNas/.../job_334/rip/t000/X.mkv'",
    "Failed to save title 0 to file /mnt/TrueNas/.../job_334/rip/t000/X.mkv",
    "0 titles saved, 1 failed",
    "Copy complete. 0 titles saved, 1 failed.",
]

# REAL: what a caching proxy in front of the tool mirror returned instead of the
# JSON the probe asked for.
PROXY_ERROR_PAGE = ("<html><head><title>502 Bad Gateway</title></head>"
                    "<body>...</body></html>")

TRUNCATED_JSON = '{"tracks": [{"id": 0, "type": "sub'

# ffprobe emits the mastering-display primaries as num/denom, numerator already
# in the units x265 wants.
MASTERING_DISPLAY = {
    "side_data_type": "Mastering display metadata",
    "green_x": "13250/50000", "green_y": "34500/50000",
    "blue_x": "7500/50000", "blue_y": "3000/50000",
    "red_x": "34000/50000", "red_y": "16000/50000",
    "white_point_x": "15635/50000", "white_point_y": "16450/50000",
    "max_luminance": "10000000/10000", "min_luminance": "50/10000",
}


def _ffprobe_doc(*, frame_side_data=None, stream_side_data=None) -> dict:
    """An ffprobe -show_streams/-show_frames document for a UHD HDR10 title."""
    stream = {
        "codec_type": "video",
        "color_primaries": "bt2020",
        "color_transfer": "smpte2084",
        "color_space": "bt2020nc",
    }
    if stream_side_data is not None:
        stream["side_data_list"] = stream_side_data
    doc = {"format": {"duration": "6821.416667"}, "streams": [stream]}
    if frame_side_data is not None:
        doc["frames"] = [{"side_data_list": frame_side_data}]
    return doc


def _ffmpeg(tmp_path: Path) -> FFmpeg:
    settings = Settings(
        tools=ToolPaths(ffmpeg=str(tmp_path / "ffmpeg")),
        storage=StorageSettings(staging_dir=tmp_path / "stage",
                                output_dir=tmp_path / "out"),
        encode=EncodeSettings(),
        languages=LanguageSettings(),
        subtitles=SubtitleSettings(),
        audio=AudioSettings(),
    )
    return FFmpeg(settings)


def _answer_probe_with(monkeypatch, payload: str) -> list[list[str]]:
    """Make the process layer answer any spawn with ``payload`` on stdout.

    Returns the list the spawned commands accumulate in, so a test can tell
    "refused what ffprobe said" apart from "never got as far as ffprobe"."""
    calls: list[list[str]] = []

    async def fake_run(cmd, *, on_line=None, check=True, **kwargs):
        calls.append(list(cmd))
        return process_mod.ProcessResult(
            returncode=0, stdout_lines=payload.splitlines(), stderr="")

    monkeypatch.setattr(process_mod, "run", fake_run)
    assert ffmpeg_mod.process.run is fake_run   # the wrapper shares the module
    return calls


# ==========================================================================
# makemkvcon robot lines
# ==========================================================================


def test_an_unterminated_quote_ends_the_line_instead_of_swallowing_the_parser():
    """makemkvcon killed mid-write (or a drive that dropped off the bus while it
    was printing) leaves a MSG line whose quote never closes. The reader has to
    end the record at the end of the line and hand back the fields it did get:
    the message text is the only record of WHY a scan found no titles, and it is
    the last field, so losing the line loses the reason.

    What it must not do is treat the opening quote as the start of a field that
    continues into the next record — that is how one truncated line eats the
    real lines behind it."""
    fields = _split_robot_line('5010,0,1,"Failed to open disc')

    assert fields == ["5010", "0", "1", "Failed to open disc"]


def test_a_message_field_of_fifty_kilobytes_keeps_its_commas():
    """A wedged drive repeats its read error into one MSG line until the line is
    huge. Splitting the payload on commas would be fine on a short message and
    quietly wrong here — MakeMKV's text is full of commas ("consider upgrading
    your hard drive, ...") and every comma shifts the fields after it, so the
    operator is shown a fragment of the middle of the error instead of the
    message field.

    Sized just under the stdout pump's 64 KiB line limit; a longer line never
    reaches this function at all (see this file's notes)."""
    body = ", ".join(["Error 'Posix error - No such device' at offset 0"] * 1000)
    assert 40_000 < len(body) < 64 * 1024

    fields = _split_robot_line(f'2003,0,3,"{body}","%1"')

    assert len(fields) == 5
    assert fields[3] == body
    assert fields[4] == "%1"


def test_an_impossible_duration_stays_impossible_rather_than_looking_plausible():
    """A garbled TINFO duration like "99:99:99" has no real reading. What it must
    not do is normalise into something an operator would accept: 99 minutes and
    99 seconds folded into 1:40:39 is a perfectly ordinary feature runtime, and
    it would be picked as the main feature and used as the denominator of the
    post-rip length check. Left as positional arithmetic it comes out at 100+
    hours — obviously junk, and above every real title on the disc.

    A negative component likewise stays negative instead of being flipped into a
    runtime that could clear the min-length gate."""
    absurd = _parse_duration("99:99:99")

    assert absurd == 99 * 3600 + 99 * 60 + 99
    assert absurd > 24 * 3600                      # cannot pass for a feature
    assert _parse_duration("1:02:03:04") > 24 * 3600   # a field too many
    assert _parse_duration("-1:00:00") < 0

    # Control: an ordinary duration on the same code path is unaffected.
    assert _parse_duration("1:40:39") == 3600 + 40 * 60 + 39


def test_a_negative_or_absurd_title_index_does_not_land_on_the_real_title_zero():
    """Title 0 is normally the main feature. A TINFO line carrying an out-of-range
    index must become its own (obviously bogus) title rather than being folded
    into title 0, because the attributes on it — a 7-second duration, say —
    would replace the real feature's and drop it under the min-length cut."""
    lines = [
        'TINFO:0,9,0,"1:47:12"',
        'TINFO:0,2,0,"Main Feature"',
        'TINFO:-3,9,0,"0:00:07"',                    # negative index
        'TINFO:99999999999999999999,9,0,"0:00:09"',  # absurd index
    ]

    disc = parse_info(lines, "disc:0")

    main = disc.title(0)
    assert main is not None
    assert main.duration_seconds == 1 * 3600 + 47 * 60 + 12
    assert main.name == "Main Feature"
    assert [t.title_id for t in disc.titles] == [-3, 0, 99999999999999999999]


def test_a_log_line_announcing_a_title_does_not_become_a_title():
    """MakeMKV's log says "File 00260.mpls was added as title #0" and "0 titles
    saved, 1 failed". None of that is disc structure — the structure only ever
    arrives as TINFO/SINFO — and reading titles out of the log is how a rip that
    saved nothing gets a title list anyway.

    ``ROBOT_MSG_LOG`` is what makes this test bite: those two announcements in
    their ``-r`` MSG form split into 4 and 6 fields, so a parser keying on field
    count rather than on the record prefix would file MSG 3307 as title 3307
    (and, in ``parse_drives``, as a drive named "%1" holding a disc "%2"). The
    plain-text lines are the milder half of the same claim — several carry a
    colon and so partition into a prefix and a payload just like a robot line.

    The genuine TINFO line in the same list proves the parser was reading this
    input at all, rather than the whole list being ignored for some other
    reason."""
    noise = HEALTHY_LOG + FAILED_RIP_LOG + ROBOT_MSG_LOG

    disc = parse_info(noise + ['TINFO:4,9,0,"1:22:00"'], "disc:0")

    assert [t.title_id for t in disc.titles] == [4]        # the real one only
    assert disc.titles[0].duration_seconds == 82 * 60
    assert disc.disc_name is None                          # no name invented
    assert parse_drives(noise) == []


def test_a_hundred_thousand_line_log_still_lists_each_drive_once():
    """The duplicate-DRV block is emitted once per re-enumeration, and a long
    session with drive resets repeats it. Dedup by index has to hold at any
    length: every extra copy of a drive in the panel carries its own Add button
    that queues the same disc as a second job."""
    log = (HEALTHY_LOG + [DRV_0, DRV_1, "PRGV:1,2,65536"]) * 8000
    assert len(log) > 100_000

    drives = parse_drives(log)

    assert [(d.index, d.disc_name, d.device) for d in drives] == [
        (0, "The Nightmare Before Christmas", "/dev/sr0"),
        (1, None, "/dev/sr1"),
    ]


def test_a_hundred_thousand_lines_of_progress_noise_do_not_multiply_the_titles():
    """A rip's stdout is mostly PRGV/PRGC ticks with the attribute dump repeated
    at every stage. Titles and streams are keyed by id precisely so that a
    repeated attribute is an overwrite and not another title — the alternative is
    a title list that grows with the length of the log and a UI listing the same
    feature thousands of times."""
    log = ([
        "PRGV:12345,3000,65536",
        'PRGC:5017,0,"Saving to MKV file"',
        'MSG:3307,0,3,"File 00260.mpls was added as title #0","%1","%2"',
        'TINFO:0,9,0,"1:47:12"',
        'SINFO:0,1,1,6201,"Video"',
    ] * 20_000)
    assert len(log) == 100_000

    disc = parse_info(log, "disc:0")

    assert [t.title_id for t in disc.titles] == [0]
    assert disc.titles[0].duration_seconds == 1 * 3600 + 47 * 60 + 12
    assert len(disc.titles[0].streams) == 1


def test_a_disc_label_decoded_in_the_wrong_encoding_is_kept_as_it_arrived():
    """makemkvcon on the Windows box prints the volume label in the console
    codepage; the pipe is decoded as UTF-8 with errors="replace", so an accented
    disc name reaches the parser with U+FFFD in it. That name becomes the library
    folder, so it has to survive verbatim — visible to the operator as something
    to rename. Dropping the line (or the whole scan) over an undecodable byte
    loses the disc's identity, and "repairing" it invents a folder name that no
    longer matches what the operator sees on the shelf."""
    mangled = 'Am�lie'
    assert mangled == "Am\xe9lie".encode("cp1252").decode("utf-8", errors="replace")

    disc = parse_info(
        [f'CINFO:2,0,"{mangled}"', 'CINFO:32,0,"AMELIE_1080"',
         'TINFO:0,9,0,"2:02:00"'],
        "disc:0")

    assert disc.disc_name == mangled
    assert disc.volume_label == "AMELIE_1080"          # unaffected
    assert disc.titles[0].duration_seconds == 122 * 60  # the scan survived it

    # Control: a name that decoded correctly is passed through the same way.
    assert parse_info(['CINFO:2,0,"Amélie"'], "disc:0").disc_name == "Amélie"


# ==========================================================================
# mkvmerge -J  (JSON)
# ==========================================================================


def test_a_proxy_error_page_is_not_read_as_a_file_with_no_subtitles():
    """An empty subtitle list is a real, common answer — plenty of rips have no
    PGS track — and the pipeline responds to it by recording the title's
    subtitles as done and placing the file. So "I could not read the probe" must
    never collapse into that same answer, or an HTML error page becomes a job
    that goes green with no sidecars and nothing in the log."""
    with pytest.raises(ValueError):          # JSONDecodeError
        parse_identify(PROXY_ERROR_PAGE)
    with pytest.raises(ValueError):
        parse_identify(TRUNCATED_JSON)

    # Control: a file that genuinely carries no subtitle track answers [].
    assert parse_identify(json.dumps({"tracks": [
        {"id": 0, "type": "video", "codec": "MPEG-H/HEVC"}]})) == []


def test_an_unreadable_probe_is_not_read_as_a_file_with_no_video():
    """``None`` from the height probe means "no video track here", and the encode
    path treats an unknown height as not-UHD: HandBrake, no HDR warning, HDR10
    quietly gone. A truncated or non-JSON probe therefore has to raise rather
    than answer None — the pipeline catches that and says so."""
    with pytest.raises(ValueError):
        parse_video_height(TRUNCATED_JSON)
    with pytest.raises(ValueError):
        parse_video_height(PROXY_ERROR_PAGE)

    # Control: a real probe of an audio-only file is the answer None exists for.
    assert parse_video_height(json.dumps({"tracks": [
        {"id": 0, "type": "audio", "properties": {"language": "eng"}}]})) is None


def test_json_that_is_an_array_where_a_probe_document_belongs_is_refused():
    """Some tool wrappers and mirrors answer with a bare array. It parses as
    JSON, so only the shape gives it away. A probe that shrugged and returned a
    zeroed ProbedFile would be worse than a crash: the post-rip length check
    returns early on a duration of 0, so a truncated rip would pass verification
    and be placed."""
    for payload in ("[]", '[{"tracks": []}]', "null"):
        with pytest.raises((AttributeError, TypeError, ValueError)):
            parse_probe(payload)
        with pytest.raises((AttributeError, TypeError, ValueError)):
            parse_identify(payload)

    # Control: the same fields inside a proper document read normally.
    probed = parse_probe(json.dumps({
        "container": {"properties": {"duration": 5_000_000_000}},
        "tracks": [{"id": 0, "type": "video",
                    "properties": {"pixel_dimensions": "3840x2160"}}]}))
    assert (probed.duration_seconds, probed.video_height) == (5, 2160)


def test_a_duration_that_arrives_as_text_is_not_read_as_a_zero_length_rip():
    """mkvmerge reports the container duration as a JSON number of nanoseconds.
    Handed the same value as a string (a wrapper that re-serialises the probe,
    or a future mkvmerge), the arithmetic must not fall back to 0: a rip whose
    length reads as 0 skips the truncation check entirely, which is exactly how
    a half-read disc gets placed in the library."""
    as_text = json.dumps({"container": {"properties": {"duration": "5000000000"}}})

    with pytest.raises((TypeError, ValueError)):
        parse_probe(as_text)

    # Control: a document with no duration at all is a legitimate 0 — raw
    # streams report none — and must still parse.
    assert parse_probe(json.dumps({"container": {"properties": {}}})).duration_seconds == 0


def test_a_track_id_that_arrives_as_a_string_is_still_a_number():
    """The track id is used twice: to pick which track to OCR and to build
    mkvextract's ``<id>:<path>`` spec. A string "3" formats into the spec
    identically but compares equal to nothing, so a selection made by id would
    silently match no track and extract nothing."""
    track = parse_identify(json.dumps({"tracks": [
        {"id": "3", "type": "subtitles", "codec": "HDMV PGS",
         "properties": {"language": "eng"}}]}))[0]

    assert track.track_id == 3
    assert isinstance(track.track_id, int)
    assert track.is_pgs


def test_deeply_nested_json_is_refused_rather_than_answered_with_nothing():
    """Pathological nesting (a mirror that wrapped the document, a fuzzed reply)
    exhausts the JSON decoder's recursion budget. However it fails, it must not
    come back as "no subtitle tracks" — the answer that ends with the job green
    and no sidecars."""
    payload = '{"tracks":' + "[" * 5000 + "]" * 5000 + "}"

    with pytest.raises((RecursionError, ValueError)):
        parse_identify(payload)


# ==========================================================================
# ffprobe -> HDR metadata
# ==========================================================================


async def test_a_proxy_error_page_stops_the_uhd_encode_with_something_to_act_on(
        tmp_path, monkeypatch):
    """An unreadable probe cannot be read as "this file is SDR": that is
    indistinguishable from a genuine SDR source, so the encode drops HDR10 and
    the output check passes vacuously — hours of encoding, silently wrong, no
    log line. The refusal has to name the tool that failed, the file, and what
    the operator gets to do about it (the rip is kept; only the encode re-runs)."""
    calls = _answer_probe_with(monkeypatch, PROXY_ERROR_PAGE)

    with pytest.raises(RuntimeError) as err:
        await _ffmpeg(tmp_path).probe_video(Path("/stage/job_317/t00.mkv"))

    # It refused what ffprobe SAID, having actually run it on this file — not
    # before spawning, which would be a different failure wearing this message.
    assert len(calls) == 1
    assert "ffprobe" in Path(calls[0][0]).name
    assert calls[0][-1] == str(Path("/stage/job_317/t00.mkv"))

    message = str(err.value)
    assert "ffprobe" in message
    assert "t00.mkv" in message                 # which title, not just "a probe"
    assert "SDR" in message                     # what goes wrong if it proceeds
    assert "Retry" in message                   # what the operator does next


async def test_probe_output_that_is_json_but_not_a_probe_never_comes_back_as_sdr(
        tmp_path, monkeypatch):
    """Same rule one layer in: a payload that decodes as JSON but is not an
    ffprobe document (a bare array, pathological nesting) gets past the decode
    and dies in the field walk. Whichever exception that is, the call must not
    return — an empty HdrInfo here is the silent HDR loss above."""
    for payload in ("[1, 2, 3]", "[" * 5000 + "]" * 5000):
        _answer_probe_with(monkeypatch, payload)

        with pytest.raises((AttributeError, TypeError, RecursionError, RuntimeError)):
            await _ffmpeg(tmp_path).probe_video(Path("/stage/job_317/t00.mkv"))

    # Control: the real document answers, so the guard above is about shape.
    _answer_probe_with(monkeypatch, json.dumps(_ffprobe_doc()))
    info, duration = await _ffmpeg(tmp_path).probe_video(Path("/stage/t00.mkv"))
    assert info.is_hdr and duration == pytest.approx(6821.416667)


def test_a_mastering_display_block_missing_one_number_is_dropped_whole():
    """ffprobe prints "N/A" for a mastering-display field it could not read. The
    block is a single x265 argument — G(..)B(..)R(..)WP(..)L(max,min) — so a
    half-filled one is not a partial answer but a wrong one: it would tell the
    encoder the display's peak brightness is something nobody measured, and the
    result is a file that tone-maps wrong on every HDR TV. No block at all is
    recoverable; a fabricated block is baked into the encode."""
    partial = dict(MASTERING_DISPLAY, max_luminance="N/A")

    info = hdr_mod.parse_hdr(_ffprobe_doc(frame_side_data=[partial]))

    assert info.master_display is None
    assert "master-display" not in hdr_mod.x265_params(info)
    assert info.transfer == "smpte2084"      # the rest of the HDR10 tags survive

    # Control: the complete block does reach x265, numerators only.
    complete = hdr_mod.parse_hdr(_ffprobe_doc(frame_side_data=[MASTERING_DISPLAY]))
    assert complete.master_display == (
        "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)")
    assert "master-display=G(13250,34500)" in hdr_mod.x265_params(complete)


def test_content_light_level_needs_both_numbers_or_neither():
    """max-cll is "maxcll,maxfall" — one number is not half of it. A block with
    only one value present would format the missing half into the argument as
    the literal text None, and x265 rejects the whole -x265-params string on it,
    so the encode dies at spawn time instead of shipping without max-cll."""
    one_sided = {"side_data_type": "Content light level metadata",
                 "max_content": 1000}

    info = hdr_mod.parse_hdr(_ffprobe_doc(frame_side_data=[one_sided]))

    assert info.max_cll is None
    assert "max-cll" not in hdr_mod.x265_params(info)

    # Control: both numbers present are carried through as one argument.
    both = dict(one_sided, max_average=400)
    assert hdr_mod.parse_hdr(_ffprobe_doc(frame_side_data=[both])).max_cll == "1000,400"


def test_an_unreadable_dolby_vision_profile_is_not_claimed_as_dolby_vision():
    """The DV profile decides a whole pipeline: extract HEVC, run dovi_tool,
    inject the RPU, remux. A profile field that is not an integer ("8.1", the
    human spelling) must leave the file as plain HDR10 rather than being taken as
    "there is a DV configuration record here, so run the DV path" — that path
    would then run against metadata nobody could read, and its failure mode is an
    empty mux. HDR10 is the disc's real base layer, so the fallback is a correct
    file minus the dynamic layer, and the job log has to say so."""
    unreadable = {"side_data_type": "DOVI configuration record", "dv_profile": "8.1"}

    info = hdr_mod.parse_hdr(_ffprobe_doc(stream_side_data=[unreadable]))

    assert info.dv_profile is None
    assert info.is_dolby_vision is False
    assert info.is_hdr is True
    assert info.summary().startswith("HDR10")

    # Control: a profile that is a number is a Dolby Vision title.
    real = {"side_data_type": "DOVI configuration record", "dv_profile": 8}
    dv = hdr_mod.parse_hdr(_ffprobe_doc(stream_side_data=[real]))
    assert dv.dv_profile == 8 and dv.is_dolby_vision
    assert dv.summary().startswith("Dolby Vision profile 8")


# ==========================================================================
# Notes: sharp edges found while writing this file, deliberately NOT pinned
# here because pinning them would pin behaviour that is wrong.
#
# 1. An unreadable ID is not the same as ID 0. ``_to_int`` answers 0 for
#    anything it cannot parse, and both parsers use it for identity, not just
#    for values: ``TINFO:<junk>,9,0,"0:00:07"`` is filed under title 0 and
#    overwrites the real title 0's duration (measured: 1:47:12 -> 7s), and
#    ``DRV:<junk>,...`` overwrites drive 0's model name. Everywhere else a junk
#    field degrades to "unknown"; here it degrades to "the main feature".
#
# 2. A DRV line is accepted on ``len(fields) >= 6`` and never on its true shape
#    (exactly 7). An unbalanced quote merges two fields, so every field after it
#    shifts left by one and the OS device path is read as the loaded disc name:
#    ``DRV:0,2,999,12,"BD-RE ASUS,"The Nightmare","/dev/sr0"`` lists a drive
#    holding a disc called "/dev/sr0", with an Add button beside it.
#
# 3. A stdout line over 64 KiB never reaches any of this. asyncio's StreamReader
#    limit is 65536 bytes and ``readline()`` raises ValueError past it, which is
#    not caught in the pump, so a tool that emits one very long line (binary
#    noise with no newline in it) loses the whole scan or rip. Were the line to
#    get through, ``_split_robot_line`` guards StopIteration but not csv's own
#    ``Error: field larger than field limit (131072)``.
#
# 4. ``probe_video`` guards the decode, not the walk. Output that decodes but is
#    not an ffprobe document (a bare JSON array, or nesting deep enough to
#    exhaust the decoder) escapes as AttributeError/RecursionError instead of
#    the "the rip is kept, only the encode re-runs" message that path exists to
#    produce. The job still fails safely — it is the operator's instructions
#    that are lost, not the file.
# ==========================================================================
