import asyncio
import json
import logging
import os
import secrets
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .ai_router import SmartDJ
from .cast_manager import CastManager, extract_room_name
from .config import Settings
from .plex_service import PlexService
from .pubsub_worker import PubSubWorker

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = Path(
    os.getenv("DATA_DIR")
    or (
        "/data"
        if Path("/data").is_dir()
        else (PROJECT_ROOT / "data" if (PROJECT_ROOT / "data").is_dir() else PROJECT_ROOT)
    )
)
CANONICAL_CATALOG = PROJECT_ROOT / "catalog.json"
CLOUD_RUN_CATALOG = PROJECT_ROOT / "gcp-cloud-run" / "catalog.json"
ICON_PATH = PROJECT_ROOT / "assets" / "icon.png"
DASHBOARD_TEMPLATE = Path(__file__).resolve().parent / "templates" / "dashboard.html"


def get_catalog_path(for_writing: bool = False) -> Path:
    env_cat = os.getenv("CATALOG_PATH")
    if env_cat:
        p = Path(env_cat)
        if for_writing:
            p.parent.mkdir(parents=True, exist_ok=True)
        return p

    data_cat = DATA_DIR / "catalog.json"
    bundled_cat = PROJECT_ROOT / "catalog.json"

    if for_writing:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not data_cat.exists() and bundled_cat.exists() and DATA_DIR != PROJECT_ROOT:
            try:
                shutil.copyfile(bundled_cat, data_cat)
            except Exception:
                pass
        return data_cat if (DATA_DIR != PROJECT_ROOT or data_cat.exists()) else bundled_cat

    if data_cat.exists():
        return data_cat
    if bundled_cat.exists():
        return bundled_cat
    return data_cat


def _load_disabled_speakers_from_catalog() -> set[str]:
    disabled: set[str] = set()
    cat_path = get_catalog_path(for_writing=False)
    if cat_path.exists():
        try:
            with open(cat_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                for spk in data.get("speakers", []):
                    if not spk.get("enabled", True):
                        if spk.get("speaker_name"):
                            disabled.add(spk["speaker_name"].casefold())
                        if spk.get("roomHint"):
                            disabled.add(spk["roomHint"].casefold())
        except Exception as exc:
            log.warning("Could not read disabled speakers from %s: %s", cat_path, exc)
    return disabled


def _persist_discovered_speakers_to_catalog(discovered: list[dict], cast_mgr: CastManager):
    """Persist discovered LAN speakers and their enabled/disabled flags into catalog.json."""
    try:
        read_path = get_catalog_path(for_writing=False)
        data = {}
        if read_path.exists():
            with open(read_path, "r", encoding="utf-8") as f:
                data = json.load(f)

        existing_by_room = {
            (s.get("roomHint") or "").casefold(): s
            for s in data.get("speakers", [])
            if s.get("roomHint")
        }

        speakers_list = []
        seen_rooms = set()
        for dev in discovered:
            name = dev["name"]
            if name.casefold() == "test speaker":
                continue
            room = dev.get("room") or extract_room_name(name)
            r_fold = room.casefold()
            if r_fold in seen_rooms:
                continue
            seen_rooms.add(r_fold)
            enabled = not cast_mgr.is_disabled(name) and not cast_mgr.is_disabled(room)
            speakers_list.append({
                "id": f"plex-jukebox:{room}",
                "name": "Plex",
                "nicknames": [f"{room} Plex", "Plex", "Plex Jukebox", "Plex Radio"],
                "roomHint": room,
                "speaker_name": name,
                "model": dev.get("model", "Google Cast"),
                "host": dev.get("host", ""),
                "port": dev.get("port", 8009),
                "uuid": dev.get("uuid", ""),
                "enabled": enabled,
            })

        # Keep any previously known speakers that might be temporarily asleep on LAN
        for r_fold, old_spk in existing_by_room.items():
            if r_fold not in seen_rooms:
                old_name = old_spk.get("speaker_name", "")
                old_room = old_spk.get("roomHint", "")
                old_spk["enabled"] = not cast_mgr.is_disabled(old_name) and not cast_mgr.is_disabled(old_room)
                speakers_list.append(old_spk)

        if speakers_list:
            data["speakers"] = speakers_list
            data["inputs"] = [s["roomHint"] for s in speakers_list if s.get("enabled", True)]
            write_path = get_catalog_path(for_writing=True)
            write_path.parent.mkdir(parents=True, exist_ok=True)
            with open(write_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
            if CLOUD_RUN_CATALOG.parent.exists():
                try:
                    shutil.copyfile(write_path, CLOUD_RUN_CATALOG)
                except Exception:
                    pass
    except Exception as exc:
        log.warning("Could not persist speaker states to catalog.json: %s", exc)


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    app.state.settings = settings
    if not settings.local_api_key or not settings.local_api_key.get_secret_value():
        log.warning("Security Notice: LOCAL_API_KEY is not configured in .env. Local endpoints are unauthenticated on LAN.")
    else:
        log.info("LOCAL_API_KEY authentication enabled for local API control endpoints.")

    app.state.plex = await run_in_threadpool(
        PlexService,
        settings.plex_url,
        settings.plex_token.get_secret_value(),
        settings.plex_music_section,
    )
    await run_in_threadpool(app.state.plex.refresh)
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
    return await run_in_threadpool(get_list)


@app.post("/api/speakers/toggle", dependencies=[Depends(verify_local_api)])
async def toggle_speaker_endpoint(toggle: SpeakerToggle):
    def update():
        app.state.cast.set_speaker_enabled(toggle.speaker, toggle.enabled)
        discovered = getattr(app.state.cast, "last_discovered", None) or app.state.cast.discover()
        for dev in discovered:
            dev["enabled"] = not app.state.cast.is_disabled(dev["name"])
        _persist_discovered_speakers_to_catalog(discovered, app.state.cast)
        return {
            "speaker": toggle.speaker,
            "enabled": toggle.enabled,
            "speakers": discovered,
        }
    return await run_in_threadpool(update)


@app.get("/api/status", dependencies=[Depends(verify_local_api)])
async def status_endpoint():
    return {name: speaker.status() for name, speaker in list(app.state.cast.speakers.items())}


@app.post("/api/library/refresh", dependencies=[Depends(verify_local_api)])
async def refresh_endpoint():
    return await run_in_threadpool(app.state.plex.refresh)


@app.get("/api/sync-catalog", dependencies=[Depends(verify_local_api)])
async def catalog_endpoint():
    return {"genres": app.state.plex.catalog["genres"], "artists": app.state.plex.catalog["artists"][:100]}
