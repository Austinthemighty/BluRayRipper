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
