"""dovi_tool command construction + profile-mode mapping + DV detection."""

from pathlib import Path

from blurayripper.config import load_settings
from blurayripper.services import hdr
from blurayripper.tools.dovitool import DoviTool, mode_for_profile


def test_mode_for_profile():
    assert mode_for_profile(7) == 2   # dual-layer -> 8.1
    assert mode_for_profile(5) == 3   # IPT-PQ -> 8.1
    assert mode_for_profile(8) == 0   # already fine
    assert mode_for_profile(None) == 0


def _dt(monkeypatch):
    s = load_settings()
    monkeypatch.setattr(s.tools, "dovitool", "/usr/bin/dovi_tool")
    return DoviTool(s)


def test_extract_rpu_command_converts_profile7(monkeypatch):
    dt = _dt(monkeypatch)
    cmd = dt.extract_rpu_command(Path("in.hevc"), Path("rpu.bin"), 7)
    assert cmd == ["/usr/bin/dovi_tool", "-m", "2", "extract-rpu", "in.hevc", "-o", "rpu.bin"]


def test_extract_rpu_command_profile8_no_mode(monkeypatch):
    dt = _dt(monkeypatch)
    cmd = dt.extract_rpu_command(Path("in.hevc"), Path("rpu.bin"), 8)
    assert "-m" not in cmd   # mode 0 -> flag omitted


def test_inject_rpu_command(monkeypatch):
    dt = _dt(monkeypatch)
    cmd = dt.inject_rpu_command(Path("enc.hevc"), Path("rpu.bin"), Path("out.hevc"))
    assert cmd == ["/usr/bin/dovi_tool", "inject-rpu", "-i", "enc.hevc",
                   "--rpu-in", "rpu.bin", "-o", "out.hevc"]


def test_detects_dolby_vision():
    probe = {"streams": [{"codec_type": "video", "color_transfer": "smpte2084",
             "side_data_list": [{"side_data_type": "DOVI configuration record",
                                 "dv_profile": 7, "dv_level": 6}]}]}
    info = hdr.parse_hdr(probe)
    assert info.is_dolby_vision and info.dv_profile == 7


def test_hdr10_is_not_dolby_vision():
    info = hdr.parse_hdr({"streams": [{"codec_type": "video", "color_transfer": "smpte2084"}]})
    assert not info.is_dolby_vision
