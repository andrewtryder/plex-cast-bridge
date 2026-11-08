"""HTTP routes for the local Plex Cast Bridge control API."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from starlette.concurrency import run_in_threadpool

from .api_auth import verify_local_api
from .api_models import Command, SpeakerTest, SpeakerToggle, Transport
from .catalog_store import (
    PROJECT_ROOT,
    persist_discovered_speakers_to_catalog,
)
from .playback_service import play, transport

log = logging.getLogger(__name__)

ICON_PATH = PROJECT_ROOT / "assets" / "icon.png"
DASHBOARD_TEMPLATE = Path(__file__).resolve().parent / "templates" / "dashboard.html"

router = APIRouter()

PLEX_REQUIRED_DETAIL = "Plex is not linked. Connect with Plex in Settings, or set PLEX_TOKEN."


def _require_plex(request: Request):
    plex = getattr(request.app.state, "plex", None)
    if plex is None:
        raise HTTPException(status_code=503, detail=PLEX_REQUIRED_DETAIL)
    return plex


@router.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok"}


@router.get("/assets/icon.png", include_in_schema=False)
async def serve_icon():
    if ICON_PATH.exists():
        return FileResponse(ICON_PATH, media_type="image/png")
    raise HTTPException(status_code=404, detail="Icon not found")


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard_ui():
    if DASHBOARD_TEMPLATE.exists():
        return HTMLResponse(DASHBOARD_TEMPLATE.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Plex Cast Bridge Dashboard template missing</h1>")


@router.post("/api/command", dependencies=[Depends(verify_local_api)])
async def command_endpoint(request: Request, command: Command):
    state = request.app.state
    plex = _require_plex(request)
    try:
        return await play(
            dj=state.dj,
            plex=plex,
            cast=state.cast,
            settings=state.settings,
            command=command,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("Command failed for query=%r speaker=%r", command.query, command.speaker)
        raise HTTPException(status_code=500, detail=f"Playback failed: {exc}") from exc


@router.get("/api/library/tracks", dependencies=[Depends(verify_local_api)])
async def library_tracks(request: Request, query: str = "", limit: int = 25):
    plex = _require_plex(request)
    if not 1 <= limit <= 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100")

    def search():
        kwargs = {"libtype": "track", "maxresults": limit}
        if query:
            kwargs["title"] = query
        tracks = plex.library.search(**kwargs)
        return [
            {
                "title": t.title,
                "artist": getattr(t, "grandparentTitle", ""),
                "album": getattr(t, "parentTitle", ""),
            }
            for t in tracks
        ]

    return await run_in_threadpool(search)


@router.post("/api/speakers/test", dependencies=[Depends(verify_local_api)])
async def test_speaker_endpoint(request: Request, command: SpeakerTest):
    state = request.app.state
    plex = _require_plex(request)

    def execute():
        tracks = (
            plex.library.search(title=command.query, libtype="track")
            if command.query
            else plex.library.search(libtype="track", maxresults=1)
        )
        if not tracks:
            raise ValueError("No matching tracks found in the Plex library")
        speaker = state.cast.get(command.speaker or state.settings.default_speaker_name)
        speaker.replace([tracks[0]], repeat=False)
        return speaker.status()

    try:
        return await run_in_threadpool(execute)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/transport", dependencies=[Depends(verify_local_api)])
async def transport_endpoint(request: Request, command: Transport):
    state = request.app.state
    try:
        return await transport(
            cast=state.cast,
            settings=state.settings,
            command=command,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("Transport '%s' failed for speaker=%r", command.action, command.speaker)
        raise HTTPException(status_code=500, detail=f"Transport failed: {exc}") from exc


@router.get("/api/speakers", dependencies=[Depends(verify_local_api)])
async def speakers_endpoint(request: Request, scan: bool = False):
    state = request.app.state

    def get_list():
        if scan or not getattr(state.cast, "last_discovered", None):
            discovered = state.cast.discover()
            persist_discovered_speakers_to_catalog(discovered, state.cast)
            return discovered
        for dev in state.cast.last_discovered:
            dev["enabled"] = not state.cast.is_disabled(dev["name"])
        return list(state.cast.last_discovered)

    try:
        return await run_in_threadpool(get_list)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Catalog persistence failed: {exc}") from exc


@router.post("/api/speakers/toggle", dependencies=[Depends(verify_local_api)])
async def toggle_speaker_endpoint(request: Request, toggle: SpeakerToggle):
    state = request.app.state

    def update():
        previous_disabled = state.cast.is_disabled(toggle.speaker)
        state.cast.set_speaker_enabled(toggle.speaker, toggle.enabled)
        try:
            discovered = getattr(state.cast, "last_discovered", None) or state.cast.discover()
            for dev in discovered:
                dev["enabled"] = not state.cast.is_disabled(dev["name"])
            persist_discovered_speakers_to_catalog(discovered, state.cast)
        except Exception:
            state.cast.set_speaker_enabled(toggle.speaker, not previous_disabled)
            raise
        return {
            "speaker": toggle.speaker,
            "enabled": toggle.enabled,
            "speakers": discovered,
        }

    try:
        return await run_in_threadpool(update)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Catalog persistence failed: {exc}") from exc


@router.get("/api/status", dependencies=[Depends(verify_local_api)])
async def status_endpoint(request: Request):
    state = request.app.state
    return {name: speaker.status() for name, speaker in list(state.cast.speakers.items())}


@router.post("/api/library/refresh", dependencies=[Depends(verify_local_api)])
async def refresh_endpoint(request: Request):
    plex = _require_plex(request)
    return await run_in_threadpool(plex.refresh)


@router.get("/api/sync-catalog", dependencies=[Depends(verify_local_api)])
async def catalog_endpoint(request: Request):
    plex = _require_plex(request)
    catalog = plex.catalog
    return {"genres": catalog["genres"], "artists": catalog["artists"][:100]}
