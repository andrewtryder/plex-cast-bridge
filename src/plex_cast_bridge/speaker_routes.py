"""Speaker catalog and Cast device management routes."""

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .api_models import SpeakerPatch, SpeakerTest, SpeakerToggle
from .catalog_store import forget_speaker, list_speaker_records, update_speaker_record
from .playback_routes import _require_plex

router = APIRouter()


def _compat_routes():
    from . import routes

    return routes


def _public_speaker(rec: dict) -> dict:
    kind = rec.get("kind") or (
        "group" if "group" in str(rec.get("model") or "").lower() else "speaker"
    )
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
        "voice_cold_start_enabled": bool(rec.get("voice_cold_start_enabled", False)),
        "voice_cold_start_selection": rec.get("voice_cold_start_selection") or "shuffle",
        "voice_cold_start_artist": rec.get("voice_cold_start_artist") or "",
        "google_home_visible": bool(rec.get("google_home_visible", False)),
        "google_home_name": rec.get("google_home_name") or rec.get("name") or "Plex",
        "availability": rec.get("availability") or "unknown",
        "last_seen": rec.get("last_seen"),
        "needs_review": bool(rec.get("needs_review", False)),
    }


@router.post("/api/speakers/test")
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


@router.get("/api/speakers")
async def speakers_endpoint(request: Request, scan: bool = False):
    state = request.app.state

    def get_list():
        summary = None
        if scan or not getattr(state.cast, "last_discovered", None):
            discovered = state.cast.discover()
            result = _compat_routes().persist_discovered_speakers_to_catalog(discovered, state.cast)
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


@router.post("/api/speakers/refresh")
async def refresh_speakers_endpoint(request: Request):
    state = request.app.state

    def refresh():
        discovered = state.cast.discover()
        result = _compat_routes().persist_discovered_speakers_to_catalog(discovered, state.cast)
        return {
            "speakers": [_public_speaker(r) for r in list_speaker_records()],
            "summary": result.get("summary"),
            "changed": result.get("changed"),
        }

    try:
        return await run_in_threadpool(refresh)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Speaker refresh failed: {exc}") from exc


@router.post("/api/speakers/toggle")
async def toggle_speaker_endpoint(request: Request, toggle: SpeakerToggle):
    state = request.app.state

    def update():
        matches = [
            rec
            for rec in list_speaker_records()
            if (rec.get("speaker_name") or "").casefold() == toggle.speaker.casefold()
        ]
        if len(matches) > 1:
            raise ValueError("Ambiguous speaker name; use the device ID")
        previous_disabled = state.cast.is_disabled(toggle.speaker)
        state.cast.set_speaker_enabled(toggle.speaker, toggle.enabled)
        try:
            for rec in list_speaker_records():
                if (rec.get("speaker_name") or "").casefold() == toggle.speaker.casefold():
                    update_speaker_record(rec["google_home_device_id"], {"enabled": toggle.enabled})
                    break
            else:
                discovered = getattr(state.cast, "last_discovered", None) or state.cast.discover()
                _compat_routes().persist_discovered_speakers_to_catalog(discovered, state.cast)
                for rec in list_speaker_records():
                    if (rec.get("speaker_name") or "").casefold() == toggle.speaker.casefold():
                        update_speaker_record(
                            rec["google_home_device_id"], {"enabled": toggle.enabled}
                        )
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


@router.patch("/api/speakers/{device_id}")
async def patch_speaker_endpoint(request: Request, device_id: str, body: SpeakerPatch):
    state = request.app.state
    patch = body.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(status_code=400, detail="No fields to update")

    def update():
        rec = update_speaker_record(device_id, patch)
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


@router.delete("/api/speakers/{device_id}")
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
