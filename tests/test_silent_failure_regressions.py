"""Four ways the app reported success while something had actually failed.

Every one of these produced a green job or a reassuring message, so none of them
would ever have been noticed from the UI — which is what makes them worth
pinning rather than merely fixing.
"""

import asyncio
import subprocess
import sys

import pytest

from blurayripper.config import load_settings
from blurayripper.tools.mkvtools import MkvTools, SubtitleTrack
from blurayripper.pipeline import Pipeline, TitleResult
from blurayripper.services import coverart
from blurayripper.tools import eject as eject_mod


class _Report:
    def __init__(self): self.events = []
    def on_event(self, m): self.events.append(m)
    def on_progress(self, *a, **k): pass
    def checkpoint(self): pass


# --- 1. two same-language tracks sharing a flavour overwrote each other -------

def test_two_sdh_tracks_in_one_language_get_separate_files(tmp_path):
    """Blu-ray rarely flags SDH, so a feature track and a commentary track both
    sniffed as SDH is the realistic case. They landed on one filename and
    library.place overwrote the first — a subtitle lost with no trace, since the
    surviving file exists and is non-empty so no later check can spot it."""
    s = load_settings()
    s.storage.output_dir = tmp_path
    s.languages.default = "eng"
    p = Pipeline(s)

    pairs = []
    for i in range(2):
        srt = tmp_path / f"src{i}.srt"
        srt.write_text(f"track {i}")
        pairs.append((srt, SubtitleTrack(track_id=i, codec="S_HDMV/PGS", is_pgs=True, language="eng", sdh=True,
                                         forced=False)))
    result = TitleResult(title_id=0)
    video = tmp_path / "Film (1999).mkv"
    video.write_bytes(b"v")

    asyncio.run(p._place_title_subs(result, video, pairs, _Report()))

    assert len(set(result.subtitle_paths)) == 2, result.subtitle_paths
    assert all(pth.exists() for pth in result.subtitle_paths)
    assert {pth.read_text() for pth in result.subtitle_paths} == {"track 0", "track 1"}


def test_forced_tracks_are_disambiguated_the_same_way(tmp_path):
    s = load_settings()
    s.storage.output_dir = tmp_path
    p = Pipeline(s)
    pairs = []
    for i in range(2):
        srt = tmp_path / f"f{i}.srt"
        srt.write_text(f"forced {i}")
        pairs.append((srt, SubtitleTrack(track_id=i, codec="S_HDMV/PGS", is_pgs=True, language="pol", sdh=False,
                                         forced=True)))
    result = TitleResult(title_id=0)
    video = tmp_path / "Film (1999).mkv"
    video.write_bytes(b"v")

    asyncio.run(p._place_title_subs(result, video, pairs, _Report()))
    assert len(set(result.subtitle_paths)) == 2


def test_the_first_plain_track_in_the_default_language_is_still_the_default(tmp_path):
    """The regrouping must not cost the behaviour Plex relies on: exactly one
    sidecar marked default, and only a plain track can be it."""
    s = load_settings()
    s.storage.output_dir = tmp_path
    s.languages.default = "eng"
    p = Pipeline(s)
    pairs = []
    for i, (lang, sdh) in enumerate([("eng", False), ("eng", True), ("pol", False)]):
        srt = tmp_path / f"p{i}.srt"
        srt.write_text("x")
        pairs.append((srt, SubtitleTrack(track_id=i, codec="S_HDMV/PGS", is_pgs=True, language=lang, sdh=sdh,
                                         forced=False)))
    result = TitleResult(title_id=0)
    video = tmp_path / "Film (1999).mkv"
    video.write_bytes(b"v")

    asyncio.run(p._place_title_subs(result, video, pairs, _Report()))
    defaults = [pth for pth in result.subtitle_paths if ".default." in pth.name]
    assert len(defaults) == 1 and ".en." in defaults[0].name


# --- 2. a POSIX eject that failed reported success ---------------------------

def test_a_failed_linux_eject_is_reported_rather_than_announced_as_done(monkeypatch):
    """check=False threw the exit status away, so a busy drive or a wrong
    /dev/srN still produced 'Ejected disc — safe to load the next one' and a
    push notification, while the tray never moved."""
    monkeypatch.setattr(sys, "platform", "linux")

    def failed(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "eject: device is busy")

    monkeypatch.setattr(eject_mod.subprocess, "run", failed)

    with pytest.raises(RuntimeError, match="device is busy"):
        eject_mod.eject("/dev/sr0")


def test_a_successful_linux_eject_stays_quiet(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(eject_mod.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))
    eject_mod.eject("/dev/sr0")          # no raise


def test_a_missing_eject_binary_says_which_one(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    def missing(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(eject_mod.subprocess, "run", missing)
    with pytest.raises(RuntimeError, match="'eject' is not installed"):
        eject_mod.eject("/dev/sr0")


# --- 3. an empty Dolby Vision mux was moved into the library -----------------

def test_an_empty_dolby_vision_mux_is_refused(tmp_path, monkeypatch):
    """mkvmerge exits 0 having written a header-only file when the RPU-injected
    stream is truncated. The caller then MOVES that into the Plex library and
    marks the title placed — a finished job with no video. Raising here makes
    the DV path fall back to plain HDR10 instead."""
    s = load_settings()
    s.tools.mkvmerge = "/usr/bin/mkvmerge"
    out = tmp_path / "out.mkv"

    async def fake_run(cmd, **kw):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"")             # exits 0, produces nothing usable
        class R:
            stdout_lines: list = []
            stderr_lines: list = []
        return R()

    monkeypatch.setattr("blurayripper.tools.mkvtools.process.run", fake_run)

    with pytest.raises(RuntimeError, match="no usable file"):
        asyncio.run(MkvTools(s).mux_dovi(tmp_path / "v.hevc", tmp_path / "a.mkv", out))


# --- 4. an untraversable share crashed the cover-art lookup ------------------

def test_an_unreadable_disc_directory_answers_no_rather_than_raising(tmp_path):
    """Path.exists() re-raises PermissionError, so an intermediate directory
    without traverse rights — an SMB share, a restrictively mounted disc — threw
    out of what is meant to be a lookup that answers 'not found'."""
    meta = tmp_path / "BDMV" / "META"
    (meta / "DL").mkdir(parents=True)
    meta.chmod(0o000)
    try:
        assert coverart.find_cover_images(tmp_path) == []
    finally:
        meta.chmod(0o755)
