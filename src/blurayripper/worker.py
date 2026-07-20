"""Distributed encode worker.

Runs on each encode server. Loops: atomically claim a queued job from the
shared Postgres, run the post-rip pipeline (encode + subtitle OCR + place) over
the shared NAS, and mark it done — heart-beating so that if this worker dies its
job is reclaimed by another. The rip machine only rips and enqueues.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import uuid

from . import handbrake_store
from .config import Settings, load_settings
from .persistence import (
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
        with contextlib.suppress(Exception):
            self._store.save(rec)


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
        # Set when a drain should be followed by a git-pull + restart (from the
        # 'u' key OR a cluster-wide "update all workers" signal from the server).
        self.update_requested = False
        self._update_token = 0  # cluster update signal value seen at startup
        # A per-process id so the store can tell a restart of THIS worker apart
        # from an ordinary heartbeat and reset its uptime clock accordingly.
        self._run_token = uuid.uuid4().hex

    def request_stop(self) -> None:
        """Drain: finish the job currently encoding (if any), claim no more, exit."""
        self._stop = True

    def request_update(self) -> None:
        """Drain, then git-pull + restart (the CLI does the pull/re-exec on exit)."""
        self.update_requested = True
        self._stop = True

    def _check_update_signal(self) -> None:
        """Act on a server-triggered 'update all workers' once, when the shared
        signal rises above what we saw at startup."""
        if self.update_requested:
            return
        try:
            tok = self.store.worker_update_token()
        except Exception:
            return  # DB blip — try again next poll
        if tok > self._update_token:
            print("Update requested from the server — draining, then git pull + restart.",
                  flush=True)
            self.request_update()

    async def run(self) -> None:
        if not self.settings.database_url:
            raise RuntimeError("A worker needs a shared database. Set BLURAYRIPPER_DATABASE_URL.")
        self.store.init()
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
            self._update_token = self.store.worker_update_token()  # only newer signals count
        print(f"Worker {self.worker_id} ready (v{self._version}) — polling for jobs…", flush=True)
        last_error = None
        while not self._stop:
            self._check_update_signal()  # server-triggered "update all workers"?
            if self._stop:
                break
            try:
                rec = self.store.claim_next(
                    self.worker_id, self._claimable_states(),
                    self.settings.cluster.stale_after_seconds)
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
                self._register(None, "idle")   # heartbeat presence while idle
                await asyncio.sleep(self.settings.cluster.worker_poll_seconds)
                continue
            if not self._fully_ripped(rec):
                # The rip box is still preparing this job: it can briefly show a
                # claimable READY state before its rip checkpoints exist. Put the
                # claim back and keep polling, rather than failing it with
                # "no ripped file to encode" (and stealing it from the rip box).
                with contextlib.suppress(Exception):
                    self.store.release(rec.id)
                self._register(None, "idle")
                print(f"Job {rec.id} claimed but not fully ripped yet — "
                      "released for the rip box.", flush=True)
                await asyncio.sleep(self.settings.cluster.worker_poll_seconds)
                continue
            self._apply_worker_settings()       # apply latest override before this job
            await self._process(rec)

    def _claimable_states(self) -> list[str]:
        """Which job states this worker may claim. The subtitle states are only
        claimable when workers own subtitle work; otherwise they're serve-owned."""
        if self.settings.cluster.worker_does_subs:
            return CLAIMABLE_STATES + _SUBS_STATES
        return CLAIMABLE_STATES

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

    def _register(self, job_id, stage: str) -> None:
        """Report this worker's presence to the store (for the UI's panel)."""
        with contextlib.suppress(Exception):
            self.store.record_worker(
                self.worker_id, getattr(self, "_version", None), job_id, stage,
                self._run_token)

    async def _process(self, rec: JobRecord) -> None:
        job = job_from_record(rec)
        reporter = WorkerReporter(self.store, self.worker_id, rec.id, job)
        staging_root = self.settings.storage.staging_dir / f"job_{rec.id}"
        reporter.on_event(f"Claimed by {self.worker_id}.")
        hb = asyncio.create_task(self._heartbeat(reporter))
        try:
            results = await self.pipeline.encode_phase(
                job, staging_root, self.scheduler, reporter)
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
                self.store.release(rec.id)

    async def _heartbeat(self, reporter: WorkerReporter) -> None:
        while True:
            await asyncio.sleep(self.settings.cluster.heartbeat_seconds)
            reporter.touch()
            self._register(reporter._job_id, reporter.stage or "working")
