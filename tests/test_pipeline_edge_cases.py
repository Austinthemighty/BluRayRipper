"""Pipeline branch decisions that silently change what lands in the library.

pipeline.py is well line-covered but branch-thin, and the untaken branches are
mostly the ones that decide OUTPUT: which sidecar name a subtitle track gets,
whether a UHD title keeps its HDR, which grain setting reaches the encoder,
which drive is ejected, which folder gets which artwork. Every test here pins
the resulting decision (a path, a call, a flag) rather than the log line beside
it — a wrong decision here still "succeeds" and is only noticed on the couch.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, EpisodeAssignment, MediaKind, Title
from blurayripper import pipeline as pipeline_mod
from blurayripper.pipeline import Job, Pipeline, TitleResult
from blurayripper.scheduler import EncodeScheduler
from blurayripper.services.coverart import DiscArt
from blurayripper.services.hdr import HdrInfo
from blurayripper.tools.mkvtools import SubtitleTrack


# --- shared doubles -------------------------------------------------------

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
    monkeypatch.setattr(s, "rip_duration_tolerance_pct", 0)
    monkeypatch.setattr(s, "tmdb_api_key", tmdb_key)  # read by TMDBClient at build time
    return Pipeline(s)


def _ffmpeg_installed(pipe, monkeypatch, present: bool) -> None:
    """FFmpeg.available is derived from the configured path (and shutil.which), so
    presence is faked by pointing it at a real binary — or at nothing."""
    monkeypatch.setattr(pipe.settings.tools, "ffmpeg", sys.executable if present else None)


def _movie(title="The Film", year=1999) -> EpisodeAssignment:
    return EpisodeAssignment(0, MediaKind.MOVIE, title, year)


def _track(track_id=1, language="eng", *, forced=False, sdh=False) -> SubtitleTrack:
    return SubtitleTrack(track_id=track_id, codec="S_HDMV/PGS", language=language,
                         is_pgs=True, forced=forced, sdh=sdh)


def _srt(tmp_path, name, cues=None) -> Path:
    """A real .srt file on disk (placement MOVES it, so it must exist)."""
    cues = cues or ["Hello there."]
    blocks = [f"{i}\n00:00:{i * 2:02d},000 --> 00:00:{i * 2 + 1:02d},000\n{t}\n"
              for i, t in enumerate(cues, 1)]
    p = tmp_path / "ocr" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(blocks), encoding="utf-8")
    return p


def _sdh_cues() -> list[str]:
    """Long enough to be classified at all, with enough sound cues to read SDH."""
    lines = [f"Line of dialogue number {i}." for i in range(80)]
    for i in range(0, 80, 8):
        lines[i] = "[explosion]"
    lines[1] = "MAN: Get down!"
    return lines


def _plain_cues() -> list[str]:
    return [f"Line of dialogue number {i}." for i in range(80)]


# --- subtitle sidecar naming ---------------------------------------------
# The sidecar name is what Plex reads: get it wrong and the wrong track becomes
# the default, or two tracks collide and one is lost.

async def test_default_flag_goes_only_to_the_configured_default_language(tmp_path, monkeypatch):
    """The .default sidecar is the one Plex turns on by itself. Flagging a
    foreign track default means every playback starts with French subtitles."""
    pipe = _pipe(tmp_path, monkeypatch)
    video = tmp_path / "lib" / "The Film (1999).mp4"
    pairs = [(_srt(tmp_path, "en.srt"), _track(1, "eng")),
             (_srt(tmp_path, "fr.srt"), _track(2, "fre"))]
    result = TitleResult(title_id=0)
    await pipe._place_title_subs(result, video, pairs, _Report())
    assert [p.name for p in result.subtitle_paths] == [
        "The Film (1999).en.default.srt", "The Film (1999).fr.srt"]


async def test_second_plain_track_of_a_language_is_indexed_not_overwritten(tmp_path, monkeypatch):
    """Two plain English tracks (commentary vs dialogue) must land on separate
    paths — without the index they resolve to the same name and one is lost."""
    pipe = _pipe(tmp_path, monkeypatch)
    video = tmp_path / "lib" / "The Film (1999).mp4"
    pairs = [(_srt(tmp_path, "a.srt"), _track(1, "eng")),
             (_srt(tmp_path, "b.srt"), _track(2, "eng")),
             (_srt(tmp_path, "c.srt"), _track(3, "eng"))]
    result = TitleResult(title_id=0)
    await pipe._place_title_subs(result, video, pairs, _Report())
    names = [p.name for p in result.subtitle_paths]
    assert names == ["The Film (1999).en.default.srt",
                     "The Film (1999).en.2.srt",
                     "The Film (1999).en.3.srt"]
    assert all(p.exists() for p in result.subtitle_paths)   # nothing overwritten


async def test_forced_and_sdh_tracks_do_not_consume_the_plain_index(tmp_path, monkeypatch):
    """Forced/SDH tracks are named by their flag, so they must not push the real
    dialogue track onto an index (and rob it of .default)."""
    pipe = _pipe(tmp_path, monkeypatch)
    video = tmp_path / "lib" / "The Film (1999).mp4"
    pairs = [(_srt(tmp_path, "f.srt"), _track(1, "eng", forced=True)),
             (_srt(tmp_path, "s.srt", _plain_cues()), _track(2, "eng", sdh=True)),
             (_srt(tmp_path, "p.srt", _plain_cues()), _track(3, "eng"))]
    result = TitleResult(title_id=0)
    await pipe._place_title_subs(result, video, pairs, _Report())
    assert [p.name for p in result.subtitle_paths] == [
        "The Film (1999).en.forced.srt",
        "The Film (1999).en.sdh.srt",
        "The Film (1999).en.default.srt"]


async def test_sdh_is_detected_from_content_when_the_pgs_flag_is_missing(tmp_path, monkeypatch):
    """Blu-ray PGS rarely carries the SDH flag. Undetected, the hearing-impaired
    track becomes the .default one and every playback shows [door creaks]."""
    pipe = _pipe(tmp_path, monkeypatch)
    video = tmp_path / "lib" / "The Film (1999).mp4"
    pairs = [(_srt(tmp_path, "sdh.srt", _sdh_cues()), _track(1, "eng")),
             (_srt(tmp_path, "plain.srt", _plain_cues()), _track(2, "eng"))]
    result = TitleResult(title_id=0)
    await pipe._place_title_subs(result, video, pairs, _Report())
    assert [p.name for p in result.subtitle_paths] == [
        "The Film (1999).en.sdh.srt", "The Film (1999).en.default.srt"]


async def test_a_forced_track_is_never_content_sniffed_into_sdh(tmp_path, monkeypatch):
    """Forced subtitles are short bracketed cues ([in Russian]) — exactly what the
    SDH heuristic looks for. The forced flag wins, or Plex loses the forced track."""
    pipe = _pipe(tmp_path, monkeypatch)
    video = tmp_path / "lib" / "The Film (1999).mp4"
    pairs = [(_srt(tmp_path, "f.srt", _sdh_cues()), _track(1, "eng", forced=True))]
    result = TitleResult(title_id=0)
    await pipe._place_title_subs(result, video, pairs, _Report())
    assert result.subtitle_paths[0].name == "The Film (1999).en.forced.srt"


async def test_an_unreadable_srt_is_still_placed_as_a_plain_track(tmp_path, monkeypatch):
    """The SDH sniff reads the OCR output; if that read fails (a share that went
    away mid-job) the sidecar must still be placed, not lost to an OSError."""
    pipe = _pipe(tmp_path, monkeypatch)
    video = tmp_path / "lib" / "The Film (1999).mp4"
    unreadable = tmp_path / "ocr" / "dir.srt"
    unreadable.mkdir(parents=True)          # read_text -> IsADirectoryError (OSError)
    result = TitleResult(title_id=0)
    await pipe._place_title_subs(result, video, [(unreadable, _track(1, "eng"))], _Report())
    assert result.subtitle_paths[0].name == "The Film (1999).en.default.srt"


async def test_upload_export_probes_the_source_video_once_for_all_tracks(tmp_path, monkeypatch):
    """FPS/height come from one mkvmerge probe of the rip. Probing per track would
    re-read a multi-GB NAS file for every subtitle on the disc."""
    pipe = _pipe(tmp_path, monkeypatch)
    monkeypatch.setattr(pipe.settings.subtitles, "upload_export_dir", tmp_path / "upload")
    source = tmp_path / "rip.mkv"
    source.write_text("x")
    probes = []

    async def dims(path):
        probes.append(Path(path))
        return 1080, 23.976
    monkeypatch.setattr(pipe.mkvtools, "video_dims", dims)

    exported = []
    monkeypatch.setattr("blurayripper.pipeline.os_upload.export_subtitle",
                        lambda *a, **k: exported.append((k["height"], k["fps"])))

    pairs = [(_srt(tmp_path, f"{i}.srt"), _track(i, "eng")) for i in (1, 2, 3)]
    await pipe._place_title_subs(TitleResult(title_id=0), tmp_path / "lib" / "F.mp4",
                                 pairs, _Report(), source_video=source,
                                 assignment=_movie())
    assert probes == [source]                       # once, not once per track
    assert exported == [(1080, 23.976)] * 3         # the probe reached every sheet


async def test_a_failed_dimension_probe_still_exports_every_sidecar(tmp_path, monkeypatch):
    """A dead mkvmerge must cost the FPS field on the upload sheet, not the whole
    export (the sidecars are already correct on disk)."""
    pipe = _pipe(tmp_path, monkeypatch)
    monkeypatch.setattr(pipe.settings.subtitles, "upload_export_dir", tmp_path / "upload")
    source = tmp_path / "rip.mkv"
    source.write_text("x")

    async def boom(path): raise OSError("mkvmerge died")
    monkeypatch.setattr(pipe.mkvtools, "video_dims", boom)

    exported = []
    monkeypatch.setattr("blurayripper.pipeline.os_upload.export_subtitle",
                        lambda *a, **k: exported.append((k["height"], k["fps"])))
    r = _Report()
    await pipe._place_title_subs(TitleResult(title_id=0), tmp_path / "lib" / "F.mp4",
                                 [(_srt(tmp_path, "a.srt"), _track(1, "eng"))], r,
                                 source_video=source, assignment=_movie())
    assert exported == [(None, None)]
    assert r.said("FPS/HD left blank")


async def test_one_failed_export_does_not_stop_the_remaining_sidecars(tmp_path, monkeypatch):
    """The export is a courtesy copy. A write failure on one track must not
    abandon the tracks after it — those sidecars are the actual deliverable."""
    pipe = _pipe(tmp_path, monkeypatch)
    monkeypatch.setattr(pipe.settings.subtitles, "upload_export_dir", tmp_path / "upload")
    seen = []

    def flaky(export_dir, srt_dest, **k):
        seen.append(srt_dest.name)
        if len(seen) == 1:
            raise OSError("export share is full")
    monkeypatch.setattr("blurayripper.pipeline.os_upload.export_subtitle", flaky)

    result = TitleResult(title_id=0)
    pairs = [(_srt(tmp_path, "a.srt"), _track(1, "eng")),
             (_srt(tmp_path, "b.srt"), _track(2, "fre"))]
    await pipe._place_title_subs(result, tmp_path / "lib" / "F.mp4", pairs, _Report(),
                                 assignment=_movie())
    assert len(seen) == 2                                   # kept going
    assert [p.name for p in result.subtitle_paths] == ["F.en.default.srt", "F.fr.srt"]
    assert all(p.exists() for p in result.subtitle_paths)


async def test_export_is_skipped_without_a_naming_assignment(tmp_path, monkeypatch):
    """``resubtitle`` places sidecars with no assignment; the upload sheet needs a
    title/year, so it must be skipped rather than built from nothing."""
    pipe = _pipe(tmp_path, monkeypatch)
    monkeypatch.setattr(pipe.settings.subtitles, "upload_export_dir", tmp_path / "upload")
    calls = []
    monkeypatch.setattr("blurayripper.pipeline.os_upload.export_subtitle",
                        lambda *a, **k: calls.append(1))
    result = TitleResult(title_id=0)
    await pipe._place_title_subs(result, tmp_path / "lib" / "F.mp4",
                                 [(_srt(tmp_path, "a.srt"), _track(1, "eng"))],
                                 _Report(), assignment=None)
    assert calls == []
    assert result.subtitle_paths[0].exists()   # the sidecar itself still lands


# --- grain resolution -----------------------------------------------------

def _grain_job(mode="auto", tune=None) -> Job:
    job = Job(source="disc:0", grain_mode=mode, tune_grain=tune)
    job.disc = DiscInfo(source="disc:0", disc_name="X",
                        titles=[Title(title_id=0, duration_seconds=3000)])
    return job


async def test_forced_grain_modes_beat_the_configured_default(tmp_path, monkeypatch):
    """"on"/"off" are the user's explicit pick for this disc; the config default
    must not leak back in and re-grain (or de-grain) the encode."""
    pipe = _pipe(tmp_path, monkeypatch)
    clips = []
    monkeypatch.setattr(pipe.handbrake, "encode_clip",
                        lambda *a, **k: clips.append(1))

    monkeypatch.setattr(pipe.settings.encode, "tune_grain", False)
    assert await pipe._resolve_grain(_grain_job("on"), Path("r.mkv"), 0, tmp_path, _Report()) is True
    monkeypatch.setattr(pipe.settings.encode, "tune_grain", True)
    assert await pipe._resolve_grain(_grain_job("off"), Path("r.mkv"), 0, tmp_path, _Report()) is False
    assert clips == []   # forced modes never render a preview


async def test_auto_grain_prefers_the_per_job_override(tmp_path, monkeypatch):
    """A per-job grain choice made in the UI has to survive into the encode; the
    config default only applies when the job has none."""
    pipe = _pipe(tmp_path, monkeypatch)
    monkeypatch.setattr(pipe.settings.encode, "tune_grain", False)
    assert await pipe._resolve_grain(_grain_job("auto", tune=True), Path("r.mkv"), 0,
                                     tmp_path, _Report()) is True
    assert await pipe._resolve_grain(_grain_job("auto"), Path("r.mkv"), 0,
                                     tmp_path, _Report()) is False


async def test_preview_mode_without_a_ui_resolver_uses_the_default(tmp_path, monkeypatch):
    """A CLI run can't show an A/B preview. It must fall through to the default
    instead of rendering clips nobody will ever see (or hanging on a gate)."""
    pipe = _pipe(tmp_path, monkeypatch)
    monkeypatch.setattr(pipe.settings.encode, "tune_grain", True)
    clips = []
    monkeypatch.setattr(pipe.handbrake, "encode_clip", lambda *a, **k: clips.append(1))
    r = _Report()   # a CLI reporter: no resolve_grain attribute
    assert await pipe._resolve_grain(_grain_job("preview"), Path("r.mkv"), 0, tmp_path, r) is True
    assert clips == []
    assert r.said("needs the web UI")


async def test_preview_returns_the_choice_the_ui_made(tmp_path, monkeypatch):
    """The whole point of the gate: the human's pick — not the default — is what
    the encoder gets, and it is offered both rendered clips to judge."""
    pipe = _pipe(tmp_path, monkeypatch)
    monkeypatch.setattr(pipe.settings.encode, "tune_grain", True)
    _ffmpeg_installed(pipe, monkeypatch, False)

    async def clip(src, dst, *, start, duration, tune_grain):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("grain" if tune_grain else "smooth")
    monkeypatch.setattr(pipe.handbrake, "encode_clip", clip)

    shown = []
    r = _Report()

    async def resolve(job, preview):
        shown.append(preview)
        return False                       # the user picks "grain off"
    r.resolve_grain = resolve

    assert await pipe._resolve_grain(_grain_job("preview"), tmp_path / "r.mkv", 0,
                                     tmp_path, r) is False
    assert shown[0].off_clip.read_text() == "smooth"
    assert shown[0].on_clip.read_text() == "grain"


async def test_a_broken_preview_falls_back_instead_of_failing_the_job(tmp_path, monkeypatch):
    """HandBrake dying on a 4-second sample must not lose the whole rip — the
    encode continues with the default grain setting."""
    pipe = _pipe(tmp_path, monkeypatch)
    monkeypatch.setattr(pipe.settings.encode, "tune_grain", True)

    async def boom(*a, **k): raise RuntimeError("HandBrakeCLI exited 3")
    monkeypatch.setattr(pipe.handbrake, "encode_clip", boom)
    r = _Report()
    r.resolve_grain = lambda job, preview: None   # never reached
    assert await pipe._resolve_grain(_grain_job("preview"), tmp_path / "r.mkv", 0,
                                     tmp_path, r) is True
    assert r.said("Grain preview failed")


async def test_preview_samples_a_third_of_the_way_into_the_title(tmp_path, monkeypatch):
    """Sampling at the start would compare studio logos and black frames, where
    grain is invisible — the A/B pick would be meaningless."""
    pipe = _pipe(tmp_path, monkeypatch)
    _ffmpeg_installed(pipe, monkeypatch, False)
    starts = []

    async def clip(src, dst, *, start, duration, tune_grain):
        starts.append(start)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("c")
    monkeypatch.setattr(pipe.handbrake, "encode_clip", clip)

    job = _grain_job("preview")             # title 0 runs 3000s
    await pipe._generate_grain_preview(job, tmp_path / "r.mkv", 0, tmp_path / "pv", _Report())
    assert starts == [990, 990]             # int(3000 * 0.33), both clips aligned


async def test_preview_posters_are_skipped_when_ffmpeg_is_absent(tmp_path, monkeypatch):
    """The clips are the preview; the still frames are a nicety. A box without
    ffmpeg must still get a usable A/B gate."""
    pipe = _pipe(tmp_path, monkeypatch)
    _ffmpeg_installed(pipe, monkeypatch, False)
    frames = []
    monkeypatch.setattr(pipe.ffmpeg, "extract_frame", lambda *a, **k: frames.append(1))

    async def clip(src, dst, **k):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("c")
    monkeypatch.setattr(pipe.handbrake, "encode_clip", clip)

    preview = await pipe._generate_grain_preview(
        _grain_job("preview"), tmp_path / "r.mkv", 0, tmp_path / "pv", _Report())
    assert frames == []
    assert preview.off_png is None and preview.on_png is None
    assert preview.off_clip.exists() and preview.on_clip.exists()


async def test_a_failed_poster_frame_does_not_lose_the_preview(tmp_path, monkeypatch):
    """ffmpeg present but choking on the clip: the gate still opens with clips,
    and reports no half-written poster paths."""
    pipe = _pipe(tmp_path, monkeypatch)
    _ffmpeg_installed(pipe, monkeypatch, True)

    async def bad_frame(src, dst, *, at_seconds): raise OSError("ffmpeg exited 1")
    monkeypatch.setattr(pipe.ffmpeg, "extract_frame", bad_frame)

    async def clip(src, dst, **k):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("c")
    monkeypatch.setattr(pipe.handbrake, "encode_clip", clip)

    r = _Report()
    preview = await pipe._generate_grain_preview(
        _grain_job("preview"), tmp_path / "r.mkv", 0, tmp_path / "pv", r)
    assert preview.off_png is None and preview.on_png is None
    assert preview.off_clip.exists()
    assert r.said("poster frames skipped")


# --- encode routing: grain checkpoint, deinterlace, UHD -------------------

class _FakeHandBrake:
    def __init__(self):
        self.calls = []

    def speed_summary(self): return "preset"

    async def encode(self, src, dst, *, on_progress=None, tune_grain=False, deinterlace=False):
        self.calls.append({"tune_grain": tune_grain, "deinterlace": deinterlace})
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_text("handbrake")


def _encode_pipe(tmp_path, monkeypatch, height=1080):
    pipe = _pipe(tmp_path, monkeypatch)
    hb = _FakeHandBrake()
    monkeypatch.setattr(pipe, "handbrake", hb)

    async def probe(path, report): return height
    monkeypatch.setattr(pipe, "_probe_height", probe)
    return pipe, hb


async def _encode(pipe, job, cp=None):
    a = _movie()
    job.assignments = {0: a}
    if cp:
        job.checkpoints[0] = dict(cp)
    return await pipe._encode_title(job, 0, a, Path(pipe.settings.storage.staging_dir) / "j",
                                    Path("rip.mkv"), EncodeScheduler(3), _Report())


async def test_the_checkpointed_grain_pick_is_what_reaches_the_encoder(tmp_path, monkeypatch):
    """Grain is decided once on the rip box and carried to the worker in the
    checkpoint. If the worker re-reads its own config the user's pick is lost and
    a whole 4K encode comes out wrong."""
    pipe, hb = _encode_pipe(tmp_path, monkeypatch)
    monkeypatch.setattr(pipe.settings.encode, "tune_grain", False)
    await _encode(pipe, Job(source="disc:0"), cp={"tune_grain": True})
    assert hb.calls[0]["tune_grain"] is True


async def test_without_a_checkpoint_the_job_override_beats_the_config(tmp_path, monkeypatch):
    """A job that never reached the grain gate still honours its per-job override
    before falling back to the machine's config default."""
    pipe, hb = _encode_pipe(tmp_path, monkeypatch)
    monkeypatch.setattr(pipe.settings.encode, "tune_grain", False)
    await _encode(pipe, Job(source="disc:0", tune_grain=True))
    assert hb.calls[0]["tune_grain"] is True


async def test_sd_sources_are_deinterlaced_and_hd_sources_are_not(tmp_path, monkeypatch):
    """DVD sources are 480i/576i: skipping decomb ships combed episodes. Running
    it on HD costs quality for nothing — the height is the whole decision."""
    pipe, hb = _encode_pipe(tmp_path, monkeypatch, height=480)
    await _encode(pipe, Job(source="disc:0"))
    assert hb.calls[0]["deinterlace"] is True

    pipe2, hb2 = _encode_pipe(tmp_path / "hd", monkeypatch, height=1080)
    await _encode(pipe2, Job(source="disc:0"))
    assert hb2.calls[0]["deinterlace"] is False


async def test_uhd_routing_switches_at_the_configured_height(tmp_path, monkeypatch):
    """One pixel below uhd_min_height must stay on HandBrake and one pixel at it
    must take ffmpeg — this threshold is what preserves HDR."""
    for height, want_ffmpeg in ((2160, True), (2159, False)):
        pipe, hb = _encode_pipe(tmp_path / f"h{height}", monkeypatch, height=height)
        monkeypatch.setattr(pipe.settings.encode, "ffmpeg_uhd", True)
        _ffmpeg_installed(pipe, monkeypatch, True)
        uhd = []

        async def fake_uhd(src, dst, grain, encode_dir, report):
            uhd.append(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text("ffmpeg")
        monkeypatch.setattr(pipe, "_encode_uhd", fake_uhd)

        await _encode(pipe, Job(source="disc:0"))
        assert bool(uhd) == want_ffmpeg
        assert bool(hb.calls) == (not want_ffmpeg)


async def test_an_already_placed_title_is_not_re_encoded(tmp_path, monkeypatch):
    """A worker resuming a job must hand back the placed file untouched. Re-running
    the encode costs hours and rewrites a file Plex has already scanned."""
    pipe, hb = _encode_pipe(tmp_path, monkeypatch)
    a = _movie()
    dest = pipe._video_path_for(a)
    dest.parent.mkdir(parents=True)
    dest.write_text("the finished encode")
    result = await _encode(pipe, Job(source="disc:0"), cp={"placed": True})
    assert hb.calls == []
    assert result.video_path == dest
    assert dest.read_text() == "the finished encode"


async def test_an_unknown_height_never_takes_the_ffmpeg_path(tmp_path, monkeypatch):
    """A failed resolution probe must not be treated as UHD: ffmpeg's UHD path
    would be handed an SD file, and the UHD block would fail a fine job."""
    pipe, hb = _encode_pipe(tmp_path, monkeypatch, height=None)
    monkeypatch.setattr(pipe.settings.encode, "ffmpeg_uhd", True)
    _ffmpeg_installed(pipe, monkeypatch, True)
    uhd = []
    monkeypatch.setattr(pipe, "_encode_uhd", lambda *a: uhd.append(1))
    await _encode(pipe, Job(source="disc:0"))
    assert uhd == [] and len(hb.calls) == 1


async def test_a_failed_resolution_probe_still_encodes_and_places_the_title(tmp_path, monkeypatch):
    """The height only picks a route and a scheduler weight. Losing it to a dead
    mkvmerge must cost the UHD routing, not the encode."""
    pipe = _pipe(tmp_path, monkeypatch)
    hb = _FakeHandBrake()
    monkeypatch.setattr(pipe, "handbrake", hb)

    async def boom(path): raise OSError("mkvmerge died")
    monkeypatch.setattr(pipe.mkvtools, "video_height", boom)

    a = _movie()
    job = Job(source="disc:0")
    job.assignments = {0: a}
    r = _Report()
    result = await pipe._encode_title(job, 0, a, tmp_path / "stg", Path("rip.mkv"),
                                      EncodeScheduler(3), r)
    assert len(hb.calls) == 1
    assert result.video_path.exists()
    assert r.said("Could not probe resolution")


# --- ffmpeg UHD / Dolby Vision routing ------------------------------------

class _FakeFFmpeg:
    def __init__(self, *, probes=None, default=None):
        self.available = True
        self.probes = probes or {}
        self.default = default if default is not None else HdrInfo()
        self.encoded = []
        self.hevc = []

    async def probe_video(self, path):
        got = self.probes.get(Path(path).name, self.default)
        if isinstance(got, Exception):
            raise got
        return got, 7200.0

    async def encode(self, src, dst, *, on_progress=None, tune_grain=False, low_priority=True):
        self.encoded.append(tune_grain)
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_text("hdr10")

    async def extract_hevc(self, src, dst, **kw):
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_text("raw")

    async def encode_hevc(self, src, dst, *, hdr, duration, on_progress=None,
                          tune_grain=False, low_priority=True):
        self.hevc.append(tune_grain)
        Path(dst).write_text("hdr10-only")


class _FakeDovi:
    def __init__(self, available=True):
        self.available = available
        self.profiles = []
        self.muxed = []

    async def extract_rpu(self, src, dst, profile):
        self.profiles.append(profile)
        Path(dst).write_text("rpu")

    async def inject_rpu(self, enc, rpu, dst):
        Path(dst).write_text("dv-injected")


def _dv_pipe(tmp_path, monkeypatch, *, dovi_available=True, mkvmerge="/usr/bin/mkvmerge",
             hdr=None):
    pipe = _pipe(tmp_path, monkeypatch)
    monkeypatch.setattr(pipe.settings.encode, "dovi", True)
    monkeypatch.setattr(pipe.settings.tools, "mkvmerge", mkvmerge)
    ff = _FakeFFmpeg(default=hdr if hdr is not None else HdrInfo(dv_profile=7))
    monkeypatch.setattr(pipe, "ffmpeg", ff)
    dv = _FakeDovi(dovi_available)
    monkeypatch.setattr(pipe, "dovitool", dv)

    async def mux(video, src, dst):     # never spawn a real mkvmerge
        dv.muxed.append(Path(video).name)
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_text("mkv")
    monkeypatch.setattr(pipe.mkvtools, "mux_dovi", mux)
    return pipe, ff, dv


async def test_dolby_vision_path_is_taken_when_the_tools_are_all_present(tmp_path, monkeypatch):
    """A DV disc with dovi_tool + mkvmerge must round-trip the RPU, not quietly
    encode HDR10 and throw the DV layer away."""
    pipe, ff, _dv = _dv_pipe(tmp_path, monkeypatch)
    used = []
    async def fake_dovi(src, dst, hdr, duration, grain, encode_dir, report):
        used.append(hdr.dv_profile)
    monkeypatch.setattr(pipe, "_encode_dovi", fake_dovi)
    await pipe._encode_uhd(tmp_path / "s.mkv", tmp_path / "d.mkv", False,
                           tmp_path / "enc", _Report())
    assert used == [7]
    assert ff.encoded == []     # the plain HDR10 encode never ran


async def test_a_dolby_vision_failure_falls_back_to_hdr10(tmp_path, monkeypatch):
    """dovi_tool is fragile; a DV failure must cost the DV layer, not the job —
    the fallback is a normal HDR10 encode of the same source."""
    pipe, ff, _dv = _dv_pipe(tmp_path, monkeypatch)

    async def boom(*a, **k): raise RuntimeError("dovi_tool exited 1")
    monkeypatch.setattr(pipe, "_encode_dovi", boom)
    r = _Report()
    await pipe._encode_uhd(tmp_path / "s.mkv", tmp_path / "d.mkv", True,
                           tmp_path / "enc", r)
    assert ff.encoded == [True]                     # fell back, keeping grain
    assert (tmp_path / "d.mkv").read_text() == "hdr10"
    assert r.said("falling back to HDR10")


async def test_a_dv_disc_without_dovi_tool_encodes_hdr10_and_says_so(tmp_path, monkeypatch):
    """Without dovi_tool the DV layer cannot be preserved. That has to be stated,
    because the resulting file looks fine until a DV TV plays it."""
    pipe, ff, dv = _dv_pipe(tmp_path, monkeypatch, dovi_available=False)
    r = _Report()
    await pipe._encode_uhd(tmp_path / "s.mkv", tmp_path / "d.mkv", False,
                           tmp_path / "enc", r)
    assert ff.encoded == [False]      # the plain HDR10 encode, not the DV round-trip
    assert dv.profiles == []          # no RPU extraction was attempted
    assert r.said("dovi_tool/mkvmerge unavailable")


async def test_dv_needs_mkvmerge_too_since_the_result_must_be_remuxed(tmp_path, monkeypatch):
    """dovi_tool alone can't produce the final MKV. Starting the DV round-trip
    without mkvmerge burns a full re-encode before failing at the last step."""
    pipe, ff, _dv = _dv_pipe(tmp_path, monkeypatch, mkvmerge=None)
    started = []
    monkeypatch.setattr(pipe, "_encode_dovi", lambda *a, **k: started.append(1))
    await pipe._encode_uhd(tmp_path / "s.mkv", tmp_path / "d.mkv", False,
                           tmp_path / "enc", _Report())
    assert started == []
    assert ff.encoded == [False]


async def test_an_sdr_source_skips_the_hdr_output_check(tmp_path, monkeypatch):
    """The output probe exists to catch a dropped HDR10 signal. On an SDR source
    there is nothing to drop, so it must not cost a second ffprobe."""
    pipe = _pipe(tmp_path, monkeypatch)
    probed = []

    class _FF(_FakeFFmpeg):
        async def probe_video(self, path):
            probed.append(Path(path).name)
            return await super().probe_video(path)

    ff = _FF(default=HdrInfo(transfer="bt709"))
    monkeypatch.setattr(pipe, "ffmpeg", ff)
    monkeypatch.setattr(pipe.settings.encode, "dovi", False)
    await pipe._encode_uhd(tmp_path / "s.mkv", tmp_path / "d.mkv", False,
                           tmp_path / "enc", _Report())
    assert probed == ["s.mkv"]     # source only; the output was not re-probed


async def test_an_unverifiable_hdr_output_is_reported_not_fatal(tmp_path, monkeypatch):
    """If the verification probe itself dies, the finished encode must be kept —
    losing hours of work to a failed sanity check is the worse outcome."""
    pipe = _pipe(tmp_path, monkeypatch)
    want = HdrInfo(colorprim="bt2020", transfer="smpte2084", colormatrix="bt2020nc")
    ff = _FakeFFmpeg(probes={"d.mkv": OSError("ffprobe not found")}, default=want)
    monkeypatch.setattr(pipe, "ffmpeg", ff)
    r = _Report()
    await pipe._verify_hdr(tmp_path / "d.mkv", want, r)
    assert r.said("Could not verify HDR")


async def test_the_dv_mux_uses_the_rpu_injected_stream(tmp_path, monkeypatch):
    """The last step decides whether the file is Dolby Vision at all: muxing the
    pre-injection HEVC produces a plain HDR10 file that claims to be DV work."""
    pipe, ff, dv = _dv_pipe(tmp_path, monkeypatch)
    muxed = []

    async def mux(video, src, dst):
        muxed.append(Path(video).read_text())
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_text("mkv")
    monkeypatch.setattr(pipe.mkvtools, "mux_dovi", mux)

    encode_dir = tmp_path / "enc"
    await pipe._encode_dovi(tmp_path / "s.mkv", tmp_path / "out.mkv",
                            HdrInfo(dv_profile=7), 7200.0, True, encode_dir, _Report())
    assert muxed == ["dv-injected"]
    assert dv.profiles == [7]                     # the RPU was extracted for profile 7
    assert ff.hevc == [True]                      # grain carried into the re-encode
    assert sorted(p.name for p in (encode_dir / "dovi").iterdir()) == []


async def test_dv_intermediates_are_removed_even_when_the_encode_dies(tmp_path, monkeypatch):
    """The DV round-trip writes raw HEVC the size of the disc. Leaving it behind
    on a failure fills the staging drive and blocks every later job."""
    pipe, ff, _dv = _dv_pipe(tmp_path, monkeypatch)

    async def boom(*a, **k): raise RuntimeError("x265 exited 1")
    monkeypatch.setattr(ff, "encode_hevc", boom)

    encode_dir = tmp_path / "enc"
    with pytest.raises(RuntimeError):
        await pipe._encode_dovi(tmp_path / "s.mkv", tmp_path / "out.mkv",
                                HdrInfo(dv_profile=7), 7200.0, False, encode_dir, _Report())
    assert list((encode_dir / "dovi").iterdir()) == []


async def test_measured_title_heights_are_kept_on_the_job(tmp_path, monkeypatch):
    """A worker with a max encode height reads them off the job to pass over a
    UHD job without claiming it."""
    pipe = _pipe(tmp_path, monkeypatch)
    ripped = pipe.settings.storage.staging_dir / "job_5" / "rip" / "t000" / "a.mkv"
    ripped.parent.mkdir(parents=True)
    ripped.write_text("x")
    job = Job(source="disc:0")
    job.selected_title_ids = [0, 1]
    job.checkpoints = {0: {"ripped_path": "job_5/rip/t000/a.mkv"},
                       1: {"ripped_path": "job_5/rip/t001/gone.mkv"}}

    async def probe(path, report): return 2160
    monkeypatch.setattr(pipe, "_probe_height", probe)

    assert await pipe.title_heights(job, tmp_path, _Report()) == [2160, 0]
    assert job.checkpoints[0]["height"] == 2160
    assert "height" not in job.checkpoints[1]         # unmeasured stays unrecorded


class _Stages(_Report):
    def __init__(self):
        super().__init__()
        self.stages = []

    def on_progress(self, stage, fraction=None):
        self.stages.append(stage)


async def test_the_dv_inject_and_mux_report_their_own_progress(tmp_path, monkeypatch):
    """dovi_tool and mkvmerge print nothing on a pipe, so after the x265 pass the
    card kept "Encode: done · 1 fps · ETA 2s" through the whole inject and mux,
    minutes each on a UHD title (job 157). Each reads its progress off its
    output file as it grows: the injected stream against the encode plus the
    RPU, the MKV against that stream."""
    pipe, ff, dv = _dv_pipe(tmp_path, monkeypatch)
    monkeypatch.setattr(pipeline_mod, "GROWTH_POLL_SECONDS", 0.01)

    async def encode_hevc(src, dst, **kw):
        Path(dst).write_bytes(b"e" * 900)
    monkeypatch.setattr(ff, "encode_hevc", encode_hevc)

    async def extract_rpu(src, dst, profile):
        Path(dst).write_bytes(b"r" * 100)
    monkeypatch.setattr(dv, "extract_rpu", extract_rpu)

    async def grow(dst, total):
        with open(dst, "wb") as f:
            for _ in range(4):
                f.write(b"x" * (total // 4))
                f.flush()
                await asyncio.sleep(0.06)

    async def inject(enc, rpu, dst):
        await grow(dst, 1000)                 # 900 encoded + 100 RPU
    monkeypatch.setattr(dv, "inject_rpu", inject)

    async def mux(video, src, dst):
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        await grow(dst, 1000)                 # the source has no other tracks here
    monkeypatch.setattr(pipe.mkvtools, "mux_dovi", mux)

    r = _Stages()
    await pipe._encode_dovi(tmp_path / "s.mkv", tmp_path / "out.mkv",
                            HdrInfo(dv_profile=7), 7200.0, False, tmp_path / "enc", r)

    for label in ("Injecting Dolby Vision RPU", "Muxing DV + audio"):
        seen = {s.split(" · ")[1] for s in r.stages
                if s.startswith(label + " · ")}
        assert seen & {"25%", "50%", "75%"}, (label, r.stages)


def test_a_step_eta_comes_from_how_fast_it_has_moved_so_far():
    """The first sighting only starts the clock; after that the time left is the
    time taken so far, scaled by how much is left against how much was done."""
    now = [100.0]
    eta = pipeline_mod._StepEta(clock=lambda: now[0])
    assert eta.update(0.10) is None
    now[0] = 110.0
    assert eta.update(0.30) == 35          # 10s for 20%, 70% to go
    now[0] = 120.0
    assert eta.update(0.30) == 70          # stalled: the estimate stretches out
    assert eta.update(None) is None


def test_a_step_line_reads_like_the_encode_line():
    assert pipeline_mod._step_line("Muxing DV + audio", 0.42, 185) == \
        "Muxing DV + audio · 42% · ETA 3m05s"
    assert pipeline_mod._step_line("Extracting the DV RPU", None, None) == \
        "Extracting the DV RPU"


# --- rip acceptance, eject + notify after the last rip --------------------

def _rip_job(source="disc:0", device="D:") -> Job:
    job = Job(source=source)
    job.disc = DiscInfo(source=source, disc_name="DISC LABEL", drive_device=device,
                        titles=[Title(title_id=0)])
    job.selected_title_ids = [0]
    return job


def _fake_rip(pipe, monkeypatch):
    async def rip(source, title_id, rip_dir, on_progress=None, on_message=None):
        rip_dir.mkdir(parents=True, exist_ok=True)
        p = rip_dir / f"t{title_id}.mkv"
        p.write_text("x")
        return [p]
    monkeypatch.setattr(pipe.makemkv, "rip_title", rip)


async def test_an_unknown_rip_runtime_never_condemns_the_rip(tmp_path, monkeypatch):
    """The truncation guard compares runtimes. A probe that reports 0s is "no
    answer", not "0% of the disc title" — treating it as the latter throws away a
    good rip and re-rips it on every retry, forever."""
    pipe = _pipe(tmp_path, monkeypatch)
    monkeypatch.setattr(pipe.settings, "rip_duration_tolerance_pct", 3)

    async def probe(path): return SimpleNamespace(duration_seconds=0)
    monkeypatch.setattr(pipe.mkvtools, "probe", probe)

    job = Job(source="disc:0")
    job.disc = DiscInfo(source="disc:0", titles=[Title(title_id=1, duration_seconds=1711)])
    r = _Report()
    await pipe._verify_rip_duration(job, 1, Path("x.mkv"), r)   # no raise
    assert not r.said("Rip verified")   # skipped, not passed — nothing was compared


async def test_a_rip_that_produced_no_file_fails_loudly(tmp_path, monkeypatch):
    """MakeMKV can exit 0 having written nothing. Carrying on would checkpoint a
    path that does not exist and encode/place from thin air."""
    pipe = _pipe(tmp_path, monkeypatch)

    async def rip(source, title_id, rip_dir, on_progress=None, on_message=None):
        return []
    monkeypatch.setattr(pipe.makemkv, "rip_title", rip)
    with pytest.raises(RuntimeError, match="produced no output"):
        await pipe._ensure_ripped(_rip_job(), 0, tmp_path / "job", _Report())


async def test_a_failed_eject_still_tells_you_the_disc_is_still_in_the_drive(tmp_path, monkeypatch):
    """The notification is how you know it's safe to swap discs. When the eject
    IOCTL is refused it must say the disc is still there, not claim it ejected."""
    pipe = _pipe(tmp_path, monkeypatch)
    monkeypatch.setattr(pipe.settings, "auto_eject", True)
    _fake_rip(pipe, monkeypatch)

    def bad_eject(dev): raise OSError("access denied")
    monkeypatch.setattr("blurayripper.pipeline.eject", bad_eject)

    async def dev(_job): return "D:"
    monkeypatch.setattr(pipe, "_eject_device_for", dev)

    r = _Report()
    await pipe._ensure_ripped(_rip_job(), 0, tmp_path / "job", r)
    assert r.said("Auto-eject failed")
    assert r.ejects == []                     # the UI's cached disc name is NOT cleared
    _title, message = r.notes[0]
    assert "Safe to swap the disc." in message and "ejected" not in message


async def test_a_file_sourced_job_never_ejects_a_drive(tmp_path, monkeypatch):
    """A job added from an .mkv on disk has no disc. Ejecting on its behalf pops
    open a drive holding some other job's disc mid-rip."""
    pipe = _pipe(tmp_path, monkeypatch)
    monkeypatch.setattr(pipe.settings, "auto_eject", True)
    _fake_rip(pipe, monkeypatch)
    ejected = []
    monkeypatch.setattr("blurayripper.pipeline.eject", lambda dev: ejected.append(dev))

    r = _Report()
    await pipe._ensure_ripped(_rip_job(source="file:/mnt/rips/movie"), 0, tmp_path / "job", r)
    assert ejected == [] and r.notes == []


async def test_eject_targets_the_drive_the_disc_is_in_now(tmp_path, monkeypatch):
    """job.disc.drive_device goes stale when a disc is moved between drives; the
    stored letter can now hold someone else's disc."""
    pipe = _pipe(tmp_path, monkeypatch)

    async def drives(timeout=None):
        return [SimpleNamespace(index=0, device="Z:"), SimpleNamespace(index=1, device="G:")]
    monkeypatch.setattr(pipe.makemkv, "list_drives", drives)

    job = _rip_job(source="disc:1", device="D:")   # stale stored device
    assert await pipe._eject_device_for(job) == "G:"


async def test_eject_falls_back_to_the_stored_device_when_the_drive_is_gone(tmp_path, monkeypatch):
    """If the current source index isn't in the enumeration (or enumeration
    fails), the best guess is the device recorded at scan time — never None."""
    pipe = _pipe(tmp_path, monkeypatch)

    async def missing(timeout=None):
        return [SimpleNamespace(index=9, device="Z:")]
    monkeypatch.setattr(pipe.makemkv, "list_drives", missing)
    assert await pipe._eject_device_for(_rip_job(source="disc:1")) == "D:"

    async def boom(timeout=None): raise OSError("makemkvcon wedged")
    monkeypatch.setattr(pipe.makemkv, "list_drives", boom)
    assert await pipe._eject_device_for(_rip_job(source="disc:1")) == "D:"


async def test_a_dev_source_is_ejected_without_re_enumerating_drives(tmp_path, monkeypatch):
    """"dev:/dev/sr0" already names the device. Enumerating drives to "resolve" it
    can only lose the answer (and blocks on a wedged drive)."""
    pipe = _pipe(tmp_path, monkeypatch)
    listed = []

    async def drives(timeout=None):
        listed.append(1)
        return []
    monkeypatch.setattr(pipe.makemkv, "list_drives", drives)

    job = _rip_job(source="dev:/dev/sr0", device="/dev/sr0")
    assert await pipe._eject_device_for(job) == "/dev/sr0"
    assert listed == []


# --- artwork placement ----------------------------------------------------

def _art_job(tmp_path, assignments) -> Job:
    job = Job(source=f"file:{tmp_path}")
    job.disc = DiscInfo(source=f"file:{tmp_path}", disc_name="X")
    job.assignments = dict(assignments)
    job.selected_title_ids = list(assignments)
    return job


async def test_cover_art_asks_only_for_the_half_that_is_missing(tmp_path, monkeypatch):
    """A folder that already has a poster (from TMDB, or a better disc of a set)
    must keep it — re-extracting overwrites good art with disc menu graphics."""
    pipe = _pipe(tmp_path, monkeypatch)
    a = _movie()
    folder = pipe._poster_folder(a)
    folder.mkdir(parents=True)
    (folder / "poster.jpg").write_bytes(b"good")
    asked = []
    monkeypatch.setattr("blurayripper.services.coverart.extract_art",
                        lambda root, dest, **k: asked.append(k) or DiscArt())
    await pipe._place_cover_art(_art_job(tmp_path, {0: a}), _Report())
    assert asked == [{"poster": False, "fanart": True}]


async def test_cover_art_is_skipped_entirely_when_both_already_exist(tmp_path, monkeypatch):
    """Resumes and multi-disc sets re-enter this stage; re-reading the disc for
    art already on disk is pure waste (and can overwrite the better art)."""
    pipe = _pipe(tmp_path, monkeypatch)
    a = _movie()
    folder = pipe._poster_folder(a)
    folder.mkdir(parents=True)
    (folder / "poster.png").write_bytes(b"p")     # any supported extension counts
    (folder / "fanart.jpg").write_bytes(b"f")
    asked = []
    monkeypatch.setattr("blurayripper.services.coverart.extract_art",
                        lambda *a_, **k: asked.append(1) or DiscArt())
    await pipe._place_cover_art(_art_job(tmp_path, {0: a}), _Report())
    assert asked == []


async def test_every_episode_of_a_show_shares_one_art_extraction(tmp_path, monkeypatch):
    """TV art lives in the show root, above Season NN. Extracting per episode
    re-copies the same file once per title on a 20-episode box set."""
    pipe = _pipe(tmp_path, monkeypatch)
    eps = {i: EpisodeAssignment(i, MediaKind.TV, "The Show", 2015, season=1, episode=i + 1)
           for i in range(3)}
    folders = []
    monkeypatch.setattr("blurayripper.services.coverart.extract_art",
                        lambda root, dest, **k: folders.append(dest) or DiscArt())
    await pipe._place_cover_art(_art_job(tmp_path, eps), _Report())
    assert folders == [pipe._poster_folder(eps[0])]


async def test_an_iso_job_reads_art_from_inside_the_image(tmp_path, monkeypatch):
    """An ISO has no readable disc root; handing its path to the folder extractor
    finds nothing and the item ships with no artwork at all."""
    pipe = _pipe(tmp_path, monkeypatch)
    a = _movie()
    job = _art_job(tmp_path, {0: a})
    job.disc.source = "iso:/images/film.iso"
    from_iso, from_folder = [], []
    monkeypatch.setattr("blurayripper.services.coverart.extract_art_from_iso",
                        lambda iso, dest, **k: from_iso.append(iso) or DiscArt())
    monkeypatch.setattr("blurayripper.services.coverart.extract_art",
                        lambda *a_, **k: from_folder.append(1) or DiscArt())
    await pipe._place_cover_art(job, _Report())
    assert from_iso == ["/images/film.iso"] and from_folder == []


async def test_unreadable_disc_art_does_not_fail_the_rip(tmp_path, monkeypatch):
    """Cover art is a nicety extracted while the disc is still in the drive. A
    read error there must not take down a job that is about to rip fine."""
    pipe = _pipe(tmp_path, monkeypatch)

    def boom(root, dest, **k): raise OSError("I/O error reading BDMV/META")
    monkeypatch.setattr("blurayripper.services.coverart.extract_art", boom)
    r = _Report()
    await pipe._place_cover_art(_art_job(tmp_path, {0: _movie()}), r)   # no raise
    assert r.said("Cover art skipped")


def _tmdb_pipe(tmp_path, monkeypatch):
    pipe = _pipe(tmp_path, monkeypatch, tmdb_key="fake-key")
    monkeypatch.setattr(pipe.settings, "fetch_tmdb_artwork", True)
    return pipe


async def test_tv_assignments_use_the_show_endpoint_once_per_show_folder(tmp_path, monkeypatch):
    """A show searched as a movie returns a same-named film's poster. And one
    lookup per episode is a TMDB request per episode for identical art."""
    pipe = _tmdb_pipe(tmp_path, monkeypatch)
    tv, movie = [], []

    async def tv_art(title, year):
        tv.append((title, year))
        return ("https://img/p.jpg", None)

    async def movie_art(title, year):
        movie.append(title)
        return (None, None)

    async def save(url, dest):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"art")
        return True
    monkeypatch.setattr(pipe.tmdb, "tv_artwork", tv_art)
    monkeypatch.setattr(pipe.tmdb, "movie_artwork", movie_art)
    monkeypatch.setattr(pipe.tmdb, "save_image", save)

    eps = [EpisodeAssignment(i, MediaKind.TV, "The Show", 2015, season=1, episode=i + 1)
           for i in range(3)]
    assert await pipe.place_tmdb_art(eps, _Report()) == 1
    assert tv == [("The Show", 2015)] and movie == []


async def test_one_titles_tmdb_failure_does_not_skip_the_rest(tmp_path, monkeypatch):
    """Artwork is fetched for a whole boxset in one pass; a single 500 from TMDB
    must not leave the remaining titles bare."""
    pipe = _tmdb_pipe(tmp_path, monkeypatch)

    async def art(title, year):
        if title == "Bad":
            raise RuntimeError("TMDB 500")
        return ("https://img/p.jpg", None)

    async def save(url, dest):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"art")
        return True
    monkeypatch.setattr(pipe.tmdb, "movie_artwork", art)
    monkeypatch.setattr(pipe.tmdb, "save_image", save)

    r = _Report()
    placed = await pipe.place_tmdb_art([_movie("Bad", 2001), _movie("Good", 2002)], r)
    assert placed == 1
    assert (pipe._poster_folder(_movie("Good", 2002)) / "poster.jpg").exists()
    assert r.said("TMDB artwork skipped")


async def test_a_failed_download_leaves_the_existing_art_alone(tmp_path, monkeypatch):
    """The stale wrong-extension art is only dropped once the replacement is on
    disk. Dropping it on a failed download leaves the folder with no poster."""
    pipe = _tmdb_pipe(tmp_path, monkeypatch)
    a = _movie()
    folder = pipe._poster_folder(a)
    folder.mkdir(parents=True)
    (folder / "poster.png").write_bytes(b"existing")

    async def art(title, year): return ("https://img/p.jpg", None)
    async def save(url, dest): return False           # download failed
    monkeypatch.setattr(pipe.tmdb, "movie_artwork", art)
    monkeypatch.setattr(pipe.tmdb, "save_image", save)

    assert await pipe.place_tmdb_art([a], _Report()) == 0
    assert (folder / "poster.png").read_bytes() == b"existing"


async def test_artwork_is_skipped_when_the_feature_is_turned_off(tmp_path, monkeypatch):
    """The setting has to gate the network call itself, not just the writing —
    an offline/rate-limited box must be able to opt out completely."""
    pipe = _tmdb_pipe(tmp_path, monkeypatch)
    monkeypatch.setattr(pipe.settings, "fetch_tmdb_artwork", False)
    called = []
    monkeypatch.setattr(pipe.tmdb, "movie_artwork", lambda *a: called.append(1))
    assert await pipe.place_tmdb_art([_movie()], _Report()) == 0
    assert called == []


async def test_a_tv_assignment_without_an_episode_number_is_refused(tmp_path, monkeypatch):
    """Naming a TV file needs season+episode. Without them the "path" would be a
    guess, and the episode lands somewhere Plex will never match."""
    pipe = _pipe(tmp_path, monkeypatch)
    bad = EpisodeAssignment(4, MediaKind.TV, "The Show", 2015, season=1, episode=None)
    with pytest.raises(ValueError, match="season/episode"):
        pipe._video_path_for(bad)
