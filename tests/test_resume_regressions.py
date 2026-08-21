"""Regressions found by the test-writing pass, in code added earlier today.

Each of these makes a job that is fine look broken, or a job that is broken look
fine — and the whole suite stayed green through all of them, which is why they
are pinned here explicitly.
"""

import asyncio
import json

from blurayripper.config import load_settings
from blurayripper.models import (
    DiscInfo, EpisodeAssignment, MediaKind, Stream, StreamType, Title,
)
from blurayripper.pipeline import Job, Pipeline, TitleResult
from blurayripper.scheduler import EncodeScheduler


class _Report:
    def __init__(self): self.events = []
    def on_event(self, m): self.events.append(m)
    def on_progress(self, *a, **k): pass
    def checkpoint(self): pass
    def notify(self, *a, **k): pass
    def ejected(self, *a, **k): pass


def _pipe(tmp_path):
    s = load_settings()
    s.storage.output_dir = tmp_path
    s.storage.staging_dir = tmp_path / "staging"
    s.write_nfo = False
    s.languages.keep = ["eng"]
    return Pipeline(s)


def _job_with_eng_subs(source="disc:0"):
    job = Job(source=source)
    job.disc = DiscInfo(source=source, disc_name="X", titles=[
        Title(title_id=0, duration_seconds=100, size_bytes=1,
              source_filename="t00.mkv",
              streams=[Stream(stream_id=1, stream_type=StreamType.SUBTITLE,
                              language="eng")])])
    job.selected_title_ids = [0]
    return job


def _assignment():
    return EpisodeAssignment(title_id=0, kind=MediaKind.MOVIE,
                             show_or_movie_title="Movie", year=2020)


def test_a_resumed_job_whose_title_is_already_placed_still_verifies_clean(tmp_path):
    """A completed job re-entered (retry, requeue, restart) skips the placed
    title — producing a result with no subtitle paths. The output check then
    reported "the disc carries eng subtitle track(s) but no .srt was produced"
    and failed a job whose sidecars were sitting on disk. Every resume, forever."""
    p = _pipe(tmp_path)
    job = _job_with_eng_subs()
    a = _assignment()
    video = p._video_path_for(a)
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")
    srt = video.with_suffix(".en.srt")
    srt.write_text("1")
    job.checkpoints[0] = {"placed": True,
                          "subs_done": [str(srt.relative_to(tmp_path))]}

    result = asyncio.run(p._process_one(job, 0, a, tmp_path, EncodeScheduler(1),
                                        _Report()))
    job.results = [result]

    assert result.from_checkpoint is True
    assert result.subtitle_paths == [srt]      # carried, not silently dropped
    assert p._verify_outputs(job) == []


def test_a_placed_title_with_no_recorded_subs_is_not_failed_either(tmp_path):
    """Titles placed before subtitle checkpoints existed have no record at all.
    They were verified by the run that placed them, so a later resume must not
    invent a failure for them."""
    p = _pipe(tmp_path)
    job = _job_with_eng_subs()
    a = _assignment()
    video = p._video_path_for(a)
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")
    job.checkpoints[0] = {"placed": True}      # legacy checkpoint, no subs_done

    result = asyncio.run(p._process_one(job, 0, a, tmp_path, EncodeScheduler(1),
                                        _Report()))
    job.results = [result]
    assert p._verify_outputs(job) == []


def test_a_failed_ocr_is_not_recorded_as_subtitles_done(tmp_path, monkeypatch):
    """_ocr_all drops a track whose OCR raised, so a failure leaves an empty
    result — the same shape as "this disc has no subtitles". Recording that as
    done convinced every later run there was nothing to do, while the encode
    marked the title placed. The subtitles could then never be recovered, which
    is precisely the OCR breakage this box just had."""
    p = _pipe(tmp_path)
    job = _job_with_eng_subs()
    a = _assignment()
    track = Stream(stream_id=1, stream_type=StreamType.SUBTITLE, language="eng")

    async def extracted(*args, **kw):
        return [(tmp_path / "t.sup", track)]        # one track extracted
    async def ocr_failed(*args, **kw):
        return {0: []}                              # ...and none survived OCR
    async def place(result, *a, **k): pass
    async def grain(*a, **k): return False

    monkeypatch.setattr(p, "_extract_title_subs", extracted)
    monkeypatch.setattr(p, "_ocr_all", ocr_failed)
    monkeypatch.setattr(p, "_place_title_subs", place)
    monkeypatch.setattr(p, "_cache_subs", lambda *a, **k: None)
    monkeypatch.setattr(p, "_resolve_grain", grain)

    rep = _Report()
    asyncio.run(p._prepare_title(job, 0, a, tmp_path, tmp_path / "r.mkv", rep))

    assert "subs_done" not in job.checkpoints[0]
    assert any("failed OCR" in e for e in rep.events)
    assert p._subs_already_done(job.checkpoints[0]) is None   # so a retry re-runs


def test_a_title_with_genuinely_no_subtitles_is_still_recorded_done(tmp_path, monkeypatch):
    """The distinction that makes the fix correct: zero tracks extracted and zero
    OCR'd is complete, not failed, and must not re-run forever."""
    p = _pipe(tmp_path)
    job = _job_with_eng_subs()
    a = _assignment()

    async def nothing(*args, **kw): return []
    async def no_ocr(*args, **kw): return {0: []}
    async def place(result, *a, **k): pass
    async def grain(*a, **k): return False

    monkeypatch.setattr(p, "_extract_title_subs", nothing)
    monkeypatch.setattr(p, "_ocr_all", no_ocr)
    monkeypatch.setattr(p, "_place_title_subs", place)
    monkeypatch.setattr(p, "_cache_subs", lambda *a, **k: None)
    monkeypatch.setattr(p, "_resolve_grain", grain)

    asyncio.run(p._prepare_title(job, 0, a, tmp_path, tmp_path / "r.mkv", _Report()))
    assert job.checkpoints[0]["subs_done"] == []


def test_an_unreadable_ffprobe_is_not_treated_as_an_sdr_source(tmp_path, monkeypatch):
    """Returning an empty HdrInfo made a probe failure indistinguishable from a
    genuinely SDR source: the encode dropped every colour flag, and the output
    check returned early because the SOURCE looked SDR — so HDR10 was stripped
    with nothing whatsoever in the log."""
    import pytest

    from blurayripper.tools import ffmpeg as ffmpeg_mod

    p = _pipe(tmp_path)

    class _Out:
        stdout_lines = ["<html>502 Bad Gateway</html>"]
        stderr_lines = []

    async def fake_run(cmd, **kw): return _Out()
    monkeypatch.setattr(ffmpeg_mod.process, "run", fake_run)

    with pytest.raises(RuntimeError, match="could not read"):
        asyncio.run(p.ffmpeg.probe_video(tmp_path / "movie.mkv"))


def test_a_resume_does_not_double_count_its_outputs(tmp_path, monkeypatch):
    """_process_titles appends, and execute never cleared the list, so a second
    run through reported twice the outputs and repeated every problem."""
    p = _pipe(tmp_path)
    job = _job_with_eng_subs()
    job.assignments = {0: _assignment()}

    async def one_result(j, tid, a, root, sched, report):
        return TitleResult(title_id=tid, video_path=tmp_path / "v.mkv")

    monkeypatch.setattr(p, "_process_one", one_result)
    monkeypatch.setattr(p, "_resolve_source_drive", _noop)
    monkeypatch.setattr(p, "_place_cover_art", _noop)
    monkeypatch.setattr(p, "_place_tmdb_art", _noop)

    for _ in range(2):
        asyncio.run(p._process_titles(job, tmp_path, EncodeScheduler(1), _Report()))
    assert len(job.results) == 1


async def _noop(*a, **k):
    return None
