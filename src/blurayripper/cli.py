"""Phase-1 interactive CLI entrypoint.

Usage:
    blurayripper rip <source> [--config DIR] [--name JOBNAME]
    blurayripper scan <source> [--config DIR]

``source`` is a makemkvcon selector: disc:0, dev:/dev/sr0, file:/path/to/BDMV
parent folder, or iso:/path/to.iso.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from .config import Settings, ToolNotFoundError, load_settings
from .models import DiscInfo, EpisodeAssignment, MediaKind
from .pipeline import Pipeline
from .scheduler import EncodeScheduler
from .services.metadata import MetadataError, TMDBClient
from .tools import discinfo
from .tools.makemkv import MakeMKV

console = Console()


class CliCallbacks:
    """Interactive implementation of the pipeline's human-gate hooks."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.tmdb = TMDBClient(settings.tmdb_api_key, settings.metadata_language)
        self._progress: Progress | None = None
        self._task = None

    # --- logging / progress ---
    def _ensure_progress(self) -> Progress:
        if self._progress is None:
            self._progress = Progress(
                SpinnerColumn(),
                TextColumn("[bold cyan]{task.description}"),
                BarColumn(bar_width=None),
                TaskProgressColumn(),
                console=console,
                transient=False,
            )
            self._progress.start()
            self._task = self._progress.add_task("starting…", total=1000)
        return self._progress

    def on_event(self, message: str) -> None:
        # Route log lines through the progress console so they appear above the
        # live bar instead of corrupting it.
        if self._progress is not None:
            self._progress.console.print(f"[dim]·[/dim] {message}")
        else:
            console.print(f"[dim]·[/dim] {message}")

    def on_progress(self, stage: str, fraction: float | None) -> None:
        progress = self._ensure_progress()
        if fraction is None:
            progress.update(self._task, description=stage)
        else:
            progress.update(self._task, description=stage, completed=int(fraction * 1000))

    def close(self) -> None:
        if self._progress is not None:
            self._progress.stop()
            self._progress = None

    # --- title selection ---
    def select_titles(self, disc: DiscInfo) -> list[int]:
        _print_titles(disc)
        kind = discinfo.guess_kind(disc)
        if kind is MediaKind.MOVIE:
            feature = discinfo.main_feature(disc)
            suggested = [feature.title_id] if feature else []
        else:
            suggested = [t.title_id for t in discinfo.episode_candidate_titles(disc)]

        console.print(
            f"[bold]Detected:[/bold] {kind.value.upper()} "
            f"— suggested titles: {suggested or 'none'}"
        )
        raw = Prompt.ask(
            "Title ids to rip (comma-separated)",
            default=",".join(str(i) for i in suggested),
        )
        return [int(x) for x in raw.replace(" ", "").split(",") if x != ""]

    # --- naming confirm ---
    async def confirm_mapping(
        self, disc: DiscInfo, selected_title_ids: list[int]
    ) -> dict[int, EpisodeAssignment]:
        default_kind = discinfo.guess_kind(disc)
        is_movie = Confirm.ask(
            "Is this a MOVIE? (No = TV show)", default=default_kind is MediaKind.MOVIE
        )
        if is_movie:
            return await self._confirm_movie(disc, selected_title_ids)
        return await self._confirm_tv(disc, selected_title_ids)

    async def _confirm_movie(
        self, disc: DiscInfo, selected_title_ids: list[int]
    ) -> dict[int, EpisodeAssignment]:
        default_title = (disc.disc_name or "").replace("_", " ").title().strip()
        title, year = await self._pick_movie(default_title)
        # Movie discs usually have one feature; assign each selected title to it.
        return {
            tid: EpisodeAssignment(
                title_id=tid, kind=MediaKind.MOVIE, show_or_movie_title=title, year=year
            )
            for tid in selected_title_ids
        }

    async def _pick_movie(self, default_title: str) -> tuple[str, int | None]:
        query = Prompt.ask("Movie title", default=default_title)
        if self.tmdb.configured:
            try:
                candidates = await self.tmdb.search_movie(query)
            except MetadataError as exc:
                console.print(f"[yellow]{exc}[/yellow]")
                candidates = []
            if candidates:
                table = Table("#", "Title", "Year")
                for i, c in enumerate(candidates[:10]):
                    table.add_row(str(i), c.title, str(c.year or "?"))
                console.print(table)
                idx = IntPrompt.ask("Pick #", default=0)
                if 0 <= idx < len(candidates):
                    c = candidates[idx]
                    return c.title, c.year
        year_raw = Prompt.ask("Year (blank if unknown)", default="")
        return query, (int(year_raw) if year_raw.strip().isdigit() else None)

    async def _confirm_tv(
        self, disc: DiscInfo, selected_title_ids: list[int]
    ) -> dict[int, EpisodeAssignment]:
        default_show = (disc.disc_name or "").replace("_", " ").title().strip()
        show, year, tv_id = await self._pick_show(default_show)
        season = IntPrompt.ask("Season number", default=1)

        episodes = []
        if tv_id is not None and self.tmdb.configured:
            try:
                episodes = await self.tmdb.get_season(tv_id, season)
            except MetadataError as exc:
                console.print(f"[yellow]{exc}[/yellow]")
        if episodes:
            table = Table("Ep", "Title")
            for e in episodes:
                table.add_row(str(e.episode_number), e.name)
            console.print(table)

        start_ep = IntPrompt.ask("Starting episode number for the first selected title", default=1)
        assignments: dict[int, EpisodeAssignment] = {}
        ep_by_num = {e.episode_number: e for e in episodes}
        for offset, tid in enumerate(selected_title_ids):
            default_ep = start_ep + offset
            ep_num = IntPrompt.ask(f"Title {tid} -> episode #", default=default_ep)
            ep_title = ep_by_num.get(ep_num).name if ep_num in ep_by_num else None
            assignments[tid] = EpisodeAssignment(
                title_id=tid,
                kind=MediaKind.TV,
                show_or_movie_title=show,
                year=year,
                season=season,
                episode=ep_num,
                episode_title=ep_title,
            )
        return assignments

    async def _pick_show(self, default_show: str) -> tuple[str, int | None, int | None]:
        query = Prompt.ask("Show name", default=default_show)
        if self.tmdb.configured:
            try:
                candidates = await self.tmdb.search_tv(query)
            except MetadataError as exc:
                console.print(f"[yellow]{exc}[/yellow]")
                candidates = []
            if candidates:
                table = Table("#", "Name", "Year")
                for i, c in enumerate(candidates[:10]):
                    table.add_row(str(i), c.name, str(c.year or "?"))
                console.print(table)
                idx = IntPrompt.ask("Pick #", default=0)
                if 0 <= idx < len(candidates):
                    c = candidates[idx]
                    return c.name, c.year, c.tmdb_id
        year_raw = Prompt.ask("Year (blank if unknown)", default="")
        return query, (int(year_raw) if year_raw.strip().isdigit() else None), None


def _print_titles(disc: DiscInfo) -> None:
    table = Table("ID", "Duration", "Chapters", "Size", "Playlist", "Audio", "Subs")
    for t in discinfo.ordered_titles(disc):
        table.add_row(
            str(t.title_id),
            t.duration_hhmmss,
            str(t.chapter_count),
            f"{t.size_bytes / 1e9:.1f} GB" if t.size_bytes else "?",
            t.source_filename or "?",
            ",".join(t.audio_languages) or "-",
            ",".join(t.subtitle_languages) or "-",
        )
    console.print(table)


async def _cmd_scan(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    messages: list[str] = []
    disc = await MakeMKV(settings).scan(args.source, on_message=messages.append)
    console.print(f"[bold]Disc:[/bold] {disc.disc_name or 'unknown'}")
    _print_titles(disc)
    if not disc.titles:
        console.print("[yellow]No titles found. MakeMKV reported:[/yellow]")
        for m in messages:
            console.print(f"  [dim]{m}[/dim]")
        console.print(
            "[yellow]Common causes: no/again-unreadable disc, wrong drive index "
            "(try `scan disc:99999` to list drives), MakeMKV registration expired, "
            "or a UHD disc the drive can't open.[/yellow]"
        )
    console.print(f"[dim]Fingerprint: {discinfo.disc_fingerprint(disc)}[/dim]")
    return 0


async def _cmd_rip(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    callbacks = CliCallbacks(settings)
    if not settings.tmdb_api_key:
        console.print("[yellow]No TMDB key configured; naming will be manual.[/yellow]")
    pipeline = Pipeline(settings, callbacks)
    try:
        job = await pipeline.run(args.source, job_name=args.name)
    finally:
        callbacks.close()
    console.print(f"[bold green]Finished[/bold green] with {len(job.results)} file(s).")
    for r in job.results:
        console.print(f"  {r.video_path}")
    return 0


async def _cmd_check(args: argparse.Namespace) -> int:
    """Pre-flight: verify tools, folders, preset, languages, and TMDB key."""
    import json
    from pathlib import Path

    settings = load_settings(args.config)
    ok = True

    # External tools. Verify each configured path actually exists on THIS machine
    # (a configured path is otherwise trusted blindly, so a shared config can name
    # a tool that isn't installed here — which then fails only mid-encode).
    import shutil as _shutil

    def _on_disk(p: str | None) -> bool:
        return bool(p) and _shutil.which(p) is not None

    # The subtitleedit path can be the classic GUI SubtitleEdit.exe or a seconv /
    # se-cli build; they need different command syntax, so show which mode is live.
    from .tools.subtitleedit import SubtitleEdit, tesseract_language

    uses_cli_ocr = bool(settings.tools.subtitleedit) and SubtitleEdit(settings)._is_cli_tool()

    tool_table = Table("Tool", "Status", title="External tools")
    for key in ("makemkvcon", "handbrakecli", "mkvmerge", "mkvextract", "subtitleedit"):
        path = getattr(settings.tools, key)
        if _on_disk(path):
            note = ""
            if key == "subtitleedit":
                note = (" [dim](seconv/se-cli CLI mode)[/dim]" if uses_cli_ocr
                        else " [dim](GUI batch mode — bundled Tesseract, GUI-set language)[/dim]")
            tool_table.add_row(key, f"[green]{path}[/green]{note}")
        elif path:
            tool_table.add_row(
                key, f"[red]{path} — configured but NOT found on this machine[/red]")
            ok = False
        else:
            tool_table.add_row(key, "[red]MISSING — install or set in settings.toml[/red]")
            ok = False
    # Java, but only on a box that can actually rip. MakeMKV shells out to a JRE
    # for BD-Java discs (menu-driven Blu-rays, and most BD+ titles); without one
    # it does not fail cleanly, it rips a subset of titles and logs "This disc
    # requires Java runtime (JRE)" partway down a long log. An encode-only worker
    # never touches a disc, so requiring it there would be noise.
    if settings.tools.makemkvcon:
        import os
        import subprocess

        java = _shutil.which("java") or ""
        if not java and os.environ.get("JAVA_HOME"):
            cand = Path(os.environ["JAVA_HOME"]) / "bin" / "java"
            java = str(cand) if cand.exists() else ""
        if java:
            ver = ""
            try:  # java prints its version to stderr, and has for decades
                out = subprocess.run([java, "-version"], capture_output=True,
                                     text=True, timeout=15)
                ver = (out.stderr or out.stdout).splitlines()[0].strip()
            except (OSError, subprocess.SubprocessError, IndexError):
                pass
            tool_table.add_row("java (BD-Java discs)",
                               f"[green]{ver or java}[/green]")
        else:
            tool_table.add_row(
                "java (BD-Java discs)",
                "[red]MISSING — MakeMKV needs a JRE for BD-Java/BD+ discs. Without "
                "it those discs rip incomplete rather than failing outright.\n"
                "  Debian/Ubuntu: sudo apt-get install -y default-jre\n"
                "  Windows: install a JRE (e.g. Adoptium Temurin) and make sure "
                "java is on PATH[/red]")
            ok = False

    runner = settings.tools.subtitleedit_runner
    if not runner:
        tool_table.add_row("subtitleedit_runner", "[dim](none — runs directly)[/dim]")
    elif _on_disk(runner):  # a bare command (e.g. "mono") resolves via PATH
        tool_table.add_row("subtitleedit_runner", f"[green]{runner}[/green]")
    else:
        tool_table.add_row(
            "subtitleedit_runner",
            f"[red]{runner} — configured but NOT found on this machine[/red]")
        ok = False

    # A configured path proves the file is THERE, not that it runs. seconv is a
    # framework-dependent .NET build, so a box whose runtime moved on (built
    # against 8.0, only 10.x installed) passes every check above and then fails
    # at OCR time, mid-job — the encode continues and the file is placed with no
    # subtitles, so the job goes green and the loss is silent.
    se = settings.tools.subtitleedit
    if se and _on_disk(se):
        import subprocess

        try:
            probe = subprocess.run([runner, se] if runner else [se],
                                   capture_output=True, text=True, timeout=60)
            blob = (probe.stderr or "") + (probe.stdout or "")
        except (OSError, subprocess.SubprocessError):
            blob = ""
        if "must install or update .NET" in blob or "app-launch-failed" in blob:
            want = ""
            for marker in ("version '", "framework_version="):
                if marker in blob:
                    want = blob.split(marker, 1)[1].split("'")[0].split("&")[0]
                    break
            tool_table.add_row(
                "subtitleedit (runs?)",
                f"[red]NO — built against .NET {want or '(older)'} but this box "
                "has a different runtime, so subtitle OCR fails mid-job and the "
                "video is placed with no subtitles.\n"
                "  Fix without reinstalling anything:\n"
                "    sudo sed -i '2i export DOTNET_ROLL_FORWARD=LatestMajor' "
                f"{se}[/red]")
            ok = False

    # Which seconv this is. Two different programs use the name, and the one
    # setup-linux.sh builds does NOcr with a single Latin database — it has no
    # Tesseract and no per-track language, so installed language data goes unused.
    # That is a quality difference you would otherwise only hear about from the
    # subtitles themselves.
    if se and _on_disk(se):
        from .tools.subtitleedit import SubtitleEdit

        se_help = await SubtitleEdit(settings).help_text()
        if se_help and "--ocr-engine" not in se_help and "--format" not in se_help:
            _, dropped = SubtitleEdit(settings)._classic_command(
                Path("x.sup"), Path("."), se_help)
            note = ("[yellow]subtitleedit-cli build: OCR is NOcr with the "
                    f"'{'/ocrdb' if '/ocrdb:' in se_help else 'built-in'}' database, "
                    "NOT Tesseract — it has no per-track language option, so the "
                    "tesseract language data above is not used by it. Accented "
                    "languages suffer most.")
            if dropped:
                note += f" Also unavailable in this build: {', '.join(dropped)}."
            tool_table.add_row("subtitleedit (flavour)", note + "[/yellow]")
        elif se_help:
            tool_table.add_row(
                "subtitleedit (flavour)",
                "[green]SubtitleEdit 5.x CLI — Tesseract with per-track "
                "language[/green]")

    def _optional_row(label: str, path: str | None, absent_msg: str) -> None:
        if _on_disk(path):
            tool_table.add_row(label, f"[green]{path}[/green]")
        elif path:
            tool_table.add_row(
                label, f"[yellow]{path} — configured but NOT found on this machine[/yellow]")
        else:
            tool_table.add_row(label, f"[yellow]{absent_msg}[/yellow]")

    _optional_row("ffmpeg [dim](optional)[/dim]", settings.tools.ffmpeg,
                  "not found — grain preview + UHD/HDR encoding disabled")
    _optional_row("dovitool [dim](optional)[/dim]", settings.tools.dovitool,
                  "not found — Dolby Vision falls back to HDR10")
    # MSVC runtime: dovi_tool.exe / mkvtoolnix are MSVC-linked and won't launch
    # without vcruntime140.dll. Only meaningful (and shown) on Windows.
    vcr_level, vcr_msg = _vcruntime_status()
    if vcr_level != "na":
        _vcr_color = {"ok": "green", "warn": "yellow", "missing": "red"}.get(vcr_level, "dim")
        tool_table.add_row(
            "vcruntime140.dll [dim](Windows)[/dim]",
            f"[{_vcr_color}]{vcr_msg}[/{_vcr_color}]",
        )

    # Tesseract OCR engine. seconv / se-cli shell out to a SYSTEM tesseract on
    # PATH (the GUI SubtitleEdit.exe bundles its own), so a seconv worker without
    # Tesseract fails every subtitle OCR. Missing is fatal for a CLI build, just
    # a note for the bundled GUI. Also cross-check the languages we keep against
    # the installed Tesseract language packs.
    tess_exe, tess_ver, tess_langs = _tesseract_status()
    if tess_exe:
        tool_table.add_row("tesseract", f"[green]{tess_ver or tess_exe}[/green]")
        want = {tesseract_language(l) for l in settings.languages.keep}
        missing_langs = sorted(w for w in want if w not in tess_langs) if tess_langs else []
        if tess_langs:
            tool_table.add_row(
                "tesseract langs",
                (f"[green]{', '.join(sorted(tess_langs))}[/green]" if not missing_langs
                 else f"[yellow]installed: {', '.join(sorted(tess_langs))} — "
                      f"MISSING for kept languages: {', '.join(missing_langs)}[/yellow]"))
    elif uses_cli_ocr:
        tool_table.add_row(
            "tesseract",
            "[red]NOT found on PATH — seconv/se-cli OCR needs it. Install from "
            "github.com/UB-Mannheim/tesseract (check 'Add to PATH')[/red]")
        ok = False
    else:
        tool_table.add_row(
            "tesseract [dim](optional)[/dim]",
            "[dim]not on PATH — GUI SubtitleEdit.exe uses its bundled copy[/dim]")

    # OCR-fix dictionary folder. In seconv CLI mode the deeper "Fix common OCR
    # errors" pass (repairs glyph confusions like a capital I read as "|") only
    # runs when --fix-common-errors is paired with a --dictionary-folder holding
    # <lang>_OCRFixReplaceList.xml. Only meaningful for a seconv build with the
    # fix pass enabled; surface whether it's set and actually present.
    if uses_cli_ocr and settings.subtitles.fix_common_errors:
        from pathlib import Path as _Path

        dict_dir = settings.subtitles.dictionary_folder
        if dict_dir and _Path(dict_dir).is_dir():
            tool_table.add_row("OCR-fix dictionary", f"[green]{dict_dir}[/green]")
        elif dict_dir:
            tool_table.add_row(
                "OCR-fix dictionary",
                f"[yellow]{dict_dir} — configured but NOT a folder on this machine[/yellow]")
        else:
            tool_table.add_row(
                "OCR-fix dictionary [dim](optional)[/dim]",
                r"[yellow]unset — OCR glyph fixes (e.g. '|'->'I') disabled. Point "
                r"\[subtitles] dictionary_folder at SubtitleEdit's Dictionaries folder[/yellow]")
    console.print(tool_table)

    # Folders (writability). Every configured folder path is probed by actually
    # creating + writing a temp file, so a typo'd or unreachable path (e.g. a NAS
    # that isn't mapped) shows NO rather than failing only mid-job.
    folder_table = Table("Folder", "Path", "Writable", title="Storage")
    required_folders = [
        ("staging", settings.storage.staging_dir),
        ("output", settings.storage.output_dir),
    ]
    for label, path in required_folders:
        writable = _check_writable(path)
        folder_table.add_row(label, str(path), "[green]yes[/green]" if writable else "[red]NO[/red]")
        ok = ok and writable
    # Optional, only if the user set it (subtitle upload export).
    export_dir = settings.subtitles.upload_export_dir
    if export_dir:
        writable = _check_writable(export_dir)
        folder_table.add_row(
            "subtitle export [dim](optional)[/dim]", str(export_dir),
            "[green]yes[/green]" if writable else "[yellow]NO — configured but unreachable[/yellow]")
    console.print(folder_table)

    # Local config layers. All of these are gitignored, so `git pull` never
    # restores them and moving to a new machine silently drops every value back
    # to the tracked defaults. That reads as "the app changed my settings" rather
    # than as a missing file, so name the files and say what each one carries.
    cfg_dir = settings.config_dir
    layer_table = Table("File", "Present", "Carries",
                        title="Local config (gitignored — git pull will NOT restore these)")
    layers = [
        ("settings.local.json", "everything saved from the web Settings page"),
        ("presets.local.json", "HandBrake presets edited in the web UI"),
        ("secrets.env", "TMDB / OpenSubtitles keys, database URL"),
        ("boxsets.local.csv", "box-set naming plan"),
    ]
    # Everything except secrets.env is now a CACHE of Postgres, refreshed at
    # serve startup — absent is fine there, it just means "not pulled yet".
    shared = {"settings.local.json", "presets.local.json", "boxsets.local.csv"}
    clustered = bool(settings.database_url)
    for fname, carries in layers:
        here = (cfg_dir / fname).exists()
        if here:
            mark = "[green]yes[/green]"
        elif clustered and fname in shared:
            mark = "[dim]no — from cluster[/dim]"
        else:
            mark = "[dim]no[/dim]"
        layer_table.add_row(fname, mark, carries)
    console.print(layer_table)
    if not (cfg_dir / "settings.local.json").exists() and not clustered:
        console.print(
            "[yellow]No settings.local.json and no database — every setting below "
            "comes from the tracked settings.toml, and anything tuned in the web "
            "UI stays on this box only.[/yellow]")
    if clustered:
        console.print(
            "[dim]Shared config is stored in Postgres and pulled at serve startup; "
            "the local files above are caches. secrets.env is the exception — it "
            "holds the database URL itself, so it must exist on every box.[/dim]")

    # Preset file <-> name <-> container
    pf: Path = settings.encode.preset_file
    if not pf.exists():
        console.print(f"[red]Preset file not found: {pf}[/red]")
        ok = False
    else:
        try:
            presets = json.loads(pf.read_text(encoding="utf-8")).get("PresetList", [])
            match = next((p for p in presets if p.get("PresetName") == settings.encode.preset_name), None)
            if match is None:
                names = ", ".join(p.get("PresetName", "?") for p in presets)
                console.print(
                    f"[red]Preset name '{settings.encode.preset_name}' not found in {pf.name}. "
                    f"Available: {names}[/red]"
                )
                ok = False
            else:
                fmt = match.get("FileFormat", "")
                want = f"av_{settings.encode.container}"
                if fmt != want:
                    console.print(
                        f"[red]Container mismatch: settings container='{settings.encode.container}' "
                        f"but preset FileFormat='{fmt}'. Set container to match.[/red]"
                    )
                    ok = False
                else:
                    console.print(
                        f"[green]Preset OK:[/green] '{settings.encode.preset_name}' "
                        f"({fmt}), encoder={match.get('VideoEncoder')}, RF={match.get('VideoQualitySlider')}"
                    )
        except json.JSONDecodeError as exc:
            console.print(f"[red]Preset file is not valid JSON: {exc}[/red]")
            ok = False

    # UHD / HDR / Dolby Vision encoding path
    enc = settings.encode
    if enc.ffmpeg_uhd:
        if not settings.tools.ffmpeg:
            console.print("[red]ffmpeg_uhd is on but ffmpeg not found — UHD titles can't encode.[/red]")
            ok = False
        else:
            console.print(
                f"[green]UHD via ffmpeg:[/green] titles >= {enc.uhd_min_height}p -> libx265 "
                f"(crf {enc.ffmpeg_crf}, {enc.ffmpeg_preset}, audio={enc.ffmpeg_audio})")
            if enc.ffmpeg_audio == "copy" and enc.container != "mkv":
                console.print(
                    f"[yellow]ffmpeg_audio='copy' needs container='mkv' (yours is "
                    f"'{enc.container}') — lossless audio (TrueHD/DTS-HD) won't fit mp4.[/yellow]")
        if enc.dovi:
            if not _on_disk(settings.tools.dovitool):
                console.print("[yellow]dovi is on but dovi_tool not found on this machine — "
                              "DV falls back to HDR10.[/yellow]")
            elif not _on_disk(settings.tools.mkvmerge):
                console.print("[yellow]dovi is on but mkvmerge not found — DV falls back to HDR10.[/yellow]")
            elif vcr_level == "missing":
                console.print(
                    "[red]dovi is on and dovi_tool is present, but vcruntime140.dll is MISSING — "
                    "dovi_tool.exe won't launch, so DV falls back to HDR10. Install the Microsoft "
                    "Visual C++ x64 Redistributable (aka.ms/vs/17/release/vc_redist.x64.exe).[/red]")
                ok = False
            else:
                console.print("[green]Dolby Vision:[/green] dovi_tool + mkvmerge present — DV preserved (profile 8.1).")
                if vcr_level == "warn":
                    console.print(f"[yellow]Note: {vcr_msg}[/yellow]")
    elif enc.dovi:
        console.print("[yellow]dovi is on but ffmpeg_uhd is off — DV needs ffmpeg_uhd=true; no effect.[/yellow]")

    # Languages + TMDB
    console.print(
        f"Languages: keep={settings.languages.keep}, default={settings.languages.default}, "
        f"audio tracks={settings.audio.tracks}"
    )
    if settings.tmdb_api_key:
        console.print("[green]TMDB key present[/green] (automatic naming lookups enabled).")
    else:
        console.print("[yellow]No TMDB key — naming will be fully manual.[/yellow]")

    console.print(
        "\n[bold green]All checks passed.[/bold green]" if ok
        else "\n[bold red]Some checks failed — fix the items above before ripping.[/bold red]"
    )
    return 0 if ok else 1


async def _cmd_verify_subs(args: argparse.Namespace) -> int:
    """Audit finished jobs: a movie whose disc had a kept-language subtitle track
    should have a .srt sidecar. Flags the ones that don't."""
    from pathlib import Path

    from .persistence import make_store
    from .subtitle_audit import audit_done_jobs

    settings = load_settings(args.config)
    store = make_store(settings.database_url)
    try:
        store.init()
        records = store.load_all()
    except Exception as exc:
        console.print(f"[red]Can't read the job store: {exc}[/red]")
        return 2

    def list_sidecars(video):
        p = Path(video)
        if not p.parent.exists():
            return None  # folder unreachable (e.g. the NAS isn't mapped here)
        return sorted(p.parent.glob(p.stem + ".*.srt"))

    results = audit_done_jobs(records, settings.languages.is_kept, list_sidecars)
    missing = [r for r in results if r["status"] == "missing"]
    unreachable = [r for r in results if r["status"] == "unreachable"]
    ok_n = sum(1 for r in results if r["status"] == "ok")

    to_show = results if args.all else missing
    if to_show:
        table = Table("Job", "Name", "SRTs", "Disc subs", "Status",
                      title="Subtitle audit (finished jobs)")
        _color = {"missing": "red", "unreachable": "yellow", "ok": "green"}
        for r in sorted(to_show, key=lambda r: r["id"]):
            c = _color[r["status"]]
            table.add_row(str(r["id"]), r["name"][:48], str(r["srts"]),
                          "yes" if r["expected"] else "no", f"[{c}]{r['status']}[/{c}]")
        console.print(table)

    console.print(
        f"\n[bold]{ok_n} OK[/bold] · [red]{len(missing)} missing subtitles[/red] · "
        f"[yellow]{len(unreachable)} unverifiable[/yellow]  (of {len(results)} finished jobs)")
    if unreachable:
        console.print("[yellow]'unverifiable' = the output folder wasn't reachable (run this on the "
                      "box that has the library/NAS mapped).[/yellow]")
    if missing:
        console.print("[yellow]'missing' = the disc had a kept-language subtitle track but the movie "
                      "has no .srt sidecar. Requeue those jobs in the web UI to re-extract + OCR "
                      "('Re-OCR all' only helps when the .sup subs were cached).[/yellow]")
    return 1 if missing else 0


def _vcruntime_status() -> tuple[str, str]:
    """Check the MSVC runtime that dovi_tool.exe / mkvtoolnix need on Windows.

    Returns (level, message) where level is one of:
      "na"      not Windows — nothing to check
      "ok"      vcruntime140.dll and vcruntime140_1.dll both present
      "warn"    vcruntime140.dll present but vcruntime140_1.dll missing
                (older dovi_tool builds run; newer MSVC builds need _1)
      "missing" vcruntime140.dll not found — MSVC-linked tools won't launch
    """
    import sys

    if sys.platform != "win32":
        return "na", "n/a (not Windows)"

    import ctypes
    import os
    from pathlib import Path

    def _locate(name: str) -> str | None:
        try:
            ctypes.WinDLL(name)  # searches the app dir, System32, and PATH
            return name
        except OSError:
            root = os.environ.get("SystemRoot", r"C:\Windows")
            for sub in ("System32", "SysWOW64"):
                cand = Path(root) / sub / name
                if cand.exists():
                    return str(cand)
            return None

    core = _locate("vcruntime140.dll")
    if not core:
        return "missing", (
            "MISSING — install the Microsoft Visual C++ x64 Redistributable "
            "(aka.ms/vs/17/release/vc_redist.x64.exe)"
        )
    if not _locate("vcruntime140_1.dll"):
        return "warn", f"{core} — but vcruntime140_1.dll missing (newer dovi_tool builds need it)"
    return "ok", core


def _tesseract_status() -> tuple[str, str | None, set[str]]:
    """Probe for the Tesseract OCR binary on PATH and its installed languages.

    Unlike the classic SubtitleEdit.exe (which bundles its own Tesseract), the
    seconv / se-cli builds shell out to a SYSTEM ``tesseract`` on PATH — so a
    worker with seconv but no Tesseract fails every OCR mid-job. Surface it here.

    Returns (path_or_None, version_or_None, installed_lang_codes).
    """
    import shutil
    import subprocess

    exe = shutil.which("tesseract")
    if not exe:
        return None, None, set()
    version: str | None = None
    langs: set[str] = set()
    try:
        out = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=15)
        first = (out.stdout or out.stderr or "").splitlines()
        if first:
            version = first[0].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        out = subprocess.run(
            [exe, "--list-langs"], capture_output=True, text=True, timeout=15)
        # First line is a header ("List of available languages ..."); the rest
        # are one code per line.
        for line in (out.stdout or "").splitlines()[1:]:
            code = line.strip()
            if code:
                langs.add(code)
    except (OSError, subprocess.SubprocessError):
        pass
    return exe, version, langs


def _check_writable(path) -> bool:
    from pathlib import Path

    p = Path(path)
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".blurayripper_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


class _QueueDisplay:
    """Multi-lane live progress: one bar per concurrent job."""

    def __init__(self) -> None:
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold magenta]{task.fields[lane]}"),
            TextColumn("{task.description}"),
            BarColumn(bar_width=30),
            TaskProgressColumn(),
            console=console,
        )

    def start(self) -> None:
        self.progress.start()

    def stop(self) -> None:
        self.progress.stop()

    def lane(self, name: str) -> "_LaneReporter":
        task = self.progress.add_task("queued…", lane=name, total=1000)
        return _LaneReporter(self, task, name)

    def update(self, task, stage: str, fraction: float | None) -> None:
        if fraction is None:
            self.progress.update(task, description=stage)
        else:
            self.progress.update(task, description=stage, completed=int(fraction * 1000))

    def event(self, name: str, message: str) -> None:
        self.progress.console.print(f"[dim][{name}][/dim] {message}")


class _LaneReporter:
    """Per-job reporter (on_event/on_progress) routed to one display lane."""

    def __init__(self, display: _QueueDisplay, task, name: str) -> None:
        self._display = display
        self._task = task
        self.name = name

    def on_event(self, message: str) -> None:
        self._display.event(self.name, message)

    def on_progress(self, stage: str, fraction: float | None) -> None:
        self._display.update(self._task, stage, fraction)


async def _cmd_queue(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    interactive = CliCallbacks(settings)
    if not settings.tmdb_api_key:
        console.print("[yellow]No TMDB key configured; naming will be manual.[/yellow]")
    pipeline = Pipeline(settings, interactive)

    # 1) Interactive setup for each disc, one at a time (scan + title/naming).
    prepared: list = []
    for source in args.sources:
        console.rule(f"Setup: {source}")
        try:
            job = await pipeline.prepare(source)
            prepared.append(job)
        except Exception as exc:
            console.print(f"[red]Skipping {source}: {type(exc).__name__}: {exc}[/red]")
    if not prepared:
        console.print("[red]Nothing to do.[/red]")
        return 1

    # 2) Run all jobs concurrently; the shared scheduler gates encodes by weight.
    console.rule("Running queue")
    console.print(
        f"[dim]Encode budget: {settings.queue.encode_capacity} units "
        f"(UHD=2, others=1) — heavy encodes won't double up.[/dim]"
    )
    scheduler = EncodeScheduler(settings.queue.encode_capacity)
    display = _QueueDisplay()
    display.start()

    async def run_one(job) -> object | None:
        disc_name = job.disc.disc_name if job.disc else None
        reporter = display.lane((disc_name or job.source)[:24])
        try:
            return await pipeline.execute(job, scheduler, reporter)
        except Exception:
            return None  # already reported on the lane

    try:
        results = await asyncio.gather(*(run_one(j) for j in prepared))
    finally:
        display.stop()

    done = [j for j in results if j is not None]
    console.print(f"[bold green]Queue finished[/bold green] — {len(done)}/{len(prepared)} succeeded.")
    for j in done:
        for r in j.results:
            console.print(f"  {r.video_path}")
    return 0 if len(done) == len(prepared) else 1


def _is_update_key(ch: str) -> bool:
    """Ctrl+U (0x15) — or a plain 'u' — requests a drain-then-update-then-restart."""
    return ch in ("\x15", "u", "U")


class _KeyPress:
    """Read single keystrokes from a TTY on a daemon thread and invoke a callback.

    Cross-platform: msvcrt on Windows, cbreak-mode termios on POSIX. A no-op when
    stdin isn't a TTY (e.g. the worker runs as a service / with redirected input),
    so it never interferes with headless deployments. Ctrl+C is left to SIGINT.
    """

    def __init__(self, on_key) -> None:
        self._on_key = on_key
        self._stop = False
        self._fd = None
        self._old = None

    def start(self) -> None:
        if not (sys.stdin and sys.stdin.isatty()):
            return
        import threading
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        if sys.platform == "win32":
            import msvcrt
            while not self._stop:
                self._on_key(msvcrt.getwch())
            return
        import select
        import termios
        import tty
        self._fd = sys.stdin.fileno()
        self._old = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)  # keeps ISIG on, so Ctrl+C still raises SIGINT
        while not self._stop:
            if select.select([sys.stdin], [], [], 0.3)[0]:
                ch = sys.stdin.read(1)
                if ch:
                    self._on_key(ch)

    def stop(self) -> None:
        # Restore the terminal before we exit or re-exec, or the shell is left in
        # cbreak mode. No-op on Windows (msvcrt doesn't change the mode).
        self._stop = True
        if self._fd is not None and self._old is not None:
            import termios
            with contextlib.suppress(Exception):
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)


def _find_git() -> str | None:
    """Locate a git executable. Many Windows boxes use GitHub Desktop, which bundles
    git but never puts it on PATH — so also look in its versioned app folder and the
    usual Git-for-Windows install dirs. BLURAYRIPPER_GIT overrides everything."""
    import os
    import shutil
    from pathlib import Path

    override = os.environ.get("BLURAYRIPPER_GIT")
    if override:
        return override if Path(override).exists() else shutil.which(override)

    onpath = shutil.which("git")
    if onpath:
        return onpath
    if sys.platform != "win32":
        return None

    candidates: list[Path] = []
    localapp = os.environ.get("LOCALAPPDATA", "")
    if localapp:
        gh = Path(localapp) / "GitHubDesktop"
        # app-<version> changes on every GitHub Desktop update — take the newest.
        for sub in ("cmd/git.exe", "mingw64/bin/git.exe"):
            candidates += sorted(gh.glob(f"app-*/resources/app/git/{sub}"), reverse=True)
        candidates.append(Path(localapp) / "Programs" / "Git" / "cmd" / "git.exe")
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        candidates.append(Path(base) / "Git" / "cmd" / "git.exe")
        candidates.append(Path(base) / "Git" / "bin" / "git.exe")
    return next((str(c) for c in candidates if c.exists()), None)


# Every "please stop" signal a worker can receive. Ctrl+C is the interactive one;
# SIGTERM (POSIX) and SIGBREAK (Windows Ctrl+Break / GenerateConsoleCtrlEvent) are
# what a supervisor sends. Not all exist on every platform.
_STOP_SIGNALS = ("SIGINT", "SIGTERM", "SIGBREAK")


def _install_stop_handlers(handler) -> list[str]:
    """Route every available stop signal to ``handler``, and report which ones
    were hooked.

    A worker run unattended (a scheduled task, a service wrapper) must DRAIN on
    shutdown — finish and place the current encode — rather than lose an
    hours-long job. Handling only SIGINT would mean an unattended stop always
    aborts. Returns the signal names actually installed, so a caller can tell
    what coverage it has."""
    import signal

    installed: list[str] = []
    for name in _STOP_SIGNALS:
        sig = getattr(signal, name, None)
        if sig is None:
            continue  # not on this platform
        # Signals are only settable on the main thread; ignore it if we're not on one.
        with contextlib.suppress(ValueError, OSError, RuntimeError):
            signal.signal(sig, handler)
            installed.append(name)
    return installed


def _pull_and_restart(console, pull: bool = True) -> None:
    """Re-exec with the same args, optionally `git pull --ff-only` first.

    ``pull=False`` is a plain restart on the current code — what the server's
    per-worker Restart asks for, as distinct from Update. Restarts even if the
    pull fails, so the worker never stays down over a network hiccup."""
    import os
    import subprocess

    from .config import REPO_ROOT

    git = _find_git() if pull else None
    if not pull:
        console.print("[bold cyan]Restarting on the current code…[/bold cyan]")
    elif not git:
        console.print(
            "[yellow]git not found — GitHub Desktop bundles git but doesn't add it to "
            "PATH. Set BLURAYRIPPER_GIT to your git.exe, or install Git for Windows. "
            "Restarting on the current code (no pull).[/yellow]")
    else:
        console.print(f"[bold cyan]Pulling latest code…[/bold cyan] [dim]({git})[/dim]")
        sys.stdout.flush()
        try:
            r = subprocess.run(
                [git, "-C", str(REPO_ROOT), "pull", "--ff-only"],
                capture_output=True, text=True, timeout=180)
            if r.stdout.strip():
                console.print(r.stdout.strip())
            if r.returncode != 0:
                console.print(
                    f"[yellow]git pull did not succeed (exit {r.returncode}) — "
                    "restarting on the current code.[/yellow]")
                if r.stderr.strip():
                    console.print(f"[yellow]{r.stderr.strip()}[/yellow]")
        except Exception as exc:  # network/git error must not leave the worker dead
            console.print(f"[yellow]git pull failed ({type(exc).__name__}: {exc}) — "
                          "restarting on the current code.[/yellow]")
    console.print("[bold green]Restarting worker…[/bold green]")
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execv(sys.executable, [sys.executable, "-m", "blurayripper", *sys.argv[1:]])
    except OSError as exc:
        console.print(f"[red]Restart (re-exec) failed: {exc}[/red]")


async def _cmd_worker(args: argparse.Namespace) -> int:
    from .worker import WorkerRunner

    settings = load_settings(args.config)
    if not settings.database_url:
        console.print(
            "[red]A worker needs a shared database. Set BLURAYRIPPER_DATABASE_URL "
            "(the same Postgres the rip machine uses) in config/secrets.env.[/red]")
        return 2
    # Pull the cluster's shared config before starting, same as serve does. A
    # worker box has its own gitignored config files, so without this it happily
    # encodes at whatever preset that box was last set to — the drift is silent,
    # and the output looks fine until you compare it to another worker's.
    try:
        from .boxset_plan import sync_from_cluster as _sync_boxsets
        from .handbrake_store import SHARED_KEY, apply_shared_preset
        from .persistence import make_store
        from .settings_store import sync_from_cluster as _sync_settings
        _store = make_store(settings.database_url)
        _shared = _store.get_shared_config(SHARED_KEY)
        changed = bool(_shared and apply_shared_preset(settings.config_dir, _shared))
        for note in (_sync_settings(settings.config_dir, _store),
                     _sync_boxsets(settings, _store)):
            if note.startswith("pulled"):
                changed = True
                console.print(f"[dim]Cluster config: {note}.[/dim]")
            elif "unreachable" in note:
                console.print(f"[yellow]Cluster config: {note}.[/yellow]")
        if changed:
            settings = load_settings(args.config)
    except Exception:
        pass  # DB down — the worker still starts on its local cache

    runner = WorkerRunner(settings, worker_id=args.name)
    console.print(
        f"[bold green]Encode worker[/bold green] {runner.worker_id} — "
        "Ctrl+C to drain (finish current encode, then exit); Ctrl+C again to abort. "
        "Press [bold]u[/bold] (or Ctrl+U) to drain, pull updates, and restart "
        "('Update all workers' in the web UI does the same to every worker).")

    import signal

    def _drain(signum, frame):
        if runner._stop:  # second Ctrl+C — abort the current encode now
            signal.signal(signal.SIGINT, signal.default_int_handler)
            raise KeyboardInterrupt
        runner.request_stop()
        console.print(
            "[yellow]Draining — will finish the current encode and place it, then "
            "exit. Ctrl+C again to abort now.[/yellow]")

    def _on_key(ch):
        if _is_update_key(ch) and not runner._stop:
            runner.request_update()
            console.print(
                "[cyan]Update requested — draining (finish the current encode), then "
                "git pull + restart. Ctrl+C to abort now.[/cyan]")

    keys = _KeyPress(_on_key)

    _install_stop_handlers(_drain)
    keys.start()
    try:
        await runner.run()
    except KeyboardInterrupt:
        keys.stop()
        console.print("[red]Aborted — the in-progress encode was discarded; its job "
                      "will be reclaimed and re-encoded from the rip.[/red]")
        return 130
    except ToolNotFoundError as exc:
        keys.stop()
        console.print(f"[red]{exc}[/red]")
        return 2
    keys.stop()
    if runner.restart_requested:  # 'u' key, or a server restart/update signal
        # update pulls first; a plain restart re-execs the code already on disk.
        _pull_and_restart(console, pull=runner.update_requested)  # os.execv — no return
        # Only reached if the re-exec failed. Exit non-zero so a supervising
        # launcher (scripts/run-worker-windows.bat) brings the worker back up
        # instead of reading this as an intentional drain and stopping.
        return 75
    return 0


async def _cmd_eject(args: argparse.Namespace) -> int:
    from .tools.eject import eject

    drive = args.drive
    console.print(f"[bold]Ejecting[/bold] {drive or 'default drive'} (method: {args.method}) …")
    try:
        eject(drive, args.method)
        console.print(f"[green]Eject command succeeded[/green] for {drive or 'default drive'}.")
        console.print("[dim]If the tray still didn't move, try --method scsi / shell / mci, or "
                      "the \\\\.\\CdRom0 device path.[/dim]")
        return 0
    except Exception as exc:
        # eject() collects each method's failure into the message.
        console.print(f"[red]Eject failed:[/red] {exc}")
        console.print(
            "[yellow]Tip:[/yellow] pass the exact drive letter (e.g. [bold]blurayripper "
            "eject F:[/bold]). The error above lists what each method (ioctl/shell/mci) "
            "returned.")
        return 1


async def _cmd_close(args: argparse.Namespace) -> int:
    from .tools.eject import close_tray

    drive = args.drive
    console.print(f"[bold]Closing tray[/bold] {drive or 'default drive'} (method: {args.method}) …")
    try:
        close_tray(drive, args.method)
        console.print(f"[green]Close command succeeded[/green] for {drive or 'default drive'}.")
        return 0
    except Exception as exc:
        console.print(f"[red]Close tray failed:[/red] {exc}")
        return 1


async def _cmd_resub(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .pipeline import Pipeline

    settings = load_settings(args.config)
    video = Path(args.video)
    source = Path(args.source) if args.source else None
    if source is not None and not source.exists():
        console.print(f"[red]Source MKV not found: {source}[/red]")
        return 2
    if not video.exists():
        console.print(f"[yellow]Warning: destination video {video} not found — sidecars "
                      "will still be written next to that path.[/yellow]")

    class _Console:
        def on_event(self, msg): console.print(f"  {msg}")
        def on_progress(self, *a): pass
        def checkpoint(self): pass

    src_note = f" from {source.name}" if source else " from cached subs"
    console.print(f"[bold]Re-subtitling[/bold] {video.name}{src_note} …")
    try:
        placed = await Pipeline(settings).resubtitle(video, _Console(), source_mkv=source)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        return 2
    except ToolNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        return 2
    if not placed:
        console.print("[yellow]No subtitle sidecars were produced.[/yellow]")
        return 1
    for p in placed:
        console.print(f"[green]Placed[/green] {p}")
    return 0


async def _cmd_secrets(args: argparse.Namespace) -> int:
    """Manage the credentials shared through Postgres.

    Values are never printed. The whole point is that they stop being readable
    from anywhere except a box that already holds the passphrase, and a CLI that
    echoes them back would put them into shell history and terminal scrollback.
    """
    from .config import _load_secrets
    from .persistence import make_store
    from .services import secretbox

    settings = load_settings(args.config)
    raw = _load_secrets(settings.config_dir)
    passphrase = raw.get(secretbox.PASSPHRASE_KEY, "").strip()
    if not settings.database_url:
        console.print("[red]No database configured — nothing to share with.[/red]")
        return 2
    if not passphrase:
        console.print(
            f"[red]{secretbox.PASSPHRASE_KEY} is not set in config/secrets.env.[/red]\n"
            "Generate one and put it in secrets.env on EVERY box (it must match):\n"
            "  python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"")
        return 2
    store = make_store(settings.database_url)

    if args.action == "clear":
        store.set_shared_config(secretbox.SHARED_KEY, {})
        console.print("[green]Cleared.[/green] Every box falls back to its own "
                      "secrets.env on the next restart.")
        return 0

    if args.action == "push":
        movable = secretbox.movable(raw)
        if not movable:
            console.print(
                "[yellow]Nothing to push — none of the shareable credentials are "
                f"set on this box.[/yellow] Shareable: {', '.join(secretbox.SHARED_KEYS)}")
            return 1
        secretbox.publish(store, passphrase, movable)
        console.print(
            f"[bold green]Stored {len(movable)} credential(s), encrypted.[/bold green]")
        for name in sorted(movable):
            console.print(f"  [dim]{name}[/dim]")
        console.print(
            "\n[dim]Other boxes pick these up on their next restart, as long as "
            f"their secrets.env has the same {secretbox.PASSPHRASE_KEY}. A local "
            "value still wins, so a box can override one.[/dim]")
        return 0

    # status
    try:
        stored = secretbox.fetch(store, passphrase)
    except secretbox.SecretsUnavailable as exc:
        console.print(f"[red]{exc}[/red]")
        return 2
    if not stored:
        console.print("[yellow]No credentials stored in the cluster.[/yellow] "
                      "Run [bold]blurayripper secrets push[/bold] from the box "
                      "that has them.")
        return 1
    # `raw` has the cluster values merged in already, so read the file directly
    # to tell "this box overrides it" apart from "this box received it".
    local_only = {}
    env_file = settings.config_dir / "secrets.env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                local_only[k.strip()] = v.strip().strip('"').strip("'")
    table = Table("Credential", "In cluster", "Overridden on this box",
                  title="Shared credentials (values never shown)")
    for name in secretbox.SHARED_KEYS:
        table.add_row(
            name,
            "[green]yes[/green]" if name in stored else "[dim]no[/dim]",
            "[yellow]yes[/yellow]" if local_only.get(name, "").strip() else "[dim]no[/dim]")
    console.print(table)
    console.print(
        "[dim]The database holds ciphertext only. Anyone who can read a box's "
        "secrets.env can still decrypt them — that is unavoidable, since a box "
        "must decrypt unattended at boot.[/dim]")
    return 0


async def _cmd_db_init(args: argparse.Namespace) -> int:
    from .persistence import MemoryStore, make_store

    settings = load_settings(args.config)
    store = make_store(settings.database_url)
    if isinstance(store, MemoryStore):
        console.print(
            "[yellow]No database configured — running in memory (no crash recovery).[/yellow]\n"
            "Set [database].url in settings.toml or BLURAYRIPPER_DATABASE_URL in secrets.env "
            "to enable Postgres persistence."
        )
        return 1
    try:
        store.init()
    except Exception as exc:
        console.print(f"[red]Database init failed: {exc}[/red]")
        return 2
    existing = len(store.load_all())
    console.print(
        f"[bold green]Database ready.[/bold green] Schema ensured; {existing} job(s) currently stored."
    )
    return 0


async def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        console.print("[red]Web UI needs extra deps. Install with: pip install -e \".[web]\"[/red]")
        return 2
    from .web.app import create_app

    settings = load_settings(args.config)

    # Pull the cluster-shared HandBrake preset (if clustered) so this box uses the
    # same one as everyone else, then reload so preset_file resolves to the synced
    # presets.local.json. Keeps a fresh serve box from silently running a different
    # preset than the workers.
    if settings.database_url:
        try:
            from .handbrake_store import SHARED_KEY, apply_shared_preset
            from .persistence import make_store
            _store = make_store(settings.database_url)
            _shared = _store.get_shared_config(SHARED_KEY)
            if _shared and apply_shared_preset(settings.config_dir, _shared):
                settings = load_settings(args.config)
                console.print("[dim]Applied the cluster's shared HandBrake preset.[/dim]")
            # The web-edited settings overlay, same deal. It used to live only in
            # a gitignored file, so a new box silently started from settings.toml
            # defaults — which looks like the app changing settings, not like a
            # file that never arrived.
            from .boxset_plan import sync_from_cluster as _sync_boxsets
            _bs = _sync_boxsets(settings, _store)
            if _bs.startswith(("pulled", "seeded")):
                console.print(f"[dim]Cluster box-set plan: {_bs}.[/dim]")
            from .settings_store import sync_from_cluster
            note = sync_from_cluster(settings.config_dir, _store)
            if note.startswith("pulled") or note.startswith("seeded"):
                settings = load_settings(args.config)
                console.print(f"[dim]Cluster settings: {note}.[/dim]")
            elif "unreachable" in note:
                console.print(f"[yellow]Cluster settings: {note}.[/yellow]")
        except Exception:
            pass  # DB down / not set up — fall back to the local preset

    host = args.host or settings.web_host
    port = args.port or settings.web_port

    # Quiet a benign Windows/Proactor quirk: when a browser drops an SSE/HTTP
    # connection, the loop logs ConnectionResetError (WinError 10054) from a
    # teardown callback. Harmless — swallow just that, keep everything else.
    def _quiet_disconnects(loop, context):
        # Benign Windows/Proactor noise when a browser drops an SSE/HTTP
        # connection: the loop logs a ConnectionReset/Aborted, or a bare
        # "socket.send() raised exception" from a teardown callback. Swallow just
        # those; pass everything else to the default handler.
        exc = context.get("exception")
        msg = context.get("message", "")
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        if "raised exception" in msg and ("socket.send" in msg or "socket.shutdown" in msg):
            return
        loop.default_exception_handler(context)

    asyncio.get_running_loop().set_exception_handler(_quiet_disconnects)

    # Kill tool children with us (Windows) so a crash/power-off can't orphan them.
    from .tools.winjob import enable_kill_on_close
    enable_kill_on_close()

    # Reap tool processes orphaned by a prior crash/power-off before starting —
    # otherwise leftover SubtitleEdit/MakeMKV/HandBrake instances pile up across
    # restarts until they exhaust the system commit limit (out-of-memory errors).
    if settings.cleanup_stale_tools:
        from pathlib import Path as _Path

        from .tools import process as _process
        # Only reap the tools THIS serve process spawns — otherwise a `worker`
        # running on the same machine would have its live encode/OCR killed by
        # name every time serve restarts. serve always rips (makemkvcon); it only
        # encodes/OCRs locally when not fully delegated to workers.
        cl = settings.cluster
        serve_encodes = (not cl.use_workers) or cl.local_encode_max_height > 0
        serve_does_subs = not cl.worker_does_subs
        paths = [settings.tools.makemkvcon]
        if serve_encodes:
            paths.append(settings.tools.handbrakecli)
        if serve_does_subs:
            paths += [settings.tools.subtitleedit, settings.tools.mkvextract]
        names = {_Path(p).name for p in paths if p}
        killed = await _process.kill_stale_tools(sorted(names))
        if killed:
            console.print(f"[dim]Cleaned up any stale tool processes: {', '.join(killed)}[/dim]")

    # Pre-flight: the staging/output roots (often a mapped NAS drive) must be
    # reachable, or the app crashes deep in a mkdir with a cryptic WinError 3.
    # Fail here with a clear, actionable message instead.
    for label, folder in (("staging_dir", settings.storage.staging_dir),
                          ("output_dir", settings.storage.output_dir)):
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            drive = folder.drive or str(folder)
            console.print(
                f"[red]Cannot access {label} at {folder}[/red]\n"
                f"[yellow]The drive {drive} isn't available to this process. Check:\n"
                f"  • the NAS / network path is up (this same outage also breaks Postgres),\n"
                f"  • the drive is actually mapped in THIS session — run: net use\n"
                f"  • you're NOT in an elevated/admin prompt (mapped drives are hidden from "
                f"elevated processes).[/yellow]\n"
                f"[dim]({type(exc).__name__}: {exc})[/dim]")
            return 2

    app = create_app(settings)
    where = "all interfaces" if host == "0.0.0.0" else host
    from .version import get_version
    console.print(f"[bold green]BluRayRipper UI[/bold green] v{get_version()} → http://{host}:{port}  ({where})")
    console.print("[dim]Ctrl+C to stop · press 'u' (or Ctrl+U) to git pull + restart.[/dim]")
    if host == "0.0.0.0":
        console.print(
            "[yellow]Bound to all interfaces — reachable on your LAN with no login. "
            "Use only on a trusted network.[/yellow]"
        )
    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning",
        timeout_graceful_shutdown=5,  # don't wait forever on the SSE stream
    )
    server = uvicorn.Server(config)
    # Let the web UI's "Restart server" button reach this server (graceful
    # shutdown frees the port) and flag a restart we honor after serve() returns.
    app.state.uvicorn_server = server

    # Ctrl+U (or 'u') at the console: git pull + restart, like a worker. Unlike a
    # worker there's no clean "drain" — in-flight rips/encodes are interrupted, but
    # crash-recovery resumes them from their last checkpoint on restart.
    serve_update = {"do": False}

    def _on_key(ch):
        if _is_update_key(ch) and not serve_update["do"]:
            serve_update["do"] = True
            console.print("[cyan]Update requested — shutting down, then git pull + "
                          "restart. In-flight jobs resume after restart.[/cyan]")
            server.should_exit = True  # graceful uvicorn shutdown, frees the port
    keys = _KeyPress(_on_key)
    keys.start()

    # uvicorn waits for active connections (the SSE stream) to finish before it
    # runs the lifespan shutdown that sets manager.closing — a deadlock that hangs
    # Ctrl+C for the full grace period and logs a scary CancelledError. Flip
    # `closing` the instant shutdown begins so the SSE generators exit at once.
    async def _close_streams_on_exit():
        while not server.should_exit:
            await asyncio.sleep(0.2)
        mgr = getattr(app.state, "manager", None)
        if mgr is not None:
            mgr.closing = True
            mgr.bus.publish()

    watcher = asyncio.ensure_future(_close_streams_on_exit())
    try:
        await server.serve()
    finally:
        watcher.cancel()
        keys.stop()

    # Ctrl+U at the console: pull the latest code, then restart.
    if serve_update["do"]:
        _pull_and_restart(console)  # git pull + os.execv — normally never returns
        return 1                    # only reached if the re-exec itself failed

    # A web-triggered restart: uvicorn has now shut down gracefully and released
    # the port, so replace this process with a fresh `serve` that picks up the
    # latest code and settings (config/settings.local.json). os.execv keeps the
    # same console/PID on POSIX; on Windows Python emulates it (this process
    # exits, a new one starts — which, via the Job Object, also reaps any tool
    # children, exactly the "restart kills in-flight work" case the UI warns about).
    if getattr(app.state, "restart_requested", False):
        import os

        if getattr(app.state, "restart_pull", False):
            # "Update server" from the UI: same as pressing u at the console.
            _pull_and_restart(console, pull=True)   # os.execv — normally no return
            return 1                                 # only if the re-exec failed
        console.print("[bold yellow]Restarting BluRayRipper…[/bold yellow]")
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            os.execv(sys.executable, [sys.executable, "-m", "blurayripper", *sys.argv[1:]])
        except OSError as exc:
            console.print(f"[red]Restart (re-exec) failed: {exc}[/red]")
            return 1
    return 0


async def _cmd_coverart(args: argparse.Namespace) -> int:
    import re as _re
    from pathlib import Path

    from .services import coverart
    from .tools.makemkv import MakeMKV

    settings = load_settings(args.config)
    src = args.source.strip()
    root: Path | None = None
    if src.startswith("file:"):
        root = Path(src[5:])
    elif src.startswith("disc:"):
        try:
            idx = int(src.split(":", 1)[1])
        except ValueError:
            console.print("[red]Bad disc selector.[/red]")
            return 2
        drives = await MakeMKV(settings).list_drives()
        drv = next((d for d in drives if d.index == idx), None)
        dev = drv.device if drv else None
        root = Path(dev + "\\") if dev and _re.match(r"^[A-Za-z]:$", dev) else None
    elif _re.match(r"^[A-Za-z]:\\?$", src):
        root = Path(src if src.endswith("\\") else src + "\\")
    else:
        root = Path(src)
    if root is None or not root.exists():
        console.print(f"[red]Could not resolve a readable disc root from {src!r}.[/red]")
        return 2
    portraits, landscapes = coverart.classify_images(root)
    if not portraits and not landscapes:
        console.print(f"[yellow]No cover art found under {root} "
                      "(no BDMV/META/DL or JACKET_P images).[/yellow]")
        return 1
    art = coverart.extract_art(root, Path(args.out))
    title = coverart.disc_title(root)
    console.print(
        f"[green]Saved[/green] "
        + (f"poster={art.poster.name} " if art.poster else "")
        + (f"fanart={art.fanart.name}" if art.fanart else "")
        + f"  ({len(portraits)} portrait, {len(landscapes)} landscape image(s))")
    if not art.poster and art.fanart:
        console.print("[dim]Disc only had a landscape image → saved as backdrop "
                      "(fanart); Plex will fetch the poster from its metadata agent.[/dim]")
    if title:
        console.print(f"Disc title: [bold]{title}[/bold]")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="blurayripper", description=__doc__)
    parser.add_argument("--config", help="Path to config directory", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="Pre-flight: verify tools, folders, and preset config")
    check.set_defaults(func=_cmd_check)

    vsub = sub.add_parser(
        "verify-subs",
        help="Audit finished jobs for placed movies missing their subtitle sidecars")
    vsub.add_argument("--all", action="store_true",
                      help="List every done job, not just those missing subtitles")
    vsub.set_defaults(func=_cmd_verify_subs)

    scan = sub.add_parser("scan", help="Scan a disc/folder/ISO and list titles")
    scan.add_argument("source", help="makemkvcon source, e.g. disc:0 / file:/path / iso:/path")
    scan.set_defaults(func=_cmd_scan)

    rip = sub.add_parser("rip", help="Run the full rip->encode->subtitle->name pipeline")
    rip.add_argument("source", help="makemkvcon source, e.g. disc:0 / file:/path / iso:/path")
    rip.add_argument("--name", help="Optional job name (staging subfolder)", default=None)
    rip.set_defaults(func=_cmd_rip)

    cover = sub.add_parser(
        "coverart", help="Extract a disc's cover art (poster) from BDMV/META or DVD JACKET_P")
    cover.add_argument("source", help="disc:N, a drive (F:), or a path / file:BDMV_parent")
    cover.add_argument("--out", default=".", help="Output directory (default: current dir)")
    cover.set_defaults(func=_cmd_coverart)

    queue = sub.add_parser(
        "queue",
        help="Rip several discs at once: confirm each up front, then run "
        "concurrently with smart encode scheduling (no two UHD encodes at once)",
    )
    queue.add_argument(
        "sources", nargs="+",
        help="Multiple makemkvcon sources, e.g. disc:0 disc:1",
    )
    queue.set_defaults(func=_cmd_queue)

    serve = sub.add_parser("serve", help="Launch the local web UI to manage the rip queue")
    serve.add_argument("--host", default=None,
                       help="Bind host (overrides [web].host; use 0.0.0.0 for LAN access)")
    serve.add_argument("--port", type=int, default=None, help="Port (overrides [web].port)")
    serve.set_defaults(func=_cmd_serve)

    db_init = sub.add_parser("db-init", help="Create/verify the Postgres schema for crash recovery")
    db_init.set_defaults(func=_cmd_db_init)

    secrets_p = sub.add_parser(
        "secrets", help="Share API credentials through Postgres, encrypted at rest")
    secrets_p.add_argument(
        "action", choices=("push", "status", "clear"),
        help="push: encrypt this box's credentials to the cluster. "
             "status: show what is stored (names only, never values). "
             "clear: remove them from the cluster.")
    secrets_p.set_defaults(func=_cmd_secrets)

    worker = sub.add_parser(
        "worker", help="Run an encode worker that pulls jobs from the shared queue "
                       "(Ctrl+C drains; 'u'/Ctrl+U drains + git-pulls + restarts)")
    worker.add_argument(
        "--name", default=None,
        help="Stable worker id (default: hostname). Give each worker on the SAME "
             "host a distinct --name.")
    worker.set_defaults(func=_cmd_worker)

    resub = sub.add_parser(
        "resub",
        help="Re-OCR subtitles onto an existing library video (no encode); uses cached subs")
    resub.add_argument("video", help="The placed library video to sit the .srt sidecars next to")
    resub.add_argument(
        "source", nargs="?", default=None,
        help="Optional source .mkv with PGS subs; omit to use the cached subs from the rip")
    resub.set_defaults(func=_cmd_resub)

    eject_p = sub.add_parser(
        "eject", help="Test optical-disc eject on a drive (diagnose auto-eject)")
    eject_p.add_argument("drive", nargs="?", default=None,
                         help="Drive to eject, e.g. F: or \\\\.\\CdRom0 (Windows), /dev/sr0 (Linux)")
    eject_p.add_argument("--method", default="auto",
                         choices=["auto", "scsi", "ioctl", "shell", "mci"],
                         help="Force a specific Windows eject method (to find which opens your drive)")
    eject_p.set_defaults(func=_cmd_eject)

    close_p = sub.add_parser("close", help="Close (load) an optical drive tray")
    close_p.add_argument("drive", nargs="?", default=None,
                         help="Drive to close, e.g. F: or \\\\.\\CdRom0 (Windows), /dev/sr0 (Linux)")
    close_p.add_argument("--method", default="auto",
                         choices=["auto", "scsi", "ioctl", "mci"],
                         help="Force a specific Windows close method")
    close_p.set_defaults(func=_cmd_close)
    return parser


def _quiet_windows_shutdown() -> None:
    """Silence the benign teardown tracebacks the Windows asyncio Proactor prints
    when subprocess/pipe transports are finalized by the GC at interpreter
    shutdown (e.g. after Ctrl+C): 'Exception ignored in __del__ … ValueError: I/O
    operation on closed pipe'. Only those; everything else goes to the default
    hook, and this is a no-op off Windows."""
    if sys.platform != "win32":
        return
    import warnings

    warnings.filterwarnings("ignore", message="unclosed transport", category=ResourceWarning)
    default_hook = sys.unraisablehook

    def _hook(unraisable):
        exc = unraisable.exc_value
        if isinstance(exc, ValueError) and "closed pipe" in str(exc):
            return  # proactor transport __del__ on an already-closed pipe — harmless
        default_hook(unraisable)

    sys.unraisablehook = _hook


def main(argv: list[str] | None = None) -> int:
    _quiet_windows_shutdown()
    parser = _build_parser()
    args = parser.parse_args(argv)
    config_dir = None
    if args.config:
        from pathlib import Path

        config_dir = Path(args.config)
    args.config = config_dir
    try:
        return asyncio.run(args.func(args))
    except ToolNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        return 2
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
