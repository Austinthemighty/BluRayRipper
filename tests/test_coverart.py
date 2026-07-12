"""Disc cover-art extraction (BDMV/META + DVD JACKET_P)."""

from blurayripper.models import DiscInfo
from blurayripper.services import coverart


def _make_bd(root, *, big=5000, small=500, with_xml=True):
    dl = root / "BDMV" / "META" / "DL"
    dl.mkdir(parents=True)
    (dl / "big.jpg").write_bytes(b"x" * big)
    (dl / "small.jpg").write_bytes(b"x" * small)
    if with_xml:
        (dl / "bdmt_eng.xml").write_text(
            '<?xml version="1.0"?><disclib xmlns:di="urn:BDA:bdmv;discinfo">'
            '<di:discinfo><di:title><di:name>The Big Movie</di:name></di:title>'
            '</di:discinfo></disclib>', encoding="utf-8")


def test_find_cover_images_largest_first(tmp_path):
    _make_bd(tmp_path, big=5000, small=500)
    imgs = coverart.find_cover_images(tmp_path)
    assert [p.name for p in imgs] == ["big.jpg", "small.jpg"]


def test_find_includes_dvd_jacket(tmp_path):
    jp = tmp_path / "JACKET_P"
    jp.mkdir()
    (jp / "J00___5L.jpg").write_bytes(b"y" * 800)
    imgs = coverart.find_cover_images(tmp_path)
    assert any(p.name == "J00___5L.jpg" for p in imgs)


# --- tiny valid JPEGs of a chosen orientation (SOF0 with width/height) ---
def _jpeg(w: int, h: int) -> bytes:
    sof = b"\xff\xc0\x00\x11\x08" + h.to_bytes(2, "big") + w.to_bytes(2, "big") + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8" + sof + b"\xff\xd9"


def test_image_size_parses_jpeg(tmp_path):
    p = tmp_path / "x.jpg"
    p.write_bytes(_jpeg(1920, 1080))
    assert coverart.image_size(p) == (1920, 1080)


def test_classify_splits_portrait_and_landscape(tmp_path):
    dl = tmp_path / "BDMV" / "META" / "DL"
    dl.mkdir(parents=True)
    (dl / "wide.jpg").write_bytes(_jpeg(1920, 1080))    # landscape -> fanart
    (dl / "tall.jpg").write_bytes(_jpeg(600, 900))      # portrait  -> poster
    portraits, landscapes = coverart.classify_images(tmp_path)
    assert [p.name for p in portraits] == ["tall.jpg"]
    assert [p.name for p in landscapes] == ["wide.jpg"]


def test_extract_art_lands_poster_and_fanart(tmp_path):
    dl = tmp_path / "BDMV" / "META" / "DL"
    dl.mkdir(parents=True)
    (dl / "wide.jpg").write_bytes(_jpeg(1920, 1080))
    (dl / "tall.jpg").write_bytes(_jpeg(600, 900))
    out = tmp_path / "Movies" / "The Big Movie (2020)"
    art = coverart.extract_art(tmp_path, out)
    assert art.poster and art.poster.name == "poster.jpg"
    assert art.fanart and art.fanart.name == "fanart.jpg"


def test_landscape_only_disc_makes_fanart_not_poster(tmp_path):
    dl = tmp_path / "BDMV" / "META" / "DL"
    dl.mkdir(parents=True)
    (dl / "wide.jpg").write_bytes(_jpeg(1280, 720))     # the common Blu-ray case
    art = coverart.extract_art(tmp_path, tmp_path / "out")
    assert art.poster is None
    assert art.fanart and art.fanart.name == "fanart.jpg"


def test_extract_art_none_when_no_art(tmp_path):
    art = coverart.extract_art(tmp_path, tmp_path / "out")
    assert art.poster is None and art.fanart is None


def test_disc_title_from_bdmt(tmp_path):
    _make_bd(tmp_path)
    assert coverart.disc_title(tmp_path) == "The Big Movie"


def test_disc_root_from_file_source(tmp_path):
    disc = DiscInfo(source=f"file:{tmp_path}", drive_device=None)
    assert coverart.disc_root(disc) == tmp_path


def test_disc_root_from_windows_drive():
    disc = DiscInfo(source="disc:0", drive_device="F:")
    root = coverart.disc_root(disc)
    assert root is not None and str(root).startswith("F:")


def test_disc_root_none_for_iso():
    assert coverart.disc_root(DiscInfo(source="iso:/x.iso")) is None


def test_image_size_bytes():
    p = _jpeg(1280, 720)
    assert coverart.image_size_bytes(p) == (1280, 720)


def test_extract_art_from_iso_degrades_without_pycdlib(tmp_path):
    # pycdlib not installed (or bad path) -> empty DiscArt, never raises.
    art = coverart.extract_art_from_iso(tmp_path / "nope.iso", tmp_path / "out")
    assert art.poster is None and art.fanart is None
