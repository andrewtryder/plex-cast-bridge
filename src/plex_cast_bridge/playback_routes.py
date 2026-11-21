"""Playback, library, queue, and transport routes for the local dashboard."""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from .api_models import Command, LibraryPlay, QueueMutate, Transport
from .playback_service import transport
from .speaker_queue import track_art_proxy_path

log = logging.getLogger(__name__)
router = APIRouter()

PLEX_REQUIRED_DETAIL = "Plex is not linked. Connect with Plex in Settings, or set PLEX_TOKEN."


def _require_plex(request: Request):
    plex = getattr(request.app.state, "plex", None)
    if plex is None:
        raise HTTPException(status_code=503, detail=PLEX_REQUIRED_DETAIL)
    return plex


def _compat_routes():
    from . import routes

    return routes


@router.post("/api/command")
async def command_endpoint(request: Request, command: Command):
    state = request.app.state
    compat = _compat_routes()
    plex = compat._require_plex(request)
    try:
        return await compat.play(
            dj=state.dj,
            plex=plex,
            cast=state.cast,
            settings=state.settings,
            command=command,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.error(
            "Command failed for speaker=%r error_type=%s", command.speaker, type(exc).__name__
        )
        raise HTTPException(status_code=500, detail=f"Playback failed: {exc}") from exc


@router.get("/api/library/tracks")
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


@router.post("/api/transport")
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


@router.get("/api/status")
async def status_endpoint(request: Request):
    state = request.app.state
    return {name: speaker.status() for name, speaker in list(state.cast.speakers.items())}


@router.post("/api/library/refresh")
async def refresh_endpoint(request: Request):
    plex = _require_plex(request)
    return await run_in_threadpool(plex.refresh)


@router.get("/api/sync-catalog")
async def catalog_endpoint(request: Request):
    plex = _require_plex(request)
    catalog = plex.catalog
    return {"genres": catalog["genres"], "artists": catalog["artists"][:100]}


@router.get("/api/artwork/{rating_key}")
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


@router.get("/api/queue")
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


@router.post("/api/queue")
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


@router.get("/api/library/browse")
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
                items.append(
                    {
                        "title": getattr(g, "title", g if isinstance(g, str) else str(g)),
                        "type": "genre",
                    }
                )
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


@router.post("/api/library/play")
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
