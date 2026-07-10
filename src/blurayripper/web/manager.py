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
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from ..models import DiscInfo, EpisodeAssignment, MediaKind, Stream, StreamType, Title
from ..persistence import (
    JobRecord,
    Store,
    assignment_from_dict,
    assignment_to_dict,
    disc_from_dict,
    disc_to_dict,
    make_store,
)
from ..pipeline import Job, JobState, Pipeline, _resolve_path
from ..scheduler import EncodeScheduler
from ..services import library
from ..services.drive_names import DriveNames
from ..services.opensubtitles import OpenSubtitlesClient, gather_references
from ..services.sub_match import identify
from ..tools import discinfo
from ..tools.makemkv import Drive
from ..version import get_version


_STATUS_CONTROL_C_EXIT = 3221225786  # 0xC000013A — Windows process terminated


def _looks_interrupted(exc: Exception) -> bool:
    """True if a failure looks like a killed subprocess (server shutdown) rather
    than a genuine tool error — so the job should resume, not be marked failed."""
    rc = getattr(getattr(exc, "result", None), "returncode", None)
    return rc == _STATUS_CONTROL_C_EXIT or (isinstance(rc, int) and rc < 0)


def _error_looks_interrupted(error: str | None) -> bool:
    """Same idea, but for a persisted error string (used during recovery to tell
    a shutdown-killed job from a genuinely failed one)."""
    if not error:
        return False
    return (
        str(_STATUS_CONTROL_C_EXIT) in error
        or bool(re.search(r"Command failed \(-\d+\)", error))
    )


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
    # Subtitle-based episode identification (auto-identify scrambled TV discs).
    auto_identify: bool = False
    show_tmdb_id: int | None = None
    season: int | None = None
    candidate_episodes: list[int] = field(default_factory=list)
    show_title: str | None = None
    show_year: int | None = None
    match_candidates: list = field(default_factory=list)  # proposals for AWAITING_MATCH
    preview_off: object = None  # Path to grain-off still (poster / fallback)
    preview_on: object = None   # Path to grain-on still
    preview_off_clip: object = None  # Path to grain-off playable clip (mp4)
    preview_on_clip: object = None   # Path to grain-on playable clip
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

    async def resolve_grain(self, job, preview) -> bool:
        """Park the job at the grain gate with preview assets; wait for the pick."""
        wj = self._wj
        wj.preview_off = preview.off_png
        wj.preview_on = preview.on_png
        wj.preview_off_clip = preview.off_clip
        wj.preview_on_clip = preview.on_clip
        wj.state = JobState.AWAITING_GRAIN
        wj._grain_future = asyncio.get_event_loop().create_future()
        self._m._persist(wj)
        self._m.bus.publish()
        chosen = await wj._grain_future
        # Gate resolved — hand the live state back to the pipeline. Without this,
        # wj.state stays AWAITING_GRAIN (a parked state), so _live_state keeps
        # reporting "awaiting grain" through the whole encode and set_grain
        # rejects further clicks ("not awaiting a grain decision").
        wj._grain_future = None
        wj.state = getattr(job, "state", JobState.ENCODING)
        self._m.bus.publish()
        return chosen


class JobManager:
    def __init__(self, settings: Settings, store: Store | None = None) -> None:
        self.settings = settings
        self.pipeline = Pipeline(settings)  # no interactive callbacks; web drives gates
        self.osub = OpenSubtitlesClient(
            settings.opensubtitles_api_key,
            settings.opensubtitles_username,
            settings.opensubtitles_password,
            cache_dir=settings.storage.staging_dir / "os_ref_cache",
        )
        self.scheduler = EncodeScheduler(settings.queue.encode_capacity)
        self.bus = EventBus()
        self.jobs: dict[int, WebJob] = {}
        self.store = store or make_store(settings.database_url)
        self.drive_names = DriveNames(
            settings.storage.output_dir / ".blurayripper" / "drive_names.json")
        self.closing = False
        self._ids = itertools.count(1)
        self._tasks: set[asyncio.Task] = set()
        self._drives_cache: list[Drive] = []
        self._last_disc: dict[int, str] = {}  # drive index -> last auto-opened disc name
        self._last_drive_error: str | None = None

    async def shutdown(self) -> None:
        """Stop cleanly: wake SSE streams, then cancel running job tasks (which
        kills their rip/encode subprocesses) so the process can exit on Ctrl+C."""
        self.closing = True
        self.bus.publish()  # let SSE generators notice and close
        tasks = [t for t in list(self._tasks) if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def set_drive_name(self, device: str, name: str | None) -> None:
        self.drive_names.set(device, name)

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
            stage=wj.stage,
            progress=round(wj.progress, 4),
            log=wj.log[-30:],
            auto_identify=wj.auto_identify,
            show_tmdb_id=wj.show_tmdb_id,
            season=wj.season,
            candidate_episodes=list(wj.candidate_episodes),
            show_title=wj.show_title,
            show_year=wj.show_year,
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
        wj.stage, wj.progress = rec.stage, rec.progress
        wj.log = list(rec.log)
        wj.auto_identify = rec.auto_identify
        wj.show_tmdb_id, wj.season = rec.show_tmdb_id, rec.season
        wj.candidate_episodes = list(rec.candidate_episodes)
        wj.show_title, wj.show_year = rec.show_title, rec.show_year
        job = Job(source=rec.source)
        job.disc = wj.disc
        job.fingerprint = rec.fingerprint
        job.selected_title_ids = list(rec.selected_title_ids)
        job.assignments = wj.assignments
        job.grain_mode = rec.grain_mode
        job.tune_grain = rec.tune_grain
        job.checkpoints = {int(k): v for k, v in rec.checkpoints.items()}
        try:
            job.state = JobState(rec.state)  # so live-state matches (not default QUEUED)
        except ValueError:
            pass
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
        # Rip-box work (rip + subtitles + grain). In clustered mode the rip box
        # ONLY re-runs these; READY / worker-owned states are left for workers to
        # claim from the store (so we don't stampede re-prepping everything).
        prep_states = [JobState.SCANNING, JobState.SELECTING, JobState.RIPPING,
                       JobState.AWAITING_GRAIN, JobState.IDENTIFYING]
        # When workers own subtitle work, EXTRACTING_SUBS / OCR_SUBS are worker
        # states — leave them for a worker to reclaim, don't re-prep on the rip box.
        if not self.settings.cluster.worker_does_subs:
            prep_states += [JobState.EXTRACTING_SUBS, JobState.OCR_SUBS]
        prep_states = tuple(prep_states)
        clustered = self.settings.cluster.use_workers
        for rec in records:
            wj = self._from_record(rec)
            self.jobs[wj.id] = wj
            max_id = max(max_id, wj.id)
            # Resume jobs that were mid-flight, plus FAILED jobs whose failure was
            # actually a killed process (server shutdown), not a real error.
            interrupted_fail = (
                wj.state is JobState.FAILED and _error_looks_interrupted(wj.error)
            )
            # An auto-identify job that hasn't been identified yet must re-run on
            # the rip box (workers can't rip/OCR/match), even at READY.
            needs_identify = wj.auto_identify and not wj.assignments
            if clustered:
                if wj.state in prep_states or (needs_identify and wj.state is JobState.READY):
                    self._resume(wj)
                    resumed += 1
                elif interrupted_fail:
                    # Make it claimable again; a worker reclaims it.
                    wj.state = JobState.READY
                    wj.error = None
                    if wj._pipeline_job is not None:
                        wj._pipeline_job.state = JobState.READY
                    self._persist(wj)
                # else READY / ENCODING / PLACING -> leave for workers
            elif wj.state not in parked or interrupted_fail:
                self._resume(wj)
                resumed += 1
        if max_id:
            self._ids = itertools.count(max_id + 1)
        self.bus.publish()
        return resumed

    def _resume(self, wj: WebJob) -> None:
        wj.state = JobState.READY
        wj.progress = 0.0
        wj.error = None
        wj.log.append("Recovered after restart — resuming.")
        wj._task = self._spawn(self._run_job(wj))

    # --- queries --------------------------------------------------------

    async def list_drives(self) -> list[Drive]:
        """Live enumeration (used by the background watcher)."""
        return await self.pipeline.makemkv.list_drives()

    def drives(self) -> list[Drive]:
        """Cached drives (populated by the watcher); fast, no per-request scan."""
        return self._drives_cache

    def start_drive_watch(self) -> None:
        self._spawn(self._watch_drives())

    def start_store_refresh(self) -> None:
        # Only needed when workers process jobs elsewhere; then the store is the
        # source of truth for their progress.
        if self.settings.cluster.use_workers and self.store.durable:
            self._spawn(self._refresh_from_store())

    _WORKER_OWNED = (
        JobState.READY, JobState.ENCODING, JobState.EXTRACTING_SUBS,
        JobState.OCR_SUBS, JobState.PLACING, JobState.DONE, JobState.FAILED,
    )

    async def _refresh_from_store(self) -> None:
        while not self.closing:
            try:
                records = {r.id: r for r in self.store.load_all()}
                changed = False
                for wj in list(self.jobs.values()):
                    rec = records.get(wj.id)
                    if rec is None or self._live_state(wj) not in self._WORKER_OWNED:
                        continue  # locally-owned (ripping/awaiting) — don't clobber
                    try:
                        new_state = JobState(rec.state)
                    except ValueError:
                        continue
                    wj.state = new_state
                    if wj._pipeline_job is not None:
                        wj._pipeline_job.state = new_state
                    wj.progress, wj.stage, wj.error = rec.progress, rec.stage, rec.error
                    if rec.log:
                        wj.log = list(rec.log)
                    if rec.outputs:
                        wj.outputs = list(rec.outputs)
                    changed = True
                if changed:
                    self.bus.publish()
            except Exception:
                pass
            await asyncio.sleep(self.settings.cluster.worker_poll_seconds)

    async def _watch_drives(self) -> None:
        while not self.closing:
            try:
                self._drives_cache = await self.list_drives()
                self._last_drive_error = None
                if self.settings.auto_open:
                    self._auto_open(self._drives_cache)
            except Exception as exc:
                # Keep the last cache; log the cause once (not every tick).
                msg = f"{type(exc).__name__}: {exc}"
                if msg != self._last_drive_error:
                    print(f"[drives] scan failed: {msg}")
                    self._last_drive_error = msg
            await asyncio.sleep(self.settings.drive_poll_seconds)

    def _auto_open(self, drives: list[Drive]) -> None:
        present = set()
        for d in drives:
            present.add(d.index)
            name = (d.disc_name or "").strip()
            if not name:
                continue  # no disc / not yet readable
            source = f"disc:{d.index}"
            if self._last_disc.get(d.index) == name or self._has_active_job(source):
                self._last_disc[d.index] = name  # already handled / in progress
                continue
            self._last_disc[d.index] = name
            self.add_disc(source)  # new disc -> scan it
        # Forget drives that are gone or now empty, so reinserting re-triggers.
        empty = {d.index for d in drives if not (d.disc_name or "").strip()}
        for idx in list(self._last_disc):
            if idx not in present or idx in empty:
                self._last_disc.pop(idx, None)

    def _has_active_job(self, source: str) -> bool:
        return any(
            wj.source == source and self._live_state(wj) not in (JobState.DONE, JobState.FAILED)
            for wj in self.jobs.values()
        )

    def snapshot(self) -> dict:
        jobs = []
        for j in self.jobs.values():
            try:
                jobs.append(self._job_dict(j))
            except Exception as exc:  # one bad job must not break the whole stream
                print(f"[snapshot] job {j.id} render failed: {type(exc).__name__}: {exc}")
                jobs.append({
                    "id": j.id, "source": j.source, "state": "failed", "stage": "",
                    "progress": 0, "error": f"render error: {exc}", "disc_name": None,
                    "kind_guess": None, "titles": [], "selected": [], "outputs": [],
                    "preview": None, "log": [],
                })
        return {"jobs": jobs, "version": get_version()}

    # --- commands -------------------------------------------------------

    def add_source(self, source: str) -> list[int]:
        """Add a disc selector (disc:/dev:/file:/iso:), a single .mkv file, or a
        directory of .mkv files. Returns the created job id(s)."""
        raw = source[4:] if source.startswith("mkv:") else source
        path = Path(raw)
        if path.is_dir():
            mkvs = sorted(path.glob("*.mkv"))
            if not mkvs:
                raise ValueError(f"No .mkv files found in {path}")
            return [self._add_mkv(m).id for m in mkvs]
        if path.suffix.lower() == ".mkv" and path.is_file():
            return [self._add_mkv(path).id]
        return [self.add_disc(source).id]

    def add_disc(self, source: str) -> WebJob:
        wj = WebJob(id=next(self._ids), source=source)
        self.jobs[wj.id] = wj
        self._persist(wj)
        wj._task = self._spawn(self._scan_job(wj))
        self.bus.publish()
        return wj

    def _add_mkv(self, path: Path) -> WebJob:
        wj = WebJob(id=next(self._ids), source=str(path))
        self.jobs[wj.id] = wj
        self._persist(wj)
        wj._task = self._spawn(self._scan_mkv(wj, path))
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

    def retry(self, job_id: int) -> None:
        """Re-queue a failed job. It resumes from its checkpoints — a finished
        rip is reused, so typically only the encode (and later stages) re-run."""
        wj = self.jobs[job_id]
        # Check the LIVE state (a failed job's pipeline_job.state is FAILED too),
        # so a second click while it's already re-queued is rejected instead of
        # spawning a duplicate task.
        if self._live_state(wj) is not JobState.FAILED:
            raise ValueError(f"Job {job_id} is not failed.")
        if wj._pipeline_job is None:
            raise ValueError(f"Job {job_id} cannot be retried (no scan state).")
        wj.error = None
        wj.progress = 0.0
        wj.state = JobState.READY
        # Also clear the pipeline job's FAILED state, or _live_state keeps
        # reporting "failed" and the retry looks like it did nothing.
        wj._pipeline_job.state = JobState.READY
        wj.log.append("Retrying…")
        self._persist(wj)
        self.bus.publish()
        wj._task = self._spawn(self._run_job(wj))

    def edit_job(self, job_id: int, grain_mode: str | None = None,
                 tune_grain: bool | None = None, edition: str | None = None) -> None:
        """Adjust a queued/running job before it encodes: grain and (movie)
        edition. Takes effect when a worker claims it (clustered) or before the
        encode reads it; ignored once the title has been placed."""
        wj = self.jobs[job_id]
        if self._live_state(wj) is JobState.DONE:
            raise ValueError("Job already finished.")
        job = wj._pipeline_job
        if grain_mode is not None:
            wj.grain_mode = grain_mode
            wj.tune_grain = tune_grain
            if job is not None:
                job.grain_mode = grain_mode
                job.tune_grain = tune_grain
                # Override the per-title checkpoint grain so the encode honors it
                # even though prep already resolved a value.
                if tune_grain is not None:
                    for cp in job.checkpoints.values():
                        cp["tune_grain"] = tune_grain
        if edition is not None:
            for a in wj.assignments.values():
                if a.kind is MediaKind.MOVIE:
                    a.edition = edition or None
        self._persist(wj)
        self.bus.publish()

    def set_auto_identify(
        self, job_id: int, show_title: str, show_year: int | None, show_tmdb_id: int,
        season: int, episodes: list[int], title_ids: list[int],
        grain_mode: str = "auto", tune_grain: bool | None = None,
    ) -> None:
        """Queue a TV disc for subtitle-based episode identification: rip the
        selected titles, then match each against the given candidate episodes."""
        wj = self.jobs[job_id]
        if wj.state not in (JobState.AWAITING_MAPPING, JobState.AWAITING_TITLES):
            raise ValueError(f"Job {job_id} is not awaiting a naming mapping.")
        if not self.osub.configured:
            raise ValueError(
                "OpenSubtitles API key not configured — needed for subtitle "
                "identification. Set OPENSUBTITLES_API_KEY in config/secrets.env.")
        if not episodes:
            raise ValueError("No candidate episodes given for this disc.")
        wj.auto_identify = True
        wj.show_title, wj.show_year = show_title, show_year
        wj.show_tmdb_id, wj.season = show_tmdb_id, season
        wj.candidate_episodes = list(episodes)
        wj.selected_title_ids = list(title_ids)
        wj.assignments = {}
        wj.grain_mode, wj.tune_grain = grain_mode, tune_grain
        wj.state = JobState.READY
        self._persist(wj)
        wj._task = self._spawn(self._run_job(wj))
        self.bus.publish()

    def confirm_match(self, job_id: int, mapping: dict[int, int]) -> None:
        """Resolve an AWAITING_MATCH job: ``mapping`` is title_id -> episode for
        every selected title. Builds assignments and resumes to encode/place."""
        wj = self.jobs[job_id]
        if wj.state is not JobState.AWAITING_MATCH:
            raise ValueError(f"Job {job_id} is not awaiting match confirmation.")
        wj.assignments = {
            tid: EpisodeAssignment(
                title_id=tid, kind=MediaKind.TV, show_or_movie_title=wj.show_title or "",
                year=wj.show_year, season=wj.season, episode=ep,
            )
            for tid, ep in mapping.items()
        }
        wj.selected_title_ids = list(mapping.keys())
        wj.match_candidates = []
        wj.state = JobState.READY
        self._persist(wj)
        wj._task = self._spawn(self._run_job(wj))
        self.bus.publish()

    async def _auto_identify(self, wj: WebJob, reporter) -> bool:
        """Rip + OCR the selected titles, fetch references, and match. Sets
        assignments on success; returns True if all titles are confidently
        identified (caller proceeds), False if it parked at AWAITING_MATCH."""
        job = wj._pipeline_job
        staging_root = self.settings.storage.staging_dir / f"job_{wj.id}"
        rips = await self.pipeline.identify_phase(job, staging_root, reporter)
        reporter.on_event(
            f"Fetching reference subtitles for episodes {wj.candidate_episodes} …")
        refs = await gather_references(
            self.osub, wj.show_tmdb_id, wj.season, wj.candidate_episodes,
            self.settings.languages.default,
        )
        if not refs:
            raise RuntimeError(
                "Could not fetch any reference subtitles from OpenSubtitles for "
                "these episodes — check the show/season and your API key.")
        matches = identify(rips, refs)
        wj.match_candidates = [
            {"title_id": m.title_id, "episode": m.episode, "score": round(m.score, 3),
             "runner_up": m.runner_up, "runner_up_score": round(m.runner_up_score, 3),
             "confident": m.confident}
            for m in matches
        ]
        for m in matches:
            reporter.on_event(
                f"title {m.title_id} -> E{m.episode} (score {m.score:.2f}"
                + (f", runner-up E{m.runner_up} {m.runner_up_score:.2f}" if m.runner_up else "")
                + (")" if m.confident else ") — needs confirmation"))
        if all(m.confident and m.episode is not None for m in matches):
            wj.assignments = {
                m.title_id: EpisodeAssignment(
                    title_id=m.title_id, kind=MediaKind.TV,
                    show_or_movie_title=wj.show_title or "", year=wj.show_year,
                    season=wj.season, episode=m.episode,
                )
                for m in matches
            }
            wj.selected_title_ids = [m.title_id for m in matches]
            return True
        wj.state = JobState.AWAITING_MATCH
        reporter.on_event("Subtitle match needs confirmation — review in the UI.")
        return False

    # Re-OCR is background maintenance — refuse it while a job is actively being
    # ripped/subbed/encoded (by this box or a worker), so it can't clobber the
    # live state badge or race the worker that owns the job.
    _REDO_BUSY = (
        JobState.SCANNING, JobState.SELECTING, JobState.CONFIRMING, JobState.RIPPING,
        JobState.EXTRACTING_SUBS, JobState.OCR_SUBS, JobState.IDENTIFYING,
        JobState.ENCODING, JobState.PLACING,
    )

    def redo_subtitles(self, job_id: int) -> None:
        """Re-OCR + place subtitles for a job using its staged or cached subs
        (no rip, no encode). Runs in the background; progress shows in the log."""
        wj = self.jobs[job_id]
        if wj._pipeline_job is None or not wj.assignments:
            raise ValueError("Job has no naming assignments to attach subtitles to.")
        if self._live_state(wj) in self._REDO_BUSY:
            raise ValueError("Job is busy (being ripped/encoded) — re-OCR once it's done.")
        wj._task = self._spawn(self._redo_subs_task(wj))
        self.bus.publish()

    def redo_subtitles_all(self) -> int:
        """Re-OCR subtitles for every resting job that has naming assignments, one
        at a time in the background. Skips jobs a worker is actively processing.
        Returns how many jobs were queued."""
        eligible = [wj for wj in self.jobs.values()
                    if wj._pipeline_job is not None and wj.assignments
                    and self._live_state(wj) not in self._REDO_BUSY]
        if eligible:
            self._spawn(self._redo_subs_all_task(eligible))
        return len(eligible)

    async def _redo_subs_all_task(self, jobs: list[WebJob]) -> None:
        for wj in jobs:  # sequential to bound CPU/IO across the batch
            await self._redo_subs_task(wj)

    async def _redo_subs_task(self, wj: WebJob) -> None:
        reporter = _Reporter(self, wj)
        job = wj._pipeline_job
        job.selected_title_ids = wj.selected_title_ids
        job.assignments = wj.assignments
        staging_root = self.settings.storage.staging_dir / f"job_{wj.id}"
        # redo_subtitles flips job.state to OCR_SUBS for progress; restore it after
        # so a maintenance re-OCR never leaves a stray state on a resting job.
        prior_state = job.state
        try:
            reporter.on_event("Re-OCR subtitles: looking for staged/cached subs …")
            placed = await self.pipeline.redo_subtitles(job, staging_root, reporter)
            reporter.on_event(
                f"Re-OCR complete — placed {len(placed)} subtitle sidecar(s)." if placed
                else "Re-OCR found no staged or cached subtitles — a re-rip is needed.")
        except Exception as exc:
            reporter.on_event(f"Re-OCR failed: {type(exc).__name__}: {exc}")
        finally:
            job.state = prior_state
        self._persist(wj)
        self.bus.publish()

    async def generate_preview(self, job_id: int) -> None:
        """Render the grain A/B preview clips on demand for a queued job (so jobs
        don't have to park at a preview gate). Stores clips (+ optional poster
        stills) on the job. Clips come from HandBrake; ffmpeg is only needed for
        the posters, so it's not required."""
        wj = self.jobs[job_id]
        job = wj._pipeline_job
        if job is None or not wj.selected_title_ids:
            raise ValueError("Job is not ready for a preview yet.")
        title_id = wj.selected_title_ids[0]
        cp = job.checkpoints.get(title_id, {})
        prior = cp.get("ripped_path")
        ripped = _resolve_path(self.settings.storage.staging_dir, prior) if prior else None
        if ripped is None or not ripped.exists():
            raise ValueError("Ripped file not available for preview.")
        reporter = _Reporter(self, wj)
        preview_dir = self.settings.storage.staging_dir / f"job_{wj.id}" / "preview"
        preview = await self.pipeline._generate_grain_preview(
            job, ripped, title_id, preview_dir, reporter)
        wj.preview_off, wj.preview_on = preview.off_png, preview.on_png
        wj.preview_off_clip, wj.preview_on_clip = preview.off_clip, preview.on_clip
        self.bus.publish()

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
        try:
            self.store.delete(job_id)
        except Exception as exc:  # a down DB must not fail the in-memory removal
            print(f"[store] delete job {job_id} failed: {type(exc).__name__}: {exc}")
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
                # A movie disc has a single main-feature title — skip the title
                # gate and go straight to naming. TV discs (multiple candidate
                # episodes) still stop so you can pick which titles to rip.
                suggested = self._suggested_titles(job.disc)
                if len(suggested) == 1:
                    wj.selected_title_ids = [next(iter(suggested))]
                    wj.state = JobState.AWAITING_MAPPING
                    reporter.on_event("Single title — auto-selected; confirm the name.")
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

    async def _scan_mkv(self, wj: WebJob, path: Path) -> None:
        """Set up a job from an existing .mkv: probe it, build a one-title disc,
        and pre-set the rip checkpoint so the pipeline skips ripping and goes
        straight to naming -> encode -> subtitles."""
        reporter = _Reporter(self, wj)
        try:
            reporter.on_event(f"Reading {path.name} …")
            try:
                probe = await self.pipeline.mkvtools.probe(path)
                audio_langs, subs, duration = probe.audio_languages, probe.subtitles, probe.duration_seconds
            except Exception as exc:
                reporter.on_event(f"Probe failed ({exc}); continuing with minimal info.")
                audio_langs, subs, duration = [], [], 0

            streams = [Stream(stream_id=0, stream_type=StreamType.VIDEO)]
            sid = 1
            for lang in audio_langs:
                streams.append(Stream(stream_id=sid, stream_type=StreamType.AUDIO, language=lang))
                sid += 1
            for s in subs:
                streams.append(Stream(stream_id=sid, stream_type=StreamType.SUBTITLE, language=s.language))
                sid += 1

            size = path.stat().st_size if path.exists() else 0
            title = Title(title_id=0, duration_seconds=duration, size_bytes=size, streams=streams)
            disc = DiscInfo(source=str(path), disc_name=path.stem, titles=[title])

            job = Job(source=str(path))
            job.disc = disc
            job.selected_title_ids = [0]
            job.checkpoints = {0: {"ripped_path": str(path)}}  # skip rip: reuse this file

            wj._pipeline_job = job
            wj.disc = disc
            wj.selected_title_ids = [0]
            wj.state = JobState.AWAITING_MAPPING  # one title; go straight to naming
            self._persist(wj)
            self.bus.publish()
        except Exception as exc:
            wj.state = JobState.FAILED
            wj.error = f"{type(exc).__name__}: {exc}"
            reporter.on_event(f"Setup failed: {wj.error}")
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
            if wj.auto_identify and not wj.assignments:
                # Rip + OCR + subtitle-match against the disc's candidate episodes.
                # Parks at AWAITING_MATCH if any title is uncertain.
                if not await self._auto_identify(wj, reporter):
                    self._persist(wj)
                    self.bus.publish()
                    return
                job.assignments = wj.assignments
                job.selected_title_ids = wj.selected_title_ids
            if self.settings.cluster.use_workers:
                # Clustered: rip + subtitles + grain here. Then either encode
                # locally (if every title is <= the configured height, e.g. 1080p)
                # or enqueue for a worker to do the heavy (UHD) encode.
                staging_root = self.settings.storage.staging_dir / f"job_{wj.id}"
                include_subs = not self.settings.cluster.worker_does_subs
                await self.pipeline.prep_phase(job, staging_root, reporter, include_subs=include_subs)
                self.pipeline._persist_mapping(job, reporter)
                limit = self.settings.cluster.local_encode_max_height
                heights = (
                    await self.pipeline.title_heights(job, staging_root, reporter)
                    if limit else []
                )
                encode_here = bool(limit) and bool(heights) and all(0 < h <= limit for h in heights)
                if not encode_here:
                    job.state = JobState.READY
                    wj.state = JobState.READY  # claimable by a worker
                    reporter.on_event("Ripped + subtitles done — queued for an encode worker.")
                    self._persist(wj)
                    self.bus.publish()
                    return
                reporter.on_event(
                    f"Ripped + subtitles done — encoding locally ({max(heights)}p <= {limit}p).")
                results = await self.pipeline.encode_phase(
                    job, staging_root, self.scheduler, reporter)
                library.cleanup(staging_root)
                wj.state = JobState.DONE
                wj.progress = 1.0
                wj.outputs = [str(r.video_path) for r in results if r.video_path]
                self._persist(wj)
                self.bus.publish()
                return
            await self.pipeline.execute(job, self.scheduler, reporter, job_name=f"job_{wj.id}")
            wj.state = JobState.DONE
            wj.progress = 1.0
            wj.outputs = [str(r.video_path) for r in job.results if r.video_path]
        except Exception as exc:
            if _looks_interrupted(exc):
                # A tool was terminated (e.g. the server stopped mid-encode), not
                # a real failure. Park as resumable so recovery picks it back up
                # — its finished rip is reused, only the encode re-runs.
                wj.state = JobState.READY
                wj.error = None
                wj.log.append("Interrupted (server stopping?) — will resume on restart.")
            else:
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
        JobState.AWAITING_MAPPING, JobState.AWAITING_GRAIN, JobState.AWAITING_MATCH,
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
                {
                    # Poster URLs only when ffmpeg produced the stills.
                    "off": (f"/api/jobs/{wj.id}/preview/off" if wj.preview_off else None),
                    "on": (f"/api/jobs/{wj.id}/preview/on" if wj.preview_on else None),
                    "off_clip": (f"/api/jobs/{wj.id}/preview-clip/off"
                                 if wj.preview_off_clip else None),
                    "on_clip": (f"/api/jobs/{wj.id}/preview-clip/on"
                                if wj.preview_on_clip else None),
                }
                # Gate on the CLIPS (the real preview), not the optional posters,
                # so a ready preview shows even when ffmpeg isn't installed.
                if live_state == JobState.AWAITING_GRAIN and wj.preview_off_clip else None
            ),
            "match": (
                {"show": wj.show_title, "season": wj.season,
                 "episodes": wj.candidate_episodes, "candidates": wj.match_candidates}
                if live_state == JobState.AWAITING_MATCH else None
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
