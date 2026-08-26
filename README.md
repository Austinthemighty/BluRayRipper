# BluRayRipper

Automated Blu-ray / DVD / UHD → Plex pipeline with a web dashboard and optional
multi-machine encoding.

It rips a disc with **MakeMKV**, transcodes to **H.265** (HandBrake for ≤1080p;
**ffmpeg + x265** for UHD so HDR10 / **Dolby Vision** survive), OCRs image
subtitles to **SRT sidecars** with **SubtitleEdit / seconv + Tesseract**, and
names everything for Plex (`Movie (Year).mkv`, `Show - S01E09.mkv`) from a
**TMDB** lookup with a confirm step. A shared **Postgres** gives it crash
recovery and lets a rip box hand heavy encodes off to **worker** machines.

Cross-platform: Windows, Linux, or a mix. Python 3.11+.

## How it works

1. **Scan** a disc/folder/ISO → list titles (duration, chapters, audio & sub languages).
2. **Rip** the chosen titles with MakeMKV; the disc auto-ejects when it's done.
3. **Name** them via TMDB (confirm/correct the match). The disc is **fingerprinted**,
   so re-inserting the same disc later is fully automatic.
4. **Subtitles** — extract each kept-language PGS track and OCR it to a `.srt`
   sidecar (`Movie (Year).en.srt`, `.pl.srt`, SDH/forced disambiguated).
5. **Encode** to H.265, keeping only your configured audio/subtitle **languages**.
   UHD HDR/DV titles take the ffmpeg path to preserve their metadata.
6. **Place** the finished files in your Plex library (e.g. a NAS) and clean up staging.

Everything is driven from the **web UI**; there's also a CLI for scripted runs.

## Quick setup (recommended)

Provisioning scripts install every external tool, wire up the paths, and run a
health check:

```powershell
# Windows (elevated PowerShell), from the repo root:
powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1 -Langs pol
```
```bash
# Linux (Debian/Ubuntu), from the repo root:
./scripts/setup-linux.sh --langs pol            # rip + encode + OCR
./scripts/setup-linux.sh --langs pol --no-makemkv   # encode-only worker
```

On Linux `setup-linux.sh` also installs the app itself (into `.venv/`, linked
onto PATH as `blurayripper`) and runs `check`, so all that is left is:

```bash
cp config/secrets.env.example config/secrets.env   # then add your TMDB key (+ DB URL if clustering)
blurayripper check             # confirms tools, folders, preset, languages, TMDB key
```

Installing by hand instead (Windows, macOS, or a distro without apt):

```bash
python3 -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[web,postgres,secrets]"           # add ",iso" for cover art inside .iso files
```

Use a virtualenv even for a one-box install. Ubuntu 24.04 and other PEP 668
distros mark the system Python "externally managed", so a plain
`pip install -e .` is refused outright — with or without `sudo`.

## External tools

`check` verifies all of these; the setup scripts install them.

| Tool | Purpose |
|------|---------|
| [MakeMKV](https://www.makemkv.com/) (`makemkvcon`) | Rip disc → MKV |
| [HandBrakeCLI](https://handbrake.fr/) | Transcode ≤1080p → H.265 |
| [ffmpeg](https://ffmpeg.org/) | UHD/HDR encode (x265, preserves HDR10) + grain preview |
| [dovi_tool](https://github.com/quietvoid/dovi_tool) | Preserve Dolby Vision (Profile 7 → 8.1) |
| [MKVToolNix](https://mkvtoolnix.download/) (`mkvmerge`, `mkvextract`) | Identify/extract subtitle tracks |
| [SubtitleEdit `seconv`](https://github.com/SubtitleEdit/subtitleedit-cli) + [Tesseract](https://github.com/UB-Mannheim/tesseract/wiki) | OCR PGS → SRT |

Notes:
- **seconv** (the SubtitleEdit 5.x CLI) is the recommended OCR tool — the classic
  GUI `SubtitleEdit.exe` hangs in batch mode. seconv shells out to a **system
  Tesseract on PATH** (the GUI bundles its own), so install Tesseract + your
  language packs. Point `[tools] subtitleedit` at `seconv`.
- ffmpeg/dovi_tool are only needed if you rip UHD and want to keep HDR/DV.
- Tools on PATH are auto-detected by name; otherwise set explicit paths.

## Configuration

Three layers, highest priority first:

- **`config/secrets.env`** (gitignored) — the DB URL, TMDB key, and any
  machine-specific tool paths (`BLURAYRIPPER_*`). This is where per-box secrets live.
- **`config/settings.local.json`** — the web **⚙ Settings** overlay (edit in the browser).
- **`config/settings.toml`** — tracked defaults.

Most things are editable from **⚙ Settings** in the web UI (globally or per-worker),
including staging/output folders, kept languages, encode options (UHD/HDR/DV),
the seconv OCR options, and cluster settings. Every field has inline help.

## Web UI

The dashboard is the primary interface: drives, add discs, confirm naming, live
progress, and cluster status.

```bash
blurayripper serve          # then open http://127.0.0.1:8723
```

From the UI you can: add a disc from its drive (or **+ Add by selector** for an
ISO / BDMV folder / .mkv), pick titles, search TMDB to name it and **Confirm**;
watch live encode progress; **⬆ Encode next** to jump a job to the front;
**Requeue** a stalled job; edit **Settings**; and restart/update boxes.

Bind to `0.0.0.0` (in Settings → Web) to reach it on your LAN — note there's no
login, so use it only on a trusted network.

Press **`u`** (or Ctrl+U) in the `serve` console to `git pull` + restart.

## Clustering (optional)

For distributed encoding, a **rip box** rips + preps and enqueues jobs into a
shared **Postgres**; separate **worker** machines claim and encode them. A shared
NAS holds staging + output. Postgres also gives crash recovery — a job resumes
from its last checkpoint after any box restarts.

```bash
# once, against your Postgres (set BLURAYRIPPER_DATABASE_URL in secrets.env):
blurayripper db-init

# on the rip box: enable workers in Settings, then run serve
blurayripper serve

# on each worker machine (same DB + NAS):
blurayripper worker            # Ctrl+C drains; 'u'/Ctrl+U drains + pulls + restarts
```

- **Workers panel** shows each worker's version, current job, and uptime, with
  **Drain / Restart / Update** per worker and **Drain all / Update all** for the
  lot. Every action drains first — the current encode finishes and is *placed*,
  so nothing in flight is lost. They differ in what follows: drain stops, restart
  re-execs the code already on that box, update `git pull`s first. The server has
  its own **↻ Restart** and **⬆ Update** in Settings, so you can move one box at a
  time instead of the whole cluster.
- **Workers look after themselves**: `worker_restart_after_days` restarts a worker
  once it has been up that long (hygiene for long-lived encoders), and
  `worker_auto_update` makes a worker follow the rip box — when serve reports a
  newer build, the worker drains, `git pull`s, and restarts on its own, so you
  update one box and the cluster converges. Both act **between jobs**, so a
  running encode always finishes and is placed first; neither ever downgrades.
- **Per-worker settings**: override tool paths / encode / OCR options for one
  machine in Settings (pick the worker in the scope dropdown).
- `worker_does_subs` moves subtitle OCR onto the workers; `local_encode_max_height`
  lets the rip box handle lighter (e.g. ≤1080p) encodes itself.

Optional **push notifications** (ntfy-style URL in Settings) fire for: needs-input
gates, a disc finishing its rip, and an encode completing.

**Offline alerts** — when a box that was online stops reporting for
`offline_alert_seconds` (default 5 min), you get a push, and another when it comes
back. The rip box watches the workers; each worker watches the rip box, because a
server that's down can't report itself. Set it to 0 to switch off. Needs a shared
database and a Notify URL. Two caveats worth knowing: with several workers online
you'll get one "server offline" push per worker, and if *everything* is off (a
power cut) nothing is left to send anything — that needs an external uptime
monitor pinging the web UI.

## Running it on boot

Both roles — the rip box (`serve`) and each `worker` — can start automatically and
come back after a crash or a power-off. Run the installer once per machine.

**Linux** — a real systemd service:

```bash
./scripts/install-service-linux.sh --role serve  --requires-mount /mnt/nas
./scripts/install-service-linux.sh --role worker --requires-mount /mnt/nas
```

Defaults to a *user* service plus `loginctl enable-linger`, so it starts at boot
with nobody logged in, needs no root, and can see a NAS you mount as your user
(this also suits immutable distros like SteamOS). Pass `--system` (with
`--user-name`) for a system-wide unit when the NAS is in `/etc/fstab`. Pass
`--requires-mount` for each NAS mount so systemd waits for it instead of failing
every job on a reboot race; `--print` shows the unit without installing, and
`--status` / `--uninstall` manage it. `serve` also needs its user in the `cdrom`
group. Stopping **drains**: the worker finishes and places the current encode
first (allow up to `--stop-timeout`, default 1h).

**Windows** — a scheduled task, not a service:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-task-windows.ps1 -Role serve
powershell -ExecutionPolicy Bypass -File scripts\install-task-windows.ps1 -Role worker
```

The launcher finds the CLI in `.venv\Scripts\` before falling back to `PATH`, so
it works from a scheduled task (which never activates your venv) — you don't need
`blurayripper` on the system PATH.

It starts at logon and restarts on failure (`-Status` / `-Uninstall`, or
`schtasks /run` / `/end`). It deliberately runs **in your session, unelevated**:
staging and output live on a **mapped drive (`Z:`)**, and mapped drives don't
exist in session 0 — nor under an elevated token — so a real Windows Service
would fail every job with `FileNotFoundError: Z:\`. Switch those paths to UNC
(`\\nas\share\…`) and a real service becomes possible. Pair it with VM *Start at
boot* + Windows auto sign-in (`netplwiz`) to survive a power-off.

Headless means no `u` keypress to update — use **↻ Update all** in the web UI, or
`git pull` then restart the service/task.

## CLI

```bash
blurayripper scan disc:0                 # list a disc's titles
blurayripper rip  disc:0                  # full pipeline, interactive naming
blurayripper queue disc:0 disc:1          # several discs at once, weighted scheduling
blurayripper scan file:/path/to/BDMV      # a ripped folder …
blurayripper rip  iso:/path/to/disc.iso   # … or an ISO
blurayripper check                        # health check (tools/folders/preset/langs)
blurayripper eject disc:0                 # tray control
```

A weighted encode scheduler keeps two heavy UHD encodes from running at once
(they'd just split the CPU), while letting a UHD overlap a 1080p disc or several DVDs.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tests cover the MakeMKV robot-output parser, HandBrake JSON progress, MKVToolNix
track identification, Plex naming, disc fingerprinting, the settings schema, the
job state machine (scan → rip → subs → encode → place), crash recovery, and the
cluster claim/queue logic — all without external tools or a disc.
