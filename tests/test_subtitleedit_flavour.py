"""Two entirely different programs ship as "seconv".

SubtitleEdit 5.x's CLI takes `--option value`, does Tesseract, and picks the OCR
language per track. The subtitleedit-cli project takes `/option:value` and does
NOcr. Inferring which one from the FILE NAME sent one flavour the other's
arguments — every option rejected, mid-job, after the extract had already run.
"""

from pathlib import Path

from blurayripper.config import load_settings
from blurayripper.tools.process import ProcessResult
from blurayripper.tools.subtitleedit import SubtitleEdit

# Trimmed from subtitleedit-cli's own help.
CLASSIC_HELP = """
usage: seconv <pattern> <name-of-format-without-spaces> [<optional-parameters>]
   /outputfolder:<folder name>
   /encoding:<encoding name>
   /overwrite
   /ocrdb:<ocr db>
   /removetextforhi
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


def _se(tmp_path):
    s = load_settings()
    s.tools.subtitleedit = "/usr/local/bin/seconv"
    s.tools.subtitleedit_runner = None
    s.subtitles.dictionary_folder = None
    return SubtitleEdit(s)


def test_the_classic_build_gets_slash_options_not_double_dashes(tmp_path):
    """The exact failure from job 45: `--encoding` etc. rejected wholesale."""
    cmd = _se(tmp_path).build_command(
        Path("/s/t000.sup"), "pol", tmp_path, CLASSIC_HELP)

    assert cmd[1:3] == ["/s/t000.sup", "srt"]        # positional pattern + format
    assert "/encoding:utf-8" in cmd
    assert f"/outputfolder:{tmp_path}" in cmd
    assert "/overwrite" in cmd
    assert not any(a.startswith("--") for a in cmd)  # nothing this build rejects


def test_an_option_the_build_lacks_is_dropped_rather_than_sent(tmp_path):
    """This CLI aborts the whole conversion on one unknown option, so sending a
    flag it does not have loses the subtitles entirely. FixCommonErrors is absent
    from the help above, so it must not be emitted."""
    cmd = _se(tmp_path).build_command(
        Path("/s/t000.sup"), "eng", tmp_path, CLASSIC_HELP)

    assert not any("fixcommonerrors" in a.lower() for a in cmd)
    assert "/SplitLongLines" in cmd     # this one IS in the help, so it is sent


def test_the_new_style_build_still_gets_double_dash_options(tmp_path):
    cmd = _se(tmp_path).build_command(
        Path("/s/t000.sup"), "pol", tmp_path, NEW_HELP)

    assert "--format" in cmd and "subrip" in cmd
    assert "--ocr-language" in cmd and "pol" in cmd
    assert not any(a.startswith("/") and ":" in a for a in cmd[1:])


def test_with_no_help_available_it_falls_back_to_the_old_behaviour(tmp_path):
    """An unreadable --help must not change what a working install does."""
    cmd = _se(tmp_path).build_command(Path("/s/t000.sup"), "pol", tmp_path, "")
    assert "--ocr-engine" in cmd


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_the_help_probe_runs_once_and_is_reused(tmp_path, monkeypatch):
    """OCR runs per track, and a subprocess launch per track would add up on a
    disc with several languages. The answer cannot change while we run."""
    se = _se(tmp_path)
    calls = []

    async def fake_run(cmd, **kw):
        calls.append(cmd)
        return ProcessResult(0, ["--ocr-engine tesseract"], "")

    monkeypatch.setattr("blurayripper.tools.subtitleedit.process.run", fake_run)

    assert "--ocr-engine" in _run(se.help_text())
    assert "--ocr-engine" in _run(se.help_text())
    assert len(calls) == 1
    assert calls[0][-1] == "--help"


def test_a_tool_that_cannot_be_probed_falls_back_instead_of_failing(tmp_path, monkeypatch):
    """A --help that errors must not break OCR — it drops to the previous
    name-based guess. Raising here would turn an unknown into an outage."""
    se = _se(tmp_path)

    async def boom(cmd, **kw):
        raise OSError("cannot execute")

    monkeypatch.setattr("blurayripper.tools.subtitleedit.process.run", boom)

    assert _run(se.help_text()) == ""
    # ...and an empty help still produces a usable command.
    cmd = se.build_command(Path("/s/t.sup"), "eng", tmp_path, "")
    assert "--ocr-engine" in cmd          # the seconv-named fallback


def test_stderr_help_is_read_too(tmp_path, monkeypatch):
    """Plenty of CLIs print usage to stderr; reading only stdout would see an
    empty help and silently pick the wrong dialect.

    The stub returns the REAL ``ProcessResult`` on purpose: this test used to
    hand back a hand-made object carrying a ``stderr_lines`` list that the
    production type has never had, so it passed against code that could not
    read stderr at all."""
    se = _se(tmp_path)

    async def fake_run(cmd, **kw):
        return ProcessResult(
            0, [], "usage: seconv <pattern> <format>\n /ocrdb:<ocr db>\n")

    monkeypatch.setattr("blurayripper.tools.subtitleedit.process.run", fake_run)
    assert "/ocrdb:" in _run(se.help_text())
