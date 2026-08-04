"""Server-triggered 'update all workers' — a cluster signal a worker acts on once."""

import pytest

from blurayripper.config import load_settings
from blurayripper.persistence import MemoryStore
from blurayripper.worker import WorkerRunner


class _DurableMem(MemoryStore):
    @property
    def durable(self):
        return True


def test_update_token_increments_on_request():
    store = MemoryStore()
    assert store.worker_update_token() == 0
    store.request_worker_update()
    store.request_worker_update()
    assert store.worker_update_token() == 2


def test_worker_acts_on_a_newer_signal_once():
    store = MemoryStore()
    store.request_worker_update()          # a request that predates this worker
    runner = WorkerRunner(load_settings(), worker_id="w1")
    runner.store = store
    runner._signal_tokens = store.cluster_signals()  # what run() records at startup

    # No new signal since startup -> no update.
    runner._check_update_signal()
    assert runner.update_requested is False

    # The server signals an update -> the worker requests a drain+update.
    store.request_worker_update()
    runner._check_update_signal()
    assert runner.update_requested is True and runner._stop is True

    # Already flagged -> repeat checks are a no-op (the guard returns early).
    runner._check_update_signal()
    assert runner.update_requested is True


def test_manager_request_worker_update_needs_durable_store():
    from blurayripper.web.manager import JobManager
    s = load_settings()

    mgr = JobManager(s, store=_DurableMem())
    assert mgr.request_worker_update() == 0     # ok (0 workers connected in the cache)

    mgr2 = JobManager(s, store=MemoryStore())   # MemoryStore is non-durable
    with pytest.raises(ValueError):
        mgr2.request_worker_update()


def test_shared_config_round_trips_in_store():
    store = MemoryStore()
    assert store.get_shared_config("handbrake_preset") is None
    store.set_shared_config("handbrake_preset", {"PresetList": [{"PresetName": "X"}]})
    assert store.get_shared_config("handbrake_preset")["PresetList"][0]["PresetName"] == "X"


def test_worker_syncs_shared_handbrake_preset(tmp_path, monkeypatch):
    from blurayripper import handbrake_store
    store = MemoryStore()
    store.set_shared_config(handbrake_store.SHARED_KEY,
                            {"PresetList": [{"PresetName": "Cluster", "VideoQualitySlider": 20}]})
    s = load_settings()
    monkeypatch.setattr(s, "config_dir", tmp_path)
    runner = WorkerRunner(s, worker_id="w1")
    runner.store = store

    # First sync writes this box's presets.local.json from the shared value.
    assert runner._sync_shared_preset() is True
    local = tmp_path / "presets.local.json"
    assert local.exists()
    import json
    assert json.loads(local.read_text())["PresetList"][0]["VideoQualitySlider"] == 20
    # Idempotent — unchanged shared value doesn't rewrite/report a change.
    assert runner._sync_shared_preset() is False


def _runner_at(store, worker_id="w1"):
    r = WorkerRunner(load_settings(), worker_id=worker_id)
    r.store = store
    r._signal_tokens = store.cluster_signals()   # what run() snapshots at startup
    return r


def test_drain_stops_without_restarting():
    """Drain is the new one: finish the current encode, place it, then exit and
    stay down — no re-exec, so it must NOT set the restart flags."""
    store = MemoryStore()
    r = _runner_at(store)
    store.request_cluster_signal("drain", "w1")
    r._check_update_signal()
    assert r._stop is True
    assert r.restart_requested is False and r.update_requested is False


def test_restart_reexecs_without_pulling():
    store = MemoryStore()
    r = _runner_at(store)
    store.request_cluster_signal("restart", "w1")
    r._check_update_signal()
    assert r._stop and r.restart_requested is True
    assert r.update_requested is False          # update_requested is what pulls


def test_update_pulls_and_restarts():
    store = MemoryStore()
    r = _runner_at(store)
    store.request_cluster_signal("update", "w1")
    r._check_update_signal()
    assert r._stop and r.restart_requested and r.update_requested


def test_a_signal_for_another_worker_is_ignored():
    """Per-worker targeting is the point: naming one box must not stop the others."""
    store = MemoryStore()
    mine, theirs = _runner_at(store, "w1"), _runner_at(store, "w2")
    store.request_cluster_signal("drain", "w2")
    mine._check_update_signal()
    theirs._check_update_signal()
    assert mine._stop is False
    assert theirs._stop is True


def test_broadcast_reaches_every_worker():
    store = MemoryStore()
    a, b = _runner_at(store, "w1"), _runner_at(store, "w2")
    store.request_cluster_signal("update", "*")
    a._check_update_signal(); b._check_update_signal()
    assert a.update_requested and b.update_requested


def test_the_most_disruptive_pending_action_wins():
    """Drain and update both pending: updating implies draining anyway, so taking
    the stronger one can't lose work."""
    store = MemoryStore()
    r = _runner_at(store)
    store.request_cluster_signal("drain", "*")
    store.request_cluster_signal("update", "w1")
    r._check_update_signal()
    assert r.update_requested is True


def test_manager_rejects_unknown_action_and_unknown_worker():
    from blurayripper.persistence import WorkerInfo
    from blurayripper.web.manager import JobManager

    mgr = JobManager(load_settings(), store=_DurableMem())
    mgr._workers_cache = [WorkerInfo("SteamMachine", "v1", None, "idle", 1.0)]

    assert mgr.request_worker_action("drain", "SteamMachine") == 1
    assert mgr.request_worker_action("update", "*") == 1
    with pytest.raises(ValueError, match="Unknown worker action"):
        mgr.request_worker_action("explode", "*")
    with pytest.raises(ValueError, match="No worker named"):
        mgr.request_worker_action("drain", "TYPO")   # would sit unread otherwise
