"""HTTP routes for the local Plex Cast Bridge control API."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from .api_auth import verify_local_api
from .api_models import Command, LibraryPlay, QueueMutate, SpeakerTest, SpeakerToggle, Transport
from .app_settings_store import apply_to_settings, load as load_app_settings, public_dict, save as save_app_settings
from .catalog_store import (
    PROJECT_ROOT,
    persist_discovered_speakers_to_catalog,
)
from .playback_service import play, transport
from .speaker_queue import track_art_proxy_path

log = logging.getLogger(__name__)

ICON_PATH = PROJECT_ROOT / "assets" / "icon.png"
ICON_144_PATH = PROJECT_ROOT / "assets" / "icon-144.png"
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


@router.get("/assets/icon-144.png", include_in_schema=False)
async def serve_icon_144():
    if ICON_144_PATH.exists():
        return FileResponse(
            ICON_144_PATH,
            media_type="image/png",
            filename="plex-cast-bridge-icon-144.png",
        )
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


def _annotate_speakers(discovered: list[dict]) -> list[dict]:
    for dev in discovered:
        model = str(dev.get("model") or "")
        kind = "group" if "group" in model.lower() else "speaker"
        dev["kind"] = kind
        dev["is_group"] = kind == "group"
    return discovered


@router.get("/api/speakers", dependencies=[Depends(verify_local_api)])
async def speakers_endpoint(request: Request, scan: bool = False):
    state = request.app.state

    def get_list():
        if scan or not getattr(state.cast, "last_discovered", None):
            discovered = state.cast.discover()
            persist_discovered_speakers_to_catalog(discovered, state.cast)
            return _annotate_speakers(discovered)
        for dev in state.cast.last_discovered:
            dev["enabled"] = not state.cast.is_disabled(dev["name"])
        return _annotate_speakers(list(state.cast.last_discovered))

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


@router.get("/api/artwork/{rating_key}", dependencies=[Depends(verify_local_api)])
async def artwork_proxy(request: Request, rating_key: str):
    """Proxy Plex thumbnails without exposing tokens to the browser (SSRF-hardened)."""
    plex = _require_plex(request)

    def fetch():
        from urllib.parse import urlparse
        import urllib.request

        try:
            key = int(rating_key)
        except ValueError as exc:
            raise ValueError("Invalid rating key") from exc
        item = plex.server.fetchItem(key)
        thumb = getattr(item, "thumbUrl", None)
        if not thumb and getattr(item, "thumb", None):
            thumb = plex.server.url(item.thumb, includeToken=True)
        if not thumb:
            raise ValueError("No artwork")
        parsed = urlparse(thumb)
        plex_host = urlparse(plex.server._baseurl).hostname  # noqa: SLF001
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Unsupported artwork URL")
        if parsed.hostname != plex_host:
            raise ValueError("Artwork host not allowed")
        req = urllib.request.Request(thumb, method="GET")
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 — host allowlisted
            content_type = resp.headers.get("Content-Type", "image/jpeg")
            if not str(content_type).startswith("image/"):
                raise ValueError("Unexpected content type")
            return resp.read(), content_type

    try:
        data, content_type = await run_in_threadpool(fetch)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Artwork unavailable: {exc}") from exc
    return Response(content=data, media_type=content_type)


@router.get("/api/queue", dependencies=[Depends(verify_local_api)])
async def queue_get(request: Request, speaker: str | None = None):
    state = request.app.state
    name = speaker or state.settings.default_speaker_name
    spk = state.cast.get(name)
    status = spk.status()
    return {
        "speaker": name,
        "current": status.get("current_track"),
        "up_next": getattr(spk, "queue_snapshot", lambda: status.get("up_next") or [])(),
        "queue_length": status.get("queue_length", 0),
    }


@router.post("/api/queue", dependencies=[Depends(verify_local_api)])
async def queue_mutate(request: Request, body: QueueMutate):
    state = request.app.state
    plex = _require_plex(request)
    name = body.speaker or state.settings.default_speaker_name
    spk = state.cast.get(name)

    def run():
        if body.action == "clear":
            n = spk.clear_upcoming() if hasattr(spk, "clear_upcoming") else 0
            return {"cleared": n, "status": spk.status()}
        if body.action == "remove":
            if body.index is None:
                raise ValueError("index required")
            removed = spk.remove_at(body.index)
            return {"removed": removed, "status": spk.status()}
        if body.action == "move":
            if body.index is None or body.to_index is None:
                raise ValueError("index and to_index required")
            spk.move(body.index, body.to_index)
            return {"status": spk.status()}
        if body.action in {"play_next", "add_end"}:
            if body.rating_key is None:
                raise ValueError("rating_key required")
            track = plex.server.fetchItem(int(body.rating_key))
            if body.action == "play_next":
                spk.play_next(track)
            else:
                spk.add_end(track)
            return {"status": spk.status()}
        raise ValueError(f"unsupported action {body.action}")

    try:
        return await run_in_threadpool(run)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/library/browse", dependencies=[Depends(verify_local_api)])
async def library_browse(
    request: Request,
    kind: str = "artists",
    query: str = "",
    limit: int = 40,
    offset: int = 0,
):
    plex = _require_plex(request)
    limit = max(1, min(100, limit))
    offset = max(0, offset)

    def browse():
        section = plex.library
        items = []
        if kind == "genres":
            choices = []
            try:
                choices = list(section.listFilterChoices("genre"))
            except Exception:
                choices = [{"title": g} for g in (plex.catalog or {}).get("genres", [])]
            for g in choices[offset : offset + limit]:
                items.append({"title": getattr(g, "title", g if isinstance(g, str) else str(g)), "type": "genre"})
            return {"kind": kind, "items": items, "offset": offset, "limit": limit}
        if kind == "artists":
            kwargs = {"libtype": "artist", "maxresults": offset + limit}
            if query:
                kwargs["title"] = query
            artists = plex.library.search(**kwargs)[offset : offset + limit]
            for a in artists:
                items.append(
                    {
                        "rating_key": getattr(a, "ratingKey", None),
                        "title": a.title,
                        "type": "artist",
                        "art": track_art_proxy_path(a),
                    }
                )
            return {"kind": kind, "items": items, "offset": offset, "limit": limit}
        if kind == "albums":
            kwargs = {"libtype": "album", "maxresults": offset + limit}
            if query:
                kwargs["title"] = query
            albums = plex.library.search(**kwargs)[offset : offset + limit]
            for a in albums:
                items.append(
                    {
                        "rating_key": getattr(a, "ratingKey", None),
                        "title": a.title,
                        "artist": getattr(a, "parentTitle", ""),
                        "type": "album",
                        "art": track_art_proxy_path(a),
                    }
                )
            return {"kind": kind, "items": items, "offset": offset, "limit": limit}
        # tracks
        kwargs = {"libtype": "track", "maxresults": offset + limit}
        if query:
            kwargs["title"] = query
        tracks = plex.library.search(**kwargs)[offset : offset + limit]
        for t in tracks:
            items.append(
                {
                    "rating_key": getattr(t, "ratingKey", None),
                    "title": t.title,
                    "artist": getattr(t, "grandparentTitle", ""),
                    "album": getattr(t, "parentTitle", ""),
                    "type": "track",
                    "art": track_art_proxy_path(t),
                }
            )
        return {"kind": "tracks", "items": items, "offset": offset, "limit": limit}

    try:
        return await run_in_threadpool(browse)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/library/play", dependencies=[Depends(verify_local_api)])
async def library_play(request: Request, body: LibraryPlay):
    state = request.app.state
    plex = _require_plex(request)
    name = body.speaker or state.settings.default_speaker_name

    def run():
        item = plex.server.fetchItem(int(body.rating_key))
        spk = state.cast.get(name)
        tracks = []
        itype = getattr(item, "type", None) or getattr(item, "TYPE", "")
        if str(itype) == "artist":
            tracks = list(item.tracks())[:100]
        elif str(itype) == "album":
            tracks = list(item.tracks())
        else:
            tracks = [item]
        if not tracks:
            raise ValueError("No tracks found for that item")
        if body.mode == "replace":
            spk.replace(tracks, repeat=False)
        elif body.mode == "play_next":
            for t in reversed(tracks):
                spk.play_next(t)
        else:
            for t in tracks:
                spk.add_end(t)
        return spk.status()

    try:
        return await run_in_threadpool(run)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc



@router.get("/api/settings", dependencies=[Depends(verify_local_api)])
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
        library = {"linked": False, "section": None, "artists": None, "albums": None, "tracks": None}
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
    return {
        "volumes": volumes,
        "stored": stored,
        "library": library,
    }


@router.patch("/api/settings", dependencies=[Depends(verify_local_api)])
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

    # Validate default <= max using merged view before save.
    settings = request.app.state.settings
    merged_default = patch.get("default_cast_volume", settings.default_cast_volume)
    merged_max = patch.get("max_cast_volume", settings.max_cast_volume)
    if merged_default > merged_max:
        raise HTTPException(status_code=400, detail="default_cast_volume cannot exceed max_cast_volume")

    stored = await run_in_threadpool(save_app_settings, patch)
    apply_to_settings(settings, stored)
    cast = getattr(request.app.state, "cast", None)
    if cast is not None:
        cast.default_volume = settings.default_cast_volume
        cast.max_volume = settings.max_cast_volume

    return await get_settings(request)
