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
from datetime import datetime, timezone
import itertools
import json
import re
import socket
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..config import Settings
from ..models import (
    SOURCE_SELECTORS,
    DiscInfo,
    EpisodeAssignment,
    MediaKind,
    Stream,
    StreamType,
    Title,
)
from ..persistence import (
    ALL_WORKERS,
    WORKER_ACTIONS,
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
from .. import boxset_plan, maintenance, mkv_folder, presence
from ..services import library, naming
from ..services import aacs_dump
from ..services.drive_caps import DriveCaps
from ..services.drive_names import DriveNames
from ..services.notify import notify
from ..services.opensubtitles import OpenSubtitlesClient, gather_references
from ..services.sub_match import identify
from ..tools import discinfo
from ..tools.makemkv import (
    UHD_CAPACITY_FLOOR_BYTES,
    Drive,
    os_disc_capacity_bytes,
    os_media_snapshot,
)
from ..version import get_version


_STATUS_CONTROL_C_EXIT = 3221225786  # 0xC000013A — Windows process terminated


def _hms(seconds: int) -> str:
    """A runtime in the form you can match against a disc case: "2h09m"."""
    seconds = max(0, int(seconds or 0))
    h, m = divmod(seconds // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


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
    aacs_dump: str | None = None  # kept AACS dump, offered for download
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
        # get_running_loop, not get_event_loop: this is inside a coroutine, so the
        # loop is unambiguous, and get_event_loop's implicit-loop behaviour is
        # deprecated (and increasingly noisy) on 3.12+.
        wj._grain_future = asyncio.get_running_loop().create_future()
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
        # Keyed on model+serial, so unlike drive_names (keyed on device) this one
        # is safe to share between boxes and survives /dev/srN being reassigned.
        self.drive_caps = DriveCaps(
            settings.storage.output_dir / ".blurayripper" / "drive_caps.json")
        self.closing = False
        # Monotonic so the reported uptime is immune to wall-clock adjustments.
        self._start_monotonic = time.monotonic()
        self._ids = itertools.count(1)
        self._tasks: set[asyncio.Task] = set()
        # Bounds how many jobs prep (rip+subs+grain) at once — so a restart that
        # recovers dozens of in-flight jobs doesn't stampede the box all at once.
        self._prep_sem = asyncio.Semaphore(max(1, settings.prep_concurrency))
        # One lock per physical drive. prep_concurrency alone bounds only the
        # NUMBER of concurrent preps, so with two disc jobs on one drive both
        # took a slot and drove makemkvcon at the same optical head.
        self._drive_locks: dict[str, asyncio.Lock] = {}
        self._auto_retries: dict[int, int] = {}  # job id -> auto-retry count so far
        self._drives_cache_file = (
            settings.storage.output_dir / ".blurayripper" / "drives_cache.json")
        # Seed from the last persisted list so the panel isn't blank right after a
        # restart (and the /api/drives endpoint never has to do a blocking scan
        # that could hang on an unresponsive drive).
        self._drives_cache: list[Drive] = self._load_drives_cache()
        self._last_disc: dict[int, str] = {}  # drive index -> last auto-opened disc name
        self._last_drive_error: str | None = None
        # True once the watcher has enumerated drives successfully at least once,
        # so "not in the cache" can be read as "not attached" rather than "unknown".
        self._drives_scanned = False
        # Cheap OS view of which drives hold media, so a tray closing on a new disc
        # triggers a scan immediately instead of waiting out the full poll interval.
        self._media_snapshot: dict[str, str] | None = None
        self._workers_cache: list = []  # WorkerInfo, refreshed by the store poll
        # Jobs whose last save to the store failed, retried once it is back.
        self._unpersisted: set[int] = set()
        # Last reported store error, PER OPERATION (see _log_store_error).
        self._last_store_error: dict[str, str] = {}
        # True when recover() could not read the store at startup, so the queue
        # in memory is not the queue that exists. Retried on the first good poll.
        self._recover_pending = False
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
            # ...but do not forget it happened. A rip that finished during an
            # outage otherwise sits at READY in memory while the store has no
            # row for it, so no worker can ever claim it and the UI shows a
            # queued job that never moves. Nothing retried the save, and the
            # refresh loop only READS. Two discs were stranded exactly this way.
            self._unpersisted.add(wj.id)
            return
        self._unpersisted.discard(wj.id)

    def _flush_unpersisted(self) -> int:
        """Re-save jobs whose last save failed. Returns how many were recovered."""
        done = 0
        for job_id in sorted(self._unpersisted):
            wj = self.jobs.get(job_id)
            if wj is None:
                self._unpersisted.discard(job_id)
                continue
            try:
                self.store.save(self._to_record(wj))
            except Exception:
                return done          # still down; keep the rest for next time
            self._unpersisted.discard(job_id)
            wj.log.append(stamp_log(
                "Saved to the database (an earlier save had failed, so this job "
                "was invisible to workers until now)."))
            done += 1
        return done

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
            aacs_dump=wj.aacs_dump,
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
        wj.aacs_dump = getattr(rec, 'aacs_dump', None)
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
        # Never park jobs for a missing rip when the staging root itself is not
        # readable. A share that has not mounted yet makes EVERY file look gone,
        # and parking the whole queue over a mount that appears a second later
        # would be far worse than the problem being solved.
        staging_readable = self._staging_readable()
        if not staging_readable:
            print(f"[recover] staging is not readable ({self.settings.storage.staging_dir})"
                  " — not checking ripped files this pass.")
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
            # A disc job at READY whose rip never finished is nobody's: serve skips
            # READY as "worker-owned", but a worker refuses it (_fully_ripped) since
            # there's no ripped file to encode — so it's claimed and released
            # forever, showing "ready" while progressing never. Only serve can rip,
            # so serve takes it back. With the disc absent it now fails fast with a
            # reason instead of looping.
            #
            # An interrupted FAILED job with no rip belongs here too. Reclaiming it
            # as READY (the branch below) MANUFACTURED that same orphan: nothing had
            # been ripped, so a worker claimed it, refused it and released it, and it
            # sat showing "ready" until the NEXT restart finally caught it as one.
            needs_rip = ((wj.state is JobState.READY or interrupted_fail)
                         and (wj.source or "").startswith(("disc:", "dev:"))
                         and self._rip_pending(wj))
            # A checkpoint can also NAME a ripped file that is no longer there.
            # Only the worker used to notice, one job at a time and only when
            # that job happened to be the next claimable one -- so a queue of
            # them drained at a job per grace period, which is hours. Serve can
            # stat the lot in one pass, and it is the only place that sees the
            # whole queue at once.
            if (not needs_rip and staging_readable and wj.state is JobState.READY
                    and self._rip_file_missing(wj)):
                needs_rip = True
                self._drop_stale_rip_checkpoints(wj)
            if clustered and needs_rip:
                # Park for review rather than re-ripping unasked. These are discs
                # whose rip never completed, so resuming means spinning up the
                # drive again — and the disc may not even be in it. FAILED puts
                # them in front of you with the controls that already exist
                # (Retry to rip, "Rip from drive" to move it, Remove to drop it),
                # so Retry IS the approval.
                wj.state = JobState.FAILED
                if wj._pipeline_job is not None:
                    wj._pipeline_job.state = JobState.FAILED
                wj.error = ("Rip never completed — no ripped file for this job.")
                wj.log.append(stamp_log(
                    "Needs review: this job's rip never finished, so there is nothing "
                    "for a worker to encode. Put the disc in a drive and click “Rip "
                    "this disc”, or Remove it. Nothing has been read or re-ripped."))
                self._persist(wj)
            elif clustered:
                if (wj.state in prep_states
                        or (needs_identify and wj.state is JobState.READY)):
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

    def _stall_info(self, wj: WebJob, live_state: JobState,
                    stalled: bool) -> tuple[str | None, str | None]:
        """(kind, sentence) for a stalled job — WHICH kind of stall this is.

        "Stalled" on its own reads as "your rip is lost", and that is wrong half
        the time. A rip-lane job can stall with the rip unfinished (the disc has
        to go back in the drive, and Requeue re-rips) or *after* the rip finished
        and was checkpointed (the file is safe, and Requeue carries on from it).
        Those ask opposite things of whoever is looking at the card, both showed
        the same red "stalled", and only this side can tell them apart.
        """
        if not stalled:
            return None, None
        if live_state in self._ACTIVE_RIP_STATES:
            if self._rip_pending(wj):
                return "rip_unfinished", (
                    "The rip stopped before it finished — a bad disc, or a drive "
                    "that stopped answering. The disc is still needed: Requeue "
                    "cancels the stuck rip and re-rips what's missing.")
            return "rip_saved", (
                "The rip finished and is saved — the job stopped after it. "
                "Requeue carries on from the ripped file and won't re-rip.")
        return "no_worker", (
            "No worker is running this any more. Requeue puts it back in the queue.")

    # Grace before "no task is working this" counts as stalled. Covers the gap
    # between a job being marked RIPPING and its task being attached.
    _RIP_TASK_GRACE = 90

    def _rip_stalled(self, wj: WebJob) -> bool:
        """A rip/scan that nothing is actually advancing — either a wedged
        makemkvcon (bad sector / unresponsive drive) that will never finish, or a
        job whose task is simply gone. Requeue cancels it (killing any subprocess,
        freeing the drive) and re-runs from the last checkpoint."""
        window = self.settings.rip_stall_after_seconds
        if window <= 0:
            return False
        now = time.monotonic()
        last = getattr(wj, "_activity_at", None)
        if last is None:
            # No baseline: the job reached a rip state without _run_job stamping
            # one — its task died early, or it was restored mid-rip. This used to
            # return False, so "no data" read as "healthy" and the job sat on
            # RIPPING forever with no stall flag and no Requeue button. Stamp
            # first sight instead, so there is something to measure from.
            last = wj._activity_at = now
        if (now - last) > window:
            return True
        # A rip only ever runs as a LOCAL task on the rip box — workers encode,
        # they never rip. So no live task means nothing is moving this job, and
        # waiting out the full no-progress window would just delay saying so.
        task = getattr(wj, "_task", None)
        if task is not None and not task.done():
            return False
        return (now - last) > self._RIP_TASK_GRACE

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
                # Its OWN try block: this is a WRITE, and sharing one with the read
                # below reported a failed heartbeat as "list_workers failed", so a
                # write-only outage (a read replica still answering, a permissions
                # or disk-full error on the workers table) blamed the wrong call —
                # on the very line that exists to explain the failure.
                await asyncio.to_thread(self._record_serve_presence)
            except Exception as exc:
                tick_ok = False
                self._log_store_error("record_worker", exc)
            try:
                self._workers_cache = await asyncio.to_thread(self.store.list_workers)
                self._check_worker_presence()
            except Exception as exc:
                tick_ok = False
                self._log_store_error("list_workers", exc)
            try:
                await asyncio.to_thread(self._fire_due_maintenance)
            except Exception as exc:
                tick_ok = False
                self._log_store_error("maintenance", exc)
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
            if tick_ok and self._last_store_error:
                print("[store] reconnected — database reachable again")
                self._last_store_error.clear()
            if tick_ok:
                # Reconverge, rather than only reading. Both of these left the
                # box wrong in a way no amount of waiting fixed: a save that
                # failed during the outage (the job is invisible to workers) and
                # a recover() that could not run at startup (the queue reads as
                # empty while workers encode jobs it never loaded).
                try:
                    saved = await asyncio.to_thread(self._flush_unpersisted)
                except Exception:
                    saved = 0
                if self._recover_pending:
                    try:
                        await asyncio.to_thread(self.recover)
                        self._recover_pending = False
                        print("[store] loaded the jobs that were unreachable at startup")
                        saved += 1
                    except Exception as exc:
                        self._log_store_error("recover", exc)
                if saved:
                    self.bus.publish()
            await asyncio.sleep(self.settings.cluster.worker_poll_seconds)

    def _log_store_error(self, op: str, exc: Exception) -> None:
        # A persistent store failure (schema drift, bad row) would otherwise make
        # the UI silently stop reflecting worker progress with zero diagnostics.
        # Log the cause once per distinct message, not every poll.
        # Keyed PER OPERATION. With one shared slot a total outage failed
        # list_workers and load_all alternately with different messages, so each
        # call saw a different "last" and printed again — two lines every poll,
        # forever, which is the every-tick spam this exists to prevent, and only
        # in the total-outage case that actually matters.
        msg = f"[store] {op} failed: {type(exc).__name__}: {exc}"
        if self._last_store_error.get(op) != msg:
            print(msg)
            self._last_store_error[op] = msg

    def _clear_store_error(self, op: str) -> None:
        """A clean call: forget the last error so a NEW outage is reported."""
        self._last_store_error.pop(op, None)

    async def _watch_drives(self) -> None:
        while not self.closing:
            try:
                self._media_snapshot = await asyncio.to_thread(os_media_snapshot)
                self._drives_cache = await self.list_drives()
                self._drives_scanned = True
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
            waiting = self._jobs_awaiting_this_disc(name)
            if waiting:
                # Putting the disc back IS the next step for these, so starting a
                # second job for it is exactly wrong: the duplicate then competes
                # with the original for the same drive, and the review the job is
                # parked for never happens.
                self._last_disc[d.index] = name
                where = self.drive_names.get(d.device)
                for wj in waiting:
                    wj.source = source   # point it at the drive it is actually in
                    wj.log.append(stamp_log(
                        f"Disc is back in {where or d.name or source}. This job is "
                        "still parked for review — use its rip button to start it. "
                        "No new job was created for this disc."))
                    self._persist(wj)
                self.bus.publish()
                self._notify(
                    f"💿 Disc back for review: {name}",
                    f"Job #{waiting[0].id} was waiting for this disc."
                    if len(waiting) == 1 else
                    f"{len(waiting)} parked jobs match this disc.")
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

    def _jobs_awaiting_this_disc(self, disc_name: str) -> list[WebJob]:
        """Parked jobs that are waiting for exactly this disc to come back.

        _disc_has_active_job deliberately ignores failed jobs, which is right for
        "that disc failed, here is a different one" — but wrong for the case the
        review gate creates, where the disc going back in is the intended next
        step. Matched on the volume label, the same key that function uses.

        Only jobs that still need a rip qualify: one that failed AFTER ripping
        has its file already and must not be re-ripped just because the disc
        happens to be in the drive."""
        return [
            wj for wj in self.jobs.values()
            if self._live_state(wj) is JobState.FAILED
            and wj.disc is not None
            and (wj.disc.disc_name or "").strip() == disc_name
            and self._rip_pending(wj)
        ]

    def _unreadable_in_this_drive(self, wj: WebJob) -> str | None:
        """Why this disc cannot be read in the drive it is sitting in, or None.

        One case so far, and it is the expensive one: a UHD disc in a drive with
        no LibreDrive. MakeMKV cannot read those at all — it fails a few hundred
        KB in with "HARDWARE ERROR:TIMEOUT ON LOGICAL UNIT" and keeps retrying,
        and those retries block drive enumeration for EVERY drive on the box.
        Two physically identical drives can differ here, because LibreDrive comes
        from patched firmware, so this is not something an operator can see.

        Deliberately conservative: it needs the disc's capacity from the OS (we
        have no opinion off Linux) AND a recorded observation that this specific
        drive lacks LibreDrive. Anything unknown proceeds as before.
        """
        src = (wj.source or "").strip()
        if not src.startswith(("disc:", "dev:")):
            return None
        drive = self._drive_for_source(src)
        if drive is None or self.drive_caps.libredrive(drive.name) is not False:
            return None
        size = os_disc_capacity_bytes(drive.device)
        if size is None or size < UHD_CAPACITY_FLOOR_BYTES:
            return None
        here = self.drive_names.get(drive.device) or drive.name
        return (
            f"This looks like a UHD disc ({size / 1_000_000_000:.0f} GB) and "
            f"{here} has no LibreDrive, so MakeMKV cannot read it — it fails in "
            f"the first megabyte and its retries block drive detection for every "
            f"other drive on this machine. {self._uhd_drive_hint()}")

    def _uhd_drive_hint(self) -> str:
        """Where the disc should go instead, named the way the panel names it."""
        for name in self.drive_caps.uhd_capable_drives():
            drive = next((d for d in self._drives_cache if d.name == name), None)
            if drive is None:
                continue
            label = self.drive_names.get(drive.device)
            return (f"Move it to {label or name}"
                    + (f" ({name})" if label else "") + ", then Retry.")
        return "Move it to a drive with LibreDrive, then Retry."

    def _drive_for_source(self, src: str) -> Drive | None:
        """The cached Drive a 'disc:N' / 'dev:/dev/srN' source points at."""
        selector = src.split(":", 1)[1] if ":" in src else ""
        if src.startswith("disc:") and selector.isdigit():
            idx = int(selector)
            return next((d for d in self._drives_cache if d.index == idx), None)
        return next((d for d in self._drives_cache if d.device == selector), None)

    def _disc_is_loaded(self, wj: WebJob) -> bool:
        """Whether this job's disc is in a drive right now, so the UI can say so
        instead of leaving you to work out which of 50 parked jobs you just fed."""
        if wj.disc is None:
            return False
        name = (wj.disc.disc_name or "").strip()
        if not name:
            return False
        return any((d.disc_name or "").strip() == name for d in self._drives_cache)

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
        label = (job.disc.volume_label or job.disc.disc_name or "").strip()
        try:
            rows = boxset_plan.load_rows(self.settings)
        except Exception as exc:
            # parse_csv promises never to raise, but it does: one unbalanced quote
            # makes csv swallow the rest of the file into a single field and blow
            # the 128 KiB field limit, and a plan saved as Windows-1252 raises
            # UnicodeDecodeError. This runs on EVERY scanned disc, so a stray quote
            # in the plan otherwise fails every disc you insert with a csv error.
            # A plan we cannot read means "no plan": name this one manually.
            reporter.on_event(
                f"Box-set plan: couldn't read {boxset_plan.plan_path(self.settings)} "
                f"({type(exc).__name__}: {exc}) — naming this one manually. Fix the "
                "CSV (a stray double-quote or a non-UTF-8 save is the usual cause) "
                "to get hands-off ripping back.")
            return False
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
        if row.year is not None and row.year <= 0:
            # A "0" in the year cell parses as the integer 0, which is falsy but not
            # None — so it silently skipped the year-matched TMDB lookup, reported
            # "No “Show” from 0 on TMDB" without ever having searched, and suppressed
            # the no-year warning below (which tests `is None`). Treat it as absent.
            reporter.on_event(
                f"Box-set plan: ignoring the year “{row.year}” for “{row.show}” — "
                "that isn't a year. Treating the row as having none.")
            row = replace(row, year=None)
        result = boxset_plan.assignments_for(row, job.disc)
        if not result.ok:
            reporter.on_event(
                f"Box-set plan matched “{row.disc_label}” but didn't apply: "
                f"{result.reason}. Confirm this one manually.")
            return False
        if row.year is None:
            # Without a year the Plex folder is just "Show", so Plex picks whichever
            # series it thinks likeliest — the 2010 remake for Hawaii Five-O.
            reporter.on_event(
                f"Box-set plan row for “{row.show}” has no year — the library folder "
                "will have none either, so Plex may match a remake instead of the "
                "original. Add a year column to pin it.")
        tmdb_id = await self._fill_episode_titles(
            result.assignments, row, result.season, reporter)
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

    async def _fill_episode_titles(self, assignments, row, season: int | None,
                                   reporter=None) -> int | None:
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
            # separates a remake from the original (Hawaii Five-O 1968 vs 2010).
            show = next((s for s in shows if s.year == row.year), None) if row.year else None
            if show is None and row.year is not None:
                # An explicit year that matches nothing TMDB returned. Falling back
                # to the most popular hit is how a 1968 disc got the 2010 remake's
                # episode titles — the years on offer are the useful thing to report.
                years = ", ".join(str(s.year) for s in shows[:5] if s.year)
                if reporter is not None:
                    reporter.on_event(
                        f"No “{row.show}” from {row.year} on TMDB"
                        + (f" (found: {years})" if years else "")
                        + " — using episode numbers only. Check the show/year in the "
                          "box-set row; TMDB may spell the title differently.")
                return None
            if show is None:
                same_name = [s for s in shows
                             if (s.name or "").strip().casefold() == row.show.strip().casefold()]
                if len(same_name) > 1:
                    # A remake shares its name with the original. Taking TMDB's most
                    # popular result would confidently apply the WRONG show's episode
                    # titles, so number them instead and say why.
                    years = ", ".join(str(s.year) for s in same_name if s.year)
                    if reporter is not None:
                        reporter.on_event(
                            f"Several shows are called “{row.show}” ({years}) and the plan "
                            "row has no year — using episode numbers only. Add a year "
                            "column to get episode titles and the right Plex folder.")
                    return None
                show = shows[0]
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
             "uptime": round(w.uptime_seconds), "address": w.address}
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
        """Add a disc selector (disc:/dev:/file:/iso:), a single .mkv file, a
        directory of .mkv files, or a bare path to an .iso or a BDMV/VIDEO_TS
        folder. Returns the created job id(s).

        A path that doesn't resolve raises instead of falling through to a disc
        scan: makemkvcon can't parse a bare filesystem path, so that fallback
        turned "this file isn't where you said" into a baffling MakeMKV error.
        """
        source = source.strip().strip('"').strip("'")  # Explorer's "Copy as path" quotes
        explicit_file = source.startswith("mkv:")
        raw = source[4:].strip() if explicit_file else source
        if not explicit_file and source.startswith(SOURCE_SELECTORS):
            return [self.add_disc(source).id]
        if not raw:
            # Path("") is Path("."), which IS a directory — so a blank source used
            # to glob the SERVE PROCESS'S working directory and queue a job for
            # every .mkv it happened to contain. Refuse it like any other source
            # that doesn't resolve.
            raise ValueError(
                "No source given — enter a drive (disc:0), a path to an .mkv, or a "
                "folder of .mkv files.")

        path = Path(raw)
        kind = self._path_kind(path)
        if kind == "dir":
            mkvs = sorted(path.glob("*.mkv"))
            if mkvs:
                # One show's episodes (MakeMKV's <base>_tNN.mkv) become ONE job with
                # a title per file, so it's named once like a TV disc. A folder of
                # unrelated films stays one job per file — they need separate names,
                # and separate jobs also encode in parallel across workers.
                mkv_set = mkv_folder.analyse(mkvs)
                if mkv_set is not None:
                    return [self._add_mkv_set(mkv_set).id]
                return [self._add_mkv(m).id for m in mkvs]
            if not explicit_file and ((path / "BDMV").is_dir() or (path / "VIDEO_TS").is_dir()):
                return [self.add_disc(f"file:{path}").id]  # a ripped disc folder
            raise ValueError(f"No .mkv files found in {path}")
        if kind == "file":
            if path.suffix.lower() == ".mkv":
                return [self._add_mkv(path).id]
            if not explicit_file and path.suffix.lower() == ".iso":
                return [self.add_disc(f"iso:{path}").id]
            raise ValueError(
                f"{path.name} isn't a .mkv. Only .mkv files can be added directly; "
                f"for a disc image use iso:{path}.")
        longest = max((len(part) for part in path.parts), default=0)
        if longest > 255:
            raise ValueError(
                f"That path has a {longest}-character name in it, which is longer "
                "than any common filesystem allows (255). Nothing can exist at "
                "it — check for a pasted line-wrap or a missing separator.")
        raise ValueError(
            f"Nothing at {path}. Check the path — and note the server only sees "
            f"drives IT has mounted, so a mapped drive letter (Z:) from your own "
            f"session usually isn't visible to it. Use the UNC path instead.")

    @staticmethod
    def _path_kind(path: Path) -> str | None:
        """``'dir'``, ``'file'``, or None for anything the OS can't resolve.

        pathlib only swallows ENOENT/ENOTDIR/EBADF/ELOOP and re-raises the rest,
        so a source with a component past the OS name limit (~255 chars) threw
        OSError straight out of add_source — a 500 from POST /api/jobs instead of
        the 400 the endpoint promises for a source it can't resolve. Any errno
        here means the same thing to the caller: there is nothing usable there.
        """
        try:
            if path.is_dir():
                return "dir"
            if path.is_file():
                return "file"
        except OSError:
            return None
        return None

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

    def _add_mkv_set(self, mkv_set) -> WebJob:
        """One job for a folder of one show's episodes — a title per file."""
        wj = WebJob(id=next(self._ids), source=str(mkv_set.files[0].parent))
        self.jobs[wj.id] = wj
        self._persist(wj)
        wj._task = self._spawn(self._scan_mkv_set(wj, mkv_set))
        self.bus.publish()
        return wj

    # A file job in one of these has no rip or encode work to throw away, so it
    # can still be folded into a season set.
    _GROUPABLE_STATES = (JobState.AWAITING_TITLES, JobState.AWAITING_MAPPING)

    def _mkv_source(self, wj: WebJob) -> Path | None:
        """This job's .mkv path, or None if it isn't a plain single-file job."""
        src = (wj.source or "").strip()
        if not src or src.startswith(SOURCE_SELECTORS):
            return None
        path = Path(src)
        return path if path.suffix.lower() == ".mkv" else None

    def _group_candidates(self, wj: WebJob):
        """The jobs that would merge with `wj` into one season job, and the set.

        Adding a folder groups episodes automatically, but files added one at a
        time — or added before grouping existed — sit as N separate jobs wanting
        the same show name typed N times. Same filename heuristic, applied after
        the fact, so it can't group what a folder scan wouldn't have."""
        if wj.state not in self._GROUPABLE_STATES:
            return [], None
        path = self._mkv_source(wj)
        if path is None:
            return [], None
        peers = {}
        for other in self.jobs.values():
            if other.state not in self._GROUPABLE_STATES:
                continue
            opath = self._mkv_source(other)
            if opath is not None and opath.parent == path.parent:
                peers[opath] = other
        if len(peers) < mkv_folder.MIN_SET:
            return [], None
        mkv_set = mkv_folder.analyse(sorted(peers))
        if mkv_set is None:
            return [], None
        members = [peers[p] for p in mkv_set.files if p in peers]
        return (members, mkv_set) if len(members) >= mkv_folder.MIN_SET else ([], None)

    async def group_season(self, job_id: int) -> int:
        """Fold this job and its sibling episode files into ONE job, named once."""
        wj = self.jobs.get(job_id)
        if wj is None:
            raise ValueError("No such job.")
        members, mkv_set = self._group_candidates(wj)
        if not members:
            raise ValueError(
                "These files don't look like one show's episodes, so grouping "
                "them would name separate films as episodes of each other. "
                "Name them one at a time instead.")
        ids = [o.id for o in members]
        for old_id in ids:
            await self.remove(old_id)
        new = self._add_mkv_set(mkv_set)
        new.log.append(stamp_log(
            f"Grouped job(s) {', '.join('#' + str(i) for i in ids)} into this one "
            f"— {len(mkv_set.files)} episode file(s) to name once. The files "
            "themselves were not touched."))
        self._persist(new)
        self.bus.publish()
        return new.id

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
        restart — the cluster-wide version of a worker's 'u' key."""
        return self.request_worker_action("update", ALL_WORKERS)

    def request_worker_action(self, action: str, target: str = ALL_WORKERS) -> int:
        """Ask one worker (or all, with ``*``) to drain / restart / update.

        Every action drains first — the current encode is finished and placed, so
        nothing in flight is ever thrown away. They differ in what happens next:
        drain exits, restart re-execs the code on disk, update pulls then re-execs.
        Returns how many workers the request addresses (for the confirmation)."""
        if action not in WORKER_ACTIONS:
            raise ValueError(f"Unknown worker action “{action}”.")
        if not self.store.durable:
            raise ValueError(
                "Controlling workers needs the shared Postgres store "
                "(set BLURAYRIPPER_DATABASE_URL) — there are no remote workers otherwise.")
        known = [w.worker_id for w in self._workers_cache
                 if not presence.is_serve(w.worker_id)]
        if target != ALL_WORKERS and target not in known:
            # A typo'd or long-gone name would just sit in the table unread.
            raise ValueError(f"No worker named “{target}” is connected.")
        self.store.request_cluster_signal(action, target)
        return len(known) if target == ALL_WORKERS else 1

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
            # Almost always a second click on a confirm that already worked, so
            # say that rather than something that reads like a fault. The UI
            # disables the button now, but a slow tick or two tabs can still
            # produce it, and "is not awaiting a naming mapping" sends people
            # looking for a problem that does not exist.
            if wj.assignments:
                raise ValueError(
                    f"Job {job_id} was already confirmed — it is "
                    f"{self._live_state(wj).value.replace('_', ' ')} now. Nothing "
                    "was lost; use Rename if you need to change the naming.")
            raise ValueError(f"Job {job_id} is not awaiting a naming mapping.")
        self._refuse_colliding_assignments(assignments)
        wj.assignments = {a.title_id: a for a in assignments}
        wj.selected_title_ids = [a.title_id for a in assignments]
        wj.grain_mode = grain_mode
        wj.tune_grain = tune_grain
        wj.state = JobState.READY
        self._start_job(wj)

    def _refuse_colliding_assignments(self, assignments) -> None:
        """Stop two titles being filed under one name, overwriting each other.

        Two cuts of a film carry the same title and year, so without a distinct
        Edition they compute the SAME output path and the second silently
        replaces the first — the exact loss the title gate was opened to prevent,
        moved one step later. The same check catches two titles mapped to one
        episode number."""
        seen: dict[str, object] = {}
        for a in assignments:
            try:
                dest = str(self.pipeline._video_path_for(a))
            except Exception:
                continue            # unnameable yet; the pipeline will complain
            other = seen.get(dest)
            if other is not None:
                raise ValueError(
                    f"Titles {other.title_id} and {a.title_id} would both be saved "
                    f"as \"{Path(dest).name}\", so one would overwrite the other. "
                    f"Give them different Editions (Plex shows "
                    f"\"{{edition-Theatrical}}\" and \"{{edition-Unrated}}\" as two "
                    f"versions of one film), or untick one.")
            seen[dest] = a

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

    def _active_duplicate(self, wj: WebJob) -> WebJob | None:
        """Another job, still in flight, that is this exact physical disc.

        Jobs 311 and 350 were both "Happy Feet 2" in disc:1 and both ripped it at
        once. A per-drive lock stops them overlapping, but two jobs for one disc
        still means ripping it twice and placing the film twice. The fingerprint
        is the disc's own identity, so it catches the duplicate even when the two
        were added from different drives.

        DONE and FAILED are deliberately not duplicates: re-ripping a disc you
        already finished is a legitimate thing to ask for."""
        fp = wj.fingerprint
        if not fp:
            return None                     # unscanned: nothing to compare yet
        others = [o for o in self.jobs.values()
                  if o.id != wj.id and o.fingerprint == fp
                  and o.state not in (JobState.DONE, JobState.FAILED)]
        return min(others, key=lambda o: o.id) if others else None

    async def _scan_job(self, wj: WebJob) -> None:
        reporter = _Reporter(self, wj)
        started = time.time()   # bounds the AACS dump search on failure (see below)
        # Before the scan, not after: scanning a UHD disc in a drive that cannot
        # read it doesn't just fail, it retries at the SCSI layer until the whole
        # drive enumeration is blocked — which makes every other drive look dead
        # and strands unrelated jobs. Covers Retry too, since that re-enters here.
        unreadable = self._unreadable_in_this_drive(wj)
        if unreadable is not None:
            wj.state = JobState.FAILED
            wj.error = unreadable
            reporter.on_event(unreadable)
            self._persist(wj)
            self.bus.publish()
            return
        try:
            job = await self.pipeline.scan_disc(wj.source, reporter)
            wj._pipeline_job = job
            wj.disc = job.disc
            wj.fingerprint = job.fingerprint
            # What this scan taught us about the DRIVE, not the disc. Only a scan
            # that opened a disc knows, and only it can teach the check above.
            await asyncio.to_thread(
                self.drive_caps.note_libredrive, job.disc.drive_name, job.disc.libredrive)
            dupe = self._active_duplicate(wj)
            if dupe is not None:
                # Park instead of ripping. FAILED + a pending rip is what puts a
                # job in "Needs review", which is exactly the decision here: keep
                # one, drop the other. Retry works once the other job finishes.
                wj.state = JobState.FAILED
                wj.error = (
                    f"Job {dupe.id} ({self._display_name(dupe)}) is already queued "
                    f"for this same disc and has not finished, so this job would "
                    f"rip it a second time and place the film twice. Delete "
                    f"whichever you don't want; Retry here once job {dupe.id} is done.")
                reporter.on_event(wj.error)
                self._persist(wj)
                self.bus.publish()
                return
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
                if not job.disc.titles:
                    # Nothing to choose from. Parking at the title gate would show
                    # an empty table and a Confirm button that can only ever error,
                    # with no way forward. Fail instead: that surfaces Retry (and
                    # "rip from another drive" for a disc), which are the actual
                    # fixes. MakeMKV's own MSG lines are already in the log above.
                    raise RuntimeError(
                        "The scan found no titles on this disc. Usually an unreadable "
                        "or dirty disc, a drive that can't read it, or a protection "
                        "MakeMKV couldn't handle — see the log above for its reason. "
                        "Clean the disc, or use another drive and Retry.")
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
                    if discinfo.guess_kind(job.disc) is MediaKind.MOVIE:
                        # Say why this one stopped when a single-cut disc would
                        # have sailed through, and say it before the operator
                        # wonders whether something went wrong.
                        runs = ", ".join(
                            _hms(t.duration_seconds)
                            for t in discinfo.feature_titles(job.disc))
                        reporter.on_event(
                            f"This disc carries {len(suggested)} feature-length cuts "
                            f"({runs}) — usually a theatrical and an extended or "
                            f"unrated version. Both are ticked: untick any you "
                            f"don't want, and give each one a different Edition so "
                            f"they don't land on the same filename.")
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
            self._keep_aacs_dump(wj, started, reporter)
            self._persist(wj)
            self.bus.publish()

    def _keep_aacs_dump(self, wj: WebJob, started: float, reporter) -> None:
        """A disc whose volume key MakeMKV doesn't know fails to open, having
        written a .tgz dump into its own data directory — which is configurable,
        easy to miss, and where the NEXT failing disc drops another one.

        Copy any dump from this scan somewhere findable and say where it went,
        plus where to email it. Detection is by timestamp rather than MakeMKV's
        message wording, which differs between versions. Best-effort throughout:
        a disc failed already, and nothing here may make that worse."""
        try:
            found = aacs_dump.find_new_dump(aacs_dump.data_dir(), started)
            if found is None:
                return
            dest = aacs_dump.collect(
                found, self.settings.storage.staging_dir / "aacs-dumps",
                wj.disc.disc_name if wj.disc else None)
            wj.aacs_dump = str(dest)
            reporter.on_event(aacs_dump.advice(dest))
            self._notify(f"🔑 Unknown disc key: {self._name_of(wj)}",
                         f"Job #{wj.id} — AACS dump saved for submission.")
        except Exception as exc:
            reporter.on_event(f"(Couldn't keep the AACS dump: {type(exc).__name__}.)")

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

    async def _scan_mkv_set(self, wj: WebJob, mkv_set) -> None:
        """Set up ONE job from a folder of one show's episode files.

        Same trick as _scan_mkv, a title per file: build a synthetic disc whose
        titles are the files and pre-set each rip checkpoint, so the pipeline
        skips ripping and the normal TV mapping form ("Fill in order") names the
        whole season at once. The disc name is the shared stem, which is what
        pre-fills the show and season in that form."""
        reporter = _Reporter(self, wj)
        try:
            reporter.on_event(
                f"Reading {len(mkv_set.files)} file(s) from {mkv_set.base} …")
            titles, checkpoints = [], {}
            for tid, path in enumerate(mkv_set.files):
                try:
                    probe = await self.pipeline.mkvtools.probe(path)
                    audio_langs, subs, duration = (
                        probe.audio_languages, probe.subtitles, probe.duration_seconds)
                except Exception as exc:
                    reporter.on_event(f"Probe failed for {path.name} ({exc}).")
                    audio_langs, subs, duration = [], [], 0
                streams = [Stream(stream_id=0, stream_type=StreamType.VIDEO)]
                sid = 1
                for lang in audio_langs:
                    streams.append(Stream(stream_id=sid, stream_type=StreamType.AUDIO,
                                          language=lang))
                    sid += 1
                for s in subs:
                    streams.append(Stream(stream_id=sid, stream_type=StreamType.SUBTITLE,
                                          language=s.language))
                    sid += 1
                titles.append(Title(
                    title_id=tid, duration_seconds=duration,
                    size_bytes=path.stat().st_size if path.exists() else 0,
                    source_filename=path.name, streams=streams))
                checkpoints[tid] = {"ripped_path": str(path)}  # skip rip: reuse the file

            disc = DiscInfo(source=wj.source, disc_name=mkv_set.base, titles=titles)
            job = Job(source=wj.source)
            job.disc = disc
            job.selected_title_ids = [t.title_id for t in titles]
            job.checkpoints = checkpoints

            wj._pipeline_job = job
            wj.disc = disc
            wj.selected_title_ids = list(job.selected_title_ids)
            wj.state = JobState.AWAITING_MAPPING   # titles are known; just name them
            reporter.on_event(
                f"{len(titles)} episode file(s) — name the set once "
                "(season and first episode, then Fill in order).")
            self._persist(wj)
            self.bus.publish()
            self._notify(f"🟡 Needs your input: {mkv_set.base}",
                         f"Job #{wj.id} — {len(titles)} episode file(s) to name.")
        except Exception as exc:
            wj.state = JobState.FAILED
            wj.error = f"{type(exc).__name__}: {exc}"
            reporter.on_event(f"Setup failed: {wj.error}")
            self._persist(wj)
            self.bus.publish()

    async def _run_job(self, wj: WebJob) -> None:
        reporter = _Reporter(self, wj)
        wj._activity_at = time.monotonic()  # baseline for the rip-stall watchdog
        wj._log_mark = len(wj.log)          # a failure hint reads only this run
        job = wj._pipeline_job
        job.selected_title_ids = wj.selected_title_ids
        job.assignments = wj.assignments
        job.grain_mode = wj.grain_mode
        job.tune_grain = wj.tune_grain
        try:
            if wj.auto_identify and not wj.assignments:
                # Rip + OCR + subtitle-match against the disc's candidate episodes.
                # Parks at AWAITING_MATCH if any title is uncertain.
                async with self._prep_slot(wj):
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
                async with self._prep_slot(wj):
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
                # Leave the rip lane BEFORE the encode starts. Without this the
                # job keeps whatever state prep left it in (RIPPING) until
                # _encode_title gets past the encode scheduler, so a job that has
                # already ejected its disc still shows as ripping, still counts
                # in "Ripping N", and — because RIPPING is a drive-held state —
                # still pins its drive in the Drives panel.
                job.state = wj.state = JobState.ENCODING
                self._persist(wj)
                self.bus.publish()
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
            async with self._prep_slot(wj):  # single-box: bound concurrent jobs
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
        if not new_source.startswith(SOURCE_SELECTORS):
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
        elif new_source.startswith("dev:"):
            # A dev: selector IS the OS device — the same reading makemkv.scan does.
            new_device = new_source.split(":", 1)[1].strip() or None
        # Whatever we worked out, the OLD device must go: it names the drive this
        # job just moved OFF. Keeping it (which is what `if new_device:` did when
        # the cache was cold — right after a restart, or a drive makemkvcon hasn't
        # enumerated yet) pointed auto-eject and cover-art at a drive now holding
        # someone else's disc. None just means "re-resolve it", which the eject
        # path already does from the source selector.
        job = wj._pipeline_job
        if job is not None:
            job.source = new_source
            if getattr(job, "disc", None) is not None:
                job.disc.source = new_source
                job.disc.drive_device = new_device
        if wj.disc is not None:  # same object as job.disc when both exist; be safe
            wj.disc.source = new_source
            wj.disc.drive_device = new_device
        wj.log.append(stamp_log(f"Rip source changed to {new_source} — click Retry to rip from it."))
        if not new_device and new_source.startswith("disc:"):
            wj.log.append(stamp_log(
                f"(No OS device known for {new_source} yet — it is resolved when the "
                "rip starts. The previous drive's device was dropped, so nothing can "
                "eject the disc that is in the OLD drive now.)"))
        self._persist(wj)
        self.bus.publish()

    def _staging_readable(self) -> bool:
        """Is the staging root actually there? See the caller for why it matters."""
        try:
            root = self.settings.storage.staging_dir
            return root.is_dir() and any(True for _ in root.iterdir())
        except OSError:
            return False

    def _rip_file_missing(self, wj: WebJob) -> str | None:
        """The first ripped file this job records that is not on disk, or None.

        Serve trusts the checkpoint everywhere else, which is exactly how 187
        jobs sat in the encode lane that no worker would ever take: the
        checkpoint said ripped, the disk disagreed, and nothing reconciled them.
        """
        from ..pipeline import _is_abs_stored, _resolve_path

        job = wj._pipeline_job
        if job is None:
            return None
        staging = self.settings.storage.staging_dir
        for tid in (wj.selected_title_ids or []):
            cp = (job.checkpoints or {}).get(tid) or {}
            if cp.get("placed"):
                continue
            prior = cp.get("ripped_path")
            if not prior:
                continue                 # no claim of a rip; _rip_pending covers it
            # ONLY rips this box made, i.e. paths stored relative to staging.
            # A job added from a file stores the SOURCE .mkv here, as an absolute
            # path from whichever machine added it. Parking one of those clears a
            # checkpoint that cannot be rebuilt -- there is no disc to re-rip --
            # so a path this box merely cannot resolve turned 121 healthy jobs
            # into permanently failed ones. The pipeline already reports a
            # genuinely missing source when the job runs, with the real reason.
            if _is_abs_stored(prior):
                continue
            try:
                if not _resolve_path(staging, prior).exists():
                    return str(_resolve_path(staging, prior))
            except OSError:
                # "I could not tell" is not "it is gone". A CIFS blip must not
                # park a job whose file is sitting there.
                continue
        return None

    def _drop_stale_rip_checkpoints(self, wj: WebJob) -> None:
        """Forget rip checkpoints whose files are gone.

        The checkpoint is what makes the job look encode-ready, so clearing it
        is what puts a disc job back in front of the operator with its rip
        button instead of leaving it queued for an encode that cannot happen."""
        from ..pipeline import _is_abs_stored, _resolve_path

        job = wj._pipeline_job
        if job is None:
            return
        staging = self.settings.storage.staging_dir
        for tid, cp in list((job.checkpoints or {}).items()):
            if not isinstance(cp, dict) or cp.get("placed"):
                continue
            prior = cp.get("ripped_path")
            if not prior or _is_abs_stored(prior):
                continue                 # see _rip_file_missing: never a source file
            try:
                gone = not _resolve_path(staging, prior).exists()
            except OSError:
                gone = False             # unknown is not gone
            if gone:
                cp.pop("ripped_path", None)

    def _rip_pending(self, wj: WebJob) -> bool:
        """True if this disc job still has a selected title left to rip — i.e. the
        disc is still needed. False once every selected title has a ripped-file
        checkpoint (an encode/subtitle/place failure no longer needs the disc).

        ``dev:`` counts as a disc source. It was excluded here while the three
        callers (recover's review gate, retry_rips, needs_review) all tested for
        ``disc:`` OR ``dev:``, so the dev: half of every one of them was dead: a
        dev:-sourced job whose rip never finished was never parked for review,
        never flagged, and re-inserting its disc created a duplicate job because
        _jobs_awaiting_this_disc couldn't match it either.
        """
        if not (wj.source or "").startswith(("disc:", "dev:")):
            return False
        job = wj._pipeline_job
        selected = wj.selected_title_ids or (getattr(job, "selected_title_ids", []) if job else [])
        if not selected:
            return True  # nothing ripped yet
        cps = getattr(job, "checkpoints", {}) if job else {}
        return any(not (cps.get(t) or {}).get("ripped_path") for t in selected)

    def _source_file_missing(self, wj: WebJob) -> str | None:
        """Why a file-sourced job can't proceed, or None if its file is there.

        Retrying can't conjure a missing file back, so this must not burn the
        auto-retry budget. The common cause is a path from another machine: a
        file job stores an ABSOLUTE source (unlike rip checkpoints, which are
        relative to staging_dir), so moving the server leaves it pointing at the
        old box's path."""
        src = (wj.source or "").strip()
        if not src or src.startswith(SOURCE_SELECTORS):
            return None
        if Path(src).exists():
            return None
        looks_foreign = "\\" in src or (len(src) > 1 and src[1] == ":")
        return (
            f"The source file isn't reachable: {src}. There's no disc to re-rip it "
            "from, so retrying won't help. "
            + ("That looks like a path from another machine — remove this job and "
               "re-add the file from its path here."
               if looks_foreign else
               "Put the file back (or check the server can reach it), then Retry.")
        )

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
        # Not in the cache. Once we've enumerated successfully at least once, that
        # means the drive isn't attached at all — so the disc certainly isn't in it,
        # and retrying is pointless. Before the first scan we genuinely don't know.
        return self._drives_scanned

    # MakeMKV failure signatures that won't fix themselves on retry — the drive is
    # offline or the disc/drive can't be read. Matched (lower-cased) against the
    # job error + recent log.
    # The drive itself is gone. "failed to open disc" is deliberately NOT here:
    # it is also what an unknown AACS volume key prints, and the two remedies
    # are opposites — power-cycle a healthy drive vs submit a key dump.
    _DRIVE_OFFLINE_SIGNS = (
        "no usable optical drives", "device does not exist", "no such device")
    _READ_ERROR_SIGNS = ("scsi error", "read error", "hash check failed", "fetchframes")
    # Ambiguous on its own: could be a dead drive, a disc the drive cannot read,
    # or a missing volume key. Worth saying, but NOT worth suppressing a retry.
    _OPEN_FAILED_SIGNS = ("failed to open disc",)

    def _run_log_mark(self, wj: WebJob) -> int:
        """Where THIS run's log lines start, so a hint never reads an old phase."""
        return min(getattr(wj, "_log_mark", 0), len(wj.log))

    def _rip_failure_hint(self, wj: WebJob) -> tuple[str, bool] | None:
        """(hint, is_terminal) for a drive/read failure, or None.

        Scoped to THIS run. It used to read the last 12 log lines whatever run
        they came from, so one "Failed to open disc" during a scan relabelled
        every later failure on that job — an encoder crash reported as a drive
        that dropped off the bus, with the wrong remedy pushed to the phone and
        the auto-retry it deserved suppressed.
        """
        blob = ((wj.error or "") + "\n"
                + "\n".join(wj.log[self._run_log_mark(wj):])).lower()
        if any(s in blob for s in self._DRIVE_OFFLINE_SIGNS):
            return ("The optical drive became unavailable to MakeMKV — it likely dropped "
                    "off the bus. The disc being inserted isn't enough: check the drive's "
                    "power/connection or reset it (or use another drive), then click Retry.",
                    True)
        if any(s in blob for s in self._READ_ERROR_SIGNS):
            return ("Disc read error — the drive couldn't read the disc (a bad sector, a "
                    "dirty/scratched disc, or the drive dropping mid-read). Clean the disc "
                    "or reset/try another drive, then click Retry.", True)
        if any(s in blob for s in self._OPEN_FAILED_SIGNS):
            return ("MakeMKV couldn't open the disc. That is either a disc it has no "
                    "volume key for (check this job for a saved AACS dump), a disc it "
                    "can't read, or a drive that has stopped responding — so this is "
                    "retried anyway in case it was transient.", False)
        return None

    def _schedule_auto_retry(self, wj: WebJob) -> None:
        """After a failure, re-queue the job up to auto_retry_max times with
        exponential backoff (30s, 60s, 120s… capped 5m). Bounded so a genuinely
        broken job doesn't loop forever."""
        # A drive/read failure (or a genuinely absent disc) won't recover on its
        # own — surface a specific reason and don't burn retries against it.
        found = self._rip_failure_hint(wj)
        hint, terminal = found if found else (None, True)
        if hint is not None and not terminal:
            # Say it, but still let the retry budget do its work.
            wj.log.append(stamp_log(hint))
            hint = None
        if hint is None:
            hint = self._source_file_missing(wj)
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

    def _drive_key(self, wj: WebJob) -> str | None:
        """The physical drive a job needs, or None if it reads a file.

        'disc:1' and 'dev:/dev/sr0' name a drive; 'file:' and 'iso:' do not, and
        those must not be serialised against each other."""
        src = (wj.source or "").strip().lower()
        return src if src.startswith(("disc:", "dev:")) else None

    @contextlib.asynccontextmanager
    async def _prep_slot(self, wj: WebJob):
        """Hold this job's drive (if it needs one) and a prep slot.

        Jobs 311 and 350 were the same disc in the same reader, and BOTH ripped
        it at once: prep_concurrency=2 let each take a slot, and nothing keyed
        the gate to the drive. Two makemkvcon processes then fought over one
        optical head — 1.6 MB/s (0.4x) instead of a normal rip, with two
        half-speed reads of the same disc landing in two staging dirs.

        The drive lock is taken OUTSIDE the semaphore on purpose. Waiting on a
        drive while holding a slot would let every slot fill with jobs queued
        behind a drive whose owner can no longer get a slot to finish."""
        key = self._drive_key(wj)
        if key is None:
            async with self._prep_sem:              # file/ISO: no drive to contend for
                yield
            return
        lock = self._drive_locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            wj.log.append(stamp_log(
                f"Waiting for {key} — another job is using that drive."))
            self.bus.publish()
        async with lock:
            async with self._prep_sem:
                yield

    def _fire_due_maintenance(self) -> None:
        """Raise the drain once the maintenance deadline passes, then clear it.

        Cleared rather than left standing so the window fires ONCE. A schedule
        that stayed set would keep every worker held after the maintenance was
        over, and the symptom — boxes up, idle, refusing work — reads as a
        broken queue rather than a schedule nobody cancelled."""
        window = maintenance.load(self.store)
        if not window.scheduled or not window.due(datetime.now(timezone.utc)):
            return
        self.store.request_cluster_signal("drain", ALL_WORKERS)
        maintenance.save(self.store, maintenance.Window(lead_hours=window.lead_hours))
        self._notify(
            "🛠 Maintenance window reached",
            "Every worker has been told to drain: each finishes the job it holds, "
            "then exits. Any still busy are listed in the workers panel.")

    def maintenance_window(self) -> dict:
        """The schedule plus what it means right now, for the UI."""
        window = maintenance.load(self.store)
        now = datetime.now(timezone.utc)
        busy = [{"worker": w.worker_id, "job_id": w.job_id, "stage": w.stage}
                for w in self._workers_cache
                if w.job_id is not None and not presence.is_serve(w.worker_id)]
        out = maintenance.to_dict(window)
        out.update({
            "quiet_from": window.quiet_from().isoformat() if window.scheduled else None,
            "holding": window.holds_new_work(now),
            "seconds_until_quiet": (
                (window.quiet_from() - now).total_seconds() if window.scheduled else None),
            "seconds_until_drain": (
                (window.drain_at - now).total_seconds() if window.scheduled else None),
            "busy": busy,
        })
        return out

    def set_maintenance(self, drain_at: str | None, lead_hours: float | None,
                        note: str = "") -> dict:
        """Schedule (or clear, with drain_at None) the window."""
        window = maintenance.from_dict({
            "drain_at": drain_at,
            "lead_hours": (maintenance.DEFAULT_LEAD_HOURS if lead_hours is None
                           else lead_hours),
            "note": note,
        })
        if drain_at and window.drain_at is None:
            raise ValueError(
                f"Could not read {drain_at!r} as a date and time. Use an ISO "
                f"timestamp, e.g. 2026-08-30T02:00:00Z.")
        # A deadline in the past is due the moment it is saved, so it would drain
        # the whole cluster on the next poll. That is never what someone booking
        # maintenance meant — it is a mistyped year — and "Drain all" is right
        # there for when you do want it now.
        if window.drain_at is not None:
            now = datetime.now(timezone.utc)
            if window.drain_at <= now:
                raise ValueError(
                    f"{window.drain_at:%Y-%m-%d %H:%M} UTC has already passed, so "
                    f"that would drain every worker straight away. Pick a time in "
                    f"the future, or use Drain all if you meant now.")
        maintenance.save(self.store, window)
        self.bus.publish()
        return self.maintenance_window()

    def _merge_candidates(self, wj: WebJob) -> list[WebJob]:
        """Other jobs for the SAME disc that hold no finished work.

        Jumanji sat in the queue three times — 345, 348 and 349 — because a
        failed rip gets re-added rather than retried, and each re-add is a fresh
        job. They are one piece of work and should be one job.

        Identity is the disc fingerprint, which keeps the resolution rule honest
        for free: a 1080p Blu-ray and a 4K UHD of one film are different discs
        and fingerprint differently, so they are never merge candidates. They are
        two VERSIONS, and _apply_version_suffix files them under Plex's
        multi-version names instead of one overwriting the other.

        Jobs holding placed output are excluded: merging must never be a way to
        delete finished files."""
        fp = wj.fingerprint
        if not fp:
            return []
        return sorted((o for o in self.jobs.values()
                       if o.id != wj.id and o.fingerprint == fp and not o.outputs),
                      key=lambda o: o.id)

    async def merge_duplicates(self, job_id: int) -> dict:
        """Fold this job's duplicates into it and drop them."""
        wj = self.jobs.get(job_id)
        if wj is None:
            raise KeyError(job_id)
        dupes = self._merge_candidates(wj)
        if not dupes:
            return {"kept": job_id, "merged": []}
        # Carry naming over if this copy never got it. Without this, merging can
        # throw away the one thing that was actually done — the mapping you typed
        # on whichever attempt got that far.
        if not wj.assignments:
            for o in dupes:
                if o.assignments:
                    wj.assignments = dict(o.assignments)
                    wj.selected_title_ids = list(
                        o.selected_title_ids or wj.selected_title_ids)
                    wj.log.append(stamp_log(
                        f"Took the naming from job #{o.id}."))
                    break
        merged = [o.id for o in dupes]
        for o in dupes:
            await self.remove(o.id)
        wj.log.append(stamp_log(
            "Merged " + ", ".join(f"#{i}" for i in merged) +
            " into this job — same disc, so they were all one piece of work."))
        self._persist(wj)
        self.bus.publish()
        return {"kept": job_id, "merged": merged}

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
        """Fire-and-forget push (if a notify_url is configured).

        An explicit priority (a per-alert escalation) wins; otherwise the
        configured default applies, which is what lets a phone actually ring for
        these instead of collecting them silently."""
        if self.settings.notify_url:
            self._spawn(notify(self.settings.notify_url, title, message,
                               priority or self.settings.notify_priority))

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

    def _disc_hint(self, wj: WebJob) -> tuple[int | None, int | None]:
        """(season, disc) as the disc's own label prints them, e.g. HOUSE_S5_D2.

        Together they are the disc's address on the shelf. The episode range says
        what a job HOLDS; it does not say which disc to put back in the drive, and
        re-ripping a bad rip of "House S05E06–E10" otherwise means opening an
        episode guide to work out that those five are on disc 2.

        The season is taken from the LABEL first because here it is part of that
        address rather than a description of the contents — "S05 D02" is the case
        you reach for. Where the label doesn't say, the season you gave when
        naming the job stands in."""
        hints = self._label_hints(wj)
        return hints.get("label_season"), hints.get("label_disc")

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
                label_season, disc = self._disc_hint(wj)
                season = label_season if label_season is not None else wj.season
                if disc is not None and season is not None:
                    return f"{show} S{season:02d} D{disc:02d}"
                if disc is not None:
                    return f"{show} D{disc:02d}"
                return f"{show} S{season:02d}" if season is not None else show
            return fallback
        first = assigns[0]
        title = (first.show_or_movie_title or "").strip()
        if not title:
            return fallback
        if first.kind is not MediaKind.TV:
            return f"{title} ({first.year})" if first.year else title
        label_season, disc = self._disc_hint(wj)
        seasons = sorted({a.season for a in assigns if a.season is not None})
        episodes = sorted({a.episode for a in assigns if a.episode is not None})
        if len(seasons) > 1:
            # A disc that spans a season boundary (S01E22 + S02E01 + S02E02) must
            # not have its two spans read independently: "S01–S02E01–E22" says
            # episodes 1–22 of BOTH seasons, when what is on the disc is the end of
            # one season and the start of the next — the opposite of the shelf hint
            # this name exists to give. Qualify each end of the span with its season.
            pairs = sorted((a.season, a.episode) for a in assigns
                           if a.season is not None and a.episode is not None)
            if pairs:
                (s0, e0), (s1, e1) = pairs[0], pairs[-1]
                # Both ends stay season-qualified; there is no one season to put
                # in the address, so the disc number leads on its own.
                lead = f"{title} D{disc:02d}" if disc is not None else title
                return f"{lead} S{s0:02d}E{e0:02d}–S{s1:02d}E{e1:02d}"
        if len(seasons) == 1:
            season = label_season if label_season is not None else seasons[0]
            season_part = f"S{season:02d}"
        elif seasons:
            season_part = f"S{seasons[0]:02d}–S{seasons[-1]:02d}"
        else:
            season_part = ""
        if len(episodes) == 1:
            episode_part = f"E{episodes[0]:02d}"
        elif episodes:
            episode_part = f"E{episodes[0]:02d}–E{episodes[-1]:02d}"
        else:
            episode_part = ""
        if disc is not None:
            # "House S05 D02 E06–E10": season and disc are the shelf address,
            # spaced apart from the episodes so the two read as separate facts.
            lead = " ".join(p for p in (season_part, f"D{disc:02d}") if p)
            return " ".join(p for p in (title, lead, episode_part) if p)
        joined = f"{season_part}{episode_part}"
        return f"{title} {joined}" if joined else title

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
        silently rip as the wrong episodes.

        Reads the volume label first, then the disc name — the same key the
        box-set plan matches on. Reading only disc_name meant that on a disc where
        MakeMKV reports a friendly name ("Hawaii Five-O") alongside the ISO label
        ("HAWAII_FIVE_0_S1_D4"), the form came up blank on exactly the fallback
        path where retyping the season for every disc hurts most."""
        label = ((wj.disc.volume_label or wj.disc.disc_name) if wj.disc else None) or ""
        if not label:
            return {"label_season": None, "label_disc": None}
        _title, _year, season = naming.clean_tmdb_query(label, "tv")
        return {"label_season": season, "label_disc": naming.disc_number(label)}

    def _job_dict(self, wj: WebJob) -> dict:
        live_state = self._live_state(wj)
        stalled = self._is_stalled(wj, live_state)
        return {
            "id": wj.id,
            "source": wj.source,
            "state": live_state.value if hasattr(live_state, "value") else str(live_state),
            "stage": wj.stage,
            "progress": round(wj.progress, 4),
            # True when the state says "encoding/OCR/placing" but nothing is actually
            # working it (worker died/restarted). Lets the UI flag a frozen job.
            "stalled": stalled,
            # WHICH kind of stall — "the disc is still needed" vs "the rip is
            # saved, just requeue". The two look identical from the outside.
            **dict(zip(("stall_kind", "stall_detail"),
                       self._stall_info(wj, live_state, stalled))),
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
            # Offered as a download so the dump never has to be hunted for.
            "aacs_dump": Path(wj.aacs_dump).name if wj.aacs_dump else None,
            "outputs": wj.outputs,
            # Whether the disc is still needed (some title left to rip). False once
            # ripping is done, so an encode/subtitle failure doesn't offer "change
            # drive" (the disc isn't involved anymore).
            "needs_disc": self._rip_pending(wj),
            # What Retry would actually do, so the button can say it rather than
            # making you infer it: a job with no ripped file RIPS (needs the disc
            # in the drive), one that's already ripped resumes from checkpoints.
            "retry_rips": ((wj.source or "").startswith(("disc:", "dev:"))
                           and self._rip_pending(wj)),
            # Failed AND needs the disc back to get anywhere — the orphaned-READY
            # jobs recovery parks, plus rips that died. Own queue because the
            # decision is physical ("find the disc, or drop it"), not a retry.
            "needs_review": (wj.state is JobState.FAILED
                             and (wj.source or "").startswith(("disc:", "dev:"))
                             and self._rip_pending(wj)),
            # True when this job's disc is sitting in a drive right now.
            "disc_loaded": self._disc_is_loaded(wj),
            # >1 when this job's folder holds sibling episode files that could be
            # folded into one job and named once instead of N times.
            "group_with": len(self._group_candidates(wj)[0]),
            # >0 when other jobs are the same disc and can be folded into this one.
            "merge_with": len(self._merge_candidates(wj)),
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
        """Which titles to tick for the operator.

        For a movie this is every FEATURE-LENGTH title, not just the longest.
        A disc carrying a theatrical and an unrated cut offers two, and
        suggesting only the longest meant the other was never ripped: the job
        completed, the file looked right, and one of the two films on the disc
        simply did not exist."""
        if discinfo.guess_kind(disc) is MediaKind.MOVIE:
            return {t.title_id for t in discinfo.feature_titles(disc)}
        return {t.title_id for t in discinfo.episode_candidate_titles(disc)}
