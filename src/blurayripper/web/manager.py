"""In-process job manager backing the web UI.

Owns the queue of jobs, the shared weighted encode scheduler, and a simple
pub/sub bus so SSE clients get live updates. Each job is parked at the
title-selection and metadata-confirm gates until the browser submits a
decision, then a background task runs the (non-interactive) rip -> encode ->
subtitle -> place pipeline.
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field

from ..config import Settings
from ..models import DiscInfo, EpisodeAssignment, MediaKind
from ..persistence import (
    JobRecord,
    Store,
    assignment_from_dict,
    assignment_to_dict,
    disc_from_dict,
    disc_to_dict,
    make_store,
)
from ..pipeline import Job, JobState, Pipeline
from ..scheduler import EncodeScheduler
from ..services import library
from ..tools import discinfo
from ..tools.makemkv import Drive


class EventBus:
    """Minimal fan-out: each subscriber gets a queue that receives a tick on
    any state change; the SSE handler then re-reads the snapshot."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self) -> None:
        for q in self._subs:
            q.put_nowait(1)


@dataclass
class WebJob:
    id: int
    source: str
    state: JobState = JobState.QUEUED
    stage: str = ""
    progress: float = 0.0
    log: list[str] = field(default_factory=list)
    error: str | None = None
    disc: DiscInfo | None = None
    fingerprint: str | None = None
    selected_title_ids: list[int] = field(default_factory=list)
    assignments: dict[int, EpisodeAssignment] = field(default_factory=dict)
    outputs: list[str] = field(default_factory=list)
    grain_mode: str = "auto"
    tune_grain: bool | None = None
    preview_off: object = None  # Path to grain-off still
    preview_on: object = None   # Path to grain-on still
    _pipeline_job: object = None  # the underlying pipeline.Job once scanned
    _grain_future: object = None  # asyncio.Future resolved when user picks grain
    _task: object = None          # the asyncio.Task running scan/process (for cancel)


class _Reporter:
    """Routes pipeline on_event/on_progress into a WebJob + publishes updates."""

    def __init__(self, manager: "JobManager", wj: WebJob) -> None:
        self._m = manager
        self._wj = wj

    def on_event(self, message: str) -> None:
        self._wj.log.append(message)
        del self._wj.log[:-200]  # cap log length
        self._m.bus.publish()

    def on_progress(self, stage: str, fraction: float | None) -> None:
        self._wj.stage = stage
        if fraction is not None:
            self._wj.progress = fraction
        self._m.bus.publish()

    def checkpoint(self) -> None:
        """Pipeline reached a durable checkpoint (rip done / output placed)."""
        self._m._persist(self._wj)

    async def resolve_grain(self, job, off_png, on_png) -> bool:
        """Park the job at the grain gate with preview stills; wait for the pick."""
        wj = self._wj
        wj.preview_off = off_png
        wj.preview_on = on_png
        wj.state = JobState.AWAITING_GRAIN
        wj._grain_future = asyncio.get_event_loop().create_future()
        self._m._persist(wj)
        self._m.bus.publish()
        return await wj._grain_future


class JobManager:
    def __init__(self, settings: Settings, store: Store | None = None) -> None:
        self.settings = settings
        self.pipeline = Pipeline(settings)  # no interactive callbacks; web drives gates
        self.scheduler = EncodeScheduler(settings.queue.encode_capacity)
        self.bus = EventBus()
        self.jobs: dict[int, WebJob] = {}
        self.store = store or make_store(settings.database_url)
        self._ids = itertools.count(1)
        self._tasks: set[asyncio.Task] = set()

    # --- persistence / recovery ----------------------------------------

    def _persist(self, wj: WebJob) -> None:
        try:
            self.store.save(self._to_record(wj))
        except Exception as exc:  # never let a persistence hiccup kill a job
            wj.log.append(f"persist failed: {exc}")

    def _to_record(self, wj: WebJob) -> JobRecord:
        job = wj._pipeline_job
        checkpoints = {str(k): v for k, v in (job.checkpoints.items() if job else {})}
        return JobRecord(
            id=wj.id,
            source=wj.source,
            state=self._live_state(wj).value,
            fingerprint=wj.fingerprint,
            disc=disc_to_dict(wj.disc),
            selected_title_ids=wj.selected_title_ids,
            assignments=[assignment_to_dict(a) for a in wj.assignments.values()],
            grain_mode=wj.grain_mode,
            tune_grain=wj.tune_grain,
            checkpoints=checkpoints,
            outputs=wj.outputs,
            error=wj.error,
        )

    def _from_record(self, rec: JobRecord) -> WebJob:
        wj = WebJob(
            id=rec.id, source=rec.source, state=JobState(rec.state), error=rec.error,
            fingerprint=rec.fingerprint, outputs=list(rec.outputs),
            selected_title_ids=list(rec.selected_title_ids),
            grain_mode=rec.grain_mode, tune_grain=rec.tune_grain,
        )
        wj.disc = disc_from_dict(rec.disc)
        wj.assignments = {a.title_id: a for a in (assignment_from_dict(d) for d in rec.assignments)}
        job = Job(source=rec.source)
        job.disc = wj.disc
        job.fingerprint = rec.fingerprint
        job.selected_title_ids = list(rec.selected_title_ids)
        job.assignments = wj.assignments
        job.grain_mode = rec.grain_mode
        job.tune_grain = rec.tune_grain
        job.checkpoints = {int(k): v for k, v in rec.checkpoints.items()}
        wj._pipeline_job = job
        return wj

    def recover(self) -> int:
        """Load persisted jobs and resume any that were mid-flight. Returns the
        number of jobs re-queued for resumption."""
        self.store.init()
        records = self.store.load_all()
        max_id = 0
        resumed = 0
        parked = (JobState.DONE, JobState.FAILED,
                  JobState.AWAITING_TITLES, JobState.AWAITING_MAPPING)
        for rec in records:
            wj = self._from_record(rec)
            self.jobs[wj.id] = wj
            max_id = max(max_id, wj.id)
            if wj.state not in parked:
                # Was running (or parked at the ephemeral grain gate): resume.
                wj.state = JobState.READY
                wj.progress = 0.0
                wj.log.append("Recovered after restart — resuming.")
                wj._task = self._spawn(self._run_job(wj))
                resumed += 1
        if max_id:
            self._ids = itertools.count(max_id + 1)
        self.bus.publish()
        return resumed

    # --- queries --------------------------------------------------------

    async def list_drives(self) -> list[Drive]:
        return await self.pipeline.makemkv.list_drives()

    def snapshot(self) -> dict:
        return {"jobs": [self._job_dict(j) for j in self.jobs.values()]}

    # --- commands -------------------------------------------------------

    def add_disc(self, source: str) -> WebJob:
        wj = WebJob(id=next(self._ids), source=source)
        self.jobs[wj.id] = wj
        self._persist(wj)
        wj._task = self._spawn(self._scan_job(wj))
        self.bus.publish()
        return wj

    def set_titles(self, job_id: int, title_ids: list[int]) -> None:
        wj = self.jobs[job_id]
        if wj.state is not JobState.AWAITING_TITLES:
            raise ValueError(f"Job {job_id} is not awaiting title selection.")
        if not title_ids:
            raise ValueError("No titles selected.")
        wj.selected_title_ids = title_ids
        wj.state = JobState.AWAITING_MAPPING
        self._persist(wj)
        self.bus.publish()

    def set_mapping(
        self, job_id: int, assignments: list[EpisodeAssignment],
        grain_mode: str = "auto", tune_grain: bool | None = None,
    ) -> None:
        wj = self.jobs[job_id]
        if wj.state not in (JobState.AWAITING_MAPPING, JobState.AWAITING_TITLES):
            raise ValueError(f"Job {job_id} is not awaiting a naming mapping.")
        wj.assignments = {a.title_id: a for a in assignments}
        wj.selected_title_ids = [a.title_id for a in assignments]
        wj.grain_mode = grain_mode
        wj.tune_grain = tune_grain
        wj.state = JobState.READY
        self._persist(wj)
        self.bus.publish()
        wj._task = self._spawn(self._run_job(wj))

    def set_grain(self, job_id: int, tune_grain: bool) -> None:
        wj = self.jobs[job_id]
        fut = wj._grain_future
        if wj.state is not JobState.AWAITING_GRAIN or fut is None or fut.done():
            raise ValueError(f"Job {job_id} is not awaiting a grain decision.")
        fut.set_result(tune_grain)
        self._persist(wj)
        self.bus.publish()

    async def remove(self, job_id: int) -> None:
        """Remove any job from the queue. If it's running (or parked at the grain
        gate), cancel its task — which kills the active rip/encode subprocess —
        then clean up its staging dir and drop it from the store."""
        wj = self.jobs.get(job_id)
        if wj is None:
            return
        task = wj._task
        if task is not None and not task.done():
            wj.log.append("Removing — cancelling in-flight work…")
            self.bus.publish()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        library.cleanup(self.settings.storage.staging_dir / f"job_{wj.id}")
        self.jobs.pop(job_id, None)
        self.store.delete(job_id)
        self.bus.publish()

    # --- internals ------------------------------------------------------

    async def _scan_job(self, wj: WebJob) -> None:
        reporter = _Reporter(self, wj)
        try:
            job = await self.pipeline.scan_disc(wj.source, reporter)
            wj._pipeline_job = job
            wj.disc = job.disc
            wj.fingerprint = job.fingerprint
            if job.auto_mapped:
                wj.assignments = job.assignments
                wj.selected_title_ids = job.selected_title_ids
                wj.state = JobState.READY
                self._persist(wj)
                self.bus.publish()
                wj._task = self._spawn(self._run_job(wj))
            else:
                wj.state = JobState.AWAITING_TITLES
                self._persist(wj)
                self.bus.publish()
        except Exception as exc:
            wj.state = JobState.FAILED
            wj.error = f"{type(exc).__name__}: {exc}"
            reporter.on_event(f"Scan failed: {wj.error}")
            self._persist(wj)
            self.bus.publish()

    async def _run_job(self, wj: WebJob) -> None:
        reporter = _Reporter(self, wj)
        job = wj._pipeline_job
        job.selected_title_ids = wj.selected_title_ids
        job.assignments = wj.assignments
        job.grain_mode = wj.grain_mode
        job.tune_grain = wj.tune_grain
        try:
            await self.pipeline.execute(job, self.scheduler, reporter, job_name=f"job_{wj.id}")
            wj.state = JobState.DONE
            wj.progress = 1.0
            wj.outputs = [str(r.video_path) for r in job.results if r.video_path]
        except Exception as exc:
            wj.state = JobState.FAILED
            wj.error = f"{type(exc).__name__}: {exc}"
        self._persist(wj)
        self.bus.publish()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    _PARKED = (
        JobState.DONE, JobState.FAILED, JobState.AWAITING_TITLES,
        JobState.AWAITING_MAPPING, JobState.AWAITING_GRAIN,
    )

    def _live_state(self, wj: WebJob) -> JobState:
        # The pipeline mutates its Job.state during execute; surface it unless
        # the web job is at a parked gate (which the pipeline doesn't know about).
        if wj._pipeline_job is not None and wj.state not in self._PARKED:
            return getattr(wj._pipeline_job, "state", wj.state)
        return wj.state

    def _job_dict(self, wj: WebJob) -> dict:
        live_state = self._live_state(wj)
        return {
            "id": wj.id,
            "source": wj.source,
            "state": live_state.value if hasattr(live_state, "value") else str(live_state),
            "stage": wj.stage,
            "progress": round(wj.progress, 4),
            "error": wj.error,
            "disc_name": wj.disc.disc_name if wj.disc else None,
            "kind_guess": (discinfo.guess_kind(wj.disc).value if wj.disc else None),
            "titles": self._title_dict(wj) if wj.disc else [],
            "selected": wj.selected_title_ids,
            "outputs": wj.outputs,
            "preview": (
                {"off": f"/api/jobs/{wj.id}/preview/off", "on": f"/api/jobs/{wj.id}/preview/on"}
                if live_state == JobState.AWAITING_GRAIN and wj.preview_off else None
            ),
            "log": wj.log[-20:],
        }

    def _title_dict(self, wj: WebJob) -> list[dict]:
        assert wj.disc is not None
        suggested = self._suggested_titles(wj.disc)
        return [
            {
                "id": t.title_id,
                "duration": t.duration_hhmmss,
                "seconds": t.duration_seconds,
                "chapters": t.chapter_count,
                "size_gb": round(t.size_bytes / 1e9, 1),
                "playlist": t.source_filename,
                "audio": t.audio_languages,
                "subs": t.subtitle_languages,
                "suggested": t.title_id in suggested,
            }
            for t in discinfo.ordered_titles(wj.disc)
        ]

    @staticmethod
    def _suggested_titles(disc: DiscInfo) -> set[int]:
        if discinfo.guess_kind(disc) is MediaKind.MOVIE:
            mf = discinfo.main_feature(disc)
            return {mf.title_id} if mf else set()
        return {t.title_id for t in discinfo.episode_candidate_titles(disc)}
