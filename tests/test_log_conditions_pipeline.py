"""The conditions pipeline.py's own messages claim to have handled.

Every message in pipeline.py is a claim about a decision the code just made: a
title was skipped, a ripped file was reused instead of re-ripped, an artwork
failure did not fail the job, a checkpoint was written — or deliberately was
NOT, so a retry redoes exactly the failed part and nothing else.

These tests assert the CLAIM, not the wording: which tool was invoked, what is
on disk afterwards, what the checkpoint says, whether the job survived. A log
line that lies is worse than none — every incident this project has had was a
stage that reported success (a rip that saved nothing queued as ready, an eject
that failed while announcing "safe to load the next one", a failed OCR recorded
as "subtitles done").
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from blurayripper.config import load_settings
from blurayripper.models import (
    DiscInfo, EpisodeAssignment, MediaKind, Stream, StreamType, Title,
)
from blurayripper.pipeline import Job, JobState, Pipeline, TitleResult
from blurayripper.scheduler import EncodeScheduler
from blurayripper.services.hdr import HdrInfo
from blurayripper.tools.mkvtools import SubtitleTrack

# Verbatim from the production cluster (real_logs.txt): the seconv build that
# could not start at all, which is what a per-track OCR failure looks like here.
SECONV_DOTNET_ERROR = (
    "You must install or update .NET to run this application.\n"
    "App: /opt/brr-tools/seconv-bin/seconv.dll\n"
    "Framework: 'Microsoft.NETCore.App', version '8.0.0' (x64)"
)
# MakeMKV when the drive dropped off the bus mid-read (job 317).
MAKEMKV_DRIVE_GONE = (
    "Error 'Posix error - No such device' occurred while reading "
    "'/BDMV/STREAM/00001.m2ts' at offset '0'"
)


class _Report:
    """A reporter exposing every optional hook the pipeline sniffs for."""

    def __init__(self):
        self.events = []
        self.notes = []
        self.ejects = []
        self.checkpoints = 0

    def on_event(self, m): self.events.append(m)
    def on_progress(self, *a): pass
    def notify(self, title, message): self.notes.append((title, message))
    def ejected(self, device): self.ejects.append(device)
    def checkpoint(self): self.checkpoints += 1

    def said(self, needle) -> bool:
        return any(needle in e for e in self.events)


def _pipe(tmp_path, monkeypatch, *, tmdb_key=None) -> Pipeline:
    s = load_settings()
    monkeypatch.setattr(s.storage, "staging_dir", tmp_path / "staging")
    monkeypatch.setattr(s.storage, "output_dir", tmp_path / "out")
    monkeypatch.setattr(s, "write_nfo", False)
    monkeypatch.setattr(s, "rip_duration_tolerance_pct", 0)  # opt in per test
    monkeypatch.setattr(s, "extract_cover_art", False)       # opt in per test
    monkeypatch.setattr(s, "auto_eject", False)              # opt in per test
    monkeypatch.setattr(s, "tmdb_api_key", tmdb_key)  # read by TMDBClient at build time
    return Pipeline(s)


def _movie(title="The Film", year=1999, title_id=0) -> EpisodeAssignment:
    return EpisodeAssignment(title_id, MediaKind.MOVIE, title, year)


def _pgs(track_id=3, language="eng") -> SubtitleTrack:
    return SubtitleTrack(track_id=track_id, codec="S_HDMV/PGS", language=language,
                         is_pgs=True)


def _disc_title(title_id=0, *, seconds=1711, sub_langs=("eng",)) -> Title:
    return Title(
        title_id=title_id, duration_seconds=seconds,
        streams=[Stream(stream_id=i, stream_type=StreamType.SUBTITLE, language=lang)
                 for i, lang in enumerate(sub_langs)])


def _job(source="disc:0", *, titles=(0,), assigned=None, sub_langs=("eng",),
         device=None) -> Job:
    """A scanned job: ``titles`` selected, ``assigned`` (default: all) named."""
    job = Job(source=source)
    job.disc = DiscInfo(source=source, disc_name="DISC LABEL", drive_device=device,
                        titles=[_disc_title(t, sub_langs=sub_langs) for t in titles])
    job.fingerprint = "fp0123456789abcdef"
    job.selected_title_ids = list(titles)
    named = list(titles) if assigned is None else list(assigned)
    job.assignments = {t: _movie(f"Film {t}", 1999 + t, title_id=t) for t in named}
    return job


class _Rig:
    """Every external tool the pipeline drives, faked and recorded.

    Nothing here decides anything the tests assert on: the fakes only stand in
    for makemkvcon/mkvmerge/mkvextract/seconv/HandBrake so the real pipeline
    logic runs end to end against tmp_path.
    """

    def __init__(self, pipe, monkeypatch, *, tracks=(), height=1080,
                 rip_seconds=1711, ocr_fails=()):
        self.pipe = pipe
        self.rips = []          # title ids makemkvcon was asked to rip
        self.encodes = []       # (src, dst) HandBrake was handed
        self.extracts = []      # [(mkv, [track_ids])] mkvextract passes
        self.identifies = []
        self.ocr = []           # .sup files seconv was handed
        self.ejects = []
        self._tracks = list(tracks)
        self._ocr_fails = set(ocr_fails)

        async def rip(source, title_id, rip_dir, on_progress=None, on_message=None):
            self.rips.append(title_id)
            rip_dir.mkdir(parents=True, exist_ok=True)
            out = rip_dir / f"title_t{title_id:02d}.mkv"
            out.write_text(f"ripped bytes for title {title_id}")
            return [out]

        async def list_drives(timeout=None):
            return [SimpleNamespace(index=0, device="D:", disc_name="DISC LABEL",
                                    makemkv_blind=False)]

        async def probe(path):
            return SimpleNamespace(duration_seconds=rip_seconds)

        async def video_height(path):
            return height

        async def identify(path):
            self.identifies.append(Path(path))
            return list(self._tracks)

        async def extract_tracks(mkv, specs, *, on_progress=None):
            self.extracts.append((Path(mkv), [tid for tid, _ in specs]))
            for _tid, out in specs:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text("PGS bitmaps")

        async def ocr_to_srt(sup, language, out_dir):
            self.ocr.append(Path(sup).name)
            if Path(sup).name in self._ocr_fails:
                raise RuntimeError(SECONV_DOTNET_ERROR)
            out_dir.mkdir(parents=True, exist_ok=True)
            srt = out_dir / (Path(sup).stem + ".srt")
            srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello there.\n",
                           encoding="utf-8")
            return srt

        async def encode(src, dst, *, on_progress=None, tune_grain=False,
                         deinterlace=False):
            self.encodes.append((Path(src), Path(dst)))
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            Path(dst).write_text("encoded video")

        monkeypatch.setattr(pipe.makemkv, "rip_title", rip)
        monkeypatch.setattr(pipe.makemkv, "list_drives", list_drives)
        monkeypatch.setattr(pipe.mkvtools, "probe", probe)
        monkeypatch.setattr(pipe.mkvtools, "video_height", video_height)
        monkeypatch.setattr(pipe.mkvtools, "identify_subtitles", identify)
        monkeypatch.setattr(pipe.mkvtools, "extract_tracks", extract_tracks)
        monkeypatch.setattr(pipe.subtitleedit, "ocr_to_srt", ocr_to_srt)
        monkeypatch.setattr(pipe.handbrake, "encode", encode)
        monkeypatch.setattr(pipe.handbrake, "speed_summary", lambda: "preset")
        monkeypatch.setattr("blurayripper.pipeline.eject", self.ejects.append)


def _staging_root(pipe, name="job") -> Path:
    return pipe.settings.storage.staging_dir / name


# --- "Skipping title N: no naming assignment." ----------------------------
# A title nobody named cannot be placed anywhere. The claim is that it is
# stepped over — not ripped, not encoded, not counted as an output.

async def test_an_unnamed_title_is_never_ripped_while_its_neighbour_is(tmp_path, monkeypatch):
    """Ripping a title with no assignment burns 40 minutes of drive time on a
    file that has no destination — and then reports it as a produced output."""
    pipe = _pipe(tmp_path, monkeypatch)
    rig = _Rig(pipe, monkeypatch, tracks=[])
    job = _job(titles=(0, 1), assigned=(1,), sub_langs=())
    r = _Report()

    await pipe._process_titles(job, _staging_root(pipe), EncodeScheduler(3), r)

    assert rig.rips == [1]                       # title 0 was never handed to MakeMKV
    assert [res.title_id for res in job.results] == [1]
    assert r.said("Skipping title 0: no naming assignment.")


async def test_the_encode_phase_skips_an_unnamed_title_before_demanding_a_rip(
        tmp_path, monkeypatch):
    """A worker's encode phase raises when a title has no ripped file. An unnamed
    title never had one and never will, so the skip has to come first — otherwise
    one unnamed extra on a disc fails the whole (already ripped) job."""
    pipe = _pipe(tmp_path, monkeypatch)
    rig = _Rig(pipe, monkeypatch)
    job = _job(titles=(0, 1), assigned=(0,), sub_langs=())
    ripped = pipe.settings.storage.staging_dir / "rip" / "t000.mkv"
    ripped.parent.mkdir(parents=True)
    ripped.write_text("rip")
    job.checkpoints[0] = {"ripped_path": "rip/t000.mkv"}
    r = _Report()

    results = await pipe.encode_phase(job, _staging_root(pipe), EncodeScheduler(3), r)

    assert [res.title_id for res in results] == [0]      # no RuntimeError for title 1
    assert len(rig.encodes) == 1
    assert r.said("Skipping title 1: no naming assignment.")


# --- "Resuming title N: reusing ripped file." -----------------------------

async def test_a_resume_hands_back_the_prior_rip_untouched(tmp_path, monkeypatch):
    """The whole point of the rip checkpoint. Re-ripping costs the drive another
    40 minutes and needs the disc, which is by then in a stack on the desk."""
    pipe = _pipe(tmp_path, monkeypatch)
    rig = _Rig(pipe, monkeypatch)
    prior = pipe.settings.storage.staging_dir / "j" / "rip" / "t000" / "prior.mkv"
    prior.parent.mkdir(parents=True)
    prior.write_text("the rip from the run before the box rebooted")
    job = _job()
    job.checkpoints[0] = {"ripped_path": "j/rip/t000/prior.mkv"}
    r = _Report()

    got = await pipe._ensure_ripped(job, 0, _staging_root(pipe), r)

    assert got == prior
    assert prior.read_text() == "the rip from the run before the box rebooted"
    assert rig.rips == []                    # makemkvcon was never invoked
    assert r.checkpoints == 0                # nothing new to persist
    assert r.said("Resuming title 0: reusing ripped file.")


async def test_a_checkpointed_rip_that_vanished_is_re_ripped(tmp_path, monkeypatch):
    """Staging lives on a NAS that gets cleaned. The checkpoint is a claim about
    a file, so it is only worth anything while the file is actually there —
    otherwise the encode is handed a path to nothing."""
    pipe = _pipe(tmp_path, monkeypatch)
    rig = _Rig(pipe, monkeypatch)
    job = _job()
    job.checkpoints[0] = {"ripped_path": "j/rip/t000/swept_away.mkv"}
    r = _Report()

    got = await pipe._ensure_ripped(job, 0, _staging_root(pipe), r)

    assert rig.rips == [0]
    assert got.exists() and got.name != "swept_away.mkv"
    assert job.checkpoints[0]["ripped_path"].endswith(got.name)
    assert not r.said("Resuming title 0")


async def test_a_file_sourced_job_whose_file_is_gone_never_calls_makemkv(
        tmp_path, monkeypatch):
    """A job added from an .mkv has no disc behind it. makemkvcon cannot even
    parse a bare path, so re-ripping produces a cryptic MakeMKV error for a job
    that never involved a disc — the operator needs to be told to restore the
    file, not to check the drive."""
    pipe = _pipe(tmp_path, monkeypatch)
    rig = _Rig(pipe, monkeypatch)
    job = _job(source=str(tmp_path / "archive" / "movie.mkv"))
    r = _Report()

    with pytest.raises(RuntimeError, match="source file is gone"):
        await pipe._ensure_ripped(job, 0, _staging_root(pipe), r)

    assert rig.rips == []
    assert job.checkpoints[0] == {}          # nothing claimed about a missing file


# --- "Title N already done (resume) — skipping." --------------------------

async def test_a_placed_title_is_skipped_and_still_proves_its_subtitles(
        tmp_path, monkeypatch):
    """The skip must hand back the sidecars recorded when the title WAS done.
    Without them the end-of-job check sees a title that produced no subtitles
    from a disc that carries them, and fails a job that is already complete —
    every single time it is resumed."""
    pipe = _pipe(tmp_path, monkeypatch)
    rig = _Rig(pipe, monkeypatch, tracks=[_pgs()])
    job = _job()
    a = job.assignments[0]
    video = pipe._video_path_for(a)
    video.parent.mkdir(parents=True)
    video.write_text("the finished encode")
    srt = video.with_suffix(".en.default.srt")
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi.\n")
    pipe._record_subs_done(job.checkpoints.setdefault(0, {}), [srt])
    job.checkpoints[0]["placed"] = True
    r = _Report()

    result = await pipe._process_one(job, 0, a, _staging_root(pipe),
                                     EncodeScheduler(3), r)

    assert rig.rips == [] and rig.encodes == []
    assert result.video_path == video and video.read_text() == "the finished encode"
    assert result.subtitle_paths == [srt]
    assert result.from_checkpoint is True
    job.results = [result]
    assert pipe._verify_outputs(job) == []      # a resumed job still verifies clean
    assert r.said("Title 0 already done (resume) — skipping.")


async def test_a_placed_flag_whose_video_is_gone_does_not_skip_the_title(
        tmp_path, monkeypatch):
    """"placed" is a claim about a file in the library. If someone deleted the
    file, honouring the flag leaves the job green with nothing in Plex."""
    pipe = _pipe(tmp_path, monkeypatch)
    rig = _Rig(pipe, monkeypatch, tracks=[])
    job = _job(sub_langs=())
    a = job.assignments[0]
    job.checkpoints[0] = {"placed": True}       # ...but the video was deleted
    r = _Report()

    result = await pipe._process_one(job, 0, a, _staging_root(pipe),
                                     EncodeScheduler(3), r)

    assert rig.rips == [0] and len(rig.encodes) == 1
    assert result.video_path.exists()
    assert not r.said("already done (resume)")


# --- the truncated-rip rejection and its checkpoint ------------------------

async def test_a_truncated_rip_is_neither_checkpointed_nor_announced_as_finished(
        tmp_path, monkeypatch):
    """A disc read error MakeMKV still exits 0 on. Checkpointing the short file
    makes every later retry "resume" from it, so the truncation becomes
    permanent — and ejecting/notifying tells the operator to put the disc away
    at the exact moment the only fix is to re-read it."""
    pipe = _pipe(tmp_path, monkeypatch)
    monkeypatch.setattr(pipe.settings, "rip_duration_tolerance_pct", 3)
    monkeypatch.setattr(pipe.settings, "auto_eject", True)
    rig = _Rig(pipe, monkeypatch, rip_seconds=900)   # disc says 1711s
    job = _job(device="D:")
    r = _Report()

    with pytest.raises(RuntimeError, match="looks truncated"):
        await pipe._ensure_ripped(job, 0, _staging_root(pipe), r)

    assert "ripped_path" not in job.checkpoints[0]
    assert r.checkpoints == 0
    assert rig.ejects == [] and r.notes == []    # the disc is still needed


async def test_a_verified_rip_is_checkpointed_by_a_path_that_survives_a_move(
        tmp_path, monkeypatch):
    """Only once the runtime matches the disc is the rip worth resuming from.
    The path is stored relative to staging so the worker that picks the job up
    resolves it against its own mount, not the rip box's."""
    pipe = _pipe(tmp_path, monkeypatch)
    monkeypatch.setattr(pipe.settings, "rip_duration_tolerance_pct", 3)
    _Rig(pipe, monkeypatch, rip_seconds=1700)    # disc says 1711s -> within 3%
    job = _job()
    r = _Report()

    ripped = await pipe._ensure_ripped(job, 0, _staging_root(pipe), r)

    stored = job.checkpoints[0]["ripped_path"]
    assert not Path(stored).is_absolute() and "\\" not in stored
    assert Path(pipe.settings.storage.staging_dir, stored) == ripped
    assert r.checkpoints == 1
    assert r.said("Rip verified: 1700s vs disc 1711s.")


# --- grain resolution and its checkpoint ----------------------------------

async def test_the_grain_choice_is_checkpointed_and_a_resume_never_re_asks(
        tmp_path, monkeypatch):
    """Grain is decided once per disc at a UI gate. A resume that re-opens the
    gate reads as an endless "pick grain" loop — and "off" is a real answer, so
    a falsy stored choice must count as decided just like "on"."""
    pipe = _pipe(tmp_path, monkeypatch)
    _Rig(pipe, monkeypatch, tracks=[])
    job = _job(sub_langs=())
    a = job.assignments[0]
    asked = []

    async def resolve_grain(j, ripped, title_id, staging_root, report):
        asked.append(title_id)
        return False                      # the user picks "grain off"
    monkeypatch.setattr(pipe, "_resolve_grain", resolve_grain)
    rip = tmp_path / "r.mkv"
    rip.write_text("rip")
    r = _Report()

    await pipe._prepare_title(job, 0, a, _staging_root(pipe), rip, r)
    assert asked == [0]
    assert job.checkpoints[0]["tune_grain"] is False
    checkpointed = r.checkpoints
    assert checkpointed >= 1

    # Second pass: the same job resumed on another box.
    await pipe._prepare_title(job, 0, a, _staging_root(pipe), rip, r)
    assert asked == [0]                   # the gate did not re-open
    assert job.checkpoints[0]["tune_grain"] is False


# --- artwork failures are non-fatal ---------------------------------------

async def test_unreadable_disc_art_costs_the_poster_not_the_disc(tmp_path, monkeypatch):
    """Cover art is scraped off the disc while it is still in the drive, before
    any rip. A studio's malformed META folder must not take down the rip that
    the disc is actually here for."""
    pipe = _pipe(tmp_path, monkeypatch)
    monkeypatch.setattr(pipe.settings, "extract_cover_art", True)
    rig = _Rig(pipe, monkeypatch, tracks=[])
    job = _job(source=f"file:{tmp_path / 'disc'}", sub_langs=())

    def boom(root, dest, **k):
        raise OSError(MAKEMKV_DRIVE_GONE)
    monkeypatch.setattr("blurayripper.services.coverart.extract_art", boom)
    r = _Report()

    await pipe.execute(job, EncodeScheduler(3), r, "job")

    assert job.state is JobState.DONE and job.error is None
    assert rig.rips == [0]
    assert pipe._video_path_for(job.assignments[0]).exists()
    assert r.said("Cover art skipped")


async def test_a_tmdb_outage_costs_the_artwork_not_the_job(tmp_path, monkeypatch):
    """TMDB is a third party reached over the internet from a box whose job is
    ripping discs. A 500 there must never be able to fail a rip."""
    pipe = _pipe(tmp_path, monkeypatch, tmdb_key="fake-key")
    monkeypatch.setattr(pipe.settings, "fetch_tmdb_artwork", True)
    rig = _Rig(pipe, monkeypatch, tracks=[])
    job = _job(sub_langs=())

    async def art(title, year):
        raise RuntimeError("TMDB 500")
    saved = []
    monkeypatch.setattr(pipe.tmdb, "movie_artwork", art)
    monkeypatch.setattr(pipe.tmdb, "save_image",
                        lambda url, dest: saved.append(dest))
    r = _Report()

    await pipe.execute(job, EncodeScheduler(3), r, "job")

    assert job.state is JobState.DONE and job.error is None
    assert saved == []                                  # nothing was downloaded
    assert pipe._video_path_for(job.assignments[0]).exists()
    assert len(rig.encodes) == 1
    assert r.said("TMDB artwork skipped")


# --- the UHD / HandBrake refusal ------------------------------------------

async def test_the_uhd_refusal_keeps_the_rip_and_starts_nothing(tmp_path, monkeypatch):
    """HandBrake would produce a watchable 4K file with the HDR quietly gone,
    after hours of CPU. Refusing costs seconds — but only if the rip survives
    it, so that fixing the setting and hitting Retry re-runs the encode alone."""
    pipe = _pipe(tmp_path, monkeypatch)
    rig = _Rig(pipe, monkeypatch, height=2160)
    monkeypatch.setattr(pipe.settings.encode, "ffmpeg_uhd", False)
    monkeypatch.setattr(pipe.settings.encode, "uhd_require_ffmpeg", True)
    job = _job(sub_langs=())
    a = job.assignments[0]
    ripped = pipe.settings.storage.staging_dir / "j" / "rip" / "t000" / "r.mkv"
    ripped.parent.mkdir(parents=True)
    ripped.write_text("the 4K rip")
    cp = job.checkpoints.setdefault(0, {"ripped_path": "j/rip/t000/r.mkv"})
    scheduler = EncodeScheduler(3)
    r = _Report()

    with pytest.raises(RuntimeError, match="drops HDR10/Dolby Vision"):
        await pipe._encode_title(job, 0, a, _staging_root(pipe), ripped, scheduler, r)

    assert rig.encodes == []                       # HandBrake never started
    assert ripped.read_text() == "the 4K rip"      # the rip is kept for the retry
    assert "placed" not in cp
    assert not pipe._video_path_for(a).exists()    # nothing landed in the library
    assert scheduler._in_use == 0                  # and no encode slot was leaked


# --- the HDR check, both ways ---------------------------------------------

def _hdr10() -> HdrInfo:
    return HdrInfo(colorprim="bt2020", transfer="smpte2084", colormatrix="bt2020nc",
                   master_display="G(13250,34500)L(10000000,50)", max_cll="1000,400")


class _FakeFFmpeg:
    """ffmpeg that encodes to a file and reports whatever HDR the test wants."""

    def __init__(self, out_info):
        self.available = True
        self._out_info = out_info
        self.probed = []

    async def probe_video(self, path):
        self.probed.append(Path(path).name)
        info = self._out_info if len(self.probed) > 1 else _hdr10()
        return info, 7200.0

    async def encode(self, src, dst, *, on_progress=None, tune_grain=False,
                     low_priority=True):
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_text("4K encode, hours of CPU")


async def test_a_failed_hdr_check_is_loud_but_keeps_and_places_the_file(
        tmp_path, monkeypatch):
    """An HDR10 source that came out SDR still plays — you notice months later
    on the couch. It has to be said out loud, and the file kept so it can be
    judged: throwing away a finished 4K encode over a metadata check is worse."""
    pipe = _pipe(tmp_path, monkeypatch)
    rig = _Rig(pipe, monkeypatch, height=2160)
    monkeypatch.setattr(pipe.settings.encode, "ffmpeg_uhd", True)
    stripped = HdrInfo(colorprim="bt709", transfer="bt709", colormatrix="bt709")
    ff = _FakeFFmpeg(stripped)
    monkeypatch.setattr(pipe, "ffmpeg", ff)
    job = _job(sub_langs=())
    a = job.assignments[0]
    r = _Report()

    result = await pipe._encode_title(job, 0, a, _staging_root(pipe),
                                      tmp_path / "rip.mkv", EncodeScheduler(3), r)

    assert result.video_path.exists()
    assert result.video_path.read_text() == "4K encode, hours of CPU"
    assert job.checkpoints[0]["placed"] is True
    assert rig.encodes == []                        # ffmpeg's path, not HandBrake's
    failure = next(e for e in r.events if "HDR CHECK FAILED" in e)
    # The metadata the source had and the output does not, named individually so
    # the log says what was lost rather than just that something was.
    assert "master-display" in failure and "max-cll" in failure
    assert not r.said("HDR check passed")


async def test_the_hdr_check_probes_the_output_not_the_source_again(
        tmp_path, monkeypatch):
    """Re-probing the source would pass every time and prove nothing — the check
    exists precisely because the OUTPUT can differ from what went in."""
    pipe = _pipe(tmp_path, monkeypatch)
    ff = _FakeFFmpeg(_hdr10())
    monkeypatch.setattr(pipe, "ffmpeg", ff)
    r = _Report()

    await pipe._encode_uhd(tmp_path / "src.mkv", tmp_path / "out.mkv", False,
                           tmp_path / "enc", r)

    assert ff.probed == ["src.mkv", "out.mkv"]
    assert r.said("HDR check passed")
    assert not r.said("HDR CHECK FAILED")


# --- subtitle extraction: failed vs genuinely none ------------------------

async def test_a_dead_mkvmerge_is_an_extraction_failure_not_an_empty_disc(
        tmp_path, monkeypatch):
    """"No subtitles" and "we could not look" are opposite answers. Confusing
    them places a subtitle-less video, marks the title done, and every later
    resume skips it — the subtitles are then unrecoverable."""
    pipe = _pipe(tmp_path, monkeypatch)
    rig = _Rig(pipe, monkeypatch)

    async def dead(path):
        raise OSError("mkvmerge: " + MAKEMKV_DRIVE_GONE)
    monkeypatch.setattr(pipe.mkvtools, "identify_subtitles", dead)
    job = _job()
    r = _Report()

    with pytest.raises(RuntimeError, match="Subtitle extraction failed for title 0"):
        await pipe._extract_title_subs(job, 0, tmp_path / "r.mkv",
                                       tmp_path / "sub", r)

    assert "subs" not in job.checkpoints[0]   # a retry re-extracts
    assert rig.extracts == []
    assert r.said("Subtitle identify failed: OSError")


async def test_a_dead_mkvextract_is_an_extraction_failure_too(tmp_path, monkeypatch):
    """Identify succeeded, so the disc definitely has kept-language tracks. A
    failure now must not be recorded as "this title has none"."""
    pipe = _pipe(tmp_path, monkeypatch)
    _Rig(pipe, monkeypatch, tracks=[_pgs(3, "eng")])

    async def dead(mkv, specs, *, on_progress=None):
        raise OSError("mkvextract: input/output error")
    monkeypatch.setattr(pipe.mkvtools, "extract_tracks", dead)
    job = _job()
    r = _Report()

    with pytest.raises(RuntimeError, match="Subtitle extraction failed for title 0"):
        await pipe._extract_title_subs(job, 0, tmp_path / "r.mkv",
                                       tmp_path / "sub", r)

    assert "subs" not in job.checkpoints[0]
    assert r.said("Subtitle extraction failed: OSError")


async def test_a_disc_with_no_kept_language_tracks_is_recorded_as_handled(
        tmp_path, monkeypatch):
    """Plenty of discs carry only tracks we don't keep. That is a finished
    answer: nothing to extract, and nothing for a resume to retry forever."""
    pipe = _pipe(tmp_path, monkeypatch)
    rig = _Rig(pipe, monkeypatch, tracks=[_pgs(3, "fre"), _pgs(4, "spa")])
    job = _job()
    r = _Report()

    out = await pipe._extract_title_subs(job, 0, tmp_path / "r.mkv",
                                         tmp_path / "sub", r)

    assert out == []
    assert rig.extracts == []                  # nothing worth a mkvextract pass
    assert job.checkpoints[0]["subs"] == []    # checkpointed as handled
    assert not r.said("Extracting 2 subtitle track(s)")


async def test_every_kept_track_is_extracted_in_one_pass_to_its_own_file(
        tmp_path, monkeypatch):
    """One mkvextract pass = one read of a multi-GB file off the NAS. And the
    .sup name carries the track id, or two English tracks overwrite each other
    and the second subtitle is silently lost."""
    pipe = _pipe(tmp_path, monkeypatch)
    rig = _Rig(pipe, monkeypatch, tracks=[_pgs(3, "eng"), _pgs(4, "eng"),
                                          _pgs(5, "fre")])
    job = _job()
    rip = tmp_path / "Film_t00.mkv"
    r = _Report()

    out = await pipe._extract_title_subs(job, 0, rip, tmp_path / "sub", r)

    assert rig.extracts == [(rip, [3, 4])]     # one pass, French left behind
    assert len({sup for sup, _t in out}) == 2 and all(sup.exists() for sup, _ in out)
    assert [t.track_id for _s, t in out] == [3, 4]
    assert r.said("Extracting 2 subtitle track(s) [eng, eng] in one pass")


# --- OCR failing on one track ---------------------------------------------

async def test_one_track_failing_ocr_keeps_the_other_and_leaves_a_cheap_retry(
        tmp_path, monkeypatch):
    """The exact shape of the failure that sealed jobs shut: _ocr_all drops a
    track whose OCR raised, leaving a result identical to "this disc has no
    subtitles". Recording that as done convinced every later run there was
    nothing to do while the encode marked the title placed. The retry must
    re-OCR — and must NOT need a re-extract or a re-rip to do it."""
    pipe = _pipe(tmp_path, monkeypatch)
    rip = tmp_path / "Film_t00.mkv"
    rip.write_text("rip")
    _Rig(pipe, monkeypatch, tracks=[_pgs(3, "eng"), _pgs(4, "eng")],
         ocr_fails=["Film_t00.t4.eng.sup"])
    job = _job()
    a = job.assignments[0]
    monkeypatch.setattr(pipe, "_resolve_grain",
                        lambda *args, **kw: _done(False))
    r = _Report()

    result = await pipe._prepare_title(job, 0, a, _staging_root(pipe), rip, r)

    # The track that OCR'd is still delivered.
    assert len(result.subtitle_paths) == 1 and result.subtitle_paths[0].exists()
    # The title is NOT recorded done, so the retry re-runs OCR.
    cp = job.checkpoints[0]
    assert "subs_done" not in cp and pipe._subs_already_done(cp) is None
    # ...and both extracted images are still on disk, so it costs no re-rip.
    reusable = pipe._reuse_subs(cp)
    assert reusable is not None and len(reusable) == 2
    assert all(sup.exists() for sup, _t in reusable)
    assert r.said("Subtitle OCR failed (eng)")
    assert r.said("1 of 2 subtitle track(s) failed OCR")


def _done(value):
    """A completed awaitable, for stubbing an async method with a lambda."""
    async def _run():
        return value
    return _run()


# --- eject + notify after the LAST title ----------------------------------

async def test_the_drive_is_ejected_only_after_the_last_selected_title(
        tmp_path, monkeypatch):
    """Ejecting between titles pops the disc out from under the next rip. The
    notification is also the operator's only signal that the drive is free, so
    it belongs at the end of the disc — not once per episode."""
    pipe = _pipe(tmp_path, monkeypatch)
    monkeypatch.setattr(pipe.settings, "auto_eject", True)
    rig = _Rig(pipe, monkeypatch)
    job = _job(titles=(0, 1, 2), device="D:")
    r = _Report()

    await pipe._ensure_ripped(job, 0, _staging_root(pipe), r)
    await pipe._ensure_ripped(job, 1, _staging_root(pipe), r)
    assert rig.ejects == [] and r.notes == []      # the disc is still being read

    await pipe._ensure_ripped(job, 2, _staging_root(pipe), r)

    assert rig.ejects == ["D:"]                    # once, on the drive it is in now
    assert r.ejects == ["D:"]                      # the UI drops its cached disc name
    assert len(r.notes) == 1
    _title, message = r.notes[0]
    assert "disc ejected, safe to swap in the next one" in message


# --- the end-of-job output check ------------------------------------------

async def test_a_job_whose_subtitles_never_appeared_is_not_reported_done(
        tmp_path, monkeypatch):
    """Job 42: the encode carried on after subtitle OCR died, the video was
    placed with no sidecars, and the job went green. Every stage reported its
    own success; only looking at the finished folder catches it."""
    pipe = _pipe(tmp_path, monkeypatch)
    rig = _Rig(pipe, monkeypatch, tracks=[_pgs(3, "eng")],
               ocr_fails=["title_t00.t3.eng.sup"])
    job = _job()                       # the disc carries an English subtitle track
    r = _Report()

    with pytest.raises(RuntimeError, match="output is incomplete"):
        await pipe.execute(job, EncodeScheduler(3), r, "job")

    assert job.state is JobState.FAILED
    assert "no .srt was produced" in job.error
    # The rip and the encode are kept, so the retry redoes only the OCR.
    video = pipe._video_path_for(job.assignments[0])
    assert video.exists()
    assert _staging_root(pipe).exists()
    assert Path(pipe.settings.storage.staging_dir,
                job.checkpoints[0]["ripped_path"]).exists()
    assert pipe._reuse_subs(job.checkpoints[0]) is not None
    assert rig.rips == [0]
    assert r.said("FAILED during")


async def test_a_complete_job_counts_its_files_clears_staging_and_saves_the_mapping(
        tmp_path, monkeypatch):
    """The green path has to be earned: every promised file present on disk, the
    intermediates removed from the NAS, and the disc's title mapping stored so
    the next copy of this disc runs unattended."""
    pipe = _pipe(tmp_path, monkeypatch)
    rig = _Rig(pipe, monkeypatch, tracks=[_pgs(3, "eng")])
    job = _job()
    r = _Report()

    await pipe.execute(job, EncodeScheduler(3), r, "job")

    assert job.state is JobState.DONE
    video = pipe._video_path_for(job.assignments[0])
    srt = video.with_suffix(".en.default.srt")
    assert video.exists() and srt.exists() and srt.stat().st_size > 0
    assert r.said("Verified 2 output file(s) in place.")   # the video and its sidecar
    assert not _staging_root(pipe).exists()                # staging cleaned up
    saved = pipe.mapping_store.get(job.fingerprint)
    assert saved is not None and [a.title_id for a in saved] == [0]
    assert r.said("Saved disc mapping for automatic future re-rips.")
    assert r.said("Done.")
    assert rig.ocr == ["title_t00.t3.eng.sup"]
