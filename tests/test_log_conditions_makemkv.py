"""Every condition makemkvcon's own output describes, and what we do about it.

MakeMKV is the only witness to what happened inside the drive. It reports almost
everything through the same MSG channel at the same severity — a fatal write
failure, a title dropped for being too short, a cosmetic AV-sync note, and its
own startup banner all arrive looking identical — and it exits 0 for most of
them. So the app's whole diagnosis rests on reading those lines correctly:

* believing a failure it states plainly ("0 titles saved, 1 failed") even though
  a partial file is sitting on disk and the exit code was fine;
* NOT inventing a failure out of a line that merely mentions one ("Loaded
  content hash table", "10 titles saved", "Title #00100.mpls was skipped");
* and separating the three remedies that are mutually exclusive in practice —
  reset the drive, clean the disc, or check the share — because doing the wrong
  one is an evening.

Every fixture below is verbatim production output from the cluster (see
real_logs.txt); several of these lines caused real incidents.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, Stream, StreamType, Title
from blurayripper.persistence import MemoryStore
from blurayripper.pipeline import Job, JobState, Pipeline
from blurayripper.tools import process
from blurayripper.tools.makemkv import Drive, MakeMKV
from blurayripper.web.manager import JobManager, WebJob

# --------------------------------------------------------------------------
# verbatim production lines (real_logs.txt)
# --------------------------------------------------------------------------

JRE_WARNING = (
    "This disc requires Java runtime (JRE), but none was found. Certain functions "
    "will fail, please install Java. See http://www.makemkv.com/bdjava/ for details."
)
MINLENGTH_SKIP = (
    "Title #00107.mpls has length of 17 seconds which is less than minimum title "
    "length of 120 seconds and was therefore skipped"
)
# job 317: the drive fell off the bus mid-read.
POSIX_NO_DEVICE = (
    "Error 'Posix error - No such device' occurred while reading "
    "'/BDMV/STREAM/00001.m2ts' at offset '0'")
TITLE_LOST = "Title #00100.mpls was skipped"
FAILED_TO_OPEN = "Failed to open disc"
# jobs 334/335: the NAS write died mid-rip and the job still queued as ready.
SLOW_WRITE_NOTE = (
    "Program reads data faster than it can write to disk, consider upgrading your "
    "hard drive if you see many of these messages.")
WRITE_EAGAIN = (
    "Error 'Posix error - Resource temporarily unavailable' occurred while writing "
    "data to '/mnt/TrueNas/.../job_334/rip/t000/X.mkv'")
# a healthy run, banner and all
HEALTHY_RUN = [
    "MakeMKV v1.18.3 linux(x64-release) started",
    "MakeMKV v1.18.3 linux(x64-release) started",
    "Using LibreDrive mode (v06.3 id=E064BA3E3F81)",
    "Using direct disc access mode",
    "Loaded content hash table, will verify integrity of M2TS files.",
    "File 00260.mpls was added as title #0",
    MINLENGTH_SKIP,
    "Operation successfully completed",
    "Saving 1 titles into directory file:///mnt/TrueNas/.../rip/t000",
    "AV synchronization issues were found in file "
    "'file:///mnt/.../X_t00.mkv' (title #1)",
    "AV sync issue in stream 5 at 0:00:00 : Forced subtitles track #5 turned out "
    "to be empty and was removed from output file",
    "1 titles saved",
    "Copy complete. 1 titles saved.",
]
DRV_BLOCK = [
    'DRV:0,2,999,12,"BD-RE ASUS BW-16D1HT 3.01 KLUL75I4959",'
    '"The Nightmare Before Christmas","/dev/sr0"',
    'DRV:1,2,999,12,"BD-RE ASUS BW-16D1HT 3.10 KX1P71E4206","","/dev/sr1"',
]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _msg(text: str, code: int = 5021) -> str:
    """One makemkvcon robot-mode MSG line carrying ``text``.

    Real format: MSG:code,flags,count,message,format,param0... — the human text
    is field 3, and it is quoted because it routinely contains commas.
    """
    quoted = '"' + text.replace('"', '""') + '"'
    return f"MSG:{code},0,1,{quoted},{quoted}"


def _fake_run(monkeypatch, stdout_lines, *, capture=None, on_cmd=None):
    """Stand in for process.run: stream ``stdout_lines`` to the caller's handler.

    Patched on the process module itself, which is what every tool wrapper
    imported — no subprocess is ever spawned. The signature mirrors the real
    ``process.run`` exactly, so a keyword the app starts passing shows up here as
    a TypeError rather than being silently accepted.
    """
    async def run(cmd, *, on_line=None, cwd=None, check=True, low_priority=False,
                  timeout=None):
        if capture is not None:
            capture.append(list(cmd))
        lines = on_cmd(cmd) if on_cmd is not None else list(stdout_lines)
        for line in lines:
            if on_line is not None:
                res = on_line(line)
                if res is not None and hasattr(res, "__await__"):
                    await res
        return process.ProcessResult(returncode=0, stdout_lines=list(lines), stderr="")

    monkeypatch.setattr(process, "run", run)
    return run


def _done(value):
    """A coroutine already holding ``value`` (for one-line async monkeypatches)."""
    async def _c():
        return value
    return _c()


def _settings(tmp_path, monkeypatch):
    """Settings with every value these tests read pinned to the tracked default.

    ``load_settings()`` layers config/settings.local.json (gitignored) and
    BLURAYRIPPER_* env vars over the checked-in TOML, so a developer whose box
    keeps subtitles in another language, or who has turned the rip-duration check
    off, would otherwise get different answers from the same test.
    """
    s = load_settings()
    monkeypatch.setattr(s.storage, "output_dir", tmp_path / "out")
    monkeypatch.setattr(s.storage, "staging_dir", tmp_path / "stage")
    monkeypatch.setattr(s.tools, "makemkvcon", "/opt/makemkv/bin/makemkvcon")
    monkeypatch.setattr(s.tools, "mkvmerge", "/usr/bin/mkvmerge")
    monkeypatch.setattr(s.tools, "mkvextract", "/usr/bin/mkvextract")
    monkeypatch.setattr(s, "auto_eject", False)
    monkeypatch.setattr(s, "database_url", None)
    monkeypatch.setattr(s, "notify_url", None)
    monkeypatch.setattr(s, "min_length_seconds", 120)
    monkeypatch.setattr(s, "rip_duration_tolerance_pct", 3)  # the truncation guard
    monkeypatch.setattr(s.languages, "keep", ["eng", "pol"])
    monkeypatch.setattr(s.languages, "default", "eng")
    return s


class _Rep:
    """The reporter the pipeline talks to (the web one appends to the job log)."""

    def __init__(self):
        self.events: list[str] = []
        self.notifications: list[tuple[str, str]] = []
        self.ejected_devices: list = []
        self.checkpoints = 0

    def on_event(self, message): self.events.append(message)
    def on_progress(self, *a, **k): pass
    def checkpoint(self): self.checkpoints += 1
    def notify(self, title, message): self.notifications.append((title, message))
    def ejected(self, device): self.ejected_devices.append(device)

    @property
    def text(self) -> str:
        return "\n".join(self.events)


def _manager(tmp_path, monkeypatch, *, retries: int = 2):
    s = _settings(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "auto_retry_max", retries)
    m = JobManager(s, store=MemoryStore())
    # A disc IS in drive 0 in every scenario below, so nothing here may ever be
    # explained away by "the disc isn't in the drive".
    m._drives_cache = [Drive(index=0, name="ASUS BW-16D1HT", device="/dev/sr0",
                             disc_name="THE_NIGHTMARE_BEFORE_CHRISTMAS")]
    m._drives_scanned = True
    return m


def _pushes(manager, monkeypatch) -> list[tuple[str, str]]:
    monkeypatch.setattr(manager.settings, "notify_url",
                        "http://ntfy.example/blurayripper")
    sent: list[tuple[str, str]] = []

    async def fake_notify(url, title, message, priority=None, click=None):
        sent.append((title, message))
        return True

    monkeypatch.setattr("blurayripper.web.manager.notify", fake_notify)
    return sent


def _failed_job(manager, log, error, job_id: int = 317) -> WebJob:
    """A job that has just failed, with ``log`` being THIS run's MakeMKV output."""
    wj = WebJob(id=job_id, source="disc:0", state=JobState.FAILED, error=error)
    wj.log = list(log)
    wj._log_mark = 0
    manager.jobs[job_id] = wj
    return wj


def _rip_job(source: str = "disc:0", *, duration: int = 6960) -> Job:
    job = Job(source=source)
    job.disc = DiscInfo(source=source, disc_name="The Nightmare Before Christmas",
                        drive_device="/dev/sr0",
                        titles=[Title(title_id=0, duration_seconds=duration,
                                      size_bytes=24_000_000_000,
                                      source_filename="00260.mpls")])
    job.selected_title_ids = [0]
    return job


# ==========================================================================
# 1. the BD-Java disc: rips a SUBSET and still exits fine
# ==========================================================================

async def test_the_bd_java_warning_is_forwarded_even_though_makemkv_exits_clean(
        tmp_path, monkeypatch):
    """A disc that needs a JRE on a box without one does not fail. makemkvcon
    prints one line partway down a long log, rips whatever it could reach, and
    exits 0 — so the disc looks done and is quietly short.

    That single MSG line is the only warning there will ever be, so the scan has
    to hand it to the operator. Nothing else in the run says anything is wrong.
    """
    s = _settings(tmp_path, monkeypatch)
    _fake_run(monkeypatch, [
        "MakeMKV v1.18.3 linux(x64-release) started",     # not an MSG line
        _msg(JRE_WARNING),
        'CINFO:2,0,"Nightmare Before Christmas"',
        'TINFO:0,9,0,"1:16:00"',
        'TINFO:0,16,0,"00260.mpls"',
        _msg("Operation successfully completed"),
    ])

    seen: list[str] = []
    disc = await MakeMKV(s).scan("disc:0", on_message=seen.append)

    # The operator gets MakeMKV's own words — the message field, not the code,
    # and not the raw robot line.
    assert seen == [JRE_WARNING, "Operation successfully completed"]
    assert "install Java" in seen[0]
    # ...and the run still "succeeded": a named disc with the subset it managed.
    assert disc.disc_name == "Nightmare Before Christmas"
    assert [t.title_id for t in disc.titles] == [0]


async def test_a_bd_java_rip_that_came_back_short_is_refused_and_no_disc_is_announced_done(
        tmp_path, monkeypatch):
    """The other half of the BD-Java failure: MakeMKV reaches only part of the
    playlist, writes it, says "Operation successfully completed / 1 titles
    saved", and exits 0. Every success signal it has is lit.

    The disc's own scan is the contradiction — 1:56:00 on the disc, 20 minutes on
    disk — so the rip is refused. What matters after that is what does NOT
    happen: no checkpoint (a Retry re-rips rather than encoding the stub), no
    eject, and no "finished ripping, safe to swap the disc" push telling the
    operator to take a disc out that still needs reading.

    The full-length run at the end is the control: identical output from
    MakeMKV, and every one of those things fires. The only difference is how
    much of the film came back.
    """
    s = _settings(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "auto_eject", True)   # would fire if the rip "passed"
    pipe = Pipeline(s)

    ejected: list = []
    monkeypatch.setattr("blurayripper.pipeline.eject",
                        lambda device=None, **k: ejected.append(device))

    def rip_of(seconds: int):
        async def rip_title(source, title_id, dest_dir, *, on_progress=None,
                            on_message=None):
            dest_dir.mkdir(parents=True, exist_ok=True)
            out = dest_dir / "Nightmare_t00.mkv"
            out.write_bytes(b"the bit before the BD-Java menu")
            on_message(JRE_WARNING)
            on_message("Operation successfully completed")
            on_message("1 titles saved")
            return [out]

        class _Probe:
            duration_seconds = seconds

        monkeypatch.setattr(pipe.makemkv, "rip_title", rip_title)
        monkeypatch.setattr(pipe.mkvtools, "probe", lambda p: _done(_Probe()))

    job = _rip_job(duration=6960)                # 1:56:00 per the disc scan
    rip_of(1200)                                 # 20 minutes of a 116 minute film
    report = _Rep()
    with pytest.raises(RuntimeError) as err:
        await pipe._ensure_ripped(job, 0, tmp_path / "stage" / "job_317", report)

    assert "looks truncated" in str(err.value)
    assert "1200s" in str(err.value) and "6960s" in str(err.value)
    assert "ripped_path" not in job.checkpoints.get(0, {})  # nothing to encode
    assert ejected == []                                    # disc stays in the drive
    assert report.notifications == []                       # nobody told it was done
    assert "safe to load the next one" not in report.text
    assert JRE_WARNING in report.events                     # the reason is on record

    # --- control: the same disc, all of it -----------------------------------
    whole = _rip_job(duration=6960)
    rip_of(6960)
    ok = _Rep()
    ripped = await pipe._ensure_ripped(whole, 0, tmp_path / "stage" / "job_318", ok)

    assert ripped.name == "Nightmare_t00.mkv"
    assert whole.checkpoints[0]["ripped_path"].endswith("Nightmare_t00.mkv")
    assert ejected == ["/dev/sr0"]                          # tray opens only now
    assert any("safe to swap in the next one" in msg
               for _t, msg in ok.notifications)


# ==========================================================================
# 2. titles skipped for being under the minimum length
# ==========================================================================

async def test_the_short_title_cut_off_is_the_configured_one_and_the_skip_is_explained(
        tmp_path, monkeypatch):
    """"...less than minimum title length of 120 seconds and was therefore
    skipped" is not MakeMKV having an opinion — 120 is OUR setting, passed in on
    the command line. Raising it hides real titles, so the number in that message
    has to be the number the operator configured, and the line itself has to
    reach them or a missing episode has no explanation at all.
    """
    s = _settings(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "min_length_seconds", 300)
    cmds: list[list[str]] = []
    _fake_run(monkeypatch, [
        _msg(MINLENGTH_SKIP),
        'TINFO:0,9,0,"1:16:00"',
    ], capture=cmds)

    seen: list[str] = []
    disc = await MakeMKV(s).scan("disc:0", on_message=seen.append)

    # Exactly one --minlength, carrying the configured value: makemkvcon takes the
    # LAST one it is given, so a stray second flag would silently win.
    assert [a for a in cmds[0] if a.startswith("--minlength")] == ["--minlength=300"]
    assert seen == [MINLENGTH_SKIP]
    # The skipped title left no placeholder: only what TINFO described is offered.
    assert [t.title_id for t in disc.titles] == [0]


async def test_the_rip_uses_the_same_cut_off_as_the_scan_so_title_numbers_still_line_up(
        tmp_path, monkeypatch):
    """makemkvcon numbers titles AFTER applying --minlength, so the cut-off is
    part of the identity of "title 3". Scanning with one value and ripping with
    another renumbers the disc between the two calls, and the title the operator
    picked in the UI silently becomes a different episode.
    """
    s = _settings(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "min_length_seconds", 420)
    cmds: list[list[str]] = []
    _fake_run(monkeypatch, [_msg("Operation successfully completed")], capture=cmds)

    mk = MakeMKV(s)
    await mk.scan("disc:0")
    await mk.rip_title("disc:0", 3, tmp_path / "rip")

    scan_cmd, rip_cmd = cmds
    minlength = [a for a in scan_cmd if a.startswith("--minlength=")]
    assert minlength == ["--minlength=420"]
    assert [a for a in rip_cmd if a.startswith("--minlength=")] == minlength
    assert "mkv" in rip_cmd and "3" in rip_cmd     # ...and it really was the rip


# ==========================================================================
# 3. the drive that dropped off the bus (job 317)
# ==========================================================================

async def test_the_job_317_block_blames_the_drive_and_not_a_missing_volume_key(
        tmp_path, monkeypatch):
    """Job 317, verbatim. Three lines arrive together and they point at two
    different remedies: "Failed to open disc" is also exactly what a disc with an
    unknown AACS volume key prints (submit the dump, never touch the hardware),
    while "Posix error - No such device" means the drive itself is gone
    (power-cycle it; the disc is irrelevant).

    Only one of them can be shown, and the device error is the one that is never
    ambiguous — so it has to win. Being sent to email a key dump for a drive that
    has fallen off the bus is a whole evening.
    """
    m = _manager(tmp_path, monkeypatch)
    sent = _pushes(m, monkeypatch)
    wj = _failed_job(m, [POSIX_NO_DEVICE, TITLE_LOST, FAILED_TO_OPEN],
                     error="ProcessError: Command failed (11): makemkvcon")

    hint, terminal = m._rip_failure_hint(wj)
    assert "dropped off the bus" in hint and "power/connection" in hint
    assert terminal is True
    assert "volume key" not in hint and "AACS" not in hint   # the other remedy
    assert "Clean the disc" not in hint                      # and the third one

    m._schedule_auto_retry(wj)
    await asyncio.sleep(0)

    # Terminal: no retry burned, none scheduled — re-reading a drive that isn't
    # there twenty times overnight only delays the five-minute fix.
    assert m._auto_retries.get(wj.id) is None
    assert not [t for t in m._tasks if not t.done()]
    assert "Auto-retry" not in "\n".join(wj.log)
    # ...and the phone gets the remedy, not the stack trace.
    assert len(sent) == 1
    title, message = sent[0]
    assert "needs attention" in title
    assert f"Job #{wj.id}" in message and "dropped off the bus" in message


async def test_a_lost_title_line_on_its_own_diagnoses_nothing(tmp_path, monkeypatch):
    """"Title #00100.mpls was skipped" is collateral, not a cause — and it is the
    same verb MakeMKV uses for the harmless under-minlength skip. Reading it as a
    fault would park healthy jobs with a remedy nobody can act on, and would fire
    on every TV disc that has a 17-second logo on it.
    """
    m = _manager(tmp_path, monkeypatch)
    wj = _failed_job(m, [TITLE_LOST], error="ProcessError: HandBrakeCLI exited with 3")

    assert m._rip_failure_hint(wj) is None

    # Control, because "returns None" would also hold if the run's log were never
    # read at all: swap that one line for one that does mean something, leave
    # everything else identical, and the same job is diagnosed.
    wj.log = [POSIX_NO_DEVICE]
    hint, _terminal = m._rip_failure_hint(wj)
    assert "dropped off the bus" in hint


# ==========================================================================
# 4. the share that went away mid-write (jobs 334/335)
# ==========================================================================

async def test_a_write_failure_on_the_share_is_retried_and_never_blamed_on_the_disc(
        tmp_path, monkeypatch):
    """Jobs 334/335: the NAS write died mid-rip. MakeMKV said so plainly, and
    both jobs still reached the encode queue as ready.

    Two things have to be true at once. The rip must fail (it did not produce a
    usable file, whatever the exit code) — and the failure must NOT be diagnosed
    as a bad disc or a dead drive, because it is neither: a CIFS share blinking
    is exactly the transient a retry fixes. Parking it with "clean the disc"
    would leave a perfectly good disc sitting in the drive until someone noticed.
    """
    s = _settings(tmp_path, monkeypatch)
    pipe = Pipeline(s)
    job = _rip_job()

    async def rip_title(source, title_id, dest_dir, *, on_progress=None, on_message=None):
        dest_dir.mkdir(parents=True, exist_ok=True)
        partial = dest_dir / "Nightmare_t00.mkv"
        partial.write_bytes(b"as much as it got before the share went")
        on_message(SLOW_WRITE_NOTE)
        on_message(WRITE_EAGAIN)
        on_message(f"Failed to save title 0 to file {partial}")
        on_message("0 titles saved, 1 failed")
        on_message("Copy complete. 0 titles saved, 1 failed.")
        return [partial]          # progressive writes -> not "no output"

    class _Probe:
        duration_seconds = 6960          # the partial would even probe "fine"

    monkeypatch.setattr(pipe.makemkv, "rip_title", rip_title)
    monkeypatch.setattr(pipe.mkvtools, "probe", lambda p: _done(_Probe()))

    report = _Rep()
    with pytest.raises(RuntimeError) as err:
        await pipe._ensure_ripped(job, 0, tmp_path / "stage" / "job_334", report)

    # The message the app produced quotes MakeMKV's own failure line — not the
    # "0 titles saved" summary that came after it — and names the real suspect.
    failure = str(err.value)
    assert "could not save title 0" in failure
    assert "Failed to save title 0 to file" in failure
    assert "staging disk or share went away mid-rip" in failure
    assert "rather than a bad disc" in failure
    assert "ripped_path" not in job.checkpoints.get(0, {})

    # Now hand that exact failure to the diagnosis the operator actually sees.
    # The write errors really are in the log being diagnosed, so the "not blamed
    # on the disc" assertions below are about a realistic log, not an empty one.
    assert WRITE_EAGAIN in report.events and SLOW_WRITE_NOTE in report.events
    m = _manager(tmp_path, monkeypatch)
    _pushes(m, monkeypatch)
    wj = _failed_job(m, report.events, error=f"RuntimeError: {failure}", job_id=334)

    assert m._rip_failure_hint(wj) is None       # not the disc, not the drive
    m._schedule_auto_retry(wj)
    await m.shutdown()      # cancel the pending retry before asserting, so a
                            # failing assertion can't leave the task dangling
    log = "\n".join(wj.log)
    assert "Disc read error" not in log and "Clean the disc" not in log
    assert "dropped off the bus" not in log
    assert m._auto_retries[wj.id] == 1           # it gets its second chance
    assert "Auto-retry 1/2 in 30s" in log


async def test_ten_titles_saved_is_not_read_as_zero_titles_saved(tmp_path, monkeypatch):
    """The failure signature is the literal "0 titles saved". A box-set disc ends
    with "Copy complete. 10 titles saved." — which contains those exact
    characters. Matching loosely fails every successful multi-episode rip on the
    shelf, and the operator is told the NAS died on a rip that went perfectly.

    Both halves go through the real rip path, and the summary line is the ONLY
    thing that differs between them: the box-set rip is kept, and the same rip
    ending in "0 titles saved, 1 failed" is refused — that summary is sometimes
    all MakeMKV prints, with no "Failed to save title" line to fall back on.

    The slow-write note rides along in both: it is advice about hard drives,
    printed on healthy rips, and means nothing has failed.
    """
    s = _settings(tmp_path, monkeypatch)
    pipe = Pipeline(s)

    def rip_ending_in(summary: str):
        async def rip_title(source, title_id, dest_dir, *, on_progress=None,
                            on_message=None):
            dest_dir.mkdir(parents=True, exist_ok=True)
            out = dest_dir / "Boxset_t00.mkv"
            out.write_bytes(b"a complete rip")
            on_message(SLOW_WRITE_NOTE)
            on_message(summary)
            return [out]

        monkeypatch.setattr(pipe.makemkv, "rip_title", rip_title)

    class _Probe:
        duration_seconds = 1800

    monkeypatch.setattr(pipe.mkvtools, "probe", lambda p: _done(_Probe()))

    job = _rip_job(duration=1800)
    rip_ending_in("Copy complete. 10 titles saved.")
    report = _Rep()
    ripped = await pipe._ensure_ripped(job, 0, tmp_path / "stage" / "job_9", report)

    assert ripped.name == "Boxset_t00.mkv"
    assert job.checkpoints[0]["ripped_path"].endswith("Boxset_t00.mkv")
    assert SLOW_WRITE_NOTE in report.events       # forwarded to the operator...
    assert "could not save" not in report.text    # ...but not treated as a failure

    # --- the real thing: the same log, one digit different --------------------
    dead = _rip_job(duration=1800)
    rip_ending_in("Copy complete. 0 titles saved, 1 failed.")
    with pytest.raises(RuntimeError) as err:
        await pipe._ensure_ripped(dead, 0, tmp_path / "stage" / "job_10", _Rep())

    assert "could not save title 0" in str(err.value)
    assert "ripped_path" not in dead.checkpoints.get(0, {})


# ==========================================================================
# 5. the disc the drive cannot read
# ==========================================================================

@pytest.mark.parametrize("makemkv_said", [
    "SCSI error - MEDIUM ERROR:ID CRC OR ECC ERROR occurred while reading 00001.m2ts",
    "Hash check failed for file 00260.m2ts - the disc may be damaged",
    "Error while getting fetchframes for title 0",
])
async def test_the_read_error_family_parks_the_job_with_the_clean_the_disc_remedy(
        makemkv_said, tmp_path, monkeypatch):
    """Three different ways the drive says "I could not read this platter": a
    SCSI medium error, a content-hash mismatch against the table MakeMKV loaded
    at the start of the run, and a frame fetch that came back empty.

    All three are the disc (or the lens), none of them are fixed by retrying, and
    none of them are fixed by power-cycling a healthy drive. So each parks the
    job with the one remedy that helps and rings the phone once.
    """
    m = _manager(tmp_path, monkeypatch)
    sent = _pushes(m, monkeypatch)
    wj = _failed_job(m, ["Using LibreDrive mode (v06.3 id=E064BA3E3F81)", makemkv_said],
                     error="ProcessError: Command failed (12): makemkvcon")

    hint, terminal = m._rip_failure_hint(wj)
    assert "Disc read error" in hint and "Clean the disc" in hint
    assert terminal is True
    assert "dropped off the bus" not in hint      # the drive is answering fine

    m._schedule_auto_retry(wj)
    await asyncio.sleep(0)

    assert m._auto_retries.get(wj.id) is None
    assert not [t for t in m._tasks if not t.done()]
    assert "Auto-retry" not in "\n".join(wj.log)
    assert len(sent) == 1 and "Disc read error" in sent[0][1]


# ==========================================================================
# 6. the healthy run — banner, mode lines, and cosmetic notices
# ==========================================================================

async def test_a_healthy_runs_banner_and_notices_never_relabel_a_later_failure(
        tmp_path, monkeypatch):
    """A completely successful rip prints thirteen lines, and half of them read
    like trouble to a careless matcher: it loads a "content hash table", it
    announces its access mode, it skips a title, it reports AV sync ISSUES, and
    it removes a subtitle track from the output.

    When the encode later dies for an unrelated reason, those lines are still
    sitting in the same job log. Diagnosing off them turns an ordinary crash into
    a hardware fault: the operator is sent to clean a disc that read perfectly,
    and — because a terminal remedy also cancels the retry budget — the retry
    that would have just worked is never attempted.
    """
    m = _manager(tmp_path, monkeypatch)
    sent = _pushes(m, monkeypatch)
    wj = _failed_job(m, HEALTHY_RUN + ["Encoding title 0 (2160p) -> Nightmare.mkv ..."],
                     error="ProcessError: Command failed (3): HandBrakeCLI")

    assert m._rip_failure_hint(wj) is None

    m._schedule_auto_retry(wj)
    await m.shutdown()      # cancel the pending retry before asserting, so a
                            # failing assertion can't leave the task dangling
    log = "\n".join(wj.log)
    assert "Disc read error" not in log
    assert "dropped off the bus" not in log
    assert "volume key" not in log
    assert m._auto_retries[wj.id] == 1
    assert "Auto-retry 1/2 in 30s" in log
    assert sent == []          # an ordinary crash does not wake anybody


async def test_a_forced_track_makemkv_dropped_does_not_become_a_phantom_sidecar(
        tmp_path, monkeypatch):
    """"Forced subtitles track #5 turned out to be empty and was removed from
    output file" — the disc advertised a forced track, and the .mkv does not have
    one. Nothing failed; there was simply nothing in it.

    So the subtitle stage has to work from what the FILE contains, not from what
    the disc scan promised. Extracting a track id that is no longer there fails
    the whole title (and with it the OCR of the tracks that DO exist), and
    trusting the scan's list would name a forced sidecar with nothing in it.
    """
    s = _settings(tmp_path, monkeypatch)
    pipe = Pipeline(s)

    job = _rip_job()
    # What the disc scan saw: a full English track AND a forced one.
    job.disc.titles[0].streams = [
        Stream(stream_id=4, stream_type=StreamType.SUBTITLE, codec="PGS", language="eng"),
        Stream(stream_id=5, stream_type=StreamType.SUBTITLE, codec="PGS", language="eng",
               name="Forced"),
    ]

    ripped = tmp_path / "rip" / "Nightmare_t00.mkv"
    ripped.parent.mkdir(parents=True)
    ripped.write_bytes(b"mkv")
    # What mkvmerge reports on the file MakeMKV actually wrote: track 5 is gone.
    identify_json = (
        '{"tracks": ['
        '{"id": 0, "type": "video", "codec": "MPEG-H/HEVC/H.265"},'
        '{"id": 1, "type": "audio", "codec": "TrueHD Atmos",'
        ' "properties": {"language": "eng"}},'
        '{"id": 4, "type": "subtitles", "codec": "HDMV PGS",'
        ' "properties": {"language": "eng"}}'
        ']}')

    extract_cmds: list[list[str]] = []

    def on_cmd(cmd):
        if "mkvextract" in cmd[0]:
            extract_cmds.append(list(cmd))
            for spec in cmd[3:]:
                Path(spec.split(":", 1)[1]).write_bytes(b"PGS")
            return ["Progress: 100%"]
        return identify_json.splitlines()

    _fake_run(monkeypatch, [], on_cmd=on_cmd)

    report = _Rep()
    sub_dir = tmp_path / "stage" / "job_317" / "sub" / "t000"
    extracted = await pipe._extract_title_subs(job, 0, ripped, sub_dir, report)

    # Exactly the one track the file has — not the two the disc advertised.
    assert [track.track_id for _sup, track in extracted] == [4]
    assert not any(t.forced for _s, t in extracted)
    assert len(extract_cmds) == 1
    assert [a for a in extract_cmds[0] if a.startswith("4:")]
    assert not [a for a in extract_cmds[0] if a.startswith("5:")]
    # ...and the title is checkpointed as done rather than failed.
    assert len(job.checkpoints[0]["subs"]) == 1
    assert "failed" not in report.text.lower()


# ==========================================================================
# 7. the enumeration makemkvcon emits twice in one invocation
# ==========================================================================

async def test_a_doubled_enumeration_still_lists_each_drive_once(tmp_path, monkeypatch):
    """makemkvcon re-enumerates after LibreDrive/driver init, so one invocation
    prints the whole DRV block twice (which is also why the startup banner
    appears twice). Both copies are the same two physical drives.

    Listing them twice put two Add buttons on the panel for one disc, each of
    which queued its own job for it. The OS-recovery pass that follows must
    dedupe against the ALREADY-deduped list too, or a drive the OS also sees
    comes back a third time as an unusable phantom.
    """
    s = _settings(tmp_path, monkeypatch)
    _fake_run(monkeypatch, [
        "MakeMKV v1.18.3 linux(x64-release) started",
        "MakeMKV v1.18.3 linux(x64-release) started",
        *DRV_BLOCK,
        *DRV_BLOCK,
    ])
    # The OS reports the same first drive (and nothing the enumeration missed).
    monkeypatch.setattr("blurayripper.tools.makemkv.os_optical_drives",
                        lambda: [("/dev/sr0", "The Nightmare Before Christmas")])
    monkeypatch.setattr("blurayripper.tools.makemkv.os_disc_label", lambda device: None)

    drives = await MakeMKV(s).list_drives()

    assert [d.index for d in drives] == [0, 1]
    assert [d.device for d in drives] == ["/dev/sr0", "/dev/sr1"]
    assert [d.disc_name for d in drives] == ["The Nightmare Before Christmas", None]
    assert not any(d.makemkv_blind for d in drives)   # no phantom third entry
