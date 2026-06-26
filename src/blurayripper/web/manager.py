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
from ..pipeline import JobState, Pipeline
from ..scheduler import EncodeScheduler
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
    _pipeline_job: object = None  # the underlying pipeline.Job once scanned


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


class JobManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.pipeline = Pipeline(settings)  # no interactive callbacks; web drives gates
        self.scheduler = EncodeScheduler(settings.queue.encode_capacity)
        self.bus = EventBus()
        self.jobs: dict[int, WebJob] = {}
        self._ids = itertools.count(1)
        self._tasks: set[asyncio.Task] = set()

    # --- queries --------------------------------------------------------

    async def list_drives(self) -> list[Drive]:
        return await self.pipeline.makemkv.list_drives()

    def snapshot(self) -> dict:
        return {"jobs": [self._job_dict(j) for j in self.jobs.values()]}

    # --- commands -------------------------------------------------------

    def add_disc(self, source: str) -> WebJob:
        wj = WebJob(id=next(self._ids), source=source)
        self.jobs[wj.id] = wj
        self._spawn(self._scan_job(wj))
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
        self.bus.publish()

    def set_mapping(self, job_id: int, assignments: list[EpisodeAssignment]) -> None:
        wj = self.jobs[job_id]
        if wj.state not in (JobState.AWAITING_MAPPING, JobState.AWAITING_TITLES):
            raise ValueError(f"Job {job_id} is not awaiting a naming mapping.")
        wj.assignments = {a.title_id: a for a in assignments}
        wj.selected_title_ids = [a.title_id for a in assignments]
        wj.state = JobState.READY
        self.bus.publish()
        self._spawn(self._run_job(wj))

    def remove(self, job_id: int) -> None:
        wj = self.jobs.get(job_id)
        if wj and wj.state in (JobState.DONE, JobState.FAILED,
                               JobState.AWAITING_TITLES, JobState.AWAITING_MAPPING):
            del self.jobs[job_id]
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
                self.bus.publish()
                self._spawn(self._run_job(wj))
            else:
                wj.state = JobState.AWAITING_TITLES
                self.bus.publish()
        except Exception as exc:
            wj.state = JobState.FAILED
            wj.error = f"{type(exc).__name__}: {exc}"
            reporter.on_event(f"Scan failed: {wj.error}")
            self.bus.publish()

    async def _run_job(self, wj: WebJob) -> None:
        reporter = _Reporter(self, wj)
        job = wj._pipeline_job
        job.selected_title_ids = wj.selected_title_ids
        job.assignments = wj.assignments
        try:
            await self.pipeline.execute(job, self.scheduler, reporter, job_name=f"job_{wj.id}")
            wj.state = JobState.DONE
            wj.progress = 1.0
            wj.outputs = [str(r.video_path) for r in job.results if r.video_path]
        except Exception as exc:
            wj.state = JobState.FAILED
            wj.error = f"{type(exc).__name__}: {exc}"
        # Mirror the pipeline's intermediate state onto the web job as it runs.
        self.bus.publish()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        # Reflect live pipeline state transitions onto the web job.

    def _job_dict(self, wj: WebJob) -> dict:
        # The pipeline mutates its Job.state during execute; surface it.
        live_state = wj.state
        if wj._pipeline_job is not None and wj.state not in (
            JobState.DONE, JobState.FAILED, JobState.AWAITING_TITLES, JobState.AWAITING_MAPPING
        ):
            live_state = getattr(wj._pipeline_job, "state", wj.state)
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
