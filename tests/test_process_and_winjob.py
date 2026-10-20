"""The subprocess plumbing every tool in the pipeline sits on.

``process.run`` is the single door to the OS: MakeMKV, HandBrake, ffmpeg,
mkvmerge and SubtitleEdit all reach the outside world through it. Its three
jobs are streaming stdout to a progress callback, capturing enough of a failed
run's output to diagnose it from the job log, and never leaving a child alive
after the parent gives up on it. ``winjob`` is the Windows-only backstop for
that last one when the parent dies outright. The HandBrake pieces covered here
are the ones that only execute while HandBrakeCLI is actually running.

Nothing here spawns a real process: ``asyncio.create_subprocess_exec`` and
``process.run`` are stubbed so the argv, the streaming and the teardown can be
checked exactly.
"""

from __future__ import annotations

import asyncio
import ctypes
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
from blurayripper.tools import handbrake as handbrake_mod
from blurayripper.tools import process, winjob
from blurayripper.tools.handbrake import HandBrake, HandBrakeJsonParser, _progress_from_block
from blurayripper.tools.process import ProcessError, ProcessResult


# --------------------------------------------------------------------------
# Fake child process
# --------------------------------------------------------------------------

class _FakeStdout:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        await asyncio.sleep(0)  # yield, so cancellation can land mid-stream
        return self._lines.pop(0) if self._lines else b""


class _FakeStderr:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.read_sizes: list[int] = []

    async def read(self, n: int) -> bytes:
        await asyncio.sleep(0)
        self.read_sizes.append(n)
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk


class _FakeProc:
    """Stands in for an asyncio subprocess. ``hang`` makes wait() never return
    until kill() is called, which is how a stuck tool behaves."""

    def __init__(self, *, returncode: int = 0, stdout: list[bytes] | None = None,
                 stderr: bytes = b"", hang: bool = False) -> None:
        self.stdout = _FakeStdout(stdout or [])
        self.stderr = _FakeStderr(stderr)
        self._returncode = returncode
        self._hang = hang
        self.kill_calls = 0

    def kill(self) -> None:
        self.kill_calls += 1
        self._hang = False

    async def wait(self) -> int:
        if self._hang:
            await asyncio.Event().wait()  # never set: only kill() releases it
        return self._returncode


def _spawn(monkeypatch, proc: _FakeProc | None = None) -> list[tuple[list[str], dict]]:
    """Replace the real spawn; return the list that records (argv, kwargs)."""
    calls: list[tuple[list[str], dict]] = []
    proc = proc if proc is not None else _FakeProc()

    async def fake_exec(*cmd, **kwargs):
        calls.append(([*cmd], kwargs))
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return calls


# --------------------------------------------------------------------------
# ProcessResult / ProcessError — what a failure looks like in the job log
# --------------------------------------------------------------------------

def test_only_exit_zero_counts_as_success():
    """``ok`` gates every ``check=True`` raise; a tool that exits 1 must never
    be mistaken for one that finished."""
    assert ProcessResult(0, [], "").ok is True
    assert ProcessResult(1, [], "").ok is False
    assert ProcessResult(-9, [], "").ok is False  # killed by signal


def test_a_failure_carries_the_last_fifteen_stdout_lines():
    """The job log shows this message and nothing else. HandBrake and MakeMKV
    emit thousands of progress lines; the useful part is always at the end, so
    the tail is what has to survive."""
    lines = [f"line{i}" for i in range(40)]
    err = ProcessError(["HandBrakeCLI", "-i", "x"], ProcessResult(1, lines, ""))
    text = str(err)
    assert "line39" in text and "line25" in text
    assert "line24" not in text  # 40 lines in, 15 out


def test_a_failure_carries_the_end_of_stderr_not_the_beginning():
    """ffmpeg prints a multi-kilobyte build banner before it ever says what
    went wrong. Truncating from the front would log the banner and drop the
    error."""
    stderr = ("banner " * 500) + "REAL_ERROR: no such file"
    assert len(stderr) > 2000
    err = ProcessError(["ffmpeg"], ProcessResult(1, [], stderr))
    text = str(err)
    assert "REAL_ERROR: no such file" in text
    assert text.count("banner") < stderr.count("banner")


def test_a_failure_names_the_tool_and_the_exit_code():
    """Which of the five external tools failed, and how, is the first thing a
    reader needs — the tail below it is meaningless without it."""
    err = ProcessError(["mkvmerge", "-o", "out.mkv"], ProcessResult(2, [], ""))
    assert "mkvmerge" in str(err) and "2" in str(err)


def test_a_failure_keeps_the_command_and_the_result_for_the_caller():
    """Recovery code inspects ``.result.returncode`` to decide retry vs abort;
    formatting the message is not enough."""
    result = ProcessResult(7, ["a"], "boom")
    err = ProcessError(("makemkvcon", "mkv"), result)
    assert err.cmd == ["makemkvcon", "mkv"]
    assert err.result is result


# --------------------------------------------------------------------------
# run() — streaming
# --------------------------------------------------------------------------

async def test_every_stdout_line_reaches_a_sync_handler(monkeypatch):
    """Progress in this pipeline is stdout lines. A dropped line is a stalled
    progress bar; a duplicated one is a jumping percentage."""
    _spawn(monkeypatch, _FakeProc(stdout=[b"one\n", b"two\n", b"three\n"]))
    seen: list[str] = []
    result = await process.run(["tool"], on_line=seen.append)
    assert seen == ["one", "two", "three"]
    assert result.stdout_lines == ["one", "two", "three"]


async def test_an_async_line_handler_is_awaited(monkeypatch):
    """The real handlers persist progress and push SSE events, so they are
    coroutines. If the runner only called them, every update would be a
    never-awaited coroutine and the UI would sit at 0%."""
    _spawn(monkeypatch, _FakeProc(stdout=[b"a\n", b"b\n"]))
    seen: list[str] = []

    async def handler(line: str) -> None:
        seen.append(line)

    await process.run(["tool"], on_line=handler)
    assert seen == ["a", "b"]


async def test_line_endings_are_stripped_including_windows_crlf(monkeypatch):
    """Tools spawned on Windows emit CRLF. A trailing \\r poisons every
    downstream parser that compares or splits on the line text."""
    _spawn(monkeypatch, _FakeProc(stdout=[b"PRGV:1,2,3\r\n", b"plain\n", b"nonl"]))
    result = await process.run(["tool"])
    assert result.stdout_lines == ["PRGV:1,2,3", "plain", "nonl"]


async def test_undecodable_stdout_bytes_do_not_kill_the_run(monkeypatch):
    """MakeMKV emits disc labels in whatever encoding the disc carries. A
    UnicodeDecodeError here would fail a rip that is otherwise fine."""
    _spawn(monkeypatch, _FakeProc(stdout=[b"caf\xe9 disc\n"]))
    result = await process.run(["tool"])
    assert result.stdout_lines == ["caf\ufffd disc"]


async def test_stdout_is_captured_even_without_a_handler(monkeypatch):
    """Callers that only want the output at the end (version probes, ``check``)
    pass no callback and read ``stdout_lines``."""
    _spawn(monkeypatch, _FakeProc(stdout=[b"v1.2.3\n"]))
    result = await process.run(["tool"])
    assert result.stdout_lines == ["v1.2.3"]


async def test_stderr_is_reassembled_across_several_reads(monkeypatch):
    """stderr arrives in 4 KiB chunks. Stopping after the first read would clip
    exactly the long ffmpeg/HandBrake diagnostics that matter most."""
    payload = b"x" * 4096 + b"tail-of-the-error"
    _spawn(monkeypatch, _FakeProc(stderr=payload))
    result = await process.run(["tool"])
    assert len(result.stderr) == len(payload)
    assert result.stderr.endswith("tail-of-the-error")


async def test_undecodable_stderr_bytes_do_not_kill_the_run(monkeypatch):
    """Same disc-encoding hazard as stdout, on the path that only runs when
    something has already gone wrong."""
    _spawn(monkeypatch, _FakeProc(stderr=b"bad \xff byte"))
    result = await process.run(["tool"])
    assert result.stderr == "bad \ufffd byte"


# --------------------------------------------------------------------------
# run() — exit codes
# --------------------------------------------------------------------------

async def test_a_nonzero_exit_raises_when_checking(monkeypatch):
    """The default. Every caller relies on a failed tool surfacing as an
    exception rather than a quietly empty output file."""
    _spawn(monkeypatch, _FakeProc(returncode=3, stdout=[b"working\n"], stderr=b"nope"))
    with pytest.raises(ProcessError) as excinfo:
        await process.run(["HandBrakeCLI"])
    assert excinfo.value.result.returncode == 3
    assert "nope" in str(excinfo.value)
    assert "working" in str(excinfo.value)


async def test_check_false_returns_the_failure_instead_of_raising(monkeypatch):
    """Probes (``--version``, capability sniffing) expect non-zero exits as a
    normal answer and must get the code and output back, not an exception."""
    _spawn(monkeypatch, _FakeProc(returncode=3, stdout=[b"usage: ...\n"]))
    result = await process.run(["tool"], check=False)
    assert result.returncode == 3
    assert result.ok is False
    assert result.stdout_lines == ["usage: ..."]


async def test_exit_zero_does_not_raise_even_when_checking(monkeypatch):
    """Guards against a check that fires on the wrong side: with ``check=True``
    a clean run must still return normally."""
    _spawn(monkeypatch, _FakeProc(returncode=0, stdout=[b"done\n"]))
    result = await process.run(["tool"], check=True)
    assert result.returncode == 0


# --------------------------------------------------------------------------
# run() — teardown
# --------------------------------------------------------------------------

async def test_a_timeout_kills_the_child_and_raises_timeout_error(monkeypatch):
    """SubtitleEdit can stop on a modal error dialog and wait forever. Without
    the kill it holds its memory until the box is rebooted; without the raise
    the job would report success on a file that was never written."""
    proc = _FakeProc(hang=True)
    _spawn(monkeypatch, proc)
    with pytest.raises(TimeoutError) as excinfo:
        await process.run(["SubtitleEdit"], timeout=0.05)
    assert proc.kill_calls == 1
    assert "SubtitleEdit" in str(excinfo.value)
    assert "killed" in str(excinfo.value)


async def test_a_process_that_finishes_inside_its_timeout_is_untouched(monkeypatch):
    """The timeout must not truncate a healthy run: same call, only the hang
    removed, and it completes normally."""
    proc = _FakeProc(returncode=0, stdout=[b"ok\n"])
    _spawn(monkeypatch, proc)
    result = await process.run(["SubtitleEdit"], timeout=30)
    assert result.returncode == 0
    assert result.stdout_lines == ["ok"]
    assert proc.kill_calls == 0


async def test_a_run_with_no_timeout_arms_no_watchdog(monkeypatch):
    """A UHD encode legitimately runs for hours, and rips for longer. Callers
    that pass no timeout must get exactly that: any accidental default would
    kill long jobs partway and leave a truncated file behind."""
    armed: list[float | None] = []
    real_wait_for = asyncio.wait_for

    async def spy(awaitable, timeout):
        armed.append(timeout)
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", spy)
    _spawn(monkeypatch, _FakeProc(stdout=[b"encoding\n"]))

    result = await process.run(["HandBrakeCLI"])

    assert result.stdout_lines == ["encoding"]
    assert armed == []


async def test_cancelling_a_run_kills_the_child_before_propagating(monkeypatch):
    """Job cancellation from the UI cancels the awaiting task. If the child
    were not killed, a 40-minute encode would keep burning CPU with nothing
    left to collect it."""
    proc = _FakeProc(hang=True)
    _spawn(monkeypatch, proc)
    task = asyncio.create_task(process.run(["HandBrakeCLI"]))
    for _ in range(10):
        await asyncio.sleep(0)  # let the pumps drain and reach proc.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.kill_calls == 1


# --------------------------------------------------------------------------
# run() — spawn options
# --------------------------------------------------------------------------

async def test_low_priority_nices_the_child_on_posix(monkeypatch):
    """Encodes must yield to the rip and the web UI. The nice value is the
    whole mechanism on POSIX."""
    monkeypatch.setattr(process.sys, "platform", "linux")
    calls = _spawn(monkeypatch)
    nice_values: list[int] = []
    monkeypatch.setattr(process.os, "nice", nice_values.append)

    await process.run(["tool"], low_priority=True)

    _, kwargs = calls[0]
    kwargs["preexec_fn"]()  # what the child runs between fork and exec
    assert nice_values == [10]


async def test_low_priority_on_windows_lowers_priority_and_detaches_ctrl_c(monkeypatch):
    """Two separate needs ride on one flag word: BELOW_NORMAL_PRIORITY_CLASS
    keeps the encode off the rip's back, and CREATE_NEW_PROCESS_GROUP stops the
    Ctrl+C that drains a worker from aborting the encode mid-file."""
    monkeypatch.setattr(process.sys, "platform", "win32")
    calls = _spawn(monkeypatch)

    await process.run(["tool"], low_priority=True)

    flags = calls[0][1]["creationflags"]
    assert flags & 0x00004000  # BELOW_NORMAL_PRIORITY_CLASS
    assert flags & 0x00000200  # CREATE_NEW_PROCESS_GROUP
    assert "preexec_fn" not in calls[0][1]


async def test_normal_priority_adds_no_spawn_flags(monkeypatch):
    """Rips and probes run at normal priority. A preexec_fn or creationflags
    leaking onto them would nice down the very work encodes yield to."""
    monkeypatch.setattr(process.sys, "platform", "linux")
    calls = _spawn(monkeypatch)

    await process.run(["tool"], low_priority=False)

    assert "preexec_fn" not in calls[0][1]
    assert "creationflags" not in calls[0][1]


async def test_the_command_and_working_directory_reach_the_spawn(monkeypatch, tmp_path):
    """Tools that resolve relative paths (SubtitleEdit's output, mkvextract's
    attachments) are run from a chosen directory; losing cwd writes files into
    whatever directory serve happens to have."""
    calls = _spawn(monkeypatch)
    await process.run(["mkvextract", "tracks", "in.mkv"], cwd=str(tmp_path))
    argv, kwargs = calls[0]
    assert argv == ["mkvextract", "tracks", "in.mkv"]
    assert kwargs["cwd"] == str(tmp_path)


# --------------------------------------------------------------------------
# kill_stale_tools — the startup reap
# --------------------------------------------------------------------------

async def test_stale_tools_are_reaped_with_taskkill_on_windows(monkeypatch):
    """This runs on the Windows serve box, where a power-off orphans every tool
    child. taskkill /F /IM is the only thing that clears them."""
    monkeypatch.setattr(process.sys, "platform", "win32")
    calls = _spawn(monkeypatch)

    killed = await process.kill_stale_tools(["SubtitleEdit.exe"])

    assert calls[0][0] == ["taskkill", "/F", "/IM", "SubtitleEdit.exe"]
    assert killed == ["SubtitleEdit.exe"]


async def test_stale_tools_are_reaped_with_pkill_on_posix(monkeypatch):
    """Dev boxes are macOS/Linux; the reap has to use the local killer or every
    startup logs a failure that hides real ones.

    -x, not -f: the pattern must match the process NAME exactly. With -f it
    matched anything whose command line merely mentioned the tool — an
    operator's own `grep makemkvcon`, an editor holding makemkvcon.log — and
    SIGKILLed those too."""
    monkeypatch.setattr(process.sys, "platform", "darwin")
    calls = _spawn(monkeypatch)

    await process.kill_stale_tools(["HandBrakeCLI"])

    assert calls[0][0] == ["pkill", "-9", "-x", "HandBrakeCLI"]


async def test_the_reap_discards_the_killers_own_output(monkeypatch):
    """taskkill prints to the console for every process it does or does not
    find. Inheriting serve's streams would bury the startup banner."""
    monkeypatch.setattr(process.sys, "platform", "win32")
    calls = _spawn(monkeypatch)

    await process.kill_stale_tools(["x.exe"])

    assert calls[0][1]["stdout"] is asyncio.subprocess.DEVNULL
    assert calls[0][1]["stderr"] is asyncio.subprocess.DEVNULL


async def test_a_missing_killer_tool_does_not_stop_serve_starting(monkeypatch):
    """No pkill/taskkill on this box is not a reason to refuse to boot — the
    reap is best effort, and the name is reported as not attempted."""
    async def boom(*cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)

    assert await process.kill_stale_tools(["SubtitleEdit.exe"]) == []


async def test_the_reap_skips_blank_names(monkeypatch):
    """Tool paths are optional in settings, so an unconfigured tool arrives as
    an empty string, and no form of ``pkill -9`` should ever be handed one."""
    monkeypatch.setattr(process.sys, "platform", "darwin")
    calls = _spawn(monkeypatch)

    killed = await process.kill_stale_tools(["", "HandBrakeCLI"])

    assert [argv for argv, _ in calls] == [["pkill", "-9", "-x", "HandBrakeCLI"]]
    assert killed == ["HandBrakeCLI"]


# --------------------------------------------------------------------------
# winjob — the Windows kill-on-close job object
# --------------------------------------------------------------------------

class _FakeApi:
    """One kernel32 entry point: records its calls and returns a canned value.
    ``restype`` is assignable because the module sets it."""

    def __init__(self, impl) -> None:
        self._impl = impl
        self.restype = None
        self.calls: list[tuple] = []

    def __call__(self, *args):
        self.calls.append(args)
        return self._impl(*args)


class _FakeKernel32:
    def __init__(self, *, create=0xABCD, set_info=1, assign=1) -> None:
        self.CreateJobObjectW = _FakeApi(lambda name, attrs: create)
        self.GetCurrentProcess = _FakeApi(lambda: 0x7777)
        self.AssignProcessToJobObject = _FakeApi(lambda job, proc: assign)
        self.limit_info: list[tuple] = []

        def _set(handle, info_class, ptr, size):
            # byref() keeps the struct reachable through ._obj
            self.limit_info.append((handle, info_class, ptr._obj, size))
            return set_info

        self.SetInformationJobObject = _FakeApi(_set)


def _windows(monkeypatch, **kw) -> _FakeKernel32:
    k32 = _FakeKernel32(**kw)
    monkeypatch.setattr(winjob.sys, "platform", "win32")
    monkeypatch.setattr(winjob, "_JOB_HANDLE", None)
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kwargs: k32, raising=False)
    return k32


def test_off_windows_the_job_object_is_never_touched(monkeypatch):
    """serve and worker call this unconditionally at startup. On the dev Macs
    and any Linux box it must be an inert no-op, not a caught exception."""
    touched: list = []
    monkeypatch.setattr(winjob.sys, "platform", "darwin")
    monkeypatch.setattr(winjob, "_JOB_HANDLE", None)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: touched.append(a), raising=False)

    assert winjob.enable_kill_on_close() is False
    assert touched == []
    assert winjob._JOB_HANDLE is None


def test_the_job_is_created_with_kill_on_job_close(monkeypatch):
    """KILL_ON_JOB_CLOSE (0x2000) on ExtendedLimitInformation (class 9) is the
    entire feature: it is what makes Windows terminate MakeMKV, HandBrake and
    SubtitleEdit the moment serve exits. A wrong flag or class silently leaves
    the orphans behind."""
    k32 = _windows(monkeypatch)

    assert winjob.enable_kill_on_close() is True

    handle, info_class, info, size = k32.limit_info[0]
    assert handle == 0xABCD
    assert info_class == 9
    assert info.BasicLimitInformation.LimitFlags & 0x2000
    assert size == ctypes.sizeof(type(info))


def test_the_current_process_is_the_one_assigned_to_the_job(monkeypatch):
    """The job must own *this* process, so the children it spawns are inside
    it. Assigning anything else creates an empty job that reaps nothing."""
    k32 = _windows(monkeypatch)

    winjob.enable_kill_on_close()

    assert k32.AssignProcessToJobObject.calls == [(0xABCD, 0x7777)]


def test_the_job_handle_is_retained_for_the_process_lifetime(monkeypatch):
    """Kill-on-close fires when the last handle closes. Letting the handle be
    garbage collected would kill every tool child immediately after startup —
    the exact opposite of the intent."""
    _windows(monkeypatch)

    winjob.enable_kill_on_close()

    assert winjob._JOB_HANDLE == 0xABCD


def test_a_second_call_does_not_create_a_second_job(monkeypatch):
    """Both serve and worker can run in one process. Re-creating the job would
    replace the retained handle and close the live one, killing the children."""
    k32 = _windows(monkeypatch)

    assert winjob.enable_kill_on_close() is True
    assert winjob.enable_kill_on_close() is False
    assert len(k32.CreateJobObjectW.calls) == 1
    assert winjob._JOB_HANDLE == 0xABCD


def test_a_job_that_cannot_be_created_reports_false(monkeypatch):
    """Nothing else may be attempted with a null handle, and the caller needs
    to know the safety net is not in place."""
    k32 = _windows(monkeypatch, create=0)

    assert winjob.enable_kill_on_close() is False
    assert k32.SetInformationJobObject.calls == []
    assert winjob._JOB_HANDLE is None


def test_a_job_whose_limits_will_not_set_is_abandoned(monkeypatch):
    """A job without KILL_ON_JOB_CLOSE reaps nothing, so assigning this process
    to it would be worse than useless — it would report success."""
    k32 = _windows(monkeypatch, set_info=0)

    assert winjob.enable_kill_on_close() is False
    assert k32.AssignProcessToJobObject.calls == []
    assert winjob._JOB_HANDLE is None


def test_a_process_that_cannot_be_assigned_reports_false(monkeypatch):
    """Pre-Win8, a process already inside a non-nestable job (a CI runner, some
    service wrappers) cannot join another. serve must still start."""
    _windows(monkeypatch, assign=0)

    assert winjob.enable_kill_on_close() is False
    assert winjob._JOB_HANDLE is None


def test_any_win32_failure_degrades_to_a_no_op(monkeypatch):
    """This is a best-effort hardening step. A missing or refusing kernel32
    must never be the reason the ripper will not boot."""
    monkeypatch.setattr(winjob.sys, "platform", "win32")
    monkeypatch.setattr(winjob, "_JOB_HANDLE", None)

    def boom(*a, **k):
        raise OSError("kernel32 unavailable")

    monkeypatch.setattr(ctypes, "WinDLL", boom, raising=False)

    assert winjob.enable_kill_on_close() is False
    assert winjob._JOB_HANDLE is None


# --------------------------------------------------------------------------
# HandBrake --json block parsing
# --------------------------------------------------------------------------

def _feed(parser: HandBrakeJsonParser, text: str) -> list[tuple[str, str]]:
    return [b for b in (parser.feed_line(l) for l in text.splitlines()) if b is not None]


def test_a_log_line_that_only_looks_like_a_block_start_is_ignored():
    """HandBrake interleaves plain log lines with its JSON. One that happens to
    contain ``: {`` must not open a block, or it swallows the real Progress
    block that follows and progress stops updating."""
    parser = HandBrakeJsonParser()
    stream = (
        '[15:04:05] sync: {frame 12} dropped\n'
        'Progress: {\n'
        ' "State": "WORKING"\n'
        '}\n'
    )
    blocks = _feed(parser, stream)
    assert [label for label, _ in blocks] == ["Progress"]


def test_an_escaped_quote_inside_a_string_does_not_end_the_block_early():
    """HandBrake quotes source paths and titles back at us. If a backslash
    escape were not honoured, the scanner would think the string had ended and
    count a brace inside it, cutting the JSON short and losing all progress."""
    parser = HandBrakeJsonParser()
    stream = (
        'Progress: {\n'
        ' "Title": "a \\" quote then a } brace",\n'
        ' "State": "WORKING"\n'
        '}\n'
    )
    blocks = _feed(parser, stream)
    assert len(blocks) == 1
    obj = json.loads(blocks[0][1])
    assert obj["Title"] == 'a " quote then a } brace'
    assert obj["State"] == "WORKING"


def test_a_block_with_no_phase_object_still_reports_its_state():
    """HandBrake announces states (IDLE, SCANDONE) with no phase sub-object.
    Reading a fraction off one would be reading a field that is not there."""
    prog = _progress_from_block({"State": "IDLE"})
    assert prog.state == "IDLE"
    assert prog.fraction is None and prog.eta_seconds is None and prog.fps is None


def test_the_first_phase_object_in_a_block_is_the_authoritative_one():
    """Working, Muxing, Scanning and WorkDone are searched in that order and
    the search stops at the first hit. Reading the last one instead would let a
    stale or trailing phase object override the live encode fraction and make
    the progress bar jump backwards."""
    prog = _progress_from_block({
        "State": "WORKING",
        "Working": {"Progress": 0.25},
        "Muxing": {"Progress": 0.99},
    })
    assert prog.fraction == 0.25


def test_the_muxing_phase_reports_progress_like_the_working_phase():
    """The last few percent of an encode is muxing. Ignoring it would freeze
    the progress bar at 99% for minutes on a big UHD title."""
    prog = _progress_from_block({"State": "MUXING", "Muxing": {"Progress": 0.5}})
    assert prog.state == "MUXING"
    assert prog.fraction == 0.5


# --------------------------------------------------------------------------
# HandBrake settings / preset reporting
# --------------------------------------------------------------------------

def _settings(tmp_path: Path, *, low_priority: bool = True,
              tune_grain: bool = False) -> Settings:
    return Settings(
        tools=ToolPaths(handbrakecli="HandBrakeCLI"),
        storage=StorageSettings(staging_dir=tmp_path / "s", output_dir=tmp_path / "o"),
        encode=EncodeSettings(
            container="mkv",
            preset_file=tmp_path / "presets.json",
            preset_name="BluRay UHD",
            low_priority=low_priority,
            tune_grain=tune_grain,
        ),
        languages=LanguageSettings(keep=["eng"], default="eng"),
        subtitles=SubtitleSettings(),
        audio=AudioSettings(),
    )


def test_a_missing_preset_file_is_reported_instead_of_crashing_the_job(tmp_path):
    """speed_summary runs while composing the job log. Letting it raise would
    fail an encode that HandBrake itself would have run fine on its built-in
    default."""
    settings = _settings(tmp_path)  # presets.json was never written
    summary = HandBrake(settings).speed_summary()
    assert "BluRay UHD" in summary
    assert "unreadable" in summary


def test_a_corrupt_preset_file_is_reported_instead_of_crashing_the_job(tmp_path):
    """A half-written presets.json (interrupted sync from another box) is a
    ValueError, not an OSError — both have to be survivable."""
    settings = _settings(tmp_path)
    settings.encode.preset_file.write_text("{not json")
    summary = HandBrake(settings).speed_summary()
    assert "BluRay UHD" in summary
    assert "unreadable" in summary


# --------------------------------------------------------------------------
# HandBrake.encode
# --------------------------------------------------------------------------

def _fake_run(monkeypatch, *, stdout_lines: tuple[str, ...] = (),
              writes: bytes | None = b"encoded video"):
    """Stub process.run as HandBrake sees it. Records the call and replays
    ``stdout_lines`` through the on_line handler.

    ``writes`` defaults to a non-empty file because that is what a HandBrake
    that really encoded leaves behind, and ``encode`` now refuses to return a
    destination it cannot see. Pass ``writes=None`` for the run that exits 0
    having written nothing.
    """
    calls: list[dict] = []

    async def fake(cmd, *, on_line=None, low_priority=False, **kwargs):
        calls.append({"cmd": list(cmd), "low_priority": low_priority, **kwargs})
        for line in stdout_lines:
            if on_line is not None:
                res = on_line(line)
                if asyncio.iscoroutine(res):
                    await res
        if writes is not None:
            out = Path(cmd[cmd.index("-o") + 1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(writes)
        return process.ProcessResult(0, list(stdout_lines), "")

    monkeypatch.setattr(handbrake_mod.process, "run", fake)
    return calls


PROGRESS_STREAM = (
    'Version: {',
    ' "Name": "HandBrake"',
    '}',
    'Progress: {',
    ' "State": "WORKING",',
    ' "Working": { "Progress": 0.25, "ETASeconds": 120, "RateAvg": 30.5 }',
    '}',
    'Progress: {',
    ' "State": "WORKDONE",',
    ' "WorkDone": { "Error": 0 }',
    '}',
)


async def test_encode_creates_the_output_directory_before_running(monkeypatch, tmp_path):
    """The library path for a new show does not exist yet. HandBrake does not
    create it — it exits with an open failure and the job dies at 100%."""
    dst = tmp_path / "out" / "Movies" / "Film (2024)" / "Film (2024).mkv"
    existed_at_spawn: list[bool] = []

    async def fake(cmd, **kwargs):
        existed_at_spawn.append(dst.parent.is_dir())
        dst.write_bytes(b"encoded video")
        return process.ProcessResult(0, [], "")

    monkeypatch.setattr(handbrake_mod.process, "run", fake)

    await HandBrake(_settings(tmp_path)).encode(tmp_path / "in.mkv", dst)

    assert existed_at_spawn == [True]


async def test_encode_reports_the_progress_fraction_from_handbrake_json(monkeypatch, tmp_path):
    """This is the only progress signal an encode produces. It drives the CLI
    bar, the web UI and the stall detector that restarts wedged jobs."""
    _fake_run(monkeypatch, stdout_lines=PROGRESS_STREAM)
    events = []

    await HandBrake(_settings(tmp_path)).encode(
        tmp_path / "in.mkv", tmp_path / "out" / "f.mkv", on_progress=events.append)

    assert [e.state for e in events] == ["WORKING", "WORKDONE"]
    assert events[0].fraction == 0.25
    assert events[0].eta_seconds == 120
    assert events[0].fps == 30.5


async def test_encode_ignores_blocks_that_are_not_progress(monkeypatch, tmp_path):
    """HandBrake also prints Version and JSON Title Set blocks. Feeding those
    to the progress handler would push a stateless event and reset the bar."""
    _fake_run(monkeypatch, stdout_lines=(
        'Version: {', ' "Name": "HandBrake", "VersionString": "1.7.3"', '}'))
    events = []

    await HandBrake(_settings(tmp_path)).encode(
        tmp_path / "in.mkv", tmp_path / "out" / "f.mkv", on_progress=events.append)

    assert events == []


async def test_encode_survives_a_truncated_progress_block(monkeypatch, tmp_path):
    """A block can be cut in half by an interleaved log line. Losing one update
    is fine; raising out of the line handler would abandon a running encode."""
    _fake_run(monkeypatch, stdout_lines=(
        'Progress: {', ' "State": WORKING,', '}',      # unquoted value: invalid JSON
        'Progress: {', ' "State": "MUXING"', '}'))
    events = []

    await HandBrake(_settings(tmp_path)).encode(
        tmp_path / "in.mkv", tmp_path / "out" / "f.mkv", on_progress=events.append)

    assert [e.state for e in events] == ["MUXING"]


async def test_encode_awaits_an_async_progress_handler(monkeypatch, tmp_path):
    """The production handler writes progress to the store and publishes SSE,
    so it is a coroutine. Not awaiting it leaves the UI frozen and logs a
    never-awaited warning nobody reads."""
    _fake_run(monkeypatch, stdout_lines=PROGRESS_STREAM)
    events = []

    async def on_progress(prog):
        events.append(prog.state)

    await HandBrake(_settings(tmp_path)).encode(
        tmp_path / "in.mkv", tmp_path / "out" / "f.mkv", on_progress=on_progress)

    assert events == ["WORKING", "WORKDONE"]


async def test_encode_tolerates_a_handler_that_returns_a_plain_value(monkeypatch, tmp_path):
    """A sync handler may return anything (``list.append`` returns None, a
    predicate returns a bool). Only awaitables may be awaited."""
    _fake_run(monkeypatch, stdout_lines=PROGRESS_STREAM)
    seen = []

    def on_progress(prog):
        seen.append(prog.state)
        return "not awaitable"

    await HandBrake(_settings(tmp_path)).encode(
        tmp_path / "in.mkv", tmp_path / "out" / "f.mkv", on_progress=on_progress)

    assert seen == ["WORKING", "WORKDONE"]


async def test_encode_runs_with_no_progress_handler_at_all(monkeypatch, tmp_path):
    """Batch re-encodes and tests call encode without a handler. Calling
    ``None`` on the first Progress block would abort the encode with a
    TypeError raised from inside the stdout pump."""
    _fake_run(monkeypatch, stdout_lines=PROGRESS_STREAM)
    dst = tmp_path / "out" / "f.mkv"

    assert await HandBrake(_settings(tmp_path)).encode(tmp_path / "in.mkv", dst) == dst


async def test_encode_passes_the_low_priority_setting_through(monkeypatch, tmp_path):
    """The setting exists so encodes yield to ripping. It has to reach the
    spawn; ignoring it makes the box unusable while a UHD title encodes."""
    calls = _fake_run(monkeypatch)
    await HandBrake(_settings(tmp_path, low_priority=True)).encode(
        tmp_path / "in.mkv", tmp_path / "out" / "a.mkv")
    await HandBrake(_settings(tmp_path, low_priority=False)).encode(
        tmp_path / "in.mkv", tmp_path / "out" / "b.mkv")

    assert [c["low_priority"] for c in calls] == [True, False]


async def test_encode_runs_the_built_command(monkeypatch, tmp_path):
    """encode must run the audited command builder, not a second inlined argv
    that could drift from it (and re-enable preset subtitle burn-in)."""
    calls = _fake_run(monkeypatch)
    src, dst = tmp_path / "in.mkv", tmp_path / "out" / "f.mkv"
    hb = HandBrake(_settings(tmp_path))

    await hb.encode(src, dst, tune_grain=True, deinterlace=True)

    assert calls[0]["cmd"] == hb.build_command(
        src, dst, tune_grain=True, deinterlace=True)


# --------------------------------------------------------------------------
# HandBrake.encode_clip — the grain preview
# --------------------------------------------------------------------------

async def test_a_preview_clip_is_a_browser_playable_video_only_mp4(monkeypatch, tmp_path):
    """The web UI plays this clip inline to show the grain difference. MKV or
    HEVC will not play in a browser, and the full builder's audio flags made
    HandBrake exit 0 having written nothing."""
    calls = _fake_run(monkeypatch, writes=b"clip")
    dst = tmp_path / "prev" / "clip.mp4"

    await HandBrake(_settings(tmp_path)).encode_clip(
        tmp_path / "in.mkv", dst, start=600, duration=4, tune_grain=False)

    cmd = calls[0]["cmd"]
    assert cmd[cmd.index("--format") + 1] == "av_mp4"
    assert cmd[cmd.index("--encoder") + 1] == "x264"
    assert "--optimize" in cmd  # faststart, so the browser can seek
    assert cmd[cmd.index("-a") + 1] == "none"
    assert cmd[cmd.index("-s") + 1] == "none"
    assert "--audio-lang-list" not in cmd


async def test_a_preview_clip_overrides_the_presets_slow_encoder_speed(monkeypatch, tmp_path):
    """The real preset can be 'slower'. A four-second preview at that speed
    takes minutes, and the user is sitting in front of the UI waiting."""
    calls = _fake_run(monkeypatch, writes=b"clip")

    await HandBrake(_settings(tmp_path)).encode_clip(
        tmp_path / "in.mkv", tmp_path / "p" / "c.mp4",
        start=0, duration=4, tune_grain=False)

    cmd = calls[0]["cmd"]
    assert cmd[cmd.index("--encoder-preset") + 1] == "veryfast"


async def test_a_preview_clip_seeks_to_the_requested_moment(monkeypatch, tmp_path):
    """The point of the preview is comparing the same four seconds with grain
    tuning on and off; a clip from the wrong offset compares nothing."""
    calls = _fake_run(monkeypatch, writes=b"clip")

    await HandBrake(_settings(tmp_path)).encode_clip(
        tmp_path / "in.mkv", tmp_path / "p" / "c.mp4",
        start=754.6, duration=4, tune_grain=False)

    cmd = calls[0]["cmd"]
    assert cmd[cmd.index("--start-at") + 1] == "seconds:755"
    assert cmd[cmd.index("--stop-at") + 1] == "seconds:4"


async def test_the_preview_grain_tune_is_the_thing_being_previewed(monkeypatch, tmp_path):
    """The whole feature is an A/B of --encoder-tune grain. If the flag did not
    follow the argument, both clips would look identical and the choice offered
    in the UI would be meaningless."""
    calls = _fake_run(monkeypatch, writes=b"clip")
    hb = HandBrake(_settings(tmp_path))

    await hb.encode_clip(tmp_path / "i.mkv", tmp_path / "p" / "on.mp4",
                         start=0, duration=4, tune_grain=True)
    await hb.encode_clip(tmp_path / "i.mkv", tmp_path / "p" / "off.mp4",
                         start=0, duration=4, tune_grain=False)

    on, off = calls[0]["cmd"], calls[1]["cmd"]
    assert on[on.index("--encoder-tune") + 1] == "grain"
    assert "--encoder-tune" not in off


async def test_a_preview_clip_creates_its_own_directory(monkeypatch, tmp_path):
    """Previews land in a scratch directory that does not exist on a fresh
    install; HandBrake would fail to open the output."""
    _fake_run(monkeypatch, writes=b"clip")
    dst = tmp_path / "previews" / "job7" / "clip.mp4"

    await HandBrake(_settings(tmp_path)).encode_clip(
        tmp_path / "in.mkv", dst, start=0, duration=4, tune_grain=False)

    assert dst.exists()


async def test_a_preview_clip_that_was_never_written_is_an_error(monkeypatch, tmp_path):
    """HandBrake exits 0 when asked to seek past the end of the title. Without
    this check the failure surfaces later and much less clearly, inside the
    frame extractor."""
    _fake_run(monkeypatch, writes=None)  # exit 0, no output file
    dst = tmp_path / "p" / "clip.mp4"

    with pytest.raises(RuntimeError) as excinfo:
        await HandBrake(_settings(tmp_path)).encode_clip(
            tmp_path / "in.mkv", dst, start=99999, duration=4, tune_grain=False)

    assert str(dst) in str(excinfo.value)


async def test_a_zero_byte_preview_clip_is_an_error(monkeypatch, tmp_path):
    """An empty file exists but plays nothing. Treating it as success puts a
    broken <video> in the UI with no explanation."""
    _fake_run(monkeypatch, writes=b"")
    dst = tmp_path / "p" / "clip.mp4"

    with pytest.raises(RuntimeError):
        await HandBrake(_settings(tmp_path)).encode_clip(
            tmp_path / "in.mkv", dst, start=0, duration=4, tune_grain=False)


async def test_a_written_preview_clip_is_returned(monkeypatch, tmp_path):
    """The caller passes this path straight to the frame extractor and the web
    route, so the success path must return it rather than None."""
    _fake_run(monkeypatch, writes=b"clip-bytes")
    dst = tmp_path / "p" / "clip.mp4"

    result = await HandBrake(_settings(tmp_path)).encode_clip(
        tmp_path / "in.mkv", dst, start=0, duration=4, tune_grain=False)

    assert result == dst


async def test_a_preview_clip_honours_the_low_priority_setting(monkeypatch, tmp_path):
    """A preview can be requested while a rip is running; it must not compete
    with it any more than a full encode does."""
    calls = _fake_run(monkeypatch, writes=b"clip")

    await HandBrake(_settings(tmp_path, low_priority=True)).encode_clip(
        tmp_path / "i.mkv", tmp_path / "p" / "a.mp4", start=0, duration=4, tune_grain=False)
    await HandBrake(_settings(tmp_path, low_priority=False)).encode_clip(
        tmp_path / "i.mkv", tmp_path / "p" / "b.mp4", start=0, duration=4, tune_grain=False)

    assert [c["low_priority"] for c in calls] == [True, False]
