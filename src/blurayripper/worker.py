"""Distributed encode worker.

Runs on each encode server. Loops: atomically claim a queued job from the
shared Postgres, run the post-rip pipeline (encode + subtitle OCR + place) over
the shared NAS, and mark it done — heart-beating so that if this worker dies its
job is reclaimed by another. The rip machine only rips and enqueues.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket

from .config import Settings
from .persistence import (
    JobRecord,
    Store,
    assignment_from_dict,
    assignment_to_dict,
    disc_from_dict,
    disc_to_dict,
    make_store,
)
from .pipeline import Job, JobState, Pipeline
from .scheduler import EncodeScheduler

# States a worker may claim: freshly-queued, plus in-progress states (reclaimed
# only when their previous worker's heartbeat has gone stale).
CLAIMABLE_STATES = [
    JobState.READY.value, JobState.ENCODING.value, JobState.EXTRACTING_SUBS.value,
    JobState.OCR_SUBS.value, JobState.PLACING.value,
]


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
        self.log.append(message)
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
        self.worker_id = worker_id or f"{socket.gethostname()}:{__import__('os').getpid()}"
        self.store = make_store(settings.database_url)
        self.pipeline = Pipeline(settings)
        self.scheduler = EncodeScheduler(settings.queue.encode_capacity)
        self._stop = False

    def request_stop(self) -> None:
        """Drain: finish the job currently encoding (if any), claim no more, exit."""
        self._stop = True

    async def run(self) -> None:
        if not self.settings.database_url:
            raise RuntimeError("A worker needs a shared database. Set BLURAYRIPPER_DATABASE_URL.")
        self.store.init()
        print(f"Worker {self.worker_id} ready — polling for jobs…", flush=True)
        last_error = None
        while not self._stop:
            try:
                rec = self.store.claim_next(
                    self.worker_id, CLAIMABLE_STATES, self.settings.cluster.stale_after_seconds)
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
                await asyncio.sleep(self.settings.cluster.worker_poll_seconds)
                continue
            await self._process(rec)

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

    async def _heartbeat(self, reporter: WorkerReporter) -> None:
        while True:
            await asyncio.sleep(self.settings.cluster.heartbeat_seconds)
            reporter.touch()
