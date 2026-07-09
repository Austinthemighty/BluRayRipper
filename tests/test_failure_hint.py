"""Failure hints + correct phase reporting on a failed job."""

import pytest

from blurayripper.pipeline import Job, JobState, Pipeline, _failure_hint
from blurayripper.config import load_settings


def test_hint_for_out_of_memory():
    e = OSError("boom"); e.winerror = 1455
    assert "virtual memory" in _failure_hint(e)
    assert "virtual memory" in _failure_hint(MemoryError())


def test_no_hint_for_ordinary_errors():
    assert _failure_hint(ValueError("nope")) is None
    assert _failure_hint(OSError("disk gone")) is None  # no winerror set


async def test_execute_reports_real_phase_not_failed(monkeypatch):
    """A failure during encoding logs 'FAILED during encoding', not the old
    'FAILED during failed' (state was flipped before the message)."""
    pipe = Pipeline(load_settings())
    job = Job(source="disc:0")

    async def boom(job, staging_root, scheduler, report):
        job.state = JobState.ENCODING
        raise OSError("kaboom")

    monkeypatch.setattr(pipe, "_process_titles", boom)

    events = []
    class R:
        def on_event(self, m): events.append(m)
        def on_progress(self, *a): pass
        def checkpoint(self): pass

    with pytest.raises(OSError):
        await pipe.execute(job, scheduler=None, report=R())
    assert any("FAILED during encoding" in e for e in events)
    assert job.state is JobState.FAILED
