"""A scheduled maintenance window must actually leave the boxes idle.

The whole feature turns on one distinction: draining AT a deadline is not being
drained BY it. A drain tells a worker to finish its current job and exit, and an
encode here runs from a few hours to several days — so the deadline has to work
backwards, holding new claims long enough that what is running can finish.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from blurayripper import maintenance


UTC = timezone.utc
DEADLINE = datetime(2026, 8, 30, 2, 0, tzinfo=UTC)


def _win(**kw):
    kw.setdefault("drain_at", DEADLINE.isoformat())
    kw.setdefault("lead_hours", 24)
    return maintenance.from_dict(kw)


def test_no_schedule_holds_nothing():
    """The default must be an ordinary working cluster."""
    w = maintenance.from_dict(None)
    assert not w.scheduled
    assert not w.holds_new_work(datetime.now(UTC))
    assert not w.due(datetime.now(UTC))


def test_work_stops_a_lead_time_before_the_deadline_not_at_it():
    """This is the point of the feature. Claiming a job an hour before a
    deadline overruns it by however long that job takes."""
    w = _win(lead_hours=24)
    assert w.quiet_from() == DEADLINE - timedelta(hours=24)
    assert not w.holds_new_work(DEADLINE - timedelta(hours=25))
    assert w.holds_new_work(DEADLINE - timedelta(hours=23))


def test_the_deadline_itself_is_what_fires_the_drain():
    w = _win()
    assert not w.due(DEADLINE - timedelta(minutes=1))
    assert w.due(DEADLINE)
    assert w.due(DEADLINE + timedelta(hours=5))


def test_a_zero_lead_still_stops_claiming_at_the_deadline():
    """Lead 0 means "keep working right up to it" — still no NEW work after."""
    w = _win(lead_hours=0)
    assert not w.holds_new_work(DEADLINE - timedelta(seconds=1))
    assert w.holds_new_work(DEADLINE)


def test_a_naive_timestamp_is_read_as_utc_on_every_box():
    """Workers sit in whatever timezone they sit in. A deadline that meant a
    different instant per machine would be worse than no deadline at all."""
    w = maintenance.from_dict({"drain_at": "2026-08-30T02:00:00"})
    assert w.drain_at == DEADLINE


def test_an_offset_timestamp_is_normalised_rather_than_trusted():
    w = maintenance.from_dict({"drain_at": "2026-08-29T22:00:00-04:00"})
    assert w.drain_at == DEADLINE


def test_rubbish_in_the_schedule_does_not_hold_the_cluster():
    """A corrupt row must fail OPEN. Failing closed would idle every box with no
    way to tell why from the panel."""
    w = maintenance.from_dict({"drain_at": "next tuesday-ish", "lead_hours": "soon"})
    assert not w.scheduled
    assert not w.holds_new_work(datetime.now(UTC))
    assert w.lead_hours == maintenance.DEFAULT_LEAD_HOURS


def test_a_database_that_cannot_be_read_holds_nothing():
    """A blip reading the schedule must not read as "stop taking work" — that
    would let a momentary outage idle the entire cluster."""
    class _Down:
        def get_shared_config(self, key): raise OSError("database is away")

    assert not maintenance.load(_Down()).scheduled


def test_a_window_survives_the_round_trip():
    w = _win(lead_hours=6, note="swap the NAS disks")
    again = maintenance.from_dict(maintenance.to_dict(w))
    assert (again.drain_at, again.lead_hours, again.note) == (
        DEADLINE, 6.0, "swap the NAS disks")


def test_a_negative_lead_is_clamped_rather_than_moving_the_deadline_later():
    """A negative lead would put the quiet period AFTER the deadline — workers
    still claiming past the time they were meant to be idle."""
    w = _win(lead_hours=-5)
    assert w.lead_hours == 0.0
    assert w.quiet_from() == DEADLINE


# --- the worker actually refuses work ----------------------------------------

def _runner(tmp_path, window):
    """A worker whose store answers with the given maintenance window."""
    from blurayripper.config import load_settings
    from blurayripper.persistence import MemoryStore
    from blurayripper.worker import WorkerRunner

    settings = load_settings()
    settings.storage.staging_dir = tmp_path / "stage"
    settings.storage.output_dir = tmp_path / "out"
    settings.database_url = "postgresql://cluster/blurayripper"
    runner = WorkerRunner(settings, worker_id="encoder-A")
    runner.store = MemoryStore()
    if window is not None:
        maintenance.save(runner.store, window)
    return runner


def test_a_worker_refuses_new_work_inside_the_window(tmp_path, monkeypatch):
    """The hold has to sit BEFORE claim_next. A job claimed inside the window is
    precisely the job that runs past the deadline."""
    runner = _runner(tmp_path, _win(lead_hours=24))
    # Only the WORKER's clock: patching maintenance.datetime too would take
    # fromisoformat with it, and the schedule would fail to parse — which reads
    # as "no maintenance booked" and would have made this test pass for the
    # wrong reason if the assertion had been the other way round.
    import blurayripper.worker as worker_mod
    monkeypatch.setattr(
        worker_mod, "datetime",
        type("_C", (), {"now": staticmethod(lambda tz=None: DEADLINE - timedelta(hours=1))}))

    hold = runner._maintenance_hold()
    assert hold and "maintenance" in hold and "2026-08-30 02:00" in hold


def test_a_worker_keeps_working_before_the_window_opens(tmp_path, monkeypatch):
    """Scheduling maintenance a week out must not idle the cluster for a week."""
    runner = _runner(tmp_path, _win(lead_hours=24))
    import blurayripper.worker as worker_mod
    monkeypatch.setattr(
        worker_mod, "datetime",
        type("_C", (), {"now": staticmethod(lambda tz=None: DEADLINE - timedelta(days=3))}))
    assert runner._maintenance_hold() is None


def test_an_unscheduled_cluster_never_holds(tmp_path):
    assert _runner(tmp_path, None)._maintenance_hold() is None


# --- serve fires it once ------------------------------------------------------

def _manager(tmp_path):
    from blurayripper.config import load_settings
    from blurayripper.persistence import MemoryStore
    from blurayripper.web.manager import JobManager

    settings = load_settings()
    settings.storage.staging_dir = tmp_path / "stage"
    settings.storage.output_dir = tmp_path / "out"
    return JobManager(settings, store=MemoryStore())


def test_the_deadline_raises_one_drain_and_clears_the_schedule(tmp_path, monkeypatch):
    """Left standing, the window would keep holding every worker after the
    maintenance was over — boxes up, idle and refusing work, which reads as a
    broken queue rather than a schedule nobody cancelled."""
    import blurayripper.web.manager as mgr_mod

    mgr = _manager(tmp_path)
    maintenance.save(mgr.store, _win())
    monkeypatch.setattr(
        mgr_mod, "datetime",
        type("_C", (), {"now": staticmethod(lambda tz=None: DEADLINE + timedelta(minutes=1))}))

    before = mgr.store.cluster_signals().get("drain:*", 0)
    mgr._fire_due_maintenance()
    after = mgr.store.cluster_signals().get("drain:*", 0)
    assert after > before, "the deadline must raise the drain"
    assert not maintenance.load(mgr.store).scheduled, "and then clear itself"

    mgr._fire_due_maintenance()
    assert mgr.store.cluster_signals().get("drain:*", 0) == after, "only once"


def test_nothing_fires_before_the_deadline(tmp_path, monkeypatch):
    import blurayripper.web.manager as mgr_mod

    mgr = _manager(tmp_path)
    maintenance.save(mgr.store, _win())
    monkeypatch.setattr(
        mgr_mod, "datetime",
        type("_C", (), {"now": staticmethod(lambda tz=None: DEADLINE - timedelta(hours=2))}))
    mgr._fire_due_maintenance()
    assert mgr.store.cluster_signals().get("drain:*", 0) == 0
    assert maintenance.load(mgr.store).scheduled


def test_clearing_the_schedule_releases_the_cluster(tmp_path):
    mgr = _manager(tmp_path)
    maintenance.save(mgr.store, _win())
    mgr.set_maintenance(None, None)
    assert not maintenance.load(mgr.store).scheduled


def test_a_time_that_cannot_be_read_is_refused_rather_than_silently_ignored(tmp_path):
    """Silently storing "no schedule" would leave you believing maintenance is
    booked and find the cluster still working when you arrive."""
    mgr = _manager(tmp_path)
    with pytest.raises(ValueError, match="date and time"):
        mgr.set_maintenance("next tuesday", 24)


async def test_the_claim_loop_really_skips_claiming_during_the_window(tmp_path, monkeypatch):
    """Knowing the hold is correct is not the same as knowing the loop obeys it.
    The one thing that matters is that claim_next is never reached — that call is
    what starts a job which then runs past the deadline."""
    import blurayripper.worker as worker_mod

    runner = _runner(tmp_path, _win(lead_hours=24))
    monkeypatch.setattr(
        worker_mod, "datetime",
        type("_C", (), {"now": staticmethod(lambda tz=None: DEADLINE - timedelta(hours=1))}))
    runner.settings.cluster.worker_poll_seconds = 0

    claims = []
    runner.store.claim_next = lambda *a, **k: claims.append(a) or None

    stages = []
    original_register = runner._register

    def _register(job_id, stage):
        stages.append(stage)
        if len(stages) >= 2:
            runner.request_stop()          # two clean passes is enough
        original_register(job_id, stage)

    runner._register = _register
    # Skip the parts of run() that reach for real tools/processes.
    monkeypatch.setattr(runner, "_init_store", lambda: _noop())
    monkeypatch.setattr(runner, "_apply_worker_settings", lambda: None)
    monkeypatch.setattr(runner, "_maybe_periodic_restart", lambda: None)
    monkeypatch.setattr(runner, "_maybe_auto_update", lambda: None)
    monkeypatch.setattr(runner, "_check_update_signal", lambda: None)

    await runner.run()

    assert claims == [], "a job was claimed inside the maintenance window"
    assert stages and all("maintenance" in s for s in stages), stages


async def _noop():
    return None


def test_a_deadline_in_the_past_is_refused_rather_than_draining_everything(tmp_path):
    """A past deadline is due the instant it is saved, so it would drain the
    whole cluster on the next poll. Nobody booking maintenance meant that — it is
    a mistyped year — and Drain all already exists for when you do mean now."""
    mgr = _manager(tmp_path)
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match="already passed"):
        mgr.set_maintenance(past, 24)
    assert not maintenance.load(mgr.store).scheduled


def test_a_future_deadline_is_accepted(tmp_path):
    mgr = _manager(tmp_path)
    soon = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    out = mgr.set_maintenance(soon, 6)
    assert out["drain_at"] and out["lead_hours"] == 6.0
    assert maintenance.load(mgr.store).scheduled
