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
    # ffmpeg is optional (only the grain preview uses it) — don't fail on it.
    tool_table.add_row(
        "ffmpeg [dim](optional)[/dim]",
        f"[green]{settings.tools.ffmpeg}[/green]" if settings.tools.ffmpeg
        else "[yellow]not found — grain preview disabled[/yellow]",
    )
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
    host = args.host or settings.web_host
    port = args.port or settings.web_port

    # Quiet a benign Windows/Proactor quirk: when a browser drops an SSE/HTTP
    # connection, the loop logs ConnectionResetError (WinError 10054) from a
    # teardown callback. Harmless — swallow just that, keep everything else.
    def _quiet_disconnects(loop, context):
        if isinstance(context.get("exception"), ConnectionResetError):
            return
        loop.default_exception_handler(context)

    asyncio.get_running_loop().set_exception_handler(_quiet_disconnects)

    app = create_app(settings)
    where = "all interfaces" if host == "0.0.0.0" else host
    console.print(f"[bold green]BluRayRipper UI[/bold green] → http://{host}:{port}  ({where})")
    if host == "0.0.0.0":
        console.print(
            "[yellow]Bound to all interfaces — reachable on your LAN with no login. "
            "Use only on a trusted network.[/yellow]"
        )
    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning",
        timeout_graceful_shutdown=5,  # don't wait forever on the SSE stream
    )
    await uvicorn.Server(config).serve()
    return 0


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
