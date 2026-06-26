"""SubtitleEdit CLI wrapper: OCR a PGS (.sup) subtitle into .srt via Tesseract.

Cross-platform: on Windows ``SubtitleEdit.exe`` runs directly; on Linux/macOS it
runs under a ``runner`` prefix (e.g. ``mono``) or as a native ``seconv`` build.
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

    def build_command(self, sup_path: Path, language: str, out_dir: Path) -> list[str]:
        cmd = list(self._launcher())
        cmd += [
            "/convert",
            str(sup_path),
            "srt",
            "/ocrengine:tesseract",
            f"/tesseractlanguage:{tesseract_language(language)}",
            "/encoding:utf-8",
            f"/outputfolder:{out_dir}",
            "/overwrite",
        ]
        if self._settings.subtitles.fix_common_errors:
            cmd.append("/fixcommonerrors")
        if self._settings.subtitles.remove_text_for_hi:
            cmd.append("/removetextforhi")
        return cmd

    async def ocr_to_srt(self, sup_path: Path, language: str, out_dir: Path) -> Path:
        """OCR ``sup_path`` to SRT in ``out_dir``; returns the produced .srt path."""
        out_dir.mkdir(parents=True, exist_ok=True)
        await process.run(self.build_command(sup_path, language, out_dir))
        srt_path = out_dir / (sup_path.stem + ".srt")
        if not srt_path.exists():
            # SubtitleEdit names the output after the input stem; surface a clear error.
            raise FileNotFoundError(
                f"SubtitleEdit did not produce expected SRT at {srt_path}. "
                f"Check the Tesseract '{tesseract_language(language)}' language data is installed."
            )
        return srt_path
