"""Regression tests for the services/ fixes in this round.

Each test names a way the service layer used to fail QUIETLY — files written
outside the folder they were meant for, a rename that reported success while
leaving the library split in two, a device name Windows would swallow, a good
quota reading wiped by a partial reply, a whole disc's worth of pointless API
calls, a config file that broke the drives panel, and cover art that could only
ever be written once. Every one of them is asserted through the caller-visible
result, not through the exception type.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import httpx
import pytest

from blurayripper.models import EpisodeAssignment, MediaKind
from blurayripper.services import coverart, library, naming, os_upload
from blurayripper.services import opensubtitles as OS
from blurayripper.services.drive_names import DriveNames
from blurayripper.services.opensubtitles import (
    OpenSubtitlesClient,
    OpenSubtitlesError,
    gather_references,
)

# ---------------------------------------------------------------------------
# os_upload — the manual-upload export must stay inside its export folder
# ---------------------------------------------------------------------------


def test_a_dot_dot_title_does_not_write_outside_the_export_folder(tmp_path):
    """A disc labelled ".." (or "  ..  ", which strips to the same thing) made
    ``_safe`` return ".." verbatim, so the per-title folder resolved to the export
    directory's PARENT — mkdir(exist_ok=True) succeeded silently and the .srt plus
    its upload sheet were written into someone else's directory, with the export
    folder left empty."""
    export = tmp_path / "export"
    export.mkdir()
    outside = tmp_path / "keep-out.txt"
    outside.write_text("untouched", encoding="utf-8")
    srt = tmp_path / "src" / "The Film.en.srt"
    srt.parent.mkdir()
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n", encoding="utf-8")

    dest = os_upload.export_subtitle(
        export, srt,
        assignment=EpisodeAssignment(0, MediaKind.MOVIE, "  ..  "),
        language_code="eng", is_sdh=False, forced=False,
        height=1080, fps=23.976, version="v1")

    # The returned file is really under the export dir, not just spelled that way.
    assert dest.resolve().is_relative_to(export.resolve())
    assert dest.exists() and dest.read_text(encoding="utf-8").startswith("1\n")
    assert (dest.parent / (srt.name + ".info.txt")).exists()
    # Nothing escaped into the parent directory.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["export", "keep-out.txt", "src"]
    assert outside.read_text(encoding="utf-8") == "untouched"


# ---------------------------------------------------------------------------
# library — renaming a title whose name contains glob metacharacters
# ---------------------------------------------------------------------------


def test_rename_moves_sidecars_of_a_title_containing_brackets(tmp_path):
    """``glob(old_stem + "*")`` read "[Extended]" as a character class, so the
    video and every stem-named sidecar stayed in the old folder while the
    fixed-name artwork moved to the new one: one film in two places, and
    rename_movie returned normally as if it had worked."""
    old_dir = tmp_path / "Movie [Extended] (2020)"
    old_dir.mkdir()
    old_video = old_dir / "Movie [Extended] (2020).mkv"
    old_video.write_text("video", encoding="utf-8")
    (old_dir / "Movie [Extended] (2020).en.srt").write_text("subs", encoding="utf-8")
    (old_dir / "Movie [Extended] (2020).nfo").write_text("nfo", encoding="utf-8")
    (old_dir / "poster.jpg").write_text("poster", encoding="utf-8")

    new_video = tmp_path / "Movie (2020)" / "Movie (2020).mkv"
    library.rename_movie(old_video, new_video)

    new_dir = new_video.parent
    assert sorted(p.name for p in new_dir.iterdir()) == [
        "Movie (2020).en.srt", "Movie (2020).mkv", "Movie (2020).nfo", "poster.jpg"]
    assert new_video.read_text(encoding="utf-8") == "video"
    assert (new_dir / "Movie (2020).en.srt").read_text(encoding="utf-8") == "subs"
    # The old folder is emptied and removed — the library is not split in two.
    assert not old_dir.exists()


def test_rename_of_an_already_moved_title_is_a_no_op(tmp_path):
    """The prefix scan replaced a glob that answered "nothing" on a missing
    folder. A re-run of a rename that already happened must stay harmless rather
    than blowing up on the vanished source directory."""
    new_video = tmp_path / "Movie (2020)" / "Movie (2020).mkv"
    new_video.parent.mkdir(parents=True)
    new_video.write_text("video", encoding="utf-8")

    library.rename_movie(tmp_path / "Gone (2020)" / "Gone (2020).mkv", new_video)

    assert new_video.read_text(encoding="utf-8") == "video"


# ---------------------------------------------------------------------------
# naming — Windows device names keep their power through an extension
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title, container",
    [("CON", "mkv"), ("NUL.2020", "mkv"), ("LPT1.x", "mkv"),
     ("con.mkv", "mkv"), ("Aux.Part.One", "mp4"), ("prn.1998", "mkv")],
)
def test_a_device_name_with_an_extension_is_still_pushed_aside(tmp_path, title, container):
    """Windows resolves a device by the text BEFORE the first dot, so CON.mkv,
    NUL.2020 and LPT1.x are the same devices as CON, NUL and LPT1: the folder
    cannot be created and a write to NUL.mkv disappears into the bit bucket while
    reporting success. The guard used to test the whole component, so only the
    bare names were caught."""
    dest = naming.movie_video_path(tmp_path, title, None, container)

    for part in (dest.parent.name, dest.name):
        assert part.split(".", 1)[0].upper() not in naming._WINDOWS_RESERVED
    # And the escaped name is still a usable path.
    dest.parent.mkdir(parents=True)
    dest.write_text("x", encoding="utf-8")
    assert dest.read_text(encoding="utf-8") == "x"


@pytest.mark.parametrize("title", ["Conan.The.Barbarian", "Console.Wars", "Nullify.2001",
                                   "Compass", "Mrs. Doubtfire"])
def test_titles_that_merely_look_like_a_device_name_keep_their_name(tmp_path, title):
    """The counterweight: only an exact device name before the first dot is a
    device. Escaping "Conan.The.Barbarian" would rename real films."""
    dest = naming.movie_video_path(tmp_path, title, 1999, "mkv")
    assert dest.parent.name == f"{title} (1999)"
    assert not dest.name.startswith("_")


# ---------------------------------------------------------------------------
# opensubtitles — the quota snapshot and the missing-key verdict
# ---------------------------------------------------------------------------

_REQ = httpx.Request("GET", "https://api.opensubtitles.com/api/v1/x")
_LINK = "https://cdn.example/ref.srt"
_SRT = "1\n00:00:01,000 --> 00:00:02,000\nHello\n"


class _FakeHTTP:
    """Replays canned responses keyed by URL fragment and counts the requests."""

    def __init__(self, routes):
        self.routes = routes
        self.urls: list[str] = []

    def factory(self, *_a, **_kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def get(self, url, headers=None, params=None):
        return self._reply(url)

    async def post(self, url, headers=None, json=None):
        return self._reply(url)

    def _reply(self, url):
        self.urls.append(url)
        for fragment, resp in self.routes.items():
            if fragment in url:
                return resp() if callable(resp) else resp
        raise AssertionError(f"unstubbed request: {url}")


def _search_hit():
    return httpx.Response(200, request=_REQ, json={"data": [{"attributes": {
        "language": "en", "download_count": 9, "files": [{"file_id": 42}]}}]})


async def test_a_reply_without_quota_fields_keeps_the_last_known_numbers(monkeypatch):
    """The quota panel falls back to this snapshot whenever /infos/user is
    unavailable (most API keys cannot call it). Rebuilding the snapshot from every
    /download reply meant one reply that omitted the fields — the API does not
    send them on every response — reset a perfectly good reading to all-None, and
    the panel then reported "remaining: null" instead of the last real figure."""
    bodies = iter([
        {"link": _LINK, "remaining": 96, "requests": 4,
         "reset_time": "5 hours", "reset_time_utc": "2026-08-22T04:10:00Z"},
        {"link": _LINK},                       # same endpoint, no quota block
    ])
    http = _FakeHTTP({
        "/subtitles": _search_hit,
        "/download": lambda: httpx.Response(200, request=_REQ, json=next(bodies)),
        _LINK: httpx.Response(200, request=_REQ, text=_SRT),
    })
    monkeypatch.setattr(OS.httpx, "AsyncClient", http.factory)
    client = OpenSubtitlesClient("key")

    assert await client.fetch_episode_srt(1043, 2, 6, "en") == _SRT
    assert await client.fetch_episode_srt(1043, 2, 7, "en") == _SRT

    assert client.last_known_quota() == {
        "remaining": 96, "requests": 4,
        "reset_time": "5 hours", "reset_time_utc": "2026-08-22T04:10:00Z"}


async def test_a_fresh_reply_still_overwrites_the_stale_numbers(monkeypatch):
    """Merging must not freeze the snapshot: when a reply DOES state the figures,
    they win, including a legitimate zero."""
    bodies = iter([
        {"link": _LINK, "remaining": 96, "requests": 4},
        {"link": _LINK, "remaining": 0, "requests": 100},
    ])
    http = _FakeHTTP({
        "/subtitles": _search_hit,
        "/download": lambda: httpx.Response(200, request=_REQ, json=next(bodies)),
        _LINK: httpx.Response(200, request=_REQ, text=_SRT),
    })
    monkeypatch.setattr(OS.httpx, "AsyncClient", http.factory)
    client = OpenSubtitlesClient("key")

    await client.fetch_episode_srt(1043, 2, 6, "en")
    await client.fetch_episode_srt(1043, 2, 7, "en")

    assert client.last_known_quota()["remaining"] == 0
    assert client.last_known_quota()["requests"] == 100


async def test_a_missing_api_key_stops_the_disc_after_the_first_episode(monkeypatch):
    """A key that is not configured fails every episode the same way, which is
    exactly what ``fatal`` means in this module. Reported as non-fatal,
    gather_references skipped its early break and re-raised once per candidate
    episode, then handed the caller a failure that reads as retryable."""
    monkeypatch.setattr(OS.httpx, "AsyncClient", _unreachable)
    client = _CountingClient(OpenSubtitlesClient(None))

    with pytest.raises(OpenSubtitlesError) as ei:
        await gather_references(client, 1043, 2, [1, 2, 3, 4, 5, 6], "en")

    assert ei.value.fatal is True
    assert "OPENSUBTITLES_API_KEY" in str(ei.value)
    assert client.calls == 1  # not once per episode on a disc-wide condition


def _unreachable(*_a, **_kw):
    raise AssertionError("an HTTP client was constructed; expected none")


class _CountingClient:
    """Counts how many episodes gather_references actually asks about."""

    def __init__(self, inner):
        self._inner = inner
        self.calls = 0

    async def fetch_episode_srt(self, *args):
        self.calls += 1
        return await self._inner.fetch_episode_srt(*args)


# ---------------------------------------------------------------------------
# drive_names — a config file that parses but is not an object
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["null", "[]", '["F:", "Top drive"]', '"F:"', "42"])
def test_a_drive_names_file_that_is_not_an_object_falls_back_to_no_names(tmp_path, text):
    """The guard exists so a damaged file costs the custom labels, nothing more.
    JSON that parsed but was not a dict slipped past it and stored itself, and the
    AttributeError only surfaced later at .get() — taking out the drives panel
    instead of showing the drives under their model names."""
    path = tmp_path / "drive_names.json"
    path.write_text(text, encoding="utf-8")

    names = DriveNames(path)

    assert names.get("F:") is None
    # Still usable afterwards: setting a name repairs the file.
    names.set("F:", "Top drive")
    assert names.get("F:") == "Top drive"
    assert json.loads(path.read_text(encoding="utf-8")) == {"F:": "Top drive"}


# ---------------------------------------------------------------------------
# coverart — art has to be replaceable, not write-once
# ---------------------------------------------------------------------------


def _jpeg(w: int, h: int, *, pad: bytes = b"") -> bytes:
    """A real JPEG header: SOI, an SOF0 segment carrying the dimensions, EOI."""
    body = (b"\x08" + h.to_bytes(2, "big") + w.to_bytes(2, "big")
            + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01")
    return (b"\xff\xd8" + b"\xff\xc0" + (len(body) + 2).to_bytes(2, "big") + body
            + pad + b"\xff\xd9")


def _disc_with_art(root: Path, *, data: bytes, mode: int = 0o444) -> Path:
    dl = root / "BDMV" / "META" / "DL"
    dl.mkdir(parents=True, exist_ok=True)
    art = dl / "art.jpg"
    art.write_bytes(data)
    os.chmod(art, mode)
    return art


def test_disc_art_can_be_re_extracted_over_the_copy_it_wrote_last_time(tmp_path):
    """Files on a mounted disc are read-only and ``shutil.copy2`` carries the mode
    across, so the FIRST extraction produced a read-only fanart.jpg that no later
    run could replace — a re-rip or a corrected disc silently kept the old art,
    reported to the operator only as "Cover art skipped"."""
    disc, dest = tmp_path / "disc", tmp_path / "out"
    dest.mkdir()
    art = _disc_with_art(disc, data=_jpeg(800, 600, pad=b"\x00" * 8))

    first = coverart.extract_art(disc, dest)
    assert first.fanart is not None

    # The disc is re-read after the art was corrected upstream.
    os.chmod(art, 0o644)
    art.write_bytes(_jpeg(800, 600, pad=b"\x11" * 64))
    os.chmod(art, 0o444)

    second = coverart.extract_art(disc, dest)

    assert second.fanart == first.fanart
    assert second.fanart.read_bytes() == art.read_bytes()   # the NEW art landed


def test_read_only_art_left_by_another_tool_is_replaced_not_abandoned(tmp_path):
    """Plex and other taggers set the read-only attribute on poster.jpg /
    fanart.jpg, and on the Z: share that makes the overwrite fail even though the
    folder is writable. metadata._replace_bytes already clears it for TMDB art;
    the disc-art path used to raise straight out of extract_art instead."""
    disc, dest = tmp_path / "disc", tmp_path / "out"
    dest.mkdir()
    _disc_with_art(disc, data=_jpeg(800, 600, pad=b"\x22" * 32), mode=0o644)
    stale = dest / "fanart.jpg"
    stale.write_bytes(b"someone else's poster")
    os.chmod(stale, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

    art = coverart.extract_art(disc, dest)

    assert art.fanart == stale
    assert stale.read_bytes() == (disc / "BDMV" / "META" / "DL" / "art.jpg").read_bytes()


def test_art_pulled_out_of_an_iso_replaces_a_read_only_file_too(tmp_path, monkeypatch):
    """The in-ISO path writes the same poster.jpg / fanart.jpg into the same Plex
    folder, so it hits the same read-only destination — and used to raise out of
    extract_art_from_iso instead of replacing it."""
    fresh = _jpeg(800, 600, pad=b"\x33" * 40)
    monkeypatch.setattr(coverart, "_iso_images", lambda _p: [("art.jpg", fresh)])
    dest = tmp_path / "out"
    dest.mkdir()
    stale = dest / "fanart.jpg"
    stale.write_bytes(b"someone else's backdrop")
    os.chmod(stale, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

    art = coverart.extract_art_from_iso(tmp_path / "disc.iso", dest)

    assert art.fanart == stale
    assert stale.read_bytes() == fresh


def test_a_worker_starts_even_when_the_nas_is_away(tmp_path):
    """Live failure on handbreak01: the CIFS share dropped, Pipeline built a
    MappingStore whose constructor mkdir'd under output_dir, and the worker died
    with PermissionError before any of its own diagnostics ran — restarting into
    the same crash every few seconds.

    A worker that starts and reports "the ripped files are not reachable" is far
    more use than one that will not boot, and the share coming back then needs
    no restart."""
    from blurayripper.services.mapping_store import MappingStore

    unreachable = tmp_path / "not-mounted" / "media" / ".blurayripper" / "m.db"
    # Simulate a directory that cannot be created, as an absent mount point is.
    (tmp_path / "not-mounted").mkdir()
    (tmp_path / "not-mounted").chmod(0o500)
    try:
        store = MappingStore(unreachable)      # must NOT raise
        with pytest.raises(OSError):
            store.get("some-fingerprint")      # ...but using it still reports
    finally:
        (tmp_path / "not-mounted").chmod(0o700)


def test_the_mapping_store_still_works_once_storage_is_there(tmp_path):
    """Deferring must not mean never: the schema is created on first use."""
    from blurayripper.models import EpisodeAssignment, MediaKind
    from blurayripper.services.mapping_store import MappingStore

    store = MappingStore(tmp_path / "out" / ".blurayripper" / "m.db")
    assert store.get("nope") is None           # creates the db on first touch
    store.save("fp1", "HOUSE_S1_D1",
               [EpisodeAssignment(0, MediaKind.TV, "House", 2004, season=1, episode=1)])
    got = store.get("fp1")
    assert got and got[0].show_or_movie_title == "House"
