"""The encode-and-place boundary, and the resumes that used to lie about it.

Every failure pinned here reported success, or reported the wrong cause, or made
the job impossible to retry — the three ways this pipeline has actually lost
work. They share a shape: a stage checkpoints a claim ("placed", "subtitles
done") that the disk does not support, and every later run trusts the claim
instead of the disk.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from blurayripper.config import load_settings
from blurayripper.models import (
    DiscInfo, EpisodeAssignment, MediaKind, Stream, StreamType, Title,
)
from blurayripper.pipeline import Job, JobState, Pipeline
from blurayripper.scheduler import EncodeScheduler
from blurayripper.services import library
from blurayripper.services.hdr import HdrInfo
from blurayripper.tools.mkvtools import SubtitleTrack


class _Report:
    """A reporter exposing every optional hook the pipeline sniffs for."""

    def __init__(self):
        self.events = []
        self.notes = []
        self.checkpoints = 0

    def on_event(self, m): self.events.append(m)
    def on_progress(self, *a): pass
    def notify(self, title, message): self.notes.append((title, message))
    def ejected(self, device): pass
    def checkpoint(self): self.checkpoints += 1

    def said(self, needle) -> bool:
        return any(needle in e for e in self.events)


class _GrainReport(_Report):
    """A reporter that can park at the grain A/B gate, like the web UI."""

    def __init__(self):
        super().__init__()
        self.gates = []

    async def resolve_grain(self, job, preview):
        self.gates.append(preview)
        return True


def _pipe(tmp_path, monkeypatch) -> Pipeline:
    """A Pipeline whose every write lands under tmp_path. load_settings() layers
    local config over the tracked TOML, so anything asserted on is pinned here."""
    s = load_settings()
    monkeypatch.setattr(s.storage, "staging_dir", tmp_path / "staging")
    monkeypatch.setattr(s.storage, "output_dir", tmp_path / "out")
    monkeypatch.setattr(s.storage, "path_map", [])
    monkeypatch.setattr(s, "write_nfo", False)
    monkeypatch.setattr(s, "rip_duration_tolerance_pct", 0)
    monkeypatch.setattr(s, "extract_cover_art", False)
    monkeypatch.setattr(s, "fetch_tmdb_artwork", False)
    monkeypatch.setattr(s, "auto_eject", False)
    monkeypatch.setattr(s, "tmdb_api_key", None)
    monkeypatch.setattr(s.languages, "keep", ["eng"])
    monkeypatch.setattr(s.languages, "default", "eng")
    monkeypatch.setattr(s.subtitles, "upload_export_dir", None)
    monkeypatch.setattr(s.cluster, "worker_does_subs", False)
    monkeypatch.setattr(s.encode, "uhd_min_height", 2160)
    return Pipeline(s)


def _staging_root(pipe: Pipeline) -> Path:
    return pipe.settings.storage.staging_dir / "job"


def _pgs(track_id=3, language="eng") -> SubtitleTrack:
    return SubtitleTrack(track_id=track_id, codec="S_HDMV/PGS", language=language,
                         is_pgs=True)


def _job(*, sub_langs=("eng",), titles=(0,)) -> Job:
    job = Job(source="disc:0")
    job.disc = DiscInfo(source="disc:0", disc_name="DISC LABEL", titles=[
        Title(title_id=t, duration_seconds=1711,
              streams=[Stream(stream_id=i, stream_type=StreamType.SUBTITLE,
                              language=lang)
                       for i, lang in enumerate(sub_langs)])
        for t in titles])
    job.fingerprint = "fp0123456789abcdef"
    job.selected_title_ids = list(titles)
    job.assignments = {t: EpisodeAssignment(t, MediaKind.MOVIE, f"Film {t}", 1999 + t)
                       for t in titles}
    return job


class _Rig:
    """Every external tool the pipeline drives, faked and recorded. Nothing here
    decides anything the tests assert on — the real pipeline logic runs against
    tmp_path."""

    def __init__(self, pipe, monkeypatch, *, tracks=(), height=1080,
                 ocr_fails=(), encode_bytes=None):
        self.pipe = pipe
        self.rips = []
        self.encodes = []       # (src, dst) HandBrake was handed
        self.ocr = []           # .sup files seconv was handed
        self._tracks = list(tracks)
        self.ocr_fails = set(ocr_fails)
        # Each encode writes DIFFERENT bytes, so "which encode is in the library"
        # is answerable — a call count alone cannot tell a reused file from a
        # silently redone one.
        self._encode_bytes = encode_bytes

        async def rip(source, title_id, rip_dir, on_progress=None, on_message=None):
            self.rips.append(title_id)
            rip_dir.mkdir(parents=True, exist_ok=True)
            out = rip_dir / f"title_t{title_id:02d}.mkv"
            out.write_text(f"ripped bytes for title {title_id}")
            return [out]

        async def list_drives(timeout=None):
            return [SimpleNamespace(index=0, device="D:", disc_name="DISC LABEL",
                                    makemkv_blind=False)]

        async def probe(path): return SimpleNamespace(duration_seconds=1711)
        async def video_height(path): return height
        async def identify(path): return list(self._tracks)

        async def extract_tracks(mkv, specs, *, on_progress=None):
            for _tid, out in specs:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text("PGS bitmaps")

        async def ocr_to_srt(sup, language, out_dir):
            self.ocr.append(Path(sup).name)
            if Path(sup).name in self.ocr_fails:
                raise RuntimeError("seconv: You must install or update .NET")
            out_dir.mkdir(parents=True, exist_ok=True)
            srt = out_dir / (Path(sup).stem + ".srt")
            srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello there.\n",
                           encoding="utf-8")
            return srt

        async def encode(src, dst, *, on_progress=None, tune_grain=False,
                         deinterlace=False):
            self.encodes.append((Path(src), Path(dst)))
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            if self._encode_bytes is not None:
                Path(dst).write_text(self._encode_bytes(len(self.encodes)))
            else:
                Path(dst).write_text("encoded video")

        async def encode_clip(src, dst, *, start, duration, tune_grain):
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            Path(dst).write_text("preview clip")

        monkeypatch.setattr(pipe.makemkv, "rip_title", rip)
        monkeypatch.setattr(pipe.makemkv, "list_drives", list_drives)
        monkeypatch.setattr(pipe.mkvtools, "probe", probe)
        monkeypatch.setattr(pipe.mkvtools, "video_height", video_height)
        monkeypatch.setattr(pipe.mkvtools, "identify_subtitles", identify)
        monkeypatch.setattr(pipe.mkvtools, "extract_tracks", extract_tracks)
        monkeypatch.setattr(pipe.subtitleedit, "ocr_to_srt", ocr_to_srt)
        monkeypatch.setattr(pipe.handbrake, "encode", encode)
        monkeypatch.setattr(pipe.handbrake, "encode_clip", encode_clip)
        monkeypatch.setattr(pipe.handbrake, "speed_summary", lambda: "preset")
        monkeypatch.setattr("blurayripper.pipeline.eject", lambda d: None)


# Captured at import, before any test patches it — a wrapper that called
# library.place by name would call whatever the last patch installed (itself).
_REAL_PLACE = library.place


def _break_video_place(monkeypatch, reason):
    """Make the move into the library fail for the VIDEO only — the shape of a
    full / read-only / dropped output share. Sidecars still place normally."""
    calls = []

    def place(src, dest, **kw):
        calls.append(Path(dest).name)
        if Path(dest).suffix == ".mkv":
            raise reason
        return _REAL_PLACE(src, dest, **kw)

    monkeypatch.setattr("blurayripper.pipeline.library.place", place)
    return calls


def _fix_place(monkeypatch):
    """The share comes back."""
    monkeypatch.setattr("blurayripper.pipeline.library.place", _REAL_PLACE)


async def _run(pipe, job, report):
    """Run the job the single-box path runs it, returning the failure (or None)."""
    try:
        await pipe.execute(job, EncodeScheduler(3), report, job_name="job")
    except Exception as exc:
        return exc
    return None


def _library_files(pipe) -> list[str]:
    root = pipe.settings.storage.movies_root
    return sorted(p.name for p in root.rglob("*") if p.is_file()) if root.exists() else []


# --- an encoder that exits 0 having written nothing -------------------------

async def test_an_empty_encode_is_never_placed_and_leaves_the_job_retryable(
        tmp_path, monkeypatch):
    """ENOSPC that the encoder does not notice: HandBrake exits 0 with a 0-byte
    file. Moving that into the library checkpointed "placed" on an empty video —
    and because every resume trusts that flag, the end-of-job check then failed
    on the same 0 bytes forever, with no way out but deleting the file by hand.
    """
    pipe = _pipe(tmp_path, monkeypatch)
    rig = _Rig(pipe, monkeypatch, tracks=(),
               encode_bytes=lambda n: "" if n == 1 else "a real encode")
    job = _job(sub_langs=())
    a = job.assignments[0]
    r = _Report()

    with pytest.raises(RuntimeError) as first:
        await pipe._encode_title(job, 0, a, _staging_root(pipe),
                                 tmp_path / "rip.mkv", EncodeScheduler(3), r)

    # Nothing empty in the library, and no claim that anything was placed.
    assert _library_files(pipe) == []
    assert not job.checkpoints[0].get("placed")
    assert not r.said("Placed video:")
    # The message names the volume that filled, not the encoder or the disc.
    assert "produced an empty file" in str(first.value)
    assert "staging disk filling up" in str(first.value)

    # Space freed: the retry encodes again and finishes. (The old build skipped
    # the encode entirely here, because "placed" was already set.)
    r2 = _Report()
    result = await pipe._encode_title(job, 0, a, _staging_root(pipe),
                                      tmp_path / "rip.mkv", EncodeScheduler(3), r2)
    assert result.video_path.read_text() == "a real encode"
    assert job.checkpoints[0]["placed"] is True


async def test_a_zero_byte_video_already_in_the_library_is_re_encoded_not_skipped(
        tmp_path, monkeypatch):
    """The state a box can already be in from the build above: an empty video in
    the library and "placed" in the checkpoints. exists() is true for a 0-byte
    file, so the resume shortcut fired and the job re-failed identically on every
    Retry. It has to re-encode over it instead."""
    pipe = _pipe(tmp_path, monkeypatch)
    rig = _Rig(pipe, monkeypatch, tracks=())
    job = _job(sub_langs=())
    a = job.assignments[0]
    video = pipe._video_path_for(a)
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"")                       # what the full disk left behind
    job.checkpoints[0] = {"placed": True}
    r = _Report()

    result = await pipe._process_one(job, 0, a, _staging_root(pipe),
                                     EncodeScheduler(3), r)
    job.results = [result]

    assert not r.said("already done (resume)"), r.events
    assert rig.encodes and result.video_path.read_text() == "encoded video"
    assert pipe._verify_outputs(job) == []       # the job can go green again


# --- a failure while PLACING is not a failure while encoding ----------------

async def test_a_share_that_refuses_the_move_is_reported_as_placing_not_encoding(
        tmp_path, monkeypatch):
    """The encode succeeded; the output share did not accept it. Reported as
    "FAILED during encoding" that sends you after tools, CPU and staging space —
    the opposite of the fix (permissions / free space on the SHARE)."""
    pipe = _pipe(tmp_path, monkeypatch)
    rig = _Rig(pipe, monkeypatch, tracks=(_pgs(),))
    _break_video_place(monkeypatch, PermissionError(13, "Access is denied"))
    job = _job()
    r = _Report()

    failure = await _run(pipe, job, r)

    assert isinstance(failure, PermissionError)
    assert job.state is JobState.FAILED
    assert r.said("FAILED during placing:"), r.events
    assert not r.said("FAILED during encoding"), r.events


async def test_a_finished_encode_survives_a_failed_place_instead_of_being_redone(
        tmp_path, monkeypatch):
    """Hours of x265 for a share that was read-only for a minute. The finished
    file is still in staging (staging is only swept on success), so the retry
    must place THAT file, not spend the whole encode again."""
    pipe = _pipe(tmp_path, monkeypatch)
    rig = _Rig(pipe, monkeypatch, tracks=(_pgs(),),
               encode_bytes=lambda n: f"x265 output of encode #{n}")
    _break_video_place(monkeypatch, OSError(30, "Read-only file system"))
    job = _job()

    assert isinstance(await _run(pipe, job, _Report()), OSError)
    assert len(rig.encodes) == 1
    encoded = _staging_root(pipe) / "encode" / "Film 0 (1999).mkv"
    assert encoded.exists()                      # kept for the retry

    # Share writable again.
    _fix_place(monkeypatch)
    r2 = _Report()
    assert await _run(pipe, job, r2) is None

    assert job.state is JobState.DONE
    assert len(rig.encodes) == 1, "the finished encode was thrown away"
    placed = pipe._video_path_for(job.assignments[0])
    assert placed.read_text() == "x265 output of encode #1"
    assert r2.said("Reusing the finished encode"), r2.events


async def test_a_grain_change_before_the_retry_beats_the_kept_encode(
        tmp_path, monkeypatch):
    """The control for the reuse above: reusing a file encoded with the OTHER
    grain setting would silently ignore the choice the operator just made."""
    pipe = _pipe(tmp_path, monkeypatch)
    rig = _Rig(pipe, monkeypatch, tracks=(),
               encode_bytes=lambda n: f"encode #{n}")
    _break_video_place(monkeypatch, OSError(30, "Read-only file system"))
    job = _job(sub_langs=())
    a = job.assignments[0]
    job.checkpoints[0] = {"tune_grain": False}   # grain resolved by the prep phase
    r = _Report()

    with pytest.raises(OSError):
        await pipe._encode_title(job, 0, a, _staging_root(pipe),
                                 tmp_path / "rip.mkv", EncodeScheduler(3), r)
    assert job.checkpoints[0]["encoded_grain"] is False

    job.checkpoints[0]["tune_grain"] = True      # what set_grain() does in the UI
    _fix_place(monkeypatch)
    result = await pipe._encode_title(job, 0, a, _staging_root(pipe),
                                      tmp_path / "rip.mkv", EncodeScheduler(3),
                                      _Report())

    assert len(rig.encodes) == 2, "the retry shipped the old grain setting"
    assert result.video_path.read_text() == "encode #2"


# --- the HDR check has to compare values, not presence ----------------------

async def test_an_output_tagged_sdr_fails_the_hdr_check_with_nothing_missing(
        tmp_path, monkeypatch):
    """An HLG source carries no mastering-display / max-cll for a presence check
    to miss, and a stripped output has colorprim/transfer/matrix all PRESENT —
    just bt709. The check called that a pass and printed "output carries SDR" as
    the success line, which is exactly the loss it exists to catch."""
    pipe = _pipe(tmp_path, monkeypatch)
    want = HdrInfo(colorprim="bt2020", transfer="arib-std-b67", colormatrix="bt2020nc")
    stripped = HdrInfo(colorprim="bt709", transfer="bt709", colormatrix="bt709")

    async def probe(dst): return stripped, 0.0
    monkeypatch.setattr(pipe.ffmpeg, "probe_video", probe)
    r = _Report()

    await pipe._verify_hdr(tmp_path / "out.mkv", want, r)

    assert not r.said("HDR check passed"), r.events
    failure = next(e for e in r.events if "HDR CHECK FAILED" in e)
    # Named individually, with both values, so the log says what was lost.
    assert "transfer (arib-std-b67 → bt709)" in failure
    assert "primaries (bt2020 → bt709)" in failure


async def test_an_output_whose_mastering_values_changed_fails_the_hdr_check(
        tmp_path, monkeypatch):
    """Same trap one level down: master-display and max-cll present but carrying
    different numbers is a re-tagged file, not a preserved one."""
    pipe = _pipe(tmp_path, monkeypatch)
    want = HdrInfo(colorprim="bt2020", transfer="smpte2084", colormatrix="bt2020nc",
                   master_display="G(8500,39850)L(10000000,50)", max_cll="1000,400")
    rewritten = HdrInfo(colorprim="bt2020", transfer="smpte2084",
                        colormatrix="bt2020nc",
                        master_display="G(13250,34500)L(4000000,50)",
                        max_cll="1000,400")

    async def probe(dst): return rewritten, 0.0
    monkeypatch.setattr(pipe.ffmpeg, "probe_video", probe)
    r = _Report()

    await pipe._verify_hdr(tmp_path / "out.mkv", want, r)

    assert not r.said("HDR check passed"), r.events
    assert r.said("master-display (G(8500,39850)L(10000000,50) → "
                  "G(13250,34500)L(4000000,50))"), r.events


async def test_an_output_that_kept_every_hdr_value_still_passes(tmp_path, monkeypatch):
    """The control: a value comparison that failed everything would be useless.
    A faithful HDR10 round-trip must still read as a pass."""
    pipe = _pipe(tmp_path, monkeypatch)
    want = HdrInfo(colorprim="bt2020", transfer="smpte2084", colormatrix="bt2020nc",
                   master_display="G(8500,39850)L(10000000,50)", max_cll="1000,400")

    async def probe(dst): return want, 0.0
    monkeypatch.setattr(pipe.ffmpeg, "probe_video", probe)
    r = _Report()

    await pipe._verify_hdr(tmp_path / "out.mkv", want, r)

    assert r.said("HDR check passed"), r.events
    assert not r.said("HDR CHECK FAILED"), r.events


# --- resumes that claimed a title was finished when it was not --------------

async def test_a_resume_whose_recorded_sidecars_are_gone_re_ocrs_them(
        tmp_path, monkeypatch):
    """"placed" plus a subs_done record made the resume report "already done",
    hand back the recorded paths, and — because it came from a checkpoint — skip
    the check that the disc's subtitles produced anything. A title whose sidecars
    had been deleted therefore went green with nothing for Plex to read."""
    pipe = _pipe(tmp_path, monkeypatch)
    rig = _Rig(pipe, monkeypatch, tracks=(_pgs(),))
    job = _job()
    a = job.assignments[0]
    r = _Report()

    # Run it once, properly: video + sidecar placed, both recorded.
    first = await pipe._process_one(job, 0, a, _staging_root(pipe),
                                    EncodeScheduler(3), r)
    srt = first.subtitle_paths[0]
    assert srt.exists() and job.checkpoints[0]["subs_done"]

    srt.unlink()                                 # ...and then it was deleted
    r2 = _Report()
    result = await pipe._process_one(job, 0, a, _staging_root(pipe),
                                     EncodeScheduler(3), r2)
    job.results = [result]

    assert not r2.said("already done (resume)"), r2.events
    assert srt.exists() and srt in result.subtitle_paths
    assert pipe._verify_outputs(job) == []
    # The recovery costs neither the disc nor the encode: the staged rip is
    # re-used and the placed video is left alone.
    assert rig.rips == [0] and len(rig.encodes) == 1


async def test_a_title_placed_with_its_ocr_broken_re_runs_the_ocr_on_retry(
        tmp_path, monkeypatch):
    """The encode carries on when OCR dies, so the title ends up placed with no
    sidecars and no subs_done record. The resume then skipped everything and —
    trusting the checkpoint — reported the job DONE with no subtitles at all,
    which is the job-42 incident this pipeline was written around."""
    pipe = _pipe(tmp_path, monkeypatch)
    rig = _Rig(pipe, monkeypatch, tracks=(_pgs(),),
               ocr_fails=("title_t00.t3.eng.sup",))
    job = _job()
    a = job.assignments[0]

    failure = await _run(pipe, job, _Report())
    assert "no .srt was produced" in str(failure)     # the first run is honest
    assert job.checkpoints[0]["placed"] is True
    assert "subs_done" not in job.checkpoints[0]

    rig.ocr_fails.clear()                             # seconv reinstalled
    r2 = _Report()
    assert await _run(pipe, job, r2) is None

    assert job.state is JobState.DONE
    assert _library_files(pipe) == ["Film 0 (1999).en.default.srt", "Film 0 (1999).mkv"]
    assert rig.ocr == ["title_t00.t3.eng.sup"] * 2    # re-OCR'd, from the kept .sup
    assert rig.rips == [0] and len(rig.encodes) == 1  # no re-rip, no re-encode


async def test_a_legacy_placed_title_with_no_subtitle_record_is_still_skipped(
        tmp_path, monkeypatch):
    """The limit of the two tests above. A title placed before subtitle
    checkpoints existed has no record either way; the run that placed it checked
    it, so re-doing its subtitles would re-rip every old title a resume touches."""
    pipe = _pipe(tmp_path, monkeypatch)
    rig = _Rig(pipe, monkeypatch, tracks=(_pgs(),))
    job = _job()
    a = job.assignments[0]
    video = pipe._video_path_for(a)
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_text("placed by a much older build")
    job.checkpoints[0] = {"placed": True}
    r = _Report()

    result = await pipe._process_one(job, 0, a, _staging_root(pipe),
                                     EncodeScheduler(3), r)
    job.results = [result]

    assert r.said("Title 0 already done (resume) — skipping."), r.events
    assert rig.rips == [] and rig.encodes == [] and rig.ocr == []
    assert pipe._verify_outputs(job) == []


# --- one subtitle cache per film -------------------------------------------

def test_two_films_under_a_long_library_root_keep_separate_subtitle_caches(
        tmp_path, monkeypatch):
    """The cache key slugged the FULL destination path and kept 40 characters,
    which on a real library root ('/mnt/TrueNas/media/Plex/Media/Library/…')
    is entirely the shared prefix. Every film collapsed onto one directory, so a
    re-OCR handed one film's .sup tracks to another and announced it as
    "Reusing N cached subtitle track(s) — no re-rip needed"."""
    pipe = _pipe(tmp_path, monkeypatch)
    root = tmp_path / "mnt" / "TrueNas" / "media" / "Plex" / "Media" / "Library"
    blade = root / "Movies" / "Blade Runner (1982)" / "Blade Runner (1982).mkv"
    wargames = root / "Movies" / "WarGames (1983)" / "WarGames (1983).mkv"
    sup = tmp_path / "blade.eng.sup"
    sup.write_text("Blade Runner's PGS bitmaps")

    pipe._cache_subs(blade, [(sup, _pgs())])

    assert pipe._cached_subs(wargames) is None      # not Blade Runner's tracks
    cached = pipe._cached_subs(blade)
    assert cached and cached[0][0].read_text() == "Blade Runner's PGS bitmaps"


# --- the grain gate is a decision about the DISC ---------------------------

async def test_the_grain_preview_gate_opens_once_per_disc_not_once_per_episode(
        tmp_path, monkeypatch):
    """A season disc ran the whole rip/preview/encode cycle per episode, and the
    gate is a modal: picking grain for episode 1 just re-opened it for episode 2,
    which reads as an endless "pick grain" loop. The titles share one source and
    one encode, so the answer is the same for all of them."""
    pipe = _pipe(tmp_path, monkeypatch)
    rig = _Rig(pipe, monkeypatch, tracks=())
    job = Job(source="disc:0")
    job.disc = DiscInfo(source="disc:0", disc_name="SHOW S1D1", titles=[
        Title(title_id=t, duration_seconds=1400, streams=[]) for t in (0, 1)])
    job.selected_title_ids = [0, 1]
    job.assignments = {
        t: EpisodeAssignment(t, MediaKind.TV, "Show", 2001, season=1, episode=t + 1)
        for t in (0, 1)}
    job.grain_mode = "preview"
    r = _GrainReport()

    await pipe._process_titles(job, _staging_root(pipe), EncodeScheduler(3), r)

    assert len(r.gates) == 1, "the grain gate re-opened for the second episode"
    # ...and the one answer was applied to both episodes, not defaulted for one.
    assert [job.checkpoints[t]["tune_grain"] for t in (0, 1)] == [True, True]
    assert len(rig.encodes) == 2


async def test_a_file_job_rebuilds_its_lost_checkpoint_from_its_own_source(tmp_path):
    """The repair path for the 121 jobs already damaged. For a job added from a
    file, the 'ripped file' and the source ARE the same file — so a missing
    checkpoint can be rebuilt rather than being terminal. Without this, anything
    that dropped the checkpoint killed the job for good: there is no disc, so
    the raise is final and Retry cannot help."""
    from blurayripper.config import load_settings
    from blurayripper.pipeline import Job, JobState, Pipeline, configure_path_map

    src = tmp_path / "To Process" / "Elemental (2023).mkv"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"a real movie")

    s = load_settings()
    s.storage.staging_dir = tmp_path / "stage"
    s.storage.output_dir = tmp_path / "out"
    configure_path_map([])
    pipe = Pipeline(s)

    job = Job(source=str(src))
    job.state = JobState.READY
    job.selected_title_ids = [0]
    job.checkpoints = {0: {}}          # the checkpoint the sweep wrongly cleared

    events: list[str] = []

    class Report:
        def on_event(self, msg): events.append(msg)
        def on_progress(self, *a, **k): pass
        def checkpoint(self): pass

    ripped = await pipe._ensure_ripped(job, 0, tmp_path / "stage", Report())

    assert ripped == src
    assert any("recovered it from the job's own source" in e for e in events), events
    # and it is written back, so the repair is not re-done on every run
    assert job.checkpoints[0].get("ripped_path")


async def test_a_file_job_whose_source_really_is_gone_still_says_so(tmp_path):
    """The rebuild must not paper over real loss — the message has to stay
    truthful for a file that genuinely is not there."""
    import pytest as _pytest

    from blurayripper.config import load_settings
    from blurayripper.pipeline import Job, JobState, Pipeline, configure_path_map

    s = load_settings()
    s.storage.staging_dir = tmp_path / "stage"
    s.storage.output_dir = tmp_path / "out"
    configure_path_map([])
    pipe = Pipeline(s)

    job = Job(source=str(tmp_path / "vanished.mkv"))
    job.state = JobState.READY
    job.selected_title_ids = [0]
    job.checkpoints = {0: {}}

    class Report:
        def on_event(self, *a, **k): pass
        def on_progress(self, *a, **k): pass
        def checkpoint(self): pass

    with _pytest.raises(RuntimeError, match="source file is gone"):
        await pipe._ensure_ripped(job, 0, tmp_path / "stage", Report())


async def test_a_requeued_truncated_rip_is_caught_before_it_is_encoded(tmp_path):
    """Requeuing a job that failed on a bad disc read reuses whatever MakeMKV
    managed to write. Only the RIP path checked the length, so a resumed
    half-title went straight to the encoder and into the library — full size on
    disk, missing its last half hour. One probe beats a multi-hour encode."""
    import pytest as _pytest

    from blurayripper.config import load_settings
    from blurayripper.models import DiscInfo, Title
    from blurayripper.pipeline import Job, JobState, Pipeline, configure_path_map

    rip = tmp_path / "stage" / "rip" / "t000" / "x.mkv"
    rip.parent.mkdir(parents=True)
    rip.write_bytes(b"half a movie")

    s = load_settings()
    s.storage.staging_dir = tmp_path / "stage"
    s.storage.output_dir = tmp_path / "out"
    s.rip_duration_tolerance_pct = 3
    configure_path_map([])
    pipe = Pipeline(s)

    class Probe:
        duration_seconds = 1800          # 30 min of a 2-hour title
    async def probe(_p): return Probe()
    pipe.mkvtools.probe = probe

    job = Job(source="disc:0")
    job.state = JobState.READY
    job.selected_title_ids = [0]
    job.checkpoints = {0: {"ripped_path": "rip/t000/x.mkv"}}
    job.disc = DiscInfo(source="disc:0", disc_name="X", titles=[
        Title(title_id=0, duration_seconds=7200, size_bytes=1,
              source_filename="t00.mkv", streams=[])])

    class Report:
        def on_event(self, *a, **k): pass
        def on_progress(self, *a, **k): pass
        def checkpoint(self): pass

    with _pytest.raises(RuntimeError, match="looks truncated"):
        await pipe._ensure_ripped(job, 0, tmp_path / "stage", Report())


async def test_a_missing_prober_is_not_reported_as_a_corrupt_rip(tmp_path):
    """No mkvmerge installed is a configuration problem, not a verdict on the
    file. Failing every rip on a box that simply lacks the tool would report
    data corruption for a missing package."""
    from blurayripper.config import ToolNotFoundError, load_settings
    from blurayripper.models import DiscInfo, Title
    from blurayripper.pipeline import Job, JobState, Pipeline, configure_path_map

    rip = tmp_path / "stage" / "rip" / "t000" / "x.mkv"
    rip.parent.mkdir(parents=True)
    rip.write_bytes(b"a movie")

    s = load_settings()
    s.storage.staging_dir = tmp_path / "stage"
    s.storage.output_dir = tmp_path / "out"
    s.rip_duration_tolerance_pct = 3
    configure_path_map([])
    pipe = Pipeline(s)

    async def no_tool(_p): raise ToolNotFoundError("mkvmerge")
    pipe.mkvtools.probe = no_tool

    job = Job(source="disc:0")
    job.state = JobState.READY
    job.selected_title_ids = [0]
    job.checkpoints = {0: {"ripped_path": "rip/t000/x.mkv"}}
    job.disc = DiscInfo(source="disc:0", disc_name="X", titles=[
        Title(title_id=0, duration_seconds=7200, size_bytes=1,
              source_filename="t00.mkv", streams=[])])

    said: list[str] = []

    class Report:
        def on_event(self, msg): said.append(msg)
        def on_progress(self, *a, **k): pass
        def checkpoint(self): pass

    got = await pipe._ensure_ripped(job, 0, tmp_path / "stage", Report())
    assert got == rip                                   # not failed
    assert any("NOT verified" in m for m in said), said  # but not silent either
