"""The ffmpeg UHD path, argument by argument.

This path exists for one reason: HandBrake mangles HDR10, so 4K discs go
through ffmpeg+libx265 instead. Every failure here is silent — the file
encodes, plays, and simply looks wrong on an HDR TV months later — so the
command builders and the ``-progress`` parser are pinned flag by flag.
"""

from pathlib import Path

import pytest

from blurayripper.config import (
    AudioSettings,
    EncodeSettings,
    LanguageSettings,
    Settings,
    StorageSettings,
    SubtitleSettings,
    ToolNotFoundError,
    ToolPaths,
)
from blurayripper.services.hdr import HdrInfo
from blurayripper.tools import ffmpeg as ffmpeg_mod
from blurayripper.tools.ffmpeg import FFmpeg, _progress_handler

SRC = Path("/stage/The Disc/title00.mkv")
DST = Path("/out/Movies/The Disc (2024)/The Disc (2024).mkv")

# What a real UHD HDR10 disc probes as.
HDR10 = HdrInfo(
    colorprim="bt2020",
    transfer="smpte2084",
    colormatrix="bt2020nc",
    master_display="G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)",
    max_cll="1000,400",
)
SDR = HdrInfo(colorprim="bt709", transfer="bt709", colormatrix="bt709")
UNTAGGED = HdrInfo()


def _ff(ffmpeg_path: str | None = "/usr/bin/ffmpeg") -> FFmpeg:
    return FFmpeg(Settings(
        tools=ToolPaths(ffmpeg=ffmpeg_path),
        storage=StorageSettings(staging_dir=Path("/stage"), output_dir=Path("/out")),
        encode=EncodeSettings(),
        languages=LanguageSettings(),
        subtitles=SubtitleSettings(),
        audio=AudioSettings(),
    ))


def _val(cmd: list[str], flag: str) -> str:
    """The argv token immediately following ``flag``."""
    return cmd[cmd.index(flag) + 1]


def _encode(hdr=HDR10, *, crf=18, preset="medium", audio_mode="copy", tune_grain=False):
    return _ff().build_encode_command(
        SRC, DST, hdr=hdr, crf=crf, preset=preset,
        audio_mode=audio_mode, tune_grain=tune_grain)


def _hevc(hdr=HDR10, *, crf=18, preset="medium", tune_grain=False):
    return _ff().build_hevc_encode_command(
        SRC, DST, hdr=hdr, crf=crf, preset=preset, tune_grain=tune_grain)


async def _feed(duration: float, lines, on_progress="collect"):
    """Push ``lines`` through the -progress handler; return the events it emitted."""
    events: list = []
    cb = events.append if on_progress == "collect" else on_progress
    handle = _progress_handler(duration, cb)
    for line in lines:
        await handle(line)
    return events


# --------------------------------------------------------------------------
# build_encode_command
# --------------------------------------------------------------------------

def test_an_hdr10_source_tags_the_output_stream_as_hdr10():
    """The container-level colour tags are what a TV reads to switch into HDR
    mode. Drop them and the file still plays — as washed-out SDR."""
    cmd = _encode(HDR10)
    assert _val(cmd, "-color_primaries") == "bt2020"
    assert _val(cmd, "-color_trc") == "smpte2084"
    assert _val(cmd, "-colorspace") == "bt2020nc"


def test_colour_tags_follow_the_source_rather_than_being_hardcoded():
    """A 1080p SDR extra encoded through this path must not be mislabelled as
    BT.2020/PQ — that makes an SDR file render as crushed, dim HDR."""
    cmd = _encode(SDR)
    assert _val(cmd, "-color_primaries") == "bt709"
    assert _val(cmd, "-color_trc") == "bt709"
    assert _val(cmd, "-colorspace") == "bt709"


def test_an_untagged_source_gets_no_empty_colour_flags():
    """When ffprobe reports no colour tags at all, emitting the flags with empty
    values makes ffmpeg exit immediately — the encode dies rather than runs."""
    cmd = _encode(UNTAGGED)
    assert "-color_primaries" not in cmd
    assert "-color_trc" not in cmd
    assert "-colorspace" not in cmd
    assert "" not in cmd
    # ...and nothing HDR-ish leaks into x265-params either.
    assert _val(cmd, "-x265-params") == "hdr-opt=1:repeat-headers=1"


def test_every_hdr10_field_reaches_x265_as_one_argument():
    """x265 only writes the HDR10 SEI when it is handed hdr-opt, the colour
    triple, the mastering display and max-cll together. Splitting the string on
    ':' into separate argv words would make ffmpeg reject the option outright."""
    cmd = _encode(HDR10)
    assert _val(cmd, "-x265-params") == (
        "hdr-opt=1:repeat-headers=1:colorprim=bt2020:transfer=smpte2084:"
        "colormatrix=bt2020nc:"
        "master-display=G(13250,34500)B(7500,3000)R(34000,16000)"
        "WP(15635,16450)L(10000000,50):"
        "max-cll=1000,400")
    # exactly one occurrence, and it is a single token
    assert cmd.count("-x265-params") == 1
    assert len([a for a in cmd if a.startswith("hdr-opt=")]) == 1


def test_repeat_headers_is_set_even_for_a_source_without_hdr_metadata():
    """repeat-headers keeps VPS/SPS/PPS with every IRAP, which is what lets a
    Plex client seek or start mid-stream without a green first second."""
    assert "repeat-headers=1" in _val(_encode(UNTAGGED), "-x265-params")
    assert "repeat-headers=1" in _val(_encode(HDR10), "-x265-params")


def test_grain_tuning_changes_x265_params_and_nothing_else():
    """Grain tuning is a per-job UI toggle. If it leaked into any other flag —
    or silently did nothing — the grain preview the user approved would not be
    what got encoded."""
    off = _encode(HDR10, tune_grain=False)
    on = _encode(HDR10, tune_grain=True)
    assert "tune=grain" not in " ".join(off)
    assert len(on) == len(off)
    differing = [i for i, (a, b) in enumerate(zip(off, on)) if a != b]
    assert differing == [off.index("-x265-params") + 1]
    assert on[differing[0]] == off[differing[0]] + ":tune=grain"


def test_copy_keeps_the_lossless_track_where_aac_re_encodes_it():
    """'copy' is the default because TrueHD/DTS-HD is the reason to keep a UHD
    rip at all. If the two modes ever produced the same argv, the setting would
    be a lie and lossless audio would quietly become 640k AAC (or vice versa)."""
    copy = _encode(HDR10, audio_mode="copy")
    aac = _encode(HDR10, audio_mode="aac")
    assert _val(copy, "-c:a") == "copy"
    assert "-b:a" not in copy          # copy must not carry a bitrate
    assert _val(aac, "-c:a") == "aac"
    assert _val(aac, "-b:a") == "640k"
    # the audio codec is the ONLY thing the mode changes
    i = copy.index("-c:a")
    assert copy[:i] == aac[:i]
    assert copy[i + 2:] == aac[i + 4:]


def test_the_encode_is_ten_bit_whether_or_not_the_source_is_hdr():
    """x265 needs a 10-bit pipeline for HDR10 to mean anything, and 10-bit also
    kills the banding an 8-bit encode of a UHD gradient produces."""
    for hdr in (HDR10, SDR, UNTAGGED):
        assert _val(_encode(hdr), "-pix_fmt") == "yuv420p10le"


def test_chapters_metadata_and_every_audio_track_survive_the_encode():
    """Plex chapter marks and the commentary tracks come from these four map
    flags; losing '0:a?' would ship a film with only its first audio track."""
    cmd = _encode(HDR10)
    maps = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]
    assert maps == ["0:v:0", "0:a?"]   # '?' = optional, so a mute title still encodes
    assert _val(cmd, "-map_chapters") == "0"
    assert _val(cmd, "-map_metadata") == "0"


def test_progress_is_piped_to_stdout_with_the_output_path_last():
    """process.run only reads stdout, so 'pipe:1' is what makes the encode
    report at all — on pipe:2 or with the flag missing the UI sits at 0% for
    the six hours a UHD encode takes. ffmpeg also requires the output last."""
    cmd = _encode(HDR10)
    assert _val(cmd, "-progress") == "pipe:1"
    assert "-nostats" in cmd
    assert cmd[-1] == str(DST)
    assert cmd.index("-progress") > cmd.index("-x265-params")


def test_the_source_is_an_input_and_overwriting_is_pre_agreed():
    """Without -y ffmpeg stops on an interactive 'overwrite?' prompt when a
    resumed job finds a partial file — the encode then hangs forever with no
    output and no error."""
    cmd = _encode(HDR10)
    assert cmd[0] == "/usr/bin/ffmpeg"
    assert "-y" in cmd
    assert _val(cmd, "-i") == str(SRC)
    assert cmd.count(str(SRC)) == 1
    assert cmd.index("-y") < cmd.index("-i")


def test_every_argument_is_a_string_so_the_spawn_cannot_typeerror():
    """crf is an int and the paths are Path objects; a single unconverted value
    makes create_subprocess_exec raise, failing the job before ffmpeg runs."""
    cmd = _encode(HDR10, crf=16, preset="slower")
    assert all(isinstance(a, str) for a in cmd), cmd
    assert _val(cmd, "-crf") == "16"
    assert _val(cmd, "-preset") == "slower"
    assert _val(cmd, "-c:v") == "libx265"


def test_a_missing_ffmpeg_path_is_refused_at_build_time():
    """Better a named ToolNotFoundError than argv[0]='' blowing up inside the
    subprocess layer as a FileNotFoundError nobody can attribute to a setting."""
    with pytest.raises(ToolNotFoundError):
        _ff(None).build_encode_command(
            SRC, DST, hdr=HDR10, crf=18, preset="medium",
            audio_mode="copy", tune_grain=False)
    with pytest.raises(ToolNotFoundError):
        _ff("").build_hevc_encode_command(
            SRC, DST, hdr=HDR10, crf=18, preset="medium", tune_grain=False)


# --------------------------------------------------------------------------
# build_hevc_encode_command (Dolby Vision base layer)
# --------------------------------------------------------------------------

def test_the_dolby_vision_base_layer_keeps_identical_hdr10_signalling():
    """A DV disc is re-encoded video-only through this second builder. If its
    HDR10 flags ever drift from the main builder, DV titles lose HDR10 while
    plain UHD titles keep it — a divergence nobody would notice for months."""
    full = _encode(HDR10)
    raw = _hevc(HDR10)
    for flag in ("-color_primaries", "-color_trc", "-colorspace", "-x265-params",
                 "-pix_fmt", "-c:v"):
        assert _val(raw, flag) == _val(full, flag), flag


def test_the_raw_hevc_stream_carries_no_audio_or_subtitles():
    """dovi_tool injects the RPU into a bare Annex-B elementary stream; anything
    muxed alongside the video makes the injection produce an unplayable file."""
    cmd = _hevc(HDR10)
    assert "-an" in cmd and "-sn" in cmd
    assert "-c:a" not in cmd and "-b:a" not in cmd
    assert "0:a?" not in cmd
    assert _val(cmd, "-map") == "0:v:0"
    assert _val(cmd, "-f") == "hevc"   # raw stream, not a container


def test_the_dolby_vision_encode_reports_progress_too():
    """The DV pass is the longest single step in the pipeline. With no progress
    plumbing there is no way to tell a running encode from a hung one."""
    cmd = _hevc(HDR10)
    assert _val(cmd, "-progress") == "pipe:1"
    assert "-nostats" in cmd
    assert cmd[-1] == str(DST)
    assert cmd.index("-f") < cmd.index("-progress")


def test_grain_tuning_reaches_the_dolby_vision_encode_as_well():
    """The toggle is set once per job; if only the HDR10 builder honoured it, a
    DV title would come out looking different from its preview."""
    off, on = _hevc(HDR10, tune_grain=False), _hevc(HDR10, tune_grain=True)
    assert "tune=grain" not in " ".join(off)
    assert _val(on, "-x265-params") == _val(off, "-x265-params") + ":tune=grain"
    assert len(on) == len(off)


def test_an_untagged_source_gets_no_empty_colour_flags_in_the_hevc_pass():
    """Same crash as the muxed path: empty-valued colour flags abort ffmpeg."""
    cmd = _hevc(UNTAGGED)
    assert "-color_primaries" not in cmd
    assert "-color_trc" not in cmd
    assert "-colorspace" not in cmd
    assert _val(cmd, "-x265-params") == "hdr-opt=1:repeat-headers=1"


# --------------------------------------------------------------------------
# -progress line parser
# --------------------------------------------------------------------------

async def test_the_fraction_is_elapsed_time_over_the_probed_duration():
    """This number is the progress bar and the queue ETA. Anchoring it to the
    ffprobe duration is the only thing that makes it mean 'quarter done'."""
    events = await _feed(3600.0, ["out_time=00:15:00.000000", "progress=continue"])
    assert len(events) == 1
    assert events[0].fraction == 0.25
    assert events[0].state == "working"


async def test_the_fraction_tracks_further_progress_lines():
    """State has to carry across lines — ffmpeg emits one key per line, so a
    handler that forgot the last out_time would report 0% forever."""
    events = await _feed(3600.0, [
        "out_time=00:15:00.000000", "progress=continue",
        "out_time=00:30:00.000000", "progress=continue",
    ])
    assert [e.fraction for e in events] == [0.25, 0.5]


async def test_a_running_encode_never_reports_a_full_bar():
    """ffprobe's duration is an estimate; out_time routinely overshoots it. An
    uncapped fraction reads as 100% (or over) while hours of encoding remain,
    so 'finished' becomes indistinguishable from 'nearly finished'."""
    events = await _feed(100.0, ["out_time=00:02:00.000000", "progress=continue"])
    assert events[0].fraction == 0.999
    assert events[0].state == "working"


async def test_the_end_marker_is_what_completes_the_job():
    """Only progress=end may say done/1.0 — the same elapsed time reported as
    'continue' must stay short of the finish, or a job would be marked complete
    while ffmpeg is still writing the file."""
    lines = ["out_time=00:00:10.000000"]
    working = await _feed(3600.0, lines + ["progress=continue"])
    finished = await _feed(3600.0, lines + ["progress=end"])
    assert working[0].state == "working" and working[0].fraction < 0.01
    assert finished[0].state == "done" and finished[0].fraction == 1.0


async def test_an_unknown_duration_gives_no_fraction_rather_than_a_crash():
    """probe_video returns 0.0 when ffprobe can't read the file. Dividing by it
    would raise inside the stdout reader and kill an otherwise fine encode."""
    events = await _feed(0.0, [
        "out_time=00:15:00.000000", "speed=2.0x", "fps=24.0", "progress=continue"])
    assert len(events) == 1
    assert events[0].fraction is None
    assert events[0].eta_seconds is None
    assert events[0].fps == 24.0      # still reports what it does know


async def test_the_eta_comes_from_the_encode_speed_multiplier():
    """Remaining wall-clock time is remaining-media / speed, not remaining
    media. At 0.5x a UHD encode takes twice the film's runtime, and the queue's
    'ready by' estimate is built on this number."""
    fast = await _feed(3600.0, ["out_time=00:15:00.000000", "speed=2.0x",
                                "progress=continue"])
    slow = await _feed(3600.0, ["out_time=00:15:00.000000", "speed=0.5x",
                                "progress=continue"])
    assert fast[0].eta_seconds == 1350     # 2700s of media left at 2x
    assert slow[0].eta_seconds == 5400     # ...at half speed


async def test_a_stalled_encode_reports_no_eta_instead_of_infinity():
    """ffmpeg prints speed=0x while it warms up or when a read stalls; dividing
    by it would raise ZeroDivisionError and take the encode down with it."""
    events = await _feed(3600.0, ["out_time=00:15:00.000000", "speed=0x",
                                  "progress=continue"])
    assert events[0].eta_seconds is None
    assert events[0].fraction == 0.25      # progress is still reported


async def test_the_frame_rate_reported_before_a_tick_is_attached_to_it():
    """ffmpeg prints fps on its own line ahead of the progress marker. Without
    carrying it forward the UI could never show an encode rate, which is the
    only signal that a worker box is thermally throttling."""
    events = await _feed(3600.0, [
        "progress=continue",             # nothing seen yet
        "fps=23.98", "progress=continue",
        "fps=47.90", "progress=continue",
    ])
    assert [e.fps for e in events] == [None, 23.98, 47.90]


async def test_ffmpeg_start_up_placeholders_do_not_kill_the_encode():
    """For the first second ffmpeg reports N/A for fps, speed and out_time. A
    ValueError escaping the line handler aborts the whole run — this is the
    single most likely line to appear in every encode."""
    events = await _feed(3600.0, [
        "frame=0", "fps=N/A", "bitrate=N/A", "total_size=N/A",
        "out_time=N/A", "speed=N/A", "progress=continue",
    ])
    assert len(events) == 1
    assert events[0].fps is None
    assert events[0].eta_seconds is None
    assert events[0].fraction == 0.0
    assert events[0].state == "working"


async def test_non_key_value_output_is_ignored_without_losing_state():
    """ffmpeg interleaves its banner and warnings with -progress output, and a
    pipe read can hand over a truncated line. Neither may raise, and neither may
    reset progress already collected."""
    events = await _feed(3600.0, [
        "ffmpeg version 7.1.1 Copyright (c) 2000-2024 the FFmpeg developers",
        "", "   ", "out_ti",
        "out_time=00:36:00.000000",
        "[hevc @ 0x7f] Could not find ref with POC 12",
        "progress=continue",
    ])
    assert len(events) == 1
    assert events[0].fraction == 0.6


async def test_only_the_progress_marker_emits_an_event():
    """ffmpeg prints a dozen keys per tick. Emitting on each would multiply
    every job's progress writes to shared Postgres by ten, for no new info."""
    events = await _feed(3600.0, [
        "frame=1234", "fps=23.98", "stream_0_0_q=28.0", "bitrate=15000.0kbits/s",
        "total_size=987654", "out_time_us=60000000", "out_time_ms=60000000",
        "out_time=00:01:00.000000", "dup_frames=0", "drop_frames=0", "speed=1.2x",
    ])
    assert events == []


async def test_progress_lines_are_harmless_with_no_listener_attached():
    """The DV base-layer pass runs encode_hevc without a callback; an
    unconditional call there would crash the Dolby Vision pipeline."""
    events = await _feed(3600.0, [
        "out_time=00:15:00.000000", "speed=2.0x", "fps=24.0",
        "progress=continue", "progress=end",
    ], on_progress=None)
    assert events == []


# --------------------------------------------------------------------------
# availability (decides whether UHD falls back to HandBrake at all)
# --------------------------------------------------------------------------

def test_a_configured_ffmpeg_that_is_not_installed_is_not_available(tmp_path):
    """This flag is what the UHD guard consults. Reporting a missing binary as
    available turns a clean 'install ffmpeg' error into a job that dies at spawn
    time after the rip has already run."""
    real = tmp_path / "ffmpeg"
    real.write_text("#!/bin/sh\n")
    real.chmod(0o755)
    assert _ff(str(real)).available is True
    assert _ff(str(tmp_path / "nope" / "ffmpeg")).available is False
    assert _ff(None).available is False
    assert _ff("").available is False


def test_availability_asks_the_operating_system_rather_than_the_config(monkeypatch):
    """A path that merely exists in settings proves nothing — the worker boxes
    are provisioned separately from the serve box that writes the settings."""
    monkeypatch.setattr(ffmpeg_mod.shutil, "which", lambda p: None)
    assert _ff("/usr/bin/ffmpeg").available is False
    monkeypatch.setattr(ffmpeg_mod.shutil, "which", lambda p: "/usr/bin/ffmpeg")
    assert _ff("/usr/bin/ffmpeg").available is True
