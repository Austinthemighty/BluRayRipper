"""In-process job manager backing the web UI.

Owns the queue of jobs, the shared weighted encode scheduler, and a simple
pub/sub bus so SSE clients get live updates. Each job is parked at the
title-selection and metadata-confirm gates until the browser submits a
decision, then a background task runs the (non-interactive) rip -> encode ->
subtitle -> place pipeline.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import re
import socket
import time
import uuid
from dataclasses import dataclass, field, replace
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
    stamp_log,
)
from ..pipeline import Job, JobState, Pipeline, _resolve_path
from ..scheduler import EncodeScheduler
from .. import boxset_plan, presence
from ..services import library, naming
from ..services.drive_names import DriveNames
from ..services.notify import notify
from ..services.opensubtitles import OpenSubtitlesClient, gather_references
from ..services.sub_match import identify
from ..tools import discinfo
from ..tools.makemkv import Drive, os_media_snapshot
from ..version import get_version


_STATUS_CONTROL_C_EXIT = 3221225786  # 0xC000013A — Windows process terminated


def _drive_index(source: str | None) -> int | None:
    """The makemkvcon drive index from a 'disc:N' selector, else None."""
    src = source or ""
    if not src.startswith("disc:"):
        return None
    try:
        return int(src.split(":", 1)[1])
    except (ValueError, IndexError):
        return None


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
    priority: int = 0  # higher = claimed/encoded sooner ("Encode next")
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
        self._wj.log.append(stamp_log(message))
        del self._wj.log[:-200]  # cap log length
        self._wj._activity_at = time.monotonic()  # feeds the rip-stall watchdog
        self._m.bus.publish()

    def on_progress(self, stage: str, fraction: float | None) -> None:
        self._wj.stage = stage
        if fraction is not None:
            self._wj.progress = fraction
        self._wj._activity_at = time.monotonic()
        self._m.bus.publish()

    def checkpoint(self) -> None:
        """Pipeline reached a durable checkpoint (rip done / output placed)."""
        self._m._persist(self._wj)

    def notify(self, title: str, message: str) -> None:
        """Pipeline asked to push a user notification (e.g. disc finished ripping)."""
        self._m._notify(title, message)

    def ejected(self, device: str | None) -> None:
        """Pipeline auto-ejected the disc — forget the drive's cached disc name so
        the panel doesn't keep showing it and the next disc reads as new."""
        self._m.note_disc_ejected(device=device, index=_drive_index(self._wj.source))

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
        self._m._notify(f"🎚 Grain choice: {self._m._name_of(wj)}",
                        f"Job #{wj.id} — pick grain on/off in the UI.")
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
        # Monotonic so the reported uptime is immune to wall-clock adjustments.
        self._start_monotonic = time.monotonic()
        self._ids = itertools.count(1)
        self._tasks: set[asyncio.Task] = set()
        # Bounds how many jobs prep (rip+subs+grain) at once — so a restart that
        # recovers dozens of in-flight jobs doesn't stampede the box all at once.
        self._prep_sem = asyncio.Semaphore(max(1, settings.prep_concurrency))
        self._auto_retries: dict[int, int] = {}  # job id -> auto-retry count so far
        self._drives_cache_file = (
            settings.storage.output_dir / ".blurayripper" / "drives_cache.json")
        # Seed from the last persisted list so the panel isn't blank right after a
        # restart (and the /api/drives endpoint never has to do a blocking scan
        # that could hang on an unresponsive drive).
        self._drives_cache: list[Drive] = self._load_drives_cache()
        self._last_disc: dict[int, str] = {}  # drive index -> last auto-opened disc name
        self._last_drive_error: str | None = None
        # Cheap OS view of which drives hold media, so a tray closing on a new disc
        # triggers a scan immediately instead of waiting out the full poll interval.
        self._media_snapshot: dict[str, str] | None = None
        self._workers_cache: list = []  # WorkerInfo, refreshed by the store poll
        # Presence: this box heartbeats so workers can spot the server going down,
        # and watches the workers for the reverse. run_token distinguishes a fresh
        # process from a heartbeat, so restarting resets the uptime clock.
        self._hostname = socket.gethostname()
        self._run_token = uuid.uuid4().hex
        self._worker_watch = presence.PresenceWatch(
            threshold_seconds=settings.cluster.offline_alert_seconds)
        self._done_notified: set[int] = set()  # jobs we've pushed a "done" notify for

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
            wj.log.append(stamp_log(f"persist failed: {exc}"))

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
            priority=wj.priority,
            stage=wj.stage,
            progress=round(wj.progress, 4),
            log=wj.log[-200:],  # keep the whole retained log so it survives a restart
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
        wj.priority = rec.priority
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
            if wj.state is JobState.DONE:
                self._done_notified.add(wj.id)  # don't re-notify old history on restart
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
        # Clear the pipeline job's restored in-flight state (e.g. RIPPING) too, or
        # _live_state keeps reporting it until the resumed task advances — same
        # reason retry() resets it. Otherwise a just-recovered job reads as
        # "ripping"/"encoding" with nothing actually running yet.
        if wj._pipeline_job is not None:
            wj._pipeline_job.state = JobState.READY
        wj.log.append(stamp_log("Recovered after restart — resuming."))
        wj._task = self._spawn(self._run_job(wj))

    # --- queries --------------------------------------------------------

    async def list_drives(self) -> list[Drive]:
        """Live enumeration (used by the background watcher)."""
        return await self.pipeline.makemkv.list_drives()

    def drives(self) -> list[Drive]:
        """Cached drives (populated by the watcher); fast, no per-request scan.

        While a rip is in flight makemkvcon can't re-probe the busy drive, so the
        cached enumeration keeps showing the PREVIOUS disc's name. The job actively
        ripping ``disc:N`` is authoritative about what's physically loaded there, so
        overlay its disc name onto that drive.
        """
        active = self._active_drive_discs()
        if not active:
            return self._drives_cache
        out = []
        for d in self._drives_cache:
            name = active.get(d.index)
            out.append(replace(d, disc_name=name) if name else d)
        return out

    # States in which a job still physically holds its source drive (disc not yet
    # released to encode/eject) — makemkvcon is using or has just used the drive.
    _DRIVE_HELD_STATES = (
        JobState.SCANNING, JobState.AWAITING_TITLES, JobState.AWAITING_MAPPING,
        JobState.AWAITING_GRAIN, JobState.AWAITING_MATCH, JobState.IDENTIFYING,
        JobState.READY, JobState.RIPPING, JobState.CONFIRMING,
    )

    def _active_drive_discs(self) -> dict[int, str]:
        """Map ``drive index -> disc name`` for jobs currently holding an optical
        drive, so the panel reflects the disc being worked on, not a stale probe."""
        out: dict[int, str] = {}
        for wj in self.jobs.values():
            if wj.state not in self._DRIVE_HELD_STATES:
                continue
            # READY spans both "queued to rip" (disc still loaded) and "ripped,
            # waiting for a worker" (already auto-ejected). Only the rip lane still
            # owns the disc — otherwise an ejected disc stays pinned to the panel.
            if self._lane(wj, wj.state) != "rip":
                continue
            idx = _drive_index(wj.source)
            if idx is None:
                continue
            name = (wj.disc.disc_name if wj.disc else None) or ""
            if name.strip():
                out[idx] = name.strip()
        return out

    def _load_drives_cache(self) -> list[Drive]:
        try:
            data = json.loads(self._drives_cache_file.read_text(encoding="utf-8"))
            return [Drive(index=d["index"], name=d.get("name", ""),
                          device=d.get("device"), disc_name=d.get("disc_name"))
                    for d in data]
        except (OSError, ValueError, KeyError, TypeError):
            return []

    def _save_drives_cache(self) -> None:
        try:
            self._drives_cache_file.parent.mkdir(parents=True, exist_ok=True)
            self._drives_cache_file.write_text(json.dumps(
                [{"index": d.index, "name": d.name, "device": d.device,
                  "disc_name": d.disc_name} for d in self._drives_cache
                 if not getattr(d, "makemkv_blind", False)]),  # blind drives are live-only
                encoding="utf-8")
        except OSError:
            pass

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
    # States that imply active work is happening RIGHT NOW (unlike READY, which is
    # a claimable queue slot). A job in one of these with no live local task and no
    # live worker claiming it has been orphaned — its worker died/restarted — so it
    # shows a frozen "encoding" that isn't actually progressing.
    _ACTIVE_WORKER_STATES = (
        JobState.ENCODING, JobState.EXTRACTING_SUBS, JobState.OCR_SUBS, JobState.PLACING,
    )
    # Local, disc-bound work (runs on THIS box). Unlike the worker states above, a
    # hung one keeps a live task (it's awaiting a wedged makemkvcon), so .done()
    # never catches it — a no-progress watchdog does.
    _ACTIVE_RIP_STATES = (
        JobState.SCANNING, JobState.SELECTING, JobState.RIPPING,
        JobState.CONFIRMING, JobState.IDENTIFYING,
    )

    def _is_stalled(self, wj: WebJob, live_state: JobState) -> bool:
        if live_state in self._ACTIVE_RIP_STATES:
            return self._rip_stalled(wj)
        if live_state not in self._ACTIVE_WORKER_STATES:
            return False
        task = getattr(wj, "_task", None)
        if task is not None and not task.done():
            return False  # a live coroutine on THIS box is working it
        stale = self.settings.cluster.stale_after_seconds
        for w in self._workers_cache:
            if w.job_id == wj.id and w.seconds_since_seen <= stale:
                return False  # a live remote worker is heartbeating it
        return True

    def _rip_stalled(self, wj: WebJob) -> bool:
        """A rip/scan that hasn't emitted progress or a log line for the configured
        window — a wedged makemkvcon (bad sector / unresponsive drive) that will
        never finish on its own. Requeue cancels it (killing the subprocess, freeing
        the drive) and re-runs from the last checkpoint."""
        window = self.settings.rip_stall_after_seconds
        if window <= 0:
            return False
        last = getattr(wj, "_activity_at", None)
        if last is None:
            return False  # never started reporting yet — give it a baseline first
        return (time.monotonic() - last) > window

    def _record_serve_presence(self) -> None:
        """Heartbeat this box into the shared workers table.

        Registering under a reserved id (rather than a new table) reuses the
        DB-clock ``last_seen`` the workers already get, so a worker deciding
        "the server is down" never depends on two machines' clocks agreeing.
        The id is filtered out of the Workers panel."""
        self.store.record_worker(
            presence.serve_id(self._hostname), get_version(), None, "serving",
            self._run_token)

    def _check_worker_presence(self) -> None:
        """Notify when a worker that was online stops reporting (and when it
        returns). Only the rip box does this for workers; each worker watches the
        rip box in turn."""
        ages = {w.worker_id: w.seconds_since_seen
                for w in self._workers_cache if not presence.is_serve(w.worker_id)}
        gone, back = self._worker_watch.update(ages)
        window = presence.describe(self.settings.cluster.offline_alert_seconds)
        for worker_id in gone:
            self._notify(f"🔌 Worker offline: {worker_id}",
                         f"No heartbeat for over {window}. Encoding stops until it's back.",
                         priority="high")
        for worker_id in back:
            self._notify(f"✅ Worker back: {worker_id}", "It's reporting in again.")

    async def _refresh_from_store(self) -> None:
        while not self.closing:
            # Run the (blocking, synchronous psycopg) reads in a thread so a slow
            # query doesn't stall the event loop — and thus every client's SSE/UI —
            # for its whole duration. Each store call opens its own connection, so
            # off-loop execution is safe. This loop only runs with a durable store.
            tick_ok = True
            try:
                # Prove this box is alive in the same table, so the workers can
                # tell when the rip box drops — it can't report that itself.
                await asyncio.to_thread(self._record_serve_presence)
                self._workers_cache = await asyncio.to_thread(self.store.list_workers)
                self._check_worker_presence()
            except Exception as exc:
                tick_ok = False
                self._log_store_error("list_workers", exc)
            try:
                records = {r.id: r for r in await asyncio.to_thread(self.store.load_all)}
                changed = False
                for wj in list(self.jobs.values()):
                    rec = records.get(wj.id)
                    if rec is None or self._live_state(wj) not in self._WORKER_OWNED:
                        continue  # locally-owned (ripping/awaiting) — don't clobber
                    try:
                        new_state = JobState(rec.state)
                    except ValueError:
                        continue
                    was_done = wj.state is JobState.DONE
                    wj.state = new_state
                    if wj._pipeline_job is not None:
                        wj._pipeline_job.state = new_state
                    wj.progress, wj.stage, wj.error = rec.progress, rec.stage, rec.error
                    if rec.log:
                        wj.log = list(rec.log)
                    if rec.outputs:
                        wj.outputs = list(rec.outputs)
                    # A worker just finished this encode — notify (once) on the
                    # transition into DONE, not for jobs that were already done.
                    if new_state is JobState.DONE and not was_done:
                        self._maybe_notify_done(wj)
                    changed = True
                if changed:
                    self.bus.publish()
            except Exception as exc:
                tick_ok = False
                self._log_store_error("load_all", exc)
            if tick_ok and getattr(self, "_last_store_error", None) is not None:
                print("[store] reconnected — database reachable again")
                self._last_store_error = None
            await asyncio.sleep(self.settings.cluster.worker_poll_seconds)

    def _log_store_error(self, op: str, exc: Exception) -> None:
        # A persistent store failure (schema drift, bad row) would otherwise make
        # the UI silently stop reflecting worker progress with zero diagnostics.
        # Log the cause once per distinct message, not every poll.
        msg = f"[store] {op} failed: {type(exc).__name__}: {exc}"
        if msg != getattr(self, "_last_store_error", None):
            print(msg)
            self._last_store_error = msg

    async def _watch_drives(self) -> None:
        while not self.closing:
            try:
                self._media_snapshot = await asyncio.to_thread(os_media_snapshot)
                self._drives_cache = await self.list_drives()
                self._last_drive_error = None
                self._save_drives_cache()  # survive restarts; keep the panel warm
                if self.settings.auto_open:
                    self._auto_open(self._drives_cache)
            except Exception as exc:
                # Keep the last cache; log the cause once (not every tick).
                msg = f"{type(exc).__name__}: {exc}"
                if msg != self._last_drive_error:
                    print(f"[drives] scan failed: {msg}")
                    self._last_drive_error = msg
            await self._wait_for_disc_change(self.settings.drive_poll_seconds)

    # How often the cheap OS media probe runs while waiting out the poll interval.
    _MEDIA_PROBE_SECONDS = 2

    async def _wait_for_disc_change(self, timeout: float) -> None:
        """Sleep up to ``timeout`` seconds, returning early the moment the OS says a
        disc appeared or disappeared — i.e. the tray closed on a new disc.

        Without this, inserting a disc isn't noticed until the next full poll. The
        probe is OS-level so it still answers while makemkvcon is busy ripping; on
        platforms where it can't tell (returns None) this is just a plain sleep."""
        deadline = time.monotonic() + timeout
        while not self.closing:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(self._MEDIA_PROBE_SECONDS, remaining))
            try:
                snap = await asyncio.to_thread(os_media_snapshot)
            except Exception:
                continue  # probe failed — fall back to waiting out the interval
            if snap is None:
                continue  # can't tell on this platform; keep the regular cadence
            if snap != self._media_snapshot:
                self._media_snapshot = snap
                return  # media changed — re-enumerate now instead of waiting

    def note_disc_ejected(self, device: str | None = None, index: int | None = None) -> None:
        """Forget a drive's cached disc after ejecting it.

        The cached name (and the auto-open dedupe entry) otherwise keep describing
        the disc that just came out, so the panel shows a stale title and
        re-inserting the SAME disc looks unchanged and never re-triggers a scan."""
        dev = (device or "").strip().rstrip("\\/").upper() or None
        for d in self._drives_cache:
            if (index is not None and d.index == index) or (
                dev is not None and (d.device or "").strip().rstrip("\\/").upper() == dev
            ):
                d.disc_name = None
                self._last_disc.pop(d.index, None)
        if index is not None:
            self._last_disc.pop(index, None)
        # Drop it from the media snapshot too, so re-inserting reads as a change.
        if self._media_snapshot is not None and dev:
            self._media_snapshot.pop(dev, None)
            self._media_snapshot.pop(dev.rstrip(":"), None)
        self._save_drives_cache()
        self.bus.publish()

    def _auto_open(self, drives: list[Drive]) -> None:
        present = set()
        for d in drives:
            if getattr(d, "makemkv_blind", False):
                continue  # OS sees it but makemkvcon can't open it — not rippable
            present.add(d.index)
            name = (d.disc_name or "").strip()
            if not name:
                continue  # no disc / not yet readable
            source = f"disc:{d.index}"
            if (self._last_disc.get(d.index) == name
                    or self._disc_has_active_job(name)
                    or self._scan_pending_for(source)):
                self._last_disc[d.index] = name  # already handled / in progress
                continue
            self._last_disc[d.index] = name
            self.add_disc(source)  # new disc -> scan it
        # Forget drives that are gone or now empty, so reinserting re-triggers.
        # EXCEPT a drive whose disc we're still scanning: makemkvcon holds the
        # drive during a scan, so the watcher's probe reads it as empty. Forgetting
        # it there made the disc look new again the moment the name came back, and
        # the half-scanned job has no disc name yet for _disc_has_active_job to
        # recognise — which added the same disc a second time.
        empty = {d.index for d in drives if not (d.disc_name or "").strip()}
        for idx in list(self._last_disc):
            if idx in present and idx in empty and self._scan_pending_for(f"disc:{idx}"):
                continue
            if idx not in present or idx in empty:
                self._last_disc.pop(idx, None)

    def _scan_pending_for(self, source: str) -> bool:
        """Whether a live job for this drive hasn't finished scanning yet.

        Between add_disc and the scan completing, the job has no ``disc`` — so
        ``_disc_has_active_job`` (which matches on disc name) can't see it. This
        covers that window so a disc can't be queued twice."""
        for wj in self.jobs.values():
            if wj.source != source or wj.disc is not None:
                continue
            if self._live_state(wj) in (JobState.DONE, JobState.FAILED):
                continue
            return True
        return False

    async def _try_auto_rip(self, wj: WebJob, job, title_id: int, reporter) -> bool:
        """Strict hands-off rip: proceed only for a single-movie disc whose cleaned
        name has EXACTLY ONE exact-title TMDB match (so an ambiguous name like
        'Dumbo' -> 1941 + 2019 never auto-picks the wrong one). Returns True if it
        confirmed + started ripping; False to leave the job at the naming gate."""
        if not (self.settings.auto_rip and self.settings.tmdb_api_key and job.disc):
            return False
        from ..services.metadata import TMDBClient
        name = (job.disc.disc_name or "").strip()
        if not name:
            return False
        cleaned, year, _season = naming.clean_tmdb_query(name, "movie")
        try:
            cands = await TMDBClient(
                self.settings.tmdb_api_key, self.settings.metadata_language
            ).search_movie(cleaned, year=year)
        except Exception as exc:
            reporter.on_event(f"Auto-rip: TMDB lookup failed ({type(exc).__name__}); "
                              "leaving for manual naming.")
            return False
        norm = lambda s: (s or "").strip().casefold()
        exact = [c for c in cands if norm(c.title) == norm(cleaned)]
        if len(exact) != 1:
            reporter.on_event(
                f"Auto-rip: {len(exact)} exact matches for '{cleaned}'"
                f"{f' ({year})' if year else ''} — ambiguous, leaving for you to confirm.")
            return False
        c = exact[0]
        wj.assignments = {title_id: EpisodeAssignment(
            title_id=title_id, kind=MediaKind.MOVIE, show_or_movie_title=c.title, year=c.year)}
        wj.selected_title_ids = [title_id]
        wj.state = JobState.READY
        job.state = JobState.READY
        reporter.on_event(f"Auto-rip: matched “{c.title} ({c.year})” — ripping automatically. "
                          "(Use Rename if it's wrong.)")
        self._notify(f"⏩ Auto-ripping: {c.title} ({c.year})",
                     f"Job #{wj.id} matched automatically and started ripping.")
        self._start_job(wj)
        return True

    async def _try_planned_tv(self, wj: WebJob, job, reporter) -> bool:
        """Apply a box-set plan row to a freshly scanned TV disc: name every
        episode-length title and start ripping, with no naming gate.

        The plan CSV is the user's up-front statement of what these discs are, so a
        match is consent to proceed. Anything the row can't state confidently (an
        unexpected title count, no season) parks the job at the normal gate instead
        — a wrong auto-rip costs a whole disc. Returns True if it took over."""
        if job.disc is None:
            return False
        rows = boxset_plan.load_rows(self.settings)
        label = (job.disc.volume_label or job.disc.disc_name or "").strip()
        if not rows:
            # Say so rather than silently falling through: "the CSV isn't working"
            # is otherwise indistinguishable from "no row matched this disc".
            if label:
                reporter.on_event(
                    "Box-set plan: no rows loaded from "
                    f"{boxset_plan.plan_path(self.settings)} — naming this one manually.")
            return False
        row = boxset_plan.match(rows, label)
        if row is None:
            reporter.on_event(
                f"Box-set plan: {len(rows)} row(s) loaded but none match the disc "
                f"label “{label}” — naming this one manually. "
                "(Check the disc_label column; a trailing * covers a whole set.)")
            return False
        result = boxset_plan.assignments_for(row, job.disc)
        if not result.ok:
            reporter.on_event(
                f"Box-set plan matched “{row.disc_label}” but didn't apply: "
                f"{result.reason}. Confirm this one manually.")
            return False
        tmdb_id = await self._fill_episode_titles(result.assignments, row, result.season)
        wj.assignments = {a.title_id: a for a in result.assignments}
        wj.selected_title_ids = [a.title_id for a in result.assignments]
        wj.show_title, wj.show_year, wj.season = row.show, row.year, result.season
        wj.show_tmdb_id = tmdb_id
        wj.state = JobState.READY
        job.state = JobState.READY
        last = result.assignments[-1].episode
        span = (f"E{result.first_episode:02d}" if last == result.first_episode
                else f"E{result.first_episode:02d}–E{last:02d}")
        reporter.on_event(
            f"Box-set plan: {row.show} S{result.season:02d}{span} from "
            f"{len(result.assignments)} title(s) — ripping automatically. "
            "(Use Rename if the show is wrong.)")
        self._notify(f"⏩ Auto-ripping: {row.show} S{result.season:02d}{span}",
                     f"Job #{wj.id} matched the box-set plan and started ripping.")
        self._start_job(wj)
        return True

    async def _fill_episode_titles(self, assignments, row, season: int | None) -> int | None:
        """Best-effort: name each episode from TMDB so placed files read
        'S01E13 - Episode Name'. Returns the show's TMDB id when resolved.

        Numbers alone already name the file correctly, so any failure here (no key,
        no network, unknown show) must degrade quietly and never block the rip."""
        if not (self.settings.tmdb_api_key and season is not None):
            return None
        try:
            from ..services.metadata import TMDBClient
            client = TMDBClient(self.settings.tmdb_api_key, self.settings.metadata_language)
            shows = await client.search_tv(row.show)
            if not shows:
                return None
            # search_tv can't filter by year, so pick on it here — that's what
            # separates a remake from the original (Hawaii Five-0 2010 vs 1968).
            show = next((s for s in shows if s.year == row.year), None) if row.year else None
            show = show or shows[0]
            episodes = await client.get_season(show.tmdb_id, season)
            by_num = {e.episode_number: e.name for e in episodes}
            for a in assignments:
                name = by_num.get(a.episode)
                if name:
                    a.episode_title = name
            return show.tmdb_id
        except Exception:
            return None  # numbers-only naming is still correct

    def _disc_has_active_job(self, disc_name: str) -> bool:
        """Whether THIS disc is already being handled — matched by disc name, not
        the drive selector. `disc:N` is reused across discs, so a queued job from a
        previous disc in the same drive must NOT block auto-scanning a new one."""
        return any(
            wj.disc is not None and (wj.disc.disc_name or "").strip() == disc_name
            and self._live_state(wj) not in (JobState.DONE, JobState.FAILED)
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
        stale = self.settings.cluster.stale_after_seconds
        # The rip box heartbeats into the same table (so workers can see IT drop);
        # it isn't an encode worker, so keep it out of the Workers panel.
        workers = [
            {"id": w.worker_id, "version": w.version, "job_id": w.job_id,
             "stage": w.stage, "age": round(w.seconds_since_seen),
             "stale": w.seconds_since_seen > stale,
             "uptime": round(w.uptime_seconds)}
            for w in self._workers_cache if not presence.is_serve(w.worker_id)
        ]
        return {
            "jobs": jobs,
            "version": get_version(),
            "workers": workers,
            "uptime": round(time.monotonic() - self._start_monotonic),
        }

    async def refresh_artwork(self, job_id: int) -> int:
        """Re-fetch TMDB poster/backdrop for one job, overwriting its folder art."""
        wj = self.jobs.get(job_id)
        if wj is None or not wj.assignments:
            return 0
        n = await self.pipeline.place_tmdb_art(list(wj.assignments.values()), _Reporter(self, wj))
        self.bus.publish()
        return n

    async def refresh_all_artwork(self) -> int:
        """Re-fetch TMDB art for every job that has a naming assignment."""
        total = 0
        for wj in list(self.jobs.values()):
            if wj.assignments:
                total += await self.pipeline.place_tmdb_art(
                    list(wj.assignments.values()), _Reporter(self, wj))
        self.bus.publish()
        return total

    def active_jobs(self) -> list[dict]:
        """Jobs THIS process is actively working on right now, i.e. holding a live
        local task (a rip/encode/OCR/placing coroutine still running here).

        Keyed off the live task, NOT the job state, on purpose: a job whose encode
        was delegated to a `worker` machine, or one left in a stale ENCODING/RIPPING
        state by a prior power-off, still *reads* as active but has no running task
        here — restarting this process wouldn't interrupt it, so it must not block
        the restart. A parked job waiting at a gate has a finished task too.
        """
        out = []
        for wj in self.jobs.values():
            task = getattr(wj, "_task", None)
            if task is not None and not task.done():
                state = self._live_state(wj)
                out.append({"id": wj.id, "name": self._name_of(wj), "state": state.value})
        return out

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

    def requeue(self, job_id: int) -> None:
        """Recover a STALLED job — one in a worker-owned state (encoding / subtitles
        / placing) with nothing actually working it (its worker died or the serve
        box restarted mid-prep). Reset it to READY, drop any stale claim, and re-run
        from its checkpoints (the finished rip is reused; only the unfinished stage
        re-runs). Rejected if the job isn't actually stalled, so it never interrupts
        live work."""
        wj = self.jobs.get(job_id)
        if wj is None:
            raise ValueError(f"No job {job_id}.")
        if not self._is_stalled(wj, self._live_state(wj)):
            raise ValueError(f"Job {job_id} isn't stalled — nothing to requeue.")
        if wj._pipeline_job is None:
            raise ValueError(f"Job {job_id} can't be requeued (no scan state).")
        old_task = getattr(wj, "_task", None)  # a hung rip's live (wedged) task, if any
        wj.error = None
        wj.progress = 0.0
        wj.state = JobState.READY
        wj._pipeline_job.state = JobState.READY
        try:
            self.store.release(job_id)  # drop a dead worker's stale claim
        except Exception:
            pass
        wj.log.append(stamp_log("Requeued (was stalled) — resuming from checkpoints."))
        self._persist(wj)
        self.bus.publish()
        wj._task = self._spawn(self._requeue_run(wj, old_task))

    async def _requeue_run(self, wj: WebJob, old_task) -> None:
        """Cancel a wedged local rip task (which kills makemkvcon and frees the
        drive) and wait for it to actually die before re-running — otherwise the
        fresh rip races the old subprocess for the same drive."""
        if old_task is not None and not old_task.done():
            old_task.cancel()
            try:
                await old_task
            except BaseException:
                pass  # cancelled (or whatever it was stuck on) — drive is released now
        await self._run_job(wj)

    def requeue_stalled(self) -> int:
        """Requeue every currently-stalled job. Returns how many were requeued."""
        ids = [jid for jid, wj in list(self.jobs.items())
               if self._is_stalled(wj, self._live_state(wj))]
        done = 0
        for jid in ids:
            try:
                self.requeue(jid)
                done += 1
            except ValueError:
                pass
        return done

    def bump_job(self, job_id: int) -> None:
        """Move a queued job to the front so it's the NEXT to encode. Raises the
        job's priority above everything currently queued; the next free worker (or
        the next local encode) then claims it first. A job already encoding keeps
        going — this only reorders what's still waiting."""
        wj = self.jobs.get(job_id)
        if wj is None:
            raise ValueError(f"No job {job_id}.")
        top = max((j.priority for j in self.jobs.values()), default=0)
        wj.priority = top + 1
        wj.log.append(stamp_log("Bumped to the front of the encode queue — next to encode."))
        self._persist(wj)
        self.bus.publish()

    def request_worker_update(self) -> int:
        """Signal every worker to drain (finish its current encode), git pull, and
        restart — the cluster-wide version of a worker's 'u' key. Returns how many
        workers are currently connected (for the confirmation message)."""
        if not self.store.durable:
            raise ValueError(
                "Updating workers needs the shared Postgres store "
                "(set BLURAYRIPPER_DATABASE_URL) — there are no remote workers otherwise.")
        self.store.request_worker_update()
        return len(self._workers_cache)

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
        self._start_job(wj)

    def rename_job(self, job_id: int, title: str, year: int | None) -> None:
        """Fix a job's identity — a movie OR a show whose title/year is wrong (e.g.
        the 1968 "Hawaii Five-O" mapped to the 2010 reboot). Updates every naming
        assignment, keeping each episode's season/episode numbers; if a video is
        already placed, renames the placed files (video + sidecars + artwork) to the
        corrected name/folder and refreshes the TMDB artwork."""
        wj = self.jobs.get(job_id)
        if wj is None:
            raise ValueError(f"No job {job_id}.")
        assigns = {tid: a for tid, a in wj.assignments.items() if a is not None}
        if not assigns:
            raise ValueError("Job has no naming assignment to rename.")
        title = (title or "").strip()
        if not title:
            raise ValueError("A title is required.")
        renamed: dict[int, EpisodeAssignment] = {}
        moves: list[tuple[Path, Path]] = []
        for tid, a in assigns.items():
            new_a = replace(a, show_or_movie_title=title, year=year)
            renamed[tid] = new_a
            # A TV assignment missing season/episode can't resolve a path; it just
            # gets the corrected name (nothing is placed for it yet).
            try:
                old_video = self.pipeline._video_path_for(a)
                new_video = self.pipeline._video_path_for(new_a)
            except ValueError:
                continue
            if old_video != new_video and old_video.exists():
                moves.append((old_video, new_video))
        wj.assignments = renamed
        for old_video, new_video in moves:
            try:
                library.rename_movie(old_video, new_video)
            except OSError as exc:
                raise ValueError(f"Couldn't rename the placed files: {exc}")
            wj.outputs = [str(new_video) if o == str(old_video) else o for o in wj.outputs]
        # Keep the show identity in sync so a later auto-identify uses the RIGHT
        # show. The old TMDB id belongs to the wrong show — drop it rather than
        # fetch reference subtitles for it again.
        if any(a.kind is MediaKind.TV for a in renamed.values()):
            wj.show_title, wj.show_year, wj.show_tmdb_id = title, year, None
        wj.log.append(stamp_log(f"Renamed to “{title}{f' ({year})' if year else ''}”."))
        self._persist(wj)
        self.bus.publish()
        # Refresh the poster/backdrop for the corrected identity (best-effort).
        self._spawn(self.refresh_artwork(job_id))

    def remap(self, job_id: int) -> None:
        """Send a resting job back to the naming gate so its identity can be set.

        Rename only edits an EXISTING assignment, so a job that never got one —
        an auto-identify disc whose subtitle match failed, or one that failed
        before naming — had no way to be named at all ("no naming assignment to
        rename"). This re-opens the form instead: back to the title gate when no
        titles were ever selected, otherwise straight to naming. The finished rip
        is kept, so confirming resumes from checkpoints rather than re-ripping."""
        wj = self.jobs.get(job_id)
        if wj is None:
            raise ValueError(f"No job {job_id}.")
        live = self._live_state(wj)
        # READY too: a ripped job waiting for an encode worker is the common case
        # for an auto-identify disc that never matched. Moving it off READY also
        # makes it unclaimable, so it can't start encoding under the old naming.
        if live not in (JobState.DONE, JobState.FAILED, JobState.READY):
            raise ValueError("This job is being worked on right now — wait for it to "
                             "finish or stop, then set the naming.")
        if wj._pipeline_job is None or wj.disc is None:
            raise ValueError("This job has no scanned disc to re-map.")
        # A live local task means it's still prepping (rip/subs) on this box; only a
        # READY job that's merely QUEUED for a worker is safe to re-open.
        task = getattr(wj, "_task", None)
        if task is not None and not task.done():
            raise ValueError("Job is still running — wait for it to stop.")
        # Drop any stale worker claim so the job can't be picked up mid-rename.
        with contextlib.suppress(Exception):
            self.store.release(job_id)
        # Auto-identify already ran (and didn't produce a mapping); re-running it
        # would just park at the same match gate, so hand naming back to the user.
        wj.auto_identify = False
        wj.error = None
        wj.progress = 0.0
        target = (JobState.AWAITING_MAPPING if wj.selected_title_ids
                  else JobState.AWAITING_TITLES)
        wj.state = target
        wj._pipeline_job.state = target
        wj.log.append(stamp_log("Re-opened for naming."))
        self._persist(wj)
        self.bus.publish()

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
        wj.log.append(stamp_log("Retrying…"))
        self._start_job(wj)

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
        self._start_job(wj)

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
        self._start_job(wj)

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
                "OpenSubtitles returned no subtitles for these episodes "
                f"(show {wj.show_title!r} S{wj.season} E{wj.candidate_episodes}). "
                "The show/season may be mismatched, or these episodes aren't on "
                "OpenSubtitles — fix the show or map episodes manually.")
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
        self._notify(f"📺 Confirm episodes: {self._name_of(wj)}",
                     f"Job #{wj.id} — subtitle match needs your confirmation.")
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

    async def remove_many(self, predicate) -> int:
        """Remove every job matching ``predicate(wj)``. Returns how many. Used by
        the bulk 'clear failed / remove completed' actions."""
        ids = [jid for jid, wj in list(self.jobs.items()) if predicate(wj)]
        for jid in ids:
            await self.remove(jid)
        return len(ids)

    async def remove(self, job_id: int) -> None:
        """Remove any job from the queue. If it's running (or parked at the grain
        gate), cancel its task — which kills the active rip/encode subprocess —
        then clean up its staging dir and drop it from the store."""
        wj = self.jobs.get(job_id)
        if wj is None:
            return
        task = wj._task
        if task is not None and not task.done():
            wj.log.append(stamp_log("Removing — cancelling in-flight work…"))
            self.bus.publish()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                # Expected: we cancelled `task`. But if remove() ITSELF is being
                # cancelled (e.g. server shutdown), propagate instead of swallowing.
                cur = asyncio.current_task()
                if cur is not None and cur.cancelling():
                    raise
            except Exception:
                pass  # the job's task errored on its way down — fine, we're removing it
        # Off-thread: this is an rmtree, and staging lives on the NAS. Run inline it
        # blocks the event loop — so every client's SSE stalls — and remove_many
        # loops this once per job, which is how a bulk "remove completed" froze the
        # UI. Unlike the store calls below there's no circuit breaker to bound it.
        await asyncio.to_thread(
            library.cleanup, self.settings.storage.staging_dir / f"job_{wj.id}")
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
                job.state = JobState.READY  # sync off SCANNING (see below)
                self._start_job(wj)
            else:
                # A box-set plan row naming this disc rips it hands-off — the CSV
                # already said what it is, so there's nothing to confirm.
                if await self._try_planned_tv(wj, job, reporter):
                    return  # plan took over (set READY + spawned the run)
                # A movie disc has a single main-feature title — skip the title
                # gate and go straight to naming. TV discs (multiple candidate
                # episodes) still stop so you can pick which titles to rip.
                suggested = self._suggested_titles(job.disc)
                if len(suggested) == 1:
                    tid = next(iter(suggested))
                    wj.selected_title_ids = [tid]
                    # Fully hands-off: a single-movie disc with one exact-name TMDB
                    # match rips automatically. Ambiguous/no match -> park for naming.
                    if await self._try_auto_rip(wj, job, tid, reporter):
                        return  # auto-rip took over (set READY + spawned the run)
                    wj.state = JobState.AWAITING_MAPPING
                    reporter.on_event("Single title — auto-selected; confirm the name.")
                else:
                    wj.state = JobState.AWAITING_TITLES
                # scan_disc leaves the pipeline job at SCANNING; sync it to the
                # parked gate so _live_state can't surface a phantom "scanning"
                # that the UI shows forever with no way to reach the gate.
                job.state = wj.state
                self._persist(wj)
                self.bus.publish()
                self._notify(f"🟡 Needs your input: {self._name_of(wj)}",
                             f"Job #{wj.id} scanned — {'confirm the name' if wj.state is JobState.AWAITING_MAPPING else 'pick titles'} in the UI.")
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
        wj._activity_at = time.monotonic()  # baseline for the rip-stall watchdog
        job = wj._pipeline_job
        job.selected_title_ids = wj.selected_title_ids
        job.assignments = wj.assignments
        job.grain_mode = wj.grain_mode
        job.tune_grain = wj.tune_grain
        try:
            if wj.auto_identify and not wj.assignments:
                # Rip + OCR + subtitle-match against the disc's candidate episodes.
                # Parks at AWAITING_MATCH if any title is uncertain.
                async with self._prep_sem:
                    ok = await self._auto_identify(wj, reporter)
                if not ok:
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
                # Throttle the prep phase (not the encode) so a recovery stampede
                # doesn't run dozens of rips/OCRs at once.
                async with self._prep_sem:
                    await self.pipeline.prep_phase(
                        job, staging_root, reporter, include_subs=include_subs)
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
                self._maybe_notify_done(wj)
                self.bus.publish()
                return
            async with self._prep_sem:  # single-box: bound concurrent jobs
                await self.pipeline.execute(job, self.scheduler, reporter, job_name=f"job_{wj.id}")
            wj.state = JobState.DONE
            wj.progress = 1.0
            wj.outputs = [str(r.video_path) for r in job.results if r.video_path]
            self._maybe_notify_done(wj)
        except Exception as exc:
            if _looks_interrupted(exc):
                # A tool was terminated (e.g. the server stopped mid-encode), not
                # a real failure. Park as resumable so recovery picks it back up
                # — its finished rip is reused, only the encode re-runs.
                wj.state = JobState.READY
                wj.error = None
                wj.log.append(stamp_log("Interrupted (server stopping?) — will resume on restart."))
            else:
                wj.state = JobState.FAILED
                wj.error = f"{type(exc).__name__}: {exc}"
                self._schedule_auto_retry(wj)
        else:
            self._auto_retries.pop(wj.id, None)  # success resets the retry budget
        self._persist(wj)
        self.bus.publish()

    def change_source(self, job_id: int, new_source: str) -> None:
        """Re-point a job at a different drive/selector (e.g. move the disc to a
        drive that can read a bad sector). Not allowed while the job is running.
        Completed titles keep their checkpoints (reused); the failed one re-rips
        from the new drive on Retry."""
        wj = self.jobs.get(job_id)
        if wj is None:
            raise KeyError(job_id)
        new_source = (new_source or "").strip()
        if not new_source.startswith(("disc:", "dev:", "iso:", "file:")):
            raise ValueError("Source must be a selector like 'disc:1'.")
        if self._live_state(wj) in self._REDO_BUSY:
            raise ValueError("Can't change the drive while the job is running.")
        wj.source = new_source
        # Resolve the OS device for the new drive so auto-eject/cover-art target the
        # RIGHT drive — a stale device would eject a DIFFERENT disc.
        new_device = None
        if new_source.startswith("disc:"):
            try:
                idx = int(new_source.split(":", 1)[1])
                drv = next((d for d in self._drives_cache if d.index == idx), None)
                new_device = drv.device if drv else None
            except ValueError:
                pass
        job = wj._pipeline_job
        if job is not None:
            job.source = new_source
            if getattr(job, "disc", None) is not None:
                job.disc.source = new_source
                if new_device:
                    job.disc.drive_device = new_device
        if wj.disc is not None:  # same object as job.disc when both exist; be safe
            wj.disc.source = new_source
            if new_device:
                wj.disc.drive_device = new_device
        wj.log.append(stamp_log(f"Rip source changed to {new_source} — click Retry to rip from it."))
        self._persist(wj)
        self.bus.publish()

    def _rip_pending(self, wj: WebJob) -> bool:
        """True if this disc job still has a selected title left to rip — i.e. the
        disc is still needed. False once every selected title has a ripped-file
        checkpoint (an encode/subtitle/place failure no longer needs the disc)."""
        if not (wj.source or "").startswith("disc:"):
            return False
        job = wj._pipeline_job
        selected = wj.selected_title_ids or (getattr(job, "selected_title_ids", []) if job else [])
        if not selected:
            return True  # nothing ripped yet
        cps = getattr(job, "checkpoints", {}) if job else {}
        return any(not (cps.get(t) or {}).get("ripped_path") for t in selected)

    def _source_disc_missing(self, wj: WebJob) -> bool:
        """True if the job rips from a disc:N whose drive currently has no disc,
        so retrying is pointless until it's reinserted. Uses the OS-aware drive
        cache (a disc the OS sees but makemkvcon's probe missed counts as present)."""
        src = wj.source or ""
        if not src.startswith("disc:"):
            return False
        try:
            idx = int(src.split(":", 1)[1])
        except ValueError:
            return False
        for d in self._drives_cache:
            if d.index == idx:
                return not (d.disc_name and d.disc_name.strip())
        return False  # drive not in the (maybe unpopulated) cache — don't assume absent

    # MakeMKV failure signatures that won't fix themselves on retry — the drive is
    # offline or the disc/drive can't be read. Matched (lower-cased) against the
    # job error + recent log.
    _DRIVE_OFFLINE_SIGNS = (
        "no usable optical drives", "device does not exist", "failed to open disc")
    _READ_ERROR_SIGNS = ("scsi error", "read error", "hash check failed", "fetchframes")

    def _rip_failure_hint(self, wj: WebJob) -> str | None:
        """A specific, non-retryable hint for a drive/read failure, or None."""
        blob = ((wj.error or "") + "\n" + "\n".join(wj.log[-12:])).lower()
        if any(s in blob for s in self._DRIVE_OFFLINE_SIGNS):
            return ("The optical drive became unavailable to MakeMKV — it likely dropped "
                    "off the bus. The disc being inserted isn't enough: check the drive's "
                    "power/connection or reset it (or use another drive), then click Retry.")
        if any(s in blob for s in self._READ_ERROR_SIGNS):
            return ("Disc read error — the drive couldn't read the disc (a bad sector, a "
                    "dirty/scratched disc, or the drive dropping mid-read). Clean the disc "
                    "or reset/try another drive, then click Retry.")
        return None

    def _schedule_auto_retry(self, wj: WebJob) -> None:
        """After a failure, re-queue the job up to auto_retry_max times with
        exponential backoff (30s, 60s, 120s… capped 5m). Bounded so a genuinely
        broken job doesn't loop forever."""
        # A drive/read failure (or a genuinely absent disc) won't recover on its
        # own — surface a specific reason and don't burn retries against it.
        hint = self._rip_failure_hint(wj)
        if hint is None and self._source_disc_missing(wj):
            hint = "The disc isn't in the drive — reinsert it, then click Retry."
        if hint:
            wj.log.append(stamp_log(hint))
            self._notify(f"💿 {self._name_of(wj)} needs attention",
                         f"Job #{wj.id}: {hint}")
            return
        n = self._auto_retries.get(wj.id, 0)
        if n >= self.settings.auto_retry_max:
            if self.settings.auto_retry_max:
                wj.log.append(stamp_log(f"Auto-retry exhausted ({n}) — hit Retry to try again."))
            self._notify(f"❌ Failed: {self._name_of(wj)}",
                         f"Job #{wj.id} failed: {wj.error}", priority="high")
            return
        self._auto_retries[wj.id] = n + 1
        delay = min(30 * (2 ** n), 300)
        wj.log.append(stamp_log(
            f"Auto-retry {n + 1}/{self.settings.auto_retry_max} in {delay}s…"))
        self._spawn(self._auto_retry_later(wj.id, delay))

    async def _auto_retry_later(self, job_id: int, delay: float) -> None:
        await asyncio.sleep(delay)
        if self.closing or job_id not in self.jobs:
            return
        try:
            self.retry(job_id)  # re-queues + spawns _run_job
        except (ValueError, KeyError):
            pass  # already retried/removed/no-longer-failed

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _start_job(self, wj: WebJob) -> None:
        """Checkpoint the job's new state, push it to the UI, and run it.

        The tail every "this job is now go" path shares — the caller has already
        set the state and any assignments. Keeping it in one place also keeps the
        order consistent (persist before publish, so a client that reacts to the
        tick can't read state the store hasn't got yet)."""
        self._persist(wj)
        self.bus.publish()
        wj._task = self._spawn(self._run_job(wj))

    def _notify(self, title: str, message: str, priority: str | None = None) -> None:
        """Fire-and-forget push (if a notify_url is configured)."""
        if self.settings.notify_url:
            self._spawn(notify(self.settings.notify_url, title, message, priority))

    def _maybe_notify_done(self, wj: WebJob) -> None:
        """Push a 'finished encoding' notification the first time a job reaches
        DONE. Guarded by a set so it fires once — and jobs already DONE at startup
        are pre-seeded (in recover), so a restart never re-notifies old history."""
        if wj.id in self._done_notified:
            return
        self._done_notified.add(wj.id)
        name = self._name_of(wj)
        n = len(wj.outputs)
        self._notify(
            f"✅ Done: {name}",
            f"{name} finished encoding — {n} file{'' if n == 1 else 's'} placed in your library.")

    def _name_of(self, wj: WebJob) -> str:
        return self._display_name(wj)

    def _display_name(self, wj: WebJob) -> str:
        """A name that identifies the physical DISC, not just its label.

        Every disc in a box set reports the same volume label (each House DVD is
        just "HOUSE"), so a failed job is impossible to place on the shelf from the
        label alone. The confirmed mapping does identify it: show + season + episode
        range for TV, title + year for a movie. Falls back to the label when a job
        failed before it was ever mapped."""
        assigns = [a for a in wj.assignments.values() if a is not None]
        fallback = (wj.disc.disc_name if wj.disc else None) or wj.source
        if not assigns:
            # Never mapped (e.g. it failed mid-identify) — auto-identify may still
            # have worked out the show/season, which is enough to find the disc.
            show = (wj.show_title or "").strip()
            if show:
                return f"{show} S{wj.season:02d}" if wj.season is not None else show
            return fallback
        first = assigns[0]
        title = (first.show_or_movie_title or "").strip()
        if not title:
            return fallback
        if first.kind is not MediaKind.TV:
            return f"{title} ({first.year})" if first.year else title
        seasons = sorted({a.season for a in assigns if a.season is not None})
        episodes = sorted({a.episode for a in assigns if a.episode is not None})
        parts = []
        if len(seasons) == 1:
            parts.append(f"S{seasons[0]:02d}")
        elif seasons:
            parts.append(f"S{seasons[0]:02d}–S{seasons[-1]:02d}")
        if len(episodes) == 1:
            parts.append(f"E{episodes[0]:02d}")
        elif episodes:
            parts.append(f"E{episodes[0]:02d}–E{episodes[-1]:02d}")
        return f"{title} {''.join(parts)}" if parts else title

    def artwork_path(self, job_id: int):
        """Path to a job's extracted cover art (poster preferred, else fanart),
        or None. Used to show a thumbnail on the card."""
        wj = self.jobs.get(job_id)
        if wj is None or not wj.assignments:
            return None
        a = next(iter(wj.assignments.values()), None)
        if a is None:
            return None
        folder = self.pipeline._poster_folder(a)
        for base in ("poster", "fanart"):
            for ext in (".jpg", ".jpeg", ".png"):
                p = folder / f"{base}{ext}"
                if p.exists():
                    return p
        return None

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

    def _lane(self, wj: WebJob, live_state: JobState) -> str | None:
        """Which queue a job belongs to, so the UI can split rip-side from
        encode-side work: 'rip' = disc-bound work on the rip/serve box, 'encode' =
        worker/encode-side. None for resting jobs (done/failed), shown separately."""
        s = live_state
        if s in (JobState.DONE, JobState.FAILED):
            return None
        if s in (JobState.ENCODING, JobState.PLACING):
            return "encode"
        if s in (JobState.EXTRACTING_SUBS, JobState.OCR_SUBS):
            # Serve-side subtitle prep is part of ripping; worker-side subs is encode.
            return "encode" if self.settings.cluster.worker_does_subs else "rip"
        if s is JobState.READY:
            # READY is overloaded: pre-prep (a live local rip task is queued or
            # running here) vs post-prep (ripped, waiting for an encode worker).
            task = getattr(wj, "_task", None)
            return "rip" if (task is not None and not task.done()) else "encode"
        # queued / scanning / selecting / ripping / confirming / identifying / awaiting_*
        return "rip"

    def _label_hints(self, wj: WebJob) -> dict:
        """Season + disc-in-set read off the disc label, e.g.
        ``HAWAII_FIVE_0_S1_D4`` -> season 1, disc 4. Both are hints for the mapping
        form to pre-fill — the user still confirms, so a mislabeled disc can't
        silently rip as the wrong episodes."""
        label = (wj.disc.disc_name if wj.disc else None) or ""
        if not label:
            return {"label_season": None, "label_disc": None}
        _title, _year, season = naming.clean_tmdb_query(label, "tv")
        return {"label_season": season, "label_disc": naming.disc_number(label)}

    def _job_dict(self, wj: WebJob) -> dict:
        live_state = self._live_state(wj)
        return {
            "id": wj.id,
            "source": wj.source,
            "state": live_state.value if hasattr(live_state, "value") else str(live_state),
            "stage": wj.stage,
            "progress": round(wj.progress, 4),
            # True when the state says "encoding/OCR/placing" but nothing is actually
            # working it (worker died/restarted). Lets the UI flag a frozen job.
            "stalled": self._is_stalled(wj, live_state),
            "lane": self._lane(wj, live_state),  # 'rip' | 'encode' | None (resting)
            "priority": wj.priority,  # higher = bumped ahead in the encode queue
            "error": wj.error,
            "disc_name": wj.disc.disc_name if wj.disc else None,
            # Identifies WHICH disc this is (box-set labels are all identical).
            "display_name": self._display_name(wj),
            # Season / disc-in-set parsed from the label ("HAWAII_FIVE_0_S1_D4"),
            # so the mapping form can pre-fill them instead of making you retype.
            **self._label_hints(wj),
            "kind_guess": (discinfo.guess_kind(wj.disc).value if wj.disc else None),
            "titles": self._title_dict(wj) if wj.disc else [],
            "selected": wj.selected_title_ids,
            # Rename edits an existing assignment; without one the job needs its
            # naming SET (re-map) instead, so the UI offers the right control.
            "has_naming": bool(wj.assignments),
            "outputs": wj.outputs,
            # Whether the disc is still needed (some title left to rip). False once
            # ripping is done, so an encode/subtitle failure doesn't offer "change
            # drive" (the disc isn't involved anymore).
            "needs_disc": self._rip_pending(wj),
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
