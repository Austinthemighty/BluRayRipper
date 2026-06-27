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


class SetMapping(BaseModel):
    assignments: list[AssignmentIn]
    grain_mode: str = "auto"      # "auto" | "on" | "off" | "preview"
    tune_grain: bool | None = None


class SetGrain(BaseModel):
    tune_grain: bool


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

    @app.get("/")
    async def index():
        return FileResponse(_STATIC / "index.html")

    @app.get("/api/drives")
    async def drives():
        from ..config import ToolNotFoundError

        try:
            found = await manager.list_drives()
        except ToolNotFoundError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except (TimeoutError, asyncio.TimeoutError):
            raise HTTPException(status_code=503, detail="Drive scan busy (rip in progress) — will retry.")
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
        wj = manager.add_disc(body.source)
        return {"id": wj.id}

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

    @app.post("/api/jobs/{job_id}/retry")
    async def retry_job(job_id: int):
        _gate(lambda: manager.retry(job_id))
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
        try:
            if kind == "movie":
                res = await tmdb.search_movie(query)
                return [{"id": c.tmdb_id, "title": c.title, "year": c.year} for c in res]
            res = await tmdb.search_tv(query)
            return [{"id": c.tmdb_id, "title": c.name, "year": c.year} for c in res]
        except MetadataError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/api/tmdb/season")
    async def tmdb_season(tv_id: int, season: int):
        try:
            eps = await tmdb.get_season(tv_id, season)
            return [{"episode": e.episode_number, "name": e.name} for e in eps]
        except MetadataError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/api/events")
    async def events():
        async def stream():
            q = manager.bus.subscribe()
            try:
                yield _sse(manager.snapshot())
                while True:
                    try:
                        await asyncio.wait_for(q.get(), timeout=15)
                        while not q.empty():
                            q.get_nowait()
                    except asyncio.TimeoutError:
                        pass  # heartbeat
                    yield _sse(manager.snapshot())
            finally:
                manager.bus.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"
