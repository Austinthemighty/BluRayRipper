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


def _low_priority_kwargs() -> dict:
    """Spawn kwargs to run a child below normal priority, so CPU-heavy work
    (encodes) yields to ripping and the UI instead of starving them."""
    if sys.platform.startswith("win"):
        return {"creationflags": 0x00004000}  # BELOW_NORMAL_PRIORITY_CLASS
    return {"preexec_fn": lambda: os.nice(10)}  # POSIX: nicer scheduling


async def run(
    cmd: Sequence[str],
    *,
    on_line: LineHandler | None = None,
    cwd: str | None = None,
    check: bool = True,
    low_priority: bool = False,
) -> ProcessResult:
    """Run ``cmd``, streaming stdout lines to ``on_line``.

    Raises ``ProcessError`` on a non-zero exit when ``check`` is True.
    ``low_priority`` runs the child below normal OS priority.
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
            raw = await proc.stdout.readline()
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

    try:
        await asyncio.gather(_pump_stdout(), _pump_stderr())
        returncode = await proc.wait()
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise

    result = ProcessResult(
        returncode=returncode,
        stdout_lines=stdout_lines,
        stderr=b"".join(stderr_chunks).decode("utf-8", errors="replace"),
    )
    if check and not result.ok:
        raise ProcessError(cmd, result)
    return result
