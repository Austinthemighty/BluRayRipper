#!/usr/bin/env python3
"""Test a single PGS (.sup) -> SRT OCR through the app's own SubtitleEdit wrapper.

This exercises the EXACT command the pipeline builds (build_command + ocr_to_srt),
so it verifies your seconv / SubtitleEdit.exe setup end-to-end — not a hand-typed
command that might drift from what the app actually runs.

Usage (run on a worker, after `git pull`):

    python scripts/test-ocr.py path\\to\\subtitle.sup
    python scripts/test-ocr.py path\\to\\subtitle.sup --language ger --out C:\\tmp

The tool path comes from your normal config (settings.toml / BLURAYRIPPER_SUBTITLEEDIT),
so it uses the same seconv you've pointed the app at.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Make `import blurayripper` work when run straight from a checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from blurayripper.config import load_settings
from blurayripper.tools.subtitleedit import SubtitleEdit, tesseract_language


async def main() -> int:
    ap = argparse.ArgumentParser(description="Test a .sup -> .srt OCR conversion.")
    ap.add_argument("sup", type=Path, help="Path to the .sup (PGS) subtitle file")
    ap.add_argument("--language", "-l", default="eng",
                    help="ISO 639-2/B subtitle language (default: eng)")
    ap.add_argument("--out", "-o", type=Path, default=None,
                    help="Output folder (default: alongside the .sup)")
    args = ap.parse_args()

    sup = args.sup.expanduser().resolve()
    if not sup.exists():
        print(f"ERROR: no such file: {sup}")
        return 2
    out_dir = (args.out or sup.parent).expanduser().resolve()

    settings = load_settings()
    tool = settings.tools.subtitleedit
    if not tool:
        print("ERROR: no SubtitleEdit/seconv path configured. Set it in settings.toml "
              "or via BLURAYRIPPER_SUBTITLEEDIT.")
        return 2

    se = SubtitleEdit(settings)
    cmd = se.build_command(sup, args.language, out_dir)
    print(f"Tool:      {tool}")
    print(f"Language:  {args.language} -> tesseract '{tesseract_language(args.language)}'")
    print(f"CLI mode:  {'seconv/se-cli' if se._is_cli_tool() else 'SubtitleEdit.exe GUI batch'}")
    print(f"Command:   {' '.join(cmd)}")
    print("-" * 60)

    try:
        srt = await se.ocr_to_srt(sup, args.language, out_dir)
    except Exception as exc:  # noqa: BLE001 — this is a diagnostic script
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return 1

    size = srt.stat().st_size
    print(f"OK: wrote {srt} ({size} bytes)")
    # Show the first few cues so you can eyeball the OCR quality.
    text = srt.read_text(encoding="utf-8", errors="replace")
    head = "\n".join(text.splitlines()[:16])
    print("-" * 60)
    print(head)
    if size < 40:
        print("-" * 60)
        print("WARNING: the .srt is nearly empty — OCR may have produced no text.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
