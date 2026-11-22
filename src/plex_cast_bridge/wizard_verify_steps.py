"""Patch persistence and per-step checks for the Google Home setup wizard."""

from __future__ import annotations

import json
import urllib.request
from typing import Any

from . import catalog_sync
from . import google_home_setup as setup


def check(name: str, ok: bool, detail: str, recovery: str | None = None) -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail, "recovery": recovery}


def apply_verify_patch(patch: dict[str, Any]) -> None:
    """Persist non-secret inputs the user entered before verifying."""
    secret_keys = {"catalog_write_key", "client_secret"}
    safe = {k: v for k, v in patch.items() if k not in secret_keys}
    if "catalog_write_key" in patch:
        url = (patch.get("cloud_gateway_url") or setup.load_setup().get("cloud_run_url") or "").rstrip("/")
        if url:
            catalog_sync.configure_gateway(url, str(patch["catalog_write_key"]))
        else:
            catalog_sync.save_sync_state({"catalog_write_key": str(patch["catalog_write_key"])})
    if "cloud_gateway_url" in patch and patch["cloud_gateway_url"]:
        url = str(patch["cloud_gateway_url"]).rstrip("/")
        oauth = dict(setup.load_setup().get("oauth") or {})
        oauth.update(
            {
                "authorization_url": f"{url}/oauth/authorize",
                "token_url": f"{url}/oauth/token",
                "fulfillment_url": f"{url}/fulfillment",
            }
        )
        setup.save_setup({"cloud_run_url": url, "oauth": oauth})
        existing_key = (catalog_sync.load_sync_state().get("catalog_write_key") or "").strip()
        key = str(patch.get("catalog_write_key") or existing_key)
        if key:
            catalog_sync.configure_gateway(url, key)
        else:
            catalog_sync.save_sync_state({"cloud_gateway_url": url})
        safe.pop("cloud_gateway_url", None)
    if "project_id" in patch and patch["project_id"]:
        mode = patch.get("mode") or setup.load_setup().get("mode") or "create_new_project"
        setup.save_setup({"project_id": str(patch["project_id"]).strip(), "mode": mode})
    if "client_id" in patch and patch["client_id"]:
        oauth = dict(setup.load_setup().get("oauth") or {})
        oauth["client_id"] = str(patch["client_id"]).strip()
        setup.save_setup({"oauth": oauth})
        safe.pop("client_id", None)
    if "provision_progress" in patch and isinstance(patch["provision_progress"], dict):
        progress = dict(setup.load_setup().get("provision_progress") or {})
        progress.update(patch["provision_progress"])
        setup.save_setup({"provision_progress": progress})
        safe.pop("provision_progress", None)
    if "billing_url" in patch:
        setup.save_setup({"billing_url": patch["billing_url"]})
    companion_auth = safe.pop("companion_auth", None)
    if companion_auth is not None:
        setup.save_setup({"companion_auth": companion_auth})
    if safe:
        setup.save_setup(safe)


def verify_welcome(request_app_state: Any) -> list[dict[str, Any]]:
    plex = getattr(request_app_state, "plex", None)
    cast = getattr(request_app_state, "cast", None)
    speakers = getattr(cast, "last_discovered", None) or []
    checks = [
        check(
            "Plex library",
            plex is not None,
            "Connected" if plex else "Not linked",
            "Use Connect with Plex in the card above",
        ),
        check(
            "Cast speakers",
            len(speakers) > 0,
            f"{len(speakers)} discovered" if speakers else "None found",
            "Open Player and Rescan speakers",
        ),
    ]
    return checks


def verify_cloud_auth(state: dict[str, Any], patch: dict[str, Any]) -> list[dict[str, Any]]:
    project_id = state.get("project_id")
    mode = state.get("mode") or "create_new_project"
    using_existing = mode == "attach_existing"
    checks: list[dict[str, Any]] = [
        check(
            "Project ID",
            bool(project_id),
            project_id or ("Enter an existing GCP project ID" if using_existing else "Enter a new GCP project ID"),
            (
                "Use an ACTIVE project you already own"
                if using_existing
                else "Choose an unused project ID (deleted IDs cannot be reused)"
            ),
        )
    ]
    companion = patch.get("companion_auth") or state.get("companion_auth")
    if companion:
        checks.append(
            check(
                "gcloud on this Mac",
                bool(companion.get("ok")),
                companion.get("account") or companion.get("detail") or "companion reported",
                "gcloud auth login && gcloud auth application-default login",
            )
        )
    else:
        checks.append(
            check(
                "gcloud on this Mac",
                False,
                "Companion offline",
                "uv run python scripts/gh_setup_companion.py serve",
            )
        )
    progress = state.get("provision_progress") or {}
    project_status = progress.get("create_project") or progress.get("attach_project")
    checks.append(
        check(
            "GCP project",
            project_status in {"ok", "exists", "created"},
            (
                "Created"
                if project_status == "created"
                else "Using existing project"
                if project_status == "exists" and using_existing
                else "Already exists"
                if project_status == "exists"
                else str(project_status or ("Not attached yet" if using_existing else "Not created yet"))
            ),
            (
                "Click Use existing project on this Mac, then Verify"
                if using_existing
                else "Click Create project on this Mac, wait for it to finish, then Verify"
            ),
        )
    )
    return checks


def verify_prepare_services(state: dict[str, Any]) -> list[dict[str, Any]]:
    provision = state.get("provision_progress") or {}
    labels = {
        "enable_apis": "APIs enabled",
        "ensure_firestore": "Firestore",
        "ensure_pubsub": "Pub/Sub",
        "ensure_service_account": "Service account",
        "ensure_catalog_bucket": "Catalog bucket",
    }
    checks: list[dict[str, Any]] = []
    for name, label in labels.items():
        ok = bool(provision.get(name) in {"ok", "exists", "created"})
        checks.append(check(label, ok, str(provision.get(name) or "not run"), "Run provision steps on this Mac"))
    billing_ok = bool(state.get("billing_confirmed")) or provision.get("confirm_billing") == "ok"
    checks.append(
        check(
            "Billing linked",
            billing_ok,
            "Linked" if billing_ok else "Not linked yet",
            state.get("billing_url")
            or f"https://console.cloud.google.com/billing/linkedaccount?project={state.get('project_id') or ''}",
        )
    )
    return checks


def verify_subscriber_auth() -> list[dict[str, Any]]:
    sync = catalog_sync.load_sync_state()
    return [
        check(
            "Catalog write key",
            bool(sync.get("catalog_write_key")),
            "Stored on bridge" if sync.get("catalog_write_key") else "Missing",
            "Mint secrets on this Mac, then paste CATALOG_WRITE_KEY",
        )
    ]


def verify_deploy_gateway(state: dict[str, Any]) -> list[dict[str, Any]]:
    url = (state.get("cloud_run_url") or "").rstrip("/")
    checks: list[dict[str, Any]] = [
        check("Cloud Run URL", bool(url), url or "Paste the service URL", "Deploy gateway then paste URL")
    ]
    if url:
        try:
            req = urllib.request.Request(f"{url}/health", method="GET")
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            checks.append(
                check(
                    "Gateway health",
                    payload.get("status") == "ok",
                    "Healthy" if payload.get("status") == "ok" else json.dumps(payload)[:200],
                    "Redeploy Cloud Run / check IAM",
                )
            )
        except Exception as exc:
            checks.append(check("Gateway health", False, str(exc), "Confirm the service URL is public HTTPS"))
    return checks


def verify_developer_console(state: dict[str, Any]) -> list[dict[str, Any]]:
    oauth = state.get("oauth") or {}
    return [
        check("OAuth client ID", bool(oauth.get("client_id")), oauth.get("client_id") or "missing", "Paste OAuth client ID"),
        check(
            "Fulfillment URL",
            bool(oauth.get("fulfillment_url")),
            oauth.get("fulfillment_url") or "missing",
            "Save Cloud Run URL first",
        ),
        check(
            "Developer Console saved",
            bool(state.get("developer_console_done")),
            "Marked complete" if state.get("developer_console_done") else "Not marked yet",
            "https://console.home.google.com/projects",
        ),
    ]


def verify_link_home(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        check(
            "Google Home link",
            bool(state.get("link_confirmed")),
            "Confirmed" if state.get("link_confirmed") else "Not confirmed yet",
            "Google Home app → Works with Google → [test] Plex Cast Bridge",
        )
    ]


def verify_connection_test(state: dict[str, Any]) -> list[dict[str, Any]]:
    last = state.get("last_test") or {}
    checks: list[dict[str, Any]] = [
        check(
            "Connection test",
            bool(last),
            last.get("summary") or "Not run yet",
            "Click Run connection test",
        )
    ]
    if last:
        advisory = {"Account linking", "Pub/Sub listener"}
        bad = [r for r in (last.get("results") or []) if r.get("status") != "ok" and r.get("name") not in advisory]
        checks.append(
            check(
                "Critical checks",
                not bad,
                "All passed" if not bad else f"{len(bad)} issue(s)",
                bad[0].get("recovery") if bad else None,
            )
        )
    return checks


def verify_unknown_step(step: str) -> list[dict[str, Any]]:
    return [check("step", False, f"Unknown step {step}")]


_STEP_HANDLERS: dict[str, Any] = {
    "welcome": lambda state, patch, req: verify_welcome(req),
    "cloud_auth": lambda state, patch, req: verify_cloud_auth(state, patch),
    "prepare_services": lambda state, patch, req: verify_prepare_services(state),
    "subscriber_auth": lambda state, patch, req: verify_subscriber_auth(),
    "deploy_gateway": lambda state, patch, req: verify_deploy_gateway(state),
    "developer_console": lambda state, patch, req: verify_developer_console(state),
    "link_home": lambda state, patch, req: verify_link_home(state),
    "connection_test": lambda state, patch, req: verify_connection_test(state),
}


def run_step_checks(
    step: str, state: dict[str, Any], patch: dict[str, Any], request_app_state: Any
) -> list[dict[str, Any]]:
    handler = _STEP_HANDLERS.get(step)
    if handler is None:
        return verify_unknown_step(step)
    return handler(state, patch, request_app_state)
