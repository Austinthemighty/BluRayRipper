"""Pipeline state machine: scan -> select -> rip -> confirm -> encode ->
extract subs -> OCR -> place in library -> done.

Human-gate points (title selection, metadata confirm) are delegated to a
``PipelineCallbacks`` implementation. The Phase-1 CLI supplies interactive
prompts; a Phase-2 web layer would supply parked states instead. If the disc
fingerprint is already known, the confirm gate is skipped automatically.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Iterator, Protocol

from .config import Settings, ToolNotFoundError
from .models import (
    SOURCE_SELECTORS, DiscInfo, EpisodeAssignment, MediaKind, StreamType,
)
from .scheduler import EncodeScheduler, encode_weight
from .services import coverart, library, naming, nfo, os_upload, pathmap
from .services.mapping_store import MappingStore
from .services.metadata import TMDBClient
from .services.subtitles import looks_like_sdh
from .version import get_version
from .tools import discinfo
from .tools.eject import eject
from .tools.process import in_detached_thread
from .tools.dovitool import DoviTool
from .tools.ffmpeg import FFmpeg
from .tools.handbrake import EncodeProgress, HandBrake
from .tools.makemkv import MakeMKV, os_disc_capacity_bytes
from .tools.mkvtools import MkvTools, SubtitleTrack
from .tools.subtitleedit import SubtitleEdit

# Headroom above a disc title's runtime before a rip is treated as holding the
# wrong content. Never tighter than this, however tight the truncation tolerance
# is set: a good rip overshoots by a second or two, a wrong one by multiples.
_RIP_OVERRUN_PCT = 25

# How far a title's runtime may differ between the disc and a copy of it before
# the copy is treated as a different disc. Seconds, not percent: this compares
# the same title read twice, so any real difference is a different title.
_BACKUP_DURATION_SLACK = 2

# How long the post-rip auto-eject may take before the job gives up on the tray
# and moves on. Generous: a healthy drive takes a second or two, and a disc that
# is still being flushed can take longer.
_EJECT_TIMEOUT = 60.0


class JobState(str, Enum):
    QUEUED = "queued"
    SCANNING = "scanning"
    SELECTING = "selecting"
    # Web-driven human gates (parked until the UI submits a decision):
    AWAITING_TITLES = "awaiting_titles"
    AWAITING_MAPPING = "awaiting_mapping"
    AWAITING_GRAIN = "awaiting_grain"
    AWAITING_MATCH = "awaiting_match"  # subtitle ID was ambiguous; needs confirm
    IDENTIFYING = "identifying"
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
    # True when an EARLIER run completed this title and this run skipped it. Its
    # subtitles were checked then; demanding fresh evidence from a run that
    # deliberately did no subtitle work fails a job that is already correct.
    from_checkpoint: bool = False


@dataclass
class GrainPreview:
    """Grain-tune A/B preview assets: playable clips (shown in motion) plus
    optional still frames used as <video> posters / fallbacks. The posters need
    ffmpeg; the clips (the real preview) come from HandBrake, so posters may be
    None when ffmpeg isn't installed."""
    off_clip: Path
    on_clip: Path
    off_png: Path | None = None
    on_png: Path | None = None


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
    # Grain handling: mode is "auto" (use config default / tune_grain), "on",
    # "off", or "preview" (generate a still comparison and ask via the reporter).
    grain_mode: str = "auto"
    tune_grain: bool | None = None
    # Per-title resume checkpoints: {title_id: {"ripped_path": str, "placed": bool}}
    checkpoints: dict[int, dict] = field(default_factory=dict)


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
        self.ffmpeg = FFmpeg(settings)
        self.dovitool = DoviTool(settings)
        self.tmdb = TMDBClient(settings.tmdb_api_key, settings.metadata_language)
        configure_path_map(settings.storage.path_map)
        self.mapping_store = MappingStore(settings.storage.output_dir / ".blurayripper" / "mappings.db")
        # Throttle concurrent subtitle extraction so a big batch (or a recovery
        # resume of many jobs) doesn't fire dozens of multi-GB NAS reads at once.
        self._extract_sem = asyncio.Semaphore(max(1, settings.subtitles.extract_concurrency))

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
            problems = self._verify_outputs(job)
            if problems:
                raise RuntimeError(
                    "Encoding finished but the output is incomplete:\n  - "
                    + "\n  - ".join(problems)
                    + "\nThe rip and encode are kept, so retrying resumes from "
                      "them rather than starting over.")
            report.on_event(f"Verified {self._output_count(job)} output file(s) "
                            "in place.")
            self._persist_mapping(job, report)
            library.cleanup(staging_root)
            job.state = JobState.DONE
            report.on_event("Done.")
        except Exception as exc:  # surface which stage failed
            phase = job.state.value  # capture the real phase BEFORE marking FAILED
            job.state = JobState.FAILED
            job.error = f"{type(exc).__name__}: {exc}"
            hint = _failure_hint(exc)
            report.on_event(
                f"FAILED during {phase}: {job.error}" + (f"  — {hint}" if hint else ""))
            raise
        return job

    # --- stages ---------------------------------------------------------

    def _output_count(self, job: Job) -> int:
        return sum(1 + len(r.subtitle_paths) for r in job.results if r.video_path)

    def _verify_outputs(self, job: Job) -> list[str]:
        """Everything that should be on disk but isn't. Empty means really done.

        A job that half-failed still finished green: job 42's subtitle OCR died
        while the encode carried on, so the video was placed with no sidecars and
        nothing said so. Every stage reports its own success, and none of them
        looks at the finished folder — checking the disk once at the end is what
        separates "finished" from "finished except for the parts that failed
        quietly".
        """
        problems: list[str] = []
        if not job.results:
            return ["nothing was produced — no titles were processed"]
        keep = {l.lower() for l in self.settings.languages.keep}
        for result in job.results:
            video = result.video_path
            if video is None:
                problems.append(f"title {result.title_id}: no video was placed")
            elif not video.exists():
                problems.append(f"missing video: {video}")
            elif video.stat().st_size == 0:
                problems.append(f"video is 0 bytes: {video}")
            for srt in result.subtitle_paths:
                if not srt.exists():
                    problems.append(f"missing subtitle: {srt.name}")
                elif srt.stat().st_size == 0:
                    problems.append(f"subtitle is 0 bytes: {srt.name}")
            # Expected but absent. Placed-file checks alone cannot catch this:
            # when OCR fails there is no path to check, so the evidence is the
            # DISC having tracks we keep while nothing came out.
            if (not result.subtitle_paths and not result.from_checkpoint
                    and job.disc is not None):
                title = next((t for t in job.disc.titles
                              if t.title_id == result.title_id), None)
                langs = sorted({
                    (st.language or "und").lower()
                    for st in (title.streams if title else [])
                    if st.stream_type is StreamType.SUBTITLE
                    and (st.language or "und").lower() in keep})
                if langs:
                    problems.append(
                        f"title {result.title_id}: the disc carries "
                        f"{', '.join(langs)} subtitle track(s) but no .srt was "
                        "produced (subtitle OCR failed or was skipped)")
            if self.settings.write_nfo and video is not None and video.exists():
                if not video.with_suffix(".nfo").exists():
                    problems.append(f"missing NFO sidecar: {video.stem}.nfo")
        return problems

    async def _process_titles(
        self, job: Job, staging_root: Path, scheduler: EncodeScheduler, report
    ) -> None:
        assert job.disc is not None
        # A resume re-enters here with the previous run's results still attached;
        # appending to them double-counts every output and reports each problem
        # twice.
        job.results = []
        # Per-RUN, not per-Job: retry() reuses the same Job object, so a stale
        # flag from an earlier run made a resume "finish ripping" again — which
        # re-ejects the drive. The eject target is re-resolved against the drives
        # as they are NOW, so that could eject a different job's disc, mid-rip,
        # and always pushed a spurious "safe to swap the next one" notification.
        job._ripped_this_run = False
        await self._resolve_source_drive(job, report)  # follow the disc across drives
        # Into STAGING, not the library: artwork written straight to the movie
        # folder created it hours before the video arrived.
        await self._place_cover_art(job, report, staging_root)  # before any rip/eject
        await self._place_tmdb_art(job, report, staging_root)   # real art overrides disc art
        await self._prepare_disc_once(job, staging_root, report)
        for title_id in job.selected_title_ids:
            assignment = job.assignments.get(title_id)
            if assignment is None:
                report.on_event(f"Skipping title {title_id}: no naming assignment.")
                continue
            result = await self._process_one(
                job, title_id, assignment, staging_root, scheduler, report
            )
            job.results.append(result)

    async def prep_phase(
        self, job: Job, staging_root: Path, report, include_subs: bool = True
    ) -> None:
        """Rip + (optionally) subtitles + grain decision. Runs on the rip machine
        so the grain preview can use the UI.

        ``include_subs=False`` defers extraction/OCR to the worker (so restarting
        the rip box's serve doesn't kill that long NAS-bound work); the rip box
        then only rips and resolves grain."""
        assert job.disc is not None
        job._ripped_this_run = False   # per-run; see _process_titles
        # 0) Follow the disc across drives, then grab the disc's cover art while
        #    it's still in the drive, then override it with the real TMDB art.
        await self._resolve_source_drive(job, report)
        await self._place_cover_art(job, report, staging_root)
        await self._place_tmdb_art(job, report, staging_root)
        # Read the disc once if that is worth doing. The loop below is
        # unchanged either way — it just reads from the copy. After the
        # artwork, which wants the disc, because this ejects it.
        await self._prepare_disc_once(job, staging_root, report)
        # 1) Rip per title (sequential — one drive).
        todo: list[tuple[int, EpisodeAssignment, Path, Path]] = []
        for title_id, assignment in _assigned_titles(job):
            cp = job.checkpoints.setdefault(title_id, {})
            if self._title_done(cp, assignment) is not None:
                continue
            ripped = await self._ensure_ripped(job, title_id, staging_root, report)
            sub_dir = staging_root / "sub" / f"t{title_id:03d}"
            todo.append((title_id, assignment, ripped, sub_dir))

        self._release_disc_copy(job, staging_root, report)

        # 2) Subtitles (extract + OCR + place sidecars) — unless a worker will.
        if include_subs:
            await self._subtitles_for_titles(job, todo, report, staging_root)

        # 3) Resolve grain — ONCE per disc, not per title. Preview mode renders a
        #    sample and parks the UI for a pick; a disc's titles share the same
        #    source/encode, so asking per episode just re-opens the grain gate over
        #    and over (which reads as an endless "pick grain" loop). Decide once for
        #    the first undecided title and apply it to every title that doesn't
        #    already have a saved choice — so a resume never re-prompts either.
        undecided = [t for t in todo
                     if "tune_grain" not in job.checkpoints.setdefault(t[0], {})]
        if undecided:
            first_id, _a, first_ripped, _sd = undecided[0]
            decision = await self._resolve_grain(job, first_ripped, first_id, staging_root, report)
            for title_id, _assignment, _ripped, _sub_dir in undecided:
                job.checkpoints[title_id]["tune_grain"] = decision
            _checkpoint(report)

    async def _subtitles_for_titles(
        self, job: Job, todo: list[tuple[int, EpisodeAssignment, Path, Path]], report,
        staging_root: Path | None = None,
    ) -> None:
        """Extract + OCR (concurrent) + place sidecars for a set of ripped titles.
        Shared by prep_phase (rip box) and the worker's encode_phase."""
        pending = []
        for entry in todo:
            done = self._subs_already_done(
                job.checkpoints.setdefault(entry[0], {}), staging_root)
            if done is None:
                pending.append(entry)
            else:
                report.on_event(
                    f"Subtitles already done for title {entry[0]} "
                    f"({len(done)} sidecar(s)) — skipping extract + OCR.")
        if not pending:
            return
        extracted: dict[int, list] = {}
        for title_id, _assignment, ripped, sub_dir in pending:
            extracted[title_id] = await self._extract_title_subs(job, title_id, ripped, sub_dir, report)
        ocr = await self._ocr_all(
            job, [(tid, a, r, sd, extracted[tid]) for tid, a, r, sd in pending], report)
        for title_id, assignment, ripped, _sub_dir in pending:
            result = TitleResult(title_id=title_id)
            video_dest = self._video_path_for(assignment)
            await self._place_title_subs(
                result, video_dest, ocr.get(title_id, []), report,
                source_video=ripped, assignment=assignment,
                stage_to=(self._staged_of(staging_root, video_dest)
                          if staging_root is not None
                          and not job.checkpoints.get(title_id, {}).get("placed")
                          else None))
            self._cache_subs(video_dest, extracted[title_id])
            self._record_if_complete(job.checkpoints.setdefault(title_id, {}),
                                     extracted[title_id], ocr.get(title_id, []),
                                     result, report)
        _checkpoint(report)

    async def _extract_title_subs(
        self, job: Job, title_id: int, ripped_mkv: Path, sub_dir: Path, report
    ) -> list[tuple[Path, SubtitleTrack]]:
        """Extract (or reuse) a title's subtitle images. No OCR."""
        job.state = JobState.EXTRACTING_SUBS
        cp = job.checkpoints.setdefault(title_id, {})
        reused = self._reuse_subs(cp)
        if reused is not None:
            report.on_event(f"Reusing {len(reused)} previously-extracted subtitle(s).")
            return reused
        extracted = await self._extract_subs(ripped_mkv, sub_dir, report)
        if extracted is None:
            # Extraction genuinely FAILED (mkvextract/identify error) — distinct
            # from [] ("this title has no kept-language subs", which proceeds).
            # Raise so the job fails and a retry re-extracts, instead of silently
            # placing a subtitle-less video and marking the title done (cp["placed"]
            # would then make every later resume skip it — subs lost for good).
            raise RuntimeError(
                f"Subtitle extraction failed for title {title_id} (see log above); "
                "retry the job to re-extract.")
        cp["subs"] = [
            _sub_checkpoint(self.settings.storage.staging_dir, sup, tr) for sup, tr in extracted
        ]
        _checkpoint(report)
        return extracted

    async def _ocr_all(
        self, job: Job, todo: list, report
    ) -> dict[int, list[tuple[Path, SubtitleTrack]]]:
        """OCR every (title, track) in ``todo`` concurrently. Returns
        ``{title_id: [(srt_path, track), ...]}`` in original track order, with
        failed tracks dropped."""
        if any(sub_jobs for *_rest, sub_jobs in todo):
            job.state = JobState.OCR_SUBS
        sem = asyncio.Semaphore(max(1, self.settings.subtitles.ocr_concurrency))
        out: dict[int, list] = {tid: [None] * len(sj) for tid, _a, _r, _sd, sj in todo}

        async def _one(tid: int, idx: int, sup: Path, track: SubtitleTrack, sub_dir: Path):
            language = track.language or "und"
            async with sem:
                try:
                    srt = await self.subtitleedit.ocr_to_srt(sup, language, sub_dir)
                except Exception as exc:
                    report.on_event(
                        f"Subtitle OCR failed ({language}): {type(exc).__name__}: {exc}")
                    return
            out[tid][idx] = (srt, track)

        tasks = [
            _one(tid, idx, sup, track, sub_dir)
            for tid, _a, _r, sub_dir, sub_jobs in todo
            for idx, (sup, track) in enumerate(sub_jobs)
        ]
        if tasks:
            await asyncio.gather(*tasks)
        return {tid: [x for x in lst if x is not None] for tid, lst in out.items()}

    async def _place_title_subs(
        self, result: TitleResult, video_dest: Path,
        ocr_pairs: list[tuple[Path, SubtitleTrack]], report,
        *, source_video: Path | None = None, assignment: EpisodeAssignment | None = None,
        stage_to: Path | None = None,
    ) -> None:
        """Place OCR'd SRT sidecars next to the video, disambiguating same-language
        tracks by forced/SDH flags and an index so they never collide. When
        subtitles.upload_export_dir is set, each sidecar is also exported with an
        OpenSubtitles manual-upload sheet (FPS/HD probed once from source_video)."""
        export_dir = self.settings.subtitles.upload_export_dir
        height = fps = None
        if export_dir and source_video is not None and source_video.exists():
            try:
                height, fps = await self.mkvtools.video_dims(source_video)
            except Exception as exc:
                report.on_event(
                    f"Upload export: could not probe video ({type(exc).__name__}); "
                    "FPS/HD left blank.")
        default_lang = self.settings.languages.default.lower()
        plain_counts: dict[str, int] = {}
        for srt, track in ocr_pairs:
            language = track.language or "und"
            # Blu-ray PGS rarely carries the SDH flag, so fall back to detecting
            # it from the OCR'd content (sound cues, music notes, speaker labels).
            is_sdh = track.sdh
            if not is_sdh and not track.forced:
                try:
                    is_sdh = looks_like_sdh(srt.read_text(encoding="utf-8", errors="ignore"))
                except OSError:
                    pass
                if is_sdh:
                    report.on_event(f"Detected SDH subtitle ({language}) from content.")
            is_default = False
            index: int | None = None
            # Count within (language, flavour), not just for plain tracks. Two
            # tracks sharing a language AND a flavour otherwise land on the same
            # filename and library.place overwrites the first — one subtitle
            # silently lost, with nothing to detect it afterwards because the
            # surviving file exists and is non-empty. Blu-ray rarely flags SDH,
            # so a feature track and a commentary track both sniffed as SDH is
            # the realistic way in, not the flags themselves.
            flavour = "forced" if track.forced else "sdh" if is_sdh else "plain"
            n = plain_counts.get((language, flavour), 0)
            plain_counts[(language, flavour)] = n + 1
            if n == 0:
                is_default = (flavour == "plain"
                              and language.lower() == default_lang)
            else:
                index = n + 1  # second of this flavour -> .2, third -> .3, ...
            srt_dest = naming.subtitle_sidecar_path(
                video_dest, language,
                default=is_default, forced=track.forced, sdh=is_sdh, index=index,
            )
            # Written beside the STAGED video while the title is still being
            # assembled, but recorded under its final name either way: every
            # checkpoint, resume check and caller speaks in library paths, and
            # only the bytes wait for the video to be ready.
            written = (naming.subtitle_sidecar_path(
                stage_to, language,
                default=is_default, forced=track.forced, sdh=is_sdh, index=index,
            ) if stage_to is not None else srt_dest)
            library.place(srt, written)
            result.subtitle_paths.append(srt_dest)
            report.on_event(
                f"Subtitle ready: {srt_dest.name}" if stage_to is not None
                else f"Placed subtitle: {srt_dest.name}")
            if export_dir and assignment is not None:
                try:
                    os_upload.export_subtitle(
                        export_dir, written, assignment=assignment,
                        language_code=track.language, is_sdh=is_sdh, forced=track.forced,
                        height=height, fps=fps, version=get_version())
                    report.on_event(f"Exported for OpenSubtitles upload: {srt_dest.name}")
                except Exception as exc:
                    report.on_event(
                        f"Upload export failed ({srt_dest.name}): {type(exc).__name__}: {exc}")

    async def encode_phase(
        self, job: Job, staging_root: Path, scheduler: EncodeScheduler, report
    ) -> list[TitleResult]:
        """Encode + place the video for all selected titles. Runs on any worker;
        grain was decided by prep_phase on the rip machine. When
        cluster.worker_does_subs is set, the (deferred) subtitle extraction + OCR
        also run here, right before encoding."""
        if self.settings.cluster.worker_does_subs:
            await self._worker_subtitles(job, staging_root, report)
        results: list[TitleResult] = []
        for title_id in job.selected_title_ids:
            assignment = job.assignments.get(title_id)
            if assignment is None:
                report.on_event(f"Skipping title {title_id}: no naming assignment.")
                continue
            cp = job.checkpoints.setdefault(title_id, {})
            prior = cp.get("ripped_path")
            ripped = _resolve_path(self.settings.storage.staging_dir, prior) if prior else None
            # _placed_video, not cp["placed"]: a placed file that is missing or
            # empty has to be re-encoded, and that needs the rip.
            if (self._placed_video(cp, assignment) is None
                    and (ripped is None or not ripped.exists())):
                raise RuntimeError(f"Title {title_id} has no ripped file to encode.")
            results.append(await self._encode_title(
                job, title_id, assignment, staging_root, ripped, scheduler, report))
        return results

    async def _worker_subtitles(self, job: Job, staging_root: Path, report) -> None:
        """Deferred subtitle work (extract + OCR + place) done on the worker from
        the already-ripped files, for the worker_does_subs mode."""
        todo: list[tuple[int, EpisodeAssignment, Path, Path]] = []
        for title_id, assignment in _assigned_titles(job):
            cp = job.checkpoints.setdefault(title_id, {})
            if self._title_done(cp, assignment) is not None:
                continue
            prior = cp.get("ripped_path")
            ripped = _resolve_path(self.settings.storage.staging_dir, prior) if prior else None
            if ripped is None or not ripped.exists():
                raise RuntimeError(f"Title {title_id} has no ripped file for subtitles.")
            todo.append((title_id, assignment, ripped, staging_root / "sub" / f"t{title_id:03d}"))
        if todo:
            report.on_event("Extracting + OCR'ing subtitles (worker) …")
            await self._subtitles_for_titles(job, todo, report, staging_root)

    def _sub_cache_dir(self, video_dest: Path) -> Path:
        return (self.settings.storage.output_dir / ".blurayripper" / "subcache"
                / _path_key(video_dest))

    def _cache_subs(self, video_dest: Path, sub_jobs: list[tuple[Path, SubtitleTrack]]) -> None:
        """Persist extracted .sup subs next to a tiny manifest so a later re-OCR
        (``resubtitle``) needs no re-rip. Best-effort: never raises into a job."""
        if not self.settings.subtitles.keep_extracted or not sub_jobs:
            return
        try:
            cache_dir = self._sub_cache_dir(video_dest)
            cache_dir.mkdir(parents=True, exist_ok=True)
            manifest = []
            for sup, track in sub_jobs:
                dest = cache_dir / Path(sup).name
                if Path(sup).resolve() != dest.resolve():
                    shutil.copy2(sup, dest)
                manifest.append(_sub_checkpoint(cache_dir, dest, track))
            (cache_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        except OSError:
            pass

    def _cached_subs(self, video_dest: Path) -> list[tuple[Path, SubtitleTrack]] | None:
        cache_dir = self._sub_cache_dir(video_dest)
        manifest = cache_dir / "manifest.json"
        if not manifest.exists():
            return None
        try:
            saved = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        out: list[tuple[Path, SubtitleTrack]] = []
        for s in saved:
            sup = _resolve_path(cache_dir, s["sup"])
            if not sup.exists():
                return None
            out.append((sup, SubtitleTrack(
                track_id=s["track_id"], codec=s.get("codec", ""), language=s.get("language"),
                is_pgs=True, forced=s.get("forced", False), sdh=s.get("sdh", False),
                name=s.get("name"),
            )))
        return out or None

    async def resubtitle(self, video_dest: Path, report, source_mkv: Path | None = None) -> list[Path]:
        """Re-OCR subtitles for an already-placed library item, without encoding.

        Uses the cached .sup subs from the original rip when available (no re-rip);
        otherwise extracts from ``source_mkv`` (a MakeMKV rip — the placed video
        has no subtitle tracks). Writes .srt sidecars next to ``video_dest``."""
        sub_dir = self.settings.storage.staging_dir / "resub" / _path_key(video_dest)
        if source_mkv is not None:
            if not source_mkv.exists():
                raise FileNotFoundError(f"Source MKV not found: {source_mkv}")
            report.on_event(f"Extracting subtitles from {source_mkv.name} …")
            extracted = await self._extract_subs(source_mkv, sub_dir, report)
        else:
            extracted = self._cached_subs(video_dest)
            if extracted is None:
                raise FileNotFoundError(
                    f"No cached subtitles for {video_dest.name}. Pass a source MKV "
                    "(this item predates subtitle caching, or caching is off).")
            report.on_event(
                f"Reusing {len(extracted)} cached subtitle track(s) — no re-rip needed.")
        if not extracted:
            report.on_event("No subtitle tracks found.")
            return []
        job = Job(source=str(video_dest))  # lightweight holder for _ocr_all
        ocr = await self._ocr_all(job, [(0, None, video_dest, sub_dir, extracted)], report)
        result = TitleResult(title_id=0)
        # No naming assignment on the raw resubtitle path, so upload export (which
        # needs title/year) is skipped; still probes from source_mkv if present.
        await self._place_title_subs(
            result, video_dest, ocr.get(0, []), report, source_video=source_mkv)
        if source_mkv is not None:
            self._cache_subs(video_dest, extracted)  # cache so next time needs no rip
        report.on_event(f"Placed {len(result.subtitle_paths)} subtitle sidecar(s).")
        return result.subtitle_paths

    async def redo_subtitles(self, job: Job, staging_root: Path, report) -> list[Path]:
        """Re-OCR + place subtitles for a job's titles, no ripping or encoding.

        Source priority per title: already-extracted .sup, then the staged ripped
        MKV (re-extract — no re-rip), then the per-item cache (already-encoded).
        Titles with none are skipped (those need a source MKV via ``resubtitle``)."""
        placed: list[Path] = []
        job.state = JobState.OCR_SUBS
        for title_id, assignment in _assigned_titles(job):
            video_dest = self._video_path_for(assignment)
            cp = job.checkpoints.get(title_id, {})
            sub_dir = staging_root / "sub" / f"t{title_id:03d}"
            # Source priority: already-extracted .sup -> the staged ripped MKV
            # (re-extract, no re-rip) -> the per-item cache.
            sub_jobs = self._reuse_subs(cp)
            if sub_jobs is None:
                prior = cp.get("ripped_path")
                ripped = _resolve_path(self.settings.storage.staging_dir, prior) if prior else None
                if ripped is not None and ripped.exists():
                    report.on_event(
                        f"Title {title_id}: re-extracting subtitles from the staged rip …")
                    sub_jobs = await self._extract_subs(ripped, sub_dir, report)
            if not sub_jobs:
                sub_jobs = self._cached_subs(video_dest)
            if not sub_jobs:
                report.on_event(
                    f"Title {title_id}: no staged rip or cached subtitles — re-rip needed.")
                continue
            ocr = await self._ocr_all(job, [(title_id, assignment, None, sub_dir, sub_jobs)], report)
            result = TitleResult(title_id=title_id)
            prior = cp.get("ripped_path")
            source = _resolve_path(self.settings.storage.staging_dir, prior) if prior else None
            await self._place_title_subs(
                result, video_dest, ocr.get(title_id, []), report,
                source_video=source, assignment=assignment)
            self._cache_subs(video_dest, sub_jobs)
            placed.extend(result.subtitle_paths)
        return placed

    async def title_heights(self, job: Job, staging_root: Path, report) -> list[int]:
        """Probe each selected title's ripped video height (0 if unknown). Used to
        decide whether the rip box should encode a job locally or hand it to a
        worker (UHD vs <=1080p)."""
        heights: list[int] = []
        for title_id in job.selected_title_ids:
            cp = job.checkpoints.get(title_id, {})
            prior = cp.get("ripped_path")
            ripped = _resolve_path(self.settings.storage.staging_dir, prior) if prior else None
            h = 0
            if ripped is not None and ripped.exists():
                h = await self._probe_height(ripped, report) or 0
            heights.append(h)
        return heights

    async def identify_phase(self, job: Job, staging_root: Path, report) -> dict[int, str]:
        """Rip the selected titles, then OCR each one's main subtitle track to SRT
        text for subtitle-based episode identification. Places nothing — returns
        ``{title_id: srt_text}`` for the caller to match against references.

        Ripping is done for ALL titles up front, back-to-back, so the optical
        drive never idles mid-job (some drives spin down between reads and
        misbehave on resume). The OCR reads only the ripped files, so the disc is
        already ejected by the time it runs."""
        assert job.disc is not None
        default_lang = self.settings.languages.default.lower()

        # Phase 1 — rip every title back-to-back. With a disc copy the disc
        # is ejected before the first title rather than after the last,
        # which is what this phase's docstring always wanted.
        await self._prepare_disc_once(job, staging_root, report)
        ripped_paths: dict[int, Path] = {}
        for title_id in job.selected_title_ids:
            ripped_paths[title_id] = await self._ensure_ripped(
                job, title_id, staging_root, report)

        self._release_disc_copy(job, staging_root, report)

        # Phase 2 — OCR each title's main track (no disc needed now).
        job.state = JobState.IDENTIFYING
        rips: dict[int, str] = {}
        for title_id in job.selected_title_ids:
            sub_dir = staging_root / "sub" / f"t{title_id:03d}"
            cp = job.checkpoints.setdefault(title_id, {})
            sub_jobs = self._reuse_subs(cp)
            if sub_jobs is None:
                extracted = await self._extract_subs(ripped_paths[title_id], sub_dir, report)
                sub_jobs = extracted or []
                if extracted is not None:
                    cp["subs"] = [
                        _sub_checkpoint(self.settings.storage.staging_dir, sup, tr)
                        for sup, tr in sub_jobs
                    ]
                    _checkpoint(report)
            # Pick the main full track: prefer a non-forced default-language one.
            choice = next(((s, t) for s, t in sub_jobs if not t.forced and (t.language or "").lower() == default_lang), None)
            choice = choice or next(((s, t) for s, t in sub_jobs if not t.forced), None)
            choice = choice or (sub_jobs[0] if sub_jobs else None)
            if choice is None:
                report.on_event(f"Title {title_id}: no subtitles to identify with.")
                continue
            sup, track = choice
            try:
                srt = await self.subtitleedit.ocr_to_srt(sup, track.language or default_lang, sub_dir)
                rips[title_id] = srt.read_text(encoding="utf-8", errors="ignore")
            except Exception as exc:
                report.on_event(f"Title {title_id}: OCR for identification failed: {exc}")
        return rips

    async def _process_one(
        self, job: Job, title_id: int, assignment: EpisodeAssignment, staging_root: Path,
        scheduler: EncodeScheduler, report,
    ) -> TitleResult:
        cp = job.checkpoints.setdefault(title_id, {})
        done = self._title_done(cp, assignment)
        if done is not None:
            report.on_event(f"Title {title_id} already done (resume) — skipping.")
            # Carry the recorded sidecars, and mark where they came from. Without
            # this the result looks like a title that produced no subtitles at
            # all, and the end-of-job check fails a job that is already complete
            # — every time it is resumed.
            return TitleResult(
                title_id=title_id, video_path=done,
                subtitle_paths=list(self._subs_already_done(cp) or []),
                from_checkpoint=True)
        ripped_mkv = await self._ensure_ripped(job, title_id, staging_root, report)
        return await self._process_after_rip(
            job, title_id, assignment, staging_root, ripped_mkv, scheduler, report)

    async def _verify_rip_duration(self, job: Job, title_id: int, ripped_mkv: Path,
                                   report, fresh: bool = True) -> None:
        """Reject a rip whose runtime does not match the disc title.

        TOO SHORT is the signature of a truncated rip (bad sectors) that MakeMKV
        still exited 0 on. TOO LONG means the file holds more than the title it
        was asked for — a DVD title overlapping its "play all", or, as happened
        here, a rip that read an entirely different disc: job 380 produced 3394s
        and 4338s files against 1292s titles and this check waved both through,
        because it only ever looked downwards.

        Skips only when it genuinely cannot compare: the tolerance is off, or the
        scan never reported a duration. A probe FAILURE is not that — this runs
        on a file just written, so a file mkvmerge cannot parse is a broken file,
        and treating "can't tell" as "fine" is what let a failed rip reach the
        encode queue."""
        tol_pct = self.settings.rip_duration_tolerance_pct
        if tol_pct <= 0:
            return
        title = job.disc.title(title_id) if job.disc else None
        expected = title.duration_seconds if title else 0
        if expected <= 0:
            return  # scan didn't report a duration — nothing to compare against
        try:
            actual = (await self.mkvtools.probe(ripped_mkv)).duration_seconds
        except ToolNotFoundError as exc:
            # No prober installed is not a verdict on the file. Failing every rip
            # on a box that simply lacks mkvmerge would be a configuration
            # problem reported as data corruption.
            report.on_event(
                f"Rip length NOT verified for title {title_id}: {exc} "
                f"A truncated rip cannot be detected until this is installed.")
            return
        except Exception as exc:
            # A file just written that cannot be parsed is a broken rip. One
            # RESUMED from a previous run may instead be a storage blip, so say
            # which case this is rather than asserting the rip failed.
            why = ("so the rip did not complete. Not keeping it; a Retry re-rips"
                   if fresh else
                   "so it is not safe to encode. This file was ripped earlier, "
                   "so a storage hiccup is as likely as a bad rip — Retry is safe")
            raise RuntimeError(
                f"The ripped file for title {title_id} can't be read back "
                f"({type(exc).__name__}) — {why}. Underlying error: {exc}"
            ) from exc
        if actual <= 0:
            return
        floor = expected * (100 - tol_pct) // 100
        if actual < floor:
            raise RuntimeError(
                f"Rip of title {title_id} is {actual}s but the disc title is "
                f"{expected}s ({actual * 100 // expected}%) — looks truncated "
                f"(disc read error?). Not keeping it; will re-rip.")
        # The ceiling is deliberately looser than the floor, and not symmetric
        # with it. A good rip routinely runs a second or two OVER the scan's
        # figure (1296s against 1294s is normal), so at the default 3% tolerance
        # a symmetric ceiling would sit close enough to trip on rounding. This
        # is not a precision check — it is here to catch a file holding wholly
        # different content, which overshoots by multiples, not by percent.
        ceiling = expected * (100 + max(tol_pct, _RIP_OVERRUN_PCT)) // 100
        if actual > ceiling:
            raise RuntimeError(
                f"Rip of title {title_id} is {actual}s but the disc title is "
                f"only {expected}s ({actual * 100 // expected}%) — the file "
                f"holds more than this title. Either it overlaps another title "
                f"(common on DVDs, where episodes share cells with the 'play "
                f"all'), or the rip read a different disc than the one that was "
                f"scanned. Not keeping it.")
        report.on_event(f"Rip verified: {actual}s vs disc {expected}s.")

    async def _ensure_ripped(self, job: Job, title_id: int, staging_root: Path, report) -> Path:
        """Rip the title, or reuse a prior run's ripped file. Ejects after the
        last rip. Returns the ripped .mkv path."""
        rip_dir = staging_root / "rip" / f"t{title_id:03d}"
        cp = job.checkpoints.setdefault(title_id, {})
        staging_dir = self.settings.storage.staging_dir
        prior_rip = cp.get("ripped_path")
        prior_path = _resolve_path(staging_dir, prior_rip) if prior_rip else None
        if prior_path and prior_path.exists():
            report.on_event(f"Resuming title {title_id}: reusing ripped file.")
            # Verify it, exactly as a fresh rip is verified. Requeuing a job that
            # failed on a bad disc read reuses whatever MakeMKV managed to write,
            # and only the rip path checked the length — so a truncated title
            # went straight to the encoder and into the library, at full length
            # on disk and missing its last half hour. One probe beats a
            # multi-hour encode of a broken file.
            await self._verify_rip_duration(job, title_id, prior_path, report,
                                            fresh=False)
            ripped_mkv = prior_path
        elif not job.source.startswith(SOURCE_SELECTORS):
            # A job built from an existing .mkv: its source is that file, not a
            # disc. Before declaring it lost, REBUILD the checkpoint from the
            # source, because for this kind of job the two are the same file.
            #
            # Without this, anything that dropped the checkpoint killed the job
            # for good: there is no disc to re-rip, so the raise below is
            # terminal and Retry cannot help. A cluster sweep did exactly that
            # to 121 jobs whose sources were sitting on the NAS the whole time.
            from_source = _resolve_path(staging_dir, job.source)
            if from_source.exists():
                report.on_event(
                    f"Title {title_id}: rip checkpoint was missing; recovered it "
                    f"from the job's own source file.")
                cp["ripped_path"] = _rel_path(staging_dir, from_source)
                report.checkpoint()
                # Fall through exactly like the resume branch above, rather than
                # returning early: the tail of this function is shared.
                ripped_mkv = from_source
            else:
                raise RuntimeError(
                    f"The source file is gone: {prior_path or job.source}. This job "
                    f"was added from a file, so there's no disc to re-rip it from. "
                    f"Put the file back (or check the server can still reach that "
                    f"path) and Retry.")
        else:
            job.state = JobState.RIPPING
            report.on_event(f"Ripping title {title_id} ...")
            # Clear any leftover .mkv from a failed prior attempt. Without this,
            # MakeMKV hits an "already exists — overwrite?" path and, since it
            # reuses the same filename, rip_title sees no NEWLY-appeared file
            # (after - before is empty) and wrongly reports "produced no output".
            if rip_dir.exists():
                for stale in rip_dir.glob("*.mkv"):
                    try:
                        stale.unlink()
                    except OSError:
                        pass
            title = job.disc.title(title_id) if job.disc else None
            speed = _RipSpeed(total_bytes=title.size_bytes if title else 0)
            # MakeMKV writes progressively, so a rip that dies partway still
            # leaves a file behind — "produced no output" alone does not catch
            # it. Watch what it actually SAYS: it reports the failure plainly,
            # and that is the most direct signal there is.
            failed_msgs: list[str] = []

            def watch(msg: str) -> None:
                low = msg.lower()
                if ("failed to save title" in low
                        or re.search(r"\b0 titles saved\b", low)):
                    failed_msgs.append(msg.strip())
                report.on_event(msg)

            # The disc copy when there is one, otherwise the disc. This single
            # substitution is the whole of the one-pass change as far as the
            # rip is concerned: same call, same per-title checkpoint, same
            # resume, same progress label — just a source that does not need
            # re-opening for every title.
            rip_from = getattr(job, "_rip_source", None) or job.source
            ripped = await self.makemkv.rip_title(
                rip_from, title_id, rip_dir,
                on_progress=lambda label, frac: report.on_progress(
                    f"Rip t{title_id}: {label}{speed.update(frac)}", frac
                ),
                on_message=watch,
            )
            if failed_msgs:
                raise RuntimeError(
                    f"MakeMKV could not save title {title_id}: {failed_msgs[0]} "
                    "The partial file is not kept, so a Retry re-rips it. A "
                    "write error here usually means the staging disk or share "
                    "went away mid-rip rather than a bad disc.")
            if not ripped:
                raise RuntimeError(f"MakeMKV produced no output for title {title_id}.")
            ripped_mkv = ripped[0]
            # Guard against a truncated rip that MakeMKV still exited 0 on (disc
            # read errors): only checkpoint once the output's runtime matches the
            # disc title. A short file raises here, so it isn't reused — it re-rips.
            await self._verify_rip_duration(job, title_id, ripped_mkv, report)
            cp["ripped_path"] = _rel_path(staging_dir, ripped_mkv)
            job._ripped_this_run = True
            _checkpoint(report)

        # After the LAST selected title — as long as SOMETHING was ripped this run
        # (not only when this specific title was freshly ripped, or a resume that
        # reuses the last title would skip this) — eject and notify that the disc
        # is done, so you know it's safe to swap in the next one.
        #
        # Skipped entirely when the titles are coming out of a disc backup: that
        # disc was ejected before the first title was made, which is the point of
        # reading it in one pass. Ejecting again here would open a drive that may
        # by now hold somebody else's disc.
        if (
            getattr(job, "_ripped_this_run", False)
            and title_id == job.selected_title_ids[-1]
            and job.source.split(":", 1)[0] in ("disc", "dev")
            and not getattr(job, "_rip_source", None)
        ):
            await self._eject_and_notify(job, report)
        return ripped_mkv

    async def _prepare_disc_once(self, job: Job, staging_root: Path, report) -> None:
        """Read the whole disc once, then point this job's rips at that copy.

        Does nothing unless it is confident, and never fails a job: anything that
        does not work out leaves ``job._rip_source`` unset and the titles are
        ripped straight off the disc exactly as before.
        """
        source = await self._backup_disc_source(job, staging_root, report)
        if source is None:
            return
        job._rip_source = source
        # The disc has been read in full and the copy checked against it, so it
        # is done with — hours earlier than it would otherwise be, which is half
        # the point of doing this at all.
        job._ripped_this_run = True
        await self._eject_and_notify(job, report)

    def _release_disc_copy(self, job: Job, staging_root: Path, report) -> None:
        """Delete the whole-disc copy once every title has been made from it.

        Kept until then on purpose: a job that dies mid-way resumes from the copy
        rather than asking for the disc back. Once the last title is checkpointed
        the copy is dead weight — and it is the size of a disc, sitting in
        staging, which on this setup is a network share.
        """
        if not getattr(job, "_rip_source", None):
            return
        cps = job.checkpoints
        if not all((cps.get(t) or {}).get("ripped_path")
                   for t in job.selected_title_ids):
            return  # something still needs it
        library.cleanup(staging_root / "disc")
        job._rip_source = None
        report.on_event("Disc copy removed — every title has been made from it.")

    async def _backup_disc_source(self, job: Job, staging_root: Path,
                                  report) -> str | None:
        """A ``file:`` selector for a verified copy of this disc, or None.

        None means "carry on against the disc", and every failure path returns
        it. This is an optimisation; it must never be the reason a disc does not
        get ripped.
        """
        if not self.settings.backup_first:
            return None
        if not job.source.startswith(("disc:", "dev:")):
            return None
        if job.disc is None:
            return None
        if len(job.selected_title_ids) <= 1:
            # One title gains nothing — the disc is opened once either way — and
            # a backup would additionally read every title that was NOT wanted.
            return None

        backup_dir = staging_root / "disc"
        have_copy = backup_dir.is_dir() and any(backup_dir.iterdir())
        if not have_copy:
            needed = os_disc_capacity_bytes(job.disc.drive_device)
            if not self._room_for_backup(staging_root, needed, report):
                return None
            report.on_event(
                "Reading the whole disc once, then making each title from that "
                "copy — MakeMKV re-opens the disc for every title otherwise.")
            try:
                await self.makemkv.backup_disc(
                    job.source, backup_dir,
                    on_progress=lambda label, frac: report.on_progress(
                        f"Disc copy: {label}", frac),
                    on_message=report.on_event)
            except Exception as exc:
                report.on_event(
                    f"Reading the disc in one pass failed ({type(exc).__name__}: "
                    f"{exc}) — ripping title by title from the disc instead.")
                library.cleanup(backup_dir)
                return None

        source = f"file:{backup_dir}"
        try:
            copied = await self.makemkv.scan(source)
        except Exception as exc:
            report.on_event(
                f"The disc copy could not be read back ({type(exc).__name__}: "
                f"{exc}) — ripping title by title from the disc instead.")
            library.cleanup(backup_dir)
            return None

        mismatch = _backup_title_mismatch(job.disc, copied)
        if mismatch is not None:
            # Refusing here is the whole safety case. Titles are ripped BY INDEX,
            # so a copy whose titles are numbered differently would file every
            # episode under the wrong number — silently, and plausibly enough
            # that nobody would catch it until the show was unwatchable.
            report.on_event(
                f"The disc copy does not line up with the disc ({mismatch}) — "
                f"discarding it and ripping title by title from the disc, which "
                f"is slower but cannot misnumber the titles.")
            library.cleanup(backup_dir)
            return None
        report.on_event(
            f"Disc copy verified against the disc ({len(copied.titles)} titles "
            f"in the same order) — ripping from it.")
        return source

    def _room_for_backup(self, staging_root: Path, needed: int | None,
                         report) -> bool:
        """Whether a whole-disc copy fits, with headroom for the rips beside it."""
        if not needed:
            return True  # the OS can't tell us the disc size; don't block on it
        try:
            staging_root.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(staging_root).free
        except OSError:
            return True  # can't tell; the backup itself will fail if it must
        # The titles are written alongside the copy, and together they come to
        # roughly twice the disc.
        if free >= needed * 2:
            return True
        report.on_event(
            f"Not enough room to copy the disc first "
            f"({free / 1_000_000_000:.0f} GB free, about "
            f"{needed * 2 / 1_000_000_000:.0f} GB needed) — ripping title by "
            f"title from the disc instead.")
        return False

    async def _eject_and_notify(self, job: Job, report) -> None:
        """Open the tray and say the disc is done with, so the next one can go in."""
        ejected = False
        if self.settings.auto_eject:
            device = await self._eject_device_for(job)
            try:
                # In a thread and bounded. Ejecting talks to the drive, so a
                # drive that has stopped answering blocks for as long as it
                # likes — and this used to run INLINE on the event loop, so
                # one unresponsive tray froze the entire server, not just
                # this job. The rip is finished and checkpointed by the time
                # we get here, so a tray that won't open must cost a log
                # line, never the job: past the deadline the thread is
                # abandoned (it ends when the drive finally errors) and the
                # job carries on to subtitles and the encode.
                await asyncio.wait_for(
                    in_detached_thread(eject, device), _EJECT_TIMEOUT)
                ejected = True
                _emit_ejected(report, device)  # drop the drive's cached disc name
                report.on_event(
                    f"Ejected disc ({device or 'default drive'}) — safe to load the next one."
                )
            except (TimeoutError, asyncio.TimeoutError):
                report.on_event(
                    f"Auto-eject timed out after {_EJECT_TIMEOUT:.0f}s "
                    f"({device or 'default drive'}) — the drive isn't responding. "
                    f"The rip is complete and saved; eject it by hand.")
            except Exception as exc:
                hint = (" — on Windows the force-eject IOCTL needs admin; run "
                        "`serve` as Administrator" if sys.platform.startswith("win") else "")
                report.on_event(f"Auto-eject failed ({device or 'no device'}): {exc}{hint}")
        name = self._rip_notify_name(job)
        _emit_notify(
            report, f"💿 Finished ripping: {name}",
            f"“{name}” is done ripping"
            + (" — disc ejected, safe to swap in the next one." if ejected
               else ". Safe to swap the disc."))

    def _rip_notify_name(self, job: Job) -> str:
        """A human name for the just-ripped disc: prefer the movie/show title from
        the naming mapping, fall back to the disc label, then the raw source."""
        if job.selected_title_ids:
            a = job.assignments.get(job.selected_title_ids[-1])
            title = getattr(a, "show_or_movie_title", None) if a else None
            if title:
                return title
        if job.disc and job.disc.disc_name:
            return job.disc.disc_name
        return job.source

    async def _eject_device_for(self, job: Job) -> str | None:
        """The OS device to auto-eject: re-resolve it from the CURRENT rip source
        (disc:N -> drive letter) rather than trusting job.disc.drive_device, which
        can go stale after a drive change — and eject the WRONG disc (e.g. another
        job's disc now sitting in the original drive)."""
        device = job.disc.drive_device if job.disc else None
        src = job.source or ""
        if src.startswith("disc:"):
            try:
                idx = int(src.split(":", 1)[1])
                drives = await self.makemkv.list_drives(timeout=15)
                drv = next((d for d in drives if d.index == idx), None)
                if drv and drv.device:
                    device = drv.device
            except Exception:
                pass  # fall back to the stored device
        return device

    async def _process_after_rip(
        self, job: Job, title_id: int, assignment: EpisodeAssignment, staging_root: Path,
        ripped_mkv: Path, scheduler: EncodeScheduler, report,
    ) -> TitleResult:
        """Single-machine path: subtitles + grain, then encode + place."""
        prep = await self._prepare_title(job, title_id, assignment, staging_root, ripped_mkv, report)
        if prep.video_path:  # already placed (resume)
            return prep
        enc = await self._encode_title(
            job, title_id, assignment, staging_root, ripped_mkv, scheduler, report)
        enc.subtitle_paths = prep.subtitle_paths
        return enc

    async def _prepare_title(
        self, job: Job, title_id: int, assignment: EpisodeAssignment, staging_root: Path,
        ripped_mkv: Path, report,
    ) -> TitleResult:
        """Subtitles (extract + OCR + place sidecars) and grain decision."""
        result = TitleResult(title_id=title_id)
        sub_dir = staging_root / "sub" / f"t{title_id:03d}"
        cp = job.checkpoints.setdefault(title_id, {})
        await self._apply_version_suffix(assignment, cp, ripped_mkv, report)
        video_dest = self._video_path_for(assignment)
        done = self._title_done(cp, assignment)
        if done is not None:
            result.video_path = done
            return result

        # Extract subtitle images, OCR them, and place the SRT sidecars. (The
        # parallel path lives in prep_phase; here OCR runs inline for one title.)
        done = self._subs_already_done(cp, staging_root)
        if done is not None:
            # Carry them onto the result: the output check at the end compares
            # against these, so a resume that skipped OCR must not look like a
            # job that produced no subtitles at all.
            result.subtitle_paths = list(done)
            report.on_event(
                f"Subtitles already done ({len(done)} sidecar(s)) — skipping OCR.")
        else:
            sub_jobs = await self._extract_title_subs(job, title_id, ripped_mkv, sub_dir, report)
            ocr = await self._ocr_all(job, [(title_id, assignment, ripped_mkv, sub_dir, sub_jobs)], report)
            pairs = ocr.get(title_id, [])
            await self._place_title_subs(
                result, video_dest, pairs, report,
                source_video=ripped_mkv, assignment=assignment,
                # Assemble in staging ONLY while the video is still to come.
                # Once it is placed there is no half-built item to protect, and
                # staging a re-OCR'd sidecar would strand it: the encode step
                # returns early for an already-placed title, so nothing would
                # ever publish it.
                stage_to=(None if cp.get("placed")
                          else self._staged_of(staging_root, video_dest)))
            self._cache_subs(video_dest, sub_jobs)
            self._record_if_complete(cp, sub_jobs, pairs, result, report)
            _checkpoint(report)

        # Resolve grain (may park at an interactive preview gate — UI only). Skip
        # if a prior run already decided it, so a resume doesn't re-prompt (which
        # otherwise loops the grain gate on a box that restarts mid-job).
        if "tune_grain" not in cp:
            decision = await self._resolve_grain(job, ripped_mkv, title_id, staging_root, report)
            # ONCE per disc, not per title. A disc's titles share one source and
            # one encode, so asking per episode re-opens the preview gate for
            # every episode of a season disc — which reads as an endless "pick
            # grain" loop. prep_phase does this for the clustered path; this is
            # the same decision for the single-box path. setdefault, so a title
            # that already has a saved choice keeps it.
            for tid, _a in _assigned_titles(job):
                job.checkpoints.setdefault(tid, {}).setdefault("tune_grain", decision)
            _checkpoint(report)
        return result

    async def _encode_uhd(self, src: Path, dst: Path, tune_grain: bool,
                          encode_dir: Path, report) -> None:
        """UHD encode via ffmpeg: Dolby Vision (RPU round-trip) when enabled and
        detected, else plain HDR10 passthrough. Any DV failure falls back to
        HDR10 so a job never dies on the DV path."""
        enc = self.settings.encode
        hdr, duration = await self.ffmpeg.probe_video(src)
        want_dv = (enc.dovi and hdr.is_dolby_vision
                   and self.dovitool.available and bool(self.settings.tools.mkvmerge))
        if want_dv:
            try:
                await self._encode_dovi(src, dst, hdr, duration, tune_grain, encode_dir, report)
                return
            except Exception as exc:
                report.on_event(
                    f"Dolby Vision path failed ({type(exc).__name__}: {exc}); "
                    "falling back to HDR10.")
        elif enc.dovi and hdr.is_dolby_vision:
            report.on_event("Dolby Vision disc but dovi_tool/mkvmerge unavailable — HDR10 only.")
        report.on_event(
            f"ffmpeg UHD: source is {hdr.summary()} -> libx265 preset "
            f"{enc.ffmpeg_preset}, CRF {enc.ffmpeg_crf}, audio {enc.ffmpeg_audio}"
            f"{', tune=grain' if tune_grain else ''}.")
        await self.ffmpeg.encode(
            src, dst, on_progress=lambda p: self._encode_progress(p, report),
            tune_grain=tune_grain, low_priority=enc.low_priority)
        await self._verify_hdr(dst, hdr, report)

    async def _verify_hdr(self, dst: Path, want, report) -> None:
        """Confirm the encode kept the thing this whole code path exists for.

        An HDR10 source that comes out SDR is a SILENT failure: the file plays
        fine and just looks washed out on an HDR TV, so you'd only notice months
        later on the couch. It costs one ffprobe to know instead."""
        if not want.is_hdr:
            return
        try:
            got, _ = await self.ffmpeg.probe_video(dst)
        except Exception as exc:
            report.on_event(
                f"Could not verify HDR on the output ({type(exc).__name__}: {exc}).")
            return
        # Compare VALUES, not just presence. A stripped encode is tagged SDR —
        # colorprim/transfer/colormatrix are all there, all saying bt709 — so
        # "is the field present?" called that a PASS and printed "output carries
        # SDR (bt709)" as the success line. That is the whole silent loss this
        # check exists to catch, and it is the ONLY evidence for a source with no
        # mastering metadata to lose (HLG, or HDR10 whose side data ffprobe
        # didn't report): nothing goes missing, the values just change.
        lost = [f"{name} ({src_v} → {out_v or 'absent'})"
                for name, src_v, out_v in (
                    ("transfer", want.transfer, got.transfer),
                    ("primaries", want.colorprim, got.colorprim),
                    ("matrix", want.colormatrix, got.colormatrix),
                    ("master-display", want.master_display, got.master_display),
                    ("max-cll", want.max_cll, got.max_cll),
                ) if src_v and src_v != out_v]
        if lost:
            report.on_event(
                f"HDR CHECK FAILED — the output carries {got.summary()}; it lost "
                + ", ".join(lost)
                + ". It will play, but look washed out on an HDR TV. The file is "
                "kept so you can judge it; the usual cause is an ffmpeg built "
                "without x265 HDR support (check `ffmpeg -h encoder=libx265`).")
        else:
            report.on_event(f"HDR check passed — output carries {got.summary()}.")

    async def _encode_dovi(self, src: Path, dst: Path, hdr, duration: float,
                           tune_grain: bool, encode_dir: Path, report) -> None:
        """Extract the DV RPU, re-encode video, inject the RPU, remux with audio.
        Uses big intermediate raw-HEVC files (cleaned up in a finally)."""
        tmp = encode_dir / "dovi"
        tmp.mkdir(parents=True, exist_ok=True)
        src_hevc, rpu = tmp / "src.hevc", tmp / "rpu.bin"
        enc_hevc, dv_hevc = tmp / "enc.hevc", tmp / "enc_dv.hevc"
        try:
            report.on_event(f"Dolby Vision (profile {hdr.dv_profile}) — extracting RPU…")
            await self.ffmpeg.extract_hevc(src, src_hevc)
            await self.dovitool.extract_rpu(src_hevc, rpu, hdr.dv_profile)
            src_hevc.unlink(missing_ok=True)  # free space before the re-encode
            report.on_event("Re-encoding video (x265, HDR10 base)…")
            await self.ffmpeg.encode_hevc(
                src, enc_hevc, hdr=hdr, duration=duration,
                on_progress=lambda p: self._encode_progress(p, report),
                tune_grain=tune_grain, low_priority=self.settings.encode.low_priority)
            report.on_event("Injecting Dolby Vision RPU…")
            await self.dovitool.inject_rpu(enc_hevc, rpu, dv_hevc)
            enc_hevc.unlink(missing_ok=True)
            report.on_event("Muxing DV + audio to MKV…")
            await self.mkvtools.mux_dovi(dv_hevc, src, dst)
        finally:
            for f in (src_hevc, rpu, enc_hevc, dv_hevc):
                try:
                    f.unlink(missing_ok=True)
                except OSError:
                    pass

    async def _encode_title(
        self, job: Job, title_id: int, assignment: EpisodeAssignment, staging_root: Path,
        ripped_mkv: Path, scheduler: EncodeScheduler, report,
    ) -> TitleResult:
        """Encode + place the video. Grain was decided in _prepare_title."""
        result = TitleResult(title_id=title_id)
        encode_dir = staging_root / "encode"
        cp = job.checkpoints.setdefault(title_id, {})
        video_dest = self._video_path_for(assignment)
        placed = self._placed_video(cp, assignment)
        if placed is not None:
            result.video_path = placed
            return result

        tune_grain = cp.get("tune_grain")
        if tune_grain is None:
            tune_grain = (self.settings.encode.tune_grain
                          if job.tune_grain is None else job.tune_grain)
        # A run that encoded fine and then failed to PLACE left the finished file
        # in staging (staging is only swept on success). Re-encoding it costs the
        # whole multi-hour encode again to fix a share that was full or read-only.
        finished = self._finished_encode(cp, bool(tune_grain))
        if finished is not None:
            report.on_event(
                f"Reusing the finished encode from the previous run "
                f"({finished.name}) — only placing it failed last time.")
            return await self._place_encoded(
                job, cp, finished, video_dest, assignment, result, report,
                staging_root)
        height = await self._probe_height(ripped_mkv, report)
        weight = encode_weight(height)
        deinterlace = self._should_deinterlace(height)
        enc = self.settings.encode
        use_ffmpeg = (enc.ffmpeg_uhd and self.ffmpeg.available
                      and bool(height) and height >= enc.uhd_min_height)
        # HDR/Dolby Vision downgrade guard: a UHD title that ISN'T taking the
        # ffmpeg path goes through HandBrake, which silently drops HDR10/DV. Warn
        # loudly — and, when the intent was clearly to keep HDR (ffmpeg_uhd is on
        # but ffmpeg is missing on THIS machine, e.g. a worker), push a notification
        # so the misconfig doesn't quietly produce SDR files for hours.
        if bool(height) and height >= enc.uhd_min_height and not use_ffmpeg:
            name = getattr(assignment, "show_or_movie_title", None) or video_dest.stem
            if enc.uhd_require_ffmpeg:
                # Stop rather than warn. HandBrake would produce a watchable file,
                # so the loss is silent — and a 4K encode runs for hours before you
                # notice the HDR is gone. Failing costs seconds and keeps the rip,
                # so fixing the setting and hitting Retry re-encodes only.
                why = ("ffmpeg isn't available on this machine"
                       if enc.ffmpeg_uhd else
                       "the ffmpeg UHD path is disabled in Settings")
                raise RuntimeError(
                    f"{name} is UHD ({height}p) and would be encoded by HandBrake, "
                    f"which drops HDR10/Dolby Vision — {why}. Enable 'ffmpeg UHD "
                    "path' (and install ffmpeg on whichever box encodes), then "
                    "Retry; the rip is kept, so only the encode re-runs. To accept "
                    "SDR instead, turn off 'Block HandBrake on UHD'.")
            if enc.ffmpeg_uhd and not self.ffmpeg.available:
                warn = (f"⚠ {name}: UHD ({height}p) but ffmpeg isn't available here — "
                        "HandBrake will encode it and HDR/Dolby Vision will NOT be "
                        "preserved. Install ffmpeg (and dovi_tool for DV) on this "
                        "worker to keep HDR.")
                report.on_event(warn)
                _emit_notify(report, f"⚠ HDR not preserved: {name}", warn)
            elif not enc.ffmpeg_uhd:
                report.on_event(
                    f"Note: {name} is UHD ({height}p) — HandBrake encode won't keep "
                    "HDR/Dolby Vision (the ffmpeg UHD path is disabled in settings).")
        acquired = await scheduler.acquire(weight)
        try:
            job.state = JobState.ENCODING
            report.on_event(
                f"Encoding title {title_id} ({height or '?'}p, weight {weight}, "
                f"grain={'on' if tune_grain else 'off'}"
                f"{', deinterlace (SD)' if deinterlace else ''}"
                f"{', ffmpeg/HDR' if use_ffmpeg else ''}) -> {video_dest.name} ..."
            )
            encoded = encode_dir / video_dest.name
            if use_ffmpeg:
                # UHD: ffmpeg+libx265, preserving HDR10 (and Dolby Vision if
                # enabled). No decomb — UHD Blu-ray is progressive.
                await self._encode_uhd(ripped_mkv, encoded, bool(tune_grain),
                                       encode_dir, report)
            else:
                report.on_event(f"HandBrake preset: {self.handbrake.speed_summary()}")
                await self.handbrake.encode(
                    ripped_mkv, encoded,
                    on_progress=lambda p: self._encode_progress(p, report),
                    tune_grain=tune_grain, deinterlace=deinterlace,
                )
        finally:
            await scheduler.release(acquired)
        # Never move an empty file into the library. HandBrake (unlike the ffmpeg
        # wrappers) can exit 0 having written nothing — ENOSPC on the staging
        # disk is the usual way — and placing that file then checkpoints
        # cp["placed"] on a 0-byte video: the end-of-job check fails on it while
        # every Retry skips the encode that would replace it, so the job re-fails
        # identically forever and only deleting the file by hand escapes.
        if not encoded.exists() or encoded.stat().st_size == 0:
            raise RuntimeError(
                f"The encoder exited successfully but produced an empty file for "
                f"title {title_id} ({encoded.name}). Nothing was put in the "
                f"library. The usual cause is the staging disk filling up "
                f"({encode_dir}) — free some space and Retry; the rip is kept, "
                f"so only the encode re-runs.")
        # Record the finished encode BEFORE placing it, so a failure at the move
        # (full/read-only/dropped output share) doesn't cost the encode as well.
        cp["encoded_path"] = _rel_path(self.settings.storage.staging_dir, encoded)
        cp["encoded_grain"] = bool(tune_grain)
        _checkpoint(report)
        return await self._place_encoded(
            job, cp, encoded, video_dest, assignment, result, report, staging_root)

    async def _place_encoded(
        self, job: Job, cp: dict, encoded: Path, video_dest: Path,
        assignment: EpisodeAssignment, result: TitleResult, report,
        staging_root: Path | None = None,
    ) -> TitleResult:
        """Move a finished encode into the library and checkpoint it as placed.

        PLACING is set BEFORE the move, not after it: this is where a full,
        read-only or dropped output share fails, and reporting that as "FAILED
        during encoding" sends you after the encoder — tools, CPU, staging disk —
        when the encode succeeded and the share is the problem. Opposite fixes.
        """
        job.state = JobState.PLACING
        if staging_root is not None:
            # Finish ASSEMBLING, then publish the whole item at once. The
            # sidecars and artwork have been waiting in staging precisely so the
            # library never shows a folder with a poster and subtitles and no
            # film in it — which is long enough to copy, or for Plex to scan.
            staged = self._staged_of(staging_root, video_dest)
            library.place(encoded, staged)
            if self.settings.write_nfo:
                try:
                    nfo.write_nfo(staged, assignment)
                except OSError as exc:
                    report.on_event(f"NFO sidecar skipped ({type(exc).__name__}: {exc}).")
            library.publish(staged, video_dest)
            result.video_path = video_dest
        else:
            result.video_path = library.place(encoded, video_dest)
            if self.settings.write_nfo:
                try:
                    nfo.write_nfo(result.video_path, assignment)
                except OSError as exc:
                    report.on_event(f"NFO sidecar skipped ({type(exc).__name__}: {exc}).")
        cp["placed"] = True
        # The staging file was MOVED into the library, so the reuse record above
        # now points at nothing; leaving it would make a later re-encode (the
        # library file deleted, say) "reuse" a file that isn't there.
        cp.pop("encoded_path", None)
        cp.pop("encoded_grain", None)
        _checkpoint(report)
        report.on_event(f"Placed video: {result.video_path}")
        return result

    async def _resolve_grain(
        self, job: Job, ripped_mkv: Path, title_id: int, staging_root: Path, report
    ) -> bool:
        """Decide whether to encode with --tune grain.

        Modes: "on"/"off" force it; "auto" uses job/config default; "preview"
        renders a still comparison and asks the reporter (web UI) to choose.
        """
        default = self.settings.encode.tune_grain if job.tune_grain is None else job.tune_grain
        if job.grain_mode == "on":
            return True
        if job.grain_mode == "off":
            return False
        if job.grain_mode != "preview":
            return default

        # The preview is HandBrake-rendered clips; ffmpeg is only for optional
        # poster stills. So we only need the UI resolver (CLI runs can't gate).
        resolver = getattr(report, "resolve_grain", None)
        if resolver is None:
            if job.grain_mode == "preview":
                report.on_event("Grain preview needs the web UI; using default.")
            return default
        try:
            report.on_event("Rendering grain preview (short sample, both ways)…")
            preview = await self._generate_grain_preview(
                job, ripped_mkv, title_id, staging_root / "preview", report
            )
            return await resolver(job, preview)
        except Exception as exc:
            report.on_event(f"Grain preview failed ({type(exc).__name__}: {exc}); using default.")
            return default

    async def _generate_grain_preview(
        self, job: Job, ripped_mkv: Path, title_id: int, preview_dir: Path, report
    ) -> "GrainPreview":
        title = job.disc.title(title_id) if job.disc else None
        duration_s = title.duration_seconds if title else 1800
        start = max(1, int(duration_s * 0.33))  # sample ~1/3 in
        # A few seconds so the grain reads in motion (it's a temporal effect);
        # still short enough to render in seconds on veryfast.
        clip_secs = 4.0
        off_clip = preview_dir / "off.mp4"
        on_clip = preview_dir / "on.mp4"
        await self.handbrake.encode_clip(
            ripped_mkv, off_clip, start=start, duration=clip_secs, tune_grain=False)
        await self.handbrake.encode_clip(
            ripped_mkv, on_clip, start=start, duration=clip_secs, tune_grain=True)
        # Still frames double as <video> posters (instant paint before the clip
        # buffers) and as a fallback where video can't play. They need ffmpeg,
        # which is optional — the clips above are the real preview, so skip the
        # posters (rather than failing the whole preview) when ffmpeg is absent.
        off_png = on_png = None
        if self.ffmpeg.available:
            try:
                off_png = preview_dir / "grain_off.png"
                on_png = preview_dir / "grain_on.png"
                await self.ffmpeg.extract_frame(off_clip, off_png, at_seconds=clip_secs / 2)
                await self.ffmpeg.extract_frame(on_clip, on_png, at_seconds=clip_secs / 2)
            except Exception as exc:
                report.on_event(f"Preview poster frames skipped ({type(exc).__name__}).")
                off_png = on_png = None
        report.on_event("Grain preview ready — choose grain on/off in the UI.")
        return GrainPreview(off_clip=off_clip, on_clip=on_clip, off_png=off_png, on_png=on_png)

    def _should_deinterlace(self, height: int | None) -> bool:
        """Whether to apply HandBrake's adaptive decomb. "auto" targets SD/DVD
        sources (interlaced 480i/576i); "on"/"off" force it. decomb only touches
        actually-combed frames, so it's safe on progressive SD too."""
        mode = self.settings.encode.deinterlace
        if mode == "on":
            return True
        if mode == "off":
            return False
        return bool(height) and height <= self.settings.encode.deinterlace_max_height

    async def _probe_height(self, ripped_mkv: Path, report) -> int | None:
        try:
            return await self.mkvtools.video_height(ripped_mkv)
        except Exception as exc:
            report.on_event(
                f"Could not probe resolution ({type(exc).__name__}: {exc}); assuming non-UHD weight.")
            return None

    def _placed_video(self, cp: dict, assignment: EpisodeAssignment) -> Path | None:
        """The library file this title's ``placed`` checkpoint claims — but only
        when it is really there and not empty. None means "encode it again".

        A 0-byte file exists(), so honouring the flag on one was a permanent dead
        end: the end-of-job check fails on "video is 0 bytes" while every Retry
        takes the resume shortcut past the encode that would replace it, so the
        job re-fails identically forever and the only escape is deleting the file
        by hand. An unreadable path (share down) is treated the same way — as not
        placed — which is what already happened when the file was missing."""
        if not cp.get("placed"):
            return None
        dest = self._video_path_for(assignment)
        try:
            return dest if dest.stat().st_size > 0 else None
        except OSError:
            return None

    def _title_done(self, cp: dict, assignment: EpisodeAssignment) -> Path | None:
        """The placed video of a title that is REALLY finished — video AND
        subtitles — or None, meaning redo whichever half is unfinished.

        The video half alone used to decide this, and "placed" then skipped the
        subtitle work too, which is how a job went green with nothing for Plex to
        read: the sidecars were deleted (or never written, because OCR failed) and
        the resume reported "already done — skipping" every time."""
        dest = self._placed_video(cp, assignment)
        if dest is None or self._subs_unfinished(cp):
            return None
        return dest

    def _subs_unfinished(self, cp: dict) -> bool:
        """Whether a placed title still owes subtitle work. Two shapes:

        - sidecars were recorded and are no longer on disk (deleted, or a box
          that cannot see them) — redo them, they were part of this title;
        - images were extracted but no sidecars were ever recorded, i.e. OCR
          failed and _record_if_complete deliberately refused to record it.

        A title with NEITHER record predates subtitle checkpoints entirely. The
        run that placed it verified it, so it is left alone — inventing work for
        it would re-rip every old title a resume touches."""
        if "subs_done" in cp:
            return self._subs_already_done(cp) is None
        return bool(cp.get("subs"))

    def _finished_encode(self, cp: dict, tune_grain: bool) -> Path | None:
        """A previous run's completed encode still sitting in staging, if it is
        usable for this run — else None (encode it).

        Only recorded between the encode finishing and the file being placed, so
        finding one means placing is what failed. The grain setting is part of
        the record because Retry can be preceded by changing it in the UI, and
        silently shipping the old encode would ignore that choice."""
        rel = cp.get("encoded_path")
        if not rel or cp.get("encoded_grain") != bool(tune_grain):
            return None
        path = _resolve_path(self.settings.storage.staging_dir, rel)
        try:
            return path if path.stat().st_size > 0 else None
        except OSError:
            return None

    def _record_subs_done(self, cp: dict, paths: list[Path]) -> None:
        """Record that a title's sidecars are placed, so a resume skips OCR.

        Stored relative to output_dir (as rip checkpoints are to staging) so the
        record survives a move between machines."""
        out = self.settings.storage.output_dir
        cp["subs_done"] = [_rel_path(out, p) for p in paths]

    def _record_if_complete(self, cp: dict, extracted: list, pairs: list,
                            result: TitleResult, report) -> None:
        """Record completion only when EVERY extracted track produced an SRT.

        _ocr_all drops a track whose OCR raised, so a failed OCR leaves an empty
        result — which is exactly the shape of "this disc has no subtitles".
        Recording that as done meant a single OCR failure permanently convinced
        every later run there was nothing to do, while the encode carried on and
        marked the title placed. The subtitles could then never be recovered.
        """
        if len(pairs) == len(extracted):
            self._record_subs_done(cp, result.subtitle_paths)
            return
        report.on_event(
            f"{len(extracted) - len(pairs)} of {len(extracted)} subtitle track(s) "
            "failed OCR — NOT marking subtitles done, so a retry re-runs them "
            "(the extracted images are kept, so no re-rip).")

    def _subs_already_done(self, cp: dict,
                           staging_root: Path | None = None) -> list[Path] | None:
        """The placed sidecars, if every one is still on disk; else None (redo).

        Only the extracted .sup images were checkpointed before, so a resume
        re-ran OCR — the expensive half, minutes to hours per disc — with the
        finished .srt files sitting right beside the video the whole time.

        An empty list is a real answer, not a missing one: a title whose disc
        carries no kept-language subtitles is DONE with zero sidecars, and must
        not be retried forever. That is why absence is None rather than []."""
        saved = cp.get("subs_done")
        if saved is None:
            return None
        out = self.settings.storage.output_dir
        paths = [_resolve_path(out, rel) for rel in saved]

        def present(p: Path) -> bool:
            # A sidecar that is ASSEMBLED but not yet published lives in staging
            # under the same relative name. Counting that as missing re-ran OCR
            # — the expensive half, minutes to hours — on every resume between
            # finishing the subtitles and finishing the encode, which is exactly
            # the window a long encode leaves open.
            if p.exists():
                return True
            return (staging_root is not None
                    and self._staged_of(staging_root, p).exists())

        if any(not present(p) for p in paths):
            return None   # deleted, or a box that cannot see them — redo properly
        return paths

    def _reuse_subs(self, cp: dict) -> list[tuple[Path, SubtitleTrack]] | None:
        """Rebuild the (sup, track) list from a checkpoint if every .sup still
        exists on disk; otherwise None (re-extract).

        An empty/absent checkpoint returns None so we re-run the (cheap) identify
        rather than trusting a possibly-failed prior extraction that recorded 0."""
        saved = cp.get("subs")
        if not saved:
            return None
        out: list[tuple[Path, SubtitleTrack]] = []
        for s in saved:
            sup = _resolve_path(self.settings.storage.staging_dir, s["sup"])
            if not sup.exists():
                return None
            out.append((sup, SubtitleTrack(
                track_id=s["track_id"], codec=s.get("codec", ""), language=s.get("language"),
                is_pgs=True, forced=s.get("forced", False), sdh=s.get("sdh", False),
                name=s.get("name"),
            )))
        return out

    async def _extract_subs(
        self, ripped_mkv: Path, sub_dir: Path, report
    ) -> list[tuple[Path, SubtitleTrack]] | None:
        """Extract kept-language PGS tracks to .sup files. Returns (sup, track).

        The .sup filename includes the track id so multiple same-language tracks
        get distinct files instead of overwriting each other.
        """
        report.on_event("Identifying subtitle tracks…")
        async with self._extract_sem:
            return await self._extract_subs_inner(ripped_mkv, sub_dir, report)

    async def _extract_subs_inner(
        self, ripped_mkv: Path, sub_dir: Path, report
    ) -> list[tuple[Path, SubtitleTrack]] | None:
        try:
            tracks = await self.mkvtools.identify_subtitles(ripped_mkv)
        except Exception as exc:
            report.on_event(f"Subtitle identify failed: {type(exc).__name__}: {exc}")
            return None  # failed (not "no subs") -> caller won't checkpoint

        kept = [
            (track, sub_dir / f"{ripped_mkv.stem}.t{track.track_id}.{track.language or 'und'}.sup")
            for track in tracks
            if track.is_pgs and self.settings.languages.is_kept(track.language)
        ]
        if not kept:
            return []  # genuinely no kept subtitles

        langs = ", ".join((t.language or "und") for t, _ in kept)
        report.on_event(
            f"Extracting {len(kept)} subtitle track(s) [{langs}] in one pass — "
            "can take a few minutes on large files…")
        try:
            await self.mkvtools.extract_tracks(
                ripped_mkv, [(t.track_id, p) for t, p in kept],
                on_progress=lambda f: report.on_progress("Extracting subtitles", f),
            )
        except Exception as exc:
            report.on_event(f"Subtitle extraction failed: {type(exc).__name__}: {exc}")
            return None
        return [(p, t) for t, p in kept]

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

    def _staged_of(self, staging_root: Path, final: Path) -> Path:
        """Where a finished library file is ASSEMBLED before it is published.

        Mirrors the library layout under the job's staging dir, so a movie
        folder is built complete and then moved in one step rather than growing
        a poster and subtitles hours before it gains a video."""
        out = self.settings.storage.output_dir
        try:
            rel = Path(final).relative_to(out)
        except ValueError:
            rel = Path(final.name)        # outside the library: stage by name
        return staging_root / "library" / rel

    def _poster_folder(self, a: EpisodeAssignment, staging_root: Path | None = None) -> Path:
        """Where Plex wants the poster: the movie folder, or a show's root folder
        (above Season NN) for TV."""
        vp = self._video_path_for(a)
        if staging_root is not None:
            vp = self._staged_of(staging_root, vp)
        return vp.parent.parent if a.kind is MediaKind.TV else vp.parent

    async def _resolve_source_drive(self, job: Job, report) -> None:
        """Follow the disc across drives: if the job's disc:N no longer holds this
        disc (moved to another drive, or makemkvcon renumbered the drives), find
        the drive that does and re-point the rip there. Match by disc TITLE (the
        volume label) first, then fall back to the fingerprint hash. Best-effort;
        on any hiccup the original source is left untouched."""
        src = job.source or ""
        if not src.startswith("disc:") or job.disc is None:
            return  # only for scanned disc jobs
        try:
            cur_idx = int(src.split(":", 1)[1])
        except ValueError:
            return
        try:
            # Short timeout: a wedged drive can make enumeration slow, and we don't
            # want to delay the rip — just proceed with the original source then.
            drives = await self.makemkv.list_drives(timeout=20)
        except Exception:
            return

        want_label = (job.disc.volume_label or job.disc.disc_name or "").strip()

        def label_matches(d) -> bool:
            return bool(want_label) and (d.disc_name or "").strip().casefold() == want_label.casefold()

        cur = next((d for d in drives if d.index == cur_idx), None)
        if cur is not None and label_matches(cur):
            return  # disc is still in the drive the job expects

        # 1) another drive whose disc title (volume label) matches
        for d in drives:
            if d.index == cur_idx or d.index < 0 or getattr(d, "makemkv_blind", False):
                continue
            if label_matches(d):
                self._rebind_drive(job, d, report, "disc title")
                return

        # 2) fallback: fingerprint (scan each candidate that has a disc — costlier)
        if job.fingerprint:
            for d in drives:
                if (d.index == cur_idx or d.index < 0 or getattr(d, "makemkv_blind", False)
                        or not d.disc_name):
                    continue
                try:
                    scanned = await self.makemkv.scan(f"disc:{d.index}")
                    if discinfo.disc_fingerprint(scanned) == job.fingerprint:
                        self._rebind_drive(job, d, report, "fingerprint")
                        return
                except Exception:
                    continue
        # No match anywhere — leave the source as-is; the rip fails with the
        # existing "reinsert the disc" handling.

    def _rebind_drive(self, job: Job, drive, report, how: str) -> None:
        new_src = f"disc:{drive.index}"
        if new_src == job.source:
            return
        report.on_event(
            f"Disc found in {new_src} ({drive.disc_name}) by {how} — ripping from "
            f"there instead of {job.source}.")
        job.source = new_src
        if job.disc is not None:
            job.disc.source = new_src
            if drive.device:
                job.disc.drive_device = drive.device

    async def _place_cover_art(self, job: Job, report,
                               staging_root: Path | None = None) -> None:
        """Copy the disc's own cover art to each output folder as poster.jpg.
        Best-effort and idempotent; must run BEFORE ripping (auto-eject removes
        the disc after the last rip)."""
        if not self.settings.extract_cover_art or job.disc is None:
            return
        src = job.disc.source or ""
        iso = src[len("iso:"):] if src.startswith("iso:") else None
        root = coverart.disc_root(job.disc)
        if root is None and iso is None:
            return  # unsupported source (e.g. a Linux optical device path)
        folders = {
            self._poster_folder(a, staging_root)
            for tid in job.selected_title_ids
            if (a := job.assignments.get(tid)) is not None
        }
        exts = (".jpg", ".jpeg", ".png")
        for folder in folders:
            have_poster = any((folder / f"poster{e}").exists() for e in exts)
            have_fanart = any((folder / f"fanart{e}").exists() for e in exts)
            if have_poster and have_fanart:
                continue  # already placed (resume / other disc of a set)
            try:
                if iso is not None:
                    art = await asyncio.to_thread(
                        coverart.extract_art_from_iso, iso, folder,
                        poster=not have_poster, fanart=not have_fanart)
                else:
                    art = await asyncio.to_thread(
                        coverart.extract_art, root, folder,
                        poster=not have_poster, fanart=not have_fanart)
                placed = [p.name for p in (art.poster, art.fanart) if p]
                if placed:
                    report.on_event(f"Saved disc art → {folder.name}/{', '.join(placed)}")
            except Exception as exc:
                report.on_event(f"Cover art skipped ({type(exc).__name__}: {exc}).")

    async def _place_tmdb_art(self, job: Job, report,
                              staging_root: Path | None = None) -> None:
        """Prep-time hook: place TMDB art for the job's selected titles."""
        assignments = [job.assignments[t] for t in job.selected_title_ids
                       if t in job.assignments]
        await self.place_tmdb_art(assignments, report, staging_root)

    async def place_tmdb_art(self, assignments, report,
                             staging_root: Path | None = None) -> int:
        """Fetch the real poster + backdrop from TMDB and write them as
        poster.jpg / fanart.jpg for each assignment's folder, OVERRIDING whatever
        art is there (disc menu graphics, a stale/wrong poster from a prior tool,
        etc.). Matches by title + year like naming, so it needs no disc and fixes
        already-placed titles too. Returns how many folders got fresh art.

        Best-effort: any single title's failure is logged and skipped."""
        if not self.settings.fetch_tmdb_artwork or not self.tmdb.configured:
            return 0
        placed = 0
        seen: set[Path] = set()
        for a in assignments:
            if a is None:
                continue
            folder = self._poster_folder(a, staging_root)
            if folder in seen:
                continue
            seen.add(folder)
            try:
                if a.kind is MediaKind.TV:
                    poster_url, fanart_url = await self.tmdb.tv_artwork(a.show_or_movie_title, a.year)
                else:
                    poster_url, fanart_url = await self.tmdb.movie_artwork(a.show_or_movie_title, a.year)
                wrote = []
                if poster_url and await self.tmdb.save_image(poster_url, folder / "poster.jpg"):
                    self._drop_stale_art(folder, "poster")  # remove wrong-ext leftovers
                    wrote.append("poster.jpg")
                if fanart_url and await self.tmdb.save_image(fanart_url, folder / "fanart.jpg"):
                    self._drop_stale_art(folder, "fanart")
                    wrote.append("fanart.jpg")
                if wrote:
                    placed += 1
                    report.on_event(f"TMDB artwork → {folder.name}/{', '.join(wrote)}")
            except Exception as exc:
                report.on_event(f"TMDB artwork skipped ({type(exc).__name__}: {exc}).")
        return placed

    @staticmethod
    def _drop_stale_art(folder: Path, base: str) -> None:
        """Remove same-name art in other extensions so the fresh .jpg is the only
        one Plex / the UI can pick up (we always write .jpg)."""
        for ext in (".jpeg", ".png"):
            stale = folder / f"{base}{ext}"
            if stale.exists():
                try:
                    stale.unlink()
                except OSError:
                    pass

    async def _apply_version_suffix(self, a: EpisodeAssignment, cp: dict,
                                    ripped_mkv: Path, report) -> None:
        """Label this movie with its resolution when the library already holds a
        copy at a DIFFERENT one.

        Two rips of one film are not duplicates when they are 1080p and 4K —
        they are the two versions Plex expects to find in a single movie folder,
        told apart by a " - <version>" suffix. Without it the second rip lands on
        the first one's name and one of them is simply lost.

        Decided once and stored on the assignment: _video_path_for feeds the
        resume check and the subtitle sidecars, so a name that moved later would
        strand the sidecars and re-place a file that was already placed."""
        if a.kind is not MediaKind.MOVIE or a.version:
            return                       # TV, or already decided on an earlier run
        if cp.get("placed"):
            return                       # our own earlier output — don't rename it
        container = self.settings.encode.container
        plain = naming.movie_video_path(
            self.settings.storage.movies_root, a.show_or_movie_title, a.year,
            container, a.edition)
        try:
            existing = [p for p in plain.parent.glob(f"*.{container}") if p != plain]
            if plain.exists():
                existing.append(plain)
        except OSError:
            return                       # share down: "can't look" is not "no versions"
        if not existing:
            return                       # first copy of this film keeps the plain name
        mine = naming.resolution_label(await self._probe_height(ripped_mkv, report) or 0)
        if not mine:
            return                       # unknown height: no label beats a wrong one
        for other in existing:
            theirs = naming.resolution_label(await self._probe_height(other, report) or 0)
            if theirs and theirs != mine:
                a.version = mine
                report.on_event(
                    f"The library already has a {theirs} copy of this film, so this "
                    f"one is filed as \"{plain.stem} - {mine}\" — Plex shows them as "
                    f"two versions of one movie instead of one overwriting the other.")
                return

    def _video_path_for(self, a: EpisodeAssignment) -> Path:
        container = self.settings.encode.container
        storage = self.settings.storage
        if a.kind is MediaKind.MOVIE:
            return naming.movie_video_path(
                storage.movies_root, a.show_or_movie_title, a.year, container,
                a.edition, a.version)
        if a.season is None or a.episode is None:
            raise ValueError(f"TV assignment for title {a.title_id} missing season/episode.")
        return naming.episode_video_path(
            storage.tv_root, a.show_or_movie_title, a.year, a.season, a.episode, container
        )


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in text)[:40] or "job"


def _path_key(path: Path) -> str:
    """One bounded, filesystem-safe directory name PER destination file.

    _slug keeps the first 40 characters, and every library path starts with the
    same long root, so '/mnt/TrueNas/media/Plex/Media/Library/Movies/...'
    slugged identically for every film on the NAS. The per-item subtitle cache
    keyed on that handed one film's .sup tracks to another while announcing
    "Reusing N cached subtitle track(s)" — wrong subtitles, reported as success.
    The tail is the part that differs (title, year, episode), and the digest of
    the whole path is what makes it collision-proof."""
    text = str(path)
    digest = hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()[:10]
    tail = "".join(c if c.isalnum() else "_" for c in text)[-29:]
    return f"{tail}_{digest}"


def _failure_hint(exc: Exception) -> str | None:
    """A plain-language hint for common environmental failures, so a cryptic
    OSError in the log points at the actual fix."""
    # WinError 1455 = ERROR_COMMITMENT_LIMIT (the system can't commit more memory).
    if getattr(exc, "winerror", None) == 1455 or isinstance(exc, MemoryError):
        return ("out of virtual memory: increase the Windows page file (System "
                "Properties > Advanced > Performance > Virtual Memory) or give the VM "
                "more RAM. Lowering [queue] encode_capacity / [subtitles] "
                "ocr_concurrency also reduces peak memory.")
    return None


def _assigned_titles(job: "Job") -> Iterator[tuple[int, EpisodeAssignment]]:
    """The selected titles that have a naming assignment, in selection order.

    A title without one can't be named or placed, so every stage skips it. Note
    the two loops that WARN about a missing assignment don't use this — they
    deliberately tell the user rather than skipping quietly."""
    for title_id in job.selected_title_ids:
        assignment = job.assignments.get(title_id)
        if assignment is not None:
            yield title_id, assignment


def _checkpoint(report) -> None:
    """Ask the reporter to persist the job, if it supports durable checkpoints."""
    fn = getattr(report, "checkpoint", None)
    if fn is not None:
        fn()


def _backup_title_mismatch(disc, copied) -> str | None:
    """Why a disc copy cannot stand in for the disc, or None if it can.

    Titles are ripped BY INDEX: `rip_title(source, 7, …)` asks for whatever the
    source calls title 7. So the copy's numbering has to be the disc's numbering,
    or every episode lands under the wrong number — the failure that is hardest
    to notice, because each file is a real episode and only the labels are wrong.

    Three checks, strongest first. Blu-ray titles carry a source filename
    ("00800.mpls") which identifies them exactly; DVDs carry none, so for those
    this comes down to the count and the per-index durations, which is weaker —
    and is why a disc copy is not trusted on its own (see the subtitle check).
    """
    want, got = disc.titles, copied.titles
    if len(want) != len(got):
        return f"the copy has {len(got)} titles, the disc had {len(want)}"
    for a, b in zip(want, got):
        if a.source_filename and b.source_filename and a.source_filename != b.source_filename:
            return (f"title {a.title_id} is {b.source_filename} in the copy but "
                    f"{a.source_filename} on the disc")
        want_s, got_s = a.duration_seconds or 0, b.duration_seconds or 0
        if want_s and got_s and abs(want_s - got_s) > _BACKUP_DURATION_SLACK:
            return (f"title {a.title_id} runs {got_s}s in the copy but {want_s}s "
                    f"on the disc")
    return None


def _emit_ejected(report, device: str | None) -> None:
    """Tell the reporter a disc was ejected, if it cares. The web _Reporter forwards
    it so the manager drops the drive's cached disc name; CLI/worker reporters don't
    implement it."""
    fn = getattr(report, "ejected", None)
    if fn is not None:
        fn(device)


def _emit_notify(report, title: str, message: str) -> None:
    """Push a user notification, if the reporter supports it. The web _Reporter
    does (it forwards to the notify service); the CLI/worker reporters don't."""
    fn = getattr(report, "notify", None)
    if fn is not None:
        fn(title, message)


def _sub_checkpoint(root: Path, sup: Path, track: SubtitleTrack) -> dict:
    """Serialize an extracted subtitle so resume reuse works on ANY machine:
    the .sup path is stored relative to the staging root, OS-neutral."""
    return {
        "sup": _rel_path(root, sup), "track_id": track.track_id, "codec": track.codec,
        "language": track.language, "forced": track.forced, "sdh": track.sdh,
        "name": track.name,
    }


def _is_abs_stored(rel: str) -> bool:
    # POSIX absolute ("/x") or Windows drive ("C:\..").
    return rel.startswith("/") or (len(rel) >= 2 and rel[1] == ":")


def _rel_path(root: Path, p: Path) -> str:
    """Path relative to ``root`` as a POSIX string (portable across machines /
    OSes). Falls back to the absolute string if it isn't under root."""
    try:
        return PurePosixPath(Path(p).relative_to(root)).as_posix()
    except ValueError:
        return str(p)


def _resolve_path(root: Path, rel: str) -> Path:
    """Resolve a stored (relative or absolute) path against this machine's root.

    A relative path floats by construction — it's re-anchored on whatever root
    THIS box uses. An absolute one was stored outside the known roots (a job added
    from a file elsewhere), so it only works on the machine that wrote it; the
    configured prefix map is what carries those across. Absent a rule, or on the
    machine where the path is still valid, it's returned untouched."""
    if _is_abs_stored(rel):
        return pathmap.resolve(rel, _PATH_RULES)
    return Path(root).joinpath(*PurePosixPath(rel).parts)


# Prefix-rewrite rules, installed once from Settings (see Pipeline.__init__).
# Module-level so the 11 call sites that funnel through _resolve_path don't each
# have to thread configuration down to it.
_PATH_RULES: list = []


def configure_path_map(entries) -> None:
    global _PATH_RULES
    _PATH_RULES = pathmap.parse_rules(entries)


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
