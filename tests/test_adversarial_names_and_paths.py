"""Hostile text reaching the filesystem.

Every path this project writes is built from text it did not author: a disc's
volume label (whatever the authoring house stamped on it), a TMDB title (a public
API anyone can edit), an episode name, an edition tag typed into the web UI.
All of it flows into ``services/naming.py`` and then straight into ``mkdir`` /
``shutil.move`` in ``services/library.py``.

The three invariants worth defending, in order of how badly they hurt:

1. **Containment** — the output must land under the configured library root. A
   title of ``../../etc`` or ``/etc/passwd`` must not become a real traversal.
2. **No silent emptiness** — a title must never sanitize down to ``""``, ``"."``
   or ``".."``, because each of those names the *directory itself* rather than a
   child, and ``place()`` would then unlink/overwrite something it was never
   asked to touch.
3. **No silent collision, and no silent loss** — when two different titles do
   land on one path, or when a name is too long for the filesystem, the operator
   has to be able to tell. A quiet overwrite of a finished encode is the failure
   mode this project has shipped before.

Tests here assert the *placed result on disk* wherever a pure-function assertion
could be satisfied for the wrong reason.
"""

from __future__ import annotations

import os
from pathlib import Path
from xml.etree import ElementTree

import pytest

from blurayripper.models import EpisodeAssignment, MediaKind
from blurayripper.pipeline import _slug
from blurayripper.services import library, naming, nfo, os_upload

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _normpath_inside(root: Path, p: Path) -> bool:
    """Whether ``p`` stays under ``root`` after lexically resolving ``..``.

    ``Path.relative_to`` is a plain prefix test and happily accepts
    ``root/../../etc/x``, so it cannot answer this. ``normpath`` collapses the
    ``..`` segments the way the kernel will.
    """
    r = os.path.normpath(str(root))
    n = os.path.normpath(str(p))
    return n.startswith(r + os.sep) and n != r


def _lib(tmp_path: Path) -> tuple[Path, Path]:
    """A created Movies root plus a staged source file to place into it."""
    movies = tmp_path / "library" / "Movies"
    movies.mkdir(parents=True)
    src = tmp_path / "staging" / "encoded.mkv"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"VIDEO-A")
    return movies, src


def _fs_folds_names(base: Path, a: str, b: str) -> bool:
    """Does this filesystem treat the distinct names ``a`` and ``b`` as one file?

    Needed because the answer differs per volume (APFS/NTFS fold case, ext4 does
    not; HFS+ folds Unicode normalization, APFS on a case-sensitive volume does
    not) and the tests below must assert the right consequence either way rather
    than encode one platform's answer.
    """
    probe = base / "fs_fold_probe"
    probe.mkdir()
    (probe / a).write_text("a")
    try:
        return (probe / b).exists()
    finally:
        for child in probe.iterdir():
            child.unlink()
        probe.rmdir()


# Titles that, read as a path, would point outside the library.
TRAVERSALS = [
    "../../etc/passwd",
    "..\\..\\Windows\\System32",
    "/etc/passwd",
    "C:\\Windows\\System32\\drivers",
    "....//....//etc",
]

# Titles that carry no usable characters at all.
DEGENERATE = ["", " ", "   ", ".", "..", "...", ".....", "\t\n", "\u00a0\u00a0", "?", "***", "<>|"]


# --------------------------------------------------------------------------- #
# 1. containment
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("title", TRAVERSALS)
def test_traversal_in_a_title_collapses_to_a_single_folder_component(tmp_path, title):
    """A disc labelled ``../../etc/passwd`` must produce exactly
    ``<Movies>/<one folder>/<one file>`` — the separators have to be *deleted*,
    not merely escaped, or the encode lands outside the library entirely."""
    movies, src = _lib(tmp_path)
    dest = naming.movie_video_path(movies, title, 2020, "mkv")

    assert _normpath_inside(movies, dest), dest
    assert dest.relative_to(movies).parts == (dest.parent.name, dest.name)
    assert len(dest.relative_to(movies).parts) == 2
    for part in dest.relative_to(movies).parts:
        assert "/" not in part and "\\" not in part
        assert part not in (".", "..")

    placed = library.place(src, dest)
    assert placed.read_bytes() == b"VIDEO-A"
    # And it is genuinely under the root once the kernel has resolved it.
    assert placed.resolve().is_relative_to(movies.resolve())
    # Nothing appeared next to the library root.
    assert sorted(p.name for p in movies.parent.iterdir()) == ["Movies"]


@pytest.mark.parametrize("title", DEGENERATE)
def test_a_worthless_title_never_names_the_library_directory_itself(tmp_path, title):
    """With no year there is nothing to pad the folder name with, so a title of
    ``".."`` would make the movie folder the library root's *parent* and the
    video would be written outside. ``sanitize`` has to substitute a real name.

    ``place()`` unlinks an existing destination, so a title resolving to ``"."``
    or ``".."`` is not just misfiled — it is aimed at a directory.
    """
    movies, src = _lib(tmp_path)
    dest = naming.movie_video_path(movies, title, None, "mkv")

    assert dest.parent.name not in ("", ".", "..")
    assert dest.stem not in ("", ".", "..")
    assert _normpath_inside(movies, dest), dest
    assert dest.parent.parent == movies

    placed = library.place(src, dest)
    assert placed.exists() and placed.read_bytes() == b"VIDEO-A"
    assert placed.resolve().is_relative_to(movies.resolve())


def test_a_traversal_in_the_edition_tag_cannot_split_the_filename(tmp_path):
    """The Plex edition tag is free text typed into the web UI and it lands in the
    *filename*, not the folder — so it needs its own sanitizing pass. Unsanitized,
    ``{edition-../../etc}`` turns one filename into three path components."""
    movies, src = _lib(tmp_path)
    dest = naming.movie_video_path(movies, "WarGames", 1983, "mkv", "../../etc/Cut")

    assert len(dest.relative_to(movies).parts) == 2
    assert "/" not in dest.name and "\\" not in dest.name
    assert dest.name.startswith("WarGames (1983) {edition-")
    assert _normpath_inside(movies, dest), dest
    assert library.place(src, dest).resolve().is_relative_to(movies.resolve())


def test_episode_paths_sanitize_the_show_name_in_the_folder_and_the_basename(tmp_path):
    """``episode_video_path`` builds the show name into two places — the show
    folder and the ``Show - S01E02`` basename. Both need sanitizing; a raw
    basename would splice extra directories in below ``Season 01``."""
    tv = tmp_path / "library" / "TV"
    tv.mkdir(parents=True)
    src = tmp_path / "e.mkv"
    src.write_bytes(b"EP")

    dest = naming.episode_video_path(tv, "../../etc/passwd", None, 1, 2, "mkv")

    rel = dest.relative_to(tv)
    assert len(rel.parts) == 3, rel          # show / Season 01 / basename.mkv
    assert rel.parts[1] == "Season 01"
    assert rel.parts[2].endswith(" - S01E02.mkv")
    assert "/" not in rel.parts[2] and "\\" not in rel.parts[2]
    assert _normpath_inside(tv, dest), dest

    placed = library.place(src, dest)
    assert placed.resolve().is_relative_to(tv.resolve())


# --------------------------------------------------------------------------- #
# 2. characters the filesystem or the OS would choke on
# --------------------------------------------------------------------------- #


def test_nul_and_control_characters_never_reach_the_filesystem(tmp_path):
    """A NUL byte in a path makes CPython raise ``ValueError`` before the syscall,
    and ESC/BEL in a filename corrupts any terminal that lists the folder. A
    successful ``place()`` is the proof the bytes were dropped — the write cannot
    succeed while they are still there."""
    movies, src = _lib(tmp_path)
    title = "Movie\x00Title\x07\x1b[31m\x1fEnd"

    dest = naming.movie_video_path(movies, title, 2020, "mkv")
    assert all(ord(c) >= 32 for c in dest.parent.name), repr(dest.parent.name)
    assert all(ord(c) >= 32 for c in dest.name), repr(dest.name)

    placed = library.place(src, dest)
    assert placed.exists()
    # Round-trip through a real directory listing: the name is addressable.
    assert dest.name in [p.name for p in dest.parent.iterdir()]


def test_ntfs_illegal_characters_are_stripped_from_every_component(tmp_path):
    """``: * ? " < > |`` are illegal on NTFS, and the deployment target is a
    Windows VM. They are legal on the Linux workers, so a file written on Linux
    would be unopenable from the box that serves Plex."""
    movies, src = _lib(tmp_path)
    title = 'Face/Off: The "Movie" <2> | Why? *Yes*'

    dest = naming.movie_video_path(movies, title, 2020, "mkv")

    for part in dest.relative_to(movies).parts:
        assert not (set(part) & set('<>:"/\\|?*')), repr(part)
    # The surviving text is still recognisable, not blanked out.
    assert "FaceOff" in dest.parent.name
    assert library.place(src, dest).exists()


@pytest.mark.parametrize(
    "title",
    ["CON", "con", "PRN", "aux", "NUL", "COM1", "com9", "LPT1", "lpt9", "CON.", "  nul  "],
)
def test_windows_reserved_device_names_are_pushed_out_of_the_way(tmp_path, title):
    """``\\\\.\\CON`` etc. are device names on Windows: ``mkdir CON`` fails and
    ``open("NUL", "w")`` writes to the bit bucket, so a movie called *Nul* would
    encode into nowhere and report success. The check has to run *after* the
    trailing-dot strip, because ``CON.`` is the same device."""
    movies, src = _lib(tmp_path)
    dest = naming.movie_video_path(movies, title, None, "mkv")

    assert dest.parent.name.startswith("_")
    assert dest.parent.name.lstrip("_").upper() in naming._WINDOWS_RESERVED
    assert dest.stem.startswith("_")
    assert library.place(src, dest).exists()


@pytest.mark.parametrize("title", ["CONAN", "Console", "Nullify", "Compass", "AUXIN", "PRNT"])
def test_titles_that_merely_start_like_a_device_name_are_left_alone(tmp_path, title):
    """The counterweight: the reserved-name rule must match the whole component,
    or *Conan the Barbarian* silently becomes ``_CONAN``."""
    movies, _src = _lib(tmp_path)
    dest = naming.movie_video_path(movies, title, None, "mkv")
    assert dest.parent.name == title
    assert dest.name == f"{title}.mkv"


@pytest.mark.parametrize("title", ["The Movie.", "The Movie ", "The Movie. . .", "The Movie   ."])
def test_no_component_ends_in_a_dot_or_space_windows_would_quietly_drop(tmp_path, title):
    """Windows strips trailing dots and spaces when *creating* a file but not when
    *opening* one, so ``"Movie ."`` becomes a file nothing can reference by the
    name we recorded in the database."""
    movies, src = _lib(tmp_path)
    dest = naming.movie_video_path(movies, title, None, "mkv")

    assert dest.parent.name == "The Movie"
    assert dest.parent.name == dest.parent.name.rstrip(". ")
    assert dest.stem == dest.stem.rstrip(". ")
    assert library.place(src, dest).exists()


def test_runs_of_whitespace_are_collapsed_so_two_labels_cannot_differ_invisibly(tmp_path):
    """``"Blade  Runner"`` and ``"Blade Runner"`` are one film with two disc
    labels. Without collapsing, they make two Plex entries that look identical in
    the UI and neither operator nor Plex can tell them apart."""
    movies, _src = _lib(tmp_path)
    wide = naming.movie_video_path(movies, "  Blade   Runner  ", 1982, "mkv")
    tight = naming.movie_video_path(movies, "Blade Runner", 1982, "mkv")

    assert wide == tight
    assert wide.parent.name == "Blade Runner (1982)"


def test_a_leading_dash_title_is_still_addressable_after_placement(tmp_path):
    """``-rf`` is a legal filename and must not be mangled, but everything this
    project shells out to (makemkvcon, HandBrakeCLI, ffmpeg) would read it as a
    flag. Assert it is placed and openable, so the name we stored is the name on
    disk."""
    movies, src = _lib(tmp_path)
    dest = naming.movie_video_path(movies, "-rf --force", 2020, "mkv")

    assert _normpath_inside(movies, dest), dest
    # Kept verbatim — quietly eating the dash would rename someone's film.
    assert dest.parent.name == "-rf --force (2020)"
    assert dest.name.startswith("-rf --force")

    placed = library.place(src, dest)
    assert placed.read_bytes() == b"VIDEO-A"
    assert placed.name in [p.name for p in placed.parent.iterdir()]


def test_combining_marks_emoji_and_an_rtl_override_survive_placement(tmp_path):
    """None of these are illegal anywhere, so the right behaviour is to keep them
    — but the placed file has to be readable back through the exact path we
    returned, or the database row points at nothing."""
    movies, src = _lib(tmp_path)
    title = "Cafe\u0301 \U0001f3ac \u202egnp.txt Fin"

    dest = naming.movie_video_path(movies, title, 2020, "mkv")
    assert "\u0301" in dest.parent.name
    assert "\U0001f3ac" in dest.parent.name
    assert "\u202e" in dest.parent.name

    placed = library.place(src, dest)
    assert placed.read_bytes() == b"VIDEO-A"
    assert dest.exists()                     # readable back via the same path
    assert dest.parent.exists()


# --------------------------------------------------------------------------- #
# 3. length
# --------------------------------------------------------------------------- #


def test_a_title_over_255_bytes_fails_loudly_and_places_nothing(tmp_path):
    """``NAME_MAX`` is 255 on every filesystem this runs on and nothing here
    truncates. The behaviour that matters is that it fails *loudly*: an OSError
    the job can report, an empty library, and the encoded source still on disk so
    a retry has something to place. A silent truncation would be worse — two long
    titles sharing a prefix would then collide."""
    movies, src = _lib(tmp_path)
    dest = naming.movie_video_path(movies, "L" * 300, 2020, "mkv")

    assert len(dest.parent.name) > 255       # not truncated by naming
    with pytest.raises(OSError):
        library.place(src, dest)

    assert list(movies.iterdir()) == []      # no half-made folder
    assert src.exists() and src.read_bytes() == b"VIDEO-A"


def test_an_over_long_edition_leaves_the_encode_where_a_retry_can_find_it(tmp_path):
    """Same limit, but only the *filename* is over it, so ``mkdir`` succeeds and
    the failure happens during the move. The recoverable state is what counts:
    the video must not be half-moved, and the source must survive."""
    movies, src = _lib(tmp_path)
    dest = naming.movie_video_path(movies, "Short", 2020, "mkv", "E" * 300)

    assert len(dest.parent.name) < 255 and len(dest.name) > 255
    with pytest.raises(OSError):
        library.place(src, dest)

    assert list(movies.rglob("*.mkv")) == []          # nothing placed
    assert src.exists() and src.read_bytes() == b"VIDEO-A"   # retryable


def test_a_path_past_4096_chars_is_not_silently_truncated(tmp_path):
    """A deep NAS root plus a long TMDB title can exceed ``PATH_MAX``. Naming is a
    pure function and stays that way: the full title is preserved so two similar
    long titles keep distinct paths, and the path is still rooted in the library.
    (Truncating here to fit would trade a loud OSError for a silent overwrite.)"""
    deep_root = Path("/nas").joinpath(*[f"level{i:02d}" + "x" * 60 for i in range(70)])
    title = "T" * 200

    a = naming.movie_video_path(deep_root, title + "1", 2020, "mkv")
    b = naming.movie_video_path(deep_root, title + "2", 2020, "mkv")

    assert len(str(a)) > 4096
    assert a != b                                        # no prefix collapse
    assert a.parent.name == f"{title}1 (2020)"           # nothing dropped
    assert len(a.relative_to(deep_root).parts) == 2
    assert _normpath_inside(deep_root, a)


# --------------------------------------------------------------------------- #
# 4. collisions
# --------------------------------------------------------------------------- #


def test_two_worthless_titles_land_on_one_path_and_the_clash_is_detectable(tmp_path):
    """``"..."`` and two non-breaking spaces both sanitize to ``Untitled``, so two
    unrelated discs really do target one file. That is the shape of the bug this
    project has shipped before, so assert the guard: ``overwrite=False`` refuses
    and the first film survives, while ``overwrite=True`` replaces on purpose."""
    movies, first_src = _lib(tmp_path)
    second_src = tmp_path / "staging" / "second.mkv"
    second_src.write_bytes(b"VIDEO-B")

    a = naming.movie_video_path(movies, "...", None, "mkv")
    b = naming.movie_video_path(movies, "\u00a0\u00a0", None, "mkv")
    assert a == b                                    # the collision is real

    library.place(first_src, a)
    with pytest.raises(FileExistsError):
        library.place(second_src, b, overwrite=False)

    assert a.read_bytes() == b"VIDEO-A"              # first film untouched
    assert second_src.exists()                       # and the second not lost

    library.place(second_src, b, overwrite=True)     # deliberate replace still works
    assert a.read_bytes() == b"VIDEO-B"


def test_titles_differing_only_by_case_never_silently_lose_the_first_film(tmp_path):
    """Plex libraries live on NTFS or a CIFS share as often as on ext4. Naming
    keeps the two titles distinct; whether they collide is the *filesystem's*
    answer, so assert the consequence that holds on either kind of volume: either
    both films exist with their own bytes, or the clash is refused."""
    movies, src_a = _lib(tmp_path)
    src_b = tmp_path / "staging" / "b.mkv"
    src_b.write_bytes(b"VIDEO-B")

    upper = naming.movie_video_path(movies, "THE MATRIX", 1999, "mkv")
    mixed = naming.movie_video_path(movies, "The Matrix", 1999, "mkv")
    assert upper != mixed                            # naming does not fold case

    library.place(src_a, upper)
    folds_case = _fs_folds_names(tmp_path, "CaseProbe", "caseprobe")

    if folds_case:
        with pytest.raises(FileExistsError):
            library.place(src_b, mixed, overwrite=False)
        assert upper.read_bytes() == b"VIDEO-A"
        assert src_b.exists()
    else:
        library.place(src_b, mixed, overwrite=False)
        assert upper.read_bytes() == b"VIDEO-A"
        assert mixed.read_bytes() == b"VIDEO-B"


def test_the_same_title_in_nfc_and_nfd_does_not_silently_lose_a_film(tmp_path):
    """``Café`` typed on a Mac arrives decomposed, from TMDB it arrives composed.
    Same rule as case: distinct at the naming layer, and on a normalizing volume
    the clash is refused rather than swallowed."""
    movies, src_a = _lib(tmp_path)
    src_b = tmp_path / "staging" / "b.mkv"
    src_b.write_bytes(b"VIDEO-B")

    nfc = naming.movie_video_path(movies, "Caf\u00e9 Society", 2016, "mkv")
    nfd = naming.movie_video_path(movies, "Cafe\u0301 Society", 2016, "mkv")
    assert nfc != nfd

    library.place(src_a, nfc)
    folds_nfd = _fs_folds_names(tmp_path, "Caf\u00e9", "Cafe\u0301")

    if folds_nfd:
        with pytest.raises(FileExistsError):
            library.place(src_b, nfd, overwrite=False)
        assert nfc.read_bytes() == b"VIDEO-A"
    else:
        library.place(src_b, nfd, overwrite=False)
        assert nfc.read_bytes() == b"VIDEO-A"
        assert nfd.read_bytes() == b"VIDEO-B"
    assert not src_b.exists() or src_b.read_bytes() == b"VIDEO-B"


# --------------------------------------------------------------------------- #
# 5. sidecars
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("lang", ["../../../etc/pw", "en/../..", "/abs", "a/b"])
def test_a_language_code_holding_a_separator_cannot_write_outside_the_folder(lang):
    """The sidecar language comes from the MKV track header, i.e. from the disc —
    mkvmerge will report whatever three bytes are in the stream. Building the
    name with ``with_name`` is what contains it: a separator is rejected outright
    instead of splicing directories into the sidecar path."""
    video = Path("/library/Movies/WarGames (1983)/WarGames (1983).mkv")
    with pytest.raises(ValueError):
        naming.subtitle_sidecar_path(video, lang)


@pytest.mark.parametrize("lang", ["eng", "..", "", "   ", "\x00", "a\\b", "zz zz", "." * 40])
def test_no_language_code_moves_the_sidecar_out_of_the_video_folder(lang):
    """The rest of the hostile codes are legal filename text, so they are kept —
    but the sidecar must stay next to the video, because that adjacency is the
    entire mechanism by which Plex associates it."""
    video = Path("/library/Movies/WarGames (1983)/WarGames (1983).mkv")
    sidecar = naming.subtitle_sidecar_path(video, lang)

    assert sidecar.parent == video.parent
    assert sidecar.name.endswith(".srt")
    assert ".." not in sidecar.parts


def test_the_sidecar_follows_a_hostile_title_into_the_sanitized_folder(tmp_path):
    """End to end: a traversal title, then a real OCR'd .srt placed beside the
    real video. Both must end up in the same sanitized folder, sharing a stem,
    or Plex shows the film with no subtitles and nothing looks wrong."""
    movies, src = _lib(tmp_path)
    srt_src = tmp_path / "staging" / "ocr.srt"
    srt_src.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n", encoding="utf-8")

    video = naming.movie_video_path(movies, "../../etc/passwd", 2020, "mkv")
    sidecar = naming.subtitle_sidecar_path(video, "eng", default=True)

    assert sidecar.parent == video.parent
    assert sidecar.name == video.stem + ".en.default.srt"

    placed_video = library.place(src, video)
    placed_srt = library.place(srt_src, sidecar)

    assert placed_srt.parent == placed_video.parent
    assert placed_srt.resolve().is_relative_to(movies.resolve())
    assert sorted(p.name for p in movies.iterdir()) == [video.parent.name]


def test_the_nfo_escapes_a_title_that_looks_like_xml_markup(tmp_path):
    """The .nfo is the one place a title becomes *markup* rather than a path. An
    unescaped ``<`` makes the file unparseable (Plex/Kodi silently ignore it) and
    a crafted title could inject extra elements — a fake ``<year>`` or a second
    ``<title>`` overriding ours."""
    movies, _src = _lib(tmp_path)
    hostile = 'Attack of the <Killer> "Tomatoes" & Sons </title><title>Pwned'
    video = naming.movie_video_path(movies, hostile, 1978, "mkv")
    video.parent.mkdir(parents=True, exist_ok=True)

    a = EpisodeAssignment(
        title_id=1, kind=MediaKind.MOVIE, show_or_movie_title=hostile, year=1978)
    path = nfo.write_nfo(video, a)

    doc = ElementTree.fromstring(path.read_text(encoding="utf-8"))
    assert doc.tag == "movie"
    assert len(doc.findall("title")) == 1        # no injected second title
    assert doc.findtext("title") == hostile      # and ours survives verbatim
    assert doc.findtext("year") == "1978"


def test_the_opensubtitles_export_folder_stays_one_component_for_a_traversal_title(tmp_path):
    """The upload staging area is built from the same title, by its own sanitizer.
    A traversal title must become one folder inside the export dir — both the .srt
    and its .info.txt sheet."""
    export = tmp_path / "export"
    export.mkdir()
    srt = tmp_path / "src" / "WarGames (1983).en.srt"
    srt.parent.mkdir()
    srt.write_text("1\n", encoding="utf-8")

    a = EpisodeAssignment(
        title_id=1, kind=MediaKind.MOVIE, show_or_movie_title="../../etc/passwd", year=None)
    dest = os_upload.export_subtitle(
        export, srt, assignment=a, language_code="eng", is_sdh=False, forced=False,
        height=1080, fps=None, version="test")

    assert dest.resolve().is_relative_to(export.resolve())
    assert dest.relative_to(export).parts[0] not in (".", "..")
    assert len(dest.relative_to(export).parts) == 2
    assert (dest.parent / (srt.name + ".info.txt")).exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["export", "src"]


# --------------------------------------------------------------------------- #
# 6. renaming an already-placed film
# --------------------------------------------------------------------------- #


def test_renaming_a_hostile_titled_film_carries_its_sidecars_and_artwork(tmp_path):
    """A rename after a mis-identification has to move the whole set — video,
    stem-named sidecars, folder artwork — and leave no stub folder behind, or
    Plex keeps showing an empty item at the old name."""
    movies, _src = _lib(tmp_path)
    old = naming.movie_video_path(movies, "-\u202eWargames \U0001f3ac", 1983, "mkv")
    old.parent.mkdir(parents=True)
    old.write_bytes(b"VIDEO-A")
    (old.parent / (old.stem + ".en.default.srt")).write_text("subs", encoding="utf-8")
    (old.parent / (old.stem + ".nfo")).write_text("<movie/>", encoding="utf-8")
    (old.parent / "poster.jpg").write_bytes(b"POSTER")
    (old.parent / "fanart.jpg").write_bytes(b"FANART")

    new = naming.movie_video_path(movies, "WarGames", 1983, "mkv")
    library.rename_movie(old, new)

    assert new.read_bytes() == b"VIDEO-A"
    assert (new.parent / (new.stem + ".en.default.srt")).read_text() == "subs"
    assert (new.parent / (new.stem + ".nfo")).read_text() == "<movie/>"
    assert (new.parent / "poster.jpg").read_bytes() == b"POSTER"
    assert (new.parent / "fanart.jpg").read_bytes() == b"FANART"
    assert not old.parent.exists()               # no empty stub left in the library
    assert sorted(p.name for p in movies.iterdir()) == [new.parent.name]


# --------------------------------------------------------------------------- #
# 7. staging slugs
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    ["", "..", "../../etc/passwd", "/etc/passwd", "Z:\\media\\x.mkv", "\x00",
     "\U0001f3ac\U0001f3ac", "L" * 500, "   ", "-rf"],
)
def test_a_staging_slug_is_always_one_bounded_filesystem_safe_component(text):
    """Staging dirs (per-job, per-resubtitle, per-subtitle-cache) are named from
    the job source or the destination path, both attacker-influenced. The slug is
    the containment boundary: alnum-or-underscore only, never empty, and capped
    so it cannot itself blow past NAME_MAX."""
    s = _slug(text)

    assert s, repr(text)
    assert len(s) <= 40
    assert all(c.isalnum() or c == "_" for c in s), repr(s)
    assert s not in (".", "..")
    assert not (set(s) & set('/\\:<>"|?*'))
    assert (Path("/staging") / s).parent == Path("/staging")
