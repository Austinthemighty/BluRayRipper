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


def test_the_media_poll_notices_a_disc_being_SWAPPED_not_just_removed(monkeypatch):
    """The poll exists to catch a tray closing within a couple of seconds rather
    than at the next 15-second enumeration. Keyed on presence alone it missed the
    commonest case of all — taking one disc out and putting another straight in,
    which leaves the keys identical — so a swapped disc waited out the full poll
    even though the OS knew immediately. Capacity distinguishes them, and costs a
    sysfs read rather than a subprocess."""
    _linux(monkeypatch, devices=["/dev/sr0", "/dev/sr1"], media=["/dev/sr1"])

    def no_subprocess(_dev):
        raise AssertionError("the media poll must not read labels")
    monkeypatch.setattr(makemkv, "_linux_disc_label", no_subprocess)

    sizes = {"/dev/sr1": 48_627_580_928}
    monkeypatch.setattr(makemkv, "os_disc_capacity_bytes", lambda d: sizes.get(d))
    before = makemkv.os_media_snapshot()
    assert before == {"/dev/sr1": "48627580928"}

    # Same drive, same "a disc is present", different disc.
    sizes["/dev/sr1"] = 47_530_508_288
    assert makemkv.os_media_snapshot() != before, "a swapped disc must read as a change"

    # A tray closing on the other drive still shows up as a new key.
    monkeypatch.setattr(makemkv, "_linux_media_present", lambda d: True)
    sizes["/dev/sr0"] = 25_000_000_000
    assert set(makemkv.os_media_snapshot()) == {"/dev/sr0", "/dev/sr1"}

    # A drive whose size can't be read still counts as holding something.
    monkeypatch.setattr(makemkv, "os_disc_capacity_bytes", lambda d: None)
    assert makemkv.os_media_snapshot() == {"/dev/sr0": "disc", "/dev/sr1": "disc"}


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


# --- UHD in a drive that cannot read it -------------------------------------
#
# Two physically identical ASUS BW-16D1HT drives, one on firmware 3.01 and one
# on 3.10, behaved completely differently: the 3.10 one had patched (LibreDrive)
# firmware and read UHD discs, the 3.01 one could not read them at all and threw
# "HARDWARE ERROR:TIMEOUT ON LOGICAL UNIT" in the first megabyte. Its retries
# then blocked makemkvcon's drive enumeration, which emits no DRV lines at all
# while that is happening — so the OTHER drive looked dead, and a job that had
# already finished ripping hung on its post-rip drive lookup.
#
# Nothing an operator can see tells the two apart. The firmware version doesn't:
# the HIGHER number was the capable one. These fixtures are that box's real
# makemkvcon output.

_LIBREDRIVE_SCAN = [
    'MSG:1005,0,1,"MakeMKV v1.18.3 linux(x64-release) started","%1 started","x"',
    'DRV:0,2,999,12,"BD-RE ASUS BW-16D1HT 3.10 KX1P71E4206","LANDMAN_SEASON2","/dev/sr1"',
    'DRV:1,1,999,0,"BD-RE ASUS BW-16D1HT 3.01 KLUL75I4959","","/dev/sr0"',
    'MSG:1011,0,1,"Using LibreDrive mode (v06.3 id=0FA242DD4D0B)","%1","x"',
    'MSG:3007,0,0,"Using direct disc access mode","Using direct disc access mode"',
    'TINFO:0,9,0,"1:52:42"',
]


def test_a_scan_records_which_drive_it_used_by_serial_not_by_index():
    """disc:N and /dev/srN are both assigned by discovery order and got swapped
    across a reboot on this box. The model+serial string is the only stable
    identity a drive has, so that is what a capability gets filed under."""
    disc = parse_drives(_LIBREDRIVE_SCAN)   # sanity: both drives parse
    assert [d.device for d in disc] == ["/dev/sr1", "/dev/sr0"]

    from blurayripper.tools.makemkv import parse_info
    scanned = parse_info(_LIBREDRIVE_SCAN, "disc:0")
    assert scanned.drive_name == "BD-RE ASUS BW-16D1HT 3.10 KX1P71E4206"
    assert scanned.read_error is False
    # dev: sources resolve the same drive the long way round, via its device.
    assert parse_info(_LIBREDRIVE_SCAN, "dev:/dev/sr1").drive_name == scanned.drive_name


def test_a_read_error_is_attributed_to_the_drive_that_was_opened():
    """What replaced the "Using LibreDrive mode" announcement, which could not be
    used: MakeMKV emits it for whichever capable drive holds a disc, not for the
    drive being opened, so it marked the drive that CANNOT read UHD as the one
    that can. A read error is about the drive that was actually read."""
    from blurayripper.tools.makemkv import parse_info

    failed = [
        'MSG:1005,0,1,"MakeMKV v1.18.3 linux(x64-release) started","%1 started","x"',
        'DRV:1,2,999,12,"BD-RE ASUS BW-16D1HT 3.01 KLUL75I4959","BOURNE","/dev/sr0"',
        'MSG:2003,0,3,"Error \'Scsi error - HARDWARE ERROR:TIMEOUT ON LOGICAL UNIT\' '
        'occurred while reading at offset \'524288\'","%1","x"',
    ]
    scanned = parse_info(failed, "disc:1")
    assert scanned.read_error is True
    assert scanned.drive_name == "BD-RE ASUS BW-16D1HT 3.01 KLUL75I4959"
    assert scanned.titles == []          # nothing readable came back

    # A clean scan of the same drive says the opposite, from the same field.
    assert parse_info(_LIBREDRIVE_SCAN, "disc:0").read_error is False


def test_disc_capacity_comes_from_the_kernel_so_it_works_on_the_bad_drive(
        monkeypatch, tmp_path):
    """The whole point of asking the OS: the kernel answers READ CAPACITY even
    on the drive that cannot decrypt the content. That is what lets a UHD disc
    be recognised BEFORE handing it to a drive that would wedge on it."""
    from blurayripper.tools import makemkv as mk

    (tmp_path / "sr9").mkdir()
    (tmp_path / "sr9" / "size").write_text("118386688\n")   # 512-byte sectors
    monkeypatch.setattr(mk.sys, "platform", "linux")
    monkeypatch.setattr(mk, "Path", lambda p=".": _FakeSysfs(p, tmp_path))

    assert mk.os_disc_capacity_bytes("/dev/sr9") == 118386688 * 512
    assert mk.os_disc_capacity_bytes("/dev/sr9") > mk.UHD_CAPACITY_FLOOR_BYTES

    # An empty tray reads 0 sectors, which is "unknown", not "a 0-byte disc".
    (tmp_path / "sr9" / "size").write_text("0\n")
    assert mk.os_disc_capacity_bytes("/dev/sr9") is None


class _FakeSysfs:
    """Redirects /sys/block reads into tmp_path, leaving .name behaving."""

    def __init__(self, p, root):
        from pathlib import Path as _P
        self._p = _P(str(p).replace("/sys/block", str(root)))

    @property
    def name(self):
        return self._p.name

    def read_text(self, *a, **k):
        return self._p.read_text(*a, **k)


def test_capacity_has_no_opinion_where_it_cannot_ask(monkeypatch):
    """None must mean "no idea", never "small" — a caller that refused work on a
    None would refuse every disc on Windows."""
    from blurayripper.tools import makemkv as mk

    monkeypatch.setattr(mk.sys, "platform", "win32")
    assert mk.os_disc_capacity_bytes("F:") is None
    monkeypatch.setattr(mk.sys, "platform", "linux")
    assert mk.os_disc_capacity_bytes(None) is None
    assert mk.os_disc_capacity_bytes("/dev/sda") is None     # not an optical dev
    assert mk.os_disc_capacity_bytes("/dev/sr404") is None   # no such sysfs node


def test_a_drives_outcome_survives_a_useless_scan_and_a_corrupt_file(tmp_path):
    """Outcomes are learned once and relied on later, so the store has to be
    harder to lose than it is to write."""
    from blurayripper.services.drive_caps import DriveCaps

    path = tmp_path / "drive_caps.json"
    caps = DriveCaps(path)
    assert caps.uhd_ok("BD-RE ASUS 3.10 SERIAL") is None   # nothing known yet

    caps.note_uhd_result("BD-RE ASUS 3.10 SERIAL", True)
    caps.note_uhd_result("BD-RE ASUS 3.01 OTHER", False)
    assert DriveCaps(path).uhd_ok("BD-RE ASUS 3.10 SERIAL") is True
    assert DriveCaps(path).uhd_ok("BD-RE ASUS 3.01 OTHER") is False
    assert DriveCaps(path).uhd_capable_drives() == ["BD-RE ASUS 3.10 SERIAL"]

    # A scan that proved nothing reports None, which is not a verdict.
    caps.note_uhd_result("BD-RE ASUS 3.10 SERIAL", None)
    assert DriveCaps(path).uhd_ok("BD-RE ASUS 3.10 SERIAL") is True

    # A hand-edit gone wrong loses the knowledge, not the drives panel.
    path.write_text("[]")
    assert DriveCaps(path).uhd_ok("BD-RE ASUS 3.10 SERIAL") is None
    DriveCaps(tmp_path / "never-written.json").note_uhd_result("x", None)  # no raise
