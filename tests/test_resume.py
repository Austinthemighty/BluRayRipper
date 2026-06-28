"""Checkpoint-aware resume in the pipeline: a completed title is skipped, and a
title with a finished rip reuses it instead of re-ripping."""

from pathlib import Path

import pytest

from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, EpisodeAssignment, MediaKind, Title
from blurayripper.pipeline import Job, Pipeline
from blurayripper.scheduler import EncodeScheduler


class DummyReport:
    def on_event(self, msg): pass
    def on_progress(self, stage, frac): pass
    def checkpoint(self): pass


def _pipeline(tmp_path):
    settings = load_settings()
    settings.storage.output_dir = tmp_path / "out"
    settings.storage.staging_dir = tmp_path / "stage"
    return Pipeline(settings)


def _job():
    job = Job(source="disc:0")
    job.disc = DiscInfo(source="disc:0", titles=[Title(title_id=0, duration_seconds=6000)])
    a = EpisodeAssignment(0, MediaKind.MOVIE, "WarGames", 1983)
    job.assignments = {0: a}
    job.selected_title_ids = [0]
    return job, a


async def test_placed_title_is_skipped(tmp_path, monkeypatch):
    p = _pipeline(tmp_path)
    job, a = _job()
    job.checkpoints = {0: {"placed": True}}

    ripped = {"called": False}

    async def boom(*args, **kwargs):
        ripped["called"] = True
        return []

    monkeypatch.setattr(p.makemkv, "rip_title", boom)

    # The final file must already exist for the skip to trigger.
    dest = p._video_path_for(a)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("already encoded")

    res = await p._process_one(job, 0, a, tmp_path / "stage" / "job", EncodeScheduler(3), DummyReport())
    assert res.video_path == dest
    assert ripped["called"] is False  # rip was skipped entirely


async def test_finished_rip_is_reused(tmp_path, monkeypatch):
    p = _pipeline(tmp_path)
    job, a = _job()
    ripped_file = tmp_path / "prior_rip.mkv"
    ripped_file.write_text("ripped data")
    job.checkpoints = {0: {"ripped_path": str(ripped_file)}}

    calls = {"rip": False, "encode": False}

    async def no_rip(*args, **kwargs):
        calls["rip"] = True
        return []

    async def no_subs(*args, **kwargs):
        return []

    async def no_height(*args, **kwargs):
        return None

    async def fake_encode(src, dst, *, on_progress=None, tune_grain=None):
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_text("encoded")
        calls["encode"] = True
        return dst

    monkeypatch.setattr(p.makemkv, "rip_title", no_rip)
    monkeypatch.setattr(p.mkvtools, "identify_subtitles", no_subs)
    monkeypatch.setattr(p.mkvtools, "video_height", no_height)
    monkeypatch.setattr(p.handbrake, "encode", fake_encode)

    res = await p._process_one(job, 0, a, tmp_path / "stage" / "job", EncodeScheduler(3), DummyReport())

    assert calls["rip"] is False      # rip reused, not re-run
    assert calls["encode"] is True    # encode still happened (not checkpointed)
    assert res.video_path.exists()
    assert job.checkpoints[0]["placed"] is True


async def test_resume_reuses_extracted_subtitles(tmp_path, monkeypatch):
    p = _pipeline(tmp_path)
    job, a = _job()
    ripped_file = tmp_path / "prior_rip.mkv"
    ripped_file.write_text("ripped data")
    sup = tmp_path / "prior.eng.sup"
    sup.write_text("pgs")
    job.checkpoints = {0: {
        "ripped_path": str(ripped_file),
        "subs": [{"sup": str(sup), "track_id": 6, "language": "eng",
                  "forced": False, "sdh": False, "codec": "HDMV PGS"}],
    }}

    calls = {"identify": False, "ocr": 0}

    async def no_identify(*a, **k):
        calls["identify"] = True
        return []

    async def fake_ocr(sup_path, language, out_dir):
        calls["ocr"] += 1
        srt = out_dir / (sup_path.stem + ".srt")
        srt.parent.mkdir(parents=True, exist_ok=True)
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
        return srt

    async def no_height(*a, **k):
        return None

    async def fake_encode(src, dst, *, on_progress=None, tune_grain=None):
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_text("enc")
        return dst

    monkeypatch.setattr(p.makemkv, "rip_title", no_height)  # must not be called anyway
    monkeypatch.setattr(p.mkvtools, "identify_subtitles", no_identify)
    monkeypatch.setattr(p.mkvtools, "video_height", no_height)
    monkeypatch.setattr(p.subtitleedit, "ocr_to_srt", fake_ocr)
    monkeypatch.setattr(p.handbrake, "encode", fake_encode)

    res = await p._process_one(job, 0, a, tmp_path / "stage" / "job", EncodeScheduler(3), DummyReport())

    assert calls["identify"] is False         # subtitle re-extraction skipped
    assert calls["ocr"] == 1                  # but OCR still runs on the reused .sup
    assert res.subtitle_paths and res.subtitle_paths[0].exists()


async def test_empty_subs_checkpoint_triggers_reextract(tmp_path, monkeypatch):
    """A prior failed extraction recorded subs=[]; that must NOT be treated as
    'done' — identify should run again."""
    p = _pipeline(tmp_path)
    job, a = _job()
    ripped = tmp_path / "r.mkv"
    ripped.write_text("x")
    job.checkpoints = {0: {"ripped_path": str(ripped), "subs": []}}

    calls = {"identify": False}

    async def identify(*args, **kwargs):
        calls["identify"] = True
        return []  # genuinely no PGS subs this time

    async def no_height(*args, **kwargs):
        return None

    async def fake_encode(src, dst, *, on_progress=None, tune_grain=None):
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_text("enc")
        return dst

    monkeypatch.setattr(p.mkvtools, "identify_subtitles", identify)
    monkeypatch.setattr(p.mkvtools, "video_height", no_height)
    monkeypatch.setattr(p.handbrake, "encode", fake_encode)

    await p._process_one(job, 0, a, tmp_path / "stage" / "job", EncodeScheduler(3), DummyReport())
    assert calls["identify"] is True   # empty checkpoint -> re-identified, not skipped
