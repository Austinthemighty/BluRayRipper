"""Story: "this disc won't rip — tell me why, and tell me what to do about it."

A red card that says ``RuntimeError`` teaches nobody anything. The operator
standing at the machine has exactly three physical moves available — clean the
disc, move it to the other drive, or fix the drive itself — plus one that isn't
physical at all: a UHD disc whose volume key MakeMKV doesn't have, which no
amount of cleaning will ever fix and which is only ever solved by emailing the
dump MakeMKV wrote.

So the failure has to say WHICH of those it is:

* an unknown volume key explains itself in words, keeps the ``.tgz`` MakeMKV
  buried in its own data directory, offers it as a download, and is explicit
  that nothing was sent anywhere on the user's behalf;
* a drive that dropped off the bus and a disc that merely won't read get
  different remedies, because doing one for the other wastes an evening;
* the dump left by the LAST failing disc is never pinned on this one;
* and once the disc is re-seated (or moved to the other drive), Retry picks the
  same job back up instead of starting a fresh one that re-reads what has
  already been read.

The pieces have unit coverage (tests/test_aacs_dump.py, test_failure_hint.py,
test_web_api_endpoints.py). This is the narrative that proves they line up:
what MakeMKV wrote, what the manager kept, what the card says, and what the
button then actually does.
"""

import asyncio
import copy
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, EpisodeAssignment, MediaKind, Title
from blurayripper.persistence import MemoryStore
from blurayripper.pipeline import Job, JobState
from blurayripper.tools.makemkv import Drive
from blurayripper.web import manager as mgr
from blurayripper.web.app import create_app
from blurayripper.web.manager import JobManager

# What makemkvcon actually prints when it meets a UHD disc it has no key for:
# it opens nothing and offers no titles, having quietly dropped a .tgz in its
# own data directory. The wording varies by version, which is exactly why the
# app must not key off it.
MAKEMKV_SAYS = "Failed to open disc"


def _settings(tmp_path: Path):
    """A server whose library, staging and config all live under tmp_path."""
    cfg = tmp_path / "cfg"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "settings.toml").write_text("", encoding="utf-8")
    settings = load_settings(cfg)
    settings.config_dir = cfg
    settings.database_url = None          # never reach for a real Postgres
    settings.storage.output_dir = tmp_path / "out"
    settings.storage.staging_dir = tmp_path / "staging"
    settings.notify_url = None            # no real pushes from tests
    settings.auto_rip = False             # naming is a click, not a TMDB lookup
    return settings


async def _settle() -> None:
    """Let the manager's background tasks (scan, run, retry, push) finish."""
    for _ in range(60):
        await asyncio.sleep(0)


def _drive(index: int, device: str, disc_name: str | None = None, **kw) -> Drive:
    return Drive(index=index, name=f"ASUS BW-16D1HT #{index}", device=device,
                 disc_name=disc_name, **kw)


def _mkv_data_dir(manager: JobManager, tmp_path: Path, monkeypatch) -> Path:
    """Stand in for MakeMKV's own data directory — the place the dump lands and
    that the user is never going to go looking in."""
    data_dir = tmp_path / "MakeMKV"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mgr.aacs_dump, "data_dir", lambda *a, **k: data_dir)
    return data_dir


def _unreadable_scan(disc_name: str, *, dump: Path | None = None,
                     says: str = MAKEMKV_SAYS):
    """A scan of a disc MakeMKV can't get into: it logs its own reason, maybe
    writes a dump, and comes back with a disc that has no titles at all."""
    async def scan(source, report):
        report.on_event(says)
        if dump is not None:
            dump.parent.mkdir(parents=True, exist_ok=True)
            dump.write_bytes(b"\x1f\x8b the volume key request")
        job = Job(source=source)
        job.disc = DiscInfo(source=source, disc_name=disc_name, drive_device="F:",
                            titles=[])
        return job
    return scan


def _good_scan(disc_name: str, *, durations: tuple[int, ...] = (7000,),
               device: str = "F:"):
    """A disc that scans fine — used when the failure comes later, at rip time.
    One feature-length title reads as a movie; several episode-length ones as a
    TV disc, which is what decides whether the title gate appears."""
    async def scan(source, report):
        job = Job(source=source)
        job.disc = DiscInfo(
            source=source, disc_name=disc_name, drive_device=device,
            titles=[Title(title_id=t, duration_seconds=secs, chapter_count=20,
                          size_bytes=40_000_000_000, source_filename=f"0000{t}.mpls")
                    for t, secs in enumerate(durations)])
        return job
    return scan


def _log(wj) -> str:
    return "\n".join(wj.log)


def _line_index(wj, needle: str) -> int:
    """Where a line first appears in the job log (the order the operator reads)."""
    for i, line in enumerate(wj.log):
        if needle in line:
            return i
    raise AssertionError(f"{needle!r} never appears in the log:\n{_log(wj)}")


def _capture_pushes(settings, monkeypatch) -> list[tuple[str, str]]:
    settings.notify_url = "http://ntfy.example/blurayripper"
    pushed: list[tuple[str, str]] = []

    async def fake_notify(url, title, message, priority=None, click=None):
        pushed.append((title, message))
        return True

    monkeypatch.setattr("blurayripper.web.manager.notify", fake_notify)
    return pushed


# --------------------------------------------------------------------------
# 1. the disc MakeMKV has no key for
# --------------------------------------------------------------------------

async def test_an_unknown_volume_key_explains_itself_and_hands_over_the_dump(
        tmp_path, monkeypatch):
    """The whole unknown-key journey, because no chapter is any use alone.

    Explaining without keeping the dump leaves the user hunting a directory
    they've never heard of; keeping it without explaining leaves a .tgz nobody
    knows to email; and either one without "nothing has been sent" leaves them
    wondering what this box just did with their disc.
    """
    settings = _settings(tmp_path)
    app = create_app(settings)
    m = app.state.manager
    data_dir = _mkv_data_dir(m, tmp_path, monkeypatch)
    original = data_dir / "aacs_dump_9f21c4.tgz"
    monkeypatch.setattr(
        m.pipeline, "scan_disc", _unreadable_scan("TRON_LEGACY_UHD", dump=original))

    wj = m.add_disc("disc:0")
    await _settle()

    assert wj.state is JobState.FAILED

    # --- what the card says --------------------------------------------------
    # MakeMKV's own words are kept (they are the evidence)…
    assert MAKEMKV_SAYS in _log(wj)
    # …but the failure the operator reads is in their terms: the candidate
    # causes, and the two moves they can actually make.
    assert wj.error is not None
    assert "found no titles on this disc" in wj.error
    assert "unreadable or dirty disc" in wj.error
    assert "a protection MakeMKV couldn't handle" in wj.error
    assert "Clean the disc, or use another drive and Retry." in wj.error

    # --- and then the specific diagnosis, in the order it is read ------------
    advice = wj.log[_line_index(wj, "AACS dump saved")]
    assert _line_index(wj, MAKEMKV_SAYS) < _line_index(wj, "Scan failed:") \
        < _line_index(wj, "AACS dump saved")
    assert "MakeMKV doesn't know this disc's volume key" in advice
    assert "svq@makemkv.com" in advice          # where it has to go
    assert "12-48h" in advice                   # and what happens after
    assert "Nothing has been sent." in advice   # the app did not email anyone

    # --- the dump itself -----------------------------------------------------
    kept = Path(wj.aacs_dump)
    assert kept.parent == settings.storage.staging_dir / "aacs-dumps"
    assert "TRON_LEGACY_UHD" in kept.name       # named for the disc, not "abc123"
    assert kept.read_bytes() == b"\x1f\x8b the volume key request"
    # Copied, never moved: MakeMKV keeps its own, and a failed copy must not
    # destroy the only record of a disc that won't read.
    assert original.exists()
    assert original.read_bytes() == kept.read_bytes()

    # The card offers a filename, not a server path — it is a download button.
    card = m._job_dict(wj)
    assert card["aacs_dump"] == kept.name
    assert "/" not in card["aacs_dump"] and "\\" not in card["aacs_dump"]

    # --- and the download really serves that file ---------------------------
    client = TestClient(app)
    r = client.get(f"/api/jobs/{wj.id}/aacs-dump")
    assert r.status_code == 200
    assert r.content == b"\x1f\x8b the volume key request"
    assert r.headers["content-type"] == "application/gzip"
    assert kept.name in r.headers["content-disposition"]


async def test_the_dump_left_by_the_last_failing_disc_is_not_pinned_on_this_one(
        tmp_path, monkeypatch):
    """Every failing disc drops another .tgz beside the last one, so "newest
    dump in the directory" is not the same question as "this disc's dump".

    Getting it wrong is worse than saying nothing: the second disc is a dirty
    DVD, and the card would tell the operator to email a stranger's volume-key
    request and wait 48h for a fix that was never going to come.
    """
    settings = _settings(tmp_path)
    app = create_app(settings)
    m = app.state.manager
    data_dir = _mkv_data_dir(m, tmp_path, monkeypatch)

    # --- disc one: a UHD with no known key ----------------------------------
    stale = data_dir / "aacs_dump_first.tgz"
    monkeypatch.setattr(
        m.pipeline, "scan_disc", _unreadable_scan("TRON_LEGACY_UHD", dump=stale))
    uhd = m.add_disc("disc:0")
    await _settle()
    assert Path(uhd.aacs_dump).exists()

    # It is still sitting in MakeMKV's data directory an hour later.
    old = time.time() - 3600
    os.utime(stale, (old, old))

    # --- disc two: a scratched DVD. MakeMKV writes no dump for it -----------
    monkeypatch.setattr(
        m.pipeline, "scan_disc",
        _unreadable_scan("HOUSE_S1_D3", says="SCSI error - MEDIUM ERROR"))
    dvd = m.add_disc("disc:1")
    await _settle()

    assert dvd.state is JobState.FAILED
    assert dvd.aacs_dump is None
    assert "AACS" not in _log(dvd)
    assert "svq@makemkv.com" not in _log(dvd)
    assert m._job_dict(dvd)["aacs_dump"] is None

    client = TestClient(app)
    gone = client.get(f"/api/jobs/{dvd.id}/aacs-dump")
    assert gone.status_code == 404
    assert "No AACS dump" in gone.json()["detail"]
    # …while the disc that really did leave one still hands it over.
    assert client.get(f"/api/jobs/{uhd.id}/aacs-dump").status_code == 200
    # And exactly one dump was ever kept — the second disc copied nothing.
    assert len(list((settings.storage.staging_dir / "aacs-dumps").glob("*.tgz"))) == 1


async def test_a_dump_that_cannot_be_kept_never_replaces_the_reason_the_disc_failed(
        tmp_path, monkeypatch):
    """Keeping the dump is a courtesy on top of a failure that already happened.

    If the copy itself blows up (staging on a NAS that just went away), the
    operator must still be told what the disc did — and the job must still be
    written to the store, or a restart loses the failure entirely.
    """
    settings = _settings(tmp_path)
    store = MemoryStore()
    m = JobManager(settings, store=store)
    data_dir = _mkv_data_dir(m, tmp_path, monkeypatch)
    monkeypatch.setattr(
        m.pipeline, "scan_disc",
        _unreadable_scan("TRON_LEGACY_UHD", dump=data_dir / "aacs_dump_9f21.tgz"))

    def exploding_collect(dump, dest_dir, disc_name=None):
        raise OSError("[Errno 5] Input/output error: Z:\\staging")

    monkeypatch.setattr(mgr.aacs_dump, "collect", exploding_collect)

    wj = m.add_disc("disc:0")
    await _settle()

    assert wj.state is JobState.FAILED
    assert "found no titles on this disc" in (wj.error or "")   # the real reason
    assert "OSError" not in (wj.error or "")                    # not the courtesy's
    assert "Couldn't keep the AACS dump: OSError." in _log(wj)
    assert wj.aacs_dump is None
    assert m._job_dict(wj)["aacs_dump"] is None

    # The failure survived to the store, so a restart still shows the red card.
    saved = {r.id: r for r in store.load_all()}[wj.id]
    assert saved.state == "failed"
    assert "found no titles on this disc" in (saved.error or "")


# --------------------------------------------------------------------------
# 2. a dead drive is not a dead disc
# --------------------------------------------------------------------------

async def test_a_drive_off_the_bus_and_a_disc_that_wont_read_get_different_remedies(
        tmp_path, monkeypatch):
    """Three rips fail. Only the failure TEXT differs, so anything the cards say
    differently comes from reading it.

    The remedies are mutually exclusive in practice: reseating the disc for a
    drive that has dropped off the bus does nothing at all, and power-cycling a
    healthy drive for a scratched disc does nothing either. And neither is worth
    an automatic retry — a machine quietly re-reading a dead drive twenty times
    overnight is how a five-minute fix becomes a lost evening.
    """
    settings = _settings(tmp_path)
    settings.auto_retry_max = 2
    m = JobManager(settings)
    pushed = _capture_pushes(settings, monkeypatch)
    # All three discs are physically in their drives: nothing below may be
    # explained away by "the disc isn't loaded".
    m._drives_cache = [_drive(0, "F:", "TRON_LEGACY_UHD"),
                       _drive(1, "G:", "HOUSE_S1_D3"),
                       _drive(2, "H:", "WARGAMES")]
    m._drives_scanned = True

    labels = ("TRON_LEGACY_UHD", "HOUSE_S1_D3", "WARGAMES")
    failures = {
        "disc:0": "ProcessError: makemkvcon reported no usable optical drives",
        "disc:1": "ProcessError: SCSI error - MEDIUM ERROR, read failed at LBA 91234",
        "disc:2": "ProcessError: HandBrakeCLI exited with 3",
    }

    async def scan(source, report):
        idx = int(source.split(":")[1])
        return await _good_scan(labels[idx])(source, report)

    async def execute(job, scheduler, report, job_name=None):
        raise RuntimeError(failures[job.source])

    monkeypatch.setattr(m.pipeline, "scan_disc", scan)
    monkeypatch.setattr(m.pipeline, "execute", execute)

    jobs = {}
    for idx in range(3):
        jobs[idx] = m.add_disc(f"disc:{idx}")
    await _settle()
    assert all(wj.state is JobState.AWAITING_MAPPING for wj in jobs.values())

    # From here on, every push is a failure push (the scan gates already pushed).
    pushed.clear()
    for idx, name in enumerate(("TRON: Legacy", "House", "WarGames")):
        m.set_mapping(jobs[idx].id,
                      [EpisodeAssignment(0, MediaKind.MOVIE, name, 2010)])
    await _settle()

    dead_drive, bad_disc, ordinary = jobs[0], jobs[1], jobs[2]
    assert all(wj.state is JobState.FAILED for wj in jobs.values())

    # --- the drive, not the disc --------------------------------------------
    drive_hint = _log(dead_drive)
    assert "dropped off the bus" in drive_hint
    assert "The disc being inserted isn't enough" in drive_hint
    assert "power/connection" in drive_hint
    assert "Clean the disc" not in drive_hint      # would be the wrong evening

    # --- the disc, not the drive --------------------------------------------
    disc_hint = _log(bad_disc)
    assert "Disc read error" in disc_hint
    assert "bad sector" in disc_hint and "dirty/scratched disc" in disc_hint
    assert "dropped off the bus" not in disc_hint  # nothing to power-cycle

    # --- neither burns a retry, and both ring the phone with the fix --------
    for wj in (dead_drive, bad_disc):
        assert "Auto-retry" not in _log(wj)
        assert wj.id not in m._auto_retries
    assert len(pushed) == 2 and all("needs attention" in t for t, _m in pushed)
    by_job = {msg.split(":")[0]: msg for _t, msg in pushed}
    assert "dropped off the bus" in by_job[f"Job #{dead_drive.id}"]
    assert "Disc read error" in by_job[f"Job #{bad_disc.id}"]

    # --- while an ordinary crash is simply retried, silently ----------------
    assert "Auto-retry 1/2 in 30s" in _log(ordinary)
    assert m._auto_retries[ordinary.id] == 1
    assert "dropped off the bus" not in _log(ordinary)
    assert "Disc read error" not in _log(ordinary)
    assert f"Job #{ordinary.id}" not in by_job          # nobody was woken for it

    await m.shutdown()   # cancel the pending 30s auto-retry


async def test_a_drive_the_os_sees_but_makemkv_cannot_open_is_shown_yet_never_started(
        tmp_path, monkeypatch):
    """The half-offline drive: Windows still lists the disc, makemkvcon can't
    enumerate the drive at all. Auto-opening it would queue a job that can only
    fail — and would keep doing it — so the drive is surfaced, flagged, and left
    alone. It also must not be written to the drives cache: a phantom drive that
    survives a restart is a drive the user cannot make go away.
    """
    settings = _settings(tmp_path)
    app = create_app(settings)
    m = app.state.manager
    added: list[str] = []
    monkeypatch.setattr(m, "add_disc", lambda source: added.append(source))

    drives = [_drive(0, "F:", "TRON_LEGACY_UHD"),
              Drive(index=-1, name="(optical drive)", device="G:",
                    disc_name="HOUSE_S1_D3", os_detected=True, makemkv_blind=True)]
    m._drives_cache = drives
    m._auto_open(drives)

    # The healthy drive's disc is queued; the wedged one's is not.
    assert added == ["disc:0"]

    # But the operator can still see it, and see that the DRIVE is the problem.
    listed = TestClient(app).get("/api/drives").json()
    assert [d["device"] for d in listed] == ["F:", "G:"]
    blind = listed[1]
    assert blind["disc_name"] == "HOUSE_S1_D3"   # the disc is fine, it's readable
    assert blind["makemkv_blind"] is True
    assert listed[0]["makemkv_blind"] is False

    # Only the real drive is remembered across a restart.
    m._save_drives_cache()
    assert [d.device for d in m._load_drives_cache()] == ["F:"]


# --------------------------------------------------------------------------
# 3. what happens after the operator fixes it
# --------------------------------------------------------------------------

async def test_moving_the_disc_to_the_other_drive_makes_retry_rip_from_that_drive(
        tmp_path, monkeypatch):
    """Using another drive is one of the two remedies the failure names, so it
    has to actually work. Re-pointing must take the DEVICE with it as well as
    the selector: auto-eject runs off the device, and a stale one pops the tray
    on a drive holding somebody else's disc.
    """
    settings = _settings(tmp_path)
    m = JobManager(settings)
    m._drives_cache = [_drive(0, "F:", "TRON_LEGACY_UHD"), _drive(1, "G:")]
    m._drives_scanned = True
    seen: list[tuple[str, str | None]] = []
    fail = {"now": True}

    async def execute(job, scheduler, report, job_name=None):
        seen.append((job.source, job.disc.drive_device))
        if fail["now"]:
            raise RuntimeError("ProcessError: SCSI error - MEDIUM ERROR at LBA 91234")
        job.checkpoints[0] = {"ripped_path": f"{job_name}/rip/t000/title_t00.mkv"}
        return job

    monkeypatch.setattr(m.pipeline, "scan_disc", _good_scan("TRON_LEGACY_UHD"))
    monkeypatch.setattr(m.pipeline, "execute", execute)

    wj = m.add_disc("disc:0")
    await _settle()
    m.set_mapping(wj.id, [EpisodeAssignment(0, MediaKind.MOVIE, "TRON: Legacy", 2010)])
    await _settle()
    assert wj.state is JobState.FAILED
    assert seen == [("disc:0", "F:")]

    # The operator carries the disc to the top drive and tells the job so.
    m.change_source(wj.id, "disc:1")
    assert "Rip source changed to disc:1 — click Retry to rip from it." in _log(wj)
    assert wj.state is JobState.FAILED   # saying where it is isn't starting it

    fail["now"] = False
    m.retry(wj.id)
    await _settle()

    assert wj.state is JobState.DONE
    # The second rip really went to the other drive — selector AND device.
    assert seen == [("disc:0", "F:"), ("disc:1", "G:")]


async def test_retrying_a_re_seated_disc_resumes_the_same_job_instead_of_a_new_one(
        tmp_path, monkeypatch):
    """A two-episode disc dies with one episode read. Retry has to be the same
    job carrying the same checkpoints, or the drive spends another 40 minutes
    re-reading an episode that is already on disk — and the card has to say
    which of those two things the button is about to do, since one needs the
    disc in the machine and the other doesn't.
    """
    settings = _settings(tmp_path)
    m = JobManager(settings)
    m._drives_cache = [_drive(0, "F:", "HOUSE_S1_D3")]
    m._drives_scanned = True
    ripped: list[int] = []
    handed: list[dict] = []
    stop = {"at": "rip"}

    async def execute(job, scheduler, report, job_name=None):
        handed.append(copy.deepcopy(job.checkpoints))
        for title_id in job.selected_title_ids:
            if job.checkpoints.get(title_id, {}).get("ripped_path"):
                continue          # already read off the disc — reuse it
            if stop["at"] == "rip" and title_id == 1:
                raise RuntimeError("ProcessError: SCSI error - MEDIUM ERROR")
            ripped.append(title_id)
            job.checkpoints[title_id] = {
                "ripped_path": f"{job_name}/rip/t{title_id:03d}/title.mkv"}
        if stop["at"] == "encode":
            raise RuntimeError("ProcessError: HandBrakeCLI exited with 3")
        return job

    monkeypatch.setattr(m.pipeline, "scan_disc",
                        _good_scan("HOUSE_S1_D3", durations=(2700, 2650)))
    monkeypatch.setattr(m.pipeline, "execute", execute)

    wj = m.add_disc("disc:0")
    await _settle()
    assert wj.state is JobState.AWAITING_TITLES     # two episode-length titles
    m.set_titles(wj.id, [0, 1])
    m.set_mapping(wj.id, [
        EpisodeAssignment(0, MediaKind.TV, "House", 2004, season=1, episode=5),
        EpisodeAssignment(1, MediaKind.TV, "House", 2004, season=1, episode=6)])
    await _settle()

    # --- chapter 1: episode one is read, then the disc stops co-operating ---
    assert wj.state is JobState.FAILED
    assert ripped == [0]
    card = m._job_dict(wj)
    assert card["needs_disc"] is True      # there is still an episode to read
    assert card["retry_rips"] is True      # so Retry means "put the disc back"
    assert card["needs_review"] is True

    # --- chapter 2: the disc is cleaned and re-seated; Retry picks up -------
    stop["at"] = "encode"
    m.retry(wj.id)
    # A second impatient click must not start a rival rip on the same drive.
    with pytest.raises(ValueError):
        m.retry(wj.id)
    await _settle()

    assert list(m.jobs) == [wj.id]                    # one job, not two
    assert ripped == [0, 1]                           # episode one was NOT re-read
    assert handed[1] == {0: {"ripped_path": f"job_{wj.id}/rip/t000/title.mkv"}}
    assert wj.state is JobState.FAILED                # …the encode died instead

    # The disc has given everything it has, so the card stops asking for it.
    card = m._job_dict(wj)
    assert card["needs_disc"] is False
    assert card["retry_rips"] is False     # Retry now means "resume"
    assert card["needs_review"] is False

    # --- chapter 3: and it finishes without the disc ever coming back -------
    stop["at"] = "never"
    m.retry(wj.id)
    await _settle()

    assert wj.state is JobState.DONE
    assert ripped == [0, 1]                # nothing re-read on the way to done
    assert handed[2] == {
        0: {"ripped_path": f"job_{wj.id}/rip/t000/title.mkv"},
        1: {"ripped_path": f"job_{wj.id}/rip/t001/title.mkv"}}

    await m.shutdown()   # cancel the auto-retry timer the encode failure armed
