"""Drive enumeration: OS-detected discs makemkvcon can't see (blind drives)."""
import asyncio

from blurayripper.config import load_settings
from blurayripper.tools import makemkv
from blurayripper.tools.makemkv import MakeMKV, parse_drives


def test_parse_drives_drops_empty_and_keeps_named():
    lines = [
        'DRV:0,2,999,12,"ASUS BD","MINORITY_REPORT","E:"',
        'DRV:1,256,999,0,"","",""',   # wedged/absent -> dropped (no drive name)
    ]
    ds = parse_drives(lines)
    assert [(d.index, d.device, d.disc_name) for d in ds] == [(0, "E:", "MINORITY_REPORT")]


def _mk(monkeypatch, stdout_lines, os_drives):
    s = load_settings()
    object.__setattr__(s.tools, "makemkvcon", "makemkvcon")
    async def fake_run(cmd, **k):
        class R: pass
        R.stdout_lines = stdout_lines
        return R()
    monkeypatch.setattr(makemkv.process, "run", fake_run)
    monkeypatch.setattr(makemkv, "os_optical_drives", lambda: os_drives)
    return MakeMKV(s)


def test_blind_drive_surfaced_when_makemkv_sees_nothing(monkeypatch):
    mk = _mk(monkeypatch, [], [("E:", "MINORITY_REPORT")])
    drives = asyncio.run(mk.list_drives(timeout=5))
    assert len(drives) == 1
    d = drives[0]
    assert d.makemkv_blind and d.index == -1
    assert d.disc_name == "MINORITY_REPORT" and d.os_detected


def test_os_drive_deduped_when_makemkv_reports_it(monkeypatch):
    mk = _mk(monkeypatch, ['DRV:0,2,999,12,"ASUS BD","MINORITY_REPORT","E:"'],
             [("E:", "MINORITY_REPORT")])
    drives = asyncio.run(mk.list_drives(timeout=5))
    assert len(drives) == 1 and drives[0].index == 0 and drives[0].makemkv_blind is False


def test_os_optical_drives_is_noop_off_windows():
    assert makemkv.os_optical_drives() == []   # tests run on macOS/Linux
