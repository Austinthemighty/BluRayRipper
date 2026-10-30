"""Distributed encode worker.

Runs on each encode server. Loops: atomically claim a queued job from the
shared Postgres, run the post-rip pipeline (encode + subtitle OCR + place) over
the shared NAS, and mark it done — heart-beating so that if this worker dies its
job is reclaimed by another. The rip machine only rips and enqueues.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
import ipaddress
import json
import socket
import time
import urllib.parse
import uuid

from . import handbrake_store, maintenance, presence
from .config import Settings, load_settings
from .persistence import (
    ALL_WORKERS,
    WORKER_ACTIONS,
    signal_key,
    JobRecord,
    Store,
    assignment_from_dict,
    assignment_to_dict,
    disc_from_dict,
    disc_to_dict,
    make_store,
    stamp_log,
)
from .pipeline import Job, JobState, Pipeline
from .version import commit_count
from .scheduler import EncodeScheduler

# States a worker may always claim: a freshly-queued job (READY), plus the
# in-progress encode/place states (reclaimed only when a previous worker's
# heartbeat has gone stale).
CLAIMABLE_STATES = [
    JobState.READY.value, JobState.ENCODING.value, JobState.PLACING.value,
]
# The subtitle states are claimable ONLY when workers do the subtitle work
# (cluster.worker_does_subs). Otherwise the serve box owns them — it passes
# through EXTRACTING_SUBS/OCR_SUBS while prepping, with no claimed_by set — and a
# worker grabbing one would start encoding a job the rip box is still preparing.
_SUBS_STATES = [JobState.EXTRACTING_SUBS.value, JobState.OCR_SUBS.value]


def job_from_record(rec: JobRecord) -> Job:
    job = Job(source=rec.source)
    job.disc = disc_from_dict(rec.disc)
    job.fingerprint = rec.fingerprint
    job.selected_title_ids = list(rec.selected_title_ids)
    job.assignments = {a.title_id: a for a in (assignment_from_dict(d) for d in rec.assignments)}
    job.grain_mode = rec.grain_mode
    job.tune_grain = rec.tune_grain
    job.checkpoints = {int(k): v for k, v in rec.checkpoints.items()}
    return job


class WorkerReporter:
    """Pipeline reporter that mirrors progress into the shared store."""

    def __init__(self, store: Store, worker_id: str, job_id: int, job: Job) -> None:
        self._store = store
        self._worker = worker_id
        self._job_id = job_id
        self._job = job
        self.stage = ""
        self.progress = 0.0
        self.log: list[str] = []

    def on_event(self, message: str) -> None:
        print(f"[job {self._job_id}] {message}", flush=True)
        self.log.append(stamp_log(message))
        del self.log[:-200]

    def on_progress(self, stage: str, fraction: float | None) -> None:
        self.stage = stage
        if fraction is not None:
            self.progress = fraction

    def checkpoint(self) -> None:
        self.save(self._state())

    def touch(self) -> None:
        """Persist current progress + prove the worker is alive."""
        self.save(self._state())
        with contextlib.suppress(Exception):
            self._store.heartbeat(self._job_id, self._worker)

    def finish(self, state: JobState, outputs: list[str], error: str | None = None) -> None:
        self._job.state = state
        self.save(state.value, outputs=outputs, error=error)

    def _state(self) -> str:
        s = getattr(self._job, "state", JobState.ENCODING)
        return s.value if hasattr(s, "value") else str(s)

    def save(self, state: str, outputs: list[str] | None = None, error: str | None = None) -> None:
        rec = JobRecord(
            id=self._job_id, source=self._job.source, state=state,
            fingerprint=self._job.fingerprint, disc=disc_to_dict(self._job.disc),
            selected_title_ids=self._job.selected_title_ids,
            assignments=[assignment_to_dict(a) for a in self._job.assignments.values()],
            grain_mode=self._job.grain_mode, tune_grain=self._job.tune_grain,
            checkpoints={str(k): v for k, v in self._job.checkpoints.items()},
            outputs=outputs or [], error=error,
            stage=self.stage, progress=round(self.progress, 4), log=self.log[-30:],
            worker=self._worker,
        )
        # Owner-scoped, like heartbeat() and release(): a box that lost the
        # database for longer than stale_after_seconds has its job legitimately
        # reclaimed while its own encode keeps running, and its next write would
        # stamp DONE/FAILED (and its own stage/progress) over the row the NEW box
        # is working — which serve then mirrors to the UI.
        with contextlib.suppress(Exception):
            self._store.save_if_owner(rec, self._worker)


def local_address(database_url: str | None) -> str | None:
    """The IP this box reaches the cluster on, or None if it can't be worked out.

    Asks the ROUTING TABLE, not the hostname. A worker typically has several
    addresses — a ZeroTier link, a LAN NIC, a Docker bridge or two, loopback —
    and the only one worth showing in the panel is the one the cluster actually
    sees. Resolving the local hostname picks whichever the resolver likes, which
    on these boxes is as often 127.0.1.1 as anything useful.

    Connecting a UDP socket sends no packets; it only makes the kernel choose a
    route, so this costs nothing and never touches the database.
    """
    host = None
    if database_url:
        with contextlib.suppress(Exception):
            candidate = urllib.parse.urlsplit(database_url).hostname
            # ONLY an IP literal. connect() on a name performs a DNS lookup, and
            # settimeout does not cover getaddrinfo — so a database URL with a
            # hostname would block the heartbeat on the resolver, for as long as
            # the resolver felt like taking. Costing a worker its heartbeat to
            # decorate a panel is not a trade worth making; the default route is
            # a good enough answer when we can't have the exact one.
            if candidate:
                ipaddress.ip_address(candidate)   # raises unless it's literal
                host = candidate
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.5)
            # Port 9 is discard, and nothing is sent to it either way.
            sock.connect((host or "8.8.8.8", 9))
            return sock.getsockname()[0]
    except OSError:
        return None


class WorkerRunner:
    def __init__(self, settings: Settings, worker_id: str | None = None) -> None:
        self.settings = settings
        # Default to the hostname (STABLE across restarts) — not host:pid, or every
        # restart would look like a brand-new worker (duplicate in the panel/scope
        # dropdown, and its per-worker settings override orphaned). Run more than one
        # worker on a host? Give each a distinct --name.
        self.worker_id = worker_id or socket.gethostname()
        self.store = make_store(settings.database_url)
        self.pipeline = Pipeline(settings)
        self.scheduler = EncodeScheduler(settings.queue.encode_capacity)
        self._stop = False
        # CONSECUTIVE refusals, not distinct job ids: the queue always re-offers
        # the lowest claimable id, so a box that cannot see any ripped file turns
        # down the same job over and over. Counting ids would have stuck at one
        # and never reported the loop it is actually in.
        self._refused = 0
        # When each job was FIRST seen unworkable, so a checkpoint pointing at a
        # file that no longer exists eventually gets parked instead of declined
        # for ever.
        self._refused_since: dict[int, float] = {}
        # When this box last passed over each job as taller than its max encode
        # height. The store skips such a job once its heights are saved; a job
        # offered straight back means they did not stick.
        self._passed_over: dict[int, float] = {}
        # Set when a drain should be followed by a git-pull + restart (from the
        # 'u' key OR a cluster-wide "update all workers" signal from the server).
        self.update_requested = False   # pull before restarting
        self.restart_requested = False  # re-exec after draining (update implies this)
        # Signal tokens seen at startup, so a request sent while this worker was
        # down doesn't fire when it comes back.
        self._signal_tokens: dict[str, int] = {}
        # A per-process id so the store can tell a restart of THIS worker apart
        # from an ordinary heartbeat and reset its uptime clock accordingly.
        self._run_token = uuid.uuid4().hex
        self._address: str | None = None
        self._address_tried = float('-inf')  # monotonic of the last probe
        # Watch the rip box: an offline server can't tell you it's offline, so the
        # workers do it. Every online worker alerts, which is deliberate — one of
        # them may be offline too, and the whole point is that you find out.
        self._serve_watch = presence.PresenceWatch(
            threshold_seconds=settings.cluster.offline_alert_seconds)
        self._notify_tasks: set = set()   # strong refs; see _notify
        self._started_monotonic = time.monotonic()  # for the periodic restart

    def request_stop(self) -> None:
        """Drain: finish the job currently encoding (if any), claim no more, exit."""
        self._stop = True

    def request_update(self) -> None:
        """Drain, then git-pull + restart (the CLI does the pull/re-exec on exit)."""
        self.update_requested = True
        self.restart_requested = True
        self._stop = True

    def request_restart(self) -> None:
        """Drain, then restart on the CURRENT code — no pull."""
        self.restart_requested = True
        self._stop = True

    # Don't auto-update again until the process has been up this long. If a pull
    # can't actually reach the server's version (serve is on an unpushed commit,
    # or the pull fails), the worker would otherwise drain-pull-restart in a hot
    # loop and never encode anything.
    _AUTO_UPDATE_MIN_UPTIME = 900

    def _uptime_seconds(self) -> float:
        return time.monotonic() - self._started_monotonic

    def _maybe_periodic_restart(self) -> None:
        """Restart once this worker has been up longer than the configured age.

        Called between jobs, so it is always drain-first: a running encode
        finishes and is placed before the restart. A worker that stays busy past
        the deadline simply restarts at its next idle moment."""
        days = self.settings.cluster.worker_restart_after_days
        if days <= 0 or self._stop:
            return
        if self._uptime_seconds() < days * 86400:
            return
        print(f"Up for over {days} day(s) — restarting (nothing is in flight).",
              flush=True)
        self.request_restart()

    def _maybe_auto_update(self) -> None:
        """Follow the rip box: if it reports a NEWER build, pull up to it.

        The server's version is the reference, so updating serve rolls out to
        every worker without visiting them. Only ever moves FORWARD — a worker
        ahead of the server is left alone rather than downgraded."""
        if not self.settings.cluster.worker_auto_update or self._stop:
            return
        if self._uptime_seconds() < self._AUTO_UPDATE_MIN_UPTIME:
            return
        mine = commit_count(getattr(self, "_version", None))
        if mine is None:
            return  # version built without git — can't compare, don't guess
        try:
            workers = self.store.list_workers()
        except Exception:
            return
        serve = next((w for w in workers if presence.is_serve(w.worker_id)), None)
        theirs = commit_count(serve.version) if serve else None
        if theirs is None or theirs <= mine:
            return
        print(f"Rip box is on build {theirs}, this worker is on {mine} — "
              "draining, then git pull + restart.", flush=True)
        self.request_update()

    def _maintenance_hold(self) -> str | None:
        """A stage string while a maintenance window says take no new work.

        Reading the window is best-effort: an unreachable database means no
        hold. "I could not read the schedule" turning into "stop working" would
        let a blip idle the whole cluster."""
        window = maintenance.load(self.store)
        if not window.scheduled:
            return None
        now = datetime.now(timezone.utc)
        if not window.holds_new_work(now):
            return None
        return (f"holding for maintenance — no new jobs "
                f"(drain at {window.drain_at:%Y-%m-%d %H:%M} UTC)")

    def _check_update_signal(self) -> None:
        """Act on a signal the serve box aimed at this worker (or at all of them),
        once, when its token rises above what we saw at startup.

        Startup-relative so a signal sent while this worker was down doesn't fire
        the moment it comes back. Keys are ``action:target``; a worker reads both
        its own name and the ``*`` broadcast."""
        if self._stop:
            return
        try:
            tokens = self.store.cluster_signals()
        except Exception:
            return  # DB blip — try again next poll
        fired = None
        for action in WORKER_ACTIONS:            # least to most disruptive
            for target in (ALL_WORKERS, self.worker_id):
                key = signal_key(action, target)
                if tokens.get(key, 0) > self._signal_tokens.get(key, 0):
                    fired = action
        self._signal_tokens = tokens             # consume every key we just saw
        if fired is None:
            return
        if fired == "drain":
            print("Drain requested from the server — finishing the current job, then "
                  "exiting.", flush=True)
            self.request_stop()
        elif fired == "restart":
            print("Restart requested from the server — draining, then restarting on "
                  "the current code.", flush=True)
            self.request_restart()
        else:
            print("Update requested from the server — draining, then git pull + restart.",
                  flush=True)
            self.request_update()

    async def _init_store(self) -> None:
        """Create the schema, WAITING for the database instead of giving up.

        A worker that boots while the database is down (both come back after the
        same power cut, and Postgres is on another host) used to raise straight
        out of run() and exit — so the box was up, idle, and encoding nothing
        until someone noticed and started it by hand. Once running it already
        rides out an outage, and serve boots without a database on purpose; this
        is the same promise at startup. A drain signal still ends the wait."""
        last = None
        while not self._stop:
            try:
                self.store.init()
                if last is not None:
                    print("Database is back — starting.", flush=True)
                return
            except Exception as exc:
                msg = f"{type(exc).__name__}: {exc}"
                if msg != last:
                    print(f"Database unavailable, waiting for it: {msg}", flush=True)
                    last = msg
                await asyncio.sleep(self.settings.cluster.worker_poll_seconds)

    async def run(self) -> None:
        if not self.settings.database_url:
            raise RuntimeError("A worker needs a shared database. Set BLURAYRIPPER_DATABASE_URL.")
        await self._init_store()
        if self._stop:
            return
        from .tools.winjob import enable_kill_on_close
        enable_kill_on_close()  # kill our HandBrake/OCR children if the worker dies
        # Reap THIS worker's orphaned tools from a prior crash (HandBrake / OCR),
        # but never makemkvcon — that belongs to a co-located serve's live rip.
        if self.settings.cleanup_stale_tools:
            from pathlib import Path as _Path

            from .tools import process as _process
            names = {_Path(p).name for p in (self.settings.tools.handbrakecli,
                     self.settings.tools.subtitleedit, self.settings.tools.mkvextract) if p}
            killed = await _process.kill_stale_tools(sorted(names))
            if killed:
                print(f"Cleaned up stale tool processes: {', '.join(killed)}", flush=True)
        from .version import get_version
        self._version = get_version()
        self._apply_worker_settings()  # central per-worker override, if any
        with contextlib.suppress(Exception):
            self._signal_tokens = self.store.cluster_signals()  # only newer signals count
        print(f"Worker {self.worker_id} ready (v{self._version}) — polling for jobs…", flush=True)
        last_error = None
        while not self._stop:
            self._check_update_signal()   # server-triggered drain / restart / update?
            self._maybe_periodic_restart()  # been up too long?
            self._maybe_auto_update()       # rip box on a newer build?
            if self._stop:
                break
            hold = self._maintenance_hold()
            if hold is not None:
                # Deliberately BEFORE claim_next: the whole point of a scheduled
                # window is that nothing new is picked up, and a job claimed here
                # is one that runs past the deadline.
                self._register(None, hold)
                await asyncio.sleep(self.settings.cluster.worker_poll_seconds)
                continue
            cap = self.settings.cluster.worker_max_height
            try:
                # The keyword only when capped: an uncapped worker keeps the
                # plain one-row claim.
                rec = self.store.claim_next(
                    self.worker_id, self._claimable_states(),
                    self.settings.cluster.stale_after_seconds,
                    **({"max_height": cap} if cap else {}))
                last_error = None
            except Exception as exc:
                # Transient DB outage (e.g. a storage blip): keep the worker
                # alive and retry rather than exiting.
                msg = f"{type(exc).__name__}: {exc}"
                if msg != last_error:
                    print(f"Queue unavailable, will retry: {msg}", flush=True)
                    last_error = msg
                await asyncio.sleep(self.settings.cluster.worker_poll_seconds)
                continue
            if rec is None:
                self._apply_worker_settings()  # pick up a central config change while idle
                # Genuinely nothing to claim -- distinct from refusing everything.
                self._register(None, "idle")   # heartbeat presence while idle
                self._check_serve_presence()   # is the rip box still alive?
                await asyncio.sleep(self.settings.cluster.worker_poll_seconds)
                continue
            if not self._fully_ripped(rec):
                # The rip box is still preparing this job: it can briefly show a
                # claimable READY state before its rip checkpoints exist. Put the
                # claim back and keep polling, rather than failing it with
                # "no ripped file to encode" (and stealing it from the rip box).
                #
                # But only for a while. A checkpoint that names a file which no
                # longer exists never becomes true, and this box is the ONLY one
                # that looks at the disk -- serve decides "ripped" from the
                # checkpoint alone. Left alone, such a job sits in the encode
                # lane for ever while every worker quietly declines it.
                if self._refused_too_long(rec):
                    self._park_for_review(rec)
                with contextlib.suppress(Exception):
                    self.store.release(rec.id, self.worker_id)
                # Say WHY, in the panel, not just on this box's console. Refusing
                # every job looked exactly like having nothing to do: both showed
                # "idle", so a queue of hundreds could churn here every few
                # seconds and the UI reported a healthy, idle worker.
                self._refused += 1
                missing = self._first_missing_file(rec)
                self._register(None, self._refusing_stage(missing))
                print(f"Job {rec.id} claimed but not fully ripped yet — released "
                      f"for the rip box. Missing: {missing}", flush=True)
                await asyncio.sleep(self.settings.cluster.worker_poll_seconds)
                continue
            self._refused = 0       # something was workable; stop reporting refusals
            if cap and await self._too_tall_here(rec, cap):
                continue
            self._apply_worker_settings()       # apply latest override before this job
            await self._process(rec)

    async def _too_tall_here(self, rec: JobRecord, cap: int) -> bool:
        """Measure a claimed job against this box's max encode height, and put
        it back if a title is taller.

        Serve records each title's height when it queues a job, and the claim
        already passed over any it knew to be too tall. A job queued before that
        has no heights, so it is measured here, and the heights are saved on the
        job either way: the next claim then filters it in the store instead of
        measuring it again."""
        recorded = [(rec.checkpoints.get(str(t)) or rec.checkpoints.get(t) or {}).get("height")
                    for t in rec.selected_title_ids]
        if recorded and all(recorded):
            return False               # all measured, and the claim checked them
        job = job_from_record(rec)     # shares rec's checkpoint dicts
        reporter = WorkerReporter(self.store, self.worker_id, rec.id, job)
        heights = await self.pipeline.title_heights(
            job, self.settings.storage.staging_dir / f"job_{rec.id}", reporter)
        tallest = max(heights, default=0)
        if tallest <= cap:
            return False               # an unmeasurable title (0) is not refused
        last = self._passed_over.get(rec.id)
        self._passed_over[rec.id] = time.monotonic()
        if last is None:
            rec.log = list(rec.log or []) + [stamp_log(
                f"Passed over by {self.worker_id}: {tallest}p is above its "
                f"{cap}p max encode height.")]
        with contextlib.suppress(Exception):
            self.store.save_if_owner(rec, self.worker_id)
        with contextlib.suppress(Exception):
            self.store.release(rec.id, self.worker_id)
        self._register(None, f"idle — passed over job {rec.id} ({tallest}p > {cap}p)")
        print(f"Job {rec.id} is {tallest}p, above this box's {cap}p max encode "
              "height — released for another worker.", flush=True)
        if last is not None and time.monotonic() - last < 60:
            # Straight back after being passed over: its heights were lost
            # (serve re-saved the job without them) or never saved. They were
            # just written again; pause so a store that won't keep them can't
            # spin this box measuring the same job.
            await asyncio.sleep(self.settings.cluster.worker_poll_seconds)
        return True

    def _claimable_states(self) -> list[str]:
        """Which job states this worker may claim. The subtitle states are only
        claimable when workers own subtitle work; otherwise they're serve-owned."""
        if self.settings.cluster.worker_does_subs:
            return CLAIMABLE_STATES + _SUBS_STATES
        return CLAIMABLE_STATES

    def _refused_too_long(self, rec: JobRecord) -> bool:
        """Has this job been unworkable long enough to stop hoping?

        Timed, not counted: a rip legitimately takes a while, and how many times
        we happen to poll in that window says nothing about it. The clock starts
        the first time we see the job unworkable."""
        grace = max(60, self.settings.cluster.stale_after_seconds * 2)
        first = self._refused_since.setdefault(rec.id, time.monotonic())
        return (time.monotonic() - first) >= grace

    def _park_for_review(self, rec: JobRecord) -> None:
        """Tell the QUEUE the ripped file is gone, instead of only this console.

        Also drops the checkpoint that names the missing file. That checkpoint is
        what makes serve believe the job is ripped, so clearing it puts a disc
        job back in front of the operator with its "rip this disc" button rather
        than leaving it looking encode-ready for ever.
        """
        missing = self._first_missing_file(rec)
        for tid in list(rec.selected_title_ids):
            cp = rec.checkpoints.get(str(tid)) or rec.checkpoints.get(tid)
            if isinstance(cp, dict) and not cp.get("placed"):
                cp.pop("ripped_path", None)
        rec.state = JobState.FAILED.value
        rec.error = (
            f"The ripped file for this job is gone: {missing}. Nothing on this "
            "box can encode it, so it was waiting in the encode queue with no "
            "worker able to take it. Put the disc back in and Retry to rip it "
            "again, or Remove it. Nothing has been deleted by this."
        )
        rec.log = list(rec.log or []) + [
            f"[{self.worker_id}] Ripped file missing ({missing}) — parked for "
            "review rather than left queued for an encode that cannot happen."
        ]
        with contextlib.suppress(Exception):
            self.store.save_if_owner(rec, self.worker_id)
        self._refused_since.pop(rec.id, None)
        print(f"Job {rec.id} parked for review: ripped file missing ({missing}).",
              flush=True)

    def _refusing_stage(self, missing: str) -> str:
        """What the panel shows while this box is turning jobs down.

        "idle" is the truth for a worker with nothing to do and a lie for one
        rejecting a queue of hundreds, and the two were indistinguishable."""
        # One refusal is ordinary: the rip box briefly shows a claimable job
        # before its checkpoint exists. A run of them is a stuck box.
        if self._refused < 3:
            return f"idle — waiting on a rip ({missing})"
        return (f"NOT ENCODING: turned down {self._refused} jobs in a row — their "
                f"ripped files are not reachable from this box. Missing: {missing}")

    def _first_missing_file(self, rec: JobRecord) -> str:
        """The first ripped file this box cannot see, for the message above."""
        from .pipeline import _resolve_path

        staging = self.settings.storage.staging_dir
        for tid in rec.selected_title_ids:
            cp = rec.checkpoints.get(str(tid)) or rec.checkpoints.get(tid) or {}
            if cp.get("placed"):
                continue
            prior = cp.get("ripped_path")
            if not prior:
                return f"job {rec.id} title {tid} has no ripped file recorded"
            resolved = _resolve_path(staging, prior)
            if not resolved.exists():
                return str(resolved)
        return f"job {rec.id}"

    def _fully_ripped(self, rec: JobRecord) -> bool:
        """True once every selected title has a ripped file (or is already placed).
        Guards against claiming a job the serve box is still ripping — which shows a
        claimable READY state during the rip, before any rip checkpoint exists."""
        from .pipeline import _resolve_path

        staging = self.settings.storage.staging_dir
        for tid in rec.selected_title_ids:
            cp = rec.checkpoints.get(str(tid)) or rec.checkpoints.get(tid) or {}
            if cp.get("placed"):
                continue
            prior = cp.get("ripped_path")
            ripped = _resolve_path(staging, prior) if prior else None
            if ripped is None or not ripped.exists():
                return False
        return True

    def _sync_shared_preset(self) -> bool:
        """Pull the cluster-shared HandBrake preset and write it to this box's
        presets.local.json, so every worker encodes with the SAME preset instead
        of drifting to its own checkout's copy. Returns True if it changed."""
        try:
            content = self.store.get_shared_config(handbrake_store.SHARED_KEY)
        except Exception:
            return False
        if not content:
            return False
        changed = handbrake_store.apply_shared_preset(self.settings.config_dir, content)
        if changed:
            print("Applied the cluster's shared HandBrake preset.", flush=True)
        return changed

    def _apply_worker_settings(self) -> None:
        """Pull this worker's central override + the shared HandBrake preset from
        the store; if either changed, reload settings and rebuild the pipeline so
        the NEXT job uses it (never mid-encode). Safe on a DB blip — keeps current."""
        preset_changed = self._sync_shared_preset()
        try:
            overlay = self.store.get_worker_settings(self.worker_id)
        except Exception:
            return
        sig = json.dumps(overlay, sort_keys=True)
        if sig == getattr(self, "_wsettings_sig", None) and not preset_changed:
            return
        self._wsettings_sig = sig
        self.settings = load_settings(self.settings.config_dir, extra_overlay=overlay or None)
        self.pipeline = Pipeline(self.settings)
        self.scheduler = EncodeScheduler(self.settings.queue.encode_capacity)
        if overlay:
            summary = ", ".join(f"{s}.{k}={v}" for s, kv in overlay.items() for k, v in kv.items())
            print(f"Applied per-worker settings: {summary}", flush=True)

    def _check_serve_presence(self) -> None:
        """Notify if the rip box stops heartbeating (and when it returns).

        The server can't report its own outage, so the workers watch it. Failing
        to read the store is NOT treated as the server being down — that's this
        worker's own connectivity, and guessing would cry wolf on every blip."""
        if self.settings.cluster.offline_alert_seconds <= 0:
            return
        try:
            workers = self.store.list_workers()
        except Exception:
            return
        ages = {w.worker_id: w.seconds_since_seen
                for w in workers if presence.is_serve(w.worker_id)}
        gone, back = self._serve_watch.update(ages)
        window = presence.describe(self.settings.cluster.offline_alert_seconds)
        for serve in gone:
            host = serve.split(":", 1)[1] or "rip box"
            self._notify(f"🔌 Server offline: {host}",
                         f"No heartbeat for over {window}. Ripping has stopped; "
                         "queued encodes keep running.", priority="high")
        for serve in back:
            host = serve.split(":", 1)[1] or "rip box"
            self._notify(f"✅ Server back: {host}", "The rip box is reporting in again.")

    def _notify(self, title: str, message: str, priority: str | None = None) -> None:
        """Fire-and-forget push. Best-effort: an alert must never break the loop.

        The task is kept in a set until it finishes: asyncio only holds a weak
        reference, so a fire-and-forget task with no strong reference can be
        garbage-collected mid-flight and the notification silently lost."""
        if not self.settings.notify_url:
            return
        from .services.notify import click_link, notify

        with contextlib.suppress(Exception):
            task = asyncio.get_running_loop().create_task(
                notify(self.settings.notify_url, title, message, priority,
                       click_link(self.settings, derive=False)))
            self._notify_tasks.add(task)
            task.add_done_callback(self._notify_tasks.discard)

    def _register(self, job_id, stage: str) -> None:
        """Report this worker's presence to the store (for the UI's panel)."""
        # Retried until it succeeds rather than resolved once at startup: a
        # worker launched by systemd at boot can beat its own network up, and one
        # that answered "unknown" forever would be exactly the box you need to
        # reach. Rate-limited, though — this runs on every heartbeat, and a box
        # that can never work its address out must not pay for a socket each
        # time to keep failing.
        now = time.monotonic()
        if self._address is None and now - self._address_tried >= 60:
            self._address_tried = now
            self._address = local_address(self.settings.database_url)
        with contextlib.suppress(Exception):
            self.store.record_worker(
                self.worker_id, getattr(self, "_version", None), job_id, stage,
                self._run_token, self._address)

    async def _process(self, rec: JobRecord) -> None:
        job = job_from_record(rec)
        reporter = WorkerReporter(self.store, self.worker_id, rec.id, job)
        staging_root = self.settings.storage.staging_dir / f"job_{rec.id}"
        reporter.on_event(f"Claimed by {self.worker_id}.")
        # Publish WHICH job this box is holding before any work starts. The
        # heartbeat sleeps before its first touch, so until now the presence row
        # still said job_id=None from the last idle poll — and serve decides "is
        # anything working this?" precisely by looking for a row naming the job.
        # For that window a live encode read as stalled, and Requeue would have
        # released the claim and started a second encode of the same title onto
        # the same output path.
        self._register(rec.id, "claimed")
        hb = asyncio.create_task(self._heartbeat(reporter))
        try:
            results = await self.pipeline.encode_phase(
                job, staging_root, self.scheduler, reporter)
            # Look at the disk before saying Done. Pipeline.execute (the
            # single-box path) verifies its outputs; this path did not, so a
            # HandBrake that exited 0 having written nothing placed a 0-byte file
            # in the Plex library, checkpointed it as placed, and finished green.
            problems = self._output_problems(job, results)
            if problems:
                raise RuntimeError(
                    "The encode finished but its output is not usable:\n  - "
                    + "\n  - ".join(problems)
                    + "\nThe rip is kept, so Retry re-encodes rather than "
                      "starting over.")
            from .services import library
            library.cleanup(staging_root)
            outputs = [str(r.video_path) for r in results if r.video_path]
            reporter.finish(JobState.DONE, outputs)
            reporter.on_event("Done.")
        except Exception as exc:
            reporter.finish(JobState.FAILED, [], error=f"{type(exc).__name__}: {exc}")
            reporter.on_event(f"FAILED: {exc}")
        finally:
            hb.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb
            # Drop our claim now the job is DONE/FAILED. Otherwise claimed_by stays
            # set with a frozen heartbeat, so a user's immediate Retry can't be
            # re-claimed by any worker until the heartbeat ages past stale_after
            # (default 180s). The state is already non-claimable here, so releasing
            # is safe; it only matters once the manager flips a retry back to READY.
            with contextlib.suppress(Exception):
                self.store.release(rec.id, self.worker_id)

    def _output_problems(self, job: Job, results: list) -> list[str]:
        """Everything a finished encode promised but didn't deliver.

        Only the video is checked here (subtitles and NFO sidecars may have been
        placed by the rip box before this job was queued, so demanding them on
        the worker would fail perfectly good jobs). A title with no naming
        assignment is counted too: encode_phase logs "Skipping title N" and
        returns, which used to leave the job DONE with zero outputs AND swept the
        staging tree — destroying the rip for a disc nobody ever named.

        Any bad video also loses its "placed" checkpoint, because otherwise the
        next Retry sees placed=True, skips the title, and fails on the same empty
        file forever.
        """
        problems: list[str] = []
        by_title = {r.title_id: r for r in results}
        for title_id in job.selected_title_ids:
            result = by_title.get(title_id)
            if result is None:
                problems.append(
                    f"title {title_id} has no naming assignment, so nothing was "
                    "encoded for it — name the disc on the rip box, then Retry")
                continue
            video = result.video_path
            if video is None:
                why = f"title {title_id}: no video was placed"
            elif not video.exists():
                why = f"missing video: {video}"
            elif video.stat().st_size == 0:
                why = f"video is 0 bytes: {video}"
            else:
                continue
            problems.append(why)
            job.checkpoints.get(title_id, {}).pop("placed", None)
        return problems

    async def _heartbeat(self, reporter: WorkerReporter) -> None:
        while True:
            await asyncio.sleep(self.settings.cluster.heartbeat_seconds)
            reporter.touch()
            self._register(reporter._job_id, reporter.stage or "working")
