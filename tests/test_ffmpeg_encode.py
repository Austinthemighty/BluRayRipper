"""ffmpeg UHD encode command construction + progress-time parsing."""

from pathlib import Path

from blurayripper.config import load_settings
from blurayripper.services.hdr import HdrInfo
from blurayripper.tools.ffmpeg import FFmpeg, _parse_time


def test_parse_time():
    assert abs(_parse_time("00:01:23.500000") - 83.5) < 0.001
    assert _parse_time("garbage") == 0.0


def _ff(monkeypatch):
    settings = load_settings()
    monkeypatch.setattr(settings.tools, "ffmpeg", "/usr/bin/ffmpeg")
    return FFmpeg(settings)


def test_build_encode_command_hdr(monkeypatch):
    ff = _ff(monkeypatch)
    info = HdrInfo(colorprim="bt2020", transfer="smpte2084", colormatrix="bt2020nc",
                   master_display="G(1,2)B(3,4)R(5,6)WP(7,8)L(9,10)", max_cll="1000,400")
    cmd = ff.build_encode_command(Path("in.mkv"), Path("out.mkv"), hdr=info, crf=18,
                                  preset="medium", audio_mode="copy", tune_grain=True)
    s = " ".join(cmd)
    assert "libx265" in s and "-crf 18" in s and "yuv420p10le" in s
    assert "-color_trc smpte2084" in s and "-c:a copy" in s and "-map_chapters 0" in s
    xp = cmd[cmd.index("-x265-params") + 1]
    assert "master-display=G(1,2)" in xp and "max-cll=1000,400" in xp and "tune=grain" in xp


def test_build_encode_command_sdr_aac(monkeypatch):
    ff = _ff(monkeypatch)
    cmd = ff.build_encode_command(Path("in.mkv"), Path("out.mp4"), hdr=HdrInfo(), crf=20,
                                  preset="slow", audio_mode="aac", tune_grain=False)
    s = " ".join(cmd)
    assert "-c:a aac" in s and "-preset slow" in s
    assert "master-display" not in s   # no HDR metadata -> not emitted
