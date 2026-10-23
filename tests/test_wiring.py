"""Call-site tests for behaviour whose helpers were tested in isolation.

Mutation testing found these: deleting the line that CALLS each helper left the
whole suite green, because every test exercised the helper directly and nothing
checked it was wired in. A helper that is never called is indistinguishable from
one that does not exist.
"""

import asyncio

import pytest

from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, EpisodeAssignment, MediaKind
from blurayripper.persistence import MemoryStore
from blurayripper.pipeline import Job, JobState, Pipeline, TitleResult
from blurayripper.web.manager import JobManager, WebJob


# --- the local encode must leave the rip lane BEFORE it starts -----------------

def test_a_local_encode_is_marked_encoding_before_the_encode_begins(tmp_path, monkeypatch):
    """Job 315's bug. The state has to change before encode_phase runs, not after
    it finishes — an encode takes many hours, and for all of them the job showed
    as ripping and pinned an already-ejected drive."""
    settings = load_settings()
    settings.cluster.use_workers = True
    settings.cluster.local_encode_max_height = 1080
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path)
    monkeypatch.setattr(settings.storage, "staging_dir", tmp_path / "staging")
    m = JobManager(settings, store=MemoryStore())

    wj = WebJob(id=315, source="disc:0", state=JobState.RIPPING)
    job = Job(source="disc:0")
    job.disc = DiscInfo(source="disc:0", disc_name="SURFSUP", titles=[])
    wj._pipeline_job = job
    wj.selected_title_ids = [0]
    wj.assignments = {0: EpisodeAssignment(
        title_id=0, kind=MediaKind.MOVIE, show_or_movie_title="Surf's Up", year=2007)}
    m.jobs[315] = wj

    seen = {}

    async def fake_prep(*a, **k): pass
    async def fake_heights(*a, **k): return [1080]
    async def fake_encode(j, *a, **k):
        seen["state_at_encode"] = j.state          # the whole point
        return [TitleResult(title_id=0, video_path=tmp_path / "out.mkv")]

    monkeypatch.setattr(m.pipeline, "prep_phase", fake_prep)
    monkeypatch.setattr(m.pipeline, "title_heights", fake_heights)
    monkeypatch.setattr(m.pipeline, "_persist_mapping", lambda *a, **k: None)
    monkeypatch.setattr(m.pipeline, "encode_phase", fake_encode)
    monkeypatch.setattr("blurayripper.web.manager.library.cleanup", lambda p: None)

    asyncio.run(m._run_job(wj))

    assert seen["state_at_encode"] is JobState.ENCODING
    assert m._lane(wj, JobState.ENCODING) == "encode"   # so it leaves the rip queue


# --- finished subtitles must actually be recorded ------------------------------

def _subs_pipeline(tmp_path, monkeypatch):
    s = load_settings()
    s.storage.output_dir = tmp_path
    s.storage.staging_dir = tmp_path / "staging"
    return Pipeline(s)


def test_the_batch_path_records_that_subtitles_are_done(tmp_path, monkeypatch):
    """Recording is what makes the skip possible; without it every resume re-OCRs
    forever and the checkpoint helper is dead code."""
    p = _subs_pipeline(tmp_path, monkeypatch)
    job = Job(source="disc:0")
    srt = tmp_path / "Movie (2020).en.srt"
    srt.write_text("1")

    async def fake_extract(j, tid, ripped, sub_dir, report): return []
    async def fake_ocr(j, entries, report): return {}
    async def fake_place(result, video_dest, pairs, report, **kw):
        result.subtitle_paths.append(srt)

    monkeypatch.setattr(p, "_extract_title_subs", fake_extract)
    monkeypatch.setattr(p, "_ocr_all", fake_ocr)
    monkeypatch.setattr(p, "_place_title_subs", fake_place)
    monkeypatch.setattr(p, "_cache_subs", lambda *a, **k: None)

    async def no_captions(ripped, sub_dir): return None
    monkeypatch.setattr(p, "_caption_srt", no_captions)

    a = EpisodeAssignment(title_id=0, kind=MediaKind.MOVIE,
                          show_or_movie_title="Movie", year=2020)
    todo = [(0, a, tmp_path / "r.mkv", tmp_path / "sub")]
    asyncio.run(p._subtitles_for_titles(job, todo, _Report()))

    assert job.checkpoints[0]["subs_done"] == ["Movie (2020).en.srt"]


def test_a_second_run_skips_extract_and_ocr_entirely(tmp_path, monkeypatch):
    """The payoff: job 317 re-OCR'd tracks it had already placed 90 minutes
    earlier. A recorded title must not be touched again."""
    p = _subs_pipeline(tmp_path, monkeypatch)
    job = Job(source="disc:0")
    srt = tmp_path / "Movie (2020).en.srt"
    srt.write_text("1")
    job.checkpoints[0] = {"subs_done": ["Movie (2020).en.srt"]}

    called = []

    async def boom_extract(*a, **k):
        called.append("extract"); return []
    async def boom_ocr(*a, **k):
        called.append("ocr"); return {}

    monkeypatch.setattr(p, "_extract_title_subs", boom_extract)
    monkeypatch.setattr(p, "_ocr_all", boom_ocr)

    a = EpisodeAssignment(title_id=0, kind=MediaKind.MOVIE,
                          show_or_movie_title="Movie", year=2020)
    rep = _Report()
    asyncio.run(p._subtitles_for_titles(job, [(0, a, tmp_path / "r.mkv", tmp_path / "s")], rep))

    assert called == []
    assert any("already done" in e for e in rep.events)


# --- the HDR check must actually run after a UHD encode ------------------------

def test_an_ffmpeg_uhd_encode_verifies_its_output(tmp_path, monkeypatch):
    """The check only helps if the encode path calls it — otherwise a dropped
    HDR10 output still ships silently, which is the whole failure it exists for."""
    from blurayripper.services.hdr import HdrInfo

    p = _subs_pipeline(tmp_path, monkeypatch)
    p.settings.encode.dovi = False
    hdr = HdrInfo(colorprim="bt2020", transfer="smpte2084", colormatrix="bt2020nc")
    verified = []

    async def fake_probe(src): return hdr, 100.0
    async def fake_encode(src, dst, **kw): return dst
    async def spy_verify(dst, want, report): verified.append(dst)

    monkeypatch.setattr(p.ffmpeg, "probe_video", fake_probe)
    monkeypatch.setattr(p.ffmpeg, "encode", fake_encode)
    monkeypatch.setattr(p, "_verify_hdr", spy_verify)

    asyncio.run(p._encode_uhd(tmp_path / "in.mkv", tmp_path / "out.mkv",
                              False, tmp_path, _Report()))
    assert verified == [tmp_path / "out.mkv"]


# --- the review flag must reach the UI ----------------------------------------

def test_a_parked_disc_job_reports_needs_review_to_the_ui(tmp_path, monkeypatch):
    """The Needs review chip is driven entirely by this flag; without it the
    parked jobs stay buried among ordinary encode failures."""
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path)
    m = JobManager(settings, store=MemoryStore())

    wj = WebJob(id=300, source="disc:0", state=JobState.FAILED)
    wj.error = "Rip never completed — no ripped file for this job."
    m.jobs[300] = wj
    assert m._job_dict(wj)["needs_review"] is True

    ripped = WebJob(id=301, source="disc:0", state=JobState.FAILED)
    job = Job(source="disc:0")
    job.selected_title_ids = [0]
    f = tmp_path / "r.mkv"; f.write_bytes(b"x")
    job.checkpoints = {0: {"ripped_path": str(f)}}
    ripped._pipeline_job = job
    ripped.selected_title_ids = [0]
    m.jobs[301] = ripped
    # Failed AFTER ripping: it needs no disc, so it is an ordinary failure.
    assert m._job_dict(ripped)["needs_review"] is False


class _Report:
    def __init__(self): self.events = []
    def on_event(self, m): self.events.append(m)
    def on_progress(self, *a, **k): pass
    def checkpoint(self): pass
    def notify(self, *a, **k): pass
    def ejected(self, *a, **k): pass


def test_the_single_box_path_also_records_that_subtitles_are_done(tmp_path, monkeypatch):
    """The non-clustered path OCRs one title at a time and has its own recording
    call. Missing it there means a single-box install re-OCRs on every resume,
    while the clustered path looks fine — the bug hides behind a config flag."""
    p = _subs_pipeline(tmp_path, monkeypatch)
    job = Job(source="disc:0")
    srt = tmp_path / "Movie (2020).en.srt"
    srt.write_text("1")

    async def fake_extract(*a, **k): return []
    async def fake_ocr(*a, **k): return {}
    async def fake_place(result, video_dest, pairs, report, **kw):
        result.subtitle_paths.append(srt)
    async def fake_grain(*a, **k): return False

    monkeypatch.setattr(p, "_extract_title_subs", fake_extract)
    monkeypatch.setattr(p, "_ocr_all", fake_ocr)
    monkeypatch.setattr(p, "_place_title_subs", fake_place)
    monkeypatch.setattr(p, "_cache_subs", lambda *a, **k: None)
    monkeypatch.setattr(p, "_resolve_grain", fake_grain)

    a = EpisodeAssignment(title_id=0, kind=MediaKind.MOVIE,
                          show_or_movie_title="Movie", year=2020)
    asyncio.run(p._prepare_title(job, 0, a, tmp_path, tmp_path / "r.mkv", _Report()))

    assert job.checkpoints[0]["subs_done"] == ["Movie (2020).en.srt"]


def test_an_encode_logs_which_handbrake_preset_it_ran(tmp_path, monkeypatch):
    """Speed and quality live in the preset JSON, not our settings, so without
    this line a preset that quietly changed reads as the machine getting slower.
    Diagnosing that by hand is exactly what it cost last time."""
    import json as _json

    from blurayripper.scheduler import EncodeScheduler

    p = _subs_pipeline(tmp_path, monkeypatch)
    pf = tmp_path / "presets.json"
    pf.write_text(_json.dumps({"PresetList": [
        {"PresetName": "Mine", "VideoEncoder": "x265_10bit",
         "VideoPreset": "slower", "VideoQualitySlider": 18}]}))
    p.settings.encode.preset_file, p.settings.encode.preset_name = pf, "Mine"
    p.settings.write_nfo = False

    async def fake_height(*a, **k): return 1080
    async def fake_encode(src, dst, **kw):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"video")
        return dst

    monkeypatch.setattr(p, "_probe_height", fake_height)
    monkeypatch.setattr(p.handbrake, "encode", fake_encode)
    monkeypatch.setattr("blurayripper.pipeline.library.place",
                        lambda src, dest: dest)

    a = EpisodeAssignment(title_id=0, kind=MediaKind.MOVIE,
                          show_or_movie_title="Movie", year=2020)
    job = Job(source="disc:0")
    job.selected_title_ids = [0]
    rep = _Report()
    asyncio.run(p._encode_title(job, 0, a, tmp_path, tmp_path / "r.mkv",
                                EncodeScheduler(1), rep))

    line = next((e for e in rep.events if "HandBrake preset" in e), "")
    assert "Mine" in line and "slower" in line
