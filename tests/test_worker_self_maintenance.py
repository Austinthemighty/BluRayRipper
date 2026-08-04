"""A worker restarting itself on age, and following the rip box's version."""

import time

from blurayripper import presence
from blurayripper.config import load_settings
from blurayripper.persistence import MemoryStore, WorkerInfo
from blurayripper.version import commit_count
from blurayripper.worker import WorkerRunner


class _Store(MemoryStore):
    def __init__(self, workers=()):
        super().__init__()
        self.workers = list(workers)

    @property
    def durable(self):
        return True

    def list_workers(self):
        return list(self.workers)


def _runner(up_seconds=0.0, **cluster):
    s = load_settings()
    for k, v in cluster.items():
        setattr(s.cluster, k, v)
    r = WorkerRunner(s, worker_id="w1")
    r.store = _Store()
    r._started_monotonic = time.monotonic() - up_seconds
    return r


def _serve(version):
    return WorkerInfo(presence.serve_id("RIPBOX"), version, None, "serving", 1.0)


# --- restart after N days ------------------------------------------------

def test_restarts_once_past_the_age_limit():
    r = _runner(up_seconds=3 * 86400 + 10, worker_restart_after_days=3)
    r._maybe_periodic_restart()
    assert r._stop and r.restart_requested is True
    assert r.update_requested is False       # a plain restart, no pull


def test_does_not_restart_before_the_limit():
    r = _runner(up_seconds=2 * 86400, worker_restart_after_days=3)
    r._maybe_periodic_restart()
    assert r._stop is False


def test_zero_days_disables_the_periodic_restart():
    r = _runner(up_seconds=400 * 86400, worker_restart_after_days=0)
    r._maybe_periodic_restart()
    assert r._stop is False


# --- follow the rip box's version ---------------------------------------

def test_updates_when_the_rip_box_is_newer():
    r = _runner(up_seconds=3600, worker_auto_update=True)
    r._version = "0.1.0+240.aaaaaaa"
    r.store.workers = [_serve("0.1.0+250.bbbbbbb")]
    r._maybe_auto_update()
    assert r._stop and r.update_requested is True   # update pulls, unlike restart


def test_does_not_downgrade_to_an_older_server():
    """A worker ahead of the server must be left alone, not rolled back."""
    r = _runner(up_seconds=3600, worker_auto_update=True)
    r._version = "0.1.0+260.aaaaaaa"
    r.store.workers = [_serve("0.1.0+250.bbbbbbb")]
    r._maybe_auto_update()
    assert r._stop is False


def test_same_version_is_a_no_op():
    r = _runner(up_seconds=3600, worker_auto_update=True)
    r._version = "0.1.0+250.aaaaaaa"
    r.store.workers = [_serve("0.1.0+250.aaaaaaa")]
    r._maybe_auto_update()
    assert r._stop is False


def test_young_process_does_not_update_again():
    """Guards a hot loop: if a pull can't actually reach the server's build (serve
    is on an unpushed commit), an unguarded worker would drain-pull-restart
    forever and never encode anything."""
    r = _runner(up_seconds=60, worker_auto_update=True)
    r._version = "0.1.0+240.aaaaaaa"
    r.store.workers = [_serve("0.1.0+250.bbbbbbb")]
    r._maybe_auto_update()
    assert r._stop is False


def test_unparseable_version_is_never_guessed():
    """Built without git -> no commit count -> no basis to compare. Do nothing."""
    r = _runner(up_seconds=3600, worker_auto_update=True)
    r._version = "0.1.0+gabc1234"
    r.store.workers = [_serve("0.1.0+250.bbbbbbb")]
    r._maybe_auto_update()
    assert r._stop is False
    assert commit_count("0.1.0+gabc1234") is None

    r2 = _runner(up_seconds=3600, worker_auto_update=True)
    r2._version = "0.1.0+240.aaaaaaa"
    r2.store.workers = [_serve("0.1.0")]          # server's is unparseable
    r2._maybe_auto_update()
    assert r2._stop is False


def test_disabled_by_default():
    r = _runner(up_seconds=3600)                  # worker_auto_update defaults off
    r._version = "0.1.0+240.aaaaaaa"
    r.store.workers = [_serve("0.1.0+250.bbbbbbb")]
    r._maybe_auto_update()
    assert r._stop is False


def test_a_peer_worker_is_not_mistaken_for_the_server():
    """Only the serve presence row counts — another worker being newer is not a
    reason to update."""
    r = _runner(up_seconds=3600, worker_auto_update=True)
    r._version = "0.1.0+240.aaaaaaa"
    r.store.workers = [WorkerInfo("w2", "0.1.0+250.bbbbbbb", None, "idle", 1.0)]
    r._maybe_auto_update()
    assert r._stop is False
