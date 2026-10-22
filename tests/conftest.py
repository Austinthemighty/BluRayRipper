"""Test-wide guard: no test may drive a real optical drive.

Ripping starts as soon as a disc's titles are picked, while it waits to be
named. A test that stubs only the scan and the encode therefore reaches MakeMKV,
and on a machine with MakeMKV installed and a disc loaded that would rip it for
real. Tests that exercise ripping fake the process layer or MakeMKV itself,
which replaces this guard. Anything else that gets as far as spawning makemkvcon
fails the way a box without MakeMKV would.
"""

import asyncio
import os

import pytest

_REAL_EXEC = asyncio.create_subprocess_exec


@pytest.fixture(autouse=True)
def _no_real_optical_tools(monkeypatch):
    async def guarded(program, *args, **kwargs):
        if os.path.basename(str(program)).lower().startswith("makemkvcon"):
            raise FileNotFoundError(f"{program}: real MakeMKV is blocked in tests")
        return await _REAL_EXEC(program, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", guarded)
