"""Pipeline state machine: scan -> select -> rip -> confirm -> encode ->
extract subs -> OCR -> place in library -> done.

Human-gate points (title selection, metadata confirm) are delegated to a
``PipelineCallbacks`` implementation. The Phase-1 CLI supplies interactive
prompts; a Phase-2 web layer would supply parked states instead. If the disc
fingerprint is already known, the confirm gate is skipped automatically.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

from .config import Settings
from .models import DiscInfo, EpisodeAssignment, MediaKind
from .scheduler import EncodeScheduler, encode_weight
from .services import library, naming
from .services.mapping_store import MappingStore
from .tools import discinfo
from .tools.eject import eject
from .tools.handbrake import EncodeProgress, HandBrake
from .tools.makemkv import MakeMKV
from .tools.mkvtools import MkvTools, SubtitleTrack
from .tools.subtitleedit import SubtitleEdit


class JobState(str, Enum):
    QUEUED = "queued"
    SCANNING = "scanning"
    SELECTING = "selecting"
    # Web-driven human gates (parked until the UI submits a decision):
    AWAITING_TITLES = "awaiting_titles"
    AWAITING_MAPPING = "awaiting_mapping"
    READY = "ready"
    RIPPING = "ripping"
    CONFIRMING = "confirming"
    ENCODING = "encoding"
    EXTRACTING_SUBS = "extracting_subs"
    OCR_SUBS = "ocr_subs"
    PLACING = "placing"
    DONE = "done"
    FAILED = "failed"


@dataclass
class TitleResult:
    title_id: int
    video_path: Path | None = None
    subtitle_paths: list[Path] = field(default_factory=list)


@dataclass
class Job:
    source: str
    state: JobState = JobState.QUEUED
    disc: DiscInfo | None = None
    fingerprint: str | None = None
    selected_title_ids: list[int] = field(default_factory=list)
    assignments: dict[int, EpisodeAssignment] = field(default_factory=dict)
    results: list[TitleResult] = field(default_factory=list)
    error: str | None = None
    auto_mapped: bool = False


class PipelineCallbacks(Protocol):
    """Hooks the pipeline calls to interact with a human and report progress."""

    def select_titles(self, disc: DiscInfo) -> list[int]:
        """Return the title ids to rip."""

    async def confirm_mapping(
        self, disc: DiscInfo, selected_title_ids: list[int]
    ) -> dict[int, EpisodeAssignment]:
        """Return the title_id -> EpisodeAssignment mapping for naming."""

    def on_event(self, message: str) -> None:
        """Log/progress line."""

    def on_progress(self, stage: str, fraction: float | None) -> None:
        """Numeric progress for the current stage (0..1)."""


class Pipeline:
    def __init__(self, settings: Settings, callbacks: PipelineCallbacks | None = None) -> None:
        self.settings = settings
        self.cb = callbacks  # only needed for the interactive run()/prepare() path
        self.makemkv = MakeMKV(settings)
        self.handbrake = HandBrake(settings)
        self.mkvtools = MkvTools(settings)
        self.subtitleedit = SubtitleEdit(settings)
        self.mapping_store = MappingStore(settings.storage.output_dir / ".blurayripper" / "mappings.db")

    async def run(self, source: str, job_name: str | None = None) -> Job:
        """Single-job convenience: interactive prepare, then execute."""
        job = await self.prepare(source)
        scheduler = EncodeScheduler(self.settings.queue.encode_capacity)
        return await self.execute(job, scheduler, self.cb, job_name)

    async def prepare(self, source: str) -> Job:
        """Interactive phase: scan + resolve the title/naming mapping.

        Kept separate from execute() so a queue can collect all user input up
        front, then run the (non-interactive) rips/encodes concurrently.
        """
        job = await self.scan_disc(source, self.cb)
        if not job.auto_mapped:
            job.state = JobState.SELECTING
            job.selected_title_ids = self.cb.select_titles(job.disc)
            if not job.selected_title_ids:
                raise RuntimeError("No titles selected to rip.")
            job.state = JobState.CONFIRMING
            job.assignments = await self.cb.confirm_mapping(job.disc, job.selected_title_ids)
        return job

    async def scan_disc(self, source: str, report) -> Job:
        """Non-interactive scan: probe the disc, fingerprint it, and auto-fill
        the mapping if this disc was confirmed before. The web layer drives the
        title/naming gates explicitly instead of via callbacks."""
        job = Job(source=source)
        job.state = JobState.SCANNING
        report.on_event(f"Scanning {source} ...")
        job.disc = await self.makemkv.scan(source, on_message=report.on_event)
        job.fingerprint = discinfo.disc_fingerprint(job.disc)
        report.on_event(
            f"Found {len(job.disc.titles)} titles "
            f"(disc: {job.disc.disc_name or 'unknown'}, fp: {job.fingerprint[:10]})."
        )
        remembered = self.mapping_store.get(job.fingerprint or "")
        if remembered:
            job.assignments = {a.title_id: a for a in remembered}
            job.selected_title_ids = [a.title_id for a in remembered]
            job.auto_mapped = True
            report.on_event("Recognized disc from a previous rip — using saved mapping.")
        return job

    async def execute(
        self, job: Job, scheduler: EncodeScheduler, report, job_name: str | None = None
    ) -> Job:
        """Non-interactive phase: rip -> (gated) encode -> subs -> place.

        ``report`` receives on_event/on_progress; ``scheduler`` gates the encode
        stage by weight so heavy UHD encodes don't pile up on the CPU.
        """
        staging_root = self.settings.storage.staging_dir / (job_name or _slug(job.source))
        try:
            await self._process_titles(job, staging_root, scheduler, report)
            self._persist_mapping(job, report)
            library.cleanup(staging_root)
            job.state = JobState.DONE
            report.on_event("Done.")
        except Exception as exc:  # surface which stage failed
            job.state = JobState.FAILED
            job.error = f"{type(exc).__name__}: {exc}"
            report.on_event(f"FAILED during {job.state.value}: {job.error}")
            raise
        return job

    # --- stages ---------------------------------------------------------

    async def _process_titles(
        self, job: Job, staging_root: Path, scheduler: EncodeScheduler, report
    ) -> None:
        assert job.disc is not None
        for title_id in job.selected_title_ids:
            assignment = job.assignments.get(title_id)
            if assignment is None:
                report.on_event(f"Skipping title {title_id}: no naming assignment.")
                continue
            result = await self._process_one(
                job, title_id, assignment, staging_root, scheduler, report
            )
            job.results.append(result)

    async def _process_one(
        self, job: Job, title_id: int, assignment: EpisodeAssignment, staging_root: Path,
        scheduler: EncodeScheduler, report,
    ) -> TitleResult:
        result = TitleResult(title_id=title_id)
        rip_dir = staging_root / "rip" / f"t{title_id:03d}"
        encode_dir = staging_root / "encode"
        sub_dir = staging_root / "sub" / f"t{title_id:03d}"

        # Rip
        job.state = JobState.RIPPING
        report.on_event(f"Ripping title {title_id} ...")
        title = job.disc.title(title_id) if job.disc else None
        speed = _RipSpeed(total_bytes=title.size_bytes if title else 0)
        ripped = await self.makemkv.rip_title(
            job.source, title_id, rip_dir,
            on_progress=lambda label, frac: report.on_progress(
                f"Rip t{title_id}: {label}{speed.update(frac)}", frac
            ),
            on_message=report.on_event,
        )
        if not ripped:
            raise RuntimeError(f"MakeMKV produced no output for title {title_id}.")
        ripped_mkv = ripped[0]

        # Eject after the last title's rip so the disc is free while the long
        # encode runs (the encode/OCR stages don't need the disc).
        if (
            self.settings.auto_eject
            and title_id == job.selected_title_ids[-1]
            and job.source.split(":", 1)[0] in ("disc", "dev")
        ):
            device = job.disc.drive_device if job.disc else None
            try:
                eject(device)
                report.on_event(
                    f"Ejected disc ({device or 'default drive'}) — safe to load the next one."
                )
            except Exception as exc:
                report.on_event(f"Auto-eject failed ({device or 'no device'}): {exc}")

        # Compute the final destination + extract subtitles (before encode drops them).
        video_dest = self._video_path_for(assignment)
        job.state = JobState.EXTRACTING_SUBS
        sub_jobs = await self._extract_subs(ripped_mkv, sub_dir, report)

        # Encode video, gated by the weighted scheduler so heavy UHD encodes
        # don't run two-at-a-time (they'd just split the CPU).
        height = await self._probe_height(ripped_mkv, report)
        weight = encode_weight(height)
        acquired = await scheduler.acquire(weight)
        try:
            job.state = JobState.ENCODING
            report.on_event(
                f"Encoding title {title_id} ({height or '?'}p, weight {weight}) "
                f"-> {video_dest.name} ..."
            )
            encoded = encode_dir / video_dest.name
            await self.handbrake.encode(
                ripped_mkv, encoded,
                on_progress=lambda p: self._encode_progress(p, report),
            )
        finally:
            await scheduler.release(acquired)
        result.video_path = library.place(encoded, video_dest)
        report.on_event(f"Placed video: {result.video_path}")

        # OCR each extracted subtitle and place the SRT sidecar. Same-language
        # tracks are disambiguated by forced/SDH flags and an index so they
        # never collide on one filename.
        job.state = JobState.OCR_SUBS
        default_lang = self.settings.languages.default.lower()
        plain_counts: dict[str, int] = {}
        for sup_path, track in sub_jobs:
            language = track.language or "und"
            try:
                srt = await self.subtitleedit.ocr_to_srt(sup_path, language, sub_dir)
            except Exception as exc:
                report.on_event(f"Subtitle OCR failed ({language}): {exc}")
                continue
            is_default = False
            index: int | None = None
            if not track.forced and not track.sdh:
                n = plain_counts.get(language, 0)
                plain_counts[language] = n + 1
                if n == 0:
                    is_default = language.lower() == default_lang
                else:
                    index = n + 1  # second plain track -> .2, third -> .3, ...
            srt_dest = naming.subtitle_sidecar_path(
                video_dest, language,
                default=is_default, forced=track.forced, sdh=track.sdh, index=index,
            )
            library.place(srt, srt_dest)
            result.subtitle_paths.append(srt_dest)
            report.on_event(f"Placed subtitle: {srt_dest.name}")

        job.state = JobState.PLACING
        return result

    async def _probe_height(self, ripped_mkv: Path, report) -> int | None:
        try:
            return await self.mkvtools.video_height(ripped_mkv)
        except Exception as exc:
            report.on_event(f"Could not probe resolution ({exc}); assuming non-UHD weight.")
            return None

    async def _extract_subs(
        self, ripped_mkv: Path, sub_dir: Path, report
    ) -> list[tuple[Path, SubtitleTrack]]:
        """Extract kept-language PGS tracks to .sup files. Returns (sup, track).

        The .sup filename includes the track id so multiple same-language tracks
        get distinct files instead of overwriting each other.
        """
        jobs: list[tuple[Path, SubtitleTrack]] = []
        try:
            tracks = await self.mkvtools.identify_subtitles(ripped_mkv)
        except Exception as exc:
            report.on_event(f"Subtitle identify failed: {exc}")
            return jobs
        for track in tracks:
            if not track.is_pgs:
                continue
            if not self.settings.languages.is_kept(track.language):
                continue
            lang = track.language or "und"
            sup_path = sub_dir / f"{ripped_mkv.stem}.t{track.track_id}.{lang}.sup"
            try:
                await self.mkvtools.extract_track(ripped_mkv, track.track_id, sup_path)
                jobs.append((sup_path, track))
                flags = "".join(f" [{f}]" for f, on in
                                (("forced", track.forced), ("sdh", track.sdh)) if on)
                report.on_event(f"Extracted {lang} PGS subtitle (track {track.track_id}){flags}.")
            except Exception as exc:
                report.on_event(f"Subtitle extract failed (track {track.track_id}): {exc}")
        return jobs

    def _encode_progress(self, p: EncodeProgress, report) -> None:
        parts = [p.state.lower()]
        if p.fps:
            parts.append(f"{p.fps:.0f} fps")
        if p.eta_seconds:
            parts.append(f"ETA {_fmt_eta(p.eta_seconds)}")
        report.on_progress(f"Encode: {' · '.join(parts)}", p.fraction)

    def _persist_mapping(self, job: Job, report) -> None:
        if job.auto_mapped or not job.assignments or not job.fingerprint:
            return
        assert job.disc is not None
        self.mapping_store.save(
            job.fingerprint,
            job.disc.disc_name,
            list(job.assignments.values()),
        )
        report.on_event("Saved disc mapping for automatic future re-rips.")

    # --- naming ---------------------------------------------------------

    def _video_path_for(self, a: EpisodeAssignment) -> Path:
        container = self.settings.encode.container
        storage = self.settings.storage
        if a.kind is MediaKind.MOVIE:
            return naming.movie_video_path(storage.movies_root, a.show_or_movie_title, a.year, container)
        if a.season is None or a.episode is None:
            raise ValueError(f"TV assignment for title {a.title_id} missing season/episode.")
        return naming.episode_video_path(
            storage.tv_root, a.show_or_movie_title, a.year, a.season, a.episode, container
        )


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in text)[:40] or "job"


# Blu-ray (including UHD BD) 1x read speed = 36 Mbit/s = 4.5 MB/s.
_BD_1X_MBPS = 4.5


class _RipSpeed:
    """Estimates rip throughput (MB/s and BD x-rate) from progress + title size.

    MakeMKV's robot output has no throughput field, so we infer it from how fast
    the overall progress fraction advances against the title's byte size, with
    light EMA smoothing and a ~1s recompute interval.
    """

    def __init__(self, total_bytes: int) -> None:
        self._total = total_bytes
        self._last_t = time.monotonic()
        self._last_bytes = 0.0
        self._mbps = 0.0

    def update(self, frac: float | None) -> str:
        if not self._total or frac is None:
            return ""
        now = time.monotonic()
        bytes_done = frac * self._total
        dt = now - self._last_t
        if dt >= 1.0:
            dbytes = bytes_done - self._last_bytes
            if dbytes > 0:
                inst = (dbytes / dt) / 1e6
                self._mbps = inst if self._mbps == 0 else 0.6 * self._mbps + 0.4 * inst
            self._last_t = now
            self._last_bytes = bytes_done
        if self._mbps <= 0:
            return ""
        return f" · {self._mbps:.1f} MB/s ({self._mbps / _BD_1X_MBPS:.1f}x)"


def _fmt_eta(seconds: int) -> str:
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    if s >= 60:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s}s"
