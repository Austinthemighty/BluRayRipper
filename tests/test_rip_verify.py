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


async def test_a_rip_that_cannot_be_read_back_is_rejected(pipe):
    """This runs on a file just written, so a file mkvmerge cannot parse is a
    broken file — not an unknown. Treating "can't tell" as "fine" is exactly how
    two failed rips reached the encode queue: MakeMKV died mid-write, the
    partial file could not be probed, the check skipped, and the jobs were
    queued as ready with nothing to encode."""
    import pytest

    async def boom(p): raise OSError("mkvmerge died")
    pipe.mkvtools.probe = boom

    with pytest.raises(RuntimeError, match="can't be read back"):
        await pipe._verify_rip_duration(_job(1711), 1, Path("x.mkv"), _Report())


async def test_an_unknown_expected_duration_still_skips_quietly(pipe):
    """The genuine can't-compare case: the scan reported no duration, so there
    is nothing to check against. That must stay non-blocking."""
    pipe.mkvtools.probe = _probe(10)
    r = _Report()
    await pipe._verify_rip_duration(_job(0), 1, Path("x.mkv"), r)   # no raise
    assert not any("can't be read back" in e for e in r.events)


async def test_a_rip_holding_far_more_than_its_title_is_rejected(pipe):
    """Job 380 believed it was ripping a 21-minute How I Met Your Mother episode
    and actually read a Landman Blu-ray from the other drive. It produced 3394s
    and 4338s files against 1292s titles, and this check waved both through —
    it only ever looked downwards, because it was written to catch truncation.

    A file three times the length of the title it was asked for is not a long
    episode; it is different content."""
    pipe.mkvtools.probe = _probe(3394)
    r = _Report()
    with pytest.raises(RuntimeError) as exc:
        await pipe._verify_rip_duration(_job(1292), 1, Path("x.mkv"), r)
    assert "holds more than this title" in str(exc.value)
    assert "3394s" in str(exc.value) and "1292s" in str(exc.value)
    assert not r.events, "a rejected rip must not also report itself verified"


async def test_the_normal_second_or_two_of_overshoot_still_passes(pipe):
    """Real rips from the live box: 1296s against a 1294s title, 1254s against
    1252s. A good rip routinely runs slightly OVER the scan's figure, so the
    ceiling cannot be symmetric with the 3% truncation tolerance — at that width
    it would sit close enough to trip on rounding."""
    for actual, expected in ((1296, 1294), (1254, 1252), (1295, 1293), (1292, 1288)):
        pipe.mkvtools.probe = _probe(actual)
        r = _Report()
        await pipe._verify_rip_duration(_job(expected), 1, Path("x.mkv"), r)
        assert r.events and "Rip verified" in r.events[0]


async def test_the_ceiling_does_not_tighten_when_the_tolerance_does(pipe):
    """Setting a strict truncation tolerance is about catching short rips. It
    must not make the app start rejecting good ones for being two seconds long."""
    pipe.settings.rip_duration_tolerance_pct = 1
    pipe.mkvtools.probe = _probe(1296)
    r = _Report()
    await pipe._verify_rip_duration(_job(1294), 1, Path("x.mkv"), r)   # no raise
    assert "Rip verified" in r.events[0]

    # ...and a genuinely wrong file is still caught at that setting.
    pipe.mkvtools.probe = _probe(4338)
    with pytest.raises(RuntimeError, match="holds more than this title"):
        await pipe._verify_rip_duration(_job(1252), 1, Path("x.mkv"), _Report())


async def test_a_disabled_tolerance_still_checks_nothing(pipe):
    """0 means off, and off has to mean both directions — otherwise turning the
    check off would silently leave half of it running."""
    pipe.settings.rip_duration_tolerance_pct = 0
    pipe.mkvtools.probe = _probe(9999)
    r = _Report()
    await pipe._verify_rip_duration(_job(1292), 1, Path("x.mkv"), r)   # no raise
    assert r.events == []
