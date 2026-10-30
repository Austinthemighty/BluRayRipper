"""Chaos in the cluster: boxes that die at the wrong moment.

The cluster is a serve box that rips discs and one or more encode boxes that
claim those jobs out of a shared Postgres. Everything injected here has actually
happened to it: an encoder losing power four hours into a 4K encode, two boxes
polling in the same millisecond, serve restarting while a disc is halfway
ripped, the NAS share disappearing between the claim and the encode, a box
running last week's build, and clocks that don't agree.

The outcome that must never happen is two processes encoding one job. They both
compute the SAME output path from the same naming, so the loser drops a
half-written file on top of the winner's finished one and the row still says
"done". So every test here is about ownership: exactly one holder at a time,
work abandoned by a dead box becoming reclaimable but only once its claim has
actually expired, a reclaimed job continuing from its checkpoints instead of
re-encoding what is already in the library, and serve refusing to requeue
anything a live worker is still heart-beating.

Stubbed: HandBrakeCLI and mkvmerge (the two binaries the encode shells out to)
and Postgres, replaced here by an in-memory table on a clock the test advances
by hand, carrying the claim/heartbeat semantics of the real SQL. The worker
loop, the pipeline's checkpoint and placement logic, the manager's recovery,
stall detection and requeue, and the presence protocol are all the real thing.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from pathlib import Path

import pytest

from blurayripper import presence
from blurayripper import version as version_mod
from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, EpisodeAssignment, MediaKind, Title
from blurayripper.persistence import (
    JobRecord,
    MemoryStore,
    PostgresStore,
    WorkerInfo,
    assignment_to_dict,
)
from blurayripper.pipeline import Job, JobState, Pipeline
from blurayripper.tools.handbrake import HandBrake
from blurayripper.tools.mkvtools import MkvTools
from blurayripper.web import manager as manager_mod
from blurayripper.web.manager import JobManager, WebJob
from blurayripper.worker import (
    CLAIMABLE_STATES,
    WorkerReporter,
    WorkerRunner,
    job_from_record,
)

RIP_BOX = "RIPBOX"
BUILD = "0.1.0+240.aaaaaaa"        # what the encode boxes are running
NEWER_BUILD = "0.1.0+260.bbbbbbb"  # ...and what serve gets pulled up to


# --- the shared Postgres, in memory ---------------------------------------

class ChaosStore(MemoryStore):
    """The shared store the whole cluster coordinates through.

    Job rows and cluster signals are MemoryStore's; what is modelled on top is
    the part MemoryStore explicitly does not have (it documents itself as
    single-process) and that all of this hinges on:

      * a claim with a heartbeat that expires — mirroring the WHERE clause in
        ``PostgresStore.claim_next``: claimable state, and either unclaimed or
        last heartbeat older than the stale window, highest priority then
        lowest id;
      * ``heartbeat`` refreshing only a row this worker still owns (the real
        SQL's ``AND claimed_by = %s``);
      * a presence table whose ages come off the STORE's clock, because
        Postgres computes them in SQL — see the DB-clock test below.

    ``clock`` is that database clock, advanced by hand so staleness happens
    without sleeping. ``cut_off`` is a box losing power: its writes simply stop
    landing, which is exactly what a dead encoder looks like from here.
    """

    durable = True

    def __init__(self) -> None:
        super().__init__()
        self.clock = 1_000.0
        self._owner: dict[int, str] = {}
        self._beat: dict[int, float] = {}
        self._presence: dict[str, dict] = {}
        self._dark: set[str] = set()
        self.claim_log: list[tuple[str, int]] = []

    # the database clock
    def tick(self, seconds: float) -> None:
        self.clock += seconds

    def cut_off(self, box_id: str) -> None:
        """Power cut / severed network: nothing this box writes lands again."""
        self._dark.add(box_id)

    def comes_back(self, box_id: str) -> None:
        self._dark.discard(box_id)

    def claim_next(self, worker_id, states, stale_after_seconds=180):
        ordered = sorted(self._rows.items(),
                         key=lambda kv: (-int(kv[1].get("priority") or 0), kv[0]))
        for job_id, data in ordered:
            if data.get("state") not in states:
                continue
            held = self._owner.get(job_id)
            if held is not None and (self.clock - self._beat.get(job_id, 0.0)) <= stale_after_seconds:
                continue  # somebody holds it and is still proving they're alive
            self._owner[job_id] = worker_id
            self._beat[job_id] = self.clock
            self.claim_log.append((worker_id, job_id))
            return JobRecord.from_json(data)
        return None

    def heartbeat(self, job_id, worker_id):
        if worker_id in self._dark:
            return
        if self._owner.get(job_id) == worker_id:
            self._beat[job_id] = self.clock

    def release(self, job_id, owner=None):
        # Owner-scoped exactly like PostgresStore.release: a worker dropping its
        # OWN claim must not be able to drop one that has since moved to another
        # box (that is how the same job gets encoded twice). An unscoped release
        # — what serve's Requeue does — still clears whoever holds it.
        if owner is not None and self._owner.get(job_id) not in (None, owner):
            return
        self._owner.pop(job_id, None)
        self._beat.pop(job_id, None)

    def owner_of(self, job_id):
        return self._owner.get(job_id)

    def save(self, record):
        if record.worker in self._dark:
            return
        super().save(record)

    def record_worker(self, worker_id, version, job_id, stage, run_token=None, address=None):
        if worker_id in self._dark:
            return
        row = self._presence.get(worker_id)
        if row is None or row["run_token"] != run_token:
            row = {"started_at": self.clock, "run_token": run_token}
            self._presence[worker_id] = row
        row.update(version=version, job_id=job_id, stage=stage, last_seen=self.clock)

    # PostgresStore.list_workers only returns rows seen in the last 10 minutes.
    FORGET_AFTER = 600

    def list_workers(self):
        return [
            WorkerInfo(box_id, row["version"], row["job_id"], row["stage"],
                       self.clock - row["last_seen"], self.clock - row["started_at"])
            for box_id, row in sorted(self._presence.items())
            if self.clock - row["last_seen"] <= self.FORGET_AFTER
        ]


# --- the boxes ------------------------------------------------------------

def _config_dir(tmp_path: Path, monkeypatch, **cluster) -> Path:
    """Write the settings every box in this file loads.

    It has to be a real config dir: a worker re-reads its settings from disk
    between jobs, so anything only poked onto a Settings object is thrown away
    on the first poll.
    """
    # load_settings lets ANY BLURAYRIPPER_*/OPENSUBTITLES_*/TMDB_API_KEY process
    # variable override the file, so a developer with (say)
    # BLURAYRIPPER_STAGING_DIR or BLURAYRIPPER_STALE_AFTER_SECONDS exported would
    # silently run these tests against a different cluster than the one written
    # below. Scrub the whole namespace rather than the two that bit us first.
    for name in list(os.environ):
        if name == "TMDB_API_KEY" or name.startswith(("BLURAYRIPPER_", "OPENSUBTITLES_")):
            monkeypatch.delenv(name, raising=False)
    cfg = dict(use_workers=True, worker_poll_seconds=0, heartbeat_seconds=0,
               stale_after_seconds=180, offline_alert_seconds=0,
               local_encode_max_height=0, worker_restart_after_days=0,
               worker_auto_update=False)
    cfg.update(cluster)
    lines = [
        "[storage]",
        f'staging_dir = "{(tmp_path / "staging").as_posix()}"',
        f'output_dir = "{(tmp_path / "library").as_posix()}"',
        "[encode]",
        'container = "mkv"',
        "[metadata]",
        "write_nfo = false",
        "fetch_artwork = false",
        "[disc]",
        "extract_cover_art = false",
        "auto_eject = false",
        "[serve]",
        "cleanup_stale_tools = false",
        "[database]",
        'url = "postgresql://cluster.example/brr"',
        "[cluster]",
    ]
    for key, value in cfg.items():
        lines.append(f"{key} = {str(value).lower() if isinstance(value, bool) else value}")
    conf = tmp_path / "config"
    conf.mkdir(parents=True, exist_ok=True)
    (conf / "settings.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return conf


def _stub_the_tools(monkeypatch, encoded: list[str], on_encode=None):
    """Replace the two binaries the encode phase shells out to.

    Patched on the CLASS: a worker rebuilds its Pipeline whenever it re-reads
    settings, so an instance stub would evaporate between jobs. ``on_encode`` is
    what the rest of the cluster does WHILE a title is encoding.
    """
    async def video_height(self, path):
        return 1080

    async def encode(self, src, dst, *, on_progress=None, tune_grain=None,
                     deinterlace=False):
        encoded.append(Path(dst).name)
        if on_encode is not None:
            await on_encode(Path(dst).name)
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_text(f"h265 bytes for {Path(dst).stem}")
        return dst

    monkeypatch.setattr(MkvTools, "video_height", video_height)
    monkeypatch.setattr(HandBrake, "encode", encode)


def _stub_the_builds(monkeypatch, worker_build=BUILD, serve_build=BUILD):
    """No git subprocess in a test. The two are separate on purpose: a mixed
    build cluster is one of the things being injected."""
    monkeypatch.setattr(version_mod, "get_version", lambda: worker_build)
    monkeypatch.setattr(manager_mod, "get_version", lambda: serve_build)


def _movie(title_id: int, name: str, year: int) -> EpisodeAssignment:
    return EpisodeAssignment(title_id, MediaKind.MOVIE, name, year)


def _library_path(cfg: Path, assignment: EpisodeAssignment) -> Path:
    """Where the pipeline itself would place this title. Used for setup, so a
    test never has to hardcode (and drift from) the real naming."""
    return Pipeline(load_settings(cfg))._video_path_for(assignment)


def _queue(store: ChaosStore, cfg: Path, job_id: int, titles, *, state="ready",
           placed=()) -> dict[int, Path]:
    """Put a ripped, named job in the store exactly as the rip box leaves it:
    the rip files on the shared staging share, checkpoints pointing at them."""
    settings = load_settings(cfg)
    checkpoints, assignments, ripped_files = {}, [], {}
    for title_id, name, year in titles:
        rel = f"job_{job_id}/rip/t{title_id:03d}/title_t{title_id:02d}.mkv"
        ripped = settings.storage.staging_dir / rel
        ripped.parent.mkdir(parents=True, exist_ok=True)
        ripped.write_text(f"a whole Blu-ray of {name}, honest")
        ripped_files[title_id] = ripped
        cp = {"ripped_path": rel}
        if title_id in placed:
            cp["placed"] = True
        checkpoints[str(title_id)] = cp
        assignments.append(assignment_to_dict(_movie(title_id, name, year)))
    store.save(JobRecord(
        id=job_id, source="disc:0", state=state,
        selected_title_ids=[t[0] for t in titles],
        assignments=assignments, checkpoints=checkpoints))
    return ripped_files


def _worker(cfg: Path, store: ChaosStore, name: str) -> WorkerRunner:
    runner = WorkerRunner(load_settings(cfg), worker_id=name)
    runner.store = store
    return runner


def _stop_after(runner: WorkerRunner, monkeypatch, polls: int = 1):
    """Drain the runner after `polls` trips round its real loop, so ``run()``
    returns without waiting out a poll interval.

    Hooked on the runner's own between-jobs signal check (the first thing each
    iteration does) rather than on the store, so several runners sharing one
    store stop independently of each other.
    """
    seen = {"n": 0}
    real = runner._check_update_signal

    def counted():
        seen["n"] += 1
        if seen["n"] > polls:
            runner.request_stop()
        real()

    monkeypatch.setattr(runner, "_check_update_signal", counted)
    return seen


async def _run(runner: WorkerRunner) -> None:
    """Run a worker's real loop, failing loudly instead of hanging."""
    await asyncio.wait_for(runner.run(), timeout=10)


async def _yield_until(ready, turns: int = 50) -> bool:
    """Yield to the event loop until `ready()` holds; report whether it did.

    Cooperative only — no wall-clock sleep, so nothing here gets slower or
    flakier on a busy machine, and `turns` is a generous ceiling rather than a
    tuned number. Returns a bool instead of raising because the callers run
    INSIDE the system under test, which would swallow an exception into a
    failed job and hide the real assertion.
    """
    for _ in range(turns):
        if ready():
            return True
        await asyncio.sleep(0)
    return ready()


def _rip_box(cfg: Path, store: ChaosStore, monkeypatch):
    """The serve box's JobManager, with its outbound pushes captured."""
    monkeypatch.setattr(socket, "gethostname", lambda: RIP_BOX)
    manager = JobManager(load_settings(cfg), store=store)
    alerts: list[tuple] = []
    monkeypatch.setattr(manager, "_notify",
                        lambda title, message, priority=None, job_id=None:
                        alerts.append((title, message, priority)))
    return manager, alerts


def _webjob(manager: JobManager, job_id: int, state: JobState, titles) -> WebJob:
    """A job on serve that is past both gates — named, ripped, in flight."""
    disc = DiscInfo(source="disc:0", disc_name="ALIEN", titles=[
        Title(title_id=t[0], duration_seconds=6900, chapter_count=12,
              source_filename=f"0000{t[0]}.mpls") for t in titles])
    wj = WebJob(id=job_id, source="disc:0", state=state)
    wj.disc = disc
    wj.selected_title_ids = [t[0] for t in titles]
    wj.assignments = {t[0]: _movie(*t) for t in titles}
    job = Job(source="disc:0", state=state)
    job.disc = disc
    job.selected_title_ids = list(wj.selected_title_ids)
    job.assignments = dict(wj.assignments)
    wj._pipeline_job = job
    manager.jobs[job_id] = wj
    return wj


def _record(store: ChaosStore, job_id: int) -> JobRecord:
    return {r.id: r for r in store.load_all()}[job_id]


def _serve_side(manager: JobManager, store: ChaosStore, job_id: int) -> WebJob:
    """The serve box's view of a job that is already in the store — restored
    through the manager's own deserialization, so it carries the real
    checkpoints rather than a hand-built approximation."""
    wj = manager._from_record(_record(store, job_id))
    manager.jobs[job_id] = wj
    return wj


async def _refresh_until(manager: JobManager, ready, timeout=10.0) -> None:
    """Run serve's REAL store-refresh loop until `ready()` holds, then stop it.

    This one does have to wait on the clock: the refresh loop hands its store
    reads to ``asyncio.to_thread``, so plain event-loop yields can't make them
    land. It polls a condition with a generous ceiling rather than sleeping a
    fixed amount, so a slow machine takes longer and still passes.
    """
    manager.closing = False
    task = asyncio.ensure_future(manager._refresh_from_store())
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(0.005)
            if ready():
                return
        raise AssertionError("serve's refresh loop never reached the expected state")
    finally:
        manager.closing = True
        await asyncio.sleep(0)
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# --- a box dies mid-encode -------------------------------------------------

async def test_a_box_that_dies_mid_encode_is_reclaimed_only_once_its_claim_expires(
        tmp_path, monkeypatch):
    """The rack loses power four hours into a two-title job: one title is already
    in the library, the second was encoding. No release, no final save — the row
    just stops moving while still saying "encoding".

    Two things have to hold. Until the claim expires nobody may touch it, because
    a live 4K encode is regularly quiet for minutes and stealing it would run the
    same job twice. Once it has expired the next box picks it up and continues
    from the checkpoints: the finished title is left exactly as it was placed and
    only the unfinished one is encoded.
    """
    cfg = _config_dir(tmp_path, monkeypatch, stale_after_seconds=180)
    store = ChaosStore()
    _stub_the_builds(monkeypatch)
    encoded: list[str] = []
    _stub_the_tools(monkeypatch, encoded)
    _queue(store, cfg, 317, [(0, "Alien", 1979), (1, "Aliens", 1986)], placed=(0,))

    already_placed = _library_path(cfg, _movie(0, "Alien", 1979))
    already_placed.parent.mkdir(parents=True, exist_ok=True)
    already_placed.write_text("placed by encoder-A before the lights went out")

    # encoder-A claims it, reports progress on the second title, and dies.
    claimed = store.claim_next("encoder-A", CLAIMABLE_STATES, 180)
    assert claimed.id == 317
    job = job_from_record(claimed)          # what the worker itself builds
    job.state = JobState.ENCODING
    reporter = WorkerReporter(store, "encoder-A", 317, job)
    reporter.on_progress("encoding title 1", 0.4)
    reporter.touch()
    store.cut_off("encoder-A")

    # 170 seconds of silence, inside this cluster's 180s window. An idle box's
    # REAL loop polls right then — the row is in a claimable state, so nothing
    # but the unexpired claim can stop it walking off with a live 4K encode.
    store.tick(170)
    assert _record(store, 317).state == "encoding"
    assert "encoding" in CLAIMABLE_STATES  # the state itself is not what blocks C
    box_c = _worker(cfg, store, "encoder-C")
    _stop_after(box_c, monkeypatch, polls=2)
    await _run(box_c)

    assert encoded == []                    # encoder-C started nothing
    assert store.owner_of(317) == "encoder-A"
    assert _record(store, 317).worker == "encoder-A"

    # Past the window the claim has expired, and encoder-B's real loop takes over.
    store.tick(30)
    box_b = _worker(cfg, store, "encoder-B")
    _stop_after(box_b, monkeypatch, polls=1)
    await _run(box_b)

    # Exactly two boxes ever held it, in that order — encoder-C never got in.
    assert store.claim_log == [("encoder-A", 317), ("encoder-B", 317)]
    assert encoded == ["Aliens (1986).mkv"]  # the placed title was NOT re-encoded
    assert already_placed.read_text() == "placed by encoder-A before the lights went out"
    finished = _record(store, 317)
    assert finished.state == "done"
    assert finished.worker == "encoder-B"
    assert sorted(Path(p).name for p in finished.outputs) == [
        "Alien (1979).mkv", "Aliens (1986).mkv"]
    assert store.owner_of(317) is None  # released, so a retry is claimable at once


async def test_the_half_written_file_a_dead_box_left_in_staging_never_reaches_the_library(
        tmp_path, monkeypatch):
    """A box killed mid-encode leaves a truncated output in its staging dir under
    the exact filename the library expects. The reclaiming box must re-encode
    over it rather than publish it: a short, unplayable file that Plex happily
    lists is the worst possible outcome, because the job reports done."""
    cfg = _config_dir(tmp_path, monkeypatch)
    store = ChaosStore()
    _stub_the_builds(monkeypatch)
    encoded: list[str] = []
    _stub_the_tools(monkeypatch, encoded)
    _queue(store, cfg, 318, [(0, "Blade Runner", 1982)])

    settings = load_settings(cfg)
    staging_root = settings.storage.staging_dir / "job_318"
    partial = staging_root / "encode" / "Blade Runner (1982).mkv"
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_text("half a movie, then the power went")

    box_b = _worker(cfg, store, "encoder-B")
    _stop_after(box_b, monkeypatch, polls=1)
    await _run(box_b)

    placed = _library_path(cfg, _movie(0, "Blade Runner", 1982))
    assert encoded == ["Blade Runner (1982).mkv"]
    assert placed.read_text() == "h265 bytes for Blade Runner (1982)"
    assert not staging_root.exists()  # the whole intermediate tree is swept
    assert _record(store, 318).outputs == [str(placed)]


async def test_a_job_abandoned_between_the_encode_and_the_library_is_finished_not_redone(
        tmp_path, monkeypatch):
    """The box died in the gap between placing the file and the row saying done,
    so it is stuck on "placing" holding a claim nobody will ever release. That
    state has to be claimable — otherwise the job sits there forever — and the
    box that takes it must recognise the title as already placed and encode
    nothing, or it re-encodes hours of video over a perfectly good file."""
    cfg = _config_dir(tmp_path, monkeypatch)
    store = ChaosStore()
    _stub_the_builds(monkeypatch)
    encoded: list[str] = []
    _stub_the_tools(monkeypatch, encoded)
    _queue(store, cfg, 319, [(0, "Solaris", 1972)], state="placing", placed=(0,))

    placed = _library_path(cfg, _movie(0, "Solaris", 1972))
    placed.parent.mkdir(parents=True, exist_ok=True)
    placed.write_text("the finished encode, already in the library")

    assert "placing" in CLAIMABLE_STATES
    box_b = _worker(cfg, store, "encoder-B")
    _stop_after(box_b, monkeypatch, polls=1)
    await _run(box_b)

    assert encoded == []  # nothing re-encoded
    assert placed.read_text() == "the finished encode, already in the library"
    finished = _record(store, 319)
    assert finished.state == "done"
    assert finished.outputs == [str(placed)]
    assert store.owner_of(319) is None


# --- two boxes racing ------------------------------------------------------

async def test_two_boxes_polling_at_the_same_moment_encode_the_output_path_once(
        tmp_path, monkeypatch):
    """The dangerous moment is the second box polling WHILE the first is mid
    encode: the row is in a claimable state, its worker is busy rather than
    chatty, and both boxes would write the same path in the library.

    A feature-length encode also runs far longer than the stale window — ten
    minutes pass here — so the only thing standing between the two boxes is the
    heartbeat the encoding one keeps sending. The second box polls at exactly
    that moment, and again after the first has finished: the finished row must
    not be claimable either, or the queue would encode every job twice over.
    """
    cfg = _config_dir(tmp_path, monkeypatch, stale_after_seconds=180)
    store = ChaosStore()
    _stub_the_builds(monkeypatch)
    encoded: list[str] = []
    seen_mid_encode: dict = {}

    box_b = _worker(cfg, store, "encoder-B")
    _stop_after(box_b, monkeypatch, polls=2)  # two polls, both while A is encoding

    async def poll_the_other_box(_name):
        # Ten minutes into the encode — well past the 180s stale window — with
        # encoder-A's heartbeat the only thing holding the claim open.
        store.tick(600)
        # Give A's heartbeat task a chance to land — then look, whether it did
        # or not, so a heartbeat that never fires shows up as the 600s of
        # silence it really is instead of a hang.
        await _yield_until(lambda: store._beat.get(700) == store.clock)
        seen_mid_encode["state"] = _record(store, 700).state
        seen_mid_encode["owner"] = store.owner_of(700)
        seen_mid_encode["silence"] = store.clock - store._beat[700]
        # encoder-B's REAL loop runs while encoder-A holds the claim.
        await _run(box_b)

    _stub_the_tools(monkeypatch, encoded, on_encode=poll_the_other_box)
    _queue(store, cfg, 700, [(0, "Dune", 1984)])

    box_a = _worker(cfg, store, "encoder-A")
    _stop_after(box_a, monkeypatch, polls=1)
    await _run(box_a)

    # B polled while the row was claimable and owned — and came away with nothing,
    # because A's heartbeat had kept the claim fresh through those ten minutes.
    assert seen_mid_encode["state"] == "encoding"
    assert seen_mid_encode["state"] in CLAIMABLE_STATES
    assert seen_mid_encode["owner"] == "encoder-A"
    assert seen_mid_encode["silence"] < 180
    assert store.claim_log == [("encoder-A", 700)]  # B never got a look-in
    assert encoded == ["Dune (1984).mkv"]           # ...so one encode, not two

    # A finished and dropped its claim; the row is done, so the next box to poll
    # must not pick it up again.
    assert store.owner_of(700) is None
    assert _record(store, 700).state == "done"
    assert store.claim_next("encoder-C", CLAIMABLE_STATES, 180) is None
    assert _library_path(cfg, _movie(0, "Dune", 1984)).read_text() == (
        "h265 bytes for Dune (1984)")


# --- serve requeueing under a live worker ----------------------------------

async def test_serve_refuses_to_requeue_a_job_a_live_worker_is_still_holding(
        tmp_path, monkeypatch):
    """Requeue is the operator's recovery button, and it releases the claim — so
    aiming it at a live encode starts a second one on the same output path. It
    is allowed only when nothing is working the job: while the holder is still
    heart-beating the call is refused and the claim is left exactly as it was.

    Once that box goes quiet past the stale window the same call is allowed, and
    what it produces has to be genuinely claimable work — so a second box picks
    it up here and finishes it.
    """
    cfg = _config_dir(tmp_path, monkeypatch, stale_after_seconds=60)
    store = ChaosStore()
    _stub_the_builds(monkeypatch)
    encoded: list[str] = []
    _stub_the_tools(monkeypatch, encoded)
    _queue(store, cfg, 401, [(0, "Stalker", 1979)], state="encoding")

    manager, _alerts = _rip_box(cfg, store, monkeypatch)
    wj = _serve_side(manager, store, 401)

    prepped: list = []

    async def fake_prep(job, staging_root, report, include_subs=True):
        prepped.append(job)  # the rip box's prep; the rip itself is already done

    monkeypatch.setattr(manager.pipeline, "prep_phase", fake_prep)
    monkeypatch.setattr(manager.pipeline, "_persist_mapping", lambda job, report: None)

    # encoder-A holds it and is reporting in.
    assert store.claim_next("encoder-A", CLAIMABLE_STATES, 60).id == 401
    live = _worker(cfg, store, "encoder-A")
    live._version = BUILD
    live._register(401, "encoding title 0")
    manager._workers_cache = store.list_workers()

    assert manager._is_stalled(wj, manager._live_state(wj)) is False
    with pytest.raises(ValueError) as refused:
        manager.requeue(401)
    assert "401" in str(refused.value) and "stalled" in str(refused.value)
    assert manager.requeue_stalled() == 0
    assert store.owner_of(401) == "encoder-A"      # the claim was NOT dropped
    assert store.claim_next("encoder-B", CLAIMABLE_STATES, 60) is None
    assert getattr(wj, "_task", None) is None      # and serve started nothing
    assert prepped == []

    # encoder-A goes dark. 90s is past this cluster's 60s window.
    store.cut_off("encoder-A")
    store.tick(90)
    manager._workers_cache = store.list_workers()
    assert manager._is_stalled(wj, manager._live_state(wj)) is True

    manager.requeue(401)
    try:
        await asyncio.wait_for(wj._task, timeout=10)
    finally:
        await manager.shutdown()

    assert store.owner_of(401) is None             # the dead claim was dropped
    assert _record(store, 401).state == "ready"
    assert len(prepped) == 1
    # Resumed, not restarted: the requeued job still carries the finished rip.
    assert prepped[0].checkpoints[0]["ripped_path"] == "job_401/rip/t000/title_t00.mkv"

    box_b = _worker(cfg, store, "encoder-B")
    _stop_after(box_b, monkeypatch, polls=1)
    await _run(box_b)
    assert encoded == ["Stalker (1979).mkv"]
    assert _record(store, 401).state == "done"


# --- a box that claims and finds nothing to encode -------------------------

async def test_a_box_whose_rip_file_vanished_hands_the_job_back_untouched(
        tmp_path, monkeypatch, capsys):
    """The NAS dropped between the rip finishing and the claim landing, so the
    checkpoint names a file that is no longer there. Failing the job would be
    wrong — nothing is broken but the mount — and holding it would deadlock it,
    since serve treats a READY job as the workers' problem. It has to go back on
    the queue exactly as it was, with no error written, and be picked up when the
    share returns.
    """
    cfg = _config_dir(tmp_path, monkeypatch)
    store = ChaosStore()
    _stub_the_builds(monkeypatch)
    encoded: list[str] = []
    _stub_the_tools(monkeypatch, encoded)
    ripped = _queue(store, cfg, 334, [(0, "Nostalghia", 1983)])
    ripped[0].unlink()  # the share went away

    box_a = _worker(cfg, store, "encoder-A")
    # Two trips round the loop: the second proves the hand-back really put the
    # job back on the queue (this box can claim it again) rather than parking it.
    _stop_after(box_a, monkeypatch, polls=2)
    await _run(box_a)

    assert encoded == []
    # It genuinely claimed the job and gave it back — not "never saw it", which
    # would satisfy the state assertions below for entirely the wrong reason.
    assert set(store.claim_log) == {("encoder-A", 334)}
    assert store.claim_log.count(("encoder-A", 334)) >= 2  # re-claimable, not parked
    handed_back = _record(store, 334)
    assert handed_back.state == "ready"
    assert handed_back.error is None
    assert handed_back.worker is None
    assert store.owner_of(334) is None
    assert "Job 334 claimed but not fully ripped yet" in capsys.readouterr().out

    # The share comes back. Nothing else changes — same store, same row.
    ripped[0].parent.mkdir(parents=True, exist_ok=True)
    ripped[0].write_text("the rip, back on the NAS")
    box_b = _worker(cfg, store, "encoder-B")
    _stop_after(box_b, monkeypatch, polls=1)
    await _run(box_b)

    assert encoded == ["Nostalghia (1983).mkv"]
    assert _record(store, 334).state == "done"


# --- serve restarting -------------------------------------------------------

async def test_serve_restarting_mid_encode_leaves_the_job_with_the_box_encoding_it(
        tmp_path, monkeypatch):
    """serve comes back up and reads every persisted row, including one that says
    "encoding" — which is a worker's job, not serve's. Resuming it locally would
    put a second encode on the same output path while the worker is still going.
    It must be loaded for display only: no local task, no release of the claim,
    and the worker's progress still lands on it afterwards.
    """
    cfg = _config_dir(tmp_path, monkeypatch)
    store = ChaosStore()
    _stub_the_builds(monkeypatch)
    _queue(store, cfg, 500, [(0, "Arrival", 2016)], state="encoding")
    _queue(store, cfg, 501, [(0, "Sicario", 2015)], state="ripping")

    claimed = store.claim_next("encoder-A", CLAIMABLE_STATES, 180)
    assert claimed.id == 500
    live = _worker(cfg, store, "encoder-A")
    live._version = BUILD
    live._register(500, "encoding title 0")

    manager, _alerts = _rip_box(cfg, store, monkeypatch)
    prepped: list[int] = []

    async def fake_prep(job, staging_root, report, include_subs=True):
        prepped.append(int(str(staging_root.name).removeprefix("job_")))

    monkeypatch.setattr(manager.pipeline, "prep_phase", fake_prep)
    monkeypatch.setattr(manager.pipeline, "encode_phase", fake_prep)
    monkeypatch.setattr(manager.pipeline, "_persist_mapping", lambda job, report: None)

    resumed = manager.recover()
    # Wait on the one resumption serve did start, instead of guessing at a number
    # of event-loop turns. Job 500 must never get a task to wait on at all — that
    # is what the assertions below check.
    await asyncio.wait_for(manager.jobs[501]._task, timeout=10)

    assert resumed == 1                      # only the rip serve owns itself
    assert prepped == [501]                  # the encode was never touched here
    assert manager.jobs[500].state is JobState.ENCODING
    assert getattr(manager.jobs[500], "_task", None) is None
    assert store.owner_of(500) == "encoder-A"

    # The worker is still alive and finishes; serve's refresh mirrors it back.
    job = job_from_record(claimed)
    job.state = JobState.ENCODING
    reporter = WorkerReporter(store, "encoder-A", 500, job)
    reporter.on_progress("encoding title 0", 0.9)
    reporter.finish(JobState.DONE, [str(_library_path(cfg, _movie(0, "Arrival", 2016)))])
    try:
        await _refresh_until(manager, lambda: manager.jobs[500].state is JobState.DONE)
    finally:
        await manager.shutdown()
    assert manager.jobs[500].progress == 0.9


async def test_serve_restarting_mid_rip_resumes_from_the_titles_already_ripped(
        tmp_path, monkeypatch):
    """serve was killed halfway through a two-title rip. Recovery re-runs the rip
    on this box — it is the only box with the drive — but the checkpoint for the
    title that already landed has to survive the round trip through the store, or
    the disc gets read from the start again and the job takes twice as long (and
    on a marginal disc, may not survive the second read at all).
    """
    cfg = _config_dir(tmp_path, monkeypatch)
    store = ChaosStore()
    _stub_the_builds(monkeypatch)
    store.save(JobRecord(
        id=502, source="disc:0", state="ripping", selected_title_ids=[0, 1],
        assignments=[assignment_to_dict(_movie(0, "Ran", 1985)),
                     assignment_to_dict(_movie(1, "Kagemusha", 1980))],
        checkpoints={"0": {"ripped_path": "job_502/rip/t000/title_t00.mkv"}},
        log=["[2026-08-24 11:00:00] Ripping title 1…"]))

    manager, _alerts = _rip_box(cfg, store, monkeypatch)
    prepped: list = []

    async def fake_prep(job, staging_root, report, include_subs=True):
        prepped.append(job)

    monkeypatch.setattr(manager.pipeline, "prep_phase", fake_prep)
    monkeypatch.setattr(manager.pipeline, "_persist_mapping", lambda job, report: None)

    resumed = manager.recover()
    try:
        await asyncio.wait_for(manager.jobs[502]._task, timeout=10)
    finally:
        await manager.shutdown()

    assert resumed == 1
    assert len(prepped) == 1
    job = prepped[0]
    assert job.checkpoints[0]["ripped_path"] == "job_502/rip/t000/title_t00.mkv"
    assert 1 not in job.checkpoints                      # the unfinished one re-rips
    assert job.selected_title_ids == [0, 1]
    assert job.assignments[1].show_or_movie_title == "Kagemusha"
    assert any("Recovered after restart — resuming." in line
               for line in manager.jobs[502].log)
    # Back on the queue for a worker, with the rip it already had.
    assert _record(store, 502).state == "ready"
    assert _record(store, 502).checkpoints == {
        "0": {"ripped_path": "job_502/rip/t000/title_t00.mkv"}}


# --- clocks -----------------------------------------------------------------

def test_liveness_is_measured_by_the_database_clock_not_by_either_box(monkeypatch):
    """Every "is that box still alive?" decision compares two timestamps, and the
    two boxes involved do not share a clock — a Windows encoder that has drifted
    ten minutes from the Linux rip box is ordinary. If either side stamped or
    compared with its OWN clock, drift would either freeze a live encode out of
    its claim (two encodes, one path) or make a dead box look alive forever.

    So no box ever sends a time: the writes stamp ``now()`` inside Postgres, the
    reads compute the age inside Postgres, and the only number crossing the wire
    is the configured window in seconds. Serve then compares the age the database
    handed it, which is the second half of this test.
    """
    sql: list[tuple] = []

    class _Cursor:
        def __init__(self, rows):
            self._rows = rows

        def execute(self, statement, params=None):
            sql.append((" ".join(statement.split()), params))

        def fetchone(self):
            return self._rows[0] if self._rows else None

        def fetchall(self):
            return list(self._rows)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _Conn:
        def __init__(self, rows):
            self._rows = rows

        def cursor(self):
            return _Cursor(self._rows)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _store(rows=()):
        store = PostgresStore("postgresql://cluster.example/brr")
        monkeypatch.setattr(store, "_connect", lambda: _Conn(list(rows)))
        return store

    _store().record_worker("encoder-A", BUILD, 317, "encoding", "run-1", "192.0.2.9")
    _store().heartbeat(317, "encoder-A")
    _store().claim_next("encoder-B", ["ready"], 180)
    # The two numeric columns are what the SELECT below computes in SQL; the
    # address is stored as the worker reported it.
    seen = _store([("encoder-A", BUILD, 317, "encoding", 3.5, 7200.0, "192.0.2.9")]
                  ).list_workers()

    # Serve gets the database's numbers back unaltered — it must never re-derive
    # an age from its own clock on the way out, or the SQL above buys nothing.
    assert [(w.worker_id, w.job_id, w.seconds_since_seen, w.uptime_seconds, w.address)
            for w in seen] == [("encoder-A", 317, 3.5, 7200.0, "192.0.2.9")]

    presence_write, beat, claim = sql[0], sql[1], sql[2]
    ages = sql[-1][0]
    assert "VALUES (%s, %s, %s, %s, now(), %s, %s)" in presence_write[0]  # first sighting
    assert "last_seen = now()" in presence_write[0]                       # every one after
    assert presence_write[1] == ("encoder-A", BUILD, 317, "encoding", "run-1", "192.0.2.9")
    assert "SET heartbeat = now()" in beat[0] and beat[1] == (317, "encoder-A")
    # Staleness: both sides of the comparison are the database's own clock, and
    # the only thing the caller supplies is how many seconds count as stale.
    assert "heartbeat < now() - make_interval(secs => %s)" in claim[0]
    assert claim[1] == ("encoder-B", ["ready"], 180)
    assert "EXTRACT(EPOCH FROM (now() - last_seen))" in ages
    assert "EXTRACT(EPOCH FROM (now() - started_at))" in ages
    for _statement, params in sql:
        for value in (params or ()):
            assert not isinstance(value, float), "a box must never send its own clock"


async def test_the_stall_verdict_follows_the_stores_clock_not_serves_own(
        tmp_path, monkeypatch):
    """The consequence of the above, on the box that acts on it. Serve's verdict
    on a remote encode moves only when the DATABASE clock moves past the window
    — and moves back the instant the box reports in again, so a box that was
    briefly unreachable is not left flagged for requeue."""
    cfg = _config_dir(tmp_path, monkeypatch, stale_after_seconds=120)
    store = ChaosStore()
    _stub_the_builds(monkeypatch)
    manager, _alerts = _rip_box(cfg, store, monkeypatch)
    wj = _webjob(manager, 800, JobState.ENCODING, [(0, "Ikiru", 1952)])

    box = _worker(cfg, store, "encoder-A")
    box._version = BUILD
    box._register(800, "encoding title 0")

    manager._workers_cache = store.list_workers()
    assert [w.worker_id for w in manager._workers_cache] == ["encoder-A"]
    assert manager._workers_cache[0].seconds_since_seen == 0.0
    assert manager._is_stalled(wj, manager._live_state(wj)) is False

    store.cut_off("encoder-A")
    store.tick(119)                       # still inside the window
    manager._workers_cache = store.list_workers()
    assert manager._is_stalled(wj, manager._live_state(wj)) is False

    store.tick(2)                         # ...and now past it
    manager._workers_cache = store.list_workers()
    assert manager._is_stalled(wj, manager._live_state(wj)) is True

    # The box was only unreachable, not dead: one heartbeat clears the verdict
    # even though the database clock has moved on by two minutes.
    store.comes_back("encoder-A")
    box._register(800, "encoding title 0")
    manager._workers_cache = store.list_workers()
    assert manager._is_stalled(wj, manager._live_state(wj)) is False
    # ...and the verdict is what actually guards Requeue, so the button is shut
    # again too, with a reason an operator can act on.
    with pytest.raises(ValueError) as refused:
        manager.requeue(800)
    assert "800" in str(refused.value) and "stalled" in str(refused.value)
    assert getattr(wj, "_task", None) is None   # nothing was started behind it


async def test_a_box_reporting_a_different_job_does_not_vouch_for_this_one(
        tmp_path, monkeypatch):
    """A box only vouches for the job its presence row names. It moved on to job
    801 long ago; 800 is the one it dropped when it restarted, and nothing is
    working that any more."""
    cfg = _config_dir(tmp_path, monkeypatch, stale_after_seconds=120)
    store = ChaosStore()
    _stub_the_builds(monkeypatch)
    manager, _alerts = _rip_box(cfg, store, monkeypatch)
    dropped = _webjob(manager, 800, JobState.ENCODING, [(0, "Ikiru", 1952)])
    current = _webjob(manager, 801, JobState.ENCODING, [(0, "Ran", 1985)])

    box = _worker(cfg, store, "encoder-A")
    box._version = BUILD
    box._register(801, "encoding title 0")
    manager._workers_cache = store.list_workers()

    assert manager._is_stalled(current, manager._live_state(current)) is False
    assert manager._is_stalled(dropped, manager._live_state(dropped)) is True


# --- a box on last week's build ---------------------------------------------

async def test_a_box_on_an_older_build_finishes_its_job_then_stops_claiming(
        tmp_path, monkeypatch):
    """serve gets updated while an encoder is hours into a job. The encoder
    follows serve's build, but the update is drain-first: the job in flight is
    finished and placed, and only then does the box stop claiming. It must also
    leave nothing behind — no half-done row, no claim on the job it never
    started — so the box still on the current build empties the queue.
    """
    cfg = _config_dir(tmp_path, monkeypatch, worker_auto_update=True)
    store = ChaosStore()
    _stub_the_builds(monkeypatch, worker_build=BUILD, serve_build=NEWER_BUILD)
    encoded: list[str] = []
    _queue(store, cfg, 600, [(0, "Brazil", 1985)])
    _queue(store, cfg, 601, [(0, "Gattaca", 1997)])

    manager, _alerts = _rip_box(cfg, store, monkeypatch)
    manager._record_serve_presence()  # serve publishes the build it is now on
    # The worker finds serve by the reserved id, not by hostname. Pinning it here
    # means a regression in how serve registers fails with "serve isn't in the
    # presence table" rather than an inscrutable "the box never updated".
    assert [w.worker_id for w in store.list_workers()
            if presence.is_serve(w.worker_id)] == [presence.serve_id(RIP_BOX)]

    old_box = _worker(cfg, store, "encoder-A")

    async def serve_gets_updated_mid_encode(_name):
        # By the time this job finishes the box has been up for hours, so the
        # auto-update age guard no longer holds it back.
        old_box._started_monotonic -= 4 * 3600

    _stub_the_tools(monkeypatch, encoded, on_encode=serve_gets_updated_mid_encode)
    _stop_after(old_box, monkeypatch, polls=6)  # a backstop, not the exit path
    await _run(old_box)

    assert encoded == ["Brazil (1985).mkv"]     # the job in flight was finished
    assert _record(store, 600).state == "done"
    assert store.owner_of(600) is None
    assert old_box.update_requested is True     # ...and only then: drain + pull
    assert old_box.restart_requested is True
    assert _record(store, 601).state == "ready"  # never started
    assert store.owner_of(601) is None           # and not stranded under a claim

    new_box = _worker(cfg, store, "encoder-B")
    _stop_after(new_box, monkeypatch, polls=1)
    await _run(new_box)
    assert encoded == ["Brazil (1985).mkv", "Gattaca (1997).mkv"]
    assert _record(store, 601).worker == "encoder-B"
