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
    runner._update_token = store.worker_update_token()  # what run() records at startup

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
