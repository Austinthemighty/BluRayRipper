"""A drive stuck in a USB reset loop must not freeze the rest of serve.

2026-10-22: after the drives' USB cables were swapped, serve stopped answering
altogether. The drive scan opened /dev/srN (to see whether it held a disc) and
ran udevadm (for its label) on the event loop itself, and opening an optical
device blocks in the kernel for as long as that drive's link is being reset.
The 2-second "did a disc go in" probe ran on asyncio.to_thread, so each poll
during a reset loop took another executor thread and never gave it back.
"""

import asyncio
import threading
import time

from blurayripper.tools import makemkv as makemkv_mod
from blurayripper.tools import process as process_mod
from blurayripper.tools.process import DeviceProbe

from tests.test_tool_wrappers import FakeProcess, _install, _mk


async def _ticks_while(coro, *, every=0.02):
    """Run ``coro`` while counting event-loop ticks. A blocked loop counts none."""
    ticks = 0
    task = asyncio.ensure_future(coro)
    while not task.done():
        await asyncio.sleep(every)
        ticks += 1
    return await task, ticks


async def test_a_drive_stuck_mid_reset_does_not_stop_the_drive_scan(tmp_path, monkeypatch):
    stuck = threading.Event()                      # never set: the drive never answers

    def hangs(*a):
        stuck.wait(5)
        return [("/dev/sr0", "SOME_DVD")]

    monkeypatch.setattr(makemkv_mod, "os_optical_drives", hangs)
    monkeypatch.setattr(makemkv_mod, "os_disc_label", lambda dev: stuck.wait(5) or "LABEL")
    monkeypatch.setattr(makemkv_mod.MakeMKV, "PROBE_TIMEOUT", 0.2)
    monkeypatch.setattr(makemkv_mod.MakeMKV, "_probes", DeviceProbe())
    _install(monkeypatch, FakeProcess(lambda cmd: [
        'DRV:0,2,999,12,"BD-RE ASUS BW-16D1HT 3.10 KX1P71E4206","","/dev/sr1"']))

    started = time.monotonic()
    drives, ticks = await _ticks_while(_mk(tmp_path).list_drives())
    stuck.set()

    assert [d.device for d in drives] == ["/dev/sr1"]   # the scan answered without them
    assert time.monotonic() - started < 2
    assert ticks >= 5, "the event loop stopped while the drive was stuck"


async def test_a_stuck_probe_is_not_started_again_while_it_is_still_stuck():
    """One stuck thread stays one, however many polls come round."""
    stuck = threading.Event()
    calls = []

    def probe():
        calls.append(1)
        stuck.wait(5)
        return "answer"

    p = DeviceProbe()
    for _ in range(5):
        assert await p.ask("media", probe, timeout=0.05, default="gave up") == "gave up"
    assert len(calls) == 1

    stuck.set()
    await asyncio.sleep(0.05)
    assert await p.ask("media", probe, timeout=1) == "answer"   # answered: ask afresh


async def test_the_disc_watcher_keeps_its_poll_while_a_drive_is_stuck(monkeypatch):
    from blurayripper.config import load_settings
    from blurayripper.persistence import MemoryStore
    from blurayripper.web import manager as mgr
    stuck = threading.Event()
    calls = []

    def snapshot():
        calls.append(1)
        stuck.wait(5)
        return {}

    monkeypatch.setattr(mgr, "os_media_snapshot", snapshot)
    m = mgr.JobManager(load_settings(), store=MemoryStore())
    monkeypatch.setattr(m, "_MEDIA_PROBE_SECONDS", 0.01)
    monkeypatch.setattr(m, "_MEDIA_PROBE_TIMEOUT", 0.05)

    started = time.monotonic()
    _, ticks = await _ticks_while(m._wait_for_disc_change(0.4))
    stuck.set()

    assert time.monotonic() - started < 2, "the poll waited on the stuck drive"
    assert ticks >= 5
    assert len(calls) == 1, "every poll started another thread stuck on the drive"
