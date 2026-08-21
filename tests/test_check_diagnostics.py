"""`check` is the primary diagnostic, so a wrong answer there costs hours.

It already has: a build whose OCR silently used the wrong engine, and a missing
JRE that rips discs incompletely rather than failing. Both were invisible until
they had already cost real time, and cli.py sat at 12% coverage while carrying
all of it.
"""

import argparse
import asyncio
import json

import pytest

from blurayripper.cli import _cmd_check
from blurayripper.config import load_settings


def _settings(tmp_path, **over):
    """A configuration where everything else already passes.

    That matters more than it looks: with other tools missing, check returns
    non-zero no matter what, so "assert rc != 0" would pass whether or not the
    thing under test failed it. The rc is only evidence when one thing varies."""
    s = load_settings()
    s.storage.staging_dir = tmp_path / "staging"
    s.storage.output_dir = tmp_path / "out"
    s.config_dir = tmp_path
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    for name in ("makemkvcon", "handbrakecli", "mkvmerge", "mkvextract",
                 "subtitleedit"):
        exe = tools / name
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)          # _on_disk uses shutil.which: must be executable
        setattr(s.tools, name, str(exe))
    if "subtitleedit" in over:
        s.tools.subtitleedit = over.pop("subtitleedit")
    s.tools.subtitleedit_runner = None
    s.tools.ffmpeg = s.tools.dovitool = ""
    preset = tmp_path / "presets.json"
    preset.write_text(json.dumps({"PresetList": [
        {"PresetName": "P", "FileFormat": "av_mkv", "VideoEncoder": "x265",
         "VideoQualitySlider": 20}]}))
    s.encode.preset_file, s.encode.preset_name = preset, "P"
    s.encode.container = "mkv"
    s.encode.ffmpeg_uhd = s.encode.dovi = False
    s.subtitles.fix_common_errors = False
    s.tmdb_api_key = "x"
    if "makemkvcon" in over:
        s.tools.makemkvcon = over.pop("makemkvcon")
    for k, v in over.items():
        setattr(s.tools, k, v)
    return s


def _run(monkeypatch, settings, java=None, help_text=""):
    monkeypatch.setattr("blurayripper.cli.load_settings", lambda *a, **k: settings)
    monkeypatch.setattr(
        "blurayripper.tools.subtitleedit.SubtitleEdit.help_text",
        _async_return(help_text))
    real_which = __import__("shutil").which
    monkeypatch.setattr("shutil.which",
                        lambda n, *a, **k: java if n == "java" else real_which(n, *a, **k))
    return asyncio.run(_cmd_check(argparse.Namespace(config=None)))


def _async_return(value):
    async def _f(self):
        return value
    return _f


def test_a_ripper_without_java_fails_the_check(tmp_path, monkeypatch, capsys):
    """MakeMKV needs a JRE for BD-Java discs. Without one it does not fail — it
    rips a subset of the titles, so the disc looks done and is quietly short.

    The pair of runs is the point: identical config, java the only difference,
    so the exit code is actually attributable to the JRE."""
    with_java = _run(monkeypatch, _settings(tmp_path), java="/usr/bin/java")
    capsys.readouterr()
    without = _run(monkeypatch, _settings(tmp_path), java=None)
    out = capsys.readouterr().out

    assert with_java == 0, "fixture should otherwise pass"
    assert without != 0                       # the JRE alone flipped it
    assert "MISSING — MakeMKV needs a JRE" in out


def test_an_encode_only_box_is_not_asked_for_java(tmp_path, monkeypatch, capsys):
    """A worker never touches a disc, so demanding a JRE there is noise you would
    learn to ignore — which is how a real warning gets missed."""
    without = _run(monkeypatch, _settings(tmp_path, makemkvcon=""), java=None)
    out = capsys.readouterr().out
    with_java = _run(monkeypatch, _settings(tmp_path, makemkvcon=""),
                     java="/usr/bin/java")
    capsys.readouterr()

    assert "BD-Java" not in out
    # Java makes no difference to the verdict here — which is the actual claim.
    # (Both runs fail on the absent makemkvcon; that is a separate requirement.)
    assert without == with_java


def test_a_ripper_with_java_passes_that_row(tmp_path, monkeypatch, capsys):
    s = _settings(tmp_path)
    rc = _run(monkeypatch, s, java="/usr/bin/java")

    out = capsys.readouterr().out
    assert "MISSING — MakeMKV needs a JRE" not in out
    assert rc == 0


CLASSIC_HELP = "usage: seconv <pattern> <format>\n /ocrdb:<ocr db>\n /overwrite\n"
NEW_HELP = "usage: seconv [options]\n --ocr-engine <engine>\n --ocr-language <lang>\n"


def test_a_nocr_build_is_named_as_such(tmp_path, monkeypatch, capsys):
    """The tesseract language data on that box is never used by this build. That
    is a quality difference you would otherwise only hear in the subtitles."""
    s = _settings(tmp_path, subtitleedit="/usr/local/bin/seconv")
    _run(monkeypatch, s, java="/usr/bin/java", help_text=CLASSIC_HELP)

    out = capsys.readouterr().out
    assert "NOcr" in out and "NOT Tesseract" in out


def test_a_tesseract_build_is_reported_as_the_good_case(tmp_path, monkeypatch, capsys):
    s = _settings(tmp_path, subtitleedit="/usr/local/bin/seconv")
    _run(monkeypatch, s, java="/usr/bin/java", help_text=NEW_HELP)

    assert "per-track" in capsys.readouterr().out


def test_the_gitignored_config_layers_are_listed(tmp_path, monkeypatch, capsys):
    """These are the files a git pull never restores and a machine move loses —
    the reason a preset silently reverted after the migration."""
    s = _settings(tmp_path)
    _run(monkeypatch, s, java="/usr/bin/java")

    out = capsys.readouterr().out
    for name in ("settings.local.json", "presets.local.json", "secrets.env",
                 "boxsets.local.csv"):
        assert name in out
