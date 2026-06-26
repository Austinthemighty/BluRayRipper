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
import sys

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from .config import Settings, ToolNotFoundError, load_settings
from .models import DiscInfo, EpisodeAssignment, MediaKind
from .pipeline import Pipeline
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

    # External tools
    tool_table = Table("Tool", "Status", title="External tools")
    for key in ("makemkvcon", "handbrakecli", "mkvmerge", "mkvextract", "subtitleedit"):
        path = getattr(settings.tools, key)
        if path:
            tool_table.add_row(key, f"[green]{path}[/green]")
        else:
            tool_table.add_row(key, "[red]MISSING — install or set in settings.toml[/red]")
            ok = False
    runner = settings.tools.subtitleedit_runner
    tool_table.add_row("subtitleedit_runner", runner or "[dim](none — runs directly)[/dim]")
    console.print(tool_table)

    # Folders (writability)
    folder_table = Table("Folder", "Path", "Writable", title="Storage")
    for label, path in (
        ("staging", settings.storage.staging_dir),
        ("output", settings.storage.output_dir),
    ):
        writable = _check_writable(path)
        folder_table.add_row(label, str(path), "[green]yes[/green]" if writable else "[red]NO[/red]")
        ok = ok and writable
    console.print(folder_table)

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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="blurayripper", description=__doc__)
    parser.add_argument("--config", help="Path to config directory", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="Pre-flight: verify tools, folders, and preset config")
    check.set_defaults(func=_cmd_check)

    scan = sub.add_parser("scan", help="Scan a disc/folder/ISO and list titles")
    scan.add_argument("source", help="makemkvcon source, e.g. disc:0 / file:/path / iso:/path")
    scan.set_defaults(func=_cmd_scan)

    rip = sub.add_parser("rip", help="Run the full rip->encode->subtitle->name pipeline")
    rip.add_argument("source", help="makemkvcon source, e.g. disc:0 / file:/path / iso:/path")
    rip.add_argument("--name", help="Optional job name (staging subfolder)", default=None)
    rip.set_defaults(func=_cmd_rip)
    return parser


def main(argv: list[str] | None = None) -> int:
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
