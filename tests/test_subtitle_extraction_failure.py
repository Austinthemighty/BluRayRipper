"""A genuine subtitle-extraction failure must fail the job (so a retry re-extracts),
not silently place a subtitle-less video and mark the title done."""

import pytest

from blurayripper.config import load_settings
from blurayripper.pipeline import Job, Pipeline


class _R:
    def on_event(self, *a): pass
    def on_progress(self, *a): pass


async def test_extraction_failure_raises(tmp_path, monkeypatch):
    s = load_settings()
    monkeypatch.setattr(s.storage, "staging_dir", tmp_path)
    pipe = Pipeline(s)
    job = Job(source="disc:0")

    async def fail(*a, **k):
        return None  # mkvextract/identify error
    monkeypatch.setattr(pipe, "_extract_subs", fail)

    with pytest.raises(RuntimeError, match="Subtitle extraction failed"):
        await pipe._extract_title_subs(job, 0, tmp_path / "x.mkv", tmp_path / "sub", _R())
    # Not checkpointed -> a retry re-attempts extraction for this title.
    assert "subs" not in job.checkpoints.get(0, {})


async def test_no_kept_subs_is_not_a_failure(tmp_path, monkeypatch):
    s = load_settings()
    monkeypatch.setattr(s.storage, "staging_dir", tmp_path)
    pipe = Pipeline(s)
    job = Job(source="disc:0")

    async def none_kept(*a, **k):
        return []  # disc genuinely has no kept-language subtitles
    monkeypatch.setattr(pipe, "_extract_subs", none_kept)

    out = await pipe._extract_title_subs(job, 1, tmp_path / "x.mkv", tmp_path / "sub", _R())
    assert out == []                       # proceeds normally, no raise
    assert job.checkpoints[1]["subs"] == []  # checkpointed as "handled, none"
