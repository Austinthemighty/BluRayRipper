"""The conditions `check` and the tool wrappers claim they can detect.

`blurayripper check` is the only thing standing between a box that is wired up
and a box that *looks* wired up. Every row it prints is a claim about this
machine, and the ones that matter are the ones where the wrong answer is silent:
a seconv built against a .NET this box no longer has passes every path check and
then fails at OCR time, mid-job — the encode continues, the file is placed, the
job goes green and the subtitles are simply gone. Same shape for a preset whose
container disagrees with the settings, for tesseract language data that does not
cover the languages being kept, and for an eject that returned normally without
moving the tray while the pipeline announced "safe to load the next one".

So the assertions here are on the VERDICT as well as the prose: which row,
saying what, and — because a script is the real consumer — what the process
exit code was. Where a row is only a warning, the test says so by pinning the
exit code to 0; where it is fatal, a paired run with the one thing changed back
shows the exit code is attributable to that row and nothing else.

Fixtures marked "real" are verbatim production output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest
from rich.console import Console

from blurayripper import cli
from blurayripper.config import load_settings
from blurayripper.tools import eject as ej
from blurayripper.tools.subtitleedit import SubtitleEdit

# --- real production output ------------------------------------------------

# seconv built for .NET 8 on a box that only has 10.x. This is what the box
# prints instead of running; every path check above it still says "present".
DOTNET_LAUNCH_FAILURE = """\
You must install or update .NET to run this application.

App: /opt/brr-tools/seconv-bin/seconv.dll
Framework: 'Microsoft.NETCore.App', version '8.0.0' (x64)

The following frameworks were found:
  10.0.10 at [/usr/lib/dotnet/shared/Microsoft.NETCore.App]
"""

# What the subtitleedit-cli flavour says when it is handed the OTHER flavour's
# arguments: the whole conversion aborts, after the extract has already run.
SECONV_ARGUMENT_REJECTION = """\
ERROR: Unexpected argument 'pol'.
ERROR: Unknown or multiply defined option '--encoding'.
ERROR: Unknown or multiply defined option '--output-folder'.
ERROR: Value does not fall within the expected range.
   at seconv.CommandLineConverter.Convert(String[] arguments) in \
/opt/brr-tools/subtitleedit-cli/src/se-cli/CommandLineConverter.cs:line 468
"""

# The two --help dialects, trimmed. Which one answers decides real capability.
CLASSIC_HELP = """
usage: seconv <pattern> <name-of-format-without-spaces> [<optional-parameters>]
   /outputfolder:<folder name>
   /encoding:<encoding name>
   /overwrite
   /ocrdb:<ocr db>
   /splitlonglines
   /balancelines
""".lower()

NEW_HELP = """
usage: seconv [options] <input>
  --format <format>
  --ocr-engine <engine>
  --ocr-language <lang>
  --output-folder <dir>
  --fix-common-errors
  --split-long-lines
  --balance-lines
""".lower()


# --- harness ---------------------------------------------------------------


_BOX_DRAWING = ("\u2500", "\u257f")

# shutil.which insists on the +x bit on POSIX and on a PATHEXT suffix on
# Windows. Read once, at import, so a test that pins sys.platform for the code
# under test cannot change what the fake binaries on disk are called.
_EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""


def _flat(text: str) -> str:
    """Rich lays the check out as tables, so the same sentence can arrive split
    across cell rows with a border between the halves. Drop the box-drawing and
    collapse whitespace so a test can assert on the sentence, not the layout."""
    stripped = "".join(
        " " if _BOX_DRAWING[0] <= ch <= _BOX_DRAWING[1] else ch for ch in text)
    return " ".join(stripped.split())


def _exe(tmp_path: Path, name: str) -> str:
    """A file that ``shutil.which`` will resolve."""
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    path = tools / (name + _EXE_SUFFIX)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _settings(tmp_path: Path, *, container: str = "mkv", preset_format: str | None = None):
    """A machine where every check already passes.

    That is the whole point of the fixture: with something else also broken the
    command returns non-zero no matter what, so "assert rc != 0" would hold
    whether or not the row under test failed it.

    Loaded from ``tmp_path`` rather than the repo's config dir: the real one
    picks up gitignored settings.local.json / secrets.env, so on a developer box
    that has them these tests would run against that box's tuning."""
    s = load_settings(tmp_path)
    s.config_dir = tmp_path
    s.storage.staging_dir = tmp_path / "staging"
    s.storage.output_dir = tmp_path / "out"
    for name in ("makemkvcon", "handbrakecli", "mkvmerge", "mkvextract", "subtitleedit"):
        setattr(s.tools, name, _exe(tmp_path, name))
    s.tools.subtitleedit_runner = None
    s.tools.ffmpeg = ""
    s.tools.dovitool = ""
    preset = tmp_path / "presets.json"
    preset.write_text(json.dumps({"PresetList": [{
        "PresetName": "P",
        "FileFormat": preset_format or f"av_{container}",
        "VideoEncoder": "x265",
        "VideoQualitySlider": 20,
    }]}), encoding="utf-8")
    s.encode.preset_file = preset
    s.encode.preset_name = "P"
    s.encode.container = container
    s.encode.ffmpeg_uhd = False
    s.encode.dovi = False
    s.subtitles.fix_common_errors = False
    s.subtitles.remove_text_for_hi = False
    s.subtitles.split_long_lines = False
    s.subtitles.balance_lines = False
    s.subtitles.upload_export_dir = None
    s.subtitles.dictionary_folder = None
    s.languages.keep = ["eng"]
    s.languages.default = "eng"
    s.tmdb_api_key = "tmdb-key"
    s.database_url = None
    return s


def _check(monkeypatch, capsys, settings, *, java="/usr/bin/java",
           tesseract=None, tess_langs=(), se_help="", se_probe=""):
    """Run `blurayripper check` against ``settings``, with this machine's real
    answers (which tools are on PATH, what tesseract reports, what the
    subtitleedit binary does when launched) replaced by the arguments."""
    # Pin the console: rich turns colour on from FORCE_COLOR/TTY_COMPATIBLE in
    # the ambient environment, and every assertion below reads the plain text.
    monkeypatch.setattr(
        cli, "console", Console(width=240, force_terminal=False, no_color=True))
    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: settings)

    async def _help(self):
        return se_help

    monkeypatch.setattr(SubtitleEdit, "help_text", _help)

    real_which = shutil.which

    def _which(name, *a, **k):
        if name == "java":
            return java
        if name == "tesseract":
            return tesseract
        return real_which(name, *a, **k)

    monkeypatch.setattr(shutil, "which", _which)

    def _run(cmd, **kw):
        argv0 = str(cmd[0])
        if java and argv0 == java:
            # java has printed its version to stderr for decades.
            return types.SimpleNamespace(
                returncode=0, stdout="", stderr='openjdk version "21.0.2" 2024-01-16\n')
        if tesseract and argv0 == tesseract:
            if "--list-langs" in cmd:
                body = "List of available languages (%d):\n%s\n" % (
                    len(tess_langs), "\n".join(tess_langs))
                return types.SimpleNamespace(returncode=0, stdout=body, stderr="")
            return types.SimpleNamespace(
                returncode=0, stdout="tesseract 5.3.3\n", stderr="")
        # Anything else is the subtitleedit launch probe.
        return types.SimpleNamespace(
            returncode=1 if se_probe else 0, stdout="", stderr=se_probe)

    monkeypatch.setattr(subprocess, "run", _run)

    rc = asyncio.run(cli._cmd_check(argparse.Namespace(config=None)))
    return rc, _flat(capsys.readouterr().out)


# ==========================================================================
# check: the external-tool rows
# ==========================================================================


def test_the_reference_machine_passes_every_check(tmp_path, monkeypatch, capsys):
    """The baseline every other test varies one thing against. If this drifts
    into failing, every "rc == 1" below stops being evidence of anything."""
    rc, out = _check(monkeypatch, capsys, _settings(tmp_path))

    assert rc == 0
    assert "All checks passed." in out
    assert "Some checks failed" not in out


def test_a_tool_nobody_configured_reads_differently_from_one_that_moved(
        tmp_path, monkeypatch, capsys):
    """Two different problems with two different fixes: nothing is set here yet,
    versus a shared config that names a path this particular box doesn't have
    (the normal state of a mixed Linux-serve / Windows-worker cluster). Both are
    fatal, and collapsing them to one message sends people to the wrong file."""
    rc_ok, _ = _check(monkeypatch, capsys, _settings(tmp_path))

    unset = _settings(tmp_path)
    unset.tools.mkvmerge = ""
    rc_unset, out_unset = _check(monkeypatch, capsys, unset)

    moved = _settings(tmp_path)
    moved.tools.mkvmerge = str(tmp_path / "gone" / "mkvmerge")
    rc_moved, out_moved = _check(monkeypatch, capsys, moved)

    assert rc_ok == 0
    assert rc_unset == 1 and rc_moved == 1     # mkvmerge alone flipped both
    assert "mkvmerge MISSING — install or set in settings.toml" in out_unset
    assert "configured but NOT found" not in out_unset
    assert (f"mkvmerge {moved.tools.mkvmerge} — configured but NOT found on this machine"
            in out_moved)
    assert "MISSING — install or set" not in out_moved


def test_a_seconv_box_without_tesseract_fails_while_the_gui_box_does_not(
        tmp_path, monkeypatch, capsys):
    """seconv/se-cli shell out to a SYSTEM tesseract; the classic GUI
    SubtitleEdit.exe bundles its own. Demanding tesseract of the GUI box would
    fail a machine over a tool it cannot use — and not demanding it of the
    seconv box means every OCR on it fails mid-job."""
    cli_box = _settings(tmp_path)
    cli_box.tools.subtitleedit = _exe(tmp_path, "seconv")
    rc_cli, out_cli = _check(monkeypatch, capsys, cli_box,
                             se_help=NEW_HELP, tesseract=None)

    rc_gui, out_gui = _check(monkeypatch, capsys, _settings(tmp_path),
                             se_help="", tesseract=None)

    assert rc_cli == 1
    assert "NOT found on PATH — seconv/se-cli OCR needs it" in out_cli
    assert rc_gui == 0
    assert "not on PATH — GUI SubtitleEdit.exe uses its bundled copy" in out_gui


def test_the_kept_languages_are_checked_against_the_installed_tesseract_data(
        tmp_path, monkeypatch, capsys):
    """MakeMKV reports ISO 639-2/B ("ger"); tesseract names its data 639-2/T
    ("deu"). Comparing the two code sets naively reports every German disc as
    missing language data — or, worse, reports nothing missing when it is."""
    s = _settings(tmp_path)
    s.languages.keep = ["eng", "ger"]
    tess = _exe(tmp_path, "tesseract")

    rc_gap, out_gap = _check(monkeypatch, capsys, s,
                             tesseract=tess, tess_langs=("eng", "fra"))
    rc_have, out_have = _check(monkeypatch, capsys, s,
                               tesseract=tess, tess_langs=("deu", "eng"))

    assert "MISSING for kept languages: deu" in out_gap
    assert "installed: eng, fra — MISSING" in out_gap
    assert "tesseract langs deu, eng" in out_have    # complete: just the list
    assert "MISSING for kept languages" not in out_have
    # A warning row, not a gate: the disc may never carry that track.
    assert rc_gap == 0 and rc_have == 0


def test_a_ripper_whose_seconv_cannot_launch_is_caught_before_the_disc_is(
        tmp_path, monkeypatch, capsys):
    """The silent one. A configured path proves the file is THERE, not that it
    runs: this seconv is a framework-dependent .NET build and the box's runtime
    moved on. Every path check passes, then OCR fails mid-job, the encode
    carries on, and the movie is placed with no subtitles at all."""
    s = _settings(tmp_path)
    s.tools.subtitleedit = _exe(tmp_path, "seconv")
    tess = _exe(tmp_path, "tesseract")

    rc_broken, out_broken = _check(monkeypatch, capsys, s, se_help=NEW_HELP,
                                   tesseract=tess, tess_langs=("eng",),
                                   se_probe=DOTNET_LAUNCH_FAILURE)
    rc_runs, out_runs = _check(monkeypatch, capsys, s, se_help=NEW_HELP,
                               tesseract=tess, tess_langs=("eng",), se_probe="")

    assert rc_broken == 1
    assert "subtitleedit (runs?)" in out_broken
    # The framework version comes out of the tool's own complaint, so the
    # operator is told which runtime it wants rather than "some .NET".
    assert "built against .NET 8.0.0" in out_broken
    assert "the video is placed with no subtitles" in out_broken
    assert "DOTNET_ROLL_FORWARD=LatestMajor" in out_broken

    assert rc_runs == 0                    # launching cleanly is the only change
    assert "subtitleedit (runs?)" not in out_runs


def test_the_three_subtitleedit_modes_are_each_named_as_themselves(
        tmp_path, monkeypatch, capsys):
    """Three real programs, not two. Calling the classic CLI "GUI batch mode"
    sent people hunting for a GUI setting that does not exist on that build."""
    s = _settings(tmp_path)
    s.tools.subtitleedit = _exe(tmp_path, "seconv")

    _, new_out = _check(monkeypatch, capsys, s, se_help=NEW_HELP)
    _, classic_out = _check(monkeypatch, capsys, s, se_help=CLASSIC_HELP)
    _, gui_out = _check(monkeypatch, capsys, _settings(tmp_path), se_help="")

    assert "seconv CLI — Tesseract, per-track language" in new_out
    assert "subtitleedit-cli — NOcr, no language option" in classic_out
    assert "GUI batch mode — bundled Tesseract, GUI-set language" in gui_out
    # ...and no run claims to be one of the others.
    assert "NOcr" not in new_out and "GUI batch mode" not in new_out
    assert "Tesseract, per-track" not in classic_out
    assert "GUI batch mode" not in classic_out
    assert "NOcr" not in gui_out and "per-track" not in gui_out


def test_the_nocr_flavour_report_lists_the_refinements_that_build_cannot_do(
        tmp_path, monkeypatch, capsys):
    """Settings that are on in the config but absent from this build's --help
    are silently dropped at OCR time (sending one aborts the whole conversion).
    Saying which ones were dropped is the only way to learn the quality
    difference without hearing it from the subtitles themselves."""
    s = _settings(tmp_path)
    s.tools.subtitleedit = _exe(tmp_path, "seconv")
    s.subtitles.fix_common_errors = True
    s.subtitles.remove_text_for_hi = True
    s.subtitles.split_long_lines = True     # this one IS in CLASSIC_HELP
    s.subtitles.balance_lines = True        # ...and so is this one

    rc, out = _check(monkeypatch, capsys, s, se_help=CLASSIC_HELP,
                     tesseract=_exe(tmp_path, "tesseract"), tess_langs=("eng",))

    assert "Also unavailable in this build: fix common OCR errors, remove text for HI" in out
    assert "split long lines" not in out and "balance lines" not in out
    assert "'/ocrdb' database" in out       # named from the help it answered with
    assert rc == 0                          # a quality note, not a gate


def test_the_ocr_fix_dictionary_is_only_demanded_where_it_would_be_used(
        tmp_path, monkeypatch, capsys):
    """The deeper glyph-repair pass needs --fix-common-errors AND a dictionary
    folder. Unset, the pass quietly does nothing. The row is meaningless for a
    build that has no such pass, so it must not appear there."""
    s = _settings(tmp_path)
    s.tools.subtitleedit = _exe(tmp_path, "seconv")
    s.subtitles.fix_common_errors = True
    tess = _exe(tmp_path, "tesseract")

    rc_unset, out_unset = _check(monkeypatch, capsys, s, se_help=NEW_HELP,
                                 tesseract=tess, tess_langs=("eng",))

    bad_dir = str(tmp_path / "no-such-dir")
    s.subtitles.dictionary_folder = bad_dir
    rc_bad, out_bad = _check(monkeypatch, capsys, s, se_help=NEW_HELP,
                             tesseract=tess, tess_langs=("eng",))

    real_dir = tmp_path / "Dictionaries"
    real_dir.mkdir()
    s.subtitles.dictionary_folder = str(real_dir)
    _, out_good = _check(monkeypatch, capsys, s, se_help=NEW_HELP,
                         tesseract=tess, tess_langs=("eng",))

    off = _settings(tmp_path)               # GUI flavour: no such pass at all
    off.subtitles.fix_common_errors = True
    _, out_gui = _check(monkeypatch, capsys, off, se_help="",
                        tesseract=tess, tess_langs=("eng",))

    assert rc_unset == 0 and rc_bad == 0    # never fatal, only ever a warning
    assert "unset — OCR glyph fixes" in out_unset
    # The config key names a TOML section; rich must not swallow the brackets
    # the way it does elsewhere, or the hint points at nothing.
    assert "[subtitles] dictionary_folder" in out_unset
    # "unset" must read as unset, not as a path this box has lost.
    assert "NOT a folder" not in out_unset

    # The offending path is named: the usual cause is a shared config pointing
    # at the OTHER box's SubtitleEdit install.
    assert f"{bad_dir} — configured but NOT a folder on this machine" in out_bad
    assert str(real_dir) in out_good and "NOT a folder" not in out_good
    assert "OCR-fix dictionary" not in out_gui


def test_the_windows_only_runtime_row_stays_off_a_non_windows_box(
        tmp_path, monkeypatch, capsys):
    """vcruntime140.dll is meaningless on the Linux serve box. A permanent
    "n/a" row is the kind of noise that trains people to skim the table.

    The platform is pinned rather than inherited, so this asserts the row's
    ABSENCE wherever it runs — read off the developer's own OS it would quietly
    assert the opposite claim on a Windows worker."""
    settings = _settings(tmp_path)
    monkeypatch.setattr(sys, "platform", "linux")

    rc, out = _check(monkeypatch, capsys, settings)

    assert rc == 0
    assert "vcruntime140.dll" not in out
    assert "n/a (not Windows)" not in out


# ==========================================================================
# check: storage
# ==========================================================================


def test_an_unwritable_output_folder_fails_now_rather_than_mid_job(
        tmp_path, monkeypatch, capsys):
    """The NAS is a CIFS mount that disappears. Probing by actually writing a
    file is the difference between finding out here and finding out after a
    two-hour encode has nowhere to land."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file", encoding="utf-8")

    broken = _settings(tmp_path)
    broken.storage.output_dir = blocker / "library"
    rc_bad, out_bad = _check(monkeypatch, capsys, broken)

    rc_ok, out_ok = _check(monkeypatch, capsys, _settings(tmp_path))

    assert rc_bad == 1
    assert f"output {broken.storage.output_dir} NO" in out_bad
    assert rc_ok == 0
    assert f"output {tmp_path / 'out'} yes" in out_ok


def test_an_unreachable_optional_export_folder_warns_without_failing(
        tmp_path, monkeypatch, capsys):
    """The subtitle-upload export folder is opt-in. An unreachable one loses a
    convenience, not a rip — blocking the whole box over it would be wrong."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file", encoding="utf-8")

    s = _settings(tmp_path)
    s.subtitles.upload_export_dir = blocker / "export"
    rc, out = _check(monkeypatch, capsys, s)

    assert rc == 0
    assert f"subtitle export (optional) {s.subtitles.upload_export_dir} NO — configured but unreachable" in out


# ==========================================================================
# check: the local (gitignored) config layers
# ==========================================================================


def test_a_standalone_box_is_told_its_local_files_are_the_only_copy(
        tmp_path, monkeypatch, capsys):
    """No database means nothing is shared and nothing is restorable: these
    files are gitignored, so `git pull` never brings them back and a machine
    move drops every tuned value to the tracked defaults. That reads as "the
    app changed my settings" unless the files are named."""
    rc, out = _check(monkeypatch, capsys, _settings(tmp_path))

    assert rc == 0
    assert "No settings.local.json and no database" in out
    for fname in ("settings.local.json", "presets.local.json",
                  "secrets.env", "boxsets.local.csv"):
        assert f"{fname} no" in out
    assert "from cluster" not in out
    assert "Shared config is stored in Postgres" not in out


def test_on_a_clustered_box_a_missing_cache_is_pending_not_lost(
        tmp_path, monkeypatch, capsys):
    """With Postgres configured the three shared files are caches refreshed at
    serve startup, so absent means "not pulled yet". secrets.env is the
    exception — it holds the database URL itself, so it can never come from the
    cluster and must exist on every box."""
    s = _settings(tmp_path)
    s.database_url = "postgresql://brr@db/brr"
    (tmp_path / "presets.local.json").write_text("{}", encoding="utf-8")

    rc, out = _check(monkeypatch, capsys, s)

    assert rc == 0
    assert "settings.local.json no — from cluster" in out
    assert "boxsets.local.csv no — from cluster" in out
    assert "presets.local.json yes" in out            # actually present here
    assert "secrets.env no TMDB" in out               # plain no, never "from cluster"
    assert "No settings.local.json and no database" not in out
    assert "it holds the database URL itself, so it must exist on every box" in out


# ==========================================================================
# check: preset file <-> name <-> container
# ==========================================================================


def test_a_preset_name_the_file_does_not_contain_lists_what_it_does(
        tmp_path, monkeypatch, capsys):
    """Renaming a preset in the web UI and not in settings.toml is the common
    way here. HandBrake would refuse at encode time, hours later."""
    s = _settings(tmp_path)
    s.encode.preset_name = "Plex H265 1080p"
    rc, out = _check(monkeypatch, capsys, s)

    assert rc == 1
    assert "Preset name 'Plex H265 1080p' not found in presets.json" in out
    assert "Available: P" in out


def test_a_container_that_disagrees_with_the_preset_names_both_sides(
        tmp_path, monkeypatch, capsys):
    """The preset decides the muxer; the setting decides the file extension.
    Disagreeing means the placed file has the wrong suffix for its contents,
    which Plex reads as a broken item."""
    mismatch = _settings(tmp_path, container="mp4", preset_format="av_mkv")
    rc_bad, out_bad = _check(monkeypatch, capsys, mismatch)

    rc_ok, out_ok = _check(monkeypatch, capsys, _settings(tmp_path, container="mkv"))

    assert rc_bad == 1
    assert "Container mismatch: settings container='mp4' but preset FileFormat='av_mkv'" in out_bad
    assert rc_ok == 0
    assert "Preset OK: 'P' (av_mkv), encoder=x265, RF=20" in out_ok


def test_a_preset_file_that_is_absent_or_unparseable_is_named_as_such(
        tmp_path, monkeypatch, capsys):
    """Two different accidents — a path that points nowhere, and a presets.json
    a half-finished web-UI write truncated — with two different fixes."""
    gone = _settings(tmp_path)
    gone.encode.preset_file = tmp_path / "nowhere" / "presets.json"
    rc_gone, out_gone = _check(monkeypatch, capsys, gone)

    corrupt = _settings(tmp_path)
    corrupt.encode.preset_file.write_text('{"PresetList": [', encoding="utf-8")
    rc_corrupt, out_corrupt = _check(monkeypatch, capsys, corrupt)

    assert rc_gone == 1
    assert f"Preset file not found: {gone.encode.preset_file}" in out_gone
    assert rc_corrupt == 1
    assert "Preset file is not valid JSON" in out_corrupt
    assert "Preset file not found" not in out_corrupt


# ==========================================================================
# check: the UHD / HDR / Dolby Vision path
# ==========================================================================


def test_the_uhd_path_turned_on_without_ffmpeg_is_fatal(tmp_path, monkeypatch, capsys):
    """uhd_require_ffmpeg means a 4K title refuses to encode at all without
    ffmpeg. Letting the box start in that state parks every UHD disc."""
    off = _settings(tmp_path)
    off.encode.ffmpeg_uhd = True
    off.encode.uhd_min_height = 2160
    off.encode.ffmpeg_crf = 20
    off.encode.ffmpeg_preset = "slow"
    off.encode.ffmpeg_audio = "copy"
    rc_bad, out_bad = _check(monkeypatch, capsys, off)

    on = _settings(tmp_path)
    on.encode.ffmpeg_uhd = True
    on.encode.uhd_min_height = 2160
    on.encode.ffmpeg_crf = 20
    on.encode.ffmpeg_preset = "slow"
    on.encode.ffmpeg_audio = "copy"
    on.tools.ffmpeg = _exe(tmp_path, "ffmpeg")
    rc_ok, out_ok = _check(monkeypatch, capsys, on)

    assert rc_bad == 1
    assert "ffmpeg_uhd is on but ffmpeg not found — UHD titles can't encode" in out_bad
    assert rc_ok == 0
    # The row reports the settings the encode will actually run with, so a
    # worker that pulled a different shared config is visible here.
    assert "UHD via ffmpeg: titles >= 2160p -> libx265 (crf 20, slow, audio=copy)" in out_ok


def test_lossless_audio_copied_into_mp4_is_warned_about_not_blocked(
        tmp_path, monkeypatch, capsys):
    """TrueHD/DTS-HD cannot live in mp4, so `copy` there silently costs the HD
    audio track. It is still a legal configuration for a box that only rips
    stereo discs, so it warns rather than refusing to start.

    Both halves of the pair are varied, because either one alone raising it
    would be wrong: `copy` into mkv is the RECOMMENDED setup on every UHD box
    here, and a row that scolds the recommended setup gets skimmed past."""
    mp4 = _settings(tmp_path, container="mp4")
    mp4.encode.ffmpeg_uhd = True
    mp4.encode.ffmpeg_audio = "copy"
    mp4.tools.ffmpeg = _exe(tmp_path, "ffmpeg")
    rc_warn, out_warn = _check(monkeypatch, capsys, mp4)

    mp4.encode.ffmpeg_audio = "aac"     # transcoded: mp4 can hold it
    rc_aac, out_aac = _check(monkeypatch, capsys, mp4)

    # NB: rewrites tmp_path/presets.json to av_mkv, so it must come last.
    mkv = _settings(tmp_path, container="mkv")
    mkv.encode.ffmpeg_uhd = True
    mkv.encode.ffmpeg_audio = "copy"    # the recommended combination
    mkv.tools.ffmpeg = _exe(tmp_path, "ffmpeg")
    rc_mkv, out_mkv = _check(monkeypatch, capsys, mkv)

    assert rc_warn == 0
    assert "ffmpeg_audio='copy' needs container='mkv' (yours is 'mp4')" in out_warn
    assert rc_aac == 0
    assert "needs container='mkv'" not in out_aac
    assert rc_mkv == 0
    assert "needs container='mkv'" not in out_mkv


def test_dolby_vision_names_whichever_half_of_the_chain_is_absent(
        tmp_path, monkeypatch, capsys):
    """DV needs dovi_tool AND mkvmerge. Both absences degrade to HDR10 rather
    than failing, so the row is the only warning — and it has to name the one
    that is actually missing or the wrong tool gets installed."""
    base = _settings(tmp_path)
    base.encode.ffmpeg_uhd = True
    base.encode.dovi = True
    base.tools.ffmpeg = _exe(tmp_path, "ffmpeg")
    base.tools.dovitool = ""
    _, out_no_dovi = _check(monkeypatch, capsys, base)

    no_mkvmerge = _settings(tmp_path)
    no_mkvmerge.encode.ffmpeg_uhd = True
    no_mkvmerge.encode.dovi = True
    no_mkvmerge.tools.ffmpeg = _exe(tmp_path, "ffmpeg")
    no_mkvmerge.tools.dovitool = _exe(tmp_path, "dovi_tool")
    no_mkvmerge.tools.mkvmerge = ""
    _, out_no_mkvmerge = _check(monkeypatch, capsys, no_mkvmerge)

    assert "dovi is on but dovi_tool not found on this machine" in out_no_dovi
    assert "mkvmerge not found — DV falls back" not in out_no_dovi
    assert "dovi is on but mkvmerge not found — DV falls back to HDR10" in out_no_mkvmerge
    assert "dovi_tool not found" not in out_no_mkvmerge


def test_a_complete_dolby_vision_chain_says_dv_survives_the_encode(
        tmp_path, monkeypatch, capsys):
    """The positive verdict matters as much: without it there is no way to tell
    "DV is configured and working" from "DV silently degraded to HDR10"."""
    s = _settings(tmp_path)
    s.encode.ffmpeg_uhd = True
    s.encode.dovi = True
    s.tools.ffmpeg = _exe(tmp_path, "ffmpeg")
    s.tools.dovitool = _exe(tmp_path, "dovi_tool")

    rc, out = _check(monkeypatch, capsys, s)

    assert rc == 0
    assert "Dolby Vision: dovi_tool + mkvmerge present — DV preserved (profile 8.1)" in out
    assert "falls back to HDR10" not in out


def test_dolby_vision_without_the_ffmpeg_path_is_reported_as_having_no_effect(
        tmp_path, monkeypatch, capsys):
    """dovi only ever runs inside the ffmpeg UHD path. On its own it is a
    setting that reads as enabled and does nothing whatsoever."""
    s = _settings(tmp_path)
    s.encode.ffmpeg_uhd = False
    s.encode.dovi = True
    s.tools.dovitool = _exe(tmp_path, "dovi_tool")

    rc, out = _check(monkeypatch, capsys, s)

    assert rc == 0
    assert "dovi is on but ffmpeg_uhd is off — DV needs ffmpeg_uhd=true; no effect" in out
    assert "DV preserved" not in out


# ==========================================================================
# check: languages, TMDB, and the exit code a script reads
# ==========================================================================


def test_a_missing_tmdb_key_is_a_manual_naming_warning_not_a_failure(
        tmp_path, monkeypatch, capsys):
    """Everything still rips without TMDB; the operator just types the titles.
    Failing the box over it would stop a rip that would have worked."""
    s = _settings(tmp_path)
    s.languages.keep = ["eng", "ger"]
    s.tmdb_api_key = None
    rc_none, out_none = _check(monkeypatch, capsys, s)

    s.tmdb_api_key = "abc123"
    rc_key, out_key = _check(monkeypatch, capsys, s)

    assert rc_none == 0
    assert "No TMDB key — naming will be fully manual." in out_none
    assert rc_key == 0
    assert "TMDB key present" in out_key
    # The kept languages are echoed so a box running someone else's shared
    # config can see what it will actually keep.
    assert "Languages: keep=['eng', 'ger'], default=eng" in out_none


def test_the_closing_verdict_and_the_exit_code_never_disagree(
        tmp_path, monkeypatch, capsys):
    """A rip station runs this from a batch file, which can only see the exit
    code. A green summary over a non-zero exit (or the reverse) is worse than
    no check at all."""
    rc_ok, out_ok = _check(monkeypatch, capsys, _settings(tmp_path))

    broken = _settings(tmp_path)
    broken.tools.handbrakecli = ""
    rc_bad, out_bad = _check(monkeypatch, capsys, broken)

    assert (rc_ok, "All checks passed." in out_ok) == (0, True)
    assert "fix the items above before ripping" not in out_ok
    assert rc_bad == 1
    assert "Some checks failed — fix the items above before ripping." in out_bad
    assert "All checks passed" not in out_bad


# ==========================================================================
# tool wrappers: subtitleedit
# ==========================================================================


def _se(tmp_path: Path, *, exe: str = "/usr/local/bin/seconv", runner=None):
    # From tmp_path, not the repo config dir: see _settings.
    s = load_settings(tmp_path)
    s.tools.subtitleedit = exe
    s.tools.subtitleedit_runner = runner
    s.subtitles.dictionary_folder = None
    s.subtitles.fix_common_errors = False
    s.subtitles.remove_text_for_hi = False
    s.subtitles.split_long_lines = False
    s.subtitles.balance_lines = False
    s.subtitles.ocr_timeout_seconds = 900
    return SubtitleEdit(s)


def test_none_of_the_arguments_that_build_rejected_are_ever_sent_to_it(
        tmp_path, monkeypatch):
    """Against the verbatim rejection from the incident: `pol` arrived as a bare
    positional (this build takes no OCR language at all), and `--encoding` /
    `--output-folder` are the other flavour's spellings. One unknown option
    aborts the whole conversion, after the extract has already run."""
    rejected_options = [
        line.split("'")[1] for line in SECONV_ARGUMENT_REJECTION.splitlines()
        if "option" in line
    ]
    rejected_positional = SECONV_ARGUMENT_REJECTION.splitlines()[0].split("'")[1]
    assert rejected_options == ["--encoding", "--output-folder"]  # fixture sanity
    assert rejected_positional == "pol"

    cmd = _se(tmp_path).build_command(
        Path("/s/t000.sup"), rejected_positional, tmp_path, CLASSIC_HELP)

    for opt in rejected_options:
        assert opt not in cmd
    # The language must not reappear as a positional either — that is the line
    # the log leads with, and it is what aborted the run.
    assert rejected_positional not in cmd
    assert cmd.count(str(tmp_path)) == 0          # only ever /outputfolder:<dir>
    assert f"/outputfolder:{tmp_path}" in cmd


def test_a_runner_prefixed_build_keeps_its_launcher_in_front_of_either_dialect(
        tmp_path):
    """On Linux seconv is a .dll launched through `dotnet`. The prefix has to
    stay argv[0..1] in BOTH dialects — putting the .sup first would hand the
    subtitle file to dotnet as the assembly to run."""
    se = _se(tmp_path, exe="/opt/brr-tools/seconv-bin/seconv.dll", runner="dotnet")

    classic = se.build_command(Path("/s/t000.sup"), "eng", tmp_path, CLASSIC_HELP)
    modern = se.build_command(Path("/s/t000.sup"), "eng", tmp_path, NEW_HELP)

    assert classic[:2] == ["dotnet", "/opt/brr-tools/seconv-bin/seconv.dll"]
    assert classic[2:4] == ["/s/t000.sup", "srt"]     # pattern + format follow it
    assert modern[:2] == ["dotnet", "/opt/brr-tools/seconv-bin/seconv.dll"]
    assert modern[2] == "/s/t000.sup"


def test_an_ocr_that_produced_no_srt_is_an_error_not_an_empty_result(
        tmp_path, monkeypatch):
    """SubtitleEdit exits 0 having written nothing when the language data is
    missing. Returning a path that isn't there would place the video and record
    the subtitles as done — the exact silent loss this project keeps shipping."""
    se = _se(tmp_path)
    ran = []

    async def _run(cmd, **kw):
        ran.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout_lines=[], stderr="")

    monkeypatch.setattr("blurayripper.tools.subtitleedit.process.run", _run)

    async def _help(self):
        return NEW_HELP

    monkeypatch.setattr(SubtitleEdit, "help_text", _help)

    out_dir = tmp_path / "subs"
    with pytest.raises(FileNotFoundError) as exc:
        asyncio.run(se.ocr_to_srt(Path("/s/t000.sup"), "ger", out_dir))

    msg = str(exc.value)
    assert str(out_dir / "t000.srt") in msg
    # Named in the form tesseract installs it under, not the disc's code.
    assert "'deu' language data" in msg
    assert ran, "the OCR must actually have been attempted"


def test_the_gui_only_language_hint_is_not_offered_to_a_tool_that_takes_a_flag(
        tmp_path, monkeypatch):
    """"OCR one file in the GUI to set the language" is sound advice for
    SubtitleEdit.exe and a dead end for seconv, which takes --ocr-language. The
    hint is attached to the failure, so the wrong one costs an afternoon."""
    async def _run(cmd, **kw):
        return types.SimpleNamespace(returncode=0, stdout_lines=[], stderr="")

    monkeypatch.setattr("blurayripper.tools.subtitleedit.process.run", _run)

    async def _help(self):
        return ""

    monkeypatch.setattr(SubtitleEdit, "help_text", _help)

    gui = _se(tmp_path, exe=r"C:\SubtitleEdit\SubtitleEdit.exe")
    with pytest.raises(FileNotFoundError) as gui_exc:
        asyncio.run(gui.ocr_to_srt(Path("/s/t000.sup"), "eng", tmp_path / "g"))

    conv = _se(tmp_path, exe="/usr/local/bin/seconv")
    with pytest.raises(FileNotFoundError) as conv_exc:
        asyncio.run(conv.ocr_to_srt(Path("/s/t000.sup"), "eng", tmp_path / "c"))

    assert "SubtitleEdit GUI" in str(gui_exc.value)
    assert "SubtitleEdit GUI" not in str(conv_exc.value)


def test_an_srt_that_did_land_comes_back_as_the_path_it_landed_at(
        tmp_path, monkeypatch):
    """The other half of the same claim: a real output must not be rejected."""
    se = _se(tmp_path)
    out_dir = tmp_path / "subs"

    async def _run(cmd, **kw):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "t000.srt").write_text("1\n", encoding="utf-8")
        return types.SimpleNamespace(returncode=0, stdout_lines=[], stderr="")

    monkeypatch.setattr("blurayripper.tools.subtitleedit.process.run", _run)

    async def _help(self):
        return NEW_HELP

    monkeypatch.setattr(SubtitleEdit, "help_text", _help)

    got = asyncio.run(se.ocr_to_srt(Path("/s/t000.sup"), "eng", out_dir))

    assert got == out_dir / "t000.srt"
    assert got.exists()


# ==========================================================================
# tool wrappers: eject
# ==========================================================================


class _FakeSubprocess:
    """Stands in for eject.py's module-global ``subprocess``."""

    def __init__(self, *, returncode=0, stdout="", stderr="", raises=None):
        self.result = types.SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=stderr)
        self.raises = raises
        self.argvs = []

    def run(self, cmd, **kw):
        self.argvs.append(list(cmd))
        if self.raises is not None:
            raise self.raises
        return self.result


def _posix(monkeypatch, platform, **kw):
    monkeypatch.setattr(ej, "sys", types.SimpleNamespace(platform=platform))
    fake = _FakeSubprocess(**kw)
    monkeypatch.setattr(ej, "subprocess", fake)
    return fake


def test_an_eject_the_drive_refused_is_raised_not_swallowed(monkeypatch):
    """The headline silent failure: check=False discarded the exit status, so a
    busy drive or a jammed tray returned normally — and the pipeline logged
    "Ejected disc — safe to load the next one" and pushed a notification saying
    so, while the tray never moved."""
    fake = _posix(monkeypatch, "linux", returncode=1,
                  stderr="eject: unable to eject, last error: Device or resource busy\n")

    with pytest.raises(RuntimeError) as exc:
        ej.eject("/dev/sr0")

    msg = str(exc.value)
    assert fake.argvs == [["eject", "/dev/sr0"]]
    assert "Could not eject" in msg
    assert "exited 1" in msg                       # the status that was dropped
    assert "Device or resource busy" in msg        # the drive's own words


def test_an_eject_that_worked_stays_quiet(monkeypatch):
    """The paired half — a zero exit must still return normally, or auto-eject
    would report a failure after every successful disc."""
    fake = _posix(monkeypatch, "linux", returncode=0)

    ej.eject("/dev/sr0")                            # must not raise

    assert fake.argvs == [["eject", "/dev/sr0"]]


def test_the_last_line_of_the_tools_complaint_is_the_one_quoted(monkeypatch):
    """`eject` narrates before it fails. Quoting the first line would report
    "using device /dev/sr0" as the reason the tray didn't move."""
    _posix(monkeypatch, "linux", returncode=1,
           stderr="eject: using device `/dev/sr0'\neject: tray is locked\n")

    with pytest.raises(RuntimeError) as exc:
        ej.eject("/dev/sr0")

    msg = str(exc.value)
    assert "tray is locked" in msg
    assert "using device" not in msg


def test_a_tool_that_only_complains_on_stdout_is_still_quoted(monkeypatch):
    """Some builds print the reason to stdout and leave stderr empty; falling
    back is the difference between a reason and a bare exit code."""
    _posix(monkeypatch, "linux", returncode=1, stderr="", stdout="drive is busy\n")

    with pytest.raises(RuntimeError) as exc:
        ej.eject("/dev/sr0")

    assert "drive is busy" in str(exc.value)


def test_a_box_with_no_eject_binary_says_so_instead_of_failing_to_spawn(
        monkeypatch):
    """A minimal container image has no util-linux. A raw FileNotFoundError
    reads as "the disc is missing"; this says which package to install."""
    _posix(monkeypatch, "linux", raises=FileNotFoundError(2, "No such file"))

    with pytest.raises(RuntimeError) as exc:
        ej.eject("/dev/sr0")

    assert "'eject' is not installed on this machine" in str(exc.value)
    assert isinstance(exc.value.__cause__, FileNotFoundError)


def test_a_close_that_failed_is_reported_as_a_close_not_as_an_eject(monkeypatch):
    """An unattended station closes the tray between discs. A close that
    silently failed leaves the next rip scanning an empty drive — and being told
    "could not eject" for it sends the diagnosis the wrong way."""
    fake = _posix(monkeypatch, "linux", returncode=1, stderr="eject: tray jammed\n")

    with pytest.raises(RuntimeError) as exc:
        ej.close_tray("/dev/sr1")

    msg = str(exc.value)
    assert fake.argvs == [["eject", "-t", "/dev/sr1"]]
    assert "Could not close the tray" in msg
    assert "eject -t /dev/sr1" in msg
    assert "tray jammed" in msg


def test_the_macos_tray_commands_are_checked_just_as_hard(monkeypatch):
    """drutil is a different binary on a different branch; the exit status was
    dropped there too, and macOS is where the reference discs get ripped."""
    fake = _posix(monkeypatch, "darwin", returncode=1,
                  stderr="drutil: no drive found\n")

    with pytest.raises(RuntimeError) as eject_exc:
        ej.eject("disk3")

    with pytest.raises(RuntimeError) as close_exc:
        ej.close_tray(None)

    # drutil addresses the default drive and takes no device argument, so the
    # macOS disk id must not be appended the way /dev/srN is on Linux.
    assert fake.argvs == [["drutil", "tray", "eject"], ["drutil", "tray", "close"]]
    assert "Could not eject" in str(eject_exc.value)
    assert "drutil tray eject" in str(eject_exc.value)
    assert "Could not close the tray" in str(close_exc.value)
    assert "drutil tray close" in str(close_exc.value)
