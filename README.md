# BluRayRipper

Automated Blu-ray/DVD → Plex pipeline. Rips a disc with **MakeMKV**, transcodes
to **H.265** with **HandBrake**, OCRs image subtitles to **SRT sidecars** with
**SubtitleEdit (Tesseract)**, and names everything for Plex
(`Show - S01E09.mp4`, `Movie (Year).mp4`) using **TMDB** lookup + a confirm step.

Cross-platform (Windows / Linux / macOS). This is **Phase 1**: a working
end-to-end command-line pipeline. A local web UI is planned for Phase 2 (see
the plan in `.claude/plans/`).

## What it does

1. **Scan** a disc/folder/ISO and list its titles (duration, chapters, audio &
   subtitle languages).
2. **Rip** the titles you choose with MakeMKV.
3. **Name** them: looks up the movie/show on TMDB and lets you confirm/correct
   the episode mapping. Once confirmed, the disc is **fingerprinted** so a
   re-rip of the same disc is fully automatic.
4. **Encode** to H.265, keeping only your configured **audio languages**
   (e.g. English + Polish) with the default flagged.
5. **Subtitles**: extracts each kept-language PGS track and OCRs it to a
   `.srt` sidecar (`Show - S01E09.en.srt`, `Show - S01E09.pl.srt`).
6. **Place** finished files into your Plex library on the configured output
   folder (e.g. a NAS) and clean up the temp/staging folder.

## Requirements

Install these external tools and make sure they're on your PATH (or set their
paths in `config/settings.toml`):

| Tool | Purpose |
|------|---------|
| [MakeMKV](https://www.makemkv.com/) (`makemkvcon`) | Rip disc → MKV |
| [HandBrakeCLI](https://handbrake.fr/downloads2.php) | Transcode → H.265 |
| [MKVToolNix](https://mkvtoolnix.download/) (`mkvmerge`, `mkvextract`) | Identify/extract subtitle tracks |
| [SubtitleEdit](https://www.nikse.dk/subtitleedit) + [Tesseract](https://github.com/tesseract-ocr/tesseract) | OCR PGS → SRT |

Plus **Python 3.11+**. On Linux, SubtitleEdit runs under `mono` — set
`subtitleedit_runner = "mono"` in the config.

## Setup

```bash
python3.11 -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .

cp config/secrets.env.example config/secrets.env   # add your TMDB API key
$EDITOR config/settings.toml                        # set folders + languages
```

Key settings in `config/settings.toml`:

- `[storage] staging_dir` / `output_dir` — temp folder and final library
  (either can point at a NAS).
- `[languages] keep` / `default` — which audio/subtitle languages to keep and
  which is primary.
- `[encode] container` — `mp4` (default) or `mkv`.
- `[tools]` — leave blank to auto-detect, or set explicit paths.

## Usage

```bash
# List a disc's titles (no ripping)
blurayripper scan disc:0

# Run the full pipeline (interactive naming)
blurayripper rip disc:0

# Rip several discs at once across drives. You confirm each disc's naming up
# front, then they run concurrently. A weighted scheduler keeps two heavy UHD
# encodes from running at the same time (they'd just split the CPU), while
# letting a UHD overlap with a 1080p disc or several DVDs.
blurayripper queue disc:0 disc:1

# Other sources: a ripped BDMV folder or an ISO (handy for testing)
blurayripper scan file:/path/to/BDMV_parent
blurayripper rip  iso:/path/to/disc.iso
```

### Web UI

A browser dashboard manages the whole queue: see your drives, add discs,
confirm naming, and watch live progress — with the same smart encode
scheduling running in the background.

```bash
pip install -e ".[web]"     # one-time: installs FastAPI + uvicorn
blurayripper serve          # then open http://127.0.0.1:8723
```

In the UI: click a drive's **Add**, pick the titles, search TMDB to name it,
and **Confirm** — the job rips and encodes on its own. Load the next disc and
repeat; heavy UHD encodes are automatically kept from doubling up.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tests cover the MakeMKV robot-output parser, HandBrake JSON progress parser,
MKVToolNix track identification, Plex naming, disc fingerprinting/heuristics,
and the mapping store — all without needing the external tools or a disc.
