"""Updating and restarting the encode boxes without losing an hour of encoding.

The cluster is two headless encode boxes and the rip box that feeds them. Every
maintenance action an operator takes -- drain one, update all of them, reboot a
wedged one -- lands on a machine that may be four hours into a 4K encode. The
whole design of the worker loop is built around that: signals are read BETWEEN
jobs, never during one, so "restart the workers" can never mean "throw away
what they were doing".

The other half is knowing what is out there. Boxes report into the shared
workers table; the rip box watches them and says so when one goes quiet -- once,
not every poll -- and the work a dead box was holding has to be recoverable by
the box that is still up. And the age guards exist for a specific past failure:
a worker that updates the moment it starts drains, pulls, restarts, and if the
pull doesn't actually move it forward it does the whole thing again, forever,
encoding nothing.

Only the external tools are stubbed here (HandBrakeCLI, mkvmerge) plus the
shared Postgres, which is replaced by an in-memory table on a clock the test
advances by hand. The worker loop, the signal protocol, the presence watch, the
manager's worker actions and the panel it renders are all the real thing.
"""

import asyncio
import contextlib
import socket
from pathlib import Path

import pytest

from blurayripper import presence
from blurayripper.config import load_settings
from blurayripper.models import EpisodeAssignment, MediaKind
from blurayripper.persistence import (
    ALL_WORKERS, JobRecord, MemoryStore, WorkerInfo, assignment_to_dict, signal_key,
)
from blurayripper.tools.handbrake import HandBrake
from blurayripper.tools.mkvtools import MkvTools
from blurayripper.web.manager import JobManager
from blurayripper.worker import CLAIMABLE_STATES, WorkerRunner

RIP_BOX = "RIPBOX"
BUILD = "0.1.0+240.aaaaaaa"          # what every box in the story is running
NEWER_BUILD = "0.1.0+250.bbbbbbb"    # ...and what the rip box gets pulled up to


# --- the shared Postgres, in memory ---------------------------------------

class ClusterStore(MemoryStore):
    """The shared store every box coordinates through.

    Job rows, claims and cluster signals come from MemoryStore unchanged; what
    is added here is the presence table (Postgres ages it with the DATABASE
    clock, so it is modelled with one clock the test moves) and the ability to
    make a box go dark -- a powered-off encoder does not tell anyone, its row
    simply stops advancing.
    """

    durable = True

    def __init__(self) -> None:
        super().__init__()
        self.clock = 1_000.0
        self._presence: dict[str, dict] = {}
        self._dark: set[str] = set()

    def tick(self, seconds: float) -> None:
        self.clock += seconds

    def go_dark(self, box_id: str) -> None:
        """That box's writes stop landing (power cut / lost network)."""
        self._dark.add(box_id)

    def come_back(self, box_id: str) -> None:
        self._dark.discard(box_id)

    def record_worker(self, worker_id, version, job_id, stage, run_token=None, address=None) -> None:
        if worker_id in self._dark:
            return
        row = self._presence.get(worker_id)
        if row is None or row["run_token"] != run_token:
            row = {"started_at": self.clock, "run_token": run_token}
            self._presence[worker_id] = row
        row.update(version=version, job_id=job_id, stage=stage, last_seen=self.clock)

    def list_workers(self) -> list[WorkerInfo]:
        for box_id, row in list(self._presence.items()):
            if self.clock - row["last_seen"] > 3600:
                del self._presence[box_id]          # long gone: the row is dropped
        return [
            WorkerInfo(box_id, row["version"], row["job_id"], row["stage"],
                       self.clock - row["last_seen"], self.clock - row["started_at"])
            for box_id, row in sorted(self._presence.items())
            if self.clock - row["last_seen"] <= 600
        ]

    def save(self, record: JobRecord) -> None:
        if record.worker in self._dark:
            return                                   # a dark box's progress never lands
        super().save(record)

    def heartbeat(self, job_id: int, worker_id: str) -> None:
        if worker_id in self._dark:
            return
        super().heartbeat(job_id, worker_id)


# --- fixtures the whole cluster shares -------------------------------------

def _config_dir(tmp_path: Path, **cluster) -> Path:
    """Write the settings every box in this story loads.

    It has to be a real config dir: a worker re-reads its settings from disk
    between jobs (that is how a central override reaches it), so anything only
    poked onto the Settings object would be thrown away on the first poll.
    """
    cfg = dict(use_workers=True, worker_poll_seconds=0, heartbeat_seconds=0,
               stale_after_seconds=180, offline_alert_seconds=300,
               worker_restart_after_days=0, worker_auto_update=False)
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


def _stub_the_tools(monkeypatch, on_encode=None, timeline=None):
    """Replace the two binaries encode_phase shells out to.

    Patched on the CLASS, not an instance: a worker rebuilds its Pipeline
    whenever it re-reads settings, so an instance-level stub would evaporate
    between jobs. ``on_encode`` is what the world does WHILE a title is
    encoding, which is where every maintenance click in this story happens.
    """
    log = timeline.append if timeline is not None else (lambda _: None)

    async def video_height(self, path):
        return 1080

    async def encode(self, src, dst, *, on_progress=None, tune_grain=None,
                     deinterlace=False):
        film = Path(dst).stem
        log(f"started encoding {film}")
        if on_encode is not None:
            await on_encode(film)
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_text(f"h265 bytes for {film}")
        log(f"finished encoding {film}")
        return dst

    monkeypatch.setattr(MkvTools, "video_height", video_height)
    monkeypatch.setattr(HandBrake, "encode", encode)


def _stub_the_version(monkeypatch, build=BUILD):
    """No git subprocess in a test; every box reports the same build."""
    import blurayripper.version as version_mod
    import blurayripper.web.manager as manager_mod
    monkeypatch.setattr(version_mod, "get_version", lambda: build)
    monkeypatch.setattr(manager_mod, "get_version", lambda: build)


def _queue(store: ClusterStore, cfg: Path, job_id: int, title: str, year: int) -> Path:
    """Put a ripped, named job on the queue exactly as the rip box leaves it:
    state READY, the rip already on the shared staging share."""
    settings = load_settings(cfg)
    ripped = settings.storage.staging_dir / f"job_{job_id}" / "rip" / "t000" / "title_t00.mkv"
    ripped.parent.mkdir(parents=True, exist_ok=True)
    ripped.write_text("a whole Blu-ray, honest")
    store.save(JobRecord(
        id=job_id, source="disc:0", state="ready", selected_title_ids=[0],
        assignments=[assignment_to_dict(
            EpisodeAssignment(0, MediaKind.MOVIE, title, year))],
        checkpoints={"0": {"ripped_path": f"job_{job_id}/rip/t000/title_t00.mkv"}},
    ))
    return ripped


def _worker(cfg: Path, store: ClusterStore, name: str) -> WorkerRunner:
    runner = WorkerRunner(load_settings(cfg), worker_id=name)
    runner.store = store
    return runner


def _rip_box(cfg: Path, store: ClusterStore, monkeypatch):
    """The serve box's JobManager, with the pushes it would send captured."""
    monkeypatch.setattr(socket, "gethostname", lambda: RIP_BOX)
    manager = JobManager(load_settings(cfg), store=store)
    alerts: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(manager, "_notify",
                        lambda title, message, priority=None:
                        alerts.append((title, message, priority)))
    return manager, alerts


def _serve_poll(manager: JobManager, store: ClusterStore) -> None:
    """One tick of the rip box's store-refresh loop: prove this box is alive,
    re-read the workers table, and alert on anything that changed."""
    manager._record_serve_presence()
    manager._workers_cache = store.list_workers()
    manager._check_worker_presence()


async def _spin(turns: int = 150) -> None:
    """Let the running worker loops make progress (poll interval is 0, so a
    turn of the event loop is a full poll). No wall-clock waiting."""
    for _ in range(turns):
        await asyncio.sleep(0)


async def _finish(task: asyncio.Task) -> None:
    await asyncio.wait_for(task, timeout=10)


def _record(store: ClusterStore, job_id: int) -> JobRecord:
    return {r.id: r for r in store.load_all()}[job_id]


def _library(cfg: Path) -> list[str]:
    root = load_settings(cfg).storage.movies_root
    return sorted(p.name for p in root.rglob("*") if p.is_file())


# --- the story --------------------------------------------------------------

async def test_both_encode_boxes_report_in_and_the_rip_box_stays_out_of_the_panel(
        tmp_path, monkeypatch):
    """Before you can drain a box you have to see it. Both encoders register
    themselves while idle; the rip box heartbeats into the SAME table (that is
    how a worker notices the server dying) and must be filtered back out, or the
    panel offers you a Drain button for the machine you are clicking on."""
    _stub_the_version(monkeypatch)
    _stub_the_tools(monkeypatch)
    cfg = _config_dir(tmp_path)
    store = ClusterStore()
    manager, _ = _rip_box(cfg, store, monkeypatch)
    box_a, box_b = _worker(cfg, store, "encoder-A"), _worker(cfg, store, "encoder-B")

    task_a = asyncio.create_task(box_a.run())
    task_b = asyncio.create_task(box_b.run())
    await _spin()
    _serve_poll(manager, store)

    panel = manager.snapshot()["workers"]
    assert [w["id"] for w in panel] == ["encoder-A", "encoder-B"]
    assert [w["stage"] for w in panel] == ["idle", "idle"]
    assert [w["version"] for w in panel] == [BUILD, BUILD]
    assert [w["stale"] for w in panel] == [False, False]
    assert [w["job_id"] for w in panel] == [None, None]
    # The rip box really is in the shared table — it is the filter that keeps it
    # off the panel, not an empty row.
    assert presence.serve_id(RIP_BOX) in {w.worker_id for w in store.list_workers()}

    box_a.request_stop()
    box_b.request_stop()
    await _finish(task_a)
    await _finish(task_b)


async def test_drain_lets_the_running_encode_finish_before_the_box_stops(
        tmp_path, monkeypatch):
    """Drain is the promise the whole maintenance story rests on: the box takes
    no NEW work, but the four-hour encode it is in the middle of runs to
    completion and gets placed. A drain that killed HandBrake mid-file would
    cost the disc's whole encode and leave a half-written file in staging."""
    _stub_the_version(monkeypatch)
    cfg = _config_dir(tmp_path)
    store = ClusterStore()
    manager, _ = _rip_box(cfg, store, monkeypatch)
    _queue(store, cfg, 1, "WarGames", 1983)
    _queue(store, cfg, 2, "Tron", 1982)          # the job drain must NOT start
    box_a = _worker(cfg, store, "encoder-A")
    timeline: list[str] = []

    async def on_encode(film):
        if film.startswith("Tron"):
            box_a.request_stop()                 # never expected; keeps the test finite
            return
        await _spin(10)                          # the box heartbeats mid-encode
        _serve_poll(manager, store)              # the panel the operator is looking at
        assert [w["job_id"] for w in manager.snapshot()["workers"]] == [1]
        # The operator clicks Drain on encoder-A while it is mid-file.
        assert manager.request_worker_action("drain", "encoder-A") == 1
        timeline.append("drain requested")
        assert store.cluster_signals()[signal_key("drain", "encoder-A")] == 1
        assert box_a._stop is False               # not noticed yet — still encoding
        await _spin(5)                            # and the encode keeps going

    _stub_the_tools(monkeypatch, on_encode, timeline)
    task = asyncio.create_task(box_a.run())
    await _finish(task)

    assert timeline == ["started encoding WarGames (1983)", "drain requested",
                        "finished encoding WarGames (1983)"]
    # The in-flight job finished and landed in the library.
    done = _record(store, 1)
    assert done.error is None
    assert done.state == "done" and done.worker == "encoder-A"
    assert [Path(p).name for p in done.outputs] == ["WarGames (1983).mkv"]
    assert Path(done.outputs[0]).exists()
    assert _library(cfg) == ["WarGames (1983).mkv"]
    # ...and only then did the box stop, without touching the next job.
    assert box_a._stop is True
    assert box_a.restart_requested is False       # drain means stay down
    assert box_a.update_requested is False
    assert _record(store, 2).state == "ready"
    assert store.claim_next("encoder-B", CLAIMABLE_STATES).id == 2


async def test_update_all_workers_also_drains_first_and_asks_for_a_pull(
        tmp_path, monkeypatch):
    """The "Update all workers" button is the one that actually gets used: one
    click after pushing a commit. It is a broadcast, so it has to reach a box
    that is busy — and it must still be drain-first, or every rollout costs
    whatever every box happened to be encoding at the time."""
    _stub_the_version(monkeypatch)
    cfg = _config_dir(tmp_path)
    store = ClusterStore()
    manager, _ = _rip_box(cfg, store, monkeypatch)
    _queue(store, cfg, 1, "WarGames", 1983)
    _queue(store, cfg, 2, "Tron", 1982)          # must survive the update untouched
    box_a = _worker(cfg, store, "encoder-A")
    box_b = _worker(cfg, store, "encoder-B")
    box_b._register(None, "idle")                # a second box, connected but idle
    box_b._signal_tokens = store.cluster_signals()  # what run() snapshots at startup
    timeline: list[str] = []

    async def on_encode(film):
        if film.startswith("Tron"):
            box_a.request_stop()                 # never expected; keeps the test finite
            return
        await _spin(10)
        _serve_poll(manager, store)
        # One click, both boxes addressed — reported back so the UI can say so.
        assert manager.request_worker_update() == 2
        timeline.append("update requested")
        assert store.cluster_signals()[signal_key("update", ALL_WORKERS)] == 1
        assert box_a._stop is False
        await _spin(5)

    _stub_the_tools(monkeypatch, on_encode, timeline)
    task = asyncio.create_task(box_a.run())
    await _finish(task)

    assert timeline == ["started encoding WarGames (1983)", "update requested",
                        "finished encoding WarGames (1983)"]
    done = _record(store, 1)
    assert done.error is None
    assert done.state == "done" and done.worker == "encoder-A"
    assert _library(cfg) == ["WarGames (1983).mkv"]
    # Unlike a drain, this box is expected back: it pulls, then re-execs.
    assert box_a.update_requested is True
    assert box_a.restart_requested is True
    assert box_a._stop is True
    assert _record(store, 2).state == "ready"    # nothing was thrown away
    # The same click reaches the box that happened to be idle at the time.
    box_b._check_update_signal()
    assert box_b.update_requested is True


async def test_an_action_aimed_at_one_box_leaves_the_other_one_encoding(
        tmp_path, monkeypatch):
    """Rolling maintenance only works if the signal is addressed. Restarting the
    box with the flaky drive must not take the whole encode farm down with it —
    the other box has to keep claiming and finishing work throughout."""
    _stub_the_version(monkeypatch)
    _stub_the_tools(monkeypatch)
    cfg = _config_dir(tmp_path)
    store = ClusterStore()
    manager, _ = _rip_box(cfg, store, monkeypatch)
    box_a, box_b = _worker(cfg, store, "encoder-A"), _worker(cfg, store, "encoder-B")

    task_a = asyncio.create_task(box_a.run())
    task_b = asyncio.create_task(box_b.run())
    await _spin()
    _serve_poll(manager, store)
    assert [w["id"] for w in manager.snapshot()["workers"]] == ["encoder-A", "encoder-B"]

    # Restart encoder-A only. Two boxes are connected; one is addressed.
    assert manager.request_worker_action("restart", "encoder-A") == 1
    await _spin()

    assert task_a.done()
    assert box_a.restart_requested is True and box_a._stop is True
    # encoder-B read the same signal table and correctly ignored the message.
    assert not task_b.done()
    assert box_b._stop is False
    assert box_b.restart_requested is False and box_b.update_requested is False

    # ...and it is still doing its job: work queued during the maintenance
    # window is picked up by the box that stayed up.
    _queue(store, cfg, 5, "Tron", 1982)
    await _spin()
    done = _record(store, 5)
    assert done.error is None
    assert done.state == "done" and done.worker == "encoder-B"
    assert _library(cfg) == ["Tron (1982).mkv"]

    box_b.request_stop()
    await _finish(task_b)
    assert box_b.restart_requested is False      # it only ever did what it was told


def test_the_panel_button_signals_the_box_named_in_the_url(tmp_path, monkeypatch):
    """End to end through HTTP: the per-row Drain/Restart/Update buttons post the
    worker's name in the path, and the worker reads a key built from that name.
    If the two sides ever disagreed on the key the request would be accepted,
    reported as sent, and silently never arrive."""
    from fastapi.testclient import TestClient

    from blurayripper.web.app import create_app

    _stub_the_version(monkeypatch)
    cfg = _config_dir(tmp_path)
    store = ClusterStore()
    monkeypatch.setattr(socket, "gethostname", lambda: RIP_BOX)
    app = create_app(load_settings(cfg))
    manager = app.state.manager
    manager.store = store
    box_a, box_b = _worker(cfg, store, "encoder-A"), _worker(cfg, store, "encoder-B")
    for box in (box_a, box_b):
        box._register(None, "idle")              # both boxes are polling for work
        box._signal_tokens = store.cluster_signals()  # what run() snapshots at startup
    manager._workers_cache = store.list_workers()
    client = TestClient(app)

    response = client.post("/api/workers/encoder-A/action", json={"action": "restart"})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "workers": 1}

    box_a._check_update_signal()
    box_b._check_update_signal()
    assert box_a.restart_requested is True and box_a._stop is True
    assert box_b._stop is False

    # A name that isn't connected is refused instead of being written to a table
    # nobody reads, and an unknown action never reaches the cluster at all.
    typo = client.post("/api/workers/encoder-C/action", json={"action": "drain"})
    assert typo.status_code == 400 and "No worker named" in typo.json()["detail"]
    bogus = client.post("/api/workers/*/action", json={"action": "explode"})
    assert bogus.status_code == 400 and "Unknown worker action" in bogus.json()["detail"]
    assert set(store.cluster_signals()) == {signal_key("restart", "encoder-A")}


async def test_a_box_that_goes_dark_is_reported_once_and_its_job_is_recovered(
        tmp_path, monkeypatch):
    """The encoder loses power four hours into a film.

    Nothing tells the rip box that: the only evidence is a row that stops
    advancing. So it has to notice, say so ONCE (an alert every poll for the
    rest of the night trains you to ignore them), refuse to disturb the job
    while the box is still alive, and hand the work to the box that is still up
    when it isn't.
    """
    _stub_the_version(monkeypatch)
    cfg = _config_dir(tmp_path)
    store = ClusterStore()
    _queue(store, cfg, 1, "WarGames", 1983)
    box_a, box_b = _worker(cfg, store, "encoder-A"), _worker(cfg, store, "encoder-B")
    wedged = asyncio.Event()                     # encoder-A's HandBrake never returns
    started: list[str] = []

    async def on_encode(film):
        started.append(film)
        if len(started) == 1:
            await wedged.wait()

    _stub_the_tools(monkeypatch, on_encode)
    task_a = asyncio.create_task(box_a.run())
    await _spin()

    # --- Chapter 1: alive and working. --------------------------------------
    manager, alerts = _rip_box(cfg, store, monkeypatch)
    manager.recover()                            # the panel is built from the store
    _serve_poll(manager, store)
    assert manager.snapshot()["jobs"][0]["state"] == "encoding"
    panel = manager.snapshot()["workers"]
    assert [(w["id"], w["job_id"], w["stale"]) for w in panel] == [("encoder-A", 1, False)]
    assert alerts == []
    # Requeue is refused while a live box is heartbeating the job — recovering it
    # here would run the same encode twice and fight over the output file.
    with pytest.raises(ValueError, match="isn't stalled"):
        manager.requeue(1)

    # --- Chapter 2: the power goes. -----------------------------------------
    store.go_dark("encoder-A")
    store.tick(400)                              # past the 5m offline window
    _serve_poll(manager, store)
    assert [(a[0], a[2]) for a in alerts] == [("🔌 Worker offline: encoder-A", "high")]
    assert "5m" in alerts[0][1]
    _serve_poll(manager, store)
    _serve_poll(manager, store)
    assert len(alerts) == 1                      # said once, not every poll
    assert manager.snapshot()["workers"][0]["stale"] is True

    # --- Chapter 3: the other box picks the film up. ------------------------
    manager._spawn = lambda coro: coro.close()   # serve would re-run it locally
    manager.requeue(1)
    assert _record(store, 1).state == "ready"
    task_b = asyncio.create_task(box_b.run())
    await _spin()

    done = _record(store, 1)
    assert done.error is None
    assert done.state == "done" and done.worker == "encoder-B"
    assert _library(cfg) == ["WarGames (1983).mkv"]
    # The same film ran twice: encoder-A's abandoned attempt, then encoder-B's.
    # Only the encode was redone — it worked from the rip already on staging
    # (there is no MakeMKV here at all, so a re-rip could only have failed).
    assert started == ["WarGames (1983)", "WarGames (1983)"]

    box_b.request_stop()
    await _finish(task_b)
    task_a.cancel()                              # the dead box, finally reaped
    with contextlib.suppress(asyncio.CancelledError):
        await task_a


async def test_a_box_that_comes_back_is_reported_too(tmp_path, monkeypatch):
    """The recovery notification closes the loop: without it the only way to
    know the encoder came back from its reboot is to go and look at the panel,
    which defeats the point of being told it went away."""
    _stub_the_version(monkeypatch)
    _stub_the_tools(monkeypatch)
    cfg = _config_dir(tmp_path)
    store = ClusterStore()
    manager, alerts = _rip_box(cfg, store, monkeypatch)

    box = _worker(cfg, store, "encoder-A")
    task = asyncio.create_task(box.run())
    await _spin()
    _serve_poll(manager, store)
    assert alerts == []
    box.request_stop()                           # it is taken down for maintenance
    await _finish(task)

    store.go_dark("encoder-A")
    store.tick(400)
    _serve_poll(manager, store)
    assert [a[0] for a in alerts] == ["🔌 Worker offline: encoder-A"]

    # The box is back on a fresh process with the same (stable) name.
    store.come_back("encoder-A")
    rebooted = _worker(cfg, store, "encoder-A")
    task = asyncio.create_task(rebooted.run())
    await _spin()
    _serve_poll(manager, store)

    assert [a[0] for a in alerts] == ["🔌 Worker offline: encoder-A",
                                      "✅ Worker back: encoder-A"]
    panel = manager.snapshot()["workers"]
    assert [(w["id"], w["stage"], w["stale"]) for w in panel] == [
        ("encoder-A", "idle", False)]
    _serve_poll(manager, store)
    assert len(alerts) == 2                      # the recovery is reported once too

    rebooted.request_stop()
    await _finish(task)


async def test_the_scheduled_restart_waits_for_the_job_in_flight(tmp_path, monkeypatch):
    """The periodic restart exists to clear whatever a week of encoding leaks,
    and it is checked BETWEEN jobs on purpose: a box that hits its age limit four
    hours into a film finishes the film first, then restarts while idle."""
    _stub_the_version(monkeypatch)
    cfg = _config_dir(tmp_path, worker_restart_after_days=3)
    store = ClusterStore()
    _queue(store, cfg, 1, "WarGames", 1983)
    _queue(store, cfg, 2, "Tron", 1982)
    box = _worker(cfg, store, "encoder-A")
    box._started_monotonic -= 2 * 86400          # up two days: not due yet
    timeline: list[str] = []

    async def on_encode(film):
        if film.startswith("Tron"):
            box.request_stop()                   # never expected; keeps the test finite
            return
        box._started_monotonic -= 86400 + 60     # the third day passes mid-encode

    _stub_the_tools(monkeypatch, on_encode, timeline)
    await _finish(asyncio.create_task(box.run()))

    # It claimed and finished a job while young, and stopped at the next idle
    # moment rather than at the moment the deadline passed.
    assert timeline == ["started encoding WarGames (1983)",
                        "finished encoding WarGames (1983)"]
    assert _record(store, 1).state == "done"
    assert _library(cfg) == ["WarGames (1983).mkv"]
    assert box.restart_requested is True
    assert box.update_requested is False         # a plain restart, no pull
    assert _record(store, 2).state == "ready"    # left for a box that is staying up


async def test_a_just_restarted_box_will_not_immediately_update_again(
        tmp_path, monkeypatch):
    """The guard against the worst failure mode in this whole area: a worker that
    follows the rip box's version drains, pulls, restarts — and comes back still
    behind (serve is on an unpushed commit, or the pull failed). Unguarded it
    does that forever and never encodes anything. So a young process encodes
    first and only considers updating once it has been up a while."""
    _stub_the_version(monkeypatch)
    cfg = _config_dir(tmp_path, worker_auto_update=True)
    store = ClusterStore()
    # The rip box is ahead — the condition that triggers an auto-update.
    store.record_worker(presence.serve_id(RIP_BOX), NEWER_BUILD, None, "serving", "srv")
    _queue(store, cfg, 1, "WarGames", 1983)
    _queue(store, cfg, 2, "Tron", 1982)
    box = _worker(cfg, store, "encoder-A")       # a brand-new process (uptime ~0)
    timeline: list[str] = []

    async def on_encode(film):
        if film.startswith("Tron"):
            box.request_stop()                   # never expected; keeps the test finite
            return
        assert box.update_requested is False     # too young to update: it encodes
        box._started_monotonic -= 1000           # ...and now it has been up a while

    _stub_the_tools(monkeypatch, on_encode, timeline)
    await _finish(asyncio.create_task(box.run()))

    assert timeline == ["started encoding WarGames (1983)",
                        "finished encoding WarGames (1983)"]
    assert _record(store, 1).state == "done"
    # Once past the minimum uptime the update does happen — the guard delays the
    # rollout, it doesn't cancel it.
    assert box.update_requested is True and box.restart_requested is True
    assert _record(store, 2).state == "ready"
