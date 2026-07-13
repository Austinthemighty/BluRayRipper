"""Post-rip duration verification: reject a truncated rip MakeMKV exited 0 on."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from blurayripper.config import load_settings
from blurayripper.models import DiscInfo, Title
from blurayripper.pipeline import Job, Pipeline


def _job(expected_seconds):
    job = Job(source="disc:0")
    job.disc = DiscInfo(source="disc:0", disc_name="X",
                        titles=[Title(title_id=1, duration_seconds=expected_seconds)])
    return job


class _Report:
    def __init__(self): self.events = []
    def on_event(self, m): self.events.append(m)
    def on_progress(self, *a): pass


def _probe(dur):
    async def _p(path): return SimpleNamespace(duration_seconds=dur)
    return _p


@pytest.fixture
def pipe():
    s = load_settings()
    s.rip_duration_tolerance_pct = 3
    return Pipeline(s)


async def test_matching_duration_passes(pipe):
    pipe.mkvtools.probe = _probe(1700)
    r = _Report()
    await pipe._verify_rip_duration(_job(1711), 1, Path("x.mkv"), r)  # no raise
    assert any("verified" in e for e in r.events)


async def test_truncated_rip_is_rejected(pipe):
    pipe.mkvtools.probe = _probe(900)  # ~52% of 1711
    with pytest.raises(RuntimeError, match="truncated"):
        await pipe._verify_rip_duration(_job(1711), 1, Path("x.mkv"), _Report())


async def test_boundary_at_tolerance_floor(pipe):
    # 3% tolerance on 1000s -> floor 970s: 970 passes, 969 fails.
    pipe.mkvtools.probe = _probe(970)
    await pipe._verify_rip_duration(_job(1000), 1, Path("x.mkv"), _Report())
    pipe.mkvtools.probe = _probe(969)
    with pytest.raises(RuntimeError):
        await pipe._verify_rip_duration(_job(1000), 1, Path("x.mkv"), _Report())


async def test_disabled_skips_probe(pipe):
    pipe.settings.rip_duration_tolerance_pct = 0
    called = []
    async def spy(p): called.append(1); return SimpleNamespace(duration_seconds=0)
    pipe.mkvtools.probe = spy
    await pipe._verify_rip_duration(_job(1711), 1, Path("x.mkv"), _Report())
    assert not called  # never even probes when off


async def test_unknown_expected_duration_skips(pipe):
    pipe.mkvtools.probe = _probe(10)
    await pipe._verify_rip_duration(_job(0), 1, Path("x.mkv"), _Report())  # no raise


async def test_probe_failure_does_not_block(pipe):
    async def boom(p): raise OSError("mkvmerge died")
    pipe.mkvtools.probe = boom
    r = _Report()
    await pipe._verify_rip_duration(_job(1711), 1, Path("x.mkv"), r)  # no raise
    assert any("skipped" in e for e in r.events)
