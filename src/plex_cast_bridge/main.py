import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from .ai_router import SmartDJ
from .api_auth import verify_local_api
from .api_models import Command, SpeakerTest, SpeakerToggle, Transport
from .cast_manager import CastManager
from .catalog_store import (
    get_catalog_path,
    load_disabled_speakers_from_catalog,
    persist_discovered_speakers_to_catalog,
)

# Re-export for tests that historically imported get_catalog_path from main.
from .config import Settings
from .playback_service import play, transport
from .plex_service import PlexService
from .pubsub_worker import PubSubWorker
from .routes import router

log = logging.getLogger(__name__)

# Compatibility re-exports for scripts/tests that imported helpers from main.
_load_disabled_speakers_from_catalog = load_disabled_speakers_from_catalog
_persist_discovered_speakers_to_catalog = persist_discovered_speakers_to_catalog


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
                return await play(
                    dj=app.state.dj,
                    plex=app.state.plex,
                    cast=app.state.cast,
                    settings=app.state.settings,
                    command=Command(query=data["query"], speaker=data.get("speaker")),
                )
            return await transport(
                cast=app.state.cast,
                settings=app.state.settings,
                command=Transport(
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
app.include_router(router)

# Re-export symbols historically imported from plex_cast_bridge.main
__all__ = [
    "Command",
    "SpeakerTest",
    "SpeakerToggle",
    "Transport",
    "_init_plex_with_retry",
    "_load_disabled_speakers_from_catalog",
    "_persist_discovered_speakers_to_catalog",
    "app",
    "lifespan",
    "play",
    "transport",
    "verify_local_api",
]
