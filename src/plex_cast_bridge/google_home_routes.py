"""Google Home wizard, sync, and diagnostics API routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import catalog_sync, wizard_verify
from . import google_home_setup as setup
from .gcp_provision import build_provision_plan, describe_cost_notes, run_provision_step

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/google-home")


class WizardAdvance(BaseModel):
    step: str
    patch: dict[str, Any] = Field(default_factory=dict)


class WizardVerify(BaseModel):
    step: str
    patch: dict[str, Any] = Field(default_factory=dict)


class ProvisionRequest(BaseModel):
    project_id: str | None = None
    create_project: bool = True
    recreate_canonical: bool = False
    dry_run: bool = True
    execute_step: str | None = None
    confirm_billable: bool = False


class GatewayConfig(BaseModel):
    cloud_gateway_url: str
    catalog_write_key: str = Field(min_length=8)


class LinkConfirm(BaseModel):
    confirmed: bool = True


@router.get("/status")
async def google_home_status(request: Request):
    plex = getattr(request.app.state, "plex", None)
    speakers = []
    try:
        from .catalog_store import list_speaker_records

        speakers = [
            {
                "name": s.get("speaker_name") or s.get("name"),
                "enabled": bool(s.get("enabled", True)),
                "google_home_visible": bool(s.get("google_home_visible", False)),
                "google_home_name": s.get("google_home_name"),
                "google_home_device_id": s.get("google_home_device_id") or s.get("id"),
                "model": s.get("model"),
                "kind": s.get("kind") or ("group" if "group" in str(s.get("model", "")).lower() else "speaker"),
                "availability": s.get("availability"),
            }
            for s in list_speaker_records()
        ]
    except Exception:
        speakers = []
    return {
        "setup": setup.public_status(),
        "catalog_sync": catalog_sync.public_sync_status(),
        "plex_linked": plex is not None,
        "speakers": speakers,
        "pubsub_enabled": bool(getattr(request.app.state.settings, "enable_pubsub_listener", False)),
    }


@router.post("/wizard/reset")
async def wizard_reset():
    return setup.save_setup(setup.default_state())


@router.post("/wizard/verify")
async def wizard_verify_step(body: WizardVerify, request: Request):
    if body.step not in setup.STEPS:
        raise HTTPException(status_code=400, detail=f"Unknown step {body.step}")
    return await run_in_threadpool(wizard_verify.verify_step, body.step, request.app.state, body.patch)


@router.post("/wizard/advance")
async def wizard_advance(body: WizardAdvance):
    if body.step not in setup.STEPS:
        raise HTTPException(status_code=400, detail=f"Unknown step {body.step}")
    state = setup.load_setup()
    if not state.get(f"verified_{body.step}"):
        raise HTTPException(
            status_code=400,
            detail=f"Step '{body.step}' is not verified yet. Run Verify and fix any failed checks first.",
        )
    patch = dict(body.patch)
    if body.step == "welcome" and patch.get("recreate_canonical"):
        patch["mode"] = "recreate_canonical"
        patch["project_id"] = setup.CANONICAL["project_id"]
        patch["create_project"] = False
    elif body.step == "cloud_auth" and patch.get("prefer_new_project", True) and not patch.get("recreate_canonical"):
        patch.setdefault("mode", "create_new_project")
    if patch:
        setup.save_setup(patch)
    return setup.mark_step_complete(body.step)


@router.post("/provision/preview")
async def provision_preview(body: ProvisionRequest):
    if setup.existing_installation_detected():
        raise HTTPException(
            status_code=409,
            detail="An existing gateway is configured. Use connection checks and the image-only upgrade procedure; first-install provisioning is disabled.",
        )
    state = setup.load_setup()
    recreate = body.recreate_canonical or state.get("mode") == "recreate_canonical"
    project_id = body.project_id or state.get("project_id") or (
        setup.CANONICAL["project_id"] if recreate else None
    )
    if not project_id and body.create_project:
        project_id = setup.CANONICAL["project_id"]
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id required")
    plan = build_provision_plan(
        project_id=project_id,
        create_project=body.create_project and not recreate,
        recreate_canonical=recreate,
    )
    preview = {
        "plan": plan.to_dict(),
        "cost_notes": describe_cost_notes(plan),
        "steps": [run_provision_step(plan, s, dry_run=True) for s in plan.steps],
    }
    setup.save_setup({"provision_preview": preview, "project_id": plan.project_id})
    return preview


@router.post("/provision/execute")
async def provision_execute(body: ProvisionRequest):
    if setup.existing_installation_detected():
        raise HTTPException(
            status_code=409,
            detail="An existing gateway is configured. Provisioning and credential minting are disabled for existing installations.",
        )
    if not body.confirm_billable:
        raise HTTPException(
            status_code=400,
            detail="confirm_billable=true is required before enabling APIs or deploying Cloud Run",
        )
    state = setup.load_setup()
    recreate = body.recreate_canonical or state.get("mode") == "recreate_canonical"
    project_id = body.project_id or state.get("project_id") or setup.CANONICAL["project_id"]
    plan = build_provision_plan(
        project_id=project_id,
        create_project=body.create_project and not recreate,
        recreate_canonical=recreate,
    )
    step = body.execute_step or "enable_apis"
    result = await run_in_threadpool(run_provision_step, plan, step, False)
    setup.save_setup({"provision_result": result, "project_id": plan.project_id})
    return result


@router.post("/gateway/configure")
async def gateway_configure(body: GatewayConfig):
    catalog_sync.configure_gateway(body.cloud_gateway_url, body.catalog_write_key)
    url = body.cloud_gateway_url.rstrip("/")
    setup.save_setup(
        {
            "cloud_run_url": url,
            "oauth": {
                "authorization_url": f"{url}/oauth/authorize",
                "token_url": f"{url}/oauth/token",
                "fulfillment_url": f"{url}/fulfillment",
            },
        }
    )
    return {"setup": setup.public_status(), "catalog_sync": catalog_sync.public_sync_status()}



class PairingApproval(BaseModel):
    ticket: str = Field(min_length=32, max_length=128)
    code: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")


@router.get("/pairing")
async def pending_pairing(request: Request):
    try:
        return await run_in_threadpool(catalog_sync.pairing_gateway_request, "/internal/oauth/pending")
    except Exception:
        log.exception("Could not fetch Google Home pairing request")
        raise HTTPException(status_code=503, detail="Google Home approval is unavailable") from None


@router.post("/pairing/approve")
async def approve_pairing(body: PairingApproval, request: Request):
    try:
        return await run_in_threadpool(catalog_sync.pairing_gateway_request, "/internal/oauth/approve", body.model_dump())
    except Exception:
        log.exception("Could not approve Google Home pairing request")
        raise HTTPException(status_code=409, detail="Approval failed or expired. Start linking again.") from None

@router.post("/sync")
async def sync_google_home():
    def run():
        result = catalog_sync.push_catalog_now(True)
        if result.get("last_push_status") in {"failed", "pending"}:
            catalog_sync.resume_pending_push()
        return catalog_sync.public_sync_status()

    return await run_in_threadpool(run)


@router.get("/sync/status")
async def sync_status():
    return catalog_sync.public_sync_status()


@router.post("/link/confirm")
async def link_confirm(body: LinkConfirm):
    data = setup.save_setup({"link_confirmed": body.confirmed})
    if body.confirmed:
        setup.mark_step_complete("link_home")
    return setup.public_status() if body.confirmed else data


@router.post("/test")
async def connection_test(request: Request):
    settings = request.app.state.settings
    results = []

    def add(name: str, ok: bool, detail: str, recovery: str | None = None):
        results.append(
            {
                "name": name,
                "status": "ok" if ok else "needs_attention",
                "detail": detail,
                "recovery": recovery,
            }
        )

    plex = getattr(request.app.state, "plex", None)
    add("Plex", plex is not None, "Library linked" if plex else "Not linked", "Settings → Connect with Plex")

    cast = getattr(request.app.state, "cast", None)
    try:
        n = len(cast.discover()) if cast else 0
        add("Cast discovery", n > 0, f"{n} speaker(s) found", "Rescan speakers on Player")
    except Exception:
        log.exception("Cast discovery failed during connection test")
        add("Cast discovery", False, "Discovery failed; check bridge logs", "Check LAN / mDNS")

    sync = catalog_sync.public_sync_status()
    # Only a successful push counts as verified; "configured"/pending are not ready.
    sync_ok = bool(sync.get("cloud_gateway_configured")) and sync.get("status") == "succeeded"
    sync_detail = f"status={sync.get('status')}"
    if sync.get("last_error"):
        sync_detail = f"{sync_detail}; check bridge logs for the sync error"
    add(
        "Cloud catalog sync",
        sync_ok,
        sync_detail,
        "Configure gateway URL and Sync Google Home",
    )

    gh = setup.public_status()
    add(
        "Gateway URL",
        bool(gh.get("cloud_run_url")),
        gh.get("cloud_run_url") or "missing",
        "Complete Deploy Cloud Gateway step",
    )
    add(
        "Account linking",
        bool(gh.get("link_confirmed")),
        "User confirmed" if gh.get("link_confirmed") else "Unverified — confirm in wizard after linking",
        "Link in Google Home app, then confirm",
    )
    add(
        "Pub/Sub listener",
        bool(settings.enable_pubsub_listener),
        "enabled" if settings.enable_pubsub_listener else "disabled",
        "Set ENABLE_PUBSUB_LISTENER=true after subscriber credentials exist",
    )

    ok = all(r["status"] == "ok" for r in results if r["name"] != "Account linking")
    summary = "Google Home is ready" if ok and gh.get("link_confirmed") else "One step needs attention"
    payload = {"summary": summary, "results": results}
    setup.save_setup({"last_test": payload})
    setup.mark_step_complete("connection_test")
    return payload


@router.get("/console-values")
async def console_values():
    """Values to paste into Google Home Developer Console (secret masked)."""
    gh = setup.public_status()
    oauth = gh.get("oauth") or {}
    return {
        "integration_name": setup.CANONICAL["integration_name"],
        "device_type": setup.CANONICAL["device_type"],
        "client_id": oauth.get("client_id"),
        "client_secret_masked": "••••••••",
        "authorization_url": oauth.get("authorization_url"),
        "token_url": oauth.get("token_url"),
        "fulfillment_url": oauth.get("fulfillment_url"),
        "developer_console_url": "https://console.home.google.com/projects",
        "note": "Client secret is shown only when generated by the admin companion; it is never stored in wizard state.",
    }
