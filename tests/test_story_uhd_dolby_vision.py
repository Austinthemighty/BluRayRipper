"""Journey: "I put a 4K disc in and I do not want a silently worse file."

Every failure mode here is invisible at the time. HandBrake produces a perfectly
watchable UHD file with the HDR stripped, and a Dolby Vision disc that quietly
falls back to HDR10 still plays. You find out months later, on the couch, on the
one TV that would have shown the difference — so the guards, the fallbacks and
the after-the-fact verification all have to be pinned.
"""

import asyncio

import pytest

from blurayripper.config import load_settings
from blurayripper.models import EpisodeAssignment, MediaKind
from blurayripper.pipeline import Job, Pipeline
from blurayripper.services.hdr import HdrInfo

HDR10 = HdrInfo(colorprim="bt2020", transfer="smpte2084", colormatrix="bt2020nc",
                master_display="G(8500,39850)B(6550,2300)R(35400,14600)"
                               "WP(15635,16450)L(10000000,1)",
                max_cll="1000,400")
DV = HdrInfo(colorprim="bt2020", transfer="smpte2084", colormatrix="bt2020nc",
             master_display="G(1)L(1,1)", max_cll="500,100", dv_profile=7)
SDR = HdrInfo(colorprim="bt709", transfer="bt709", colormatrix="bt709")


class _Report:
    def __init__(self): self.events = []
    def on_event(self, m): self.events.append(m)
    def on_progress(self, *a, **k): pass
    def checkpoint(self): pass
    def notify(self, *a, **k): pass
    def ejected(self, *a, **k): pass


def _pipe(tmp_path, **enc):
    s = load_settings()
    s.storage.output_dir = tmp_path / "out"
    s.storage.staging_dir = tmp_path / "stage"
    s.write_nfo = False
    for k, v in enc.items():
        setattr(s.encode, k, v)
    return Pipeline(s)


def _job():
    job = Job(source="disc:0")
    job.selected_title_ids = [0]
    return job


def _assignment():
    return EpisodeAssignment(title_id=0, kind=MediaKind.MOVIE,
                             show_or_movie_title="Dune", year=2021)


async def _encode(p, tmp_path, height, report):
    """Run the encode step for one title at a given height."""
    async def probe(path, rep): return height
    p._probe_height = probe
    ripped = tmp_path / "ripped.mkv"
    ripped.write_bytes(b"rip")
    return await p._encode_title(_job(), 0, _assignment(), tmp_path, ripped,
                                 _Sched(), report)


class _Sched:
    async def acquire(self, weight): return weight
    async def release(self, acquired): pass


# --- the guard: HandBrake must not silently strip HDR ------------------------

def test_a_uhd_title_is_refused_rather_than_encoded_without_its_hdr(tmp_path):
    """HandBrake would produce a watchable file, so the loss is silent — and a 4K
    encode runs for hours before you could notice. Failing costs seconds and
    keeps the rip, so only the encode re-runs after the setting is fixed."""
    p = _pipe(tmp_path, ffmpeg_uhd=False, uhd_require_ffmpeg=True)
    rep = _Report()

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(_encode(p, tmp_path, 2160, rep))

    msg = str(exc.value)
    assert "2160p" in msg and "HandBrake" in msg
    assert "ffmpeg UHD path" in msg           # names the setting to change
    assert "the rip is kept" in msg           # and that a retry is cheap


def test_a_1080p_title_is_never_touched_by_the_uhd_guard(tmp_path, monkeypatch):
    """The guard must fire on UHD only. Blocking a 1080p disc would stop the
    ordinary case dead."""
    p = _pipe(tmp_path, ffmpeg_uhd=False, uhd_require_ffmpeg=True)
    placed = []

    async def hb_encode(src, dst, **kw):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"handbrake output")
        placed.append(dst)
        return dst

    monkeypatch.setattr(p.handbrake, "encode", hb_encode)
    monkeypatch.setattr("blurayripper.pipeline.library.place", lambda s, d: d)

    asyncio.run(_encode(p, tmp_path, 1080, _Report()))
    assert len(placed) == 1                   # HandBrake did it, no raise


def test_turning_the_guard_off_accepts_sdr_but_says_so_loudly(tmp_path, monkeypatch):
    """The escape hatch has to exist — but taking it must be announced, or the
    next person cannot tell an SDR file from a mistake."""
    p = _pipe(tmp_path, ffmpeg_uhd=False, uhd_require_ffmpeg=False)
    rep = _Report()

    async def hb_encode(src, dst, **kw):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"sdr")
        return dst

    monkeypatch.setattr(p.handbrake, "encode", hb_encode)
    monkeypatch.setattr("blurayripper.pipeline.library.place", lambda s, d: d)

    asyncio.run(_encode(p, tmp_path, 2160, rep))
    assert any("won't keep HDR" in e or "HDR" in e for e in rep.events)


# --- the ffmpeg path actually carries the metadata ---------------------------

def test_the_uhd_encode_carries_every_piece_of_hdr_signalling(tmp_path):
    """These flags ARE the HDR. Dropping any one of them produces a file that
    plays and looks wrong, which nothing downstream can detect."""
    p = _pipe(tmp_path, ffmpeg_uhd=True)
    cmd = p.ffmpeg.build_encode_command(
        tmp_path / "in.mkv", tmp_path / "out.mkv", hdr=HDR10, crf=18,
        preset="slower", audio_mode="copy", tune_grain=False)

    assert "libx265" in cmd
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p10le"   # 10-bit or no HDR
    assert cmd[cmd.index("-color_primaries") + 1] == "bt2020"
    assert cmd[cmd.index("-color_trc") + 1] == "smpte2084"
    assert cmd[cmd.index("-colorspace") + 1] == "bt2020nc"
    params = cmd[cmd.index("-x265-params") + 1]
    for piece in ("hdr-opt=1", "colorprim=bt2020", "transfer=smpte2084",
                  "master-display=", "max-cll=1000,400"):
        assert piece in params, params


def test_an_sdr_source_gets_no_hdr_flags_invented_for_it(tmp_path):
    """Signalling HDR on an SDR source is its own corruption — the player would
    tone-map footage that was never HDR."""
    p = _pipe(tmp_path, ffmpeg_uhd=True)
    cmd = p.ffmpeg.build_encode_command(
        tmp_path / "in.mkv", tmp_path / "out.mkv", hdr=SDR, crf=18,
        preset="medium", audio_mode="copy", tune_grain=False)

    params = cmd[cmd.index("-x265-params") + 1]
    assert "master-display" not in params and "max-cll" not in params


# --- Dolby Vision: preserved when possible, degraded not failed otherwise -----

def test_dolby_vision_takes_the_rpu_round_trip_in_order(tmp_path, monkeypatch):
    """DV survives only if every step happens and in this order: pull the RPU
    off the source, re-encode the base layer, inject the RPU back, then mux with
    the original audio. A step out of order yields a file Plex reads as plain
    HDR10."""
    p = _pipe(tmp_path, ffmpeg_uhd=True, dovi=True)
    steps = []

    async def probe(src): return DV, 100.0
    async def extract_hevc(src, out): steps.append("extract_hevc"); out.write_bytes(b"h")
    async def extract_rpu(src, out, profile): steps.append("extract_rpu"); out.write_bytes(b"r")
    async def encode_hevc(src, out, **kw): steps.append("encode_hevc"); out.write_bytes(b"e")
    async def inject_rpu(src, rpu, out): steps.append("inject_rpu"); out.write_bytes(b"d")
    async def mux(video, audio, out):
        steps.append("mux")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"mkv")
        return out

    monkeypatch.setattr(p.ffmpeg, "probe_video", probe)
    monkeypatch.setattr(p.ffmpeg, "extract_hevc", extract_hevc)
    monkeypatch.setattr(p.ffmpeg, "encode_hevc", encode_hevc)
    monkeypatch.setattr(p.dovitool, "extract_rpu", extract_rpu)
    monkeypatch.setattr(p.dovitool, "inject_rpu", inject_rpu)
    monkeypatch.setattr(p.mkvtools, "mux_dovi", mux)
    monkeypatch.setattr(type(p.dovitool), "available", property(lambda self: True))
    p.settings.tools.mkvmerge = "/usr/bin/mkvmerge"

    asyncio.run(p._encode_uhd(tmp_path / "in.mkv", tmp_path / "o" / "out.mkv",
                              False, tmp_path, _Report()))

    assert steps == ["extract_hevc", "extract_rpu", "encode_hevc",
                     "inject_rpu", "mux"]


def test_a_broken_dolby_vision_step_falls_back_to_hdr10_instead_of_failing(
        tmp_path, monkeypatch):
    """Losing DV is a downgrade; losing the job is a lost evening. The disc has
    already been ripped by this point, so degrade rather than abort."""
    p = _pipe(tmp_path, ffmpeg_uhd=True, dovi=True)
    rep = _Report()
    fell_back = []

    async def probe(src): return DV, 100.0
    async def boom(*a, **k): raise RuntimeError("dovi_tool exploded")
    async def plain_encode(src, dst, **kw):
        fell_back.append(dst)
        return dst

    monkeypatch.setattr(p.ffmpeg, "probe_video", probe)
    monkeypatch.setattr(p.ffmpeg, "extract_hevc", boom)
    monkeypatch.setattr(p.ffmpeg, "encode", plain_encode)
    monkeypatch.setattr(p, "_verify_hdr", _anoop)
    monkeypatch.setattr(type(p.dovitool), "available", property(lambda self: True))
    p.settings.tools.mkvmerge = "/usr/bin/mkvmerge"

    asyncio.run(p._encode_uhd(tmp_path / "in.mkv", tmp_path / "out.mkv",
                              False, tmp_path, rep))

    assert len(fell_back) == 1                       # the HDR10 encode still ran
    assert any("falling back to HDR10" in e for e in rep.events)


def test_a_dv_disc_without_the_tools_says_so_rather_than_pretending(
        tmp_path, monkeypatch):
    """dovi_tool missing on the box that happens to claim the job is the common
    cluster case, and the resulting file is HDR10 — worth a line in the log."""
    p = _pipe(tmp_path, ffmpeg_uhd=True, dovi=True)
    rep = _Report()

    async def probe(src): return DV, 100.0
    async def plain_encode(src, dst, **kw): return dst

    monkeypatch.setattr(p.ffmpeg, "probe_video", probe)
    monkeypatch.setattr(p.ffmpeg, "encode", plain_encode)
    monkeypatch.setattr(p, "_verify_hdr", _anoop)
    monkeypatch.setattr(type(p.dovitool), "available", property(lambda self: False))

    asyncio.run(p._encode_uhd(tmp_path / "in.mkv", tmp_path / "out.mkv",
                              False, tmp_path, rep))
    assert any("dovi_tool" in e and "HDR10 only" in e for e in rep.events)


# --- and afterwards, prove it actually survived ------------------------------

def test_an_output_that_lost_its_hdr_is_reported_not_shipped_quietly(
        tmp_path, monkeypatch):
    """The last line of defence. Everything above can be right and the encoder
    still drop the metadata; without this the job goes green regardless."""
    p = _pipe(tmp_path, ffmpeg_uhd=True)
    rep = _Report()
    stripped = HdrInfo(colorprim="bt709", transfer="bt709", colormatrix="bt709")

    async def probe(dst): return stripped, 0.0
    monkeypatch.setattr(p.ffmpeg, "probe_video", probe)

    asyncio.run(p._verify_hdr(tmp_path / "out.mkv", HDR10, rep))

    failure = next(e for e in rep.events if "HDR CHECK FAILED" in e)
    assert "master-display" in failure and "max-cll" in failure


def test_an_output_that_kept_its_hdr_says_so(tmp_path, monkeypatch):
    """"No news" must not be the success signal — it is indistinguishable from a
    check that never ran."""
    p = _pipe(tmp_path, ffmpeg_uhd=True)
    rep = _Report()

    async def probe(dst): return HDR10, 0.0
    monkeypatch.setattr(p.ffmpeg, "probe_video", probe)

    asyncio.run(p._verify_hdr(tmp_path / "out.mkv", HDR10, rep))
    assert any("HDR check passed" in e for e in rep.events)


async def _anoop(*a, **k):
    return None
