"""Pipeline state machine: scan -> select -> rip -> confirm -> encode ->
extract subs -> OCR -> place in library -> done.

Human-gate points (title selection, metadata confirm) are delegated to a
``PipelineCallbacks`` implementation. The Phase-1 CLI supplies interactive
prompts; a Phase-2 web layer would supply parked states instead. If the disc
fingerprint is already known, the confirm gate is skipped automatically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

from .config import Settings
from .models import DiscInfo, EpisodeAssignment, MediaKind
from .services import library, naming
from .services.mapping_store import MappingStore
from .tools import discinfo
from .tools.handbrake import EncodeProgress, HandBrake
from .tools.makemkv import MakeMKV
from .tools.mkvtools import MkvTools
from .tools.subtitleedit import SubtitleEdit


class JobState(str, Enum):
    QUEUED = "queued"
    SCANNING = "scanning"
    SELECTING = "selecting"
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
    def __init__(self, settings: Settings, callbacks: PipelineCallbacks) -> None:
        self.settings = settings
        self.cb = callbacks
        self.makemkv = MakeMKV(settings)
        self.handbrake = HandBrake(settings)
        self.mkvtools = MkvTools(settings)
        self.subtitleedit = SubtitleEdit(settings)
        self.mapping_store = MappingStore(settings.storage.output_dir / ".blurayripper" / "mappings.db")

    async def run(self, source: str, job_name: str | None = None) -> Job:
        job = Job(source=source)
        staging_root = self.settings.storage.staging_dir / (job_name or _slug(source))
        try:
            await self._scan(job)
            self._resolve_mapping(job)
            await self._process_titles(job, staging_root)
            self._persist_mapping(job)
            library.cleanup(staging_root)
            job.state = JobState.DONE
            self.cb.on_event("Done.")
        except Exception as exc:  # surface which stage failed
            job.state = JobState.FAILED
            job.error = f"{type(exc).__name__}: {exc}"
            self.cb.on_event(f"FAILED during {job.state.value}: {job.error}")
            raise
        return job

    # --- stages ---------------------------------------------------------

    async def _scan(self, job: Job) -> None:
        job.state = JobState.SCANNING
        self.cb.on_event(f"Scanning {job.source} ...")
        job.disc = await self.makemkv.scan(job.source)
        job.fingerprint = discinfo.disc_fingerprint(job.disc)
        self.cb.on_event(
            f"Found {len(job.disc.titles)} titles "
            f"(disc: {job.disc.disc_name or 'unknown'}, fp: {job.fingerprint[:10]})."
        )

    def _resolve_mapping(self, job: Job) -> None:
        assert job.disc is not None
        remembered = self.mapping_store.get(job.fingerprint or "")
        if remembered:
            job.assignments = {a.title_id: a for a in remembered}
            job.selected_title_ids = [a.title_id for a in remembered]
            job.auto_mapped = True
            job.state = JobState.CONFIRMING
            self.cb.on_event("Recognized disc from a previous rip — using saved mapping.")
            return

        job.state = JobState.SELECTING
        job.selected_title_ids = self.cb.select_titles(job.disc)
        if not job.selected_title_ids:
            raise RuntimeError("No titles selected to rip.")

    async def _ensure_mapping(self, job: Job) -> None:
        if job.assignments:
            return
        assert job.disc is not None
        job.state = JobState.CONFIRMING
        job.assignments = await self.cb.confirm_mapping(job.disc, job.selected_title_ids)

    async def _process_titles(self, job: Job, staging_root: Path) -> None:
        assert job.disc is not None
        await self._ensure_mapping(job)
        for title_id in job.selected_title_ids:
            assignment = job.assignments.get(title_id)
            if assignment is None:
                self.cb.on_event(f"Skipping title {title_id}: no naming assignment.")
                continue
            result = await self._process_one(job, title_id, assignment, staging_root)
            job.results.append(result)

    async def _process_one(
        self, job: Job, title_id: int, assignment: EpisodeAssignment, staging_root: Path
    ) -> TitleResult:
        result = TitleResult(title_id=title_id)
        rip_dir = staging_root / "rip" / f"t{title_id:03d}"
        encode_dir = staging_root / "encode"
        sub_dir = staging_root / "sub" / f"t{title_id:03d}"

        # Rip
        job.state = JobState.RIPPING
        self.cb.on_event(f"Ripping title {title_id} ...")
        ripped = await self.makemkv.rip_title(
            job.source, title_id, rip_dir,
            on_progress=lambda label, frac: self.cb.on_progress(f"rip:{label}", frac),
            on_message=self.cb.on_event,
        )
        if not ripped:
            raise RuntimeError(f"MakeMKV produced no output for title {title_id}.")
        ripped_mkv = ripped[0]

        # Compute the final destination + extract subtitles (before encode drops them).
        video_dest = self._video_path_for(assignment)
        job.state = JobState.EXTRACTING_SUBS
        sub_jobs = await self._extract_subs(ripped_mkv, sub_dir)

        # Encode video to the staging encode dir, then place it.
        job.state = JobState.ENCODING
        self.cb.on_event(f"Encoding title {title_id} -> {video_dest.name} ...")
        encoded = encode_dir / video_dest.name
        await self.handbrake.encode(
            ripped_mkv, encoded,
            on_progress=lambda p: self._encode_progress(p),
        )
        result.video_path = library.place(encoded, video_dest)
        self.cb.on_event(f"Placed video: {result.video_path}")

        # OCR each extracted subtitle and place the SRT sidecar.
        job.state = JobState.OCR_SUBS
        for sup_path, language in sub_jobs:
            try:
                srt = await self.subtitleedit.ocr_to_srt(sup_path, language, sub_dir)
            except Exception as exc:
                self.cb.on_event(f"Subtitle OCR failed ({language}): {exc}")
                continue
            is_default = language.lower() == self.settings.languages.default.lower()
            srt_dest = naming.subtitle_sidecar_path(video_dest, language, default=is_default)
            library.place(srt, srt_dest)
            result.subtitle_paths.append(srt_dest)
            self.cb.on_event(f"Placed subtitle: {srt_dest.name}")

        job.state = JobState.PLACING
        return result

    async def _extract_subs(self, ripped_mkv: Path, sub_dir: Path) -> list[tuple[Path, str]]:
        """Extract kept-language PGS tracks to .sup files. Returns (sup, lang) list."""
        jobs: list[tuple[Path, str]] = []
        try:
            tracks = await self.mkvtools.identify_subtitles(ripped_mkv)
        except Exception as exc:
            self.cb.on_event(f"Subtitle identify failed: {exc}")
            return jobs
        for track in tracks:
            if not track.is_pgs:
                continue
            if not self.settings.languages.is_kept(track.language):
                continue
            lang = track.language or "und"
            sup_path = sub_dir / f"{ripped_mkv.stem}.{lang}.sup"
            try:
                await self.mkvtools.extract_track(ripped_mkv, track.track_id, sup_path)
                jobs.append((sup_path, lang))
                self.cb.on_event(f"Extracted {lang} PGS subtitle (track {track.track_id}).")
            except Exception as exc:
                self.cb.on_event(f"Subtitle extract failed (track {track.track_id}): {exc}")
        return jobs

    def _encode_progress(self, p: EncodeProgress) -> None:
        label = p.state.lower()
        if p.eta_seconds:
            label = f"{label} (ETA {p.eta_seconds}s)"
        self.cb.on_progress(f"encode:{label}", p.fraction)

    def _persist_mapping(self, job: Job) -> None:
        if job.auto_mapped or not job.assignments or not job.fingerprint:
            return
        assert job.disc is not None
        self.mapping_store.save(
            job.fingerprint,
            job.disc.disc_name,
            list(job.assignments.values()),
        )
        self.cb.on_event("Saved disc mapping for automatic future re-rips.")

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
