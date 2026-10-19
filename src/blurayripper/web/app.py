"""FastAPI app for the local web UI.

Endpoints
  GET  /                      -> dashboard (static HTML)
  GET  /api/drives            -> optical drives + loaded disc
  GET  /api/jobs              -> queue snapshot
  POST /api/jobs              -> add a disc to the queue {source}
  POST /api/jobs/{id}/titles  -> submit selected title ids {title_ids}
  POST /api/jobs/{id}/mapping -> submit naming mapping {assignments:[...]}
  DELETE /api/jobs/{id}       -> remove a finished/parked job
  GET  /api/tmdb/search       -> TMDB search proxy {kind, query}
  GET  /api/tmdb/season       -> TMDB season episodes {tv_id, season}
  GET  /api/jobs/{id}/aacs-dump -> download the kept AACS dump
  POST /api/workers/{id}/action -> drain/restart/update one worker ("*" = all)
  GET  /api/boxsets           -> box-set plan CSV + parsed rows
  POST /api/boxsets           -> save (or dry-run) the plan CSV {csv, dry_run}
  GET  /api/events            -> SSE stream of queue snapshots
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import json
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from ..config import Settings, load_settings
from ..settings_store import save_settings, settings_ui_payload
from ..models import EpisodeAssignment, MediaKind
from ..services import naming
from ..services.metadata import MetadataError, TMDBClient
from ..services.notify import notify as send_notify
from ..services.opensubtitles import OpenSubtitlesError
from ..tools.eject import close_tray, eject
from .manager import JobManager

_STATIC = Path(__file__).parent / "static"


class AddDisc(BaseModel):
    source: str


class RemoveByState(BaseModel):
    states: list[str]


class RestartServer(BaseModel):
    force: bool = False
    pull: bool = False   # git pull before re-execing (i.e. "update" vs "restart")


class SetTitles(BaseModel):
    title_ids: list[int]


class AssignmentIn(BaseModel):
    title_id: int
    kind: str  # "movie" | "tv"
    show_or_movie_title: str
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    episode_title: str | None = None
    edition: str | None = None


class SetMapping(BaseModel):
    assignments: list[AssignmentIn]
    grain_mode: str = "auto"      # "auto" | "on" | "off" | "preview"
    tune_grain: bool | None = None


class SetGrain(BaseModel):
    tune_grain: bool


class AutoIdentify(BaseModel):
    title_ids: list[int]
    show_title: str
    show_year: int | None = None
    show_tmdb_id: int
    season: int
    episodes: list[int]            # candidate episodes on this disc
    grain_mode: str = "auto"
    tune_grain: bool | None = None


class ConfirmMatch(BaseModel):
    mapping: dict[int, int]        # title_id -> episode


class EditJob(BaseModel):
    grain_mode: str | None = None
    tune_grain: bool | None = None
    edition: str | None = None


class ChangeSource(BaseModel):
    source: str


class NameDrive(BaseModel):
    device: str
    name: str | None = None  # null/empty clears the custom name


class DriveDevice(BaseModel):
    device: str | None = None  # e.g. "F:" or "\\.\CdRom0"; null = default drive


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="BluRayRipper")
    manager = JobManager(settings)
    app.state.manager = manager  # so serve can flip `closing` when shutdown starts
    tmdb = TMDBClient(settings.tmdb_api_key, settings.metadata_language)

    @app.on_event("startup")
    async def _recover():
        # Recover persisted jobs and resume any that were mid-flight.
        try:
            n = manager.recover()
            if n:
                print(f"Recovered {n} in-flight job(s) from the last run.")
        except Exception as exc:  # don't block startup on a recovery hiccup
            # Flag it so the refresh loop retries once the store is reachable.
            # Without that the UI comes up with an EMPTY queue and stays that
            # way -- the refresh only updates jobs it already knows about -- so
            # the operator sees no jobs while workers are encoding them, until
            # serve happens to be restarted a second time.
            manager._recover_pending = True
            print(f"Job recovery skipped ({exc}); will retry when the database "
                  "is reachable.")
        manager.start_drive_watch()  # background drive polling + auto-open
        manager.start_store_refresh()  # reflect remote worker progress (clustered)

    @app.on_event("shutdown")
    async def _shutdown():
        await manager.shutdown()

    @app.get("/")
    async def index():
        """Serve the dashboard."""
        # No-cache so a pulled+restarted server always serves the latest UI
        # (otherwise the browser keeps running the old cached page).
        return FileResponse(
            _STATIC / "index.html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.get("/manifest.webmanifest")
    async def manifest():
        """Web-app manifest, so the dashboard installs to a home screen."""
        # Makes the UI installable (Add to Home Screen) as a fullscreen app.
        return JSONResponse({
            "name": "BluRayRipper", "short_name": "BluRayRipper",
            "start_url": "/", "display": "standalone",
            "background_color": "#0e1116", "theme_color": "#0e1116",
            "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml",
                       "purpose": "any"}],
        })

    @app.get("/icon.svg")
    async def icon():
        """The app icon, inline SVG so there is no binary asset to ship."""
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
            '<rect width="512" height="512" rx="104" fill="#0e1116"/>'
            '<circle cx="256" cy="256" r="150" fill="none" stroke="#58a6ff" stroke-width="30"/>'
            '<circle cx="256" cy="256" r="46" fill="#161b22" stroke="#58a6ff" stroke-width="22"/>'
            '</svg>'
        )
        return Response(content=svg, media_type="image/svg+xml",
                        headers={"Cache-Control": "max-age=86400"})

    @app.get("/api/drives")
    async def drives():
        """Every optical drive and what is currently in it."""
        from ..config import ToolNotFoundError

        # Prefer the watcher's cache; if it hasn't populated yet, do a one-off
        # live scan so the panel isn't blank (and surface real errors).
        found = manager.drives()
        if not found:
            try:
                found = await manager.list_drives()
                manager._drives_cache = found
            except ToolNotFoundError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except (TimeoutError, asyncio.TimeoutError):
                raise HTTPException(
                    status_code=503,
                    detail="Drive scan timed out — the system is busy, or a drive "
                           "isn't responding (check/unplug a failing drive). Will retry.")
        return [
            {
                "index": d.index, "name": d.name, "device": d.device,
                "disc_name": d.disc_name, "label": manager.drive_names.get(d.device),
                "os_detected": getattr(d, "os_detected", False),
                "makemkv_blind": getattr(d, "makemkv_blind", False),
            }
            for d in found
        ]

    @app.post("/api/drives/name")
    async def name_drive(body: NameDrive):
        """Give a drive a human label ("left bay"), since device paths are unreadable."""
        manager.set_drive_name(body.device, body.name)
        return {"ok": True}

    @app.post("/api/drives/eject")
    async def drive_eject(body: DriveDevice):
        """Open a drive's tray."""
        try:
            await asyncio.to_thread(eject, body.device)  # off-thread: ctypes/MCI can block
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Eject failed: {exc}")
        # Forget the disc that just came out, so the panel doesn't keep showing it
        # and re-inserting it counts as a new disc worth scanning.
        manager.note_disc_ejected(device=body.device)
        return {"ok": True}

    @app.post("/api/drives/close")
    async def drive_close(body: DriveDevice):
        """Close a drive's tray."""
        try:
            await asyncio.to_thread(close_tray, body.device)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Close tray failed: {exc}")
        return {"ok": True}

    @app.get("/api/jobs")
    async def jobs():
        """The whole queue in one snapshot: jobs, workers and server state.

        This is what the dashboard renders. `/api/events` pushes the same shape
        as it changes, so poll this only if you can't hold an SSE connection."""
        return manager.snapshot()

    @app.get("/api/jobs/{job_id}/log")
    async def job_log(job_id: int):
        """The full retained log for one job.

        The snapshot carries only the last few lines per job, to keep each SSE
        frame small; this is the whole thing, for diagnosing a failure."""
        # The snapshot only carries the last few log lines (per-job, to keep the
        # SSE frame small); this returns the full retained log for diagnosis.
        wj = manager.jobs.get(job_id)
        if wj is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        return {"id": job_id, "log": list(wj.log)}

    @app.post("/api/jobs")
    async def add_job(body: AddDisc):
        """Queue a source: a disc selector (`disc:0`), an .mkv, an ISO, or a folder.

        A folder of one show's episode files becomes ONE job, so the season is
        named once rather than per file."""
        try:
            ids = manager.add_source(body.source)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except OSError as exc:
            # add_source promises ValueError for a source it can't resolve, but
            # some paths never get that far: pathlib re-raises ENAMETOOLONG (a
            # pasted path with one component past ~255 chars) straight out of
            # is_dir(). Without this the "add a folder" box answers 500 and the
            # UI shows nothing the operator can act on.
            raise HTTPException(
                status_code=400,
                detail=f"Couldn't read that source: {exc.strerror or exc}")
        return {"ids": ids}

    @contextlib.contextmanager
    def _job_errors():
        """Map the manager's exceptions onto HTTP for a job endpoint: an unknown
        job id is 404, a rejected request (wrong state, bad input) is 400.

        A context manager rather than a callback, so the same wrapper works for
        `await`ing endpoints and for ones that need the return value — those had
        to hand-roll this try/except before.
        """
        try:
            yield
        except KeyError:
            raise HTTPException(status_code=404, detail="Job not found.")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/jobs/{job_id}/titles")
    async def submit_titles(job_id: int, body: SetTitles):
        """Choose which of the disc's titles to rip."""
        with _job_errors():
            manager.set_titles(job_id, body.title_ids)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/mapping")
    async def submit_mapping(job_id: int, body: SetMapping):
        """Confirm what each title is. This is what names the output files."""
        try:
            assignments = [
                EpisodeAssignment(
                    title_id=a.title_id,
                    kind=MediaKind(a.kind),
                    show_or_movie_title=a.show_or_movie_title,
                    year=a.year,
                    season=a.season,
                    episode=a.episode,
                    episode_title=a.episode_title,
                    edition=a.edition,
                )
                for a in body.assignments
            ]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        with _job_errors():
            manager.set_mapping(job_id, assignments, body.grain_mode, body.tune_grain)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/grain")
    async def submit_grain(job_id: int, body: SetGrain):
        """Answer the film-grain question for a job."""
        with _job_errors():
            manager.set_grain(job_id, body.tune_grain)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/auto-identify")
    async def auto_identify(job_id: int, body: AutoIdentify):
        """Identify a scrambled TV disc by OCRing each title's subtitles.

        For box sets whose titles are in no useful order. Parks the job for
        confirmation rather than trusting the match."""
        with _job_errors():
            manager.set_auto_identify(
                job_id, body.show_title, body.show_year, body.show_tmdb_id, body.season,
                body.episodes, body.title_ids, body.grain_mode, body.tune_grain)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/confirm-match")
    async def confirm_match(job_id: int, body: ConfirmMatch):
        """Accept, or correct, the episode match auto-identify proposed."""
        with _job_errors():
            manager.confirm_match(job_id, {int(k): v for k, v in body.mapping.items()})
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/retry")
    async def retry_job(job_id: int):
        """Retry a failed job from its last checkpoint — a finished rip is reused."""
        with _job_errors():
            manager.retry(job_id)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/bump")
    async def bump_job(job_id: int):
        """Move a job to the front of the encode queue."""
        with _job_errors():
            manager.bump_job(job_id)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/requeue")
    async def requeue_job(job_id: int):
        """Cancel whatever is holding a job and run it again from its checkpoint.

        For a wedged rip: cancelling kills the subprocess and frees the drive."""
        with _job_errors():
            manager.requeue(job_id)
        return {"ok": True}

    @app.post("/api/jobs/requeue-stalled")
    async def requeue_stalled():
        """Requeue every job nothing is advancing."""
        return {"ok": True, "requeued": manager.requeue_stalled()}

    @app.post("/api/jobs/{job_id}/edit")
    async def edit_job(job_id: int, body: EditJob):
        """Change grain or edition on a job that is already named."""
        with _job_errors():
            manager.edit_job(job_id, body.grain_mode, body.tune_grain, body.edition)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/rename")
    async def rename_job(job_id: int, body: dict = Body(...)):
        """Change the title and year a job will be filed under."""
        year = body.get("year")
        # int() on a JSON list/object raises TypeError, which _job_errors does
        # not map -- so a hand-edited fetch() came back 500 instead of the 400
        # the UI can render. ("abc" was always a 400: that is a ValueError.)
        if year in (None, ""):
            year = None
        else:
            try:
                year = int(year)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400,
                    detail=f"Year must be a number (got {type(year).__name__}).")
        with _job_errors():
            manager.rename_job(job_id, body.get("title", ""), year)
        return {"ok": True}

    @app.get("/api/jobs/{job_id}/aacs-dump")
    async def job_aacs_dump(job_id: int):
        """Download the AACS dump kept for a disc whose key MakeMKV doesn't know,
        so it can be emailed without hunting through MakeMKV's data directory."""
        wj = manager.jobs.get(job_id)
        if wj is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        path = Path(wj.aacs_dump) if wj.aacs_dump else None
        if path is None or not path.exists():
            raise HTTPException(
                status_code=404,
                detail="No AACS dump kept for this job (or it has since been moved).")
        return FileResponse(path, filename=path.name, media_type="application/gzip")

    @app.post("/api/jobs/{job_id}/remap")
    async def remap_job(job_id: int):
        """Start naming over, for a job that never got a mapping at all."""
        # For a job that never got a naming assignment (auto-identify that didn't
        # match, or a failure before naming) — rename has nothing to edit.
        with _job_errors():
            manager.remap(job_id)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/group-season")
    async def group_season(job_id: int):
        """Fold this job's sibling episode files into one job, named once."""
        # N separate episode jobs -> the one-job-per-folder shape a folder scan
        # would have produced, so the season is named once.
        with _job_errors():
            new_id = await manager.group_season(job_id)
        return {"ok": True, "id": new_id}

    @app.post("/api/jobs/{job_id}/change-source")
    async def change_source(job_id: int, body: ChangeSource):
        """Point a job at a different drive or file — e.g. a disc moved to another reader."""
        with _job_errors():
            manager.change_source(job_id, body.source)
        return {"ok": True}

    @app.post("/api/jobs/redo-subs-all")
    async def redo_subs_all():
        """Re-OCR subtitles for every named job, reusing staged subs (no re-encode)."""
        return {"jobs": manager.redo_subtitles_all()}

    @app.post("/api/jobs/{job_id}/redo-subs")
    async def redo_subs(job_id: int):
        """Re-OCR subtitles for one job, reusing staged subs (no re-encode)."""
        with _job_errors():
            manager.redo_subtitles(job_id)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/preview-grain")
    async def preview_grain(job_id: int):
        """Render the grain-on / grain-off preview pair for a job."""
        # The context manager wraps an await here — the old callback form couldn't.
        with _job_errors():
            await manager.generate_preview(job_id)
        return {"ok": True}

    @app.get("/api/jobs/{job_id}/preview/{which}")
    async def preview_image(job_id: int, which: str):
        """One still from the grain preview — `which` is "on" or "off"."""
        wj = manager.jobs.get(job_id)
        path = wj and (wj.preview_on if which == "on" else wj.preview_off)
        if not path or not Path(path).exists():
            raise HTTPException(status_code=404, detail="No preview.")
        return FileResponse(path)

    @app.get("/api/jobs/{job_id}/preview-clip/{which}")
    async def preview_clip(job_id: int, which: str):
        """A short clip from the grain preview — `which` is "on" or "off".

        Served with range support so the player can scrub it."""
        wj = manager.jobs.get(job_id)
        path = wj and (wj.preview_on_clip if which == "on" else wj.preview_off_clip)
        if not path or not Path(path).exists():
            raise HTTPException(status_code=404, detail="No preview clip.")
        # FileResponse honors the Range header (206), so <video> can seek/scrub.
        return FileResponse(path, media_type="video/mp4")

    @app.get("/api/jobs/{job_id}/artwork")
    async def job_artwork(job_id: int):
        """The job's poster (or fanart), for the card thumbnail."""
        path = manager.artwork_path(job_id)
        if not path or not Path(path).exists():
            raise HTTPException(status_code=404, detail="No artwork.")
        return FileResponse(path)

    @app.delete("/api/jobs/{job_id}")
    async def remove_job(job_id: int):
        """Remove a job from the queue.

        Files already placed in the library are left where they are."""
        await manager.remove(job_id)
        return {"ok": True}

    @app.post("/api/jobs/remove-by-state")
    async def remove_by_state(body: RemoveByState):
        """Bulk-remove every job currently in the given states."""
        wanted = set(body.states)
        n = await manager.remove_many(
            lambda wj: manager._live_state(wj).value in wanted)
        return {"removed": n}

    @app.get("/api/maintenance")
    async def get_maintenance():
        """The maintenance window, plus what it means right now.

        Includes when workers stop claiming, when they drain, and which jobs are
        still running — the last being how you tell whether the deadline is
        achievable."""
        return manager.maintenance_window()

    @app.post("/api/maintenance")
    async def set_maintenance(body: dict = Body(...)):
        """Schedule the maintenance window, or clear it with `drain_at: null`.

        Workers stop taking NEW jobs `lead_hours` before `drain_at`, because
        draining only at the deadline would not leave them idle by it."""
        try:
            return manager.set_maintenance(
                body.get("drain_at"), body.get("lead_hours"), body.get("note") or "")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/jobs/{job_id}/merge")
    async def merge_duplicates(job_id: int):
        """Fold other jobs for this same disc into this one and drop them.

        Identified by disc fingerprint, so two resolutions of one film are never
        merged — those are versions, not duplicates."""
        try:
            return await manager.merge_duplicates(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"No job {job_id}.")

    @app.post("/api/jobs/refresh-artwork")
    async def refresh_artwork_all():
        """Re-fetch poster and backdrop from TMDB for every named job."""
        return {"jobs": await manager.refresh_all_artwork()}

    @app.post("/api/jobs/{job_id}/refresh-artwork")
    async def refresh_artwork(job_id: int):
        """Re-fetch poster and backdrop from TMDB for one job."""
        return {"ok": True, "placed": await manager.refresh_artwork(job_id)}

    @app.post("/api/workers/update")
    async def update_workers():
        """Tell every worker to drain, git pull, and restart."""
        try:
            n = manager.request_worker_update()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True, "workers": n}

    @app.post("/api/workers/{worker_id}/action")
    async def worker_action(worker_id: int | str, body: dict = Body(...)):
        """drain, restart or update one worker — or `*` for all of them.

        Every action drains first, so a job in flight is finished and placed
        rather than thrown away."""
        # One named worker, or "*" for all. Every action drains first, so a job in
        # flight is finished and placed rather than thrown away.
        try:
            n = manager.request_worker_action(str(body.get("action", "")), str(worker_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True, "workers": n}

    @app.post("/api/server/restart")
    async def restart_server(body: RestartServer):
        """Restart the server process.

        Answers 409 with the list of running jobs unless `force` is set."""
        srv = getattr(app.state, "uvicorn_server", None)
        if srv is None:
            raise HTTPException(
                status_code=501,
                detail="Restart isn't available (server not launched via `serve`).")
        active = manager.active_jobs()
        if active and not body.force:
            # 409 with the list so the UI can warn and offer to force.
            raise HTTPException(status_code=409, detail={
                "message": f"{len(active)} job(s) are still running — "
                           "restarting will interrupt them.",
                "active": active,
            })
        app.state.restart_requested = True
        app.state.restart_pull = bool(body.pull)   # serve pulls before re-execing

        async def _shutdown_soon():
            await asyncio.sleep(0.4)   # let this response flush first
            srv.should_exit = True     # graceful shutdown frees the port; serve() re-execs

        asyncio.ensure_future(_shutdown_soon())
        return {"ok": True, "restarting": True}

    @app.post("/api/notify/test")
    async def notify_test():
        """Send a test push to the configured notify URL."""
        url = settings.notify_url
        if not url:
            return {"configured": False}
        ok = await send_notify(
            url, "🔔 BluRayRipper test",
            "Test notification — if you see this, notifications are working.", "3")
        return {"configured": True, "ok": ok, "url": url}

    @app.get("/api/opensubtitles/quota")
    async def opensubtitles_quota():
        """Remaining OpenSubtitles download quota for the configured account."""
        if not manager.osub.configured:
            return {"configured": False}
        # Preferred: /infos/user (needs /login, which some API keys can't use).
        try:
            info = await manager.osub.user_info()
            return {
                "configured": True, "source": "account",
                "remaining": info.get("remaining_downloads"),
                "allowed": info.get("allowed_downloads"),
                "used": info.get("downloads_count"),
                "level": info.get("level"), "vip": info.get("vip"),
            }
        except Exception as exc:
            # Fall back to what the last /download reported (API-key auth — always
            # available to us). Populated after the first reference download.
            last = manager.osub.last_known_quota()
            if last is not None:
                return {
                    "configured": True, "source": "last-download",
                    "remaining": last.get("remaining"),
                    "reset_time": last.get("reset_time_utc") or last.get("reset_time"),
                    "note": str(exc),
                }
            return {"configured": True, "error": str(exc)}

    @app.get("/api/handbrake")
    async def get_handbrake():
        """The HandBrake preset currently in effect."""
        from ..handbrake_store import handbrake_ui_payload
        try:
            return handbrake_ui_payload(settings.encode.preset_file, settings.encode.preset_name)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/handbrake")
    async def post_handbrake(updates: dict = Body(...)):
        """Update the HandBrake preset, shared cluster-wide so workers don't drift."""
        from ..handbrake_store import SHARED_KEY, save_handbrake
        try:
            data = save_handbrake(settings.encode.preset_file, settings.encode.preset_name, updates)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        # Share it cluster-wide so every worker picks up the same preset (no drift).
        shared = False
        if manager.store.durable:
            try:
                manager.store.set_shared_config(SHARED_KEY, data)
                shared = True
            except Exception:
                pass  # the local save still succeeded
        return {"ok": True, "restart_required": True, "shared": shared}

    def _parse_plan(text: str):
        """parse_csv, but honouring its "never raises" contract.

        The csv module does raise: one unmatched double quote swallows every
        following line into a single field, and past 128 KB that is a csv.Error.
        Letting it out 500s the box-set editor, which is the only place the bad
        quote can be FIXED -- so report it as a parse error like any other bad
        line and still hand the raw text back.
        """
        from .. import boxset_plan
        try:
            return boxset_plan.parse_csv(text)
        except csv.Error as exc:
            return [], [f"Couldn't read the plan ({exc}). Look for an unmatched "
                        'double quote (") — everything after it is read as one '
                        "field."]

    @app.get("/api/boxsets")
    async def get_boxsets():
        """The box-set naming plan, plus any parse errors in it."""
        from .. import boxset_plan
        text = boxset_plan.load_plan(settings)
        rows, errors = _parse_plan(text)
        return {"csv": text, "rows": [r.as_dict() for r in rows], "errors": errors,
                "columns": list(boxset_plan.COLUMNS), "example": boxset_plan.EXAMPLE_CSV,
                "path": str(boxset_plan.plan_path(settings))}

    @app.post("/api/boxsets")
    async def post_boxsets(body: dict = Body(...)):
        """Replace the box-set plan. `dry_run` parses and reports without saving."""
        # dry_run parses and reports without writing, so the UI can preview exactly
        # what a paste would do before it takes effect on the next disc inserted.
        from .. import boxset_plan
        text = body.get("csv", "")
        if not isinstance(text, str):
            raise HTTPException(status_code=400, detail="csv must be text.")
        rows, errors = _parse_plan(text)
        dry_run = bool(body.get("dry_run"))
        if not dry_run:
            try:
                boxset_plan.save_plan(settings, text, manager.store)
            except OSError as exc:
                raise HTTPException(status_code=400, detail=f"Couldn't save the plan: {exc}")
        return {"ok": True, "saved": not dry_run, "errors": errors,
                "rows": [r.as_dict() for r in rows]}

    @app.get("/api/settings")
    async def get_settings(scope: str = "global"):
        """Settings for a scope: `global`, or a worker id for that box's overrides."""
        return settings_ui_payload(settings.config_dir, scope, manager.store)

    @app.post("/api/settings/notify-test")
    async def notify_test():
        """Send a test push using the settings just SAVED, not the ones in memory."""
        # Reload rather than using the settings this process started with: the
        # point is to test what you JUST saved, and saved settings otherwise only
        # take effect on the next restart.
        from ..config import load_settings as _load
        from ..services.notify import notify as _notify

        fresh = _load()
        if not fresh.notify_url:
            return {"ok": False, "detail": "No ntfy URL is set. Save one first."}
        sent = await _notify(
            fresh.notify_url, "✅ BluRayRipper test",
            "If you can read this, alerts are working. Sent from the Settings page.",
            fresh.notify_priority)
        return {"ok": bool(sent),
                "detail": (f"Delivered to {fresh.notify_url} at priority "
                           f"'{fresh.notify_priority}'." if sent else
                           f"The POST to {fresh.notify_url} did not succeed. Check "
                           "the URL and that this machine can reach it.")}

    @app.post("/api/settings")
    async def post_settings(updates: dict = Body(...), scope: str = "global"):
        """Save settings for a scope: `global`, or a worker id for one box."""
        # `bool(scope) and ...` is what settings_store uses to route the save.
        # Without the truthiness half, `?scope=` (what the UI sends before its
        # worker dropdown has loaded) edited the SERVER's overlay while the
        # reply said "per-worker, applies next job" — so the operator waited for
        # a change that needed a restart.
        is_worker = bool(scope) and scope not in ("global", "server")
        if is_worker and getattr(manager.store, "durable", False):
            # A typo'd worker id would otherwise create a worker_settings row
            # that no worker ever reads, and the Settings page lists every row
            # forever — so the phantom name outlives the typo. request_worker_
            # action already refuses an unknown target for the same reason.
            known, checked = set(), True
            try:
                known |= {w.worker_id for w in manager.store.list_workers()}
                known |= set(manager.store.all_worker_settings().keys())
            except Exception:
                checked = False  # can't tell right now — don't block the save
            if checked and scope not in known:
                raise HTTPException(
                    status_code=400,
                    detail=f"No worker named “{scope}” has checked in. Pick one "
                           "from the worker list — a box appears there once it "
                           "has connected.")
        try:
            save_settings(settings.config_dir, updates, scope, manager.store)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {
            "ok": True,
            "scope": scope if is_worker else "global",
            # The server applies on restart; a worker picks up its override when
            # it starts its next job (no restart needed).
            "restart_required": not is_worker,
            "applies": "next job" if is_worker else "restart",
        }

    @app.post("/api/settings/clear")
    async def clear_worker_settings(body: dict = Body(...)):
        """Drop a worker's overrides so it follows the shared settings again."""
        scope = (body or {}).get("scope")
        if not scope or scope in ("global", "server"):
            raise HTTPException(status_code=400, detail="Clear applies to a worker scope only.")
        store = manager.store
        if store is None or not getattr(store, "durable", False):
            raise HTTPException(
                status_code=400, detail="Per-worker settings need a shared Postgres store.")
        store.set_worker_settings(scope, {})  # empty overlay deletes the row
        return {"ok": True, "cleared": scope}

    @app.get("/api/tmdb/search")
    async def tmdb_search(kind: str, query: str):
        """Search TMDB for a movie or show, with disc-label noise stripped out."""
        if not tmdb.configured:
            raise HTTPException(status_code=400, detail="TMDB API key not configured.")
        # Disc names carry noise TMDB can't match — a trailing "(YYYY)" and, for
        # TV, "Season N Disc N". Strip it to the bare title (+year filter).
        cleaned, year, _season = naming.clean_tmdb_query(query, kind)
        try:
            if kind == "movie":
                res = await tmdb.search_movie(cleaned, year=year)
                return [{"id": c.tmdb_id, "title": c.title, "year": c.year} for c in res]
            res = await tmdb.search_tv(cleaned)
            return [{"id": c.tmdb_id, "title": c.name, "year": c.year} for c in res]
        except MetadataError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # network/DNS/HTTP errors reaching TMDB
            raise HTTPException(status_code=502, detail=f"TMDB request failed: {type(exc).__name__}: {exc}")

    @app.get("/api/tmdb/movie/{tmdb_id}/runtime")
    async def tmdb_movie_runtime(tmdb_id: int):
        """The film's canonical runtime in minutes, for telling two cuts apart.

        TMDB carries ONE runtime per film — the theatrical release in nearly
        every case — not one per edition. So this says which of a disc's
        feature-length titles is the theatrical cut; it cannot say whether the
        other is unrated, extended or a director's cut.
        """
        if not tmdb.configured:
            raise HTTPException(status_code=400, detail="TMDB API key not configured.")
        try:
            return {"tmdb_id": tmdb_id, "runtime": await tmdb.movie_runtime(tmdb_id)}
        except MetadataError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=502,
                                detail=f"TMDB request failed: {type(exc).__name__}: {exc}")

    @app.get("/api/tmdb/season")
    async def tmdb_season(tv_id: int, season: int):
        """The episode list for one season, from TMDB."""
        try:
            eps = await tmdb.get_season(tv_id, season)
            return [{"episode": e.episode_number, "name": e.name, "runtime": e.runtime} for e in eps]
        except MetadataError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"TMDB request failed: {type(exc).__name__}: {exc}")

    @app.get("/api/events")
    async def events(request: Request):
        """Server-sent events: the queue snapshot, pushed whenever it changes.

        The dashboard's live feed. Same payload as `GET /api/jobs`."""
        async def stream():
            q = manager.bus.subscribe()
            try:
                first = _safe_sse(manager)
                if first:
                    yield first
                while not manager.closing:
                    if await request.is_disconnected():
                        break  # client went away — stop before writing to a dead socket
                    try:
                        await asyncio.wait_for(q.get(), timeout=15)
                        while not q.empty():
                            q.get_nowait()
                    except asyncio.TimeoutError:
                        pass  # heartbeat
                    if manager.closing or await request.is_disconnected():
                        break
                    msg = _safe_sse(manager)
                    if msg:
                        yield msg
            finally:
                manager.bus.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _safe_sse(manager) -> str | None:
    """Serialize a snapshot to an SSE frame; never raise (which would truncate
    the stream and cause a client JSON parse error). Returns None on failure."""
    try:
        return _sse(manager.snapshot())
    except Exception as exc:
        print(f"[sse] snapshot serialize failed: {type(exc).__name__}: {exc}")
        return None
