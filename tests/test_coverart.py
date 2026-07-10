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


def test_extract_poster_copies_largest(tmp_path):
    _make_bd(tmp_path, big=9000, small=100)
    out = tmp_path / "Movies" / "The Big Movie (2020)"
    dest = coverart.extract_poster(tmp_path, out)
    assert dest is not None and dest.name == "poster.jpg"
    assert dest.read_bytes() == b"x" * 9000  # the largest image


def test_extract_poster_none_when_no_art(tmp_path):
    assert coverart.extract_poster(tmp_path, tmp_path / "out") is None


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
