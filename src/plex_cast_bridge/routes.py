"""HTTP routes for the local Plex Cast Bridge control API."""

import io
import json
import logging
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from .api_auth import verify_local_api
from .api_models import Command, LibraryPlay, QueueMutate, SpeakerPatch, SpeakerTest, SpeakerToggle, Transport
from .app_settings_store import apply_to_settings, load as load_app_settings, public_dict, save as save_app_settings
from .catalog_store import (
    DATA_DIR,
    PROJECT_ROOT,
    forget_speaker,
    list_speaker_records,
    persist_discovered_speakers_to_catalog,
    update_speaker_record,
)
from .playback_service import play, transport
from .speaker_queue import track_art_proxy_path
from . import activity

log = logging.getLogger(__name__)

ICON_PATH = PROJECT_ROOT / "assets" / "icon.png"
ICON_144_PATH = PROJECT_ROOT / "assets" / "icon-144.png"
DASHBOARD_TEMPLATE = Path(__file__).resolve().parent / "templates" / "dashboard.html"

router = APIRouter()


@router.get("/api/activity", dependencies=[Depends(verify_local_api)])
async def recent_activity():
    return {"events": activity.recent()}

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


def _public_speaker(rec: dict) -> dict:
    kind = rec.get("kind") or ("group" if "group" in str(rec.get("model") or "").lower() else "speaker")
    return {
        "id": rec.get("google_home_device_id") or rec.get("id"),
        "google_home_device_id": rec.get("google_home_device_id") or rec.get("id"),
        "name": rec.get("speaker_name") or rec.get("name"),
        "speaker_name": rec.get("speaker_name"),
        "room": rec.get("room_hint") or rec.get("roomHint"),
        "room_hint": rec.get("room_hint") or rec.get("roomHint"),
        "model": rec.get("model") or "Google Cast",
        "kind": kind,
        "is_group": kind == "group",
        "host": rec.get("host") or "",
        "port": rec.get("port") or 8009,
        "uuid": rec.get("cast_uuid") or rec.get("uuid") or "",
        "enabled": bool(rec.get("enabled", True)),
        "google_home_visible": bool(rec.get("google_home_visible", False)),
        "google_home_name": rec.get("google_home_name") or rec.get("name") or "Plex",
        "availability": rec.get("availability") or "unknown",
        "last_seen": rec.get("last_seen"),
        "needs_review": bool(rec.get("needs_review", False)),
    }


@router.get("/api/speakers", dependencies=[Depends(verify_local_api)])
async def speakers_endpoint(request: Request, scan: bool = False):
    state = request.app.state

    def get_list():
        summary = None
        if scan or not getattr(state.cast, "last_discovered", None):
            discovered = state.cast.discover()
            result = persist_discovered_speakers_to_catalog(discovered, state.cast)
            summary = result.get("summary")
        records = list_speaker_records()
        payload = [_public_speaker(r) for r in records]
        if summary is not None:
            return {"speakers": payload, "summary": summary}
        return payload

    try:
        return await run_in_threadpool(get_list)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Catalog persistence failed: {exc}") from exc


@router.post("/api/speakers/refresh", dependencies=[Depends(verify_local_api)])
async def refresh_speakers_endpoint(request: Request):
    state = request.app.state

    def refresh():
        discovered = state.cast.discover()
        result = persist_discovered_speakers_to_catalog(discovered, state.cast)
        return {
            "speakers": [_public_speaker(r) for r in result["speakers"] if not r.get("removed")],
            "summary": result.get("summary"),
            "changed": result.get("changed"),
        }

    try:
        return await run_in_threadpool(refresh)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Speaker refresh failed: {exc}") from exc


@router.post("/api/speakers/toggle", dependencies=[Depends(verify_local_api)])
async def toggle_speaker_endpoint(request: Request, toggle: SpeakerToggle):
    state = request.app.state

    def update():
        matches = [rec for rec in list_speaker_records() if (rec.get("speaker_name") or "").casefold() == toggle.speaker.casefold()]
        if len(matches) > 1:
            raise ValueError("Ambiguous speaker name; use the device ID")
        previous_disabled = state.cast.is_disabled(toggle.speaker)
        state.cast.set_speaker_enabled(toggle.speaker, toggle.enabled)
        try:
            # Prefer stable id match via speaker_name.
            for rec in list_speaker_records():
                if (rec.get("speaker_name") or "").casefold() == toggle.speaker.casefold():
                    update_speaker_record(rec["google_home_device_id"], {"enabled": toggle.enabled})
                    break
            else:
                discovered = getattr(state.cast, "last_discovered", None) or state.cast.discover()
                persist_discovered_speakers_to_catalog(discovered, state.cast)
                for rec in list_speaker_records():
                    if (rec.get("speaker_name") or "").casefold() == toggle.speaker.casefold():
                        update_speaker_record(rec["google_home_device_id"], {"enabled": toggle.enabled})
                        break
        except Exception:
            state.cast.set_speaker_enabled(toggle.speaker, not previous_disabled)
            raise
        speakers = [_public_speaker(r) for r in list_speaker_records()]
        return {
            "speaker": toggle.speaker,
            "enabled": toggle.enabled,
            "speakers": speakers,
        }

    try:
        return await run_in_threadpool(update)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Catalog persistence failed: {exc}") from exc


@router.patch("/api/speakers/{device_id}", dependencies=[Depends(verify_local_api)])
async def patch_speaker_endpoint(request: Request, device_id: str, body: SpeakerPatch):
    state = request.app.state
    patch = body.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(status_code=400, detail="No fields to update")

    def update():
        rec = update_speaker_record(device_id, patch)
        # Keep CastManager disabled set in sync with playback enable.
        target = rec.get("speaker_name")
        if target and not rec.get("cast_uuid") and "enabled" in patch:
            state.cast.set_speaker_enabled(target, bool(patch["enabled"]))
        return _public_speaker(rec)

    try:
        return await run_in_threadpool(update)
    except KeyError:
        raise HTTPException(status_code=404, detail="Speaker not found") from None
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Speaker update failed: {exc}") from exc


@router.delete("/api/speakers/{device_id}", dependencies=[Depends(verify_local_api)])
async def forget_speaker_endpoint(device_id: str):
    def remove():
        rec = forget_speaker(device_id)
        return {
            "removed": True,
            "speaker": _public_speaker(rec),
            "note": (
                "This removes the virtual Plex device from Google Home during the next "
                "successful sync. It does not remove the Nest speaker from Google Home."
            ),
        }

    try:
        return await run_in_threadpool(remove)
    except KeyError:
        raise HTTPException(status_code=404, detail="Speaker not found") from None


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

@router.get("/api/diagnostics", dependencies=[Depends(verify_local_api)])
async def diagnostics(request: Request):
    from . import catalog_sync
    from . import google_home_setup as gh

    settings = request.app.state.settings
    plex = getattr(request.app.state, "plex", None)
    cast = request.app.state.cast
    sync = catalog_sync.public_sync_status()
    setup = gh.public_status()
    checks = [
        {
            "id": "local_bridge",
            "label": "Local bridge",
            "status": "connected",
            "detail": "API responding",
        },
        {
            "id": "plex",
            "label": "Plex",
            "status": "connected" if plex else "not_configured",
            "detail": "Linked" if plex else "Connect with Plex",
        },
        {
            "id": "cast",
            "label": "Cast discovery",
            "status": "connected" if getattr(cast, "last_discovered", None) else "needs_attention",
            "detail": f"{len(getattr(cast, 'last_discovered', []) or [])} known device(s)",
        },
        {
            "id": "google_cloud",
            "label": "Google Cloud gateway",
            "status": "connected" if setup.get("cloud_run_url") else "not_configured",
            "detail": setup.get("cloud_run_url") or "Not configured",
        },
        {
            "id": "catalog_sync",
            "label": "Cloud catalog sync",
            "status": (
                "connected"
                if sync.get("status") == "succeeded"
                else ("needs_attention" if sync.get("status") in {"failed", "configured", "pending"} else "not_configured")
            ),
            "detail": sync.get("status"),
        },
        {
            "id": "pubsub",
            "label": "Pub/Sub listener",
            "status": "connected" if settings.enable_pubsub_listener else "not_configured",
            "detail": "enabled" if settings.enable_pubsub_listener else "disabled",
        },
        {
            "id": "home_link",
            "label": "Google Home linking",
            "status": "connected" if setup.get("link_confirmed") else "not_configured",
            "detail": "Confirmed" if setup.get("link_confirmed") else "Unverified",
        },
    ]
    return {
        "checks": checks,
        "updated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "technical": {
            "project_id": settings.gcp_project_id or None,
            "subscription_id": settings.gcp_pubsub_subscription_id or None,
            "max_cast_volume": settings.max_cast_volume,
            "default_cast_volume": settings.default_cast_volume,
        },
    }


@router.get("/api/backup", dependencies=[Depends(verify_local_api)])
async def backup_export(request: Request):
    """Download non-secret configuration backup (excludes tokens, PEMs, SA keys)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        meta = {
            "schema_version": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "includes_secrets": False,
        }
        zf.writestr("backup_meta.json", json.dumps(meta, indent=2))
        cat = DATA_DIR / "catalog.json"
        if cat.exists():
            zf.writestr("catalog.json", cat.read_text(encoding="utf-8"))
        app_settings = DATA_DIR / "app_settings.json"
        if app_settings.exists():
            zf.writestr("app_settings.json", app_settings.read_text(encoding="utf-8"))
        for name in ("google_home_setup.json", "google_home_sync.json"):
            p = DATA_DIR / name
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                data.pop("catalog_write_key", None)
                zf.writestr(name, json.dumps(data, indent=2))
        settings = request.app.state.settings
        zf.writestr(
            "settings_public.json",
            json.dumps(
                {
                    "default_cast_volume": settings.default_cast_volume,
                    "max_cast_volume": settings.max_cast_volume,
                    "night_cast_volume": settings.night_cast_volume,
                    "gcp_project_id": settings.gcp_project_id,
                    "enable_pubsub_listener": settings.enable_pubsub_listener,
                    "cloud_gateway_url": settings.cloud_gateway_url,
                },
                indent=2,
            ),
        )
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=plex-cast-bridge-backup.zip"},
    )


@router.post("/api/backup/restore/preview", dependencies=[Depends(verify_local_api)])
async def backup_restore_preview(request: Request):
    """Preview a non-secret backup zip without writing files."""
    form = await request.form()
    upload = form.get("file")
    if upload is None:
        raise HTTPException(status_code=400, detail="file required")
    raw = await upload.read()
    if len(raw) > 20_000_000:
        raise HTTPException(status_code=400, detail="archive too large")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="invalid zip") from exc
    names = zf.namelist()
    for name in names:
        if name.startswith("/") or ".." in Path(name).parts:
            raise HTTPException(status_code=400, detail="path traversal rejected")
    meta = {}
    if "backup_meta.json" in names:
        meta = json.loads(zf.read("backup_meta.json"))
    if meta.get("includes_secrets"):
        raise HTTPException(status_code=400, detail="refusing backup that claims to include secrets")
    blocked = {"plex_connection.json", "device_private.pem", "gcp-credentials.json"}
    would_write = [n for n in names if Path(n).name not in blocked and n != "backup_meta.json"]
    return {
        "schema_version": meta.get("schema_version"),
        "created_at": meta.get("created_at"),
        "files": would_write,
        "blocked": sorted(blocked.intersection({Path(n).name for n in names})),
        "writable": True,
    }


@router.post("/api/backup/restore", dependencies=[Depends(verify_local_api)])
async def backup_restore(request: Request):
    """Apply a non-secret backup. Requires confirm=true."""
    form = await request.form()
    if str(form.get("confirm") or "").lower() not in {"1", "true", "yes"}:
        raise HTTPException(status_code=400, detail="confirm=true required")
    upload = form.get("file")
    if upload is None:
        raise HTTPException(status_code=400, detail="file required")
    raw = await upload.read()
    if len(raw) > 20_000_000:
        raise HTTPException(status_code=400, detail="archive too large")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="invalid zip") from exc
    allowed = {
        "catalog.json",
        "app_settings.json",
        "google_home_setup.json",
        "google_home_sync.json",
        "settings_public.json",
    }
    written = []
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for name in zf.namelist():
        if name.startswith("/") or ".." in Path(name).parts:
            raise HTTPException(status_code=400, detail="path traversal rejected")
        base = Path(name).name
        if base not in allowed:
            continue
        target = DATA_DIR / base
        data = zf.read(name)
        if base.endswith(".json"):
            parsed = json.loads(data)
            if isinstance(parsed, dict):
                parsed.pop("catalog_write_key", None)
                parsed.pop("plex_token", None)
                data = (json.dumps(parsed, indent=2) + "\n").encode("utf-8")
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
        written.append(base)
    return {"restored": written}
