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

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ..config import Settings, load_settings
from ..models import EpisodeAssignment, MediaKind
from ..services.metadata import MetadataError, TMDBClient
from .manager import JobManager

_STATIC = Path(__file__).parent / "static"


class AddDisc(BaseModel):
    source: str


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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="BluRayRipper")
    manager = JobManager(settings)
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
                    status_code=503, detail="Drive scan timed out (system busy) — will retry.")
        return [
            {
                "index": d.index, "name": d.name, "device": d.device,
                "disc_name": d.disc_name, "label": manager.drive_names.get(d.device),
            }
            for d in found
        ]

    @app.post("/api/drives/name")
    async def name_drive(body: NameDrive):
        manager.set_drive_name(body.device, body.name)
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

    @app.delete("/api/jobs/{job_id}")
    async def remove_job(job_id: int):
        await manager.remove(job_id)
        return {"ok": True}

    @app.get("/api/tmdb/search")
    async def tmdb_search(kind: str, query: str):
        if not tmdb.configured:
            raise HTTPException(status_code=400, detail="TMDB API key not configured.")
        # Disc names often carry a trailing "(YYYY)" — TMDB matches the bare
        # title better, so pull the year out and use it as the year filter.
        cleaned, year = _clean_query(query)
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
    async def events():
        async def stream():
            q = manager.bus.subscribe()
            try:
                first = _safe_sse(manager)
                if first:
                    yield first
                while not manager.closing:
                    try:
                        await asyncio.wait_for(q.get(), timeout=15)
                        while not q.empty():
                            q.get_nowait()
                    except asyncio.TimeoutError:
                        pass  # heartbeat
                    if manager.closing:
                        break
                    msg = _safe_sse(manager)
                    if msg:
                        yield msg
            finally:
                manager.bus.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


def _clean_query(query: str) -> tuple[str, int | None]:
    """Strip a trailing '(YYYY)' from a search query and return (title, year)."""
    import re

    m = re.search(r"\((\d{4})\)\s*$", query)
    year = int(m.group(1)) if m else None
    cleaned = re.sub(r"\s*\(\d{4}\)\s*$", "", query).strip()
    return (cleaned or query.strip()), year


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
