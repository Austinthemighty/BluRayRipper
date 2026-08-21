"""Cover-art extraction, exhaustively: disc layouts, image-header parsing, the
optional in-ISO path, and what actually lands next to the movie.

The art on a disc is the only poster the pipeline can get without TMDB, and it is
read once, before ripping, while the disc is still in the drive — a crash or a
silently-empty result here is not retried later. So these tests pin the failure
modes (unreadable mount, malformed JPEG, missing ``pycdlib``) as hard as the
happy paths.
"""

import pathlib
import sys
import types
from contextlib import contextmanager
from pathlib import Path

from blurayripper.models import DiscInfo
from blurayripper.services import coverart


# --------------------------------------------------------------------------
# image builders — real headers, byte for byte
# --------------------------------------------------------------------------

def _seg(marker: int, payload: bytes) -> bytes:
    """A JPEG marker segment: FF <marker> <2-byte length incl. itself> <payload>."""
    return bytes((0xFF, marker)) + (len(payload) + 2).to_bytes(2, "big") + payload


def _sof_payload(w: int, h: int) -> bytes:
    """SOFn body: precision, height, width, then a 3-component descriptor."""
    return (b"\x08" + h.to_bytes(2, "big") + w.to_bytes(2, "big")
            + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01")


def _jpeg(w: int, h: int, *, before: bytes = b"") -> bytes:
    return b"\xff\xd8" + before + _seg(0xC0, _sof_payload(w, h)) + b"\xff\xd9"


def _png(w: int, h: int) -> bytes:
    return (b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
            + w.to_bytes(4, "big") + h.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00")


def _gif(w: int, h: int, *, version: bytes = b"GIF89a") -> bytes:
    return version + w.to_bytes(2, "little") + h.to_bytes(2, "little") + b"\xf7\x00\x00"


def _bdmv_dl(root: Path) -> Path:
    d = root / "BDMV" / "META" / "DL"
    d.mkdir(parents=True)
    return d


@contextmanager
def _unreadable(d: Path):
    """Make a directory list-proof, the way a badly-mounted disc or an SMB share
    with no traverse rights shows up."""
    d.chmod(0o000)
    try:
        yield d
    finally:
        d.chmod(0o755)


# --------------------------------------------------------------------------
# locating art on a disc tree
# --------------------------------------------------------------------------

def test_bluray_meta_and_dvd_jacket_art_are_ranked_together_by_size(tmp_path):
    """Hybrid/flipper discs carry both layouts. The caller takes [0] as the best
    image, so the two sources have to be merged into ONE ranking, not
    concatenated BDMV-first."""
    dl = _bdmv_dl(tmp_path)
    (dl / "thumb.jpg").write_bytes(b"a" * 900)
    jp = tmp_path / "JACKET_P"
    jp.mkdir()
    (jp / "J00___5L.jpg").write_bytes(b"b" * 4000)

    assert [p.name for p in coverart.find_cover_images(tmp_path)] == [
        "J00___5L.jpg", "thumb.jpg"]


def test_non_image_files_in_the_art_folder_are_ignored(tmp_path):
    """BDMV/META/DL also holds bdmt_*.xml and .cls thumbnails; copying one of
    those to poster.jpg would give Plex an unreadable poster."""
    dl = _bdmv_dl(tmp_path)
    (dl / "bdmt_eng.xml").write_bytes(b"<x/>" * 5000)   # far bigger than the art
    (dl / "cover.png").write_bytes(_png(600, 900))

    assert [p.name for p in coverart.find_cover_images(tmp_path)] == ["cover.png"]


def test_lowercase_bdmv_folder_is_still_found(tmp_path, monkeypatch):
    """Ripped disc folders come off UDF as lowercase 'bdmv/meta/dl' about as often
    as uppercase. Windows/macOS hide this because their filesystems are
    case-insensitive; on the Linux worker the exact-case lookup misses and the
    fallback scan is the only thing that finds the art."""
    dl = tmp_path / "bdmv" / "meta" / "dl"
    dl.mkdir(parents=True)
    (dl / "cover.jpg").write_bytes(_jpeg(600, 900))

    real_exists, real_iterdir = pathlib.Path.exists, pathlib.Path.iterdir

    def case_sensitive_exists(self, *a, **kw):
        """Emulate ext4/UDF on this case-insensitive test filesystem."""
        if not real_exists(self, *a, **kw):
            return False
        try:
            return self.name in {c.name for c in real_iterdir(self.parent)}
        except OSError:
            return True

    monkeypatch.setattr(pathlib.Path, "exists", case_sensitive_exists)
    assert not (tmp_path / "BDMV").exists()      # emulation really is in force

    assert [p.name for p in coverart.find_cover_images(tmp_path)] == ["cover.jpg"]


def test_an_unlistable_art_folder_yields_no_art_instead_of_raising(tmp_path):
    """A disc that mounts but whose art folder won't list (permissions, a dying
    drive) must cost the artwork only — the rip that follows is the valuable
    part."""
    dl = _bdmv_dl(tmp_path)
    (dl / "cover.jpg").write_bytes(_jpeg(600, 900))
    with _unreadable(dl):
        assert coverart.find_cover_images(tmp_path) == []
    assert [p.name for p in coverart.find_cover_images(tmp_path)] == ["cover.jpg"]


def test_a_source_that_is_a_file_is_not_walked_as_a_disc_folder(tmp_path):
    """'file:' sources and the `coverart` CLI hand this a path the user typed; it
    is regularly a .iso or an .mkv rather than a disc folder. Probing children of
    a plain file raises NotADirectoryError, which would take out the whole pre-rip
    art step for a case that just means 'no art here'."""
    src = tmp_path / "Arrival.iso"
    src.write_bytes(b"not a disc folder")

    assert coverart.find_cover_images(src) == []
    assert coverart.disc_title(src) is None


def test_size_of_a_vanished_file_is_zero_not_an_error(tmp_path):
    """find_cover_images sorts by st_size; the disc can be ejected mid-scan, and
    one stat failure must not take the whole ranking down."""
    assert coverart._size(tmp_path / "ejected.jpg") == 0


def test_scanning_a_path_that_is_not_a_directory_is_empty(tmp_path):
    """_dir_path guards this for real trees, but ISO/UDF mounts hand back paths
    that stop being directories between the check and the read."""
    f = tmp_path / "BDMV"
    f.write_bytes(b"not a directory")
    assert coverart._images_in(f) == []


# --------------------------------------------------------------------------
# disc title from bdmt_<lang>.xml
# --------------------------------------------------------------------------

def test_disc_title_is_none_without_a_meta_folder(tmp_path):
    """DVDs and folder rips have no BDMV/META; naming must fall back to TMDB
    rather than crash on the missing directory."""
    assert coverart.disc_title(tmp_path) is None


def test_disc_title_is_none_when_the_meta_folder_cannot_be_listed(tmp_path):
    """Same unreadable-mount case as the art scan: a title we can't read is a
    'None', never an exception out of the pre-rip step."""
    dl = _bdmv_dl(tmp_path)
    (dl / "bdmt_eng.xml").write_text("<x/>", encoding="utf-8")
    with _unreadable(dl):
        assert coverart.disc_title(tmp_path) is None


def test_a_corrupt_bdmt_does_not_hide_the_next_language(tmp_path):
    """Discs ship several bdmt_<lang>.xml files and studios truncate them. Bailing
    on the first parse error would throw away a perfectly good English title."""
    dl = _bdmv_dl(tmp_path)
    (dl / "bdmt_deu.xml").write_text("<di:discinfo><unclosed", encoding="utf-8")
    (dl / "bdmt_eng.xml").write_text(
        '<disclib xmlns:di="urn:BDA:bdmv;discinfo"><di:discinfo><di:title>'
        '<di:name>Blade Runner 2049</di:name></di:title></di:discinfo></disclib>',
        encoding="utf-8")

    assert coverart.disc_title(tmp_path) == "Blade Runner 2049"


def test_a_blank_name_element_is_skipped_for_a_real_one(tmp_path):
    """<di:name/> placeholders are common; returning "" would name the movie
    folder after nothing."""
    dl = _bdmv_dl(tmp_path)
    (dl / "bdmt_eng.xml").write_text(
        '<disclib xmlns:di="urn:BDA:bdmv;discinfo"><di:discinfo>'
        '<di:name>   </di:name><di:name>Arrival</di:name>'
        '</di:discinfo></disclib>', encoding="utf-8")

    assert coverart.disc_title(tmp_path) == "Arrival"


def test_disc_title_is_none_when_the_xml_names_nothing(tmp_path):
    """Well-formed metadata with no <name> at all still has to produce None so the
    caller asks TMDB."""
    dl = _bdmv_dl(tmp_path)
    (dl / "bdmt_eng.xml").write_text(
        '<disclib><discinfo><description>no title here</description>'
        '</discinfo></disclib>', encoding="utf-8")

    assert coverart.disc_title(tmp_path) is None


# --------------------------------------------------------------------------
# image_size: JPEG/PNG/GIF headers without Pillow
# --------------------------------------------------------------------------

def test_png_dimensions_come_from_the_ihdr_chunk(tmp_path):
    """Some discs carry .png art; without PNG support every one of them would be
    filed as landscape and never become a poster."""
    p = tmp_path / "cover.png"
    p.write_bytes(_png(1000, 1500))
    assert coverart.image_size(p) == (1000, 1500)


def test_gif_dimensions_are_read_little_endian(tmp_path):
    """GIF stores width/height LSB-first, unlike PNG/JPEG. Reading it big-endian
    turns a 720-wide image into a 53760-wide one and flips its orientation."""
    p = tmp_path / "a.gif"
    p.write_bytes(_gif(720, 480))
    assert coverart.image_size(p) == (720, 480)
    p.write_bytes(_gif(720, 480, version=b"GIF87a"))
    assert coverart.image_size(p) == (720, 480)


def test_jpeg_segments_before_the_sof_are_skipped_by_their_length(tmp_path):
    """Real disc JPEGs start with APP0/COM blocks whose payload can contain the
    bytes FF C0. The parser must jump over a segment using its declared length —
    scanning through it picks up the first look-alike and reports garbage
    dimensions, which silently mis-sorts poster vs fanart."""
    decoy = b"\xff\xc0\x00\x11" + _sof_payload(4242, 2424)
    p = tmp_path / "a.jpg"
    p.write_bytes(_jpeg(1920, 1080, before=_seg(0xFE, decoy)))
    assert coverart.image_size(p) == (1920, 1080)


def test_padding_between_segments_is_walked_past(tmp_path):
    """Authoring tools pad between segments, and a segment's declared length can
    leave the reader a byte or two short of the next marker. Requiring a marker at
    exactly the next byte would abandon the file and report no size at all."""
    p = tmp_path / "a.jpg"
    p.write_bytes(_jpeg(1024, 768, before=_seg(0xE0, b"JFIF\x00") + b"\x00\x00\x11"))
    assert coverart.image_size(p) == (1024, 768)


def test_a_huffman_table_is_not_mistaken_for_a_frame_header(tmp_path):
    """DHT (FF C4), JPG (FF C8) and DAC (FF CC) sit inside the SOFn marker range
    but are not frame headers. Reading DHT as one yields 0x0000 x 0x0000 and every
    disc image collapses to the same bogus size."""
    dht = _seg(0xC4, b"\x00" + bytes(16) + b"\x01\x02\x03\x04")
    p = tmp_path / "a.jpg"
    p.write_bytes(_jpeg(600, 900, before=dht))
    assert coverart.image_size(p) == (600, 900)


def test_fill_bytes_before_a_marker_are_tolerated(tmp_path):
    """FF padding before a marker is legal JPEG. Taking the first FF as the marker
    reads the length from the wrong offset and the parse walks off the end."""
    p = tmp_path / "a.jpg"
    p.write_bytes(_jpeg(800, 1200, before=b"\xff\xff"))
    assert coverart.image_size(p) == (800, 1200)


def test_standalone_markers_carry_no_length_field(tmp_path):
    """SOI/EOI/RSTn have no length. Reading two bytes after one of them consumes
    the start of the next marker and the SOF is never found."""
    p = tmp_path / "a.jpg"
    p.write_bytes(_jpeg(640, 960, before=b"\xff\xd8\xff\xd0\xff\xd7"))
    assert coverart.image_size(p) == (640, 960)


def test_image_size_of_a_missing_file_is_none(tmp_path):
    """classify_images stats every file it just listed; a disc pulled between the
    listing and the read must not abort art extraction."""
    assert coverart.image_size(tmp_path / "gone.jpg") is None


def test_unparseable_images_are_none_rather_than_exceptions(tmp_path):
    """Every one of these has been seen on a real disc or a half-copied folder.
    image_size() is called on files chosen only by extension, so anything that
    raises here escapes straight out of the pre-rip art step."""
    cases = {
        "empty.jpg": b"",
        "text.jpg": b"this is not an image at all",
        "soi-only.jpg": b"\xff\xd8",                       # truncated to nothing
        "dangling-ff.jpg": b"\xff\xd8\xff",                # marker never arrives
        "short-length.jpg": b"\xff\xd8\xff\xc0\x00",       # length field cut off
        "short-sof.jpg": b"\xff\xd8\xff\xc0\x00\x11\x08",  # frame header cut off
        "short-png.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 4,
        "short-gif.gif": b"GIF89a",
    }
    for name, data in cases.items():
        p = tmp_path / name
        p.write_bytes(data)
        assert coverart.image_size(p) is None, name
        assert coverart.image_size_bytes(data) is None, name


# --------------------------------------------------------------------------
# portrait / landscape classification
# --------------------------------------------------------------------------

def test_images_with_an_unreadable_header_are_treated_as_landscape(tmp_path):
    """A file we can't measure must not be guessed into poster.jpg: a wrong poster
    is visible on every Plex tile, a wrong backdrop is barely noticed."""
    dl = _bdmv_dl(tmp_path)
    (dl / "mystery.jpg").write_bytes(b"\x00" * 4000)   # no readable header

    portraits, landscapes = coverart.classify_images(tmp_path)
    assert [p.name for p in portraits] == []
    assert [p.name for p in landscapes] == ["mystery.jpg"]


def test_a_zero_width_image_is_not_a_portrait(tmp_path):
    """A header claiming 0x900 satisfies 'height >= width' but is degenerate;
    accepting it makes a corrupt file outrank the disc's real art for poster."""
    dl = _bdmv_dl(tmp_path)
    (dl / "broken.jpg").write_bytes(_jpeg(0, 900) + b"\x00" * 3000)
    (dl / "wide.jpg").write_bytes(_jpeg(1920, 1080))

    portraits, landscapes = coverart.classify_images(tmp_path)
    assert [p.name for p in portraits] == []
    assert [p.name for p in landscapes] == ["broken.jpg", "wide.jpg"]


def test_square_art_counts_as_a_poster(tmp_path):
    """CD-style square jackets are portrait-ish; the boundary is h >= w, so a
    square must land in poster, not fanart."""
    dl = _bdmv_dl(tmp_path)
    (dl / "square.jpg").write_bytes(_jpeg(1000, 1000))

    portraits, landscapes = coverart.classify_images(tmp_path)
    assert [p.name for p in portraits] == ["square.jpg"]
    assert landscapes == []


# --------------------------------------------------------------------------
# writing the art next to the movie
# --------------------------------------------------------------------------

def test_extract_art_keeps_the_real_extension_and_lowercases_it(tmp_path):
    """Disc files are UPPERCASE (COVER.JPG) and can be .png. Writing PNG bytes to
    poster.jpg gives Plex a file whose contents don't match its extension."""
    dl = _bdmv_dl(tmp_path)
    (dl / "COVER.PNG").write_bytes(_png(600, 900))
    (dl / "BACK.JPG").write_bytes(_jpeg(1920, 1080))

    art = coverart.extract_art(tmp_path, tmp_path / "out")
    assert art.poster.name == "poster.png"
    assert art.fanart.name == "fanart.jpg"


def test_extract_art_creates_the_movie_folder_and_copies_the_bytes(tmp_path):
    """Art is placed before the rip, so the movie folder usually does not exist
    yet; and a zero-byte poster is worse than none."""
    dl = _bdmv_dl(tmp_path)
    (dl / "tall.jpg").write_bytes(_jpeg(600, 900) + b"payload")
    dest = tmp_path / "Movies" / "Arrival (2016)"
    assert not dest.exists()

    art = coverart.extract_art(tmp_path, dest)
    assert art.poster.read_bytes() == (dl / "tall.jpg").read_bytes()


def test_extract_art_skips_the_half_the_caller_already_has(tmp_path):
    """The pipeline re-runs this per output folder and passes poster=False once a
    poster is already there; writing anyway would overwrite the good TMDB poster
    with the disc's own lower-quality art."""
    dl = _bdmv_dl(tmp_path)
    (dl / "tall.jpg").write_bytes(_jpeg(600, 900))
    (dl / "wide.jpg").write_bytes(_jpeg(1920, 1080))
    dest = tmp_path / "out"

    art = coverart.extract_art(tmp_path, dest, poster=False)
    assert art.poster is None
    assert art.fanart is not None
    assert [p.name for p in dest.iterdir()] == ["fanart.jpg"]


def test_the_largest_portrait_wins_the_poster_slot(tmp_path):
    """Discs carry the same key art at several resolutions; the poster should be
    the biggest one, not whichever the filesystem lists first."""
    dl = _bdmv_dl(tmp_path)
    (dl / "small.jpg").write_bytes(_jpeg(300, 450) + b"\x00" * 100)
    (dl / "large.jpg").write_bytes(_jpeg(1200, 1800) + b"\x00" * 9000)

    art = coverart.extract_art(tmp_path, tmp_path / "out")
    assert art.poster.read_bytes() == (dl / "large.jpg").read_bytes()


# --------------------------------------------------------------------------
# disc_root: which sources can be read at all
# --------------------------------------------------------------------------

def test_disc_root_is_none_for_a_linux_optical_device():
    """/dev/sr0 is not a path we can walk; returning it would make the pre-rip art
    step try to iterdir a character device on every Linux worker."""
    assert coverart.disc_root(DiscInfo(source="disc:0", drive_device="/dev/sr0")) is None


def test_disc_root_ignores_a_non_drive_letter_device():
    """Only a bare 'F:' maps to a mount root; anything else (a UNC path, a stray
    label) must not be turned into a filesystem root."""
    assert coverart.disc_root(DiscInfo(source="disc:0", drive_device=r"\\nas\bd")) is None


# --------------------------------------------------------------------------
# art from inside an .iso, via the optional pycdlib
# --------------------------------------------------------------------------

class _FakeChild:
    def __init__(self, ident: str, *, is_dir: bool = False, broken: bool = False):
        self._ident, self._is_dir, self._broken = ident, is_dir, broken

    def is_dir(self) -> bool:
        return self._is_dir

    def file_identifier(self) -> bytes:
        if self._broken:
            raise ValueError("corrupt directory record")
        return self._ident.encode("utf-8")


class _FakeIso:
    """Stands in for pycdlib.PyCdlib. ``tree`` maps (kind, dir) -> children and
    ``files`` maps (kind, full path) -> bytes, the way pycdlib addresses the UDF
    and ISO9660 sides of a hybrid disc image."""

    def __init__(self, tree=None, files=None, *, open_error=None, close_error=None,
                 read_errors=()):
        self.tree, self.files = tree or {}, files or {}
        self.open_error, self.close_error = open_error, close_error
        self.read_errors = set(read_errors)
        self.opened: str | None = None
        self.closed = False
        self.reads: list[tuple[str, str]] = []

    @staticmethod
    def _kind(kwargs) -> tuple[str, str]:
        (key, value), = kwargs.items()
        assert key in ("udf_path", "iso_path"), key
        return key[:-5], value

    def open(self, path):
        if self.open_error:
            raise self.open_error
        self.opened = path

    def list_children(self, **kwargs):
        key = self._kind(kwargs)
        if key not in self.tree:
            raise ValueError(f"no such directory {key}")
        return iter(self.tree[key])

    def get_file_from_iso_fp(self, fp, **kwargs):
        key = self._kind(kwargs)
        self.reads.append(key)
        if key in self.read_errors:
            raise ValueError(f"unreadable {key}")
        if key not in self.files:
            raise ValueError(f"no such file {key}")
        fp.write(self.files[key])

    def close(self):
        self.closed = True
        if self.close_error:
            raise self.close_error


def _install_pycdlib(monkeypatch, iso: _FakeIso) -> _FakeIso:
    monkeypatch.setitem(sys.modules, "pycdlib",
                        types.SimpleNamespace(PyCdlib=lambda: iso))
    return iso


def test_iso_art_is_read_from_the_udf_side_and_written_out(tmp_path, monkeypatch):
    """A .iso never reaches disc_root(), so this is the only way an ISO source
    gets a poster. Blu-ray art lives on the UDF side of the image."""
    dl = ("udf", "/BDMV/META/DL")
    iso = _install_pycdlib(monkeypatch, _FakeIso(
        tree={dl: [_FakeChild("TALL.JPG;1"), _FakeChild("WIDE.JPG;1")]},
        files={("udf", "/BDMV/META/DL/TALL.JPG;1"): _jpeg(600, 900) + b"p",
               ("udf", "/BDMV/META/DL/WIDE.JPG;1"): _jpeg(1920, 1080) + b"f"}))

    art = coverart.extract_art_from_iso(tmp_path / "movie.iso", tmp_path / "out")

    assert iso.opened == str(tmp_path / "movie.iso")
    assert art.poster.read_bytes() == _jpeg(600, 900) + b"p"
    assert art.fanart.read_bytes() == _jpeg(1920, 1080) + b"f"


def test_iso_version_suffixes_do_not_leak_into_the_written_name(tmp_path, monkeypatch):
    """ISO9660 identifiers carry a ';1' revision. Left on, the extension test
    fails and Plex would be handed a file called poster.jpg;1."""
    _install_pycdlib(monkeypatch, _FakeIso(
        tree={("udf", "/BDMV/META/DL"): [_FakeChild("COVER.JPG;1")]},
        files={("udf", "/BDMV/META/DL/COVER.JPG;1"): _jpeg(600, 900)}))

    art = coverart.extract_art_from_iso(tmp_path / "m.iso", tmp_path / "out")
    assert art.poster.name == "poster.jpg"


def test_iso_jacket_art_is_found_on_the_iso9660_side(tmp_path, monkeypatch):
    """DVD jacket pictures may exist only in the ISO9660 tree of an image with no
    UDF side at all; searching UDF alone loses them."""
    _install_pycdlib(monkeypatch, _FakeIso(
        tree={("iso", "/JACKET_P"): [_FakeChild("J00___5L.JPG;1")]},
        files={("iso", "/JACKET_P/J00___5L.JPG;1"): _jpeg(1920, 1080)}))

    art = coverart.extract_art_from_iso(tmp_path / "dvd.iso", tmp_path / "out")
    assert art.fanart is not None and art.fanart.name == "fanart.jpg"


def test_iso_directories_are_never_read_as_files(tmp_path, monkeypatch):
    """'.' , '..' and subdirectories come back from list_children like any other
    entry. Feeding a directory record to get_file_from_iso_fp is how this used to
    blow up on discs with a DL/ subfolder."""
    dl = ("udf", "/BDMV/META/DL")
    iso = _install_pycdlib(monkeypatch, _FakeIso(
        tree={dl: [_FakeChild("SUB.JPG;1", is_dir=True), _FakeChild("REAL.JPG;1")]},
        files={("udf", "/BDMV/META/DL/REAL.JPG;1"): _jpeg(1920, 1080)}))

    art = coverart.extract_art_from_iso(tmp_path / "m.iso", tmp_path / "out")

    assert art.fanart is not None
    assert iso.reads == [("udf", "/BDMV/META/DL/REAL.JPG;1")]


def test_iso_keeps_going_past_unreadable_entries(tmp_path, monkeypatch):
    """One corrupt directory record, one missing directory and one unreadable
    extent must each cost only their own file — the surviving art still lands."""
    dl = ("udf", "/BDMV/META/DL")
    iso = _install_pycdlib(monkeypatch, _FakeIso(
        tree={dl: [_FakeChild("BAD.JPG;1", broken=True),
                   _FakeChild("UNREADABLE.JPG;1"),
                   _FakeChild("GOOD.JPG;1")]},
        files={("udf", "/BDMV/META/DL/GOOD.JPG;1"): _jpeg(1920, 1080) + b"good"},
        read_errors={("udf", "/BDMV/META/DL/UNREADABLE.JPG;1")}))

    art = coverart.extract_art_from_iso(tmp_path / "m.iso", tmp_path / "out")

    assert art.fanart.read_bytes() == _jpeg(1920, 1080) + b"good"
    assert iso.closed is True


def test_iso_non_image_members_are_left_alone(tmp_path, monkeypatch):
    """The same directories hold bdmt XML and menu data. Only jpg/jpeg/png may be
    written as artwork — note the .txt below is the biggest member, so a missing
    extension filter would make it the fanart."""
    dl = ("udf", "/BDMV/META/DL")
    _install_pycdlib(monkeypatch, _FakeIso(
        tree={dl: [_FakeChild("BDMT_ENG.XML;1"), _FakeChild("README;1"),
                   _FakeChild("ART.JPG;1")]},
        files={("udf", "/BDMV/META/DL/BDMT_ENG.XML;1"): b"<x/>" * 5000,
               ("udf", "/BDMV/META/DL/README;1"): b"r" * 9000,
               ("udf", "/BDMV/META/DL/ART.JPG;1"): _jpeg(1920, 1080)}))

    art = coverart.extract_art_from_iso(tmp_path / "m.iso", tmp_path / "out")
    assert art.fanart.read_bytes() == _jpeg(1920, 1080)
    assert art.poster is None


def test_iso_zero_byte_members_are_not_written(tmp_path, monkeypatch):
    """An empty extent reads back as b'' without raising; writing it leaves a
    0-byte fanart.jpg that the idempotence check then treats as 'already placed',
    wedging that folder's artwork for good."""
    dl = ("udf", "/BDMV/META/DL")
    _install_pycdlib(monkeypatch, _FakeIso(
        tree={dl: [_FakeChild("EMPTY.JPG;1"), _FakeChild("TALL.JPG;1")]},
        files={("udf", "/BDMV/META/DL/EMPTY.JPG;1"): b"",
               ("udf", "/BDMV/META/DL/TALL.JPG;1"): _jpeg(600, 900)}))

    art = coverart.extract_art_from_iso(tmp_path / "m.iso", tmp_path / "out")
    assert art.poster is not None
    assert art.fanart is None
    assert [p.name for p in (tmp_path / "out").iterdir()] == ["poster.jpg"]


def test_iso_that_cannot_be_opened_produces_no_art(tmp_path, monkeypatch):
    """Truncated downloads and split .iso parts are common; the pre-rip art step
    must degrade to 'no art', never take the job down."""
    iso = _install_pycdlib(monkeypatch, _FakeIso(open_error=OSError("bad ISO")))

    art = coverart.extract_art_from_iso(tmp_path / "m.iso", tmp_path / "out")

    assert (art.poster, art.fanart) == (None, None)
    assert iso.reads == []


def test_art_survives_a_failing_iso_close(tmp_path, monkeypatch):
    """pycdlib can raise on close after a successful read; losing the art we
    already extracted to a teardown error would be gratuitous."""
    _install_pycdlib(monkeypatch, _FakeIso(
        tree={("udf", "/BDMV/META/DL"): [_FakeChild("A.JPG;1")]},
        files={("udf", "/BDMV/META/DL/A.JPG;1"): _jpeg(600, 900)},
        close_error=OSError("device busy")))

    art = coverart.extract_art_from_iso(tmp_path / "m.iso", tmp_path / "out")
    assert art.poster is not None


def test_iso_largest_image_of_each_orientation_wins(tmp_path, monkeypatch):
    """Discs carry several sizes of the same key art; inside an ISO there is no
    st_size to sort by, so the byte length has to do the ranking."""
    dl = ("udf", "/BDMV/META/DL")
    _install_pycdlib(monkeypatch, _FakeIso(
        tree={dl: [_FakeChild("SMALL.JPG;1"), _FakeChild("BIG.JPG;1")]},
        files={("udf", "/BDMV/META/DL/SMALL.JPG;1"): _jpeg(300, 450) + b"\x00" * 10,
               ("udf", "/BDMV/META/DL/BIG.JPG;1"): _jpeg(1200, 1800) + b"\x00" * 5000}))

    art = coverart.extract_art_from_iso(tmp_path / "m.iso", tmp_path / "out")
    assert art.poster.read_bytes().startswith(_jpeg(1200, 1800))


def test_iso_extraction_honours_the_poster_and_fanart_flags(tmp_path, monkeypatch):
    """Resume path: the pipeline asks only for the half that is missing, so an
    already-placed poster is not overwritten by the disc's own art."""
    dl = ("udf", "/BDMV/META/DL")
    _install_pycdlib(monkeypatch, _FakeIso(
        tree={dl: [_FakeChild("TALL.JPG;1"), _FakeChild("WIDE.JPG;1")]},
        files={("udf", "/BDMV/META/DL/TALL.JPG;1"): _jpeg(600, 900),
               ("udf", "/BDMV/META/DL/WIDE.JPG;1"): _jpeg(1920, 1080)}))
    dest = tmp_path / "out"

    art = coverart.extract_art_from_iso(tmp_path / "m.iso", dest, fanart=False)
    assert art.fanart is None
    assert [p.name for p in dest.iterdir()] == ["poster.jpg"]


def test_iso_art_is_skipped_when_pycdlib_is_not_installed(tmp_path, monkeypatch):
    """pycdlib is an optional extra. On a box without it, ISO sources must simply
    get no art instead of an ImportError in the pre-rip step."""
    monkeypatch.setitem(sys.modules, "pycdlib", None)   # forces ImportError

    art = coverart.extract_art_from_iso(tmp_path / "m.iso", tmp_path / "out")

    assert (art.poster, art.fanart) == (None, None)
    assert not (tmp_path / "out").exists()
