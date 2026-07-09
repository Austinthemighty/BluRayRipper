"""process.run timeout kills a hung child; kill_stale_tools is best-effort."""

import sys

import pytest

from blurayripper.tools import process


@pytest.mark.skipif(sys.platform.startswith("win"), reason="uses a POSIX sleep")
async def test_run_timeout_kills_hung_process():
    with pytest.raises(TimeoutError):
        await process.run(["sleep", "30"], timeout=0.3)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="uses a POSIX cmd")
async def test_run_no_timeout_completes():
    result = await process.run(["true"], timeout=10)
    assert result.returncode == 0


async def test_kill_stale_tools_is_best_effort_and_returns_attempts():
    # A bogus image name — nothing to kill, but it must not raise, and returns
    # the names it attempted (empty ones skipped).
    attempted = await process.kill_stale_tools(["definitely_not_a_real_process_xyz", ""])
    assert attempted == ["definitely_not_a_real_process_xyz"]
