"""Plex account linking and library selection routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from .api_auth import verify_local_api
from .api_models import PlexSelect

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/plex", dependencies=[Depends(verify_local_api)])


def _require_auth(request: Request):
    auth = getattr(request.app.state, "plex_auth", None)
    if auth is None:
        raise HTTPException(status_code=503, detail="Plex auth service is unavailable")
    return auth


@router.get("/status")
async def plex_status(request: Request):
    auth = _require_auth(request)
    return auth.public_status(request.app.state.plex)


@router.post("/connect/start")
async def plex_connect_start(request: Request):
    auth = _require_auth(request)

    def start():
        forward = str(request.base_url).rstrip("/") + "/#settings"
        return auth.start_connect(forward_url=forward)

    try:
        return await run_in_threadpool(start)
    except Exception as exc:
        log.exception("Plex connect start failed")
        raise HTTPException(status_code=500, detail=f"Could not start Plex login: {exc}") from exc


@router.get("/connect/status")
async def plex_connect_status(request: Request):
    auth = _require_auth(request)
    return await run_in_threadpool(auth.connect_status)


@router.get("/servers")
async def plex_servers(request: Request):
    auth = _require_auth(request)
    try:
        return await run_in_threadpool(auth.list_servers)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/sections")
async def plex_sections(request: Request, server_id: str = Query(min_length=1)):
    auth = _require_auth(request)
    try:
        return await run_in_threadpool(auth.list_sections, server_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/select")
async def plex_select(request: Request, body: PlexSelect):
    auth = _require_auth(request)
    try:
        plex = await run_in_threadpool(auth.select_server, body.server_id, body.section)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("Plex server select failed")
        raise HTTPException(status_code=500, detail=f"Could not connect to Plex server: {exc}") from exc
    request.app.state.plex = plex
    return auth.public_status(plex)


@router.post("/disconnect")
async def plex_disconnect(request: Request):
    auth = _require_auth(request)
    await run_in_threadpool(auth.disconnect)
    request.app.state.plex = None
    return auth.public_status(None)


@router.post("/refresh")
async def plex_refresh(request: Request):
    auth = _require_auth(request)
    plex = request.app.state.plex
    if plex is None:
        raise HTTPException(status_code=503, detail="Plex is not linked. Connect with Plex in Settings.")
    try:
        catalog = await run_in_threadpool(auth.refresh_library, plex)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"status": "ok", "artists": len(catalog.get("artists", [])), "genres": len(catalog.get("genres", []))}
