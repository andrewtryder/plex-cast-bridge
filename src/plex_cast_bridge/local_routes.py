"""Dashboard UI, static assets, and activity for the local control API."""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from .catalog_store import PROJECT_ROOT
from . import activity

ICON_PATH = PROJECT_ROOT / "assets" / "icon.png"
ICON_144_PATH = PROJECT_ROOT / "assets" / "icon-144.png"
DASHBOARD_TEMPLATE = Path(__file__).resolve().parent / "templates" / "dashboard.html"

router = APIRouter()


@router.get("/api/activity")
async def recent_activity():
    return {"events": activity.recent()}


@router.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok"}


@router.get("/assets/icon.png", include_in_schema=False)
async def serve_icon():
    if ICON_PATH.exists():
        return FileResponse(ICON_PATH, media_type="image/png")
    raise HTTPException(status_code=404, detail="Icon not found")


@router.get("/assets/icon-144.png", include_in_schema=False)
async def serve_icon_144():
    if ICON_144_PATH.exists():
        return FileResponse(
            ICON_144_PATH,
            media_type="image/png",
            filename="plex-cast-bridge-icon-144.png",
        )
    raise HTTPException(status_code=404, detail="Icon not found")


@router.get("/assets/tutorials/manifest.json", include_in_schema=False)
async def serve_tutorial_manifest():
    path = PROJECT_ROOT / "assets" / "tutorials" / "manifest.json"
    if path.exists():
        return FileResponse(path, media_type="application/json")
    raise HTTPException(status_code=404, detail="Tutorial manifest not found")


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard_ui():
    if DASHBOARD_TEMPLATE.exists():
        return HTMLResponse(DASHBOARD_TEMPLATE.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Plex Cast Bridge Dashboard template missing</h1>")
