"""Async subprocess runner with line streaming and cancellation.

All external tools are long-running and emit progress on stdout. This module
spawns them, streams stdout line-by-line to a callback (so callers can parse
progress and forward it to the CLI/SSE), and supports cancellation.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

# A line handler may be sync or async. It receives one decoded line (no newline).
LineHandler = Callable[[str], None] | Callable[[str], Awaitable[None]]


@dataclass
class ProcessResult:
    returncode: int
    stdout_lines: list[str]
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class ProcessError(RuntimeError):
    def __init__(self, cmd: Sequence[str], result: ProcessResult) -> None:
        out_tail = "\n".join(result.stdout_lines[-15:])
        # Show the END of stderr — the real error usually trails long banners
        # (e.g. ffmpeg's build configuration) that would otherwise bury it.
        err_tail = result.stderr.strip()[-2000:]
        super().__init__(
            f"Command failed ({result.returncode}): {cmd[0]}\n"
            f"stderr tail: {err_tail}\n"
            f"stdout tail:\n{out_tail}"
        )
        self.cmd = list(cmd)
        self.result = result


async def _maybe_await(value) -> None:
    if asyncio.iscoroutine(value):
        await value


async def _kill_and_reap(proc) -> None:
    """End the child and wait for it, tolerating one that has already exited.

    ProcessLookupError is swallowed because this only ever runs while another
    exception is in flight; letting a lost race replace it would hide the real
    reason the run failed.
    """
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    await proc.wait()


def _low_priority_kwargs() -> dict:
    """Spawn kwargs to run a child below normal priority, so CPU-heavy work
    (encodes) yields to ripping and the UI instead of starving them.

    On Windows we also put the child in its own process group so a console
    Ctrl+C (used to *drain* a worker) doesn't abort the running encode — the
    parent still ends it explicitly via proc.kill() on real cancellation."""
    if sys.platform.startswith("win"):
        # BELOW_NORMAL_PRIORITY_CLASS | CREATE_NEW_PROCESS_GROUP
        return {"creationflags": 0x00004000 | 0x00000200}
    return {"preexec_fn": lambda: os.nice(10)}  # POSIX: nicer scheduling


async def kill_stale_tools(image_names: Sequence[str]) -> list[str]:
    """Best-effort kill of leftover tool processes by image name.

    Called ONLY at serve startup, before any jobs run — so anything matching is
    an orphan from a prior crash/power-off (Windows doesn't reap a dead parent's
    children), never work we own. Prevents e.g. SubtitleEdit windows piling up
    across restarts until they exhaust the system commit limit. Returns the names
    it attempted to kill."""
    killed: list[str] = []
    for name in image_names:
        if not name:
            continue
        if sys.platform.startswith("win"):
            cmd = ["taskkill", "/F", "/IM", name]
        else:
            cmd = ["pkill", "-9", "-f", name]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.wait()
            killed.append(name)
        except Exception:
            pass  # tool not running / taskkill unavailable — nothing to do
    return killed


async def run(
    cmd: Sequence[str],
    *,
    on_line: LineHandler | None = None,
    cwd: str | None = None,
    check: bool = True,
    low_priority: bool = False,
    timeout: float | None = None,
) -> ProcessResult:
    """Run ``cmd``, streaming stdout lines to ``on_line``.

    Raises ``ProcessError`` on a non-zero exit when ``check`` is True.
    ``low_priority`` runs the child below normal OS priority. ``timeout`` (seconds)
    kills the child if it runs longer — important for tools that can hang on an
    error dialog (e.g. SubtitleEdit) and otherwise pile up holding memory.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        **(_low_priority_kwargs() if low_priority else {}),
    )
    stdout_lines: list[str] = []

    async def _pump_stdout() -> None:
        assert proc.stdout is not None
        while True:
            try:
                raw = await proc.stdout.readline()
            except ValueError:
                # asyncio's StreamReader caps a line at 64 KiB and raises past
                # it. A tool that emits binary noise with no newline (a crashing
                # makemkvcon, a driver dumping a buffer) would otherwise take
                # the whole rip or scan down with one unreadable line. readline
                # has already dropped the over-long data, so the next call
                # resumes at the following line; note the loss so a later
                # failure tail says why a line is missing.
                stdout_lines.append("[line dropped: longer than 64 KiB]")
                continue
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            stdout_lines.append(line)
            if on_line is not None:
                await _maybe_await(on_line(line))

    stderr_chunks: list[bytes] = []

    async def _pump_stderr() -> None:
        assert proc.stderr is not None
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            stderr_chunks.append(chunk)

    async def _drive() -> int:
        pumps = asyncio.gather(_pump_stdout(), _pump_stderr())
        try:
            await pumps
        except BaseException:
            # gather() propagates the first failure but leaves its siblings
            # running; without this the stderr pump outlives the call and reads
            # a pipe nobody will ever look at.
            pumps.cancel()
            raise
        return await proc.wait()

    try:
        # ``timeout is not None``, not ``if timeout``: 0 means "expire at once",
        # and reading it as "no timeout" would let a caller that meant to cap a
        # tool run it forever. Callers that use 0 as "disabled" map it to None
        # themselves (see SubtitleEdit.ocr_to_srt).
        returncode = await (asyncio.wait_for(_drive(), timeout)
                            if timeout is not None else _drive())
    except asyncio.TimeoutError:
        await _kill_and_reap(proc)
        raise TimeoutError(f"{cmd[0]} exceeded {timeout:.0f}s and was killed")
    except BaseException:
        # Anything else that escapes — in practice the caller's on_line handler,
        # which writes progress to Postgres and pushes SSE and so raises when
        # the database is unreachable — used to leave the child RUNNING with
        # nobody holding a reference. On Windows a dead parent does not reap its
        # children, so one raising progress callback orphaned a full-speed
        # HandBrake/makemkvcon and the next job was queued onto a box still
        # burning its CPU on the last one. Covers CancelledError too.
        await _kill_and_reap(proc)
        raise

    result = ProcessResult(
        returncode=returncode,
        stdout_lines=stdout_lines,
        stderr=b"".join(stderr_chunks).decode("utf-8", errors="replace"),
    )
    if check and not result.ok:
        raise ProcessError(cmd, result)
    return result
