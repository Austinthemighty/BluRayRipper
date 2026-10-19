"""Something went wrong overnight: fix it and pick up where it left off.

The whole point of the checkpoints is that an operator who comes down in the
morning to a red card never has to feed the disc back in. A retry that re-rips
costs an hour of drive time and needs the disc, which by then is back in its
sleeve (or the box is a headless worker with no drive at all). So every failure
has to leave the finished work behind it intact, and every resume has to reuse
it.

The other half is the opposite failure: a job that reports Done when it isn't.
Job 42 placed a video whose subtitle OCR had died, and the card went green --
which is why there is an output check at the very end that looks at the disk
instead of trusting the stages.
"""

import asyncio
import copy
from pathlib import Path

from blurayripper.config import load_settings
from blurayripper.models import (
    DiscInfo, EpisodeAssignment, MediaKind, Stream, StreamType, Title,
)
from blurayripper.pipeline import Job, JobState, Pipeline
from blurayripper.scheduler import EncodeScheduler
from blurayripper.tools.mkvtools import ProbedFile, SubtitleTrack
from blurayripper.web.manager import JobManager

TITLE_SECONDS = 6000
JOB_NAME = "job_7"


class Report:
    """Stands in for the web reporter: keeps the log lines the UI would show and
    snapshots the job's checkpoints every time the pipeline asks for a durable
    save (that is what a crash right at that instant would have preserved)."""

    def __init__(self, job=None):
        self.events: list[str] = []
        self.snapshots: list[dict] = []
        self._job = job

    def on_event(self, message):
        self.events.append(message)

    def on_progress(self, stage, fraction):
        pass

    def checkpoint(self):
        self.snapshots.append(
            copy.deepcopy(self._job.checkpoints) if self._job is not None else {})

    @property
    def log(self) -> str:
        return "\n".join(self.events)


class Tools:
    """Every external binary the pipeline shells out to, replaced by a counter
    and a switch. Nothing here touches a disc, a subprocess or the network."""

    def __init__(self):
        self.counts = {"rip": 0, "identify": 0, "extract": 0, "ocr": 0, "encode": 0}
        self.drive_error: str | None = None
        self.identify_error: str | None = None
        self.ocr_error: str | None = None
        self.encode_error: str | None = None
        self.rip_dir_at_call: list[str] = []

    def install(self, pipeline: Pipeline) -> None:
        async def rip_title(source, title_id, dest_dir, *, on_progress=None, on_message=None):
            self.counts["rip"] += 1
            self.rip_dir_at_call = sorted(p.name for p in Path(dest_dir).glob("*.mkv"))
            if self.drive_error:
                raise RuntimeError(self.drive_error)
            dest_dir.mkdir(parents=True, exist_ok=True)
            out = dest_dir / f"title_t{title_id:02d}.mkv"
            out.write_text("a whole Blu-ray, honest")
            return [out]

        async def list_drives(timeout=None):
            return []

        async def probe(path):
            return ProbedFile(duration_seconds=TITLE_SECONDS, video_height=1080,
                              audio_languages=["eng"], subtitles=[])

        async def video_height(path):
            return 1080

        async def identify_subtitles(path):
            self.counts["identify"] += 1
            if self.identify_error:
                raise RuntimeError(self.identify_error)
            return [SubtitleTrack(track_id=2, codec="HDMV PGS", language="eng", is_pgs=True)]

        async def extract_tracks(src, pairs, on_progress=None):
            self.counts["extract"] += 1
            for _track_id, dest in pairs:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text("PGS bitmaps")

        async def ocr_to_srt(sup, language, out_dir):
            self.counts["ocr"] += 1
            if self.ocr_error:
                raise RuntimeError(self.ocr_error)
            out_dir.mkdir(parents=True, exist_ok=True)
            srt = out_dir / (Path(sup).stem + ".srt")
            srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nShall we play a game?\n")
            return srt

        async def encode(src, dst, *, on_progress=None, tune_grain=None, deinterlace=False):
            self.counts["encode"] += 1
            if self.encode_error:
                raise RuntimeError(self.encode_error)
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            Path(dst).write_text("x265 bytes")
            return dst

        pipeline.makemkv.rip_title = rip_title
        pipeline.makemkv.list_drives = list_drives
        pipeline.mkvtools.probe = probe
        pipeline.mkvtools.video_height = video_height
        pipeline.mkvtools.identify_subtitles = identify_subtitles
        pipeline.mkvtools.extract_tracks = extract_tracks
        pipeline.subtitleedit.ocr_to_srt = ocr_to_srt
        pipeline.handbrake.encode = encode


def _pipeline(tmp_path: Path) -> Pipeline:
    settings = load_settings()
    settings.storage.output_dir = tmp_path / "library"
    settings.storage.staging_dir = tmp_path / "staging"
    settings.languages.keep = ["eng"]
    settings.auto_eject = False          # no drive to talk to in a test
    settings.extract_cover_art = False   # disc art / TMDB are separate stories
    settings.fetch_tmdb_artwork = False
    settings.write_nfo = False
    settings.subtitles.upload_export_dir = None
    return Pipeline(settings)


def _job() -> Job:
    """A one-title movie disc that carries an English subtitle track. The track
    matters: it is what tells the end-of-job check a sidecar was owed."""
    job = Job(source="disc:0")
    job.disc = DiscInfo(source="disc:0", disc_name="WarGames", titles=[
        Title(title_id=0, duration_seconds=TITLE_SECONDS, size_bytes=25_000_000_000,
              streams=[Stream(stream_id=1, stream_type=StreamType.SUBTITLE,
                              language="eng")])])
    job.selected_title_ids = [0]
    job.assignments = {0: EpisodeAssignment(0, MediaKind.MOVIE, "WarGames", 1983)}
    return job


def _click_retry(job: Job) -> None:
    """What JobManager.retry does to the pipeline job: clear the failure and put
    it back to READY. Note what it does NOT do -- touch the checkpoints."""
    assert job.state is JobState.FAILED
    job.error = None
    job.state = JobState.READY


def _library_files(pipeline: Pipeline) -> list[str]:
    root = pipeline.settings.storage.movies_root
    if not root.exists():
        return []
    return sorted(p.name for p in root.rglob("*") if p.is_file())


def _staged_files(pipeline: Pipeline) -> list[str]:
    """What a title has ASSEMBLED but not yet published to the library."""
    root = pipeline.settings.storage.staging_dir / JOB_NAME / "library"
    if not root.exists():
        return []
    return sorted(p.name for p in root.rglob("*") if p.is_file())


async def _settle() -> None:
    """Let the manager's background tasks (scan, run, retry) get to their end."""
    for _ in range(40):
        await asyncio.sleep(0)


async def _run(pipeline: Pipeline, job: Job, report: Report):
    """Run the job the way the single-box path does, returning the exception it
    failed with (or None)."""
    try:
        await pipeline.execute(job, EncodeScheduler(3), report, job_name=JOB_NAME)
    except Exception as exc:
        return exc
    return None


async def test_a_night_of_failures_resumes_instead_of_starting_over(tmp_path):
    """The full overnight story on one job: the drive drops off the bus, the
    retry rips, the encode is killed, and the morning resume finishes it.

    Each chapter is only worth anything because of the next one -- a failure
    that left nothing behind, or a resume that redid the rip, would cost the
    operator the disc and the hour it takes to read it.
    """
    pipeline = _pipeline(tmp_path)
    tools = Tools()
    tools.install(pipeline)
    job = _job()
    staging = pipeline.settings.storage.staging_dir

    # --- Chapter 1: the drive dropped off the bus mid-rip. ------------------
    tools.drive_error = "makemkvcon: Failed to open disc — device no longer present"
    report = Report(job)
    failure = await _run(pipeline, job, report)

    assert isinstance(failure, RuntimeError)
    assert job.state is JobState.FAILED
    # The reason has to name the drive, not just "RuntimeError" — this is the
    # whole text the card shows at 7am.
    assert job.error == (
        "RuntimeError: makemkvcon: Failed to open disc — device no longer present")
    # ...and the log has to name the stage it died in, captured before the state
    # was overwritten with FAILED (otherwise every failure reads "during failed").
    assert "FAILED during ripping:" in report.log
    # Nothing half-placed: no video, no sidecar, no "this title is done" flag.
    assert _library_files(pipeline) == []
    assert job.results == []
    assert "ripped_path" not in job.checkpoints.get(0, {})
    assert "placed" not in job.checkpoints.get(0, {})
    assert tools.counts["encode"] == 0

    # --- Chapter 2: retry. The rip lands and is checkpointed; the encode dies.
    _click_retry(job)
    tools.drive_error = None
    tools.encode_error = "HandBrake was killed (the box rebooted for updates)"
    report = Report(job)
    failure = await _run(pipeline, job, report)

    assert isinstance(failure, RuntimeError)
    assert job.state is JobState.FAILED
    assert "HandBrake was killed" in (job.error or "")
    cp = job.checkpoints[0]
    # The rip is recorded RELATIVE to the staging root, so the record still means
    # something on a worker that mounts staging at a different path.
    assert not Path(cp["ripped_path"]).is_absolute()
    assert (staging / cp["ripped_path"]).exists()
    # It was persisted the moment the rip finished — before any subtitle work —
    # so a crash during OCR still knows the disc has been read.
    assert any("ripped_path" in snap.get(0, {})
               and "subs" not in snap.get(0, {})
               and "subs_done" not in snap.get(0, {})
               for snap in report.snapshots)
    # The subtitles got all the way to a finished sidecar and were checkpointed,
    # again relative — but the title is NOT marked placed, because the video
    # never made it into the library. The sidecar is therefore still in staging:
    # the library shows nothing rather than a movie folder with no film in it.
    assert _library_files(pipeline) == []
    assert _staged_files(pipeline) == ["WarGames (1983).en.default.srt"]
    assert len(cp["subs_done"]) == 1
    assert not Path(cp["subs_done"][0]).is_absolute()
    assert not cp.get("placed")
    assert tools.counts == {"rip": 2, "identify": 1, "extract": 1, "ocr": 1, "encode": 1}

    # --- Chapter 3: morning resume. The disc is back in its sleeve. ---------
    _click_retry(job)
    tools.encode_error = None
    tools.drive_error = "no disc in the drive"  # touching the drive now = failure
    report = Report(job)
    failure = await _run(pipeline, job, report)

    assert failure is None
    assert job.state is JobState.DONE
    # Only the encode re-ran. The rip was reused (no disc needed) and the
    # subtitle work — extract AND OCR, the expensive half — was skipped whole.
    assert tools.counts == {"rip": 2, "identify": 1, "extract": 1, "ocr": 1, "encode": 2}
    assert "Resuming title 0: reusing ripped file." in report.events
    assert "Subtitles already done (1 sidecar(s)) — skipping OCR." in report.events
    # The skipped sidecar is still carried onto the result, so the end-of-job
    # check sees a job with subtitles rather than one that produced none.
    assert [p.name for p in job.results[0].subtitle_paths] == [
        "WarGames (1983).en.default.srt"]
    assert pipeline._verify_outputs(job) == []
    assert _library_files(pipeline) == [
        "WarGames (1983).en.default.srt", "WarGames (1983).mkv"]
    # Staging is cleaned out only once the job really finished.
    assert not (staging / job.checkpoints[0]["ripped_path"]).exists()


async def test_a_video_placed_without_its_subtitles_does_not_go_green(tmp_path):
    """Job 42: OCR died, the encode carried on, the video landed in the library
    and the card went green. Every stage reported its own success, so only a look
    at the finished folder can tell the difference between done and done-ish."""
    broken = _pipeline(tmp_path / "broken")
    broken_tools = Tools()
    broken_tools.install(broken)
    broken_tools.ocr_error = "SubtitleEdit crashed (OCR engine missing)"
    broken_job = _job()

    failure = await _run(broken, broken_job, Report(broken_job))

    # The trap: the video really is there, and every stage said it worked.
    assert _library_files(broken) == ["WarGames (1983).mkv"]
    assert broken_tools.counts["encode"] == 1
    # It is still not done.
    assert isinstance(failure, RuntimeError)
    assert broken_job.state is JobState.FAILED
    assert "the disc carries eng subtitle track(s) but no .srt was produced" in str(failure)
    assert "The rip and encode are kept" in str(failure)

    # Vary exactly one thing — the OCR binary works — and the same job finishes.
    working = _pipeline(tmp_path / "working")
    working_tools = Tools()
    working_tools.install(working)
    working_job = _job()

    assert await _run(working, working_job, Report(working_job)) is None
    assert working_job.state is JobState.DONE
    assert _library_files(working) == [
        "WarGames (1983).en.default.srt", "WarGames (1983).mkv"]


async def test_the_dead_ocr_is_redone_from_the_staged_subs_without_the_disc(tmp_path):
    """The fix for job 42 must not need the disc. The extracted PGS images were
    checkpointed before OCR ran, so re-OCR reads those — no re-rip, and not even
    a re-extract from the (multi-GB, NAS-bound) ripped file."""
    pipeline = _pipeline(tmp_path)
    tools = Tools()
    tools.install(pipeline)
    tools.ocr_error = "SubtitleEdit crashed (OCR engine missing)"
    job = _job()
    await _run(pipeline, job, Report(job))
    assert _library_files(pipeline) == ["WarGames (1983).mkv"]  # video only
    before = dict(tools.counts)

    # OCR is fixed; the disc is gone and the encoder would fail if touched.
    tools.ocr_error = None
    tools.drive_error = "no disc in the drive"
    tools.encode_error = "the encoder must not run for a subtitle redo"
    report = Report(job)
    placed = await pipeline.redo_subtitles(
        job, pipeline.settings.storage.staging_dir / JOB_NAME, report)

    assert [p.name for p in placed] == ["WarGames (1983).en.default.srt"]
    assert _library_files(pipeline) == [
        "WarGames (1983).en.default.srt", "WarGames (1983).mkv"]
    assert tools.counts["rip"] == before["rip"]            # no disc was needed
    assert tools.counts["identify"] == before["identify"]  # the .sup was reused
    assert tools.counts["extract"] == before["extract"]
    assert tools.counts["encode"] == before["encode"]      # nothing re-encoded
    assert tools.counts["ocr"] == before["ocr"] + 1        # only the missing work


async def test_a_half_written_mkv_from_the_killed_rip_is_cleared_before_re_ripping(tmp_path):
    """A rip that died left a partial .mkv with the name MakeMKV will pick again.
    Left in place, makemkvcon takes its "already exists" path and produces no
    NEWLY-appeared file, so the retry reports "produced no output" forever."""
    pipeline = _pipeline(tmp_path)
    tools = Tools()
    tools.install(pipeline)
    job = _job()
    rip_dir = pipeline.settings.storage.staging_dir / JOB_NAME / "rip" / "t000"
    rip_dir.mkdir(parents=True)
    (rip_dir / "title_t00.mkv").write_text("half a rip")  # killed before checkpointing

    assert await _run(pipeline, job, Report(job)) is None
    # MakeMKV was invoked, and the leftover was already gone when it was.
    assert tools.counts["rip"] == 1
    assert tools.rip_dir_at_call == []
    assert job.state is JobState.DONE


async def test_a_source_file_that_vanished_says_so_instead_of_blaming_makemkv(tmp_path):
    """A job added from an existing .mkv has no disc behind it. When the file is
    unreachable (a worker that lost the share), the operator must be told to put
    the file back — not handed a MakeMKV error for a rip that can never happen."""
    pipeline = _pipeline(tmp_path)
    tools = Tools()
    tools.install(pipeline)
    gone = tmp_path / "share" / "WarGames.mkv"
    job = _job()
    job.source = str(gone)
    job.checkpoints = {0: {"ripped_path": str(gone)}}

    failure = await _run(pipeline, job, Report(job))

    assert isinstance(failure, RuntimeError)
    assert "The source file is gone" in str(failure)
    assert "no disc to re-rip it from" in str(failure)
    assert tools.counts["rip"] == 0  # makemkvcon can't parse a bare path anyway


async def test_retry_hands_the_job_back_to_the_pipeline_with_its_checkpoints(tmp_path,
                                                                             monkeypatch):
    """The Retry button in the UI. It re-queues the SAME pipeline job, so the
    finished rip recorded by the failed run is still there for the second run to
    reuse. A retry that started from a clean job would demand the disc again."""
    settings = load_settings()
    settings.storage.output_dir = tmp_path / "library"
    settings.storage.staging_dir = tmp_path / "staging"
    manager = JobManager(settings)

    async def fake_scan(source, report):
        job = Job(source=source)
        job.disc = DiscInfo(source=source, disc_name="WarGames", titles=[
            Title(title_id=t, duration_seconds=2700, chapter_count=12,
                  source_filename=f"0000{t}.mpls") for t in range(3)])
        job.fingerprint = "fp_story"
        return job

    seen: list[dict] = []

    async def fake_execute(job, scheduler, report, job_name=None):
        seen.append(copy.deepcopy(job.checkpoints))
        if len(seen) == 1:
            job.checkpoints[0] = {"ripped_path": "job_1/rip/t000/title_t00.mkv"}
            raise RuntimeError("HandBrake was killed (the box rebooted)")
        job.checkpoints[0]["placed"] = True
        return job

    monkeypatch.setattr(manager.pipeline, "scan_disc", fake_scan)
    monkeypatch.setattr(manager.pipeline, "execute", fake_execute)

    wj = manager.add_disc("disc:0")
    await _settle()
    manager.set_titles(wj.id, [0])
    manager.set_mapping(wj.id, [EpisodeAssignment(0, MediaKind.MOVIE, "WarGames", 1983)])
    await _settle()
    assert manager.snapshot()["jobs"][0]["state"] == "failed"

    manager.retry(wj.id)
    await _settle()

    assert manager.snapshot()["jobs"][0]["state"] == "done"
    assert seen[0] == {}  # the first run started from nothing
    # The retry got the rip the failed run left behind, not a blank slate.
    assert seen[1] == {0: {"ripped_path": "job_1/rip/t000/title_t00.mkv"}}
