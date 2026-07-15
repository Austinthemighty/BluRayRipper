"""SubtitleEdit OCR command shape — must not pass the unsupported language flag."""

from pathlib import Path

from blurayripper.config import load_settings
from blurayripper.tools.subtitleedit import SubtitleEdit


def _se(monkeypatch):
    s = load_settings()
    # Pretend the tool is present so build_command doesn't error on lookup.
    monkeypatch.setattr(s.tools, "subtitleedit", "/usr/bin/SubtitleEdit")
    monkeypatch.setattr(s.tools, "subtitleedit_runner", None)
    return SubtitleEdit(s)


def test_command_uses_tesseract_engine_without_language_flag(monkeypatch):
    cmd = _se(monkeypatch).build_command(Path("x.sup"), "eng", Path("/out"))
    joined = " ".join(cmd)
    assert "/ocrengine:tesseract" in cmd
    # SubtitleEdit batch OCR rejects a language flag — it must NOT be present.
    assert "tesseractlanguage" not in joined
    assert "/convert" in cmd and "srt" in cmd


def test_fixcommonerrors_toggles(monkeypatch):
    se = _se(monkeypatch)
    monkeypatch.setattr(se._settings.subtitles, "fix_common_errors", True)
    assert "/fixcommonerrors" in se.build_command(Path("x.sup"), "eng", Path("/out"))
    monkeypatch.setattr(se._settings.subtitles, "fix_common_errors", False)
    assert "/fixcommonerrors" not in se.build_command(Path("x.sup"), "eng", Path("/out"))


def test_gui_exe_gets_convert_but_seconv_does_not(monkeypatch):
    s = load_settings()
    monkeypatch.setattr(s.tools, "subtitleedit_runner", None)

    # GUI SubtitleEdit.exe -> needs the /convert batch keyword
    monkeypatch.setattr(s.tools, "subtitleedit", r"C:\Program Files\Subtitle Edit\SubtitleEdit.exe")
    gui = SubtitleEdit(s).build_command(Path("x.sup"), "eng", Path("/out"))
    assert gui[1] == "/convert" and gui[2].endswith("x.sup")

    # seconv / se-cli -> convert-only, must NOT get /convert (it would open GUI / fail)
    for cli in (r"C:\se-cli\seconv.exe", r"C:\tools\se-cli", "/usr/bin/seconv"):
        monkeypatch.setattr(s.tools, "subtitleedit", cli)
        cmd = SubtitleEdit(s).build_command(Path("x.sup"), "eng", Path("/out"))
        assert "/convert" not in cmd
        assert cmd[1].endswith("x.sup") and cmd[2] == "srt"     # input is first arg
        assert "/ocrengine:tesseract" in cmd
