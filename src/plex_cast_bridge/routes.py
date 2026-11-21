"""Compatibility and composition module for the local dashboard API.

Importers continue to use :data:`router` and the compatibility seams below.
Responsibility-specific routers are mounted before the legacy playback/speaker
router so public URLs and route precedence remain unchanged during extraction.
"""

from fastapi import APIRouter, Request

from .backup_routes import router as backup_router
from .catalog_store import DATA_DIR, persist_discovered_speakers_to_catalog
from .diagnostics_routes import router as diagnostics_router
from .local_routes import router as local_router
from .playback_routes import PLEX_REQUIRED_DETAIL, _require_plex, router as playback_router
from .playback_service import play
from .settings_routes import router as settings_router
from .speaker_routes import router as speaker_router

router = APIRouter()
router.include_router(settings_router)
router.include_router(diagnostics_router)
router.include_router(backup_router)
router.include_router(playback_router)
router.include_router(speaker_router)
router.include_router(local_router)

__all__ = [
    "DATA_DIR",
    "PLEX_REQUIRED_DETAIL",
    "Request",
    "_require_plex",
    "persist_discovered_speakers_to_catalog",
    "play",
    "router",
]
