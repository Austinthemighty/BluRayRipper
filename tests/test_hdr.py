"""HDR metadata parsing from ffprobe JSON -> x265 params."""

from blurayripper.pipeline import Pipeline
from blurayripper.services import hdr

SAMPLE = {
    "streams": [{"codec_type": "video", "color_primaries": "bt2020",
                 "color_transfer": "smpte2084", "color_space": "bt2020nc"}],
    "frames": [{"side_data_list": [
        {"side_data_type": "Mastering display metadata",
         "red_x": "34000/50000", "red_y": "16000/50000",
         "green_x": "13250/50000", "green_y": "34500/50000",
         "blue_x": "7500/50000", "blue_y": "3000/50000",
         "white_point_x": "15635/50000", "white_point_y": "16450/50000",
         "min_luminance": "50/10000", "max_luminance": "10000000/10000"},
        {"side_data_type": "Content light level metadata",
         "max_content": 1000, "max_average": 400},
    ]}],
    "format": {"duration": "7200.5"},
}


def test_parse_hdr():
    info = hdr.parse_hdr(SAMPLE)
    assert info.is_hdr
    assert info.colorprim == "bt2020"
    assert info.transfer == "smpte2084"
    assert info.colormatrix == "bt2020nc"
    # x265 order is G B R WP L, numerators taken verbatim
    assert info.master_display == (
        "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)")
    assert info.max_cll == "1000,400"


def test_x265_params_string():
    p = hdr.x265_params(hdr.parse_hdr(SAMPLE), ["tune=grain"])
    for token in ("hdr-opt=1", "colorprim=bt2020", "transfer=smpte2084",
                  "master-display=G(", "max-cll=1000,400", "tune=grain"):
        assert token in p


def test_sdr_is_not_hdr():
    info = hdr.parse_hdr({"streams": [{"codec_type": "video", "color_transfer": "bt709"}]})
    assert not info.is_hdr
    assert info.master_display is None and info.max_cll is None


def test_a_dropped_hdr10_output_is_reported_not_silently_shipped(tmp_path):
    """The failure this guards is invisible: an HDR10 source encoded to an SDR
    file still plays, it just looks washed out on an HDR TV. The log has to say
    so, because nothing else will until you're on the couch months later."""
    import asyncio
    from blurayripper.services.hdr import HdrInfo

    want = HdrInfo(colorprim="bt2020", transfer="smpte2084", colormatrix="bt2020nc",
                   master_display="G(..)B(..)R(..)WP(..)L(1000,1)", max_cll="1000,400")
    stripped = HdrInfo(colorprim="bt709", transfer="bt709", colormatrix="bt709")

    class FakePipeline:
        ffmpeg = type("F", (), {
            "probe_video": staticmethod(
                lambda dst: asyncio.sleep(0, result=(stripped, 0.0)))})()
        _verify_hdr = Pipeline._verify_hdr

    events = []
    report = type("R", (), {"on_event": staticmethod(events.append)})()
    asyncio.run(FakePipeline()._verify_hdr(tmp_path / "out.mkv", want, report))

    assert any("HDR CHECK FAILED" in e for e in events)
    assert "master-display" in events[-1] and "max-cll" in events[-1]


def test_an_hdr_output_that_kept_its_metadata_says_so(tmp_path):
    """The passing case has to be visible too — otherwise 'no news' is
    indistinguishable from 'the check never ran'."""
    import asyncio
    from blurayripper.services.hdr import HdrInfo

    want = HdrInfo(colorprim="bt2020", transfer="smpte2084", colormatrix="bt2020nc",
                   master_display="G(..)L(1000,1)", max_cll="1000,400")

    class FakePipeline:
        ffmpeg = type("F", (), {
            "probe_video": staticmethod(
                lambda dst: asyncio.sleep(0, result=(want, 0.0)))})()
        _verify_hdr = Pipeline._verify_hdr

    events = []
    report = type("R", (), {"on_event": staticmethod(events.append)})()
    asyncio.run(FakePipeline()._verify_hdr(tmp_path / "out.mkv", want, report))

    assert any("HDR check passed" in e and "HDR10" in e for e in events)


def test_an_sdr_encode_is_not_checked_for_hdr(tmp_path):
    """A 1080p SDR title has no HDR to lose — warning about it would be noise."""
    import asyncio
    from blurayripper.services.hdr import HdrInfo

    class FakePipeline:
        ffmpeg = None            # must never be touched
        _verify_hdr = Pipeline._verify_hdr

    events = []
    report = type("R", (), {"on_event": staticmethod(events.append)})()
    asyncio.run(FakePipeline()._verify_hdr(
        tmp_path / "out.mkv", HdrInfo(transfer="bt709"), report))
    assert events == []
