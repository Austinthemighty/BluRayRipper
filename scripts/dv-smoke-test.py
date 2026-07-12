#!/usr/bin/env python3
"""Smoke-test the UHD / Dolby-Vision encode path on a short clip of a real file.

Cuts a short segment, runs the app's UHD encode on it — the full Dolby Vision
RPU round-trip if the source is DV and dovi_tool is present, else HDR10 — then
re-probes the output so you can confirm the metadata survived. Minutes, not
hours, so you shake out the pipeline before a full movie.

    python scripts/dv-smoke-test.py "/Volumes/Share/.../WALL - E (2008).mkv"
    python scripts/dv-smoke-test.py <file> --seconds 90 --start 600 --out ~/dv/out.mkv

Needs ffmpeg (with libx265); for DV also dovi_tool + mkvmerge
(macOS: brew install ffmpeg dovi_tool mkvtoolnix).
"""
from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blurayripper.config import load_settings   # noqa: E402
from blurayripper.pipeline import Pipeline       # noqa: E402


class _Report:
    def on_event(self, msg):
        print(f"  >> {msg}", flush=True)

    def on_progress(self, stage, frac):
        if frac is not None:
            print(f"     {stage}  {frac * 100:5.1f}%", end="\r", flush=True)

    def checkpoint(self):
        pass


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--seconds", type=int, default=90, help="clip length (default 90s)")
    ap.add_argument("--start", type=int, default=600, help="clip start offset (default 600s)")
    ap.add_argument("--out", default=None, help="output path (default: temp dir)")
    args = ap.parse_args()

    settings = load_settings()
    settings.encode.ffmpeg_uhd = True
    settings.encode.dovi = True
    if not settings.tools.ffmpeg:
        return _fail("ffmpeg not found — brew install ffmpeg (or set BLURAYRIPPER_FFMPEG)")

    pipe = Pipeline(settings)
    workdir = (Path(args.out).parent if args.out else Path(tempfile.gettempdir()) / "dv_test")
    workdir.mkdir(parents=True, exist_ok=True)
    clip = workdir / "clip.mkv"
    out = Path(args.out) if args.out else workdir / "OUTPUT_dv_test.mkv"

    print(f"1) Cutting a {args.seconds}s clip at {args.start}s from {Path(args.source).name}…")
    subprocess.run(
        [settings.tools.ffmpeg, "-y", "-ss", str(args.start), "-i", args.source,
         "-t", str(args.seconds), "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", str(clip)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    hdr, _dur = await pipe.ffmpeg.probe_video(clip)
    print(f"2) Source: {'DV profile ' + str(hdr.dv_profile) if hdr.is_dolby_vision else 'HDR10' if hdr.is_hdr else 'SDR'}"
          f"  transfer={hdr.transfer}  master-display={'yes' if hdr.master_display else 'no'}")
    if hdr.is_dolby_vision:
        if not settings.tools.dovitool:
            print("   ! dovi_tool missing — HDR10 only. brew install dovi_tool")
        if not settings.tools.mkvmerge:
            print("   ! mkvmerge missing — HDR10 only. brew install mkvtoolnix")

    print("3) Encoding…")
    await pipe._encode_uhd(clip, out, tune_grain=False, encode_dir=workdir, report=_Report())

    print(f"\n\n4) Output -> {out}")
    ohdr, _ = await pipe.ffmpeg.probe_video(out)
    label = ("Dolby Vision profile %s" % ohdr.dv_profile if ohdr.is_dolby_vision
             else "HDR10" if ohdr.is_hdr else "SDR (!)")
    print(f"   Verify: {label}  transfer={ohdr.transfer}  "
          f"master-display={'yes' if ohdr.master_display else 'no'}")
    if hdr.is_dolby_vision and ohdr.dv_profile:
        print(f"   ✅ Dolby Vision preserved (source profile {hdr.dv_profile} -> "
              f"output profile {ohdr.dv_profile}; 8 == 8.1).")
    elif hdr.is_dolby_vision:
        print("   ⚠ Source was DV but output has no DV — check the log above.")
    return 0


def _fail(msg: str) -> int:
    print(f"ERROR: {msg}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
