import asyncio
import logging
import os
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .ai_router import SmartDJ
from .api_models import Command, SpeakerTest, SpeakerToggle, Transport
from .cast_manager import CastManager
from .catalog_store import (
    get_catalog_path,
    load_disabled_speakers_from_catalog,
    persist_discovered_speakers_to_catalog,
)

# Re-export for tests that historically imported get_catalog_path from main.
from .app_settings_store import apply_to_settings, load as load_app_settings
from .config import Settings
from .playback_service import play, transport
from .plex_auth import PlexAuthService, init_plex_service
from .google_home_routes import router as google_home_router
from .plex_routes import router as plex_router
from .plex_service import PlexService
from .pubsub_worker import PubSubWorker
from .routes import router
from .music_index_routes import router as music_index_router
from .mix_routes import router as mix_router
from . import music_index, music_index_scan
from . import catalog_sync
from .startup_guards import ensure_persistent_data_mount, ensure_pubsub_credentials, has_existing_configuration, require_existing_configuration
from .startup_lifecycle import (
    LifespanBackgroundTasks,
    abort_startup_after_background_tasks,
    build_pubsub_dispatch,
    schedule_google_home_reconcile,
    schedule_music_index,
    schedule_speaker_discovery,
    shutdown_application,
)

log = logging.getLogger(__name__)

# Compatibility re-exports for scripts/tests that imported helpers from main.
_load_disabled_speakers_from_catalog = load_disabled_speakers_from_catalog
_persist_discovered_speakers_to_catalog = persist_discovered_speakers_to_catalog


async def _init_plex_with_retry(settings: Settings, attempts: int = 5, base_delay_sec: float = 1.0):
    """Connect to Plex using env token with bounded backoff (legacy helper for tests)."""
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            token = settings.plex_token.get_secret_value() if settings.plex_token else ""
            if not token:
                raise ValueError("PLEX_TOKEN is not set")
            plex = await run_in_threadpool(
                PlexService,
                settings.plex_url,
                token,
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


async def _init_plex_flexible(settings: Settings, auth: PlexAuthService):
    """JWT store first, then env token; return None when Plex is not linked."""
    return await run_in_threadpool(init_plex_service, settings, auth)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .catalog_store import DATA_DIR

    ensure_persistent_data_mount(DATA_DIR)
    require_existing_configuration(DATA_DIR)
    has_existing_data = has_existing_configuration(DATA_DIR)

    if has_existing_data:
        log.info("Mounted persistent data directory at %s with existing configuration", DATA_DIR)
    else:
        log.info("Mounted persistent data directory at %s (empty; initial setup)", DATA_DIR)

    # Skip automatic mDNS / slow Plex retries during pytest TestClient lifespan.
    skip_auto_discovery = os.environ.get("PCB_DISABLE_BACKGROUND_DISCOVERY", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    settings = Settings()
    ensure_pubsub_credentials(settings.enable_pubsub_listener, os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))

    if music_index.database_path().exists():
        await run_in_threadpool(music_index_scan.recover_interrupted)
    stored = load_app_settings()
    if stored:
        apply_to_settings(settings, stored)
        log.info(
            "Loaded app settings from data/app_settings.json "
            "(default_cast_volume=%.2f max_cast_volume=%.2f)",
            settings.default_cast_volume,
            settings.max_cast_volume,
        )
    app.state.settings = settings
    app.state.plex_auth = PlexAuthService()
    log.warning("Local dashboard and control API are unauthenticated; restrict access to a trusted network.")

    try:
        plex_attempts = 1 if skip_auto_discovery else int(os.environ.get("PCB_PLEX_STARTUP_ATTEMPTS", "5") or "5")
        app.state.plex = await run_in_threadpool(
            init_plex_service,
            settings,
            app.state.plex_auth,
            max(1, plex_attempts),
            0.1 if skip_auto_discovery else 1.0,
        )
    except Exception as exc:
        log.error("Plex initialization failed; continuing without library access: %s", exc)
        app.state.plex = None

    if app.state.plex is None:
        log.info("Dashboard available. Link Plex under Settings → Connect with Plex.")

    app.state.dj = SmartDJ(settings.gemini_api_key.get_secret_value())
    disabled_speakers = _load_disabled_speakers_from_catalog()
    app.state.cast = CastManager(
        default_volume=settings.default_cast_volume,
        max_volume=settings.max_cast_volume,
        default_speaker_host=settings.default_speaker_host,
        disabled_speakers=disabled_speakers,
        catalog_path=get_catalog_path(for_writing=False),
    )
    cloud_url = getattr(settings, "cloud_gateway_url", "") or ""
    write_key = settings.catalog_write_key.get_secret_value() if getattr(settings, "catalog_write_key", None) else ""
    if cloud_url and write_key:
        try:
            catalog_sync.configure_gateway(cloud_url, write_key)
        except Exception as exc:
            log.warning("Could not configure catalog sync from env: %s", exc)
    catalog_sync.resume_pending_push()
    discovery_lock = asyncio.Lock()
    interval = int(getattr(settings, "speaker_discovery_interval_sec", 0) or 0)

    if not skip_auto_discovery:
        try:
            discovered = await run_in_threadpool(app.state.cast.discover)
            await run_in_threadpool(_persist_discovered_speakers_to_catalog, discovered, app.state.cast)
            log.info("Startup LAN scan discovered %d Cast targets", len(discovered))
        except Exception as exc:
            log.warning("Initial speaker discovery warning: %s", exc)

    bg_tasks = LifespanBackgroundTasks(
        discovery=schedule_speaker_discovery(
            interval_sec=interval,
            skip=skip_auto_discovery,
            lock=discovery_lock,
            discover=app.state.cast.discover,
            persist=_persist_discovered_speakers_to_catalog,
            cast=app.state.cast,
        ),
        music_index=schedule_music_index(
            app=app,
            database_path=music_index.database_path,
            get_settings=music_index.get_settings,
            status=music_index.status,
            schedule_due=music_index_scan.schedule_due,
            start_scan=music_index_scan.start,
        ),
        google_home_reconcile=schedule_google_home_reconcile(
            interval_sec=settings.catalog_reconcile_interval_sec,
            reconcile=catalog_sync.reconcile_cloud,
        ),
    )
    if bg_tasks.discovery is not None:
        log.info("Background speaker discovery every %ss", interval)

    app.state.worker = None
    try:
        if settings.enable_pubsub_listener:
            dispatch = build_pubsub_dispatch(
                app,
                play=play,
                transport=transport,
                command_cls=Command,
                transport_cls=Transport,
            )
            app.state.worker = PubSubWorker(settings, asyncio.get_running_loop(), dispatch)
    except Exception:
        await abort_startup_after_background_tasks(
            bg_tasks,
            stop_pending_push=catalog_sync.stop_pending_push,
            close_cast=app.state.cast.close,
        )
        raise

    try:
        yield
    finally:
        await shutdown_application(
            app,
            bg_tasks,
            database_path=music_index.database_path,
            cancel_index_scan=music_index_scan.cancel,
            stop_pending_push=catalog_sync.stop_pending_push,
            close_cast=app.state.cast.close,
        )


app = FastAPI(title="Plex Cast Bridge", lifespan=lifespan)


@app.middleware("http")
async def reject_url_api_keys(request, call_next):
    if "api_key" in request.query_params:
        return JSONResponse({"detail": "API keys in URLs are not accepted"}, status_code=400)
    response = await call_next(request)
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


_STATIC = Path(__file__).resolve().parent / "static"
if _STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
app.include_router(router)
app.include_router(music_index_router)
app.include_router(mix_router)
app.include_router(plex_router)
app.include_router(google_home_router)

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
]
