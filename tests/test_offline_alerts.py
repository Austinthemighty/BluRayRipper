"""Workers watch the rip box: an offline server can't report its own outage."""

from blurayripper import presence
from blurayripper.config import load_settings
from blurayripper.persistence import MemoryStore, WorkerInfo
from blurayripper.worker import WorkerRunner


class _Store(MemoryStore):
    """A store whose worker list the test drives directly."""
    def __init__(self):
        super().__init__()
        self.workers: list[WorkerInfo] = []
        self.fail = False

    @property
    def durable(self):
        return True

    def list_workers(self):
        if self.fail:
            raise RuntimeError("database unreachable")
        return list(self.workers)


def _runner(monkeypatch, tmp_path, window=300):
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path)
    settings.database_url = "postgresql://x/y"
    settings.cluster.offline_alert_seconds = window
    settings.notify_url = "https://ntfy.example/brr"
    r = WorkerRunner(settings, worker_id="SteamMachine")
    r.store = _Store()
    sent = []
    monkeypatch.setattr(r, "_notify",
                        lambda t, m, priority=None: sent.append((t, m, priority)))
    return r, sent


def _serve(age):
    return WorkerInfo(worker_id=presence.serve_id("RIPBOX"), version="v1",
                      job_id=None, stage="serving", seconds_since_seen=age)


def test_worker_notifies_when_the_server_goes_quiet(monkeypatch, tmp_path):
    r, sent = _runner(monkeypatch, tmp_path)

    r.store.workers = [_serve(5)]
    r._check_serve_presence()
    assert sent == []                                  # healthy -> silence

    r.store.workers = [_serve(600)]
    r._check_serve_presence()
    assert len(sent) == 1
    assert "Server offline" in sent[0][0] and "RIPBOX" in sent[0][0]
    assert sent[0][2] == "high"

    r._check_serve_presence()                          # still down, don't nag
    assert len(sent) == 1

    r.store.workers = [_serve(3)]
    r._check_serve_presence()
    assert len(sent) == 2 and "Server back" in sent[1][0]


def test_a_database_outage_is_not_reported_as_the_server_being_down(monkeypatch, tmp_path):
    """Losing the store is THIS worker's connectivity problem. Guessing would cry
    wolf on every blip — and the rip box may be perfectly fine."""
    r, sent = _runner(monkeypatch, tmp_path)
    r.store.workers = [_serve(5)]
    r._check_serve_presence()

    r.store.fail = True
    for _ in range(3):
        r._check_serve_presence()
    assert sent == []


def test_worker_ignores_other_workers(monkeypatch, tmp_path):
    """A worker watches the SERVER only — the server already watches the workers,
    so alerting on peers would double up."""
    r, sent = _runner(monkeypatch, tmp_path)
    peer = WorkerInfo(worker_id="DESKTOP-S1SQ57I", version="v1", job_id=None,
                      stage="idle", seconds_since_seen=5)
    r.store.workers = [peer]
    r._check_serve_presence()
    r.store.workers = [WorkerInfo(worker_id="DESKTOP-S1SQ57I", version="v1", job_id=None,
                                  stage="idle", seconds_since_seen=9999)]
    r._check_serve_presence()
    assert sent == []


def test_alerts_off_when_window_is_zero(monkeypatch, tmp_path):
    r, sent = _runner(monkeypatch, tmp_path, window=0)
    r.store.workers = [_serve(5)]
    r._check_serve_presence()
    r.store.workers = [_serve(99999)]
    r._check_serve_presence()
    assert sent == []
