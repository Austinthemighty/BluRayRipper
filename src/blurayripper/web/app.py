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
  GET  /api/events            -> SSE stream of queue snapshots
"""

from __future__ import annotations

import asyncio
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
            print(f"Job recovery skipped: {exc}")
        manager.start_drive_watch()  # background drive polling + auto-open
        manager.start_store_refresh()  # reflect remote worker progress (clustered)

    @app.on_event("shutdown")
    async def _shutdown():
        await manager.shutdown()

    @app.get("/")
    async def index():
        # No-cache so a pulled+restarted server always serves the latest UI
        # (otherwise the browser keeps running the old cached page).
        return FileResponse(
            _STATIC / "index.html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.get("/manifest.webmanifest")
    async def manifest():
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
            }
            for d in found
        ]

    @app.post("/api/drives/name")
    async def name_drive(body: NameDrive):
        manager.set_drive_name(body.device, body.name)
        return {"ok": True}

    @app.post("/api/drives/eject")
    async def drive_eject(body: DriveDevice):
        try:
            await asyncio.to_thread(eject, body.device)  # off-thread: ctypes/MCI can block
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Eject failed: {exc}")
        return {"ok": True}

    @app.post("/api/drives/close")
    async def drive_close(body: DriveDevice):
        try:
            await asyncio.to_thread(close_tray, body.device)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Close tray failed: {exc}")
        return {"ok": True}

    @app.get("/api/jobs")
    async def jobs():
        return manager.snapshot()

    @app.post("/api/jobs")
    async def add_job(body: AddDisc):
        try:
            ids = manager.add_source(body.source)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ids": ids}

    def _gate(fn):
        try:
            fn()
        except KeyError:
            raise HTTPException(status_code=404, detail="Job not found.")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/jobs/{job_id}/titles")
    async def submit_titles(job_id: int, body: SetTitles):
        _gate(lambda: manager.set_titles(job_id, body.title_ids))
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/mapping")
    async def submit_mapping(job_id: int, body: SetMapping):
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
        _gate(lambda: manager.set_mapping(job_id, assignments, body.grain_mode, body.tune_grain))
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/grain")
    async def submit_grain(job_id: int, body: SetGrain):
        _gate(lambda: manager.set_grain(job_id, body.tune_grain))
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/auto-identify")
    async def auto_identify(job_id: int, body: AutoIdentify):
        _gate(lambda: manager.set_auto_identify(
            job_id, body.show_title, body.show_year, body.show_tmdb_id, body.season,
            body.episodes, body.title_ids, body.grain_mode, body.tune_grain))
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/confirm-match")
    async def confirm_match(job_id: int, body: ConfirmMatch):
        _gate(lambda: manager.confirm_match(job_id, {int(k): v for k, v in body.mapping.items()}))
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/retry")
    async def retry_job(job_id: int):
        _gate(lambda: manager.retry(job_id))
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/edit")
    async def edit_job(job_id: int, body: EditJob):
        _gate(lambda: manager.edit_job(job_id, body.grain_mode, body.tune_grain, body.edition))
        return {"ok": True}

    @app.post("/api/jobs/redo-subs-all")
    async def redo_subs_all():
        return {"jobs": manager.redo_subtitles_all()}

    @app.post("/api/jobs/{job_id}/redo-subs")
    async def redo_subs(job_id: int):
        _gate(lambda: manager.redo_subtitles(job_id))
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/preview-grain")
    async def preview_grain(job_id: int):
        try:
            await manager.generate_preview(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Job not found.")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True}

    @app.get("/api/jobs/{job_id}/preview/{which}")
    async def preview_image(job_id: int, which: str):
        wj = manager.jobs.get(job_id)
        path = wj and (wj.preview_on if which == "on" else wj.preview_off)
        if not path or not Path(path).exists():
            raise HTTPException(status_code=404, detail="No preview.")
        return FileResponse(path)

    @app.get("/api/jobs/{job_id}/preview-clip/{which}")
    async def preview_clip(job_id: int, which: str):
        wj = manager.jobs.get(job_id)
        path = wj and (wj.preview_on_clip if which == "on" else wj.preview_off_clip)
        if not path or not Path(path).exists():
            raise HTTPException(status_code=404, detail="No preview clip.")
        # FileResponse honors the Range header (206), so <video> can seek/scrub.
        return FileResponse(path, media_type="video/mp4")

    @app.get("/api/jobs/{job_id}/artwork")
    async def job_artwork(job_id: int):
        path = manager.artwork_path(job_id)
        if not path or not Path(path).exists():
            raise HTTPException(status_code=404, detail="No artwork.")
        return FileResponse(path)

    @app.delete("/api/jobs/{job_id}")
    async def remove_job(job_id: int):
        await manager.remove(job_id)
        return {"ok": True}

    @app.post("/api/jobs/remove-by-state")
    async def remove_by_state(body: RemoveByState):
        wanted = set(body.states)
        n = await manager.remove_many(
            lambda wj: manager._live_state(wj).value in wanted)
        return {"removed": n}

    @app.post("/api/jobs/refresh-artwork")
    async def refresh_artwork_all():
        return {"jobs": await manager.refresh_all_artwork()}

    @app.post("/api/jobs/{job_id}/refresh-artwork")
    async def refresh_artwork(job_id: int):
        return {"ok": True, "placed": await manager.refresh_artwork(job_id)}

    @app.post("/api/server/restart")
    async def restart_server(body: RestartServer):
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

        async def _shutdown_soon():
            await asyncio.sleep(0.4)   # let this response flush first
            srv.should_exit = True     # graceful shutdown frees the port; serve() re-execs

        asyncio.ensure_future(_shutdown_soon())
        return {"ok": True, "restarting": True}

    @app.post("/api/notify/test")
    async def notify_test():
        url = settings.notify_url
        if not url:
            return {"configured": False}
        ok = await send_notify(
            url, "🔔 BluRayRipper test",
            "Test notification — if you see this, notifications are working.", "3")
        return {"configured": True, "ok": ok, "url": url}

    @app.get("/api/opensubtitles/quota")
    async def opensubtitles_quota():
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

    @app.get("/api/settings")
    async def get_settings(scope: str = "global"):
        return settings_ui_payload(settings.config_dir, scope, manager.store)

    @app.post("/api/settings")
    async def post_settings(updates: dict = Body(...), scope: str = "global"):
        try:
            save_settings(settings.config_dir, updates, scope, manager.store)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        is_worker = scope not in ("global", "server")
        return {
            "ok": True,
            "scope": scope,
            # The server applies on restart; a worker picks up its override when
            # it starts its next job (no restart needed).
            "restart_required": not is_worker,
            "applies": "next job" if is_worker else "restart",
        }

    @app.get("/api/tmdb/search")
    async def tmdb_search(kind: str, query: str):
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

    @app.get("/api/tmdb/season")
    async def tmdb_season(tv_id: int, season: int):
        try:
            eps = await tmdb.get_season(tv_id, season)
            return [{"episode": e.episode_number, "name": e.name, "runtime": e.runtime} for e in eps]
        except MetadataError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"TMDB request failed: {type(exc).__name__}: {exc}")

    @app.get("/api/events")
    async def events(request: Request):
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
