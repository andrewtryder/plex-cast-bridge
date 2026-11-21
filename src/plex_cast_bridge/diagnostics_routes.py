"""Read-only local dashboard diagnostic routes."""

from datetime import UTC, datetime

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/api/diagnostics")
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
            "status": "connected"
            if sync.get("status") == "succeeded"
            else (
                "needs_attention"
                if sync.get("status") in {"failed", "configured", "pending"}
                else "not_configured"
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
