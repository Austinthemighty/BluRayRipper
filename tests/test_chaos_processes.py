"""Chaos at the one door to the OS: the external tools misbehave.

Every tool in this pipeline reaches the outside world through ``process.run``,
so every way a tool can misbehave arrives here first: killed by a signal, dead
with its only words on stderr, exiting 0 having written nothing, hung with the
pipe still open, flooding megabytes, speaking bytes that aren't UTF-8, or simply
not being installed. What the tests below pin is not that an exception escaped —
that is the easy half — but what the SYSTEM does with it:

  * the failure names the tool and carries the tail of its output, because that
    tail is the entire content of the job log an operator reads;
  * a bounded run really is bounded, and the child is killed AND reaped rather
    than left running on a box that will queue the next job onto it;
  * a checkpoint is written only when there is genuinely something to reuse, so
    a retry re-does the failed half and not the expensive half that succeeded;
  * a failed encode gives its scheduler slot back, or the queue quietly loses
    capacity every time a tool dies.

Nothing here spawns a real process or sleeps meaningfully: the child is a fake
object, ``asyncio.create_subprocess_exec`` is replaced, and the two tests that
must observe a real timeout use a 20-50 ms one or a stubbed ``wait_for``.

The tool output used as fixtures is verbatim from the production cluster
(``real_logs.txt``): the drive that dropped off the bus mid-rip, the NAS write
that died, and the seconv build that wanted a .NET runtime the box didn't have.
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
)
from blurayripper.models import DiscInfo, EpisodeAssignment, MediaKind, Title
from blurayripper.pipeline import Job, Pipeline
from blurayripper.scheduler import EncodeScheduler
from blurayripper.tools import process
from blurayripper.tools.makemkv import MakeMKV
from blurayripper.tools.mkvtools import MkvTools
from blurayripper.tools.process import ProcessError
from blurayripper.tools.subtitleedit import SubtitleEdit


# --------------------------------------------------------------------------
# Real tool output (verbatim from the production cluster)
# --------------------------------------------------------------------------

DRIVE_DROPPED = [
    b"MSG:2004,0,1,\"Error 'Posix error - No such device' occurred while reading "
    b"'/BDMV/STREAM/00001.m2ts' at offset '0'\"\n",
    b'MSG:2003,0,1,"Title #00100.mpls was skipped"\n',
    b'MSG:5010,0,0,"Failed to open disc"\n',
]

BD_JAVA_NO_JRE = [
    b'MSG:1002,0,1,"This disc requires Java runtime (JRE), but none was found. '
    b'Certain functions will fail, please install Java."\n',
    b'MSG:5005,0,0,"Operation successfully completed"\n',
]

SECONV_NEEDS_DOTNET = (
    b"You must install or update .NET to run this application.\n"
    b"App: /opt/brr-tools/seconv-bin/seconv.dll\n"
    b"Framework: 'Microsoft.NETCore.App', version '8.0.0' (x64)\n"
    b"The following frameworks were found:\n"
    b"  10.0.10 at [/usr/lib/dotnet/shared/Microsoft.NETCore.App]\n"
)


# --------------------------------------------------------------------------
# A fake child process
# --------------------------------------------------------------------------

class _Stdout:
    def __init__(self, lines, *, never_ends: bool = False, hook=None) -> None:
        self._lines = list(lines)
        self._never_ends = never_ends
        self._hook = hook

    async def readline(self) -> bytes:
        await asyncio.sleep(0)  # yield, so a timeout/cancel can land mid-stream
        if self._hook is not None:
            hook, self._hook = self._hook, None
            hook()
        if self._lines:
            return self._lines.pop(0)
        if self._never_ends:
            # The child is alive with its pipe open and nothing more to say —
            # what a tool stuck on a modal dialog or a dead NAS handle looks
            # like. Only a kill from outside ends this.
            await asyncio.Event().wait()
        return b""


class _Stderr:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self, n: int) -> bytes:
        await asyncio.sleep(0)
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk


class _Child:
    """Stands in for an asyncio subprocess, recording how it was torn down."""

    def __init__(self, *, returncode: int = 0, stdout=(), stderr: bytes = b"",
                 never_ends: bool = False, hook=None) -> None:
        self.stdout = _Stdout(stdout, never_ends=never_ends, hook=hook)
        self.stderr = _Stderr(stderr)
        self._returncode = returncode
        self.kill_calls = 0
        self.waits = 0
        self.waits_after_kill = 0

    def kill(self) -> None:
        self.kill_calls += 1
        self.stdout._never_ends = False  # the pipe closes when the child dies

    async def wait(self) -> int:
        self.waits += 1
        if self.kill_calls:
            self.waits_after_kill += 1
        return self._returncode


def _spawn(monkeypatch, child):
    """Replace the real spawn. ``child`` may be a _Child (reused for every
    spawn) or a zero-arg factory. Returns the list of (argv, kwargs) calls."""
    calls: list[tuple[list[str], dict]] = []
    make = child if callable(child) else (lambda: child)

    async def fake_exec(*cmd, **kwargs):
        calls.append(([*cmd], kwargs))
        return make()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return calls


def _arm_spy(monkeypatch, *, run_with: float | None = None):
    """Record every timeout armed via ``asyncio.wait_for``. With ``run_with`` the
    real wait_for is called with that (tiny) timeout instead, so a bound of 1800
    seconds can be observed without waiting 1800 seconds."""
    armed: list[float | None] = []
    real = asyncio.wait_for

    async def spy(awaitable, timeout):
        armed.append(timeout)
        return await real(awaitable, timeout if run_with is None else run_with)

    monkeypatch.setattr(asyncio, "wait_for", spy)
    return armed


class _Report:
    """The reporter shape the pipeline talks to, plus a checkpoint spy."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.progress: list[tuple[str, float | None]] = []
        self.checkpoints = 0

    def on_event(self, message: str) -> None:
        self.events.append(message)

    def on_progress(self, stage: str, fraction=None) -> None:
        self.progress.append((stage, fraction))

    def checkpoint(self) -> None:
        self.checkpoints += 1


def _settings(tmp_path: Path, **over) -> Settings:
    s = Settings(
        tools=ToolPaths(
            makemkvcon="makemkvcon", handbrakecli="HandBrakeCLI", mkvmerge="mkvmerge",
            mkvextract="mkvextract", subtitleedit="seconv", ffmpeg="ffmpeg",
        ),
        storage=StorageSettings(staging_dir=tmp_path / "stage",
                                output_dir=tmp_path / "out"),
        encode=EncodeSettings(container="mkv", preset_file=tmp_path / "presets.json",
                              preset_name="Plex H265 1080p"),
        languages=LanguageSettings(keep=["eng"], default="eng"),
        subtitles=SubtitleSettings(),
        audio=AudioSettings(),
    )
    s.auto_eject = False
    s.write_nfo = False
    for key, value in over.items():
        setattr(s, key, value)
    return s


# ==========================================================================
# The runner itself, under chaos
# ==========================================================================

async def test_a_tool_killed_by_a_signal_is_a_failure_not_a_finished_run(monkeypatch):
    """A child killed by SIGKILL (the OOM killer on the encode box, or our own
    teardown) reports a NEGATIVE exit status, not a positive one. A check that
    only looked for "exited > 0" would wave it through, and the job would go on
    to encode whatever half-written file the dead tool left behind."""
    _spawn(monkeypatch, _Child(returncode=-9, stdout=DRIVE_DROPPED))

    with pytest.raises(ProcessError) as excinfo:
        await process.run(["makemkvcon", "-r", "mkv", "disc:0", "0", "/stage"])

    assert excinfo.value.result.returncode == -9
    text = str(excinfo.value)
    assert "makemkvcon" in text            # which of the five tools died
    assert "-9" in text                    # and how
    assert "Failed to open disc" in text   # its last words, verbatim, for the log


async def test_a_failure_that_spoke_only_on_stderr_still_says_what_happened(monkeypatch):
    """The seconv build compiled for .NET 8 on a box with only 10.x. It exits
    non-zero having printed NOTHING to stdout — every word of the diagnosis is
    on stderr. Reporting only the stdout tail would give the operator a bare
    exit code for a failure whose fix is written out in full."""
    _spawn(monkeypatch, _Child(returncode=150, stdout=[], stderr=SECONV_NEEDS_DOTNET))

    with pytest.raises(ProcessError) as excinfo:
        await process.run(["seconv", "x.sup", "--format", "subrip"])

    text = str(excinfo.value)
    assert "seconv" in text
    assert "You must install or update .NET to run this application." in text
    assert "Framework: 'Microsoft.NETCore.App', version '8.0.0' (x64)" in text
    assert "10.0.10 at [/usr/lib/dotnet/shared/Microsoft.NETCore.App]" in text


async def test_megabytes_of_output_are_neither_lost_nor_dumped_into_the_job_log(
        monkeypatch):
    """HandBrake and makemkvcon emit progress every frame; a wedged tool can
    emit hundreds of thousands of lines before it dies. Two things must hold at
    once: nothing is dropped on the way to the progress callback (a dropped
    line is a stalled progress bar), and the failure message stays small enough
    to be a log entry — the job log is rendered in a browser."""
    lines = [f"PRGV:{i},1000,1000\n".encode() for i in range(20_000)]
    stderr = b"x" * 400_000 + b"REAL_ERROR: disk full"
    _spawn(monkeypatch, _Child(returncode=1, stdout=lines, stderr=stderr))
    seen: list[str] = []

    with pytest.raises(ProcessError) as excinfo:
        await process.run(["HandBrakeCLI"], on_line=seen.append)

    assert len(seen) == 20_000                    # every line was delivered
    assert seen[0] == "PRGV:0,1000,1000"
    assert seen[-1] == "PRGV:19999,1000,1000"     # ...in order, to the end
    text = str(excinfo.value)
    assert len(text) < 4_000                      # ~420 KB in, a log line out
    assert "PRGV:19999,1000,1000" in text         # the useful end survived
    assert "REAL_ERROR: disk full" in text        # on both streams


async def test_a_tool_that_hangs_with_its_pipe_open_is_killed_and_reaped(monkeypatch):
    """The commonest hang is not a closed pipe: the child is alive, holding its
    stdout open, saying nothing (SubtitleEdit on a modal dialog, mkvextract on a
    dropped CIFS handle). Waiting on EOF would wait forever. The timeout has to
    fire, the child has to be killed — and then WAITED ON, or on Linux it stays
    a zombie and on Windows the job object never closes."""
    child = _Child(never_ends=True)
    _spawn(monkeypatch, child)

    with pytest.raises(TimeoutError) as excinfo:
        await process.run(["SubtitleEdit.exe", "/convert"], timeout=0.05)

    assert child.kill_calls == 1
    assert child.waits_after_kill == 1  # reaped, not just signalled
    assert "SubtitleEdit.exe" in str(excinfo.value)
    assert "killed" in str(excinfo.value)


async def test_the_timeout_message_tells_the_operator_how_long_it_waited(monkeypatch):
    """"seconv exceeded 3600s and was killed" is the whole story of that job in
    one line: which tool, and that it was OUR watchdog that ended it rather than
    the tool failing. Without the number, an operator cannot tell a genuinely
    slow 4K OCR from a hang, or know which setting to raise."""
    child = _Child(never_ends=True)
    _spawn(monkeypatch, child)

    async def instant_timeout(awaitable, timeout):
        awaitable.close()  # never awaited: don't leave a dangling coroutine
        raise asyncio.TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", instant_timeout)

    with pytest.raises(TimeoutError) as excinfo:
        await process.run(["seconv", "big.sup"], timeout=3600)

    assert str(excinfo.value) == "seconv exceeded 3600s and was killed"
    assert child.kill_calls == 1
    assert child.waits_after_kill == 1


async def test_a_binary_that_is_not_installed_can_never_look_like_a_result(monkeypatch):
    """A path in settings.toml that points at a tool this box doesn't have (the
    Windows worker's HandBrake path on the Linux serve box) fails at spawn, not
    at exit. There is no exit code and no output, so there is no ProcessResult
    to return — including for the ``check=False`` callers, which would otherwise
    read ``returncode`` off a run that never started."""
    async def missing(*cmd, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", cmd[0])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing)

    with pytest.raises(FileNotFoundError) as excinfo:
        await process.run(["C:\\HandBrakeCLI.exe", "-i", "x"], check=False)

    assert excinfo.value.filename == "C:\\HandBrakeCLI.exe"


async def test_a_line_handler_that_blows_up_does_not_pass_as_a_finished_run(monkeypatch):
    """The real line handlers persist progress and push SSE events, so they
    touch Postgres and the network — both of which have gone away mid-encode on
    this cluster. If the runner swallowed that, ``run`` would return a
    ProcessResult for a tool that is still running, and the caller would go
    place a file that isn't finished. The lines already consumed still count:
    they are what the parser has, and the failure must not rewind them."""
    _spawn(monkeypatch, _Child(stdout=[b"a\n", b"b\n", b"c\n", b"d\n"]))
    seen: list[str] = []

    def handler(line: str) -> None:
        seen.append(line)
        if len(seen) == 3:
            raise RuntimeError("persist failed: database unreachable")

    with pytest.raises(RuntimeError) as excinfo:
        await process.run(["HandBrakeCLI"], on_line=handler)

    assert not isinstance(excinfo.value, ProcessError)  # not dressed up as a tool failure
    assert str(excinfo.value) == "persist failed: database unreachable"
    assert seen == ["a", "b", "c"]
    await asyncio.sleep(0)  # let the sibling stderr pump finish


# ==========================================================================
# MakeMKV's call site
# ==========================================================================

async def test_a_rip_killed_mid_write_never_returns_the_partial_file(monkeypatch, tmp_path):
    """makemkvcon writes progressively, so a rip that dies partway leaves a real
    .mkv on disk. ``rip_title`` reports whatever appeared in the destination
    while it ran — which is exactly that partial file. The only thing standing
    between it and the encode queue is the failure being raised at all."""
    dest = tmp_path / "rip" / "t000"
    dest.mkdir(parents=True)

    def write_partial() -> None:
        (dest / "title_t00.mkv").write_bytes(b"half a movie")

    _spawn(monkeypatch, _Child(returncode=-9, stdout=DRIVE_DROPPED, hook=write_partial))
    messages: list[str] = []

    with pytest.raises(ProcessError) as excinfo:
        await MakeMKV(_settings(tmp_path)).rip_title(
            "disc:0", 0, dest, on_message=messages.append)

    assert (dest / "title_t00.mkv").exists()      # the partial really is there
    assert "makemkvcon" in str(excinfo.value)     # ...and it was never returned
    assert "Failed to open disc" in str(excinfo.value)
    assert messages[-1] == "Failed to open disc"  # forwarded to the log as it happened


async def test_a_disc_label_in_a_broken_encoding_does_not_fail_the_rip(monkeypatch, tmp_path):
    """MakeMKV echoes disc labels in whatever encoding the disc carries, and
    plenty of European discs are not UTF-8. Decoding strictly would abort a rip
    that is otherwise perfect, at whatever point the title happened to be
    mentioned — which is early, so the whole disc is lost to a mojibake."""
    dest = tmp_path / "rip" / "t000"

    def write_output() -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "title_t00.mkv").write_bytes(b"a real rip")

    _spawn(monkeypatch, _Child(stdout=[
        b'MSG:3007,0,2,"Saving title from disc L\xe9on"\n',
        b'MSG:5005,0,0,"1 titles saved"\n',
    ], hook=write_output))
    messages: list[str] = []

    produced = await MakeMKV(_settings(tmp_path)).rip_title(
        "disc:0", 0, dest, on_message=messages.append)

    assert produced == [dest / "title_t00.mkv"]   # the rip completed
    assert messages == ["Saving title from disc L\ufffdon", "1 titles saved"]


async def test_a_rip_that_exits_zero_having_saved_nothing_is_not_checkpointed(
        monkeypatch, tmp_path):
    """A BD-Java disc with no JRE installed: makemkvcon warns, then reports
    "Operation successfully completed" and exits 0 with an empty output folder.
    Nothing failed, in its own telling. If that were checkpointed, the resume
    would "reuse the ripped file" forever and every retry would encode nothing;
    the checkpoint must only record a file that actually exists."""
    settings = _settings(tmp_path)
    pipe = Pipeline(settings)
    _spawn(monkeypatch, _Child(returncode=0, stdout=BD_JAVA_NO_JRE))

    job = Job(source="disc:0")
    job.disc = DiscInfo(source="disc:0", disc_name="X",
                        titles=[Title(title_id=0, duration_seconds=6000, size_bytes=1)])
    job.selected_title_ids = [0]
    report = _Report()

    with pytest.raises(RuntimeError, match="produced no output for title 0"):
        await pipe._ensure_ripped(job, 0, settings.storage.staging_dir / "job_1", report)

    assert "ripped_path" not in job.checkpoints.get(0, {})
    assert report.checkpoints == 0   # nothing was persisted as reusable
    assert any("Java runtime" in e for e in report.events)  # the reason is in the log


# ==========================================================================
# MKVToolNix's call site
# ==========================================================================

async def test_a_wedged_probe_is_bounded_and_the_child_is_killed_not_orphaned(
        monkeypatch, tmp_path):
    """mkvmerge reading a file on a CIFS share that has gone away blocks in the
    kernel and never returns. The wrapper bounds it — but a bound that only
    stops WAITING leaves mkvmerge pinning a NAS handle for the life of the box,
    and a worker collects one of those per job until it dies. Cancelling the run
    has to take the child with it."""
    child = _Child(never_ends=True)
    _spawn(monkeypatch, child)
    armed = _arm_spy(monkeypatch, run_with=0.02)

    with pytest.raises(asyncio.TimeoutError):
        await MkvTools(_settings(tmp_path)).probe(tmp_path / "t.mkv")

    assert armed == [120]                # the bound the wrapper asked for
    assert child.kill_calls == 1         # ...and the child it took down with it
    assert child.waits_after_kill == 1


async def test_a_track_name_in_a_broken_encoding_still_identifies_the_track(
        monkeypatch, tmp_path):
    """mkvmerge -J is JSON, and the track names inside it come from whatever the
    ripper wrote. One undecodable byte in a track title must not cost the whole
    probe — the subtitle tracks it lists are how the pipeline knows there are
    subtitles to extract at all, so losing the probe silently loses the subs."""
    payload = (
        b'{"container":{"properties":{"duration":7200000000000}},"tracks":['
        b'{"id":0,"type":"video","properties":{"pixel_dimensions":"1920x1080"}},'
        b'{"id":2,"type":"subtitles","codec":"HDMV PGS",'
        b'"properties":{"language":"eng","track_name":"Fran\xe7ais"}}]}'
    )
    _spawn(monkeypatch, _Child(stdout=[payload + b"\n"]))

    probed = await MkvTools(_settings(tmp_path)).probe(tmp_path / "t.mkv")

    assert probed.duration_seconds == 7200
    assert probed.video_height == 1080
    assert [t.track_id for t in probed.subtitles] == [2]
    assert probed.subtitles[0].is_pgs is True
    assert probed.subtitles[0].name == "Fran\ufffdais"


async def test_a_missing_mkvmerge_fails_the_title_without_a_subtitle_checkpoint(
        monkeypatch, tmp_path):
    """MKVToolNix installed on the rip box but not on a worker: the path is
    configured, so nothing complains until the first extract, which fails at
    spawn. Extraction returning "nothing" here is indistinguishable from a disc
    with no subtitles — and a title recorded as having no subtitles is recorded
    as DONE, so every later resume skips it and the subtitles are lost for good.
    It has to fail loudly, and leave no checkpoint behind to reuse."""
    async def missing(*cmd, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", cmd[0])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing)
    settings = _settings(tmp_path)
    pipe = Pipeline(settings)
    job = Job(source="disc:0")
    report = _Report()

    with pytest.raises(RuntimeError) as excinfo:
        await pipe._extract_title_subs(
            job, 7, tmp_path / "t.mkv", tmp_path / "sub", report)

    assert "Subtitle extraction failed for title 7" in str(excinfo.value)
    assert "retry" in str(excinfo.value).lower()
    assert "subs" not in job.checkpoints.get(7, {})
    assert pipe._reuse_subs(job.checkpoints.get(7, {})) is None
    assert report.checkpoints == 0
    assert any("mkvmerge" in e for e in report.events)  # says WHICH tool is missing


# ==========================================================================
# SubtitleEdit's call site
# ==========================================================================

def _se(tmp_path: Path, **subs) -> tuple[SubtitleEdit, Path, Path]:
    settings = _settings(tmp_path)
    for key, value in subs.items():
        setattr(settings.subtitles, key, value)
    sup = tmp_path / "in" / "t.eng.sup"
    sup.parent.mkdir(parents=True, exist_ok=True)
    sup.write_bytes(b"pgs")
    return SubtitleEdit(settings), sup, tmp_path / "srt"


async def test_a_hung_ocr_is_killed_at_the_configured_per_track_timeout(
        monkeypatch, tmp_path):
    """SubtitleEdit stops on a modal error dialog and waits for a click that
    will never come on a headless box. Per-track OCR is the one place with a
    configurable watchdog, and the number an operator set in Settings is the
    number that must reach the runner — not a default, and not "no timeout"."""
    se, sup, out_dir = _se(tmp_path, ocr_timeout_seconds=1800)
    children = []

    def make_child():
        children.append(_Child(never_ends=True))
        return children[-1]

    _spawn(monkeypatch, make_child)
    armed = _arm_spy(monkeypatch, run_with=0.02)

    with pytest.raises(TimeoutError) as excinfo:
        await se.ocr_to_srt(sup, "eng", out_dir)

    # The --help probe is bounded at 60s; the OCR itself at the configured 1800.
    assert armed[-1] == 1800
    assert "exceeded 1800s and was killed" in str(excinfo.value)
    assert "seconv" in str(excinfo.value)
    assert children[-1].kill_calls == 1
    assert children[-1].waits_after_kill == 1


async def test_a_zero_ocr_timeout_leaves_a_long_ocr_alone(monkeypatch, tmp_path):
    """0 means off, and off has to mean no watchdog at all. A UHD disc's SDH
    track legitimately OCRs for over an hour; an accidental default would kill
    it every time, and the job would never get past its subtitles."""
    se, sup, out_dir = _se(tmp_path, ocr_timeout_seconds=0)
    _spawn(monkeypatch, lambda: _Child(stdout=[b"Converted 1 file(s)\n"]))
    armed = _arm_spy(monkeypatch)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "t.eng.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")

    produced = await se.ocr_to_srt(sup, "eng", out_dir)

    assert produced == out_dir / "t.eng.srt"
    assert armed == [60]  # the --help probe only; the OCR ran unbounded


async def test_an_ocr_that_died_on_stderr_names_the_real_problem(monkeypatch, tmp_path):
    """This one cost an afternoon: seconv wanted a .NET runtime the box didn't
    have, so it exited before OCR'ing anything. The downstream check ("no .srt
    appeared") would blame the Tesseract language data and send the operator to
    install language packs that were already there. The tool's own exit must be
    what surfaces, with its stderr attached."""
    se, sup, out_dir = _se(tmp_path)
    _spawn(monkeypatch, lambda: _Child(returncode=150, stderr=SECONV_NEEDS_DOTNET))

    with pytest.raises(ProcessError) as excinfo:
        await se.ocr_to_srt(sup, "eng", out_dir)

    text = str(excinfo.value)
    assert "You must install or update .NET" in text
    assert "seconv" in text
    assert "language data" not in text  # not the misleading downstream guess


async def test_an_ocr_that_exits_zero_with_no_srt_says_what_to_check(
        monkeypatch, tmp_path):
    """SubtitleEdit's GUI batch mode exits 0 having done nothing when the OCR
    language was never picked in the GUI — its batch tool has no language flag.
    The message has to name the file that should have appeared and the Tesseract
    language pack in Tesseract's own spelling: the disc says "ger", Tesseract
    calls it "deu", and looking for a "ger" pack finds nothing forever."""
    settings = _settings(tmp_path)
    settings.tools.subtitleedit = r"C:\Program Files\SubtitleEdit\SubtitleEdit.exe"
    sup = tmp_path / "in" / "t.ger.sup"
    sup.parent.mkdir(parents=True, exist_ok=True)
    sup.write_bytes(b"pgs")
    out_dir = tmp_path / "srt"
    _spawn(monkeypatch, lambda: _Child(returncode=0, stdout=[b"Done\n"]))

    with pytest.raises(FileNotFoundError) as excinfo:
        await SubtitleEdit(settings).ocr_to_srt(sup, "ger", out_dir)

    text = str(excinfo.value)
    assert str(out_dir / "t.ger.srt") in text
    assert "'deu'" in text                       # Tesseract's name, not the disc's
    assert "SubtitleEdit GUI" in text            # the GUI-only hint, for a GUI build


async def test_an_srt_the_tool_wrote_and_then_removed_is_not_reported_as_produced(
        monkeypatch, tmp_path):
    """SubtitleEdit writes its output, then deletes it on the way out when the
    conversion is rejected after the fact (and a staging cleanup racing the job
    looks identical). The path would still be returned, the sidecar recorded as
    placed, and the title marked done with no subtitle on disk. Checking that
    the file is there at the moment it is claimed is the only defence."""
    se, sup, out_dir = _se(tmp_path)
    srt = out_dir / "t.eng.srt"
    keep = {"file": True}

    async def fake_run(cmd, **kwargs):
        if cmd[-1] == "--help":
            return process.ProcessResult(0, ["--ocr-engine tesseract"], "")
        out_dir.mkdir(parents=True, exist_ok=True)
        srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
        if not keep["file"]:
            srt.unlink()          # ...and then took it away again
        return process.ProcessResult(0, [], "")

    monkeypatch.setattr(process, "run", fake_run)

    # Control: the same run that keeps its output does return the path, so the
    # check below is a real check and not an unconditional failure.
    assert await se.ocr_to_srt(sup, "eng", out_dir) == srt

    keep["file"] = False
    with pytest.raises(FileNotFoundError, match="did not produce expected SRT"):
        await se.ocr_to_srt(sup, "eng", out_dir)


async def test_a_hung_ocr_costs_the_track_not_the_extraction(monkeypatch, tmp_path):
    """The whole point of checkpointing the extracted .sup images separately:
    when OCR times out, the multi-GB read off the NAS that produced those images
    must not be thrown away with it, and the title must NOT be recorded as
    "subtitles done" — that record is what makes every later resume skip OCR.
    A retry should re-OCR and nothing more."""
    settings = _settings(tmp_path)
    settings.subtitles.ocr_timeout_seconds = 900
    pipe = Pipeline(settings)
    _spawn(monkeypatch, lambda: _Child(never_ends=True))
    _arm_spy(monkeypatch, run_with=0.02)

    sup = settings.storage.staging_dir / "sub" / "t.eng.sup"
    sup.parent.mkdir(parents=True, exist_ok=True)
    sup.write_bytes(b"pgs")
    from blurayripper.tools.mkvtools import SubtitleTrack
    track = SubtitleTrack(track_id=2, codec="HDMV PGS", language="eng", is_pgs=True)

    job = Job(source="disc:0")
    cp = job.checkpoints.setdefault(3, {})
    cp["subs"] = [{"sup": "sub/t.eng.sup", "track_id": 2, "codec": "HDMV PGS",
                   "language": "eng", "forced": False, "sdh": False, "name": None}]
    report = _Report()

    ocr = await pipe._ocr_all(
        job, [(3, None, None, sup.parent, [(sup, track)])], report)
    result = type("R", (), {"subtitle_paths": []})()
    pipe._record_if_complete(cp, [(sup, track)], ocr[3], result, report)

    assert ocr[3] == []                                   # the track was dropped
    # Named in full: it was OUR watchdog that ended it, at the configured bound,
    # and the language it belonged to — not some other error passing through.
    assert any("Subtitle OCR failed (eng): TimeoutError: seconv exceeded 900s"
               in e for e in report.events)
    assert "subs_done" not in cp                          # so a resume re-OCRs
    assert pipe._subs_already_done(cp) is None
    assert pipe._reuse_subs(cp) == [(sup, track)]         # ...but re-extracts nothing


# ==========================================================================
# HandBrake's call site
# ==========================================================================

async def test_an_encode_killed_by_a_signal_places_nothing_and_frees_its_slot(
        monkeypatch, tmp_path):
    """Two failures in one: a killed encode must not leave a half-written file
    in the Plex library or a "placed" checkpoint that makes the resume skip the
    title — and it must give its weight back to the scheduler. The budget is how
    the cluster avoids running two 4K encodes at once; leaking a slot per failed
    encode silently shrinks it until nothing is allowed to start at all."""
    settings = _settings(tmp_path)
    settings.encode.preset_file.write_text(json.dumps(
        {"PresetList": [{"PresetName": "Plex H265 1080p", "VideoEncoder": "x265",
                         "VideoPreset": "slow", "VideoQualitySlider": 20}]}))
    pipe = Pipeline(settings)

    async def height(_path):
        return 1080

    monkeypatch.setattr(pipe.mkvtools, "video_height", height)
    _spawn(monkeypatch, _Child(
        returncode=-9,
        stdout=[b'Progress: {\n', b' "State": "WORKING",\n',
                b' "Working": { "Progress": 0.31 }\n', b'}\n'],
        stderr=b"[15:04:11] hb_error: reader: lseek failed\n"))

    job = Job(source="disc:0")
    assignment = EpisodeAssignment(title_id=0, kind=MediaKind.MOVIE,
                                   show_or_movie_title="Heat", year=1995)
    job.assignments = {0: assignment}
    scheduler = EncodeScheduler(3)
    report = _Report()

    with pytest.raises(ProcessError) as excinfo:
        await pipe._encode_title(job, 0, assignment, tmp_path / "stage",
                                 tmp_path / "t.mkv", scheduler, report)

    assert "HandBrakeCLI" in str(excinfo.value)
    assert "hb_error: reader: lseek failed" in str(excinfo.value)
    assert scheduler._in_use == 0                      # the slot came back
    assert "placed" not in job.checkpoints.get(0, {})  # a retry re-encodes
    assert not pipe._video_path_for(assignment).exists()
    assert report.progress and report.progress[0][1] == 0.31  # it did report before dying
