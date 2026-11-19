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
    # Refuse to run with ephemeral state if /data is unmounted in container environments
    from .catalog_store import DATA_DIR
    if (
        DATA_DIR == Path("/data")
        and (Path("/.dockerenv").exists() or os.getenv("DOCKER_CONTAINER") == "1")
        and not os.path.ismount(DATA_DIR)
        and os.environ.get("PCB_ALLOW_EPHEMERAL_DATA", "").strip().lower() not in {"1", "true", "yes"}
    ):
        raise RuntimeError(
            "CRITICAL: /data is not a mounted persistent volume. "
            "Plex Cast Bridge requires a persistent mount for /data to prevent data loss across updates. "
            "Mount a host directory or named volume (e.g. -v /path/to/data:/data), "
            "or set PCB_ALLOW_EPHEMERAL_DATA=1 if stateless execution is intended."
        )

    # Validate persistent data state during upgrades / existing installations
    require_existing = os.environ.get("PCB_REQUIRE_EXISTING_DATA", "").strip().lower() in {"1", "true", "yes"} or \
                       os.environ.get("PCB_UPGRADE", "").strip().lower() in {"1", "true", "yes"}
    has_existing_data = any(
        (DATA_DIR / f).exists()
        for f in (
            "catalog.json",
            "plex_connection.json",
            "music_index.sqlite3",
            "app_settings.json",
            "google_home_sync.json",
            "google_home_setup.json",
        )
    )
    if require_existing and not has_existing_data:
        raise RuntimeError(
            f"CRITICAL: PCB_REQUIRE_EXISTING_DATA is set, but {DATA_DIR} does not contain existing "
            "Plex Cast Bridge configuration (no catalog.json, plex_connection.json, music_index.sqlite3, "
            "app_settings.json, or google_home_sync.json found). "
            "Refusing to start to prevent data loss from an incorrect or empty mount path."
        )

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
    if settings.enable_pubsub_listener:
        creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if creds_path:
            p = Path(creds_path)
            if not p.exists() or not p.is_file() or p.stat().st_size == 0:
                raise RuntimeError(
                    f"CRITICAL: ENABLE_PUBSUB_LISTENER=true is set, but GOOGLE_APPLICATION_CREDENTIALS ({creds_path}) "
                    "points to an unmounted placeholder (/dev/null), missing file, or empty key. "
                    "Provide a valid Google Cloud service account key via PCB_GCP_CREDENTIALS to enable Google Home voice control."
                )

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
    discovery_task: asyncio.Task | None = None
    discovery_lock = asyncio.Lock()
    interval = int(getattr(settings, "speaker_discovery_interval_sec", 0) or 0)

    if not skip_auto_discovery:
        try:
            discovered = await run_in_threadpool(app.state.cast.discover)
            await run_in_threadpool(_persist_discovered_speakers_to_catalog, discovered, app.state.cast)
            log.info("Startup LAN scan discovered %d Cast targets", len(discovered))
        except Exception as exc:
            log.warning("Initial speaker discovery warning: %s", exc)

    async def _background_speaker_discovery():
        while True:
            await asyncio.sleep(max(interval, 60))
            if discovery_lock.locked():
                log.debug("Skipping overlapping speaker discovery job")
                continue
            async with discovery_lock:
                try:
                    discovered = await run_in_threadpool(app.state.cast.discover)
                    result = await run_in_threadpool(
                        _persist_discovered_speakers_to_catalog, discovered, app.state.cast
                    )
                    summary = (result or {}).get("summary") or {}
                    if (result or {}).get("changed"):
                        log.info("Background speaker discovery: %s", summary.get("message") or "catalog updated")
                    else:
                        log.debug("Background speaker discovery: unchanged")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("Background speaker discovery failed: %s", exc)

    if interval > 0 and not skip_auto_discovery:
        discovery_task = asyncio.create_task(_background_speaker_discovery())
        log.info("Background speaker discovery every %ss", interval)

    async def _reconcile_google_home():
        interval_sec = max(10, int(settings.catalog_reconcile_interval_sec))
        backoff = interval_sec
        while True:
            try:
                result = await run_in_threadpool(catalog_sync.reconcile_cloud)
                backoff = min(300, backoff * 2) if result.get("state") == "failed" else interval_sec
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Google Home reconciliation failed")
                backoff = min(300, backoff * 2)
            await asyncio.sleep(backoff)

    async def _schedule_music_index():
        while True:
            try:
                if music_index.database_path().exists():
                    settings_data = await run_in_threadpool(music_index.get_settings)
                    plex = getattr(app.state, 'plex', None)
                    if plex and settings_data['indexing_enabled'] and settings_data['schedule'] != 'manual':
                        state = await run_in_threadpool(music_index.status)
                        if music_index_scan.schedule_due(settings_data, state['scan']):
                            await run_in_threadpool(music_index_scan.start, plex)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Music indexing scheduler failed')
            await asyncio.sleep(60)

    index_task = asyncio.create_task(_schedule_music_index())
    reconcile_task = asyncio.create_task(_reconcile_google_home())
    app.state.worker = None
    if settings.enable_pubsub_listener:

        async def dispatch(data):
            if data["action"] == "play":
                return await play(
                    dj=app.state.dj,
                    plex=app.state.plex,
                    cast=app.state.cast,
                    settings=app.state.settings,
                    command=Command(query=data["query"], speaker=data.get("speaker"), device_id=data.get("device_id")),
                )
            return await transport(
                cast=app.state.cast,
                settings=app.state.settings,
                command=Transport(
                    action=data["action"],
                    device_id=data.get("device_id"),
                    speaker=data.get("speaker"),
                    volume=data.get("volume"),
                    relative_volume=data.get("relative_volume"),
                    mute=data.get("mute"),
                ),
            )

        app.state.worker = PubSubWorker(settings, asyncio.get_running_loop(), dispatch)
    try:
        yield
    finally:
        index_task.cancel()
        try:
            await index_task
        except asyncio.CancelledError:
            pass
        if music_index.database_path().exists():
            await run_in_threadpool(music_index_scan.cancel)
        catalog_sync.stop_pending_push()
        reconcile_task.cancel()
        try:
            await reconcile_task
        except asyncio.CancelledError:
            pass
        if discovery_task is not None:
            discovery_task.cancel()
            try:
                await discovery_task
            except asyncio.CancelledError:
                pass
        if app.state.worker:
            app.state.worker.close()
        await run_in_threadpool(app.state.cast.close)


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
