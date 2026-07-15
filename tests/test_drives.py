"""Drive enumeration: OS-detected discs makemkvcon can't see (blind drives)."""
import asyncio

from blurayripper.config import load_settings
from blurayripper.tools import makemkv
from blurayripper.tools.makemkv import Drive, MakeMKV, parse_drives


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


def _report():
    class R:
        def __init__(s): s.events = []
        def on_event(s, m): s.events.append(m)
        def on_progress(s, *a): pass
    return R()


def _disc_job(source, vol, titles):
    from blurayripper.pipeline import Job
    from blurayripper.models import DiscInfo
    from blurayripper.tools import discinfo
    j = Job(source=source)
    j.disc = DiscInfo(source=source, disc_name="SV S2 D1", volume_label=vol,
                      drive_device="E:", titles=titles)
    j.fingerprint = discinfo.disc_fingerprint(j.disc)
    return j


def _pipe():
    from blurayripper.config import load_settings
    from blurayripper.pipeline import Pipeline
    s = load_settings()
    object.__setattr__(s.tools, "makemkvcon", "makemkvcon")
    return Pipeline(s)


_T = None
def _titles():
    from blurayripper.models import Title
    return [Title(title_id=0, duration_seconds=1711), Title(title_id=1, duration_seconds=1689)]


def test_resolve_noop_when_disc_in_expected_drive(monkeypatch):
    pipe = _pipe()
    job = _disc_job("disc:0", "SV_S2_D1", _titles())
    async def fake_list(timeout=60): return [Drive(0, "E", "E:", "SV_S2_D1"), Drive(1, "F", "F:", "OTHER")]
    monkeypatch.setattr(pipe.makemkv, "list_drives", fake_list)
    r = _report()
    asyncio.run(pipe._resolve_source_drive(job, r))
    assert job.source == "disc:0" and r.events == []


def test_resolve_rebinds_by_disc_title(monkeypatch):
    pipe = _pipe()
    job = _disc_job("disc:0", "SV_S2_D1", _titles())
    async def fake_list(timeout=60): return [Drive(0, "E", "E:", None), Drive(1, "F", "F:", "SV_S2_D1")]
    monkeypatch.setattr(pipe.makemkv, "list_drives", fake_list)
    asyncio.run(pipe._resolve_source_drive(job, _report()))
    assert job.source == "disc:1" and job.disc.source == "disc:1" and job.disc.drive_device == "F:"


def test_resolve_falls_back_to_fingerprint(monkeypatch):
    from blurayripper.models import DiscInfo
    pipe = _pipe()
    job = _disc_job("disc:0", "SV_S2_D1", _titles())
    async def fake_list(timeout=60): return [Drive(0, "E", "E:", None), Drive(1, "F", "F:", "weird")]
    async def fake_scan(sel): return DiscInfo(source=sel, volume_label="SV_S2_D1", titles=_titles())
    monkeypatch.setattr(pipe.makemkv, "list_drives", fake_list)
    monkeypatch.setattr(pipe.makemkv, "scan", fake_scan)
    asyncio.run(pipe._resolve_source_drive(job, _report()))
    assert job.source == "disc:1"   # matched by fingerprint, not title


def test_resolve_leaves_source_when_no_match(monkeypatch):
    pipe = _pipe()
    job = _disc_job("disc:0", "SV_S2_D1", _titles())
    async def fake_list(timeout=60): return [Drive(0, "E", "E:", None)]   # disc nowhere
    monkeypatch.setattr(pipe.makemkv, "list_drives", fake_list)
    asyncio.run(pipe._resolve_source_drive(job, _report()))
    assert job.source == "disc:0"   # untouched -> rip fails with the reinsert message
