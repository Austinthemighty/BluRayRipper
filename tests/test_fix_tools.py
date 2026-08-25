"""Regressions for the tool wrappers — the layer that spawns MakeMKV,
HandBrake, ffmpeg, mkvmerge and SubtitleEdit and reads what they say back.

Every failure pinned here is a SILENT one: a tool that exits 0 having written
nothing, a parser that files junk under the main feature, a probe binary that is
really the encoder, a child left running after the parent gave up on it. None of
them surfaces as an exception at the time; they surface weeks later as an empty
file in Plex, a 7-second "movie", or a box whose CPU is pegged by a job nobody
is waiting for.

Nothing here spawns a real process or sleeps: the spawn and ``process.run`` are
stubbed so the argv, the streaming and the teardown can be checked exactly.
"""

from __future__ import annotations

import asyncio
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
    load_settings,
)
from blurayripper.tools import eject as eject_mod
from blurayripper.tools import ffmpeg as ffmpeg_mod
from blurayripper.tools import handbrake as handbrake_mod
from blurayripper.tools import process
from blurayripper.tools import subtitleedit as se_mod
from blurayripper.tools.ffmpeg import FFmpeg
from blurayripper.tools.handbrake import HandBrake
from blurayripper.tools.makemkv import _split_robot_line, parse_drives, parse_info
from blurayripper.tools.process import ProcessResult
from blurayripper.tools.subtitleedit import SubtitleEdit


# --------------------------------------------------------------------------
# Shared fixtures: settings and a fake child process
# --------------------------------------------------------------------------

def _settings(tmp_path: Path, **tools) -> Settings:
    return Settings(
        tools=ToolPaths(handbrakecli="HandBrakeCLI", ffmpeg="/usr/bin/ffmpeg",
                        mkvmerge="/usr/bin/mkvmerge", **tools),
        storage=StorageSettings(staging_dir=tmp_path / "stage",
                                output_dir=tmp_path / "out"),
        encode=EncodeSettings(preset_file=tmp_path / "presets.json",
                              preset_name="BluRay UHD"),
        languages=LanguageSettings(keep=["eng"], default="eng"),
        subtitles=SubtitleSettings(),
        audio=AudioSettings(),
    )


class _FakeStdout:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        await asyncio.sleep(0)
        return self._lines.pop(0) if self._lines else b""


class _FakeStderr:
    async def read(self, n: int) -> bytes:
        await asyncio.sleep(0)
        return b""


class _FakeProc:
    """An asyncio subprocess stand-in that remembers whether it was ended."""

    def __init__(self, stdout: list[bytes] | None = None, *, hang: bool = False) -> None:
        self.stdout = _FakeStdout(stdout or [])
        self.stderr = _FakeStderr()
        self.kill_calls = 0
        self.wait_calls = 0
        self._hang = hang

    @property
    def running(self) -> bool:
        return self.kill_calls == 0

    def kill(self) -> None:
        self.kill_calls += 1
        self._hang = False

    async def wait(self) -> int:
        self.wait_calls += 1
        if self._hang:
            await asyncio.Event().wait()  # only kill() releases it
        return 0


def _spawn(monkeypatch, proc: _FakeProc) -> _FakeProc:
    async def fake_exec(*cmd, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return proc


# --------------------------------------------------------------------------
# process.run — never walk away from a live child
# --------------------------------------------------------------------------

async def test_a_raising_line_handler_does_not_leave_the_tool_running(monkeypatch):
    """The real line handlers write progress to Postgres and push SSE, so they
    raise whenever the database is unreachable. run() only ever killed the child
    on timeout and cancellation, so that raise walked out with a full-speed
    HandBrake/makemkvcon still running and nobody holding a reference to it — on
    Windows a dead parent reaps nothing, so the next job was scheduled onto a box
    still burning its CPU on the last one."""
    proc = _spawn(monkeypatch, _FakeProc([b"one\n", b"two\n", b"three\n"]))
    seen: list[str] = []

    def on_line(line: str) -> None:
        seen.append(line)
        if line == "two":
            raise RuntimeError("persist failed: database unreachable")

    with pytest.raises(RuntimeError, match="database unreachable"):
        await process.run(["makemkvcon", "mkv", "disc:0"], on_line=on_line)

    assert proc.kill_calls == 1          # the child was ended...
    assert proc.wait_calls == 1          # ...and reaped, not just signalled
    assert proc.running is False
    assert seen == ["one", "two"]        # and the caller's error is not disguised


async def test_a_zero_timeout_expires_at_once_instead_of_meaning_no_limit(monkeypatch):
    """``if timeout`` read 0 as "no timeout at all", the exact opposite of what
    a caller asking for a zero-second cap means: the tool it meant to bound
    would run forever instead."""
    proc = _spawn(monkeypatch, _FakeProc(hang=True))

    with pytest.raises(TimeoutError, match="exceeded 0s"):
        await process.run(["SubtitleEdit.exe"], timeout=0)

    assert proc.kill_calls == 1
    assert proc.running is False


async def test_a_line_longer_than_the_pipe_limit_costs_that_line_not_the_run(
        monkeypatch):
    """asyncio's StreamReader caps a line at 64 KiB and raises past it. Nothing
    caught that, so one burst of binary noise from a crashing tool took the
    whole scan or rip down with it — including the MSG lines after it that say
    what actually went wrong."""
    reader = asyncio.StreamReader()      # the real reader, with its real limit
    reader.feed_data(b"\xff" * 100_000 + b"\n")
    reader.feed_data(b'MSG:5011,0,1,"Failed to open disc"\n')
    reader.feed_eof()
    proc = _FakeProc()
    proc.stdout = reader                 # type: ignore[assignment]
    _spawn(monkeypatch, proc)
    seen: list[str] = []

    result = await process.run(["makemkvcon", "info", "disc:0"], on_line=seen.append)

    assert result.returncode == 0        # the run completed instead of raising
    assert seen == ['MSG:5011,0,1,"Failed to open disc"']   # the next line survived
    assert any("longer than 64 KiB" in line for line in result.stdout_lines)


# --------------------------------------------------------------------------
# HandBrake.encode — the empty file that reached the library
# --------------------------------------------------------------------------

def _handbrake_run(monkeypatch, *, writes: bytes | None):
    async def fake(cmd, **kwargs):
        if writes is not None:
            out = Path(cmd[cmd.index("-o") + 1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(writes)
        return ProcessResult(0, [], "")

    monkeypatch.setattr(handbrake_mod.process, "run", fake)


@pytest.mark.parametrize("writes", [None, b""], ids=["no file", "zero bytes"])
async def test_an_encode_that_wrote_nothing_is_refused_not_returned(
        monkeypatch, tmp_path, writes):
    """HandBrake exits 0 having written nothing when its preset's audio or
    subtitle selection conflicts. encode() was the only encoder wrapper with no
    output check, so it returned the path anyway: the caller moved the empty
    file into the Plex library, checkpointed the title as placed, and the job
    finished green with no video — and because 'placed' is checkpointed, every
    later Retry skipped the title."""
    _handbrake_run(monkeypatch, writes=writes)
    dst = tmp_path / "out" / "Movies" / "Heat (1995)" / "Heat (1995).mkv"

    with pytest.raises(RuntimeError) as exc:
        await HandBrake(_settings(tmp_path)).encode(tmp_path / "rip.mkv", dst)

    assert str(dst) in str(exc.value)
    assert "Retry" in str(exc.value)     # says how to recover, not just "failed"
    assert not dst.exists() or dst.stat().st_size == 0

    # ...and the same guard must not turn a working encode into a failure.
    _handbrake_run(monkeypatch, writes=b"encoded video")
    assert await HandBrake(_settings(tmp_path)).encode(tmp_path / "rip.mkv", dst) == dst


# --------------------------------------------------------------------------
# ffmpeg — probe resolution and probe output
# --------------------------------------------------------------------------

def _ff(tmp_path: Path, ffmpeg: str = "/usr/bin/ffmpeg") -> FFmpeg:
    s = _settings(tmp_path)
    s.tools.ffmpeg = ffmpeg
    return FFmpeg(s)


def test_a_wrapper_named_anything_else_never_becomes_its_own_ffprobe(
        tmp_path, monkeypatch):
    """ffprobe is derived by replacing 'ffmpeg' in the FILENAME. For a wrapper
    script, a renamed static build or avconv the replace is a no-op, so the
    'sibling' was the ffmpeg binary itself — it exists, so it was returned and
    then run with ffprobe's arguments. The probe failed, the error blamed
    ffprobe, and the perfectly good ffprobe on PATH was never tried."""
    wrapper = tmp_path / "encode-tool"
    wrapper.write_text("#!/bin/sh\n")
    monkeypatch.setattr(ffmpeg_mod.shutil, "which",
                        lambda p: "/opt/bin/ffprobe" if p == "ffprobe" else None)

    assert _ff(tmp_path, str(wrapper))._ffprobe_bin() == "/opt/bin/ffprobe"


def test_with_no_ffprobe_anywhere_the_encoder_is_still_not_offered_as_one(
        tmp_path, monkeypatch):
    """Nothing to run either way, but argv[0] decides what the failure says.
    Naming the wrapper would report a probe that "ran" and produced nonsense;
    naming ffprobe reports the tool that is actually missing."""
    wrapper = tmp_path / "encode-tool"
    wrapper.write_text("#!/bin/sh\n")
    monkeypatch.setattr(ffmpeg_mod.shutil, "which", lambda p: None)

    assert _ff(tmp_path, str(wrapper))._ffprobe_bin() == "ffprobe"


def _probe_stdout(monkeypatch, text: str) -> None:
    async def fake(cmd, **kwargs):
        return ProcessResult(0, text.split("\n"), "")

    monkeypatch.setattr(ffmpeg_mod.process, "run", fake)


@pytest.mark.parametrize("payload", ['[{"codec_type": "video"}]', '"nope"', "42"],
                         ids=["array", "string", "number"])
async def test_probe_output_that_is_json_but_not_a_probe_document_is_actionable(
        monkeypatch, tmp_path, payload):
    """The decode was guarded but the field walk after it was not, so output
    that parses and is not an ffprobe document escaped as a bare AttributeError.
    The job failed either way — what was lost is the one thing the operator
    needs: that the rip is kept and only the encode has to be re-run."""
    _probe_stdout(monkeypatch, payload)

    with pytest.raises(RuntimeError) as exc:
        await _ff(tmp_path).probe_video(Path("/stage/title00.mkv"))

    message = str(exc.value)
    assert "title00.mkv" in message
    assert "the rip is kept" in message
    assert "Retry" in message


async def test_a_probe_document_with_an_unusable_duration_still_yields_its_hdr(
        monkeypatch, tmp_path):
    """The duration only drives the progress fraction, so a malformed one must
    cost the progress bar and nothing else — the colour metadata the encode is
    built from is right there in the same document."""
    _probe_stdout(monkeypatch, json.dumps({
        "streams": [{"codec_type": "video", "color_transfer": "smpte2084",
                     "color_primaries": "bt2020", "color_space": "bt2020nc"}],
        "format": [],          # ffprobe -show_format suppressed / wrong shape
    }))

    info, duration = await _ff(tmp_path).probe_video(Path("/stage/title00.mkv"))

    assert info.is_hdr is True
    assert info.transfer == "smpte2084"
    assert duration == 0.0


async def test_a_frame_extract_that_produced_no_image_is_refused(
        monkeypatch, tmp_path):
    """Seeking past the end of a short clip makes ffmpeg exit 0 with no frame
    written. Returning the path anyway handed the grain preview a file the
    browser cannot load, so the UI showed a broken image and never said which
    side of the A/B failed."""
    async def fake(cmd, **kwargs):
        return ProcessResult(0, [], "")

    monkeypatch.setattr(ffmpeg_mod.process, "run", fake)
    png = tmp_path / "preview" / "grain_on.png"

    with pytest.raises(RuntimeError, match="no frame"):
        await _ff(tmp_path).extract_frame(Path("/stage/on.mp4"), png, at_seconds=9999.0)


async def test_an_hevc_extract_that_produced_nothing_is_refused(
        monkeypatch, tmp_path):
    """This raw stream is what dovi_tool reads the RPU out of. An empty one made
    dovi_tool report a confusing RPU-extraction error instead of the true cause,
    which is that the source video track never came out."""
    async def fake(cmd, **kwargs):
        out = Path(cmd[-1])
        out.write_bytes(b"")             # exits 0, header-only/empty
        return ProcessResult(0, [], "")

    monkeypatch.setattr(ffmpeg_mod.process, "run", fake)
    out = tmp_path / "dovi" / "src.hevc"

    with pytest.raises(RuntimeError, match="no HEVC stream"):
        await _ff(tmp_path).extract_hevc(Path("/stage/title00.mkv"), out)


# --------------------------------------------------------------------------
# SubtitleEdit — help printed on stderr
# --------------------------------------------------------------------------

CLASSIC_USAGE_ON_STDERR = (
    "usage: seconv <pattern> <name-of-format-without-spaces> [<parameters>]\n"
    "   /outputfolder:<folder name>\n"
    "   /encoding:<encoding name>\n"
    "   /overwrite\n"
)


async def test_usage_printed_on_stderr_still_picks_the_dialect_the_tool_accepts(
        monkeypatch, tmp_path):
    """Two entirely different programs ship as "seconv". help_text() asked the
    ProcessResult for a ``stderr_lines`` attribute it has never had (stderr is
    one string), so help printed on stderr always read as "no help" and the
    dialect fell back to the name-based guess. The binary here IS named seconv,
    so that guess picks the --option form — and this build rejects every one of
    them, mid-job, after the extract has already run."""
    settings = load_settings()
    settings.tools.subtitleedit = "/usr/local/bin/seconv"
    settings.tools.subtitleedit_runner = None
    settings.subtitles.dictionary_folder = None
    se = SubtitleEdit(settings)
    spawned: list[list[str]] = []

    async def fake_run(cmd, **kwargs):
        spawned.append(list(cmd))
        if cmd[-1] == "--help":
            return ProcessResult(1, [], CLASSIC_USAGE_ON_STDERR)
        (tmp_path / "t000.srt").write_text("1\n")
        return ProcessResult(0, [], "")

    monkeypatch.setattr(se_mod.process, "run", fake_run)

    await se.ocr_to_srt(Path("/stage/t000.sup"), "eng", tmp_path)

    convert = spawned[-1]
    assert "/encoding:utf-8" in convert          # the dialect stderr advertised
    assert f"/outputfolder:{tmp_path}" in convert
    assert not any(a.startswith("--") for a in convert)


# --------------------------------------------------------------------------
# makemkv — an unreadable id is not id 0
# --------------------------------------------------------------------------

def test_a_junk_title_id_does_not_overwrite_the_main_features_duration():
    """_to_int answered 0 for anything unparseable, and parse_info used it for
    IDENTITY. A truncated or interleaved line therefore filed itself under title
    0 and the last write won: a 1:47:12 main feature came back as 7 seconds and
    dropped under the min-length cut, so the disc looked like it had no feature
    at all."""
    disc = parse_info([
        'TINFO:0,9,0,"1:47:12"',
        'TINFO:0,2,0,"Main Feature"',
        'TINFO:junk,9,0,"0:00:07"',      # the garbage line
    ], "disc:0")

    assert [t.title_id for t in disc.titles] == [0]
    assert disc.titles[0].duration_seconds == 6432
    assert disc.titles[0].name == "Main Feature"


def test_a_junk_drive_index_does_not_rename_a_real_drive():
    """Same fault in parse_drives: a later junk enumeration line indexed to 0 and
    took over the real drive's model name, disc and device — so the drives panel
    offered an Add button for a drive that does not exist."""
    drives = parse_drives([
        'DRV:0,2,999,12,"BD-RE ASUS BW-16D1HT","The Nightmare","/dev/sr0"',
        'DRV:junk,2,999,12,"GARBAGE DRIVE","","/dev/sr9"',
    ])

    assert [d.index for d in drives] == [0]
    assert drives[0].name == "BD-RE ASUS BW-16D1HT"
    assert drives[0].disc_name == "The Nightmare"
    assert drives[0].device == "/dev/sr0"


def test_a_junk_drv_index_does_not_point_auto_eject_at_another_drive():
    """parse_info resolves the OS device to eject from the DRV index. A junk
    index folded onto 0 replaced drive 0's device, so ripping disc:0 would pop
    the tray of a different drive — mid-rip, on the box the user is standing
    at."""
    disc = parse_info([
        'DRV:0,2,999,12,"BD-RE ASUS","The Nightmare","/dev/sr0"',
        'DRV:???,2,999,12,"OTHER DRIVE","Other Disc","/dev/sr9"',
        'TINFO:0,9,0,"1:47:12"',
    ], "disc:0")

    assert disc.drive_device == "/dev/sr0"


def test_an_over_long_robot_field_costs_that_line_not_the_whole_scan():
    """csv refuses a single field longer than 131072 characters. A crashing
    makemkvcon that dumps a buffer hits that, and the csv.Error escaped the
    stdout pump — losing the scan or rip rather than the one bad line."""
    monstrous = '0,0,1,"' + ("y" * 200_000) + '"'

    assert _split_robot_line(monstrous) == []

    # ...and the surrounding scan still produces its titles.
    disc = parse_info([f"MSG:{monstrous}", 'TINFO:0,9,0,"1:47:12"'], "file:/fake")
    assert [t.duration_seconds for t in disc.titles] == [6432]


# --------------------------------------------------------------------------
# eject — the Windows back-end chain
# --------------------------------------------------------------------------

def _stub_eject(monkeypatch, fail=()):
    calls: list[tuple[str, str]] = []

    def make(name):
        def fn(arg):
            calls.append((name, arg))
            if name in fail:
                raise OSError(f"{name} said no")
        return fn

    for name, attr in (("scsi", "_eject_scsi"), ("ioctl", "_eject_ioctl"),
                       ("shell", "_eject_shell"), ("mci", "_eject_mci")):
        monkeypatch.setattr(eject_mod, attr, make(name))
    return calls


def test_a_drive_letter_typed_without_its_colon_still_reaches_every_backend(
        monkeypatch):
    """'F' matched neither the device-path branch nor the ':' branch, so ioctl,
    shell and scsi were all skipped and the bare 'F' went to MCI alone. The
    command exists to try four back-ends against a stubborn drive and reported
    one failure instead."""
    calls = _stub_eject(monkeypatch, fail=("ioctl", "shell", "scsi", "mci"))

    with pytest.raises(OSError):
        eject_mod._eject_windows("F")

    assert calls == [("ioctl", r"\\.\F:"), ("shell", "F:"),
                     ("scsi", r"\\.\F:"), ("mci", "F:")]


def test_the_same_normalisation_reaches_the_close_tray_path(monkeypatch):
    """_eject_windows used to re-implement the hint parsing inline, so a fix
    applied to one copy never reached _close_windows. They share it now."""
    assert eject_mod._win_paths("f") == (r"\\.\f:", "f:")
    assert eject_mod._win_paths(None) == (None, None)
    assert eject_mod._win_paths("") == (None, None)
    assert eject_mod._win_paths(r"\\.\CdRom0") == (r"\\.\CdRom0", None)


def test_ejecting_with_no_drive_hint_uses_mcis_default_device(monkeypatch):
    """MCI's `set CDAudio door open wait` needs no drive name, and it is the one
    back-end that can work when _eject_device_for() returned None. The auto
    chain skipped it on a falsy argument, so `blurayripper eject` with no drive
    printed 'Ejecting default drive …' and then could never succeed on
    Windows."""
    calls = _stub_eject(monkeypatch)

    eject_mod._eject_windows(None)       # must not raise

    assert calls == [("mci", "")]


def test_a_device_path_is_not_handed_to_mci_which_cannot_take_one(monkeypatch):
    r"""MakeMKV can report \\.\CdRom0 instead of a letter. MCI does not accept a
    device path, so passing the raw hint made the last fallback in that case a
    guaranteed failure; its default-device form at least has a chance."""
    calls = _stub_eject(monkeypatch, fail=("ioctl", "scsi"))

    eject_mod._eject_windows(r"\\.\CdRom0")

    assert calls[-1] == ("mci", "")
    assert not any(arg.startswith("\\\\") for name, arg in calls if name == "mci")
