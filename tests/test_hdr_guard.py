"""A UHD title that falls back to HandBrake (ffmpeg path unavailable) must warn +
notify that HDR/Dolby Vision won't be preserved, instead of silently making SDR."""

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


def _pipe(tmp_path, monkeypatch, ffmpeg_uhd, ffmpeg_available):
    s = load_settings()
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
