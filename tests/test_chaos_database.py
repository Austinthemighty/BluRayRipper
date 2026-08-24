"""Chaos: Postgres goes away mid-job, and comes back.

The database is on its own host, so it disappears on its own schedule — during a
rip, during an encode, while serve is polling. Production has the log line for
it, captured verbatim below:

    persist failed: database unreachable (in cooldown after a recent failure)

These tests inject a store that fails on save / load_all / claim_next /
record_worker / heartbeat at each phase, and assert the SYSTEM'S RESPONSE rather
than that an exception escaped: is the work in flight still there, is it claimed
twice, does serve keep running, is the outage named once instead of every tick,
and when the database returns does the box reconverge or stay stuck?
"""

import re
from collections import Counter

import pytest
from fastapi.testclient import TestClient

import blurayripper.web.manager as mgr
from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, EpisodeAssignment, MediaKind, Title
from blurayripper.persistence import (
    JobRecord,
    MemoryStore,
    PostgresStore,
    WorkerInfo,
)
from blurayripper.pipeline import Job, JobState, TitleResult
from blurayripper.web.app import create_app
from blurayripper.web.manager import JobManager, WebJob
from blurayripper.worker import WorkerReporter, WorkerRunner, job_from_record

# The exact text PostgresStore raises once its circuit breaker is open, and the
# text the production log shows on the other side of _persist's except clause.
DB_DOWN = "database unreachable (in cooldown after a recent failure)"
DB_DOWN_RE = re.escape(DB_DOWN)


# --- a store that can be switched off and on ------------------------------


class FlakyStore(MemoryStore):
    """A durable store whose individual operations can be taken down.

    Everything not in ``down`` behaves exactly like MemoryStore, so a job that
    survives an outage really is readable afterwards — the tests below claim and
    load through this same object rather than peeking at private state.
    """

    durable = True

    def __init__(self) -> None:
        super().__init__()
        self.down: set[str] = set()
        self.calls: Counter = Counter()
        self.hooks: dict = {}          # op -> fn(call_number), fired before the guard
        self.workers: list[WorkerInfo] = []
        self.presence: list[tuple] = []
        self.on_tick = lambda: None    # fired once per _refresh_from_store pass

    def _guard(self, op: str) -> None:
        self.calls[op] += 1
        hook = self.hooks.get(op)
        if hook is not None:
            hook(self.calls[op])
        if op in self.down:
            raise ConnectionError(DB_DOWN)

    def init(self) -> None:
        self._guard("init")

    def save(self, record):
        self._guard("save")
        super().save(record)

    def load_all(self):
        self._guard("load_all")
        return super().load_all()

    def claim_next(self, worker_id, states, stale_after_seconds=180):
        self._guard("claim_next")
        return super().claim_next(worker_id, states, stale_after_seconds)

    def release(self, job_id, owner=None):
        self._guard("release")
        super().release(job_id)

    def heartbeat(self, job_id, worker_id):
        self._guard("heartbeat")

    def record_worker(self, worker_id, version, job_id, stage, run_token=None):
        self.on_tick()
        self._guard("record_worker")
        self.presence.append((worker_id, job_id, stage))

    def list_workers(self):
        self._guard("list_workers")
        return list(self.workers)


def _disc(name="WarGames"):
    return DiscInfo(source="disc:0", disc_name=name,
                    titles=[Title(title_id=0, duration_seconds=6762, chapter_count=12)])


@pytest.fixture
def manager(tmp_path, monkeypatch):
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path / "library")
    monkeypatch.setattr(settings.storage, "staging_dir", tmp_path / "staging")
    settings.notify_url = None
    settings.cluster.use_workers = True
    settings.cluster.worker_poll_seconds = 0
    return JobManager(settings, store=FlakyStore())


def _job(manager, job_id, source="disc:0", state=JobState.QUEUED, disc=None):
    wj = WebJob(id=job_id, source=source, state=state)
    wj.disc = disc
    job = Job(source=source)
    job.disc = disc
    job.state = state
    wj._pipeline_job = job
    manager.jobs[job_id] = wj
    return wj


def _poll(manager, ticks):
    """Let ``_refresh_from_store`` run exactly ``ticks`` passes, then stop it.

    Counted on record_worker, the first store call of every pass, so the count is
    right whether or not the pass's reads succeed.
    """
    manager.settings.cluster.worker_poll_seconds = 0
    manager.closing = False
    seen = {"n": 0}

    def tick():
        seen["n"] += 1
        if seen["n"] >= ticks:
            manager.closing = True

    manager.store.on_tick = tick
    return seen


# --- the connection-level circuit breaker ---------------------------------


def _install_psycopg(monkeypatch, connect):
    """Register a fake psycopg (plus the types.json submodule save() imports)."""
    import sys
    import types

    psycopg = types.ModuleType("psycopg")
    psycopg.connect = connect
    types_mod = types.ModuleType("psycopg.types")
    json_mod = types.ModuleType("psycopg.types.json")
    json_mod.Jsonb = lambda value: ("jsonb", value)
    types_mod.json = json_mod
    psycopg.types = types_mod
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.types", types_mod)
    monkeypatch.setitem(sys.modules, "psycopg.types.json", json_mod)


class _FakeCursor:
    def __init__(self, log):
        self._log = log

    def execute(self, sql, params=None):
        self._log.append((sql, params))

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, log):
        self._log = log

    def cursor(self):
        return _FakeCursor(self._log)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_dead_database_costs_one_connect_storm_not_one_per_call(monkeypatch):
    """Every call opens its own connection, so without the breaker a Postgres host
    that is *down* (packets dropped, not refused) blocks each persist for the whole
    connect timeout — and serve's event loop with it. After the first sustained
    failure every operation the cluster leans on must fail instantly instead.
    """
    import blurayripper.persistence as P

    connects = {"n": 0}

    def dead(*a, **k):
        connects["n"] += 1
        raise OSError("connection to server at \"db\" failed: Connection refused")

    _install_psycopg(monkeypatch, dead)
    monkeypatch.setattr(P.time, "sleep", lambda *_a: None)  # skip the retry backoff
    store = PostgresStore("postgresql://db/brr")

    with pytest.raises(OSError):
        store.save(JobRecord(id=1, source="disc:0", state="ready"))
    assert connects["n"] == 3  # retried in-call: one blip must not open the breaker

    for call in (
        lambda: store.load_all(),
        lambda: store.claim_next("enc1", ["ready"]),
        lambda: store.record_worker("enc1", "v1", None, "idle"),
        lambda: store.heartbeat(1, "enc1"),
    ):
        with pytest.raises(ConnectionError, match=DB_DOWN_RE):
            call()
    assert connects["n"] == 3  # cooldown — four more ops, zero extra connects


def test_the_breaker_closes_again_when_postgres_comes_back(monkeypatch):
    """A cooldown that never lifts would be its own outage. Once the window has
    passed and the host answers, the write must actually reach SQL and the breaker
    must clear, or the next call pays the connect storm all over again.
    """
    import time

    import blurayripper.persistence as P

    log: list = []
    up = {"yes": False}

    def connect(*a, **k):
        if not up["yes"]:
            raise OSError("Connection refused")
        return _FakeConn(log)

    _install_psycopg(monkeypatch, connect)
    monkeypatch.setattr(P.time, "sleep", lambda *_a: None)
    store = PostgresStore("postgresql://db/brr")
    rec = JobRecord(id=42, source="disc:0", state="ready")

    with pytest.raises(OSError):
        store.save(rec)
    assert store._down_until > 0        # breaker open
    with pytest.raises(ConnectionError, match=DB_DOWN_RE):
        store.save(rec)
    assert log == []                    # and nothing reached SQL while it was open

    up["yes"] = True                    # Postgres is back…
    store._down_until = time.monotonic() - 0.001   # …and the cooldown has elapsed
    store.save(rec)

    assert store._down_until == 0.0     # healthy again, no cooldown for the next call
    assert any("INSERT INTO jobs" in sql for sql, _ in log)
    assert log[-1][1][0] == 42 and log[-1][1][2] == "ready"  # id + claimable state


# --- serve keeps running --------------------------------------------------


def test_a_persist_failure_never_kills_the_job_and_names_the_cause(manager):
    """The gate the operator just clicked must still move. Letting the store's
    exception out of _persist would surface a 500 on a click that did work, and
    strand the job at the gate it already left.
    """
    manager.store.down.add("save")
    wj = _job(manager, 1, state=JobState.AWAITING_TITLES, disc=_disc())

    manager.set_titles(1, [0])

    assert wj.state is JobState.AWAITING_MAPPING     # the decision was taken
    assert wj.selected_title_ids == [0]
    assert any(f"persist failed: {DB_DOWN}" in line for line in wj.log)


async def test_an_outage_must_not_make_a_live_remote_encode_look_stalled(manager):
    """Serve decides "is anything working this job?" from the worker rows it last
    read. Blanking that view because the DATABASE is unreachable would flag a
    perfectly healthy encode as stalled — and Requeue then releases the claim and
    starts a SECOND encode of the same title onto the same output path.
    """
    wj = _job(manager, 3, state=JobState.ENCODING)
    wj._pipeline_job.state = JobState.ENCODING
    wj.progress = 0.42
    manager._workers_cache = [WorkerInfo("enc1", "v1", 3, "encode", 4.0)]
    manager._spawn = lambda coro: coro.close()

    manager.store.down |= {"record_worker", "list_workers", "load_all"}
    _poll(manager, 3)
    await manager._refresh_from_store()

    assert [w.worker_id for w in manager._workers_cache] == ["enc1"]  # view kept
    assert wj.state is JobState.ENCODING and wj.progress == 0.42
    assert manager._is_stalled(wj, JobState.ENCODING) is False
    with pytest.raises(ValueError, match="isn't stalled"):
        manager.requeue(3)

    # Contrast: once the database answers again and the worker really has gone,
    # the same job DOES read as stalled and Requeue is accepted — so the check
    # above passed because of the preserved view, not because nothing can stall.
    manager.store.down.clear()
    manager.store.workers = []
    _poll(manager, 2)
    await manager._refresh_from_store()

    assert manager._workers_cache == []
    assert manager._is_stalled(wj, JobState.ENCODING) is True
    manager.requeue(3)
    assert wj.state is JobState.READY
    assert {r.id: r.state for r in manager.store.load_all()}[3] == "ready"


async def test_a_database_outage_does_not_page_you_about_a_worker_that_is_fine(manager):
    """The offline alert is "high" priority — it rings a phone. Firing it because
    THIS box can't reach Postgres would cry wolf on every blip, and then be ignored
    on the night a worker actually dies.
    """
    sent: list[str] = []
    manager._notify = lambda title, msg, priority=None: sent.append(title)
    manager.settings.cluster.offline_alert_seconds = 300
    manager.store.workers = [WorkerInfo("enc1", "v1", None, "idle", 2.0)]

    _poll(manager, 2)
    await manager._refresh_from_store()      # healthy: enc1 is seen and remembered
    assert sent == []

    manager.store.down |= {"record_worker", "list_workers", "load_all"}
    _poll(manager, 4)
    await manager._refresh_from_store()
    assert sent == []                        # four blind polls, no alert

    manager.store.down.clear()               # database back, and enc1 really is gone
    manager.store.workers = []
    _poll(manager, 2)
    await manager._refresh_from_store()
    assert [t for t in sent if "Worker offline" in t] == ["🔌 Worker offline: enc1"]


async def test_the_outage_is_named_once_and_recovery_only_announced_when_it_is_over(
    manager, capsys
):
    """Once per distinct failure, not once per poll — a line repeated every poll
    interval buries the one that says what broke. And "reconnected" has to mean
    it: a pass where the job read succeeded but the presence side is still failing
    is not a recovery, and announcing one would say the outage is over while the
    workers panel is still frozen.
    """
    manager.store.down.add("list_workers")
    _poll(manager, 3)
    await manager._refresh_from_store()

    out = capsys.readouterr().out
    assert out.count("[store] list_workers failed") == 1   # three passes, one line
    assert DB_DOWN in out
    # load_all was fine all three passes; the pass still isn't "ok", so nothing
    # claims the database is back.
    assert "reconnected" not in out

    manager.store.down.clear()
    _poll(manager, 2)
    await manager._refresh_from_store()
    assert capsys.readouterr().out.count("reconnected") == 1


async def test_a_row_that_vanished_from_the_database_does_not_reset_the_job(manager, capsys):
    """Postgres coming back from an older dump (or a hand-deleted row) leaves jobs
    the store has never heard of. The in-memory job is then the only copy of that
    encode's progress; treating "no row" as "reset it" throws that away, and
    letting the missing row raise aborts the pass for every OTHER job too.
    """
    wj = _job(manager, 4, state=JobState.ENCODING)
    wj._pipeline_job.state = JobState.ENCODING
    wj.stage, wj.progress = "encode", 0.77
    manager.store.save(JobRecord(id=99, source="disc:9", state="done"))  # a different job

    _poll(manager, 2)
    await manager._refresh_from_store()

    assert wj.state is JobState.ENCODING
    assert (wj.stage, wj.progress) == ("encode", 0.77)
    assert "load_all failed" not in capsys.readouterr().out


def test_serve_starts_with_the_database_down_instead_of_refusing_to_boot(
    tmp_path, monkeypatch, capsys
):
    """Recovery reads the store on startup. If that read taking the process down
    were allowed, a Postgres outage would also mean no UI at all — no way to see
    the queue, and no way to notice the moment the database comes back.
    """
    store = FlakyStore()
    store.down |= {"init", "load_all"}
    monkeypatch.setattr(mgr, "make_store", lambda url: store)
    monkeypatch.setattr(mgr, "get_version", lambda: "0.1.0+1.testing")
    monkeypatch.setattr(JobManager, "start_drive_watch", lambda self: None)
    refreshing = []

    async def fake_refresh(self):
        refreshing.append(self)

    monkeypatch.setattr(JobManager, "_refresh_from_store", fake_refresh)

    settings = load_settings()
    settings.storage.output_dir = tmp_path / "library"
    settings.storage.staging_dir = tmp_path / "staging"
    settings.cluster.use_workers = True
    settings.notify_url = None

    with TestClient(create_app(settings)) as client:
        body = client.get("/api/jobs").json()

    assert body["jobs"] == []                       # nothing recovered, but…
    assert "Job recovery skipped" in capsys.readouterr().out
    assert refreshing, "the store poll must still start, so serve can reconverge"


# --- the divergence: serve says READY, the database never heard ------------


def _rip_during_outage(manager, tmp_path, monkeypatch):
    """Run a clustered job whose rip completes while every save fails."""
    ripped = tmp_path / "staging" / "job_12" / "rip" / "t000" / "WarGames_t00.mkv"
    ripped.parent.mkdir(parents=True, exist_ok=True)
    ripped.write_bytes(b"video")

    wj = _job(manager, 12, state=JobState.READY, disc=_disc())
    wj.selected_title_ids = [0]
    wj.assignments = {0: EpisodeAssignment(0, MediaKind.MOVIE, "WarGames", 1983)}

    async def fake_prep(job, staging_root, report, include_subs=True):
        report.on_event("Saving 1 titles into directory file:///mnt/TrueNas/.../rip/t000")
        report.on_event("Copy complete. 1 titles saved.")
        job.checkpoints[0] = {"ripped_path": str(ripped)}
        report.checkpoint()          # the durable checkpoint that cannot be written

    monkeypatch.setattr(manager.pipeline, "prep_phase", fake_prep)
    manager.store.down.add("save")
    return wj, ripped


async def test_a_rip_that_finishes_during_the_outage_is_queued_where_no_worker_sees_it(
    manager, tmp_path, monkeypatch
):
    """The sharpest case, and one that really happened: serve's memory says READY,
    the persist that would have told the database failed. The rip itself must
    survive in full — the disc may already be ejected — and the operator must be
    able to see WHY the job is going nowhere, because a job no worker can claim is
    invisible for as long as the divergence lasts.
    """
    wj, ripped = _rip_during_outage(manager, tmp_path, monkeypatch)

    await manager._run_job(wj)

    assert wj.state is JobState.READY                                  # serve: queued
    assert wj._pipeline_job.checkpoints[0]["ripped_path"] == str(ripped)
    assert ripped.exists()                                             # rip not lost
    assert manager.store.load_all() == []                              # database: never heard
    assert manager.store.claim_next("enc1", ["ready"]) is None         # so nobody can take it
    # Every failed persist is recorded against the job, so the log says what the
    # queue position doesn't: the checkpoint AND the hand-off both went nowhere.
    assert sum(f"persist failed: {DB_DOWN}" in line for line in wj.log) >= 2


async def test_when_the_database_returns_the_next_persist_makes_the_job_claimable(
    manager, tmp_path, monkeypatch
):
    """Reconvergence has to hand a worker enough to resume rather than start over:
    the row it finally writes must carry the rip checkpoints, or the worker's own
    gate bounces the job straight back to the rip box as "not fully ripped".
    """
    wj, ripped = _rip_during_outage(manager, tmp_path, monkeypatch)
    await manager._run_job(wj)
    assert manager.store.claim_next("enc1", ["ready"]) is None  # invisible, as above

    manager.store.down.clear()          # Postgres is back
    manager.bump_job(12)                # any action that persists republishes it

    rec = {r.id: r for r in manager.store.load_all()}[12]
    assert rec.state == "ready"
    assert rec.checkpoints == {"0": {"ripped_path": str(ripped)}}
    claimed = manager.store.claim_next("enc2", ["ready"])
    assert claimed is not None and claimed.id == 12

    runner = _runner(manager.settings)
    assert runner._fully_ripped(claimed) is True   # encodes; does NOT re-rip


# --- workers --------------------------------------------------------------


def _runner(settings, store=None, worker_id="enc1"):
    settings.database_url = "postgresql://db/brr"
    settings.cleanup_stale_tools = False
    settings.notify_url = None
    settings.cluster.worker_poll_seconds = 0
    runner = WorkerRunner(settings, worker_id=worker_id)
    if store is not None:
        runner.store = store
    return runner


async def test_a_worker_rides_out_a_queue_outage_and_claims_once_the_db_returns(
    tmp_path, monkeypatch, capsys
):
    """A worker that exits on a database blip has to be restarted by hand on every
    box. It must keep polling, say why exactly once, take nothing while it is
    blind, and pick the job up when the queue answers again.
    """
    monkeypatch.setattr("blurayripper.version.get_version", lambda: "0.1.0+9.deadbee")
    settings = load_settings()
    settings.storage.staging_dir = tmp_path / "staging"
    settings.storage.output_dir = tmp_path / "library"
    store = FlakyStore()
    runner = _runner(settings, store)
    monkeypatch.setattr(runner, "_apply_worker_settings", lambda: None)

    ripped = tmp_path / "staging" / "job_7" / "rip" / "t000" / "a.mkv"
    ripped.parent.mkdir(parents=True)
    ripped.write_bytes(b"video")
    store.save(JobRecord(id=7, source="disc:0", state="ready", selected_title_ids=[0],
                         checkpoints={"0": {"ripped_path": str(ripped)}}))

    events: list = []
    store.down.add("claim_next")

    def db_returns(n):
        if n == 3:                    # three blind polls, then the host answers
            store.down.discard("claim_next")
            events.append("db_back")

    store.hooks["claim_next"] = db_returns

    async def fake_process(rec):
        events.append(f"processed {rec.id}")
        runner.request_stop()

    monkeypatch.setattr(runner, "_process", fake_process)

    await runner.run()

    assert events == ["db_back", "processed 7"]     # nothing taken while blind
    out = capsys.readouterr().out
    assert out.count("Queue unavailable, will retry") == 1   # three failures, one line
    assert DB_DOWN in out


async def test_a_worker_that_cannot_report_its_result_still_finishes_and_frees_the_job(
    tmp_path, monkeypatch
):
    """A store outage during an encode must not throw the encode away, and must not
    leave the row claimed behind a frozen heartbeat — that is 180 stale seconds in
    which no worker will touch a job the operator can see is going nowhere.
    """
    settings = load_settings()
    settings.storage.staging_dir = tmp_path / "staging"
    settings.storage.output_dir = tmp_path / "library"
    store = FlakyStore()
    runner = _runner(settings, store)

    ripped = tmp_path / "staging" / "job_9" / "rip" / "t000" / "a.mkv"
    ripped.parent.mkdir(parents=True)
    ripped.write_bytes(b"video")
    store.save(JobRecord(id=9, source="disc:0", state="ready", selected_title_ids=[0],
                         checkpoints={"0": {"ripped_path": str(ripped)}}))
    rec = store.claim_next("enc1", ["ready"])
    assert rec is not None

    placed = tmp_path / "library" / "WarGames (1983)" / "WarGames (1983).mkv"
    placed.parent.mkdir(parents=True)

    async def fake_encode(job, staging_root, scheduler, report):
        report.on_event("Encoding …")
        placed.write_bytes(b"encoded")
        job.checkpoints[0] = {"ripped_path": str(ripped), "placed": str(placed)}
        report.checkpoint()
        return [TitleResult(title_id=0, video_path=placed)]

    monkeypatch.setattr(runner.pipeline, "encode_phase", fake_encode)
    store.down |= {"save", "heartbeat"}

    await runner._process(rec)

    assert placed.exists()                    # the encode completed and was placed
    assert {r.id: r.state for r in store.load_all()}[9] == "ready"  # DB never learned
    # The claim is dropped anyway, so the job is immediately reclaimable rather
    # than orphaned until its heartbeat ages out.
    again = store.claim_next("enc2", ["ready"])
    assert again is not None and again.id == 9


def test_checkpoints_taken_during_the_outage_all_land_on_the_first_save_after_it(tmp_path):
    """The reporter sends a whole snapshot, not a delta. That is what makes an
    outage cost nothing once it ends: every title finished while the database was
    away is still marked placed, so a reclaim re-encodes none of them.
    """
    store = FlakyStore()
    store.save(JobRecord(id=9, source="disc:0", state="ready", selected_title_ids=[0, 1]))
    rec = store.load_all()[0]
    job = job_from_record(rec)
    reporter = WorkerReporter(store, "enc1", 9, job)

    store.down.add("save")
    job.checkpoints[0] = {"ripped_path": "job_9/rip/t000/a.mkv", "placed": "/lib/a.mkv"}
    reporter.checkpoint()
    job.checkpoints[1] = {"ripped_path": "job_9/rip/t001/b.mkv", "placed": "/lib/b.mkv"}
    reporter.checkpoint()
    assert {r.id: r.state for r in store.load_all()}[9] == "ready"  # nothing got through

    store.down.clear()
    reporter.finish(JobState.DONE, ["/lib/a.mkv", "/lib/b.mkv"])

    saved = {r.id: r for r in store.load_all()}[9]
    assert saved.state == "done"
    assert sorted(saved.checkpoints) == ["0", "1"]
    assert all(cp.get("placed") for cp in saved.checkpoints.values())
    assert saved.outputs == ["/lib/a.mkv", "/lib/b.mkv"]
