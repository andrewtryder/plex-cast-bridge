import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .ai_router import SmartDJ
from .cast_manager import CastManager
from .catalog_store import (
    PROJECT_ROOT,
    get_catalog_path,
    load_disabled_speakers_from_catalog,
    persist_discovered_speakers_to_catalog,
)
from .config import Settings
from .plex_service import PlexService
from .pubsub_worker import PubSubWorker

log = logging.getLogger(__name__)

ICON_PATH = PROJECT_ROOT / "assets" / "icon.png"
DASHBOARD_TEMPLATE = Path(__file__).resolve().parent / "templates" / "dashboard.html"

# Compatibility re-exports for scripts/tests that imported private helpers from main.
_load_disabled_speakers_from_catalog = load_disabled_speakers_from_catalog
_persist_discovered_speakers_to_catalog = persist_discovered_speakers_to_catalog


class Command(BaseModel):
    query: str = Field(min_length=1)
    speaker: str | None = None


class SpeakerTest(BaseModel):
    speaker: str | None = None
    query: str | None = None


class SpeakerToggle(BaseModel):
    speaker: str = Field(min_length=1)
    enabled: bool


class Transport(BaseModel):
    action: Literal["pause", "resume", "next", "previous", "stop", "volume"]
    speaker: str | None = None
    volume: float | None = Field(default=None, ge=0, le=1)
    relative_volume: float | None = Field(default=None, ge=-1, le=1)


async def verify_local_api(
    request: Request,
    x_api_key: str | None = Header(None, alias="X-API-Key"),
):
    settings = getattr(request.app.state, "settings", None)
    local_key = getattr(settings, "local_api_key", None)
    if not local_key or not getattr(local_key, "get_secret_value", lambda: "")():
        return None
    expected = local_key.get_secret_value()
    token = x_api_key or request.query_params.get("api_key")
    if not token:
        auth_hdr = request.headers.get("Authorization")
        if auth_hdr and auth_hdr.lower().startswith("bearer "):
            token = auth_hdr.split(" ", 1)[1]
    if not token or not secrets.compare_digest(token, expected):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: invalid or missing local API key (pass X-API-Key or ?api_key=)",
        )
    return token


async def _init_plex_with_retry(settings: Settings, attempts: int = 5, base_delay_sec: float = 1.0):
    """Connect to Plex and refresh the library catalog with bounded backoff."""
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            plex = await run_in_threadpool(
                PlexService,
                settings.plex_url,
                settings.plex_token.get_secret_value(),
                settings.plex_music_section,
            )
            await run_in_threadpool(plex.refresh)
            if attempt > 1:
                log.info("Plex became reachable on startup attempt %d/%d", attempt, attempts)
            return plex
        except Exception as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            delay = base_delay_sec * (2 ** (attempt - 1))
            log.warning(
                "Plex startup attempt %d/%d failed (%s); retrying in %.1fs",
                attempt,
                attempts,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
    raise RuntimeError(
        f"Plex unavailable after {attempts} startup attempts: {last_exc}"
    ) from last_exc


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    app.state.settings = settings
    if not settings.local_api_key or not settings.local_api_key.get_secret_value():
        log.warning(
            "Local control API running without LOCAL_API_KEY "
            "(ALLOW_UNAUTHENTICATED_LOCAL_API=true). Restrict to a trusted LAN."
        )
    else:
        log.info("LOCAL_API_KEY authentication enabled for local API control endpoints.")

    app.state.plex = await _init_plex_with_retry(settings)
    app.state.dj = SmartDJ(settings.gemini_api_key.get_secret_value())
    disabled_speakers = _load_disabled_speakers_from_catalog()
    app.state.cast = CastManager(
        default_volume=settings.default_cast_volume,
        default_speaker_host=settings.default_speaker_host,
        disabled_speakers=disabled_speakers,
        catalog_path=get_catalog_path(for_writing=False),
    )
    # Run initial background speaker discovery on daemon startup
    try:
        discovered = await run_in_threadpool(app.state.cast.discover)
        await run_in_threadpool(_persist_discovered_speakers_to_catalog, discovered, app.state.cast)
        log.info("Startup LAN scan discovered %d Cast targets", len(discovered))
    except Exception as exc:
        log.warning("Initial speaker discovery warning: %s", exc)

    app.state.worker = None
    if settings.enable_pubsub_listener:
        async def dispatch(data):
            if data["action"] == "play":
                return await play(app, Command(query=data["query"], speaker=data.get("speaker")))
            return await transport(
                app,
                Transport(
                    action=data["action"],
                    speaker=data.get("speaker"),
                    volume=data.get("volume"),
                    relative_volume=data.get("relative_volume"),
                ),
            )
        app.state.worker = PubSubWorker(settings, asyncio.get_running_loop(), dispatch)
    try:
        yield
    finally:
        if app.state.worker:
            app.state.worker.close()
        await run_in_threadpool(app.state.cast.close)


app = FastAPI(title="Plex Cast Bridge", lifespan=lifespan)


@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok"}


async def play(app, command: Command):
    def execute():
        intent = app.state.dj.parse(command.query, app.state.plex.catalog)
        tracks = app.state.plex.resolve(intent)
        if not tracks:
            raise ValueError("No matching tracks found in the Plex library")
        name = command.speaker or intent.target_speaker or app.state.settings.default_speaker_name
        speaker = app.state.cast.get(name)
        speaker.replace(tracks, intent.mode in ("artist", "artist_radio", "theme_mix"))
        return {"intent": intent.model_dump(), "playback": speaker.status()}
    return await run_in_threadpool(execute)


async def transport(app, command: Transport):
    def execute():
        speaker = app.state.cast.get(command.speaker or app.state.settings.default_speaker_name)
        if command.action == "next":
            speaker.skip()
        elif command.action == "previous":
            if hasattr(speaker, "previous"):
                speaker.previous()
            else:
                speaker.skip()
        elif command.action == "stop":
            speaker.stop()
        elif command.action in ("pause", "resume"):
            getattr(speaker, command.action)()
        if command.volume is not None:
            speaker.set_volume(command.volume)
        elif command.relative_volume is not None and hasattr(speaker, "adjust_volume"):
            speaker.adjust_volume(command.relative_volume)
        return speaker.status()
    return await run_in_threadpool(execute)


@app.get("/assets/icon.png", include_in_schema=False)
async def serve_icon():
    if ICON_PATH.exists():
        return FileResponse(ICON_PATH, media_type="image/png")
    raise HTTPException(status_code=404, detail="Icon not found")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard_ui():
    if DASHBOARD_TEMPLATE.exists():
        return HTMLResponse(DASHBOARD_TEMPLATE.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Plex Cast Bridge Dashboard template missing</h1>")


@app.post("/api/command", dependencies=[Depends(verify_local_api)])
async def command_endpoint(command: Command):
    try:
        return await play(app, command)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/library/tracks", dependencies=[Depends(verify_local_api)])
async def library_tracks(query: str = "", limit: int = 25):
    if not 1 <= limit <= 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100")

    def search():
        kwargs = {"libtype": "track", "maxresults": limit}
        if query:
            kwargs["title"] = query
        tracks = app.state.plex.library.search(**kwargs)
        return [
            {
                "title": t.title,
                "artist": getattr(t, "grandparentTitle", ""),
                "album": getattr(t, "parentTitle", ""),
            }
            for t in tracks
        ]
    return await run_in_threadpool(search)


@app.post("/api/speakers/test", dependencies=[Depends(verify_local_api)])
async def test_speaker_endpoint(command: SpeakerTest):
    def execute():
        tracks = (
            app.state.plex.library.search(title=command.query, libtype="track")
            if command.query
            else app.state.plex.library.search(libtype="track", maxresults=1)
        )
        if not tracks:
            raise ValueError("No matching tracks found in the Plex library")
        speaker = app.state.cast.get(command.speaker or app.state.settings.default_speaker_name)
        speaker.replace([tracks[0]], repeat=False)
        return speaker.status()
    try:
        return await run_in_threadpool(execute)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/transport", dependencies=[Depends(verify_local_api)])
async def transport_endpoint(command: Transport):
    try:
        return await transport(app, command)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/speakers", dependencies=[Depends(verify_local_api)])
async def speakers_endpoint(scan: bool = False):
    def get_list():
        if scan or not getattr(app.state.cast, "last_discovered", None):
            discovered = app.state.cast.discover()
            _persist_discovered_speakers_to_catalog(discovered, app.state.cast)
            return discovered
        for dev in app.state.cast.last_discovered:
            dev["enabled"] = not app.state.cast.is_disabled(dev["name"])
        return app.state.cast.last_discovered
    try:
        return await run_in_threadpool(get_list)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Catalog persistence failed: {exc}") from exc


@app.post("/api/speakers/toggle", dependencies=[Depends(verify_local_api)])
async def toggle_speaker_endpoint(toggle: SpeakerToggle):
    def update():
        previous_disabled = app.state.cast.is_disabled(toggle.speaker)
        app.state.cast.set_speaker_enabled(toggle.speaker, toggle.enabled)
        try:
            discovered = getattr(app.state.cast, "last_discovered", None) or app.state.cast.discover()
            for dev in discovered:
                dev["enabled"] = not app.state.cast.is_disabled(dev["name"])
            _persist_discovered_speakers_to_catalog(discovered, app.state.cast)
        except Exception:
            # Roll back in-memory enablement so API state matches catalog on disk.
            app.state.cast.set_speaker_enabled(toggle.speaker, not previous_disabled)
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


@app.get("/api/status", dependencies=[Depends(verify_local_api)])
async def status_endpoint():
    return {name: speaker.status() for name, speaker in list(app.state.cast.speakers.items())}


@app.post("/api/library/refresh", dependencies=[Depends(verify_local_api)])
async def refresh_endpoint():
    return await run_in_threadpool(app.state.plex.refresh)


@app.get("/api/sync-catalog", dependencies=[Depends(verify_local_api)])
async def catalog_endpoint():
    return {"genres": app.state.plex.catalog["genres"], "artists": app.state.plex.catalog["artists"][:100]}
