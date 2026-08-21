"""A job showing "ripping" when nothing is ripping must become recoverable.

The watchdog measured elapsed time since the last rip event, but that timestamp
is runtime-only and set inside _run_job. A job that reached a rip state without
it -- task died early, or restored mid-rip -- had no baseline, and "no data" was
read as "healthy", so it sat on RIPPING forever: never flagged, so never offered
the Requeue that would free the drive.
"""

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
