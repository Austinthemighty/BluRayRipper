"""SubtitleEdit CLI wrapper: OCR a PGS (.sup) subtitle into .srt via Tesseract.

Cross-platform: on Windows ``SubtitleEdit.exe`` runs directly; on Linux/macOS it
runs under a ``runner`` prefix (``dotnet`` for a seconv .dll, or ``mono`` for the
legacy SE 4.x GUI) or directly as a native ``seconv`` build.
The launcher prefix is a config value so the rest of the app is OS-agnostic.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Settings
from . import process

# Map MakeMKV/mkvmerge ISO 639-2/B (bibliographic) codes to the Tesseract
# language data names (ISO 639-2/T, terminology) where they differ.
_TESSERACT_LANG = {
    "ger": "deu",
    "fre": "fra",
    "dut": "nld",
    "gre": "ell",
    "chi": "chi_sim",
    "cze": "ces",
    "ice": "isl",
    "per": "fas",
    "rum": "ron",
    "slo": "slk",
    "wel": "cym",
    "alb": "sqi",
    "arm": "hye",
    "baq": "eus",
    "bur": "mya",
    "geo": "kat",
    "may": "msa",
    "mac": "mkd",
    "tib": "bod",
}


def tesseract_language(iso_639_2b: str) -> str:
    code = iso_639_2b.strip().lower()
    return _TESSERACT_LANG.get(code, code)


class SubtitleEdit:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _launcher(self) -> list[str]:
        exe = self._settings.tools.require("subtitleedit")
        runner = self._settings.tools.subtitleedit_runner
        return ([runner, exe] if runner else [exe])

    def _is_cli_tool(self) -> bool:
        """True when the configured tool is the dedicated CLI (seconv / se-cli),
        which is convert-only and must NOT be given the GUI's /convert keyword.
        The GUI SubtitleEdit.exe still needs /convert; without it, SubtitleEdit 5
        (the cross-platform rewrite) just opens the window instead of batching."""
        exe = self._settings.tools.require("subtitleedit")
        base = exe.replace("\\", "/").rsplit("/", 1)[-1]  # basename, either separator
        stem = base.rsplit(".", 1)[0].lower().replace("_", "-")
        return stem in ("seconv", "se-cli", "secli")

    def build_command(self, sup_path: Path, language: str, out_dir: Path) -> list[str]:
        cmd = list(self._launcher())
        subs = self._settings.subtitles
        if self._is_cli_tool():
            # seconv (SubtitleEdit 5.x CLI, Spectre.Console.Cli): the format is an
            # OPTION (--format), and option values are space-separated. It takes the
            # OCR language directly (--ocr-language), so — unlike the GUI batch tool
            # — there's NO need to pre-select the language in the GUI.
            cmd += [
                str(sup_path),
                "--format", "subrip",
                "--ocr-engine", "tesseract",
                "--ocr-language", tesseract_language(language),
                "--encoding", "utf-8",
                "--output-folder", str(out_dir),
                "--overwrite",
            ]
            if subs.fix_common_errors:
                cmd.append("--fix-common-errors")
                # The deeper "Fix common OCR errors" pass (repairs glyph confusions
                # like a capital I read as "|") only runs when a dictionary folder
                # with <lang>_OCRFixReplaceList.xml is supplied.
                if subs.dictionary_folder:
                    cmd += ["--dictionary-folder", str(subs.dictionary_folder)]
            if subs.remove_text_for_hi:
                cmd.append("--remove-text-for-hi")
            # OCR emits one long single line; wrap (and balance) it into two so it
            # respects the max single-line length instead of running off-screen.
            if subs.split_long_lines:
                cmd.append("--split-long-lines")
            if subs.balance_lines:
                cmd.append("--balance-lines")
            return cmd
        # Classic GUI SubtitleEdit.exe batch mode. It needs the /convert keyword,
        # and its batch OCR does NOT accept a language flag — Tesseract's language
        # is whatever was last selected in the SubtitleEdit GUI (saved in
        # Settings.xml). So set it once in the GUI (OCR any .sup, pick the language)
        # and the batch tool reuses it. Prefer seconv to avoid this entirely.
        cmd += [
            "/convert",
            str(sup_path),
            "srt",
            "/ocrengine:tesseract",
            "/encoding:utf-8",
            f"/outputfolder:{out_dir}",
            "/overwrite",
        ]
        if subs.fix_common_errors:
            cmd.append("/fixcommonerrors")
        if subs.remove_text_for_hi:
            cmd.append("/removetextforhi")
        return cmd

    async def ocr_to_srt(self, sup_path: Path, language: str, out_dir: Path) -> Path:
        """OCR ``sup_path`` to SRT in ``out_dir``; returns the produced .srt path."""
        out_dir.mkdir(parents=True, exist_ok=True)
        await process.run(
            self.build_command(sup_path, language, out_dir),
            timeout=self._settings.subtitles.ocr_timeout_seconds or None,
        )
        srt_path = out_dir / (sup_path.stem + ".srt")
        if not srt_path.exists():
            # SubtitleEdit names the output after the input stem; surface a clear error.
            tlang = tesseract_language(language)
            gui_hint = ("" if self._is_cli_tool() else
                        " and that you've OCR'd one file in the SubtitleEdit GUI to set the "
                        "OCR language (the GUI batch tool can't take a language flag — seconv can)")
            raise FileNotFoundError(
                f"SubtitleEdit did not produce expected SRT at {srt_path}. "
                f"Ensure the Tesseract '{tlang}' language data is installed{gui_hint}."
            )
        return srt_path
