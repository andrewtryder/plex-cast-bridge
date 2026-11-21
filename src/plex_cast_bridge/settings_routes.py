"""Dashboard settings routes.

This router deliberately owns only mutable local-dashboard settings.  It has
no dependency on playback or speaker routing.
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .app_settings_store import (
    apply_to_settings,
    load as load_app_settings,
    public_dict,
    save as save_app_settings,
)

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/settings")
async def get_settings(request: Request):
    """Public non-secret settings + library summary for Settings → Playback."""
    settings = request.app.state.settings
    stored = public_dict(load_app_settings())
    volumes = {
        "default_cast_volume": settings.default_cast_volume,
        "max_cast_volume": settings.max_cast_volume,
        "night_cast_volume": settings.night_cast_volume,
    }
    plex = getattr(request.app.state, "plex", None)
    if plex is None:
        library = {
            "linked": False,
            "section": None,
            "artists": None,
            "albums": None,
            "tracks": None,
        }
    else:
        try:
            library = await run_in_threadpool(plex.library_summary)
        except Exception as exc:
            log.warning("library_summary failed: %s", exc)
            library = {
                "linked": True,
                "section": getattr(settings, "plex_music_section", None),
                "artists": None,
                "albums": None,
                "tracks": None,
            }
    return {"volumes": volumes, "stored": stored, "library": library}


@router.patch("/api/settings")
async def patch_settings(request: Request):
    """Update editable volumes; persist to data/app_settings.json and apply live."""
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")

    patch: dict = {}
    for key in ("default_cast_volume", "max_cast_volume", "night_cast_volume"):
        if key not in body:
            continue
        val = body[key]
        if val is None and key == "night_cast_volume":
            patch[key] = None
            continue
        try:
            patch[key] = float(val)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"{key} must be a number") from exc
        if not 0.0 <= patch[key] <= 1.0:
            raise HTTPException(status_code=400, detail=f"{key} must be between 0 and 1")

    if not patch:
        raise HTTPException(status_code=400, detail="No editable settings provided")

    settings = request.app.state.settings
    merged_default = patch.get("default_cast_volume", settings.default_cast_volume)
    merged_max = patch.get("max_cast_volume", settings.max_cast_volume)
    if merged_default > merged_max:
        raise HTTPException(
            status_code=400, detail="default_cast_volume cannot exceed max_cast_volume"
        )

    current_volumes = {
        "default_cast_volume": settings.default_cast_volume,
        "max_cast_volume": settings.max_cast_volume,
        "night_cast_volume": settings.night_cast_volume,
    }
    stored = await run_in_threadpool(save_app_settings, {**current_volumes, **patch})
    apply_to_settings(settings, stored)
    cast = getattr(request.app.state, "cast", None)
    if cast is not None:
        cast.default_volume = settings.default_cast_volume
        cast.max_volume = settings.max_cast_volume
        with cast.lock:
            for speaker in cast.speakers.values():
                if hasattr(speaker, "default_volume") and hasattr(speaker, "max_volume"):
                    speaker.default_volume = settings.default_cast_volume
                    speaker.max_volume = settings.max_cast_volume

    return await get_settings(request)
