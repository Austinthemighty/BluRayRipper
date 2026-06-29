"""prep_phase OCRs subtitle tracks concurrently (bounded), and encoding only
runs after OCR finishes."""

import asyncio

import pytest

from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, EpisodeAssignment, MediaKind, Title
from blurayripper.pipeline import Job, Pipeline
from blurayripper.tools.mkvtools import SubtitleTrack


class _Report:
    def on_event(self, *a): pass
    def on_progress(self, *a): pass
    def checkpoint(self): pass


def _job(n_titles):
    job = Job(source="disc:0")
    job.disc = DiscInfo(source="disc:0", disc_name="Show",
                        titles=[Title(title_id=i, duration_seconds=1300) for i in range(n_titles)])
    job.selected_title_ids = list(range(n_titles))
    job.assignments = {
        i: EpisodeAssignment(i, MediaKind.TV, "Show", 2020, season=1, episode=i + 1)
        for i in range(n_titles)
    }
    return job


async def test_prep_phase_ocr_runs_concurrently(tmp_path, monkeypatch):
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path)
    monkeypatch.setattr(settings.storage, "staging_dir", tmp_path / "stage")
    settings.subtitles.ocr_concurrency = 4
    pipe = Pipeline(settings)

    n = 4
    job = _job(n)

    # Skip real ripping; pretend each title has one extracted subtitle track.
    async def fake_ripped(job, title_id, staging_root, report):
        return tmp_path / f"t{title_id}.mkv"

    async def fake_extract(job, title_id, ripped, sub_dir, report):
        return [(sub_dir / "0.sup", SubtitleTrack(track_id=0, codec="PGS", language="eng", is_pgs=True))]

    concurrent = 0
    peak = 0

    async def fake_ocr(sup_path, language, out_dir):
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0.02)  # hold the slot so overlap is observable
        concurrent -= 1
        out_dir.mkdir(parents=True, exist_ok=True)
        srt = out_dir / (sup_path.stem + ".srt")
        srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n", encoding="utf-8")
        return srt

    placed = []
    monkeypatch.setattr(pipe, "_ensure_ripped", fake_ripped)
    monkeypatch.setattr(pipe, "_extract_title_subs", fake_extract)
    monkeypatch.setattr(pipe.subtitleedit, "ocr_to_srt", fake_ocr)
    monkeypatch.setattr(pipe, "_place_title_subs", lambda *a, **k: placed.append(1))

    async def fake_grain(job, ripped, title_id, staging_root, report):
        return False

    monkeypatch.setattr(pipe, "_resolve_grain", fake_grain)

    await pipe.prep_phase(job, tmp_path / "stage" / "job_1", _Report())

    assert peak >= 2          # tracks across titles overlapped (not serialized)
    assert len(placed) == n   # all titles still placed


async def test_ocr_concurrency_capped(tmp_path, monkeypatch):
    settings = load_settings()
    monkeypatch.setattr(settings.storage, "output_dir", tmp_path)
    monkeypatch.setattr(settings.storage, "staging_dir", tmp_path / "stage")
    settings.subtitles.ocr_concurrency = 2  # cap at 2
    pipe = Pipeline(settings)
    job = _job(6)

    async def fake_ripped(job, title_id, staging_root, report):
        return tmp_path / f"t{title_id}.mkv"

    async def fake_extract(job, title_id, ripped, sub_dir, report):
        return [(sub_dir / "0.sup", SubtitleTrack(track_id=0, codec="PGS", language="eng", is_pgs=True))]

    concurrent = peak = 0

    async def fake_ocr(sup_path, language, out_dir):
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0.01)
        concurrent -= 1
        out_dir.mkdir(parents=True, exist_ok=True)
        srt = out_dir / (sup_path.stem + ".srt")
        srt.write_text("x", encoding="utf-8")
        return srt

    monkeypatch.setattr(pipe, "_ensure_ripped", fake_ripped)
    monkeypatch.setattr(pipe, "_extract_title_subs", fake_extract)
    monkeypatch.setattr(pipe.subtitleedit, "ocr_to_srt", fake_ocr)
    monkeypatch.setattr(pipe, "_place_title_subs", lambda *a, **k: None)

    async def fake_grain(*a, **k):
        return False

    monkeypatch.setattr(pipe, "_resolve_grain", fake_grain)

    await pipe.prep_phase(job, tmp_path / "stage" / "job_1", _Report())
    assert peak <= 2  # never exceeded the configured cap
