"""Drive enumeration: OS-detected discs makemkvcon can't see (blind drives)."""
import asyncio

import pytest

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


def test_ensure_ripped_clears_stale_partial(tmp_path, monkeypatch):
    """A leftover .mkv from a failed prior attempt is removed before re-ripping,
    so rip_title's newly-appeared-file detection works (no false 'no output')."""
    from blurayripper.config import load_settings
    from blurayripper.pipeline import Pipeline, Job
    from blurayripper.models import DiscInfo, Title

    s = load_settings()
    monkeypatch.setattr(s.storage, "staging_dir", tmp_path)
    monkeypatch.setattr(s, "auto_eject", False)
    pipe = Pipeline(s)

    job = Job(source="disc:0")
    job.disc = DiscInfo(source="disc:0", titles=[Title(title_id=5, duration_seconds=1711)])
    job.selected_title_ids = [5]
    staging_root = tmp_path / "job_194"
    rip_dir = staging_root / "rip" / "t005"
    rip_dir.mkdir(parents=True)
    stale = rip_dir / "Silicon Valley S2 D1_t05.mkv"
    stale.write_text("partial from the failed disc:0 attempt")

    seen = {}
    async def fake_rip(source, title_id, dest_dir, **k):
        seen["at_rip"] = sorted(p.name for p in dest_dir.glob("*.mkv"))
        out = dest_dir / f"new_t{title_id:02d}.mkv"
        out.write_text("full rip")
        return [out]
    async def noverify(*a): pass
    monkeypatch.setattr(pipe.makemkv, "rip_title", fake_rip)
    monkeypatch.setattr(pipe, "_verify_rip_duration", noverify)

    ripped = asyncio.run(pipe._ensure_ripped(job, 5, staging_root, _report()))
    assert seen["at_rip"] == []            # the stale partial was cleared first
    assert not stale.exists()
    assert ripped.name == "new_t05.mkv"    # the fresh rip is what gets used


def test_ensure_ripped_on_a_file_job_reports_the_missing_file_not_makemkv(tmp_path, monkeypatch):
    """A job added from an existing .mkv has a file path as its source, not a
    disc selector. If that file disappears there's nothing to re-rip — it must
    say so rather than hand a bare path to makemkvcon, which only ever errors."""
    from blurayripper.config import load_settings
    from blurayripper.pipeline import Pipeline, Job
    from blurayripper.models import DiscInfo, Title

    s = load_settings()
    monkeypatch.setattr(s.storage, "staging_dir", tmp_path)
    pipe = Pipeline(s)

    gone = tmp_path / "WarGames.mkv"          # never created
    job = Job(source=str(gone))
    job.disc = DiscInfo(source=str(gone), titles=[Title(title_id=0, duration_seconds=6700)])
    job.selected_title_ids = [0]
    job.checkpoints = {0: {"ripped_path": str(gone)}}

    async def boom(*a, **k):
        raise AssertionError("MakeMKV must not run for a file-sourced job")

    monkeypatch.setattr(pipe.makemkv, "rip_title", boom)

    with pytest.raises(RuntimeError, match="source file is gone"):
        asyncio.run(pipe._ensure_ripped(job, 0, tmp_path / "job_307", _report()))


def test_eject_device_resolves_from_current_source(monkeypatch):
    """Auto-eject targets the drive that currently maps to the rip source, not a
    stale disc.drive_device — so it never ejects a different job's disc."""
    from blurayripper.pipeline import Job
    from blurayripper.models import DiscInfo
    pipe = _pipe()
    job = Job(source="disc:1")                       # disc was moved to disc:1
    job.disc = DiscInfo(source="disc:1", drive_device="E:", titles=[])  # STALE E:
    async def fake_list(timeout=60):
        return [Drive(0, "E", "E:", "MINORITY_REPORT"), Drive(1, "F", "F:", "SV_S2_D1")]
    monkeypatch.setattr(pipe.makemkv, "list_drives", fake_list)
    assert asyncio.run(pipe._eject_device_for(job)) == "F:"   # NOT the stale E:


def test_eject_device_falls_back_when_enumeration_fails(monkeypatch):
    from blurayripper.pipeline import Job
    from blurayripper.models import DiscInfo
    pipe = _pipe()
    job = Job(source="disc:1")
    job.disc = DiscInfo(source="disc:1", drive_device="F:", titles=[])
    async def boom(timeout=60): raise RuntimeError("drive scan down")
    monkeypatch.setattr(pipe.makemkv, "list_drives", boom)
    assert asyncio.run(pipe._eject_device_for(job)) == "F:"   # stored device
