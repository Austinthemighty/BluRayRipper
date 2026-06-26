"""Async subprocess runner with line streaming and cancellation.

All external tools are long-running and emit progress on stdout. This module
spawns them, streams stdout line-by-line to a callback (so callers can parse
progress and forward it to the CLI/SSE), and supports cancellation.
"""

from __future__ import annotations

import asyncio
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
        tail = "\n".join(result.stdout_lines[-15:])
        super().__init__(
            f"Command failed ({result.returncode}): {cmd[0]}\n"
            f"stderr: {result.stderr.strip()[:2000]}\n"
            f"stdout tail:\n{tail}"
        )
        self.cmd = list(cmd)
        self.result = result


async def _maybe_await(value) -> None:
    if asyncio.iscoroutine(value):
        await value


async def run(
    cmd: Sequence[str],
    *,
    on_line: LineHandler | None = None,
    cwd: str | None = None,
    check: bool = True,
) -> ProcessResult:
    """Run ``cmd``, streaming stdout lines to ``on_line``.

    Raises ``ProcessError`` on a non-zero exit when ``check`` is True.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
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
