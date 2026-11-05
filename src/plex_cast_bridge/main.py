import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .ai_router import SmartDJ
from .cast_manager import CastManager
from .config import Settings
from .plex_service import PlexService
from .pubsub_worker import PubSubWorker

log = logging.getLogger(__name__)


class Command(BaseModel):
    query: str = Field(min_length=1)
    speaker: str | None = None


class SpeakerTest(BaseModel):
    speaker: str | None = None
    query: str | None = None


class Transport(BaseModel):
    action: Literal["pause", "resume", "next", "stop", "volume"]
    speaker: str | None = None
    volume: float | None = Field(default=None, ge=0, le=1)
    relative_volume: float | None = Field(default=None, ge=-1, le=1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    app.state.settings = settings
    app.state.plex = await run_in_threadpool(
        PlexService,
        settings.plex_url,
        settings.plex_token.get_secret_value(),
        settings.plex_music_section,
    )
    await run_in_threadpool(app.state.plex.refresh)
    app.state.dj = SmartDJ(settings.gemini_api_key.get_secret_value())
    app.state.cast = CastManager(
        default_volume=settings.default_cast_volume,
        default_speaker_host=settings.default_speaker_host,
    )
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


async def play(app, command: Command):
    def execute():
        intent = app.state.dj.parse(command.query, app.state.plex.catalog)
        tracks = app.state.plex.resolve(intent)
        if not tracks:
            raise ValueError("No matching tracks found in the Plex library")
        name = command.speaker or intent.target_speaker or app.state.settings.default_speaker_name
        speaker = app.state.cast.get(name)
        speaker.replace(tracks, intent.mode in ("artist_radio", "theme_mix"))
        return {"intent": intent.model_dump(), "playback": speaker.status()}
    return await run_in_threadpool(execute)


async def transport(app, command: Transport):
    def execute():
        speaker = app.state.cast.get(command.speaker or app.state.settings.default_speaker_name)
        if command.action == "next":
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


@app.post("/api/command")
async def command_endpoint(command: Command):
    try:
        return await play(app, command)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/library/tracks")
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


@app.post("/api/speakers/test")
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


@app.post("/api/transport")
async def transport_endpoint(command: Transport):
    try:
        return await transport(app, command)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/speakers")
async def speakers_endpoint():
    return await run_in_threadpool(app.state.cast.discover)


@app.get("/api/status")
async def status_endpoint():
    return {name: speaker.status() for name, speaker in list(app.state.cast.speakers.items())}


@app.post("/api/library/refresh")
async def refresh_endpoint():
    return await run_in_threadpool(app.state.plex.refresh)


@app.get("/api/sync-catalog")
async def catalog_endpoint():
    return {"genres": app.state.plex.catalog["genres"], "artists": app.state.plex.catalog["artists"][:100]}
