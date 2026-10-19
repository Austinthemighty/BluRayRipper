"""Chaos: the storage underneath vanishes mid-operation.

Staging and the Plex library are both CIFS shares on a NAS, and CIFS drops.
Every scenario below is one of those drops, injected at the phase where it
actually bit this cluster: the share going away mid-rip, MakeMKV's EAGAIN write
error (jobs 334/335 — the log lines here are verbatim from that incident), the
encode disk filling up, the output share coming back read-only, a file
disappearing between being written and being verified, and the zero-byte stub a
killed tool leaves behind.

What is asserted is never "it raised". A storage drop is survivable; what makes
it survivable is the state it leaves behind. So each test asks the four
questions the operator asks the next morning:

  * is the job still recoverable, or did the failure destroy the evidence?
  * did a bad file get into the library, or into the checkpoints where a later
    run would trust it?
  * was the disc released — and, more importantly, was the operator only TOLD
    it was released when it really was?
  * does the retry redo the lost work and nothing else?
"""

import copy
import errno
import os
import shutil
from pathlib import Path

from blurayripper.config import load_settings
from blurayripper.models import (
    DiscInfo, EpisodeAssignment, MediaKind, Stream, StreamType, Title,
)
from blurayripper.persistence import JobRecord, MemoryStore, assignment_to_dict
from blurayripper.pipeline import Job, JobState, Pipeline
from blurayripper.scheduler import EncodeScheduler
from blurayripper.services import library
from blurayripper.tools.mkvtools import ProbedFile, SubtitleTrack
from blurayripper.worker import CLAIMABLE_STATES, WorkerRunner

TITLE_SECONDS = 6000
JOB_NAME = "job_334"
DEVICE = "/dev/sr0"
VIDEO_NAME = "WarGames (1983).mkv"
SRT_NAME = "WarGames (1983).en.default.srt"

# MakeMKV reuses the same output name for a title, which is what makes a
# leftover file from a killed attempt dangerous: the second attempt writes to
# the name that is already there.
RIP_NAME = "WarGames_t{:02d}.mkv"

# Verbatim from the production log (jobs 334/335): the NAS write died mid-rip
# and both jobs were still queued as ready for an encode worker.
EAGAIN_LOG = [
    "Program reads data faster than it can write to disk, consider upgrading "
    "your hard drive if you see many of these messages.",
    "Error 'Posix error - Resource temporarily unavailable' occurred while "
    "writing data to '{path}'",
    "Failed to save title 0 to file {path}",
    "0 titles saved, 1 failed",
    "Copy complete. 0 titles saved, 1 failed.",
]


# --- reporter / tools -------------------------------------------------------

class Report:
    """The web reporter's surface: log lines, durable checkpoint saves, push
    notifications, and the eject callback. ``on_event_hook`` is how a test
    injects a share drop at an exact instant mid-pipeline."""

    def __init__(self, job=None):
        self.events: list[str] = []
        self.snapshots: list[dict] = []
        self.notifications: list[tuple[str, str]] = []
        self.ejected_devices: list[str | None] = []
        self.on_event_hook = None
        self._job = job

    def on_event(self, message):
        self.events.append(message)
        if self.on_event_hook is not None:
            self.on_event_hook(message)

    def on_progress(self, stage, fraction):
        pass

    def checkpoint(self):
        self.snapshots.append(
            copy.deepcopy(self._job.checkpoints) if self._job is not None else {})

    def notify(self, title, message):
        self.notifications.append((title, message))

    def ejected(self, device):
        self.ejected_devices.append(device)

    @property
    def log(self) -> str:
        return "\n".join(self.events)


class Tools:
    """Stand-ins for makemkvcon / mkvmerge / mkvextract / SubtitleEdit /
    HandBrake. No subprocesses, no disc, no network.

    Two details are copied from the real tools on purpose, because the code
    under test only means anything against them:

    * the rip returns the .mkv files that APPEARED during the call (makemkvcon
      is told a directory, not a filename), so a leftover file with the same
      name reads as "produced no output";
    * probing a zero-byte file fails, the way mkvmerge fails on one.
    """

    def __init__(self):
        self.counts = {"rip": 0, "identify": 0, "extract": 0, "ocr": 0, "encode": 0}
        self.rip_fault = None      # callable(dest_dir, on_message)
        self.encode_fault = None   # callable(dst)
        self.rip_dir_seen: list[list[str]] = []

    def install(self, pipeline: Pipeline) -> None:
        async def rip_title(source, title_id, dest_dir, *, on_progress=None, on_message=None):
            self.counts["rip"] += 1
            dest_dir = Path(dest_dir)
            dest_dir.mkdir(parents=True, exist_ok=True)
            self.rip_dir_seen.append(sorted(p.name for p in dest_dir.glob("*.mkv")))
            before = set(dest_dir.glob("*.mkv"))
            if self.rip_fault is not None:
                self.rip_fault(dest_dir, on_message)
            else:
                (dest_dir / RIP_NAME.format(title_id)).write_text("a whole Blu-ray, honest")
                on_message("Operation successfully completed")
                on_message("1 titles saved")
            return sorted(set(dest_dir.glob("*.mkv")) - before)

        async def list_drives(timeout=None):
            return []

        async def probe(path):
            p = Path(path)
            if not p.exists() or p.stat().st_size == 0:
                raise RuntimeError(f"mkvmerge: '{p}' could not be opened or parsed")
            return ProbedFile(duration_seconds=TITLE_SECONDS, video_height=1080,
                              audio_languages=["eng"], subtitles=[])

        async def video_height(path):
            return 1080

        async def identify_subtitles(path):
            self.counts["identify"] += 1
            return [SubtitleTrack(track_id=2, codec="HDMV PGS", language="eng", is_pgs=True)]

        async def extract_tracks(src, pairs, on_progress=None):
            self.counts["extract"] += 1
            for _track_id, dest in pairs:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text("PGS bitmaps")

        async def ocr_to_srt(sup, language, out_dir):
            self.counts["ocr"] += 1
            out_dir.mkdir(parents=True, exist_ok=True)
            srt = out_dir / (Path(sup).stem + ".srt")
            srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nShall we play a game?\n")
            return srt

        async def encode(src, dst, *, on_progress=None, tune_grain=None, deinterlace=False):
            self.counts["encode"] += 1
            dst = Path(dst)
            if self.encode_fault is not None:
                self.encode_fault(dst)   # writes its own output, and may raise
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text("x265 bytes")
            return dst

        pipeline.makemkv.rip_title = rip_title
        pipeline.makemkv.list_drives = list_drives
        pipeline.mkvtools.probe = probe
        pipeline.mkvtools.video_height = video_height
        pipeline.mkvtools.identify_subtitles = identify_subtitles
        pipeline.mkvtools.extract_tracks = extract_tracks
        pipeline.subtitleedit.ocr_to_srt = ocr_to_srt
        pipeline.handbrake.encode = encode


class _SharedStore(MemoryStore):
    """The cluster's Postgres, in memory: durable, so the paths that only run
    against a shared store run here too."""

    @property
    def durable(self) -> bool:
        return True


# --- harness ----------------------------------------------------------------

def _settings(tmp_path: Path, *, auto_eject: bool = False):
    s = load_settings()
    s.storage.output_dir = tmp_path / "library"
    s.storage.staging_dir = tmp_path / "staging"
    s.languages.keep = ["eng"]
    s.auto_eject = auto_eject
    s.extract_cover_art = False    # disc art / TMDB are other stories
    s.fetch_tmdb_artwork = False
    s.write_nfo = False
    s.subtitles.upload_export_dir = None
    # load_settings() reads the repo's config/ — including the gitignored
    # settings.local.json and BLURAYRIPPER_* env overrides a developer may have
    # set. Pin the two knobs these scenarios hinge on so they mean the same
    # thing on every box:
    #   * a zero tolerance turns off the read-back check that catches a
    #     truncated/empty rip, so the zero-byte scenario would stop testing it;
    #   * worker_does_subs moves subtitle work onto the encode worker, which
    #     changes what the worker scenario is allowed to touch.
    s.rip_duration_tolerance_pct = 3
    s.cluster.worker_does_subs = False
    return s


def _pipeline(tmp_path: Path, *, auto_eject: bool = False) -> Pipeline:
    return Pipeline(_settings(tmp_path, auto_eject=auto_eject))


def _job() -> Job:
    """A one-title movie disc carrying an English subtitle track. The track is
    what tells the end-of-job check that a sidecar was owed."""
    job = Job(source="disc:0")
    job.disc = DiscInfo(
        source="disc:0", disc_name="WarGames", volume_label="WARGAMES",
        drive_device=DEVICE,
        titles=[Title(title_id=0, duration_seconds=TITLE_SECONDS,
                      size_bytes=25_000_000_000,
                      streams=[Stream(stream_id=1, stream_type=StreamType.SUBTITLE,
                                      language="eng")])])
    job.selected_title_ids = [0]
    job.assignments = {0: EpisodeAssignment(0, MediaKind.MOVIE, "WarGames", 1983)}
    return job


async def _run(pipeline: Pipeline, job: Job, report: Report):
    """Run the job the single-box path runs it, returning the failure (or None)."""
    try:
        await pipeline.execute(job, EncodeScheduler(3), report, job_name=JOB_NAME)
    except Exception as exc:
        return exc
    return None


def _click_retry(job: Job) -> None:
    """Exactly what JobManager.retry does to the pipeline job — note what it
    does NOT do: touch the checkpoints."""
    assert job.state is JobState.FAILED
    job.error = None
    job.state = JobState.READY


def _library_files(pipeline: Pipeline) -> list[str]:
    root = pipeline.settings.storage.movies_root
    if not root.exists():
        return []
    return sorted(p.name for p in root.rglob("*") if p.is_file())


def _movie_dir(pipeline: Pipeline) -> Path:
    return pipeline.settings.storage.movies_root / "WarGames (1983)"


def _staged_movie_dir(pipeline: Pipeline) -> Path:
    """Where a title is ASSEMBLED before it is published to the library.

    Sidecars and artwork wait here until the video is ready, so the library
    never shows a movie folder with no film in it."""
    rel = _movie_dir(pipeline).relative_to(pipeline.settings.storage.output_dir)
    return pipeline.settings.storage.staging_dir / JOB_NAME / "library" / rel


def _watch_eject(monkeypatch) -> list:
    calls = []

    def fake_eject(device, method="auto"):
        calls.append(device)

    monkeypatch.setattr("blurayripper.pipeline.eject", fake_eject)
    return calls


def _the_nas_write_dies(dest_dir: Path, on_message) -> None:
    """MakeMKV's EAGAIN: a progressively-written fragment is left on disk and the
    tool reports the failure in its own words rather than by exiting non-zero."""
    partial = dest_dir / RIP_NAME.format(0)
    partial.write_bytes(b"the first 3GB")
    for line in EAGAIN_LOG:
        on_message(line.format(path=partial))


def _enospc(dst: Path) -> None:
    """HandBrake wrote 40 minutes of video and then the volume filled."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("the first 40 minutes of x265")
    raise OSError(errno.ENOSPC, "No space left on device", str(dst))


def _deny_writes(request, monkeypatch, folder: Path):
    """Make new files in ``folder`` fail, the way an output share that came back
    read-only does. Returns an idempotent callable that puts it back.

    The chmod is registered as a teardown finalizer as well as being returned:
    an assertion that fires before the caller restores it would otherwise leave
    a 0o500 directory behind for pytest's tmp_path reaper to trip over.
    """
    folder.mkdir(parents=True, exist_ok=True)
    mode = folder.stat().st_mode
    folder.chmod(0o500)
    request.addfinalizer(lambda: folder.chmod(mode))
    if not os.access(folder, os.W_OK):
        return lambda: folder.chmod(mode)
    # Running as root (CI containers usually are): the mode bits are advisory
    # there, so refuse at the call the placer actually makes instead. monkeypatch
    # undoes this at teardown even if an assertion fires first.
    real_move = shutil.move

    def denied(src, dst, *a, **k):
        if Path(dst).parent == folder:
            raise PermissionError(errno.EACCES, "Permission denied", str(dst))
        return real_move(src, dst, *a, **k)

    monkeypatch.setattr(library.shutil, "move", denied)

    def restore():
        monkeypatch.setattr(library.shutil, "move", real_move)
        folder.chmod(mode)

    return restore


# --- the staging share goes away mid-rip ------------------------------------

async def test_the_staging_share_vanishing_mid_rip_leaves_nothing_that_looks_ripped(
        tmp_path, monkeypatch):
    """The CIFS mount dropped while makemkvcon was writing into it.

    The rip is genuinely gone, so it has to happen again — but only if nothing
    is left behind that a later run would mistake for a finished rip, and only
    if the operator is never told the disc is free while it is still needed.
    """
    pipeline = _pipeline(tmp_path, auto_eject=True)
    tools = Tools()
    tools.install(pipeline)
    ejects = _watch_eject(monkeypatch)
    job = _job()
    staging = pipeline.settings.storage.staging_dir

    def the_mount_drops(dest_dir, on_message):
        on_message("Error 'Posix error - No such device' occurred while reading "
                   "'/BDMV/STREAM/00001.m2ts' at offset '0'")
        shutil.rmtree(staging)
        raise OSError(errno.ENOENT, "No such file or directory", str(dest_dir))

    tools.rip_fault = the_mount_drops

    report = Report(job)
    failure = await _run(pipeline, job, report)

    assert isinstance(failure, OSError)
    assert job.state is JobState.FAILED
    # The card has to name the stage, captured before the state became FAILED.
    assert "FAILED during ripping:" in report.log
    # Nothing recorded, nothing placed, nothing encoded.
    assert job.checkpoints.get(0, {}) == {}
    assert job.results == []
    assert _library_files(pipeline) == []
    assert tools.counts["encode"] == 0
    # And the disc was NOT released: it is still needed, and nothing claimed
    # otherwise.
    assert ejects == []
    assert report.notifications == []
    assert "safe to load the next one" not in report.log

    # The share comes back. The rip is the only thing that was destroyed, so it
    # is the only thing redone — and the job finishes from there.
    _click_retry(job)
    tools.rip_fault = None
    report = Report(job)

    assert await _run(pipeline, job, report) is None
    assert job.state is JobState.DONE
    assert tools.counts == {"rip": 2, "identify": 1, "extract": 1, "ocr": 1, "encode": 1}
    assert _library_files(pipeline) == [SRT_NAME, VIDEO_NAME]


# --- MakeMKV's EAGAIN write error (jobs 334/335) ----------------------------

async def test_the_eagain_write_error_that_queued_two_failed_rips_as_ready(
        tmp_path, monkeypatch):
    """Jobs 334 and 335. The NAS write died mid-rip, MakeMKV said "0 titles
    saved, 1 failed", and both jobs were logged as ripped and queued for an
    encode worker anyway.

    MakeMKV writes progressively, so the fragment it left behind is not "no
    output" — believing what it actually SAID is the only signal that fires
    before a worker picks the job up.
    """
    pipeline = _pipeline(tmp_path, auto_eject=True)
    tools = Tools()
    tools.install(pipeline)
    ejects = _watch_eject(monkeypatch)
    job = _job()

    tools.rip_fault = _the_nas_write_dies

    report = Report(job)
    failure = await _run(pipeline, job, report)

    assert isinstance(failure, RuntimeError)
    # The reason quotes MakeMKV's own line...
    assert "could not save title 0" in str(failure)
    assert "Failed to save title 0 to file" in str(failure)
    # ...and points at the share, not at the disc, which would be the wrong fix.
    assert "went away mid-rip rather than a bad disc" in str(failure)
    assert job.state is JobState.FAILED
    assert "FAILED during ripping:" in report.log

    # The incident itself: a fragment on disk and the job queued as ready.
    assert "ripped_path" not in job.checkpoints.get(0, {})
    assert tools.counts["encode"] == 0
    assert _library_files(pipeline) == []
    # The disc is still needed, so it is neither ejected nor announced as free.
    assert ejects == []
    assert report.ejected_devices == []
    assert report.notifications == []


async def test_a_rip_that_actually_landed_is_the_one_that_releases_the_disc(
        tmp_path, monkeypatch):
    """The control for the test above. Without it, "no eject after a failed
    rip" would also pass on a build that had stopped ejecting altogether."""
    pipeline = _pipeline(tmp_path, auto_eject=True)
    tools = Tools()
    tools.install(pipeline)
    ejects = _watch_eject(monkeypatch)
    job = _job()
    report = Report(job)

    assert await _run(pipeline, job, report) is None

    assert ejects == [DEVICE]
    assert report.ejected_devices == [DEVICE]   # so the UI drops its cached disc
    assert "Ejected disc (/dev/sr0) — safe to load the next one." in report.events
    titles = [t for t, _m in report.notifications]
    assert "💿 Finished ripping: WarGames" in titles
    assert any("safe to swap in the next one" in m for _t, m in report.notifications)


async def test_the_fragment_from_the_failed_rip_is_cleared_before_the_re_rip(
        tmp_path, monkeypatch):
    """A retry that leaves the fragment in place is worse than useless:
    makemkvcon reuses the same output name, so nothing NEW appears and the
    retry reports "produced no output" on a rip that actually worked."""
    pipeline = _pipeline(tmp_path)
    tools = Tools()
    tools.install(pipeline)
    job = _job()
    rip_dir = pipeline.settings.storage.staging_dir / JOB_NAME / "rip" / "t000"

    tools.rip_fault = _the_nas_write_dies
    assert isinstance(await _run(pipeline, job, Report(job)), RuntimeError)
    # The fragment is still sitting there under the name the next rip will use.
    assert sorted(p.name for p in rip_dir.glob("*.mkv")) == [RIP_NAME.format(0)]

    _click_retry(job)
    tools.rip_fault = None
    report = Report(job)

    assert await _run(pipeline, job, report) is None
    assert job.state is JobState.DONE
    # Both attempts started from an empty rip directory — the second one only
    # because the fragment was cleared for it.
    assert tools.rip_dir_seen == [[], []]
    assert _library_files(pipeline) == [SRT_NAME, VIDEO_NAME]


async def test_a_zero_byte_rip_left_by_a_killed_tool_is_never_checkpointed(tmp_path):
    """The box lost power with makemkvcon mid-write. The file exists and has
    the right name; it is empty, and MakeMKV — killed — never printed a failure
    line. Reading the file back is all that is left to tell the difference."""
    pipeline = _pipeline(tmp_path)
    tools = Tools()
    tools.install(pipeline)
    job = _job()

    def killed_mid_write(dest_dir, on_message):
        (dest_dir / RIP_NAME.format(0)).write_bytes(b"")

    tools.rip_fault = killed_mid_write

    failure = await _run(pipeline, job, Report(job))

    assert isinstance(failure, RuntimeError)
    assert "can't be read back" in str(failure)
    assert "a Retry re-rips" in str(failure)
    # Not kept: no checkpoint means no worker can be handed an empty file.
    assert "ripped_path" not in job.checkpoints.get(0, {})
    assert tools.counts["encode"] == 0
    assert _library_files(pipeline) == []

    _click_retry(job)
    tools.rip_fault = None
    assert await _run(pipeline, job, Report(job)) is None
    assert job.state is JobState.DONE
    assert _library_files(pipeline) == [SRT_NAME, VIDEO_NAME]


# --- the encode volume fills up ---------------------------------------------

async def test_enospc_partway_through_an_encode_keeps_the_rip_and_places_nothing(
        tmp_path, monkeypatch):
    """The staging volume filled while HandBrake was writing. The half-encode is
    worthless; the rip next to it cost an hour of drive time and a disc that is
    back in its sleeve by now, so it must survive untouched."""
    pipeline = _pipeline(tmp_path, auto_eject=True)
    tools = Tools()
    tools.install(pipeline)
    ejects = _watch_eject(monkeypatch)
    job = _job()
    staging = pipeline.settings.storage.staging_dir
    tools.encode_fault = _enospc

    report = Report(job)
    failure = await _run(pipeline, job, report)

    assert isinstance(failure, OSError)
    assert failure.errno == errno.ENOSPC
    assert job.state is JobState.FAILED
    # The half-written encode stays in staging and never reaches the library.
    assert (staging / JOB_NAME / "encode" / VIDEO_NAME).exists()
    # NOTHING reaches the library — which is what this test's name claims. The
    # sidecar used to land there on its own, leaving a movie folder holding
    # subtitles and no film for as long as the encode took.
    assert _library_files(pipeline) == []
    assert (_staged_movie_dir(pipeline) / SRT_NAME).exists()
    assert "Placed video:" not in report.log
    assert not job.checkpoints[0].get("placed")
    # The disc was released back when the rip finished, so nobody is standing
    # over a drive waiting for a volume to be cleared.
    assert ejects == [DEVICE]
    assert any("Finished ripping" in t for t, _m in report.notifications)
    # The rip was saved DURABLY before any encode work began: the first
    # checkpoint the reporter was asked to persist already names the ripped file
    # and carries nothing else yet. Deferring that save to the next stage is how
    # an hour of drive time gets lost to a crash that happens in between.
    assert report.snapshots[0] == {0: {"ripped_path": job.checkpoints[0]["ripped_path"]}}
    # And the rip itself is still there: staging is only cleaned on success.
    ripped = staging / job.checkpoints[0]["ripped_path"]
    assert ripped.exists()

    # Space freed. The retry re-encodes and does nothing else — no disc, no
    # re-extract, no re-OCR.
    _click_retry(job)
    tools.encode_fault = None
    report = Report(job)

    assert await _run(pipeline, job, report) is None
    assert job.state is JobState.DONE
    assert tools.counts == {"rip": 1, "identify": 1, "extract": 1, "ocr": 1, "encode": 2}
    assert "Resuming title 0: reusing ripped file." in report.events
    assert "Subtitles already done (1 sidecar(s)) — skipping OCR." in report.events
    assert _library_files(pipeline) == [SRT_NAME, VIDEO_NAME]
    assert not ripped.exists()   # staging cleaned only now that it really finished


async def test_an_encoder_that_exits_zero_on_a_full_disk_does_not_report_done(tmp_path):
    """The nastier half of ENOSPC: the encoder hit the wall, wrote nothing, and
    exited 0 anyway. Every stage reported success and the file has the right name
    in the right folder — only its size says otherwise.

    The file is weighed BEFORE the move now (it used to be placed, and only the
    end-of-job check caught it — after "placed" had been checkpointed on an empty
    video, which made every later Retry skip the encode that would replace it and
    re-fail identically forever). So: nothing reaches the library, and freeing
    space and retrying finishes the job."""
    pipeline = _pipeline(tmp_path)
    tools = Tools()
    tools.install(pipeline)
    job = _job()

    def out_of_space_but_exit_zero(dst):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"")

    tools.encode_fault = out_of_space_but_exit_zero

    report = Report(job)
    failure = await _run(pipeline, job, report)

    # Nothing empty in the library, and nothing claiming there is.
    assert not (_movie_dir(pipeline) / VIDEO_NAME).exists()
    assert "Placed video:" not in report.log
    assert not job.checkpoints[0].get("placed")
    assert isinstance(failure, RuntimeError)
    assert job.state is JobState.FAILED
    # ...and the reason points at the volume that filled, not at the encoder.
    assert "produced an empty file" in str(failure)
    assert "staging disk filling up" in str(failure)

    # Space freed: the retry re-encodes and the job completes.
    _click_retry(job)
    tools.encode_fault = None
    assert await _run(pipeline, job, Report(job)) is None
    assert job.state is JobState.DONE
    assert _library_files(pipeline) == [SRT_NAME, VIDEO_NAME]


# --- the output share comes back read-only ----------------------------------

async def test_the_output_share_going_read_only_never_marks_the_title_placed(
        tmp_path, monkeypatch, request):
    """The encode finished and then the move into the library was refused.

    Nothing is in the library, so nothing may claim it is: a "placed" flag here
    would make every future resume skip the encode and hand back a video that
    does not exist.
    """
    pipeline = _pipeline(tmp_path)
    tools = Tools()
    tools.install(pipeline)
    job = _job()
    staging = pipeline.settings.storage.staging_dir
    restore = None

    def encode_then_the_share_turns_read_only(dst):
        nonlocal restore
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("a finished x265 encode")
        restore = _deny_writes(request, monkeypatch, _movie_dir(pipeline))

    tools.encode_fault = encode_then_the_share_turns_read_only

    report = Report(job)
    failure = await _run(pipeline, job, report)

    assert isinstance(failure, PermissionError)
    assert job.state is JobState.FAILED
    assert not job.checkpoints[0].get("placed")
    assert "Placed video:" not in report.log
    # The sidecar landed before the share flipped; the video did not land at all.
    # The subtitles are still in staging, so the library shows nothing at all
    # rather than a movie folder that will never gain its film.
    assert _library_files(pipeline) == []
    assert (_staged_movie_dir(pipeline) / SRT_NAME).exists()
    # The rip is untouched, so fixing the share costs no disc time.
    ripped = staging / job.checkpoints[0]["ripped_path"]
    assert ripped.exists()

    # Permissions restored: the job finishes without going back to the disc.
    assert restore is not None
    restore()
    tools.encode_fault = None
    _click_retry(job)
    report = Report(job)

    assert await _run(pipeline, job, report) is None
    assert job.state is JobState.DONE
    assert tools.counts["rip"] == 1
    assert tools.counts["identify"] == 1 and tools.counts["ocr"] == 1
    assert _library_files(pipeline) == [SRT_NAME, VIDEO_NAME]


# --- files that vanish between being written and being verified -------------

async def test_a_video_that_vanishes_between_placing_and_the_final_check_is_not_done(
        tmp_path):
    """The move succeeded and the share dropped a moment later. Every stage
    reported success, and the video is not there — which is exactly why the
    end-of-job check looks at the disk instead of at the stages."""
    pipeline = _pipeline(tmp_path)
    tools = Tools()
    tools.install(pipeline)
    job = _job()
    video = _movie_dir(pipeline) / VIDEO_NAME
    report = Report(job)

    def the_share_drops_right_after_the_move(message):
        if message.startswith("Placed video:"):
            video.unlink()

    report.on_event_hook = the_share_drops_right_after_the_move

    failure = await _run(pipeline, job, report)

    assert isinstance(failure, RuntimeError)
    assert f"missing video: {video}" in str(failure)
    assert "The rip and encode are kept" in str(failure)
    assert job.state is JobState.FAILED
    # The checkpoint does say placed — and the resume does not take its word for
    # it, because it checks the disk before skipping the encode.
    assert job.checkpoints[0]["placed"] is True

    _click_retry(job)
    report = Report(job)

    assert await _run(pipeline, job, report) is None
    assert job.state is JobState.DONE
    assert video.exists()
    assert tools.counts == {"rip": 1, "identify": 1, "extract": 1, "ocr": 1, "encode": 2}


async def test_a_sidecar_that_vanished_from_the_share_is_re_ocred_from_the_kept_images(
        tmp_path):
    """The subtitle checkpoint records placed sidecars. When the share loses
    one, trusting the record means the subtitles are gone for good — so the
    record is only believed while the files are still there. The recovery is an
    OCR of the .sup images already in staging: no re-extract from a multi-GB
    file, and certainly no re-rip."""
    pipeline = _pipeline(tmp_path)
    tools = Tools()
    tools.install(pipeline)
    job = _job()
    # Fail the encode so the job stops before staging is cleaned up.
    tools.encode_fault = _enospc
    assert isinstance(await _run(pipeline, job, Report(job)), OSError)

    srt = _movie_dir(pipeline) / SRT_NAME
    staged_srt = _staged_movie_dir(pipeline) / SRT_NAME
    # The encode failed, so NOTHING was published: the sidecar is still waiting
    # in staging and the library has no half-built movie folder in it.
    assert staged_srt.exists() and not srt.exists()
    assert not _movie_dir(pipeline).exists()
    # The checkpoint still speaks in library paths — only the bytes are waiting.
    assert job.checkpoints[0]["subs_done"] == [f"Movies/WarGames (1983)/{SRT_NAME}"]

    staged_srt.unlink()   # the share dropped it between runs
    _click_retry(job)
    tools.encode_fault = None
    report = Report(job)

    assert await _run(pipeline, job, report) is None
    assert job.state is JobState.DONE
    assert srt.exists()
    # The OCR ran again; the extract (and the rip) did not.
    assert tools.counts == {"rip": 1, "identify": 1, "extract": 1, "ocr": 2, "encode": 2}
    assert "Reusing 1 previously-extracted subtitle(s)." in report.events
    assert "Subtitles already done" not in report.log


async def test_a_job_added_from_a_file_that_vanished_says_so_instead_of_blaming_a_disc(
        tmp_path):
    """A job built from an .mkv on the share has no disc behind it. When the
    share drops the file, handing the bare path to makemkvcon only produces a
    MakeMKV error about a source it cannot parse — which reads as a disc problem
    on a job that never involved one."""
    pipeline = _pipeline(tmp_path)
    tools = Tools()
    tools.install(pipeline)
    job = _job()
    job.source = "/mnt/TrueNas/incoming/WarGames.mkv"
    job.disc.source = job.source

    failure = await _run(pipeline, job, Report(job))

    assert isinstance(failure, RuntimeError)
    assert "The source file is gone: /mnt/TrueNas/incoming/WarGames.mkv" in str(failure)
    assert "no disc to re-rip it from" in str(failure)
    assert "check the server can still reach that path" in str(failure)
    assert tools.counts["rip"] == 0        # makemkvcon is never handed a bare path
    assert "ripped_path" not in job.checkpoints.get(0, {})


# --- the share drops between the rip box and the worker ---------------------

async def test_the_share_dropping_before_the_worker_starts_leaves_the_job_reclaimable(
        tmp_path):
    """The rip box finished and queued the job; by the time a worker claimed it
    the NAS had dropped, so the ripped file the checkpoint names is not there.

    The rip still happened. Clearing the checkpoint — or leaving the claim on a
    worker that has already given up — would each cost the operator the disc.
    """
    settings = _settings(tmp_path)
    # A URL only so the runner is built the way a clustered one is; the store is
    # replaced below with the in-memory stand-in, so nothing ever dials Postgres.
    settings.database_url = "postgresql://cluster/blurayripper"
    runner = WorkerRunner(settings, worker_id="encoder-A")
    runner.store = _SharedStore()
    tools = Tools()
    tools.install(runner.pipeline)

    ripped_rel = f"{JOB_NAME}/rip/t000/{RIP_NAME.format(0)}"
    runner.store.save(JobRecord(
        id=334, source="disc:0", state="ready",
        assignments=[assignment_to_dict(
            EpisodeAssignment(0, MediaKind.MOVIE, "WarGames", 1983))],
        selected_title_ids=[0],
        checkpoints={"0": {"ripped_path": ripped_rel, "tune_grain": False}},
    ))

    claimed = runner.store.claim_next("encoder-A", CLAIMABLE_STATES)
    assert claimed is not None
    await runner._process(claimed)

    saved = {r.id: r for r in runner.store.load_all()}[334]
    assert saved.state == "failed"
    assert "has no ripped file to encode" in saved.error
    # The record of the rip survives the failure...
    assert saved.checkpoints["0"]["ripped_path"] == ripped_rel
    assert tools.counts["encode"] == 0
    assert _library_files(runner.pipeline) == []

    # ...the share comes back, and the retry is claimable straight away rather
    # than waiting out a frozen heartbeat on the worker that failed.
    ripped = settings.storage.staging_dir / ripped_rel
    ripped.parent.mkdir(parents=True, exist_ok=True)
    ripped.write_text("the rip that was there all along")
    saved.state = "ready"
    runner.store.save(saved)

    # A genuinely different box takes the retry. It has to be its own runner:
    # a worker's progress writes are scoped to the claim it holds, so driving
    # encoder-B's job through encoder-A's runner is a cluster state that cannot
    # happen and would hide whether the retry was really recorded.
    second = WorkerRunner(settings, worker_id="encoder-B")
    second.store = runner.store
    tools.install(second.pipeline)

    retry = second.store.claim_next("encoder-B", CLAIMABLE_STATES)
    assert retry is not None and retry.id == 334
    await second._process(retry)

    done = {r.id: r for r in runner.store.load_all()}[334]
    assert done.state == "done"
    assert done.outputs == [str(_movie_dir(runner.pipeline) / VIDEO_NAME)]
    # Only the encode ran on the worker; the disc was never touched.
    assert tools.counts["rip"] == 0
    assert tools.counts["encode"] == 1
    assert _library_files(runner.pipeline) == [VIDEO_NAME]
    assert not ripped.exists()   # staging cleaned once it really finished
