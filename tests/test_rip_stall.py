"""A job showing "ripping" when nothing is ripping must become recoverable.

The watchdog measured elapsed time since the last rip event, but that timestamp
is runtime-only and set inside _run_job. A job that reached a rip state without
it -- task died early, or restored mid-rip -- had no baseline, and "no data" was
read as "healthy", so it sat on RIPPING forever: never flagged, so never offered
the Requeue that would free the drive.

The scan had the same hole one step earlier (job 391). _scan_job never set a
state, so a job awaiting makemkvcon stayed at QUEUED with no pipeline job, and
_live_state reported "queued" for the whole scan. QUEUED is not a watched state,
so a scan wedged on drive enumeration -- one drive dropping off the bus blocks
the scan of every other -- was never flagged, and Requeue refused it anyway for
having no scan state: the very thing it was stuck trying to produce.
"""

import asyncio
import time

from blurayripper.config import load_settings
from blurayripper.persistence import MemoryStore
from blurayripper.pipeline import JobState
from blurayripper.web.manager import JobManager, WebJob


def _mgr(tmp_path, monkeypatch, window=900):
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path)
    settings.rip_stall_after_seconds = window
    return JobManager(settings, store=MemoryStore())


def _ripping(m, **attrs):
    wj = WebJob(id=315, source="disc:0", state=JobState.RIPPING)
    for k, v in attrs.items():
        setattr(wj, k, v)
    m.jobs[315] = wj
    return wj


class _LiveTask:
    def done(self): return False


class _DeadTask:
    def done(self): return True


def test_a_rip_with_no_baseline_and_no_task_is_eventually_flagged(tmp_path, monkeypatch):
    """The reported case. It must not require a baseline that will never arrive."""
    m = _mgr(tmp_path, monkeypatch)
    wj = _ripping(m)                      # no _activity_at, no _task

    # First look only stamps a baseline — it cannot know yet how long it is.
    assert m._is_stalled(wj, JobState.RIPPING) is False
    assert wj._activity_at is not None

    wj._activity_at -= m._RIP_TASK_GRACE + 1
    assert m._is_stalled(wj, JobState.RIPPING) is True


def test_a_live_rip_task_is_never_called_stalled(tmp_path, monkeypatch):
    """A long rip with a working task is normal, not a fault."""
    m = _mgr(tmp_path, monkeypatch)
    wj = _ripping(m, _task=_LiveTask(), _activity_at=time.monotonic() - 60)
    assert m._is_stalled(wj, JobState.RIPPING) is False


def test_a_wedged_makemkv_still_stalls_on_the_no_progress_window(tmp_path, monkeypatch):
    """The original case this watchdog existed for: the task IS alive, awaiting a
    makemkvcon that has stopped saying anything."""
    m = _mgr(tmp_path, monkeypatch, window=900)
    wj = _ripping(m, _task=_LiveTask(), _activity_at=time.monotonic() - 901)
    assert m._is_stalled(wj, JobState.RIPPING) is True


def test_a_dead_task_is_not_flagged_during_the_startup_grace(tmp_path, monkeypatch):
    """_start_job marks the state before attaching the task, so a momentarily
    absent task must not read as a stall."""
    m = _mgr(tmp_path, monkeypatch)
    wj = _ripping(m, _task=_DeadTask(), _activity_at=time.monotonic() - 5)
    assert m._is_stalled(wj, JobState.RIPPING) is False


def test_a_dead_task_is_flagged_once_past_the_grace(tmp_path, monkeypatch):
    m = _mgr(tmp_path, monkeypatch)
    wj = _ripping(m, _task=_DeadTask(),
                  _activity_at=time.monotonic() - (JobManager._RIP_TASK_GRACE + 1))
    assert m._is_stalled(wj, JobState.RIPPING) is True


def test_the_watchdog_can_still_be_turned_off(tmp_path, monkeypatch):
    m = _mgr(tmp_path, monkeypatch, window=0)
    wj = _ripping(m, _activity_at=time.monotonic() - 100000)
    assert m._is_stalled(wj, JobState.RIPPING) is False


def test_encoding_is_neither_a_rip_lane_nor_a_drive_held_state(tmp_path, monkeypatch):
    """Why marking a local encode as ENCODING matters — RIPPING keeps a job in
    the rip queue AND pins its drive, so a job that kept RIPPING through an
    encode held an already-ejected drive for hours.

    This checks the mapping only. That the local-encode path actually sets
    ENCODING is covered in test_wiring.py, which is the part that was missing."""
    m = _mgr(tmp_path, monkeypatch)
    wj = _ripping(m)
    assert m._lane(wj, JobState.RIPPING) == "rip"
    assert JobState.RIPPING in m._DRIVE_HELD_STATES     # why it pinned the drive
    assert m._lane(wj, JobState.ENCODING) == "encode"
    assert JobState.ENCODING not in m._DRIVE_HELD_STATES


# --- the scan phase ---------------------------------------------------------

def _wedged_scan(m, monkeypatch):
    """A scan_disc that, like 391's makemkvcon, says hello and then nothing."""
    calls = []
    release = asyncio.Event()

    async def scan(source, report):
        calls.append(source)
        report.on_event(f"Scanning {source} ...")
        await release.wait()
        raise RuntimeError("released")

    monkeypatch.setattr(m.pipeline, "scan_disc", scan)
    return calls


async def _settle():
    for _ in range(20):
        await asyncio.sleep(0)


async def test_a_scan_in_flight_reads_as_scanning_not_queued(tmp_path, monkeypatch):
    """The reported case. Everything downstream -- the stall flag, the Requeue
    button, the drive shown as held -- keys off this one answer."""
    m = _mgr(tmp_path, monkeypatch)
    _wedged_scan(m, monkeypatch)
    wj = m.add_disc("disc:0")
    await _settle()

    assert wj._pipeline_job is None           # still no scan result, as in 391
    assert m._live_state(wj) is JobState.SCANNING
    wj._task.cancel()


async def test_a_scan_that_goes_quiet_is_flagged(tmp_path, monkeypatch):
    m = _mgr(tmp_path, monkeypatch, window=900)
    _wedged_scan(m, monkeypatch)
    wj = m.add_disc("disc:0")
    await _settle()

    assert m._is_stalled(wj, m._live_state(wj)) is False   # just started
    wj._activity_at -= 901
    assert m._is_stalled(wj, m._live_state(wj)) is True
    wj._task.cancel()


def test_a_talking_scan_is_not_stalled_for_having_no_progress_bar(tmp_path, monkeypatch):
    """A scan never calls on_progress, so its _progress_at never moves. Judging it
    by movement would flag every scan longer than the window, however busy."""
    m = _mgr(tmp_path, monkeypatch, window=900)
    now = time.monotonic()
    wj = _ripping(m, _task=_LiveTask(), _activity_at=now - 5, _progress_at=now - 901)

    assert m._is_stalled(wj, JobState.SCANNING) is False
    assert m._is_stalled(wj, JobState.RIPPING) is True     # a rip still is


def test_a_stuck_scan_does_not_claim_the_rip_stopped(tmp_path, monkeypatch):
    """Nothing has been ripped yet, and the drive at fault may be a different one.
    The rip_unfinished text says neither."""
    m = _mgr(tmp_path, monkeypatch)
    wj = _ripping(m)
    kind, text = m._stall_info(wj, JobState.SCANNING, stalled=True)
    assert kind == "scan_stuck"
    assert "scans again" in text


async def test_a_stuck_scan_can_be_requeued_and_scans_again(tmp_path, monkeypatch):
    m = _mgr(tmp_path, monkeypatch, window=900)
    calls = _wedged_scan(m, monkeypatch)
    wj = m.add_disc("disc:0")
    await _settle()
    first = wj._task
    wj._activity_at -= 901

    m.requeue(wj.id)                          # used to raise "no scan state"
    await _settle()

    assert first.cancelled()                  # the wedged makemkvcon is let go
    assert calls == ["disc:0", "disc:0"]
    assert m._live_state(wj) is JobState.SCANNING
    assert "scanning the disc again" in "".join(wj.log)
    wj._task.cancel()


async def test_requeue_all_stalled_includes_a_stuck_scan(tmp_path, monkeypatch):
    m = _mgr(tmp_path, monkeypatch, window=900)
    _wedged_scan(m, monkeypatch)
    wj = m.add_disc("disc:0")
    await _settle()
    wj._activity_at -= 901

    assert m.requeue_stalled() == 1
    await _settle()
    wj._task.cancel()


async def test_a_job_interrupted_mid_scan_scans_again_on_recovery(tmp_path, monkeypatch):
    """Now that a scan persists SCANNING, recover() resumes it -- and resuming
    must mean the scan. _run_job on a job with no disc would try to rip it."""
    m = _mgr(tmp_path, monkeypatch)
    calls = _wedged_scan(m, monkeypatch)
    ran = []

    async def run_job(wj):
        ran.append(wj.id)

    monkeypatch.setattr(m, "_run_job", run_job)
    wj = WebJob(id=391, source="disc:0", state=JobState.SCANNING)
    m.jobs[391] = wj

    m._resume(wj)
    await _settle()

    assert calls == ["disc:0"] and ran == []
    wj._task.cancel()
