"""A database outage must not leave the box permanently wrong.

The failure mode is not the outage itself — it is what remains afterwards. A rip
that finished while Postgres was away sat at READY in serve's memory with no row
in the store, so no worker could ever claim it and the UI showed a queued job
that never moved. That is how two discs were stranded. Nothing retried the save,
and the refresh loop only ever read.
"""

import asyncio

from blurayripper.config import load_settings
from blurayripper.persistence import MemoryStore
from blurayripper.pipeline import Job, JobState
from blurayripper.web.manager import JobManager, WebJob


class Flaky(MemoryStore):
    """A durable store that can be taken away and given back."""

    def __init__(self):
        super().__init__()
        self.down = False

    @property
    def durable(self): return True

    def _boom(self):
        raise ConnectionError("database unreachable")

    def save(self, record):
        if self.down: self._boom()
        return super().save(record)

    def load_all(self):
        if self.down: self._boom()
        return super().load_all()

    def list_workers(self):
        if self.down: self._boom()
        return super().list_workers()


def _mgr(tmp_path, monkeypatch, store):
    s = load_settings()
    monkeypatch.setattr(s.storage, "output_dir", tmp_path)
    s.cluster.use_workers = True
    return JobManager(s, store=store)


def _ready_job(m, job_id=334):
    wj = WebJob(id=job_id, source="disc:0", state=JobState.READY)
    wj._pipeline_job = Job(source="disc:0")
    # The persisted state comes from the PIPELINE job (_live_state), so leaving
    # this at its default would store "queued" and no worker would claim it —
    # for a reason unrelated to the outage being tested.
    wj._pipeline_job.state = JobState.READY
    m.jobs[job_id] = wj
    return wj


# --- a save that failed during the outage is retried afterwards --------------

def test_a_job_saved_during_an_outage_is_written_once_the_database_returns(
        tmp_path, monkeypatch):
    """Jobs 334 and 335. Serve held READY, the store had no row, so
    claim_next found nothing and the job was invisible to every worker —
    forever, since nothing retried and the refresh loop only reads."""
    store = Flaky()
    m = _mgr(tmp_path, monkeypatch, store)
    wj = _ready_job(m)

    store.down = True
    m._persist(wj)
    assert store.claim_next("w1", ["ready"]) is None      # invisible to workers
    assert 334 in m._unpersisted                          # ...but not forgotten

    store.down = False
    assert m._flush_unpersisted() == 1
    assert m._unpersisted == set()
    claimed = store.claim_next("w1", ["ready"])
    assert claimed is not None and claimed.id == 334      # a worker can see it


def test_the_job_says_it_was_invisible_rather_than_recovering_silently(
        tmp_path, monkeypatch):
    """The operator watched it sit there. Saying nothing would leave them
    wondering whether it had been stuck or they had imagined it."""
    store = Flaky()
    m = _mgr(tmp_path, monkeypatch, store)
    wj = _ready_job(m)
    store.down = True
    m._persist(wj)
    store.down = False
    m._flush_unpersisted()

    assert any("invisible to workers" in line for line in wj.log)


def test_a_still_broken_database_keeps_the_job_queued_for_retry(tmp_path, monkeypatch):
    """A flush during a continuing outage must not drop the job from the list —
    that would lose the only record that it still needs writing."""
    store = Flaky()
    m = _mgr(tmp_path, monkeypatch, store)
    wj = _ready_job(m)
    store.down = True
    m._persist(wj)

    assert m._flush_unpersisted() == 0
    assert 334 in m._unpersisted


def test_a_successful_save_clears_any_earlier_failure(tmp_path, monkeypatch):
    """An operator action that happens to persist repairs it too; the job must
    not then be written a second time."""
    store = Flaky()
    m = _mgr(tmp_path, monkeypatch, store)
    wj = _ready_job(m)
    store.down = True
    m._persist(wj)
    store.down = False
    m._persist(wj)

    assert m._unpersisted == set()


def test_a_job_removed_while_unpersisted_is_dropped_not_resurrected(
        tmp_path, monkeypatch):
    """Flushing must not write back a job the operator has since deleted."""
    store = Flaky()
    m = _mgr(tmp_path, monkeypatch, store)
    _ready_job(m)
    store.down = True
    m._persist(m.jobs[334])
    del m.jobs[334]

    store.down = False
    assert m._flush_unpersisted() == 0
    assert m._unpersisted == set()
    assert store.claim_next("w1", ["ready"]) is None


# --- the outage message must not become every-tick spam ----------------------

def test_a_total_outage_reports_each_operation_once_not_on_every_poll(
        tmp_path, monkeypatch, capsys):
    """With one shared slot, a full outage failed list_workers and load_all
    alternately with DIFFERENT messages, so each call saw a different "last" and
    printed again — two lines every poll interval, forever. It degraded in
    exactly the case the dedupe exists for."""
    m = _mgr(tmp_path, monkeypatch, MemoryStore())
    for _ in range(3):
        m._log_store_error("list_workers", ConnectionError("database unreachable"))
        m._log_store_error("load_all", ConnectionError("database unreachable"))

    out = capsys.readouterr().out
    assert out.count("list_workers failed") == 1
    assert out.count("load_all failed") == 1


def test_a_new_outage_is_reported_again_after_a_clean_run(tmp_path, monkeypatch, capsys):
    """Silencing it forever would be the opposite failure."""
    m = _mgr(tmp_path, monkeypatch, MemoryStore())
    m._log_store_error("load_all", ConnectionError("database unreachable"))
    m._last_store_error.clear()                    # a clean poll happened
    m._log_store_error("load_all", ConnectionError("database unreachable"))

    assert capsys.readouterr().out.count("load_all failed") == 2


# --- a startup that could not read the store must retry ----------------------

def test_a_startup_during_an_outage_loads_the_queue_once_the_store_returns(
        tmp_path, monkeypatch):
    """The refresh loop only updates jobs it ALREADY knows about, so a recover()
    that failed at boot left the queue reading as empty while workers encoded
    jobs it had never loaded — until someone restarted serve a second time."""
    store = Flaky()
    store.save(_rec(901))
    m = _mgr(tmp_path, monkeypatch, store)

    store.down = True
    try:
        m.recover()
    except ConnectionError:
        m._recover_pending = True
    assert m.jobs == {}                            # nothing known

    store.down = False
    m.recover()                                    # what the poll loop retries
    assert 901 in m.jobs


def _rec(job_id):
    from blurayripper.persistence import JobRecord
    return JobRecord(id=job_id, source="disc:0", state="ready",
                     selected_title_ids=[], outputs=[])


# --- a checkpoint that names a file which is gone ----------------------------

def _ripped_job(m, tmp_path, job_id, rel, make_file):
    """A clustered READY job whose checkpoint claims a ripped file."""
    from blurayripper.models import DiscInfo, Title
    wj = WebJob(id=job_id, source="disc:1", state=JobState.READY)
    job = Job(source="disc:1")
    job.state = JobState.READY
    job.disc = DiscInfo(source="disc:1", disc_name="X", titles=[
        Title(title_id=0, duration_seconds=100, size_bytes=1,
              source_filename="t00.mkv", streams=[])])
    job.selected_title_ids = [0]
    job.checkpoints = {0: {"ripped_path": rel}}
    wj._pipeline_job = job
    wj.selected_title_ids = [0]
    wj.disc = job.disc
    if make_file:
        f = m.settings.storage.staging_dir / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"a real rip")
    m.jobs[job_id] = wj
    m.store.save(m._to_record(wj))
    return wj


def _clustered(tmp_path, monkeypatch, store):
    s = load_settings()
    monkeypatch.setattr(s.storage, "output_dir", tmp_path / "out")
    monkeypatch.setattr(s.storage, "staging_dir", tmp_path / "stage")
    (tmp_path / "stage").mkdir(parents=True, exist_ok=True)
    (tmp_path / "stage" / ".keep").write_text("")   # staging is readable
    s.cluster.use_workers = True
    return JobManager(s, store=store)


def test_a_ready_job_whose_ripped_file_is_gone_is_parked_at_startup(tmp_path, monkeypatch):
    """187 of these sat in the encode lane. The checkpoint said ripped, the disk
    disagreed, and only the worker ever looked — one job at a time, and only when
    that job happened to be next claimable, which drains at a job per grace
    period. Serve sees the whole queue and can stat the lot in one pass."""
    store = Flaky()
    m = _clustered(tmp_path, monkeypatch, store)
    _ripped_job(m, tmp_path, 311, "job_311/rip/t000/gone.mkv", make_file=False)

    m.jobs.clear()
    m.recover()

    wj = m.jobs[311]
    assert wj.state is JobState.FAILED
    assert "Rip never completed" in (wj.error or "")
    # the lying checkpoint is cleared, so it reads as needing a rip again
    assert not (wj._pipeline_job.checkpoints.get(0) or {}).get("ripped_path")
    assert m._job_dict(wj)["needs_review"] is True


def test_a_ready_job_whose_rip_is_really_there_is_left_for_a_worker(tmp_path, monkeypatch):
    """The check must not touch the healthy majority — that would fail an entire
    working queue."""
    store = Flaky()
    m = _clustered(tmp_path, monkeypatch, store)
    _ripped_job(m, tmp_path, 312, "job_312/rip/t000/here.mkv", make_file=True)

    m.jobs.clear()
    m.recover()

    wj = m.jobs[312]
    assert wj.state is JobState.READY
    assert (wj._pipeline_job.checkpoints.get(0) or {}).get("ripped_path")


def test_an_unmounted_share_parks_nothing_at_all(tmp_path, monkeypatch, capsys):
    """The dangerous case. A share that has not mounted yet makes EVERY ripped
    file look gone, and parking the whole queue over a mount that appears a
    second later is far worse than the problem being solved."""
    store = Flaky()
    s = load_settings()
    monkeypatch.setattr(s.storage, "output_dir", tmp_path / "out")
    monkeypatch.setattr(s.storage, "staging_dir", tmp_path / "not-mounted")
    s.cluster.use_workers = True
    m = JobManager(s, store=store)
    _ripped_job(m, tmp_path, 313, "job_313/rip/t000/gone.mkv", make_file=False)

    m.jobs.clear()
    m.recover()

    assert m.jobs[313].state is JobState.READY       # untouched
    assert "not readable" in capsys.readouterr().out
