"""HDR metadata parsing from ffprobe JSON -> x265 params."""

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
