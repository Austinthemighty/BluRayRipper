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


def test_os_optical_drives_is_noop_on_an_os_we_cannot_probe(monkeypatch):
    monkeypatch.setattr(makemkv.sys, "platform", "freebsd13")
    assert makemkv.os_optical_drives() == []
    assert makemkv.os_media_snapshot() is None   # "can't tell", not "nothing there"
    assert makemkv.os_disc_label("/dev/cd0") is None


# --- the Linux half of all three, which did not exist -----------------------
#
# Every one of these helpers was Windows-only, so on a Linux rip box
# makemkvcon's cached quick probe was the only thing that could say a disc was
# present. When it came back empty the panel showed an empty drive, with no
# recovery and nothing to distinguish that from a drive that had dropped off
# the bus — which is what the newer of two identical drives was doing.

def _linux(monkeypatch, *, devices=(), media=(), labels=None):
    monkeypatch.setattr(makemkv.sys, "platform", "linux")
    monkeypatch.setattr(makemkv, "_linux_optical_devices", lambda: list(devices))
    monkeypatch.setattr(makemkv, "_linux_media_present", lambda d: d in media)
    monkeypatch.setattr(makemkv, "_linux_disc_label",
                        lambda d: (labels or {}).get(d))


def test_on_linux_the_os_recovers_a_disc_name_makemkv_missed(monkeypatch):
    """makemkvcon's quick probe reports the drive but no disc — the exact state
    the newer drive kept landing in. udev knows the label, so the card can name
    the disc instead of showing an empty drive."""
    _linux(monkeypatch, devices=["/dev/sr0", "/dev/sr1"], media=["/dev/sr1"],
           labels={"/dev/sr1": "SHERLOCK_GNOMES"})
    assert makemkv.os_disc_label("/dev/sr1") == "SHERLOCK_GNOMES"
    # No media -> no label, rather than a stale one from the last disc.
    assert makemkv.os_disc_label("/dev/sr0") is None


def test_on_linux_a_drive_makemkv_cannot_enumerate_still_shows_up(monkeypatch):
    """A drive that has half dropped off the bus makes makemkvcon report "no
    usable optical drives" for it. On Windows it still appeared, flagged blind;
    on Linux it vanished from the panel entirely."""
    _linux(monkeypatch, devices=["/dev/sr1"], media=["/dev/sr1"],
           labels={"/dev/sr1": "FIFTY_SHADES_FREED"})
    assert makemkv.os_optical_drives() == [("/dev/sr1", "FIFTY_SHADES_FREED")]

    mk = _mk(monkeypatch, [], makemkv.os_optical_drives())
    drives = asyncio.run(mk.list_drives(timeout=5))
    assert len(drives) == 1
    assert drives[0].makemkv_blind and drives[0].index == -1
    assert drives[0].device == "/dev/sr1" and drives[0].os_detected
    assert drives[0].disc_name == "FIFTY_SHADES_FREED"


def test_on_linux_a_device_form_dedupes_against_what_makemkv_reported(monkeypatch):
    """The Linux pairs are (device, label), which is the form Drive.device takes
    there — so a drive BOTH sources can see must not appear twice, once with an
    Add button for the same disc."""
    _linux(monkeypatch, devices=["/dev/sr0"], media=["/dev/sr0"],
           labels={"/dev/sr0": "SHERLOCK_GNOMES"})
    mk = _mk(monkeypatch,
             ['DRV:0,2,999,12,"ASUS BD","SHERLOCK_GNOMES","/dev/sr0"'],
             makemkv.os_optical_drives())
    drives = asyncio.run(mk.list_drives(timeout=5))
    assert len(drives) == 1 and drives[0].index == 0
    assert drives[0].makemkv_blind is False


def test_on_linux_the_media_poll_reports_devices_and_stays_cheap(monkeypatch):
    """The poll runs every couple of seconds purely to notice a tray closing, so
    its keys are what must change — never a subprocess per drive per tick."""
    _linux(monkeypatch, devices=["/dev/sr0", "/dev/sr1"], media=["/dev/sr1"])

    def no_subprocess(_dev):
        raise AssertionError("the media poll must not read labels")
    monkeypatch.setattr(makemkv, "_linux_disc_label", no_subprocess)

    assert makemkv.os_media_snapshot() == {"/dev/sr1": "disc"}
    # A tray closing on the other drive changes the keys, which is the signal.
    monkeypatch.setattr(makemkv, "_linux_media_present", lambda d: True)
    assert set(makemkv.os_media_snapshot()) == {"/dev/sr0", "/dev/sr1"}


def test_the_media_probe_opens_the_device_without_waiting_for_it(monkeypatch):
    """O_NONBLOCK is the whole safety story. Opening an optical device normally
    waits for the disc to spin up and settle, and on a drive that has stopped
    answering that wait is unbounded — inside a two-second poll, on the box that
    runs the server. The flag has to be there."""
    import os as real_os

    opened = {}

    def fake_open(path, flags):
        opened["path"], opened["flags"] = path, flags
        return 7

    monkeypatch.setattr(makemkv.sys, "platform", "linux")
    monkeypatch.setattr(real_os, "open", fake_open)
    monkeypatch.setattr(real_os, "close", lambda fd: None)
    import fcntl
    monkeypatch.setattr(fcntl, "ioctl", lambda fd, req, arg: 4)  # CDS_DISC_OK

    assert makemkv._linux_media_present("/dev/sr1") is True
    assert opened["path"] == "/dev/sr1"
    assert opened["flags"] & real_os.O_NONBLOCK, "must not block on a wedged drive"
    assert opened["flags"] & real_os.O_RDONLY == real_os.O_RDONLY

    # An empty tray, a tray that is open, and a drive that errors are all "no".
    monkeypatch.setattr(fcntl, "ioctl", lambda fd, req, arg: 1)  # CDS_NO_DISC
    assert makemkv._linux_media_present("/dev/sr1") is False

    def refuses(fd, req, arg):
        raise OSError("no medium found")
    monkeypatch.setattr(fcntl, "ioctl", refuses)
    assert makemkv._linux_media_present("/dev/sr1") is False

    def cannot_open(path, flags):
        raise OSError("permission denied")
    monkeypatch.setattr(real_os, "open", cannot_open)
    assert makemkv._linux_media_present("/dev/sr1") is False


def test_a_label_lookup_tolerates_a_box_without_udevadm(monkeypatch):
    """Best-effort means best-effort: a container or a stripped image with no
    udevadm must cost a missing label, not an exception on the drives panel."""
    import subprocess as real_subprocess

    def missing(*a, **k):
        raise FileNotFoundError("udevadm")
    monkeypatch.setattr(real_subprocess, "run", missing)
    assert makemkv._linux_disc_label("/dev/sr1") is None

    class _Out:
        stdout = "DEVNAME=/dev/sr1\nID_FS_LABEL=THE_MATRIX\nID_FS_TYPE=udf\n"
    monkeypatch.setattr(real_subprocess, "run", lambda *a, **k: _Out())
    assert makemkv._linux_disc_label("/dev/sr1") == "THE_MATRIX"

    class _Blank:
        stdout = "DEVNAME=/dev/sr1\nID_FS_LABEL=\n"
    monkeypatch.setattr(real_subprocess, "run", lambda *a, **k: _Blank())
    assert makemkv._linux_disc_label("/dev/sr1") is None


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
