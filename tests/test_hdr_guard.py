"""A UHD title that would fall back to HandBrake loses HDR/Dolby Vision.

By default that is BLOCKED (uhd_require_ffmpeg). With the block turned off it
must still warn + notify rather than silently producing SDR."""

import types

from blurayripper.config import load_settings
from blurayripper.models import EpisodeAssignment, MediaKind
from blurayripper.pipeline import Job, Pipeline
from blurayripper.scheduler import EncodeScheduler


class _R:
    def __init__(self): self.events = []; self.notes = []
    def on_event(self, m): self.events.append(m)
    def on_progress(self, *a): pass
    def notify(self, title, message): self.notes.append(title)


async def _run(pipe, report, tmp_path):
    job = Job(source="disc:0")
    a = EpisodeAssignment(0, MediaKind.MOVIE, "WALL-E", 2008)
    job.assignments = {0: a}
    await pipe._encode_title(job, 0, a, tmp_path / "stg", tmp_path / "x.mkv",
                             EncodeScheduler(3), report)


def _pipe(tmp_path, monkeypatch, ffmpeg_uhd, ffmpeg_available, block_uhd=False):
    s = load_settings()
    s.encode.uhd_require_ffmpeg = block_uhd   # default True; off here to reach the warn path
    s.encode.ffmpeg_uhd = ffmpeg_uhd
    s.encode.uhd_min_height = 2160
    s.write_nfo = False
    monkeypatch.setattr(s.storage, "output_dir", tmp_path)
    pipe = Pipeline(s)
    monkeypatch.setattr(pipe, "ffmpeg", types.SimpleNamespace(available=ffmpeg_available))

    async def probe(path, report):
        return 2160  # a UHD title
    monkeypatch.setattr(pipe, "_probe_height", probe)

    async def fake_hb(src, dst, **k):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("x")
    monkeypatch.setattr(pipe.handbrake, "encode", fake_hb)
    monkeypatch.setattr("blurayripper.pipeline.library.place", lambda enc, dest: dest)
    return pipe


async def test_uhd_handbrake_with_ffmpeg_wanted_warns_and_notifies(tmp_path, monkeypatch):
    # ffmpeg_uhd ON but ffmpeg missing here -> the dangerous silent downgrade.
    pipe = _pipe(tmp_path, monkeypatch, ffmpeg_uhd=True, ffmpeg_available=False)
    r = _R()
    await _run(pipe, r, tmp_path)
    assert any("HDR not preserved" in n for n in r.notes)
    assert any("will NOT be preserved" in e for e in r.events)


async def test_uhd_handbrake_when_ffmpeg_disabled_notes_but_no_notify(tmp_path, monkeypatch):
    # ffmpeg_uhd deliberately OFF -> a note in the log, but no notification spam.
    pipe = _pipe(tmp_path, monkeypatch, ffmpeg_uhd=False, ffmpeg_available=False)
    r = _R()
    await _run(pipe, r, tmp_path)
    assert r.notes == []
    assert any("won't keep HDR" in e for e in r.events)


async def test_no_warning_when_ffmpeg_path_is_used(tmp_path, monkeypatch):
    # ffmpeg available + enabled -> takes the UHD path, no downgrade warning.
    pipe = _pipe(tmp_path, monkeypatch, ffmpeg_uhd=True, ffmpeg_available=True)

    async def fake_uhd(src, dst, grain, encode_dir, report):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("x")
    monkeypatch.setattr(pipe, "_encode_uhd", fake_uhd)
    r = _R()
    await _run(pipe, r, tmp_path)
    assert r.notes == []
    assert not any("HDR" in e and "NOT" in e for e in r.events)


async def test_uhd_is_blocked_from_handbrake_by_default(tmp_path, monkeypatch):
    """The default: refuse rather than spend hours making an SDR 4K file. The
    message has to say which setting to change and that the rip is kept."""
    import pytest
    pipe = _pipe(tmp_path, monkeypatch, ffmpeg_uhd=False, ffmpeg_available=True,
                 block_uhd=True)
    r = _R()
    with pytest.raises(RuntimeError) as exc:
        await _run(pipe, r, tmp_path)
    msg = str(exc.value)
    assert "UHD" in msg and "HandBrake" in msg
    assert "disabled in Settings" in msg          # names the actual cause
    assert "Retry" in msg and "rip is kept" in msg


async def test_block_message_distinguishes_a_missing_ffmpeg(tmp_path, monkeypatch):
    """Same block, different cause: the intent WAS to keep HDR, but this box has
    no ffmpeg — that's a per-worker install problem, not a settings one."""
    import pytest
    pipe = _pipe(tmp_path, monkeypatch, ffmpeg_uhd=True, ffmpeg_available=False,
                 block_uhd=True)
    with pytest.raises(RuntimeError) as exc:
        await _run(pipe, _R(), tmp_path)
    assert "isn't available on this machine" in str(exc.value)


async def test_sub_uhd_titles_are_untouched_by_the_block(tmp_path, monkeypatch):
    """The guard must only fire on UHD — a 1080p title still encodes normally."""
    pipe = _pipe(tmp_path, monkeypatch, ffmpeg_uhd=False, ffmpeg_available=True,
                 block_uhd=True)

    async def probe(path, report):
        return 1080
    monkeypatch.setattr(pipe, "_probe_height", probe)
    await _run(pipe, _R(), tmp_path)      # no raise
