"""Resumable Google Home wizard state (non-secret) under DATA_DIR."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import catalog_store as store

log = logging.getLogger(__name__)

SETUP_FILE = "google_home_setup.json"
CANONICAL = {
    "project_id": "plex-cast-bridge-home",
    "project_name": "Plex Cast Bridge",
    "region": "us-central1",
    "service_name": "plex-google-home-fulfillment",
    "topic_id": "plex-commands",
    "subscription_id": "plex-commands-local-worker",
    "dlq_topic_id": "plex-commands-dlq",
    "service_account": "plex-fulfillment-sa",
    "integration_name": "Plex Cast Bridge",
    "agent_user_id": "plex-personal-user",
    "device_type": "TV or Audio Video Receiver",
}

STEPS = [
    "welcome",
    "cloud_auth",
    "prepare_services",
    "subscriber_auth",
    "deploy_gateway",
    "developer_console",
    "link_home",
    "connection_test",
]


def _path() -> Path:
    store.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return store.DATA_DIR / SETUP_FILE


def _utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def detect_installation_operation() -> str:
    """Detect installation state from persistent config, never from wizard-file presence."""
    try:
        from . import catalog_sync

        state = catalog_sync.load_sync_state()
        if not (state.get("cloud_gateway_url") or "").strip():
            return "first_time_installation"
        if state.get("last_push_status") == "failed" or state.get("last_error"):
            return "existing_installation_recovery"
        return "application_upgrade"
    except Exception:
        log.exception("Could not inspect persistent Google Home gateway configuration")
        # A failed detection must not enable first-install provisioning.
        return "existing_installation_recovery"


def existing_installation_detected() -> bool:
    return detect_installation_operation() != "first_time_installation"


def default_state() -> dict[str, Any]:
    operation = detect_installation_operation()
    existing = operation != "first_time_installation"
    return {
        "schema_version": 1,
        "mode": operation,
        "installation_detected": existing,
        "current_step": "connection_test" if existing else "welcome",
        "completed_steps": [],
        "canonical": dict(CANONICAL),
        "project_id": None,
        "cloud_run_url": None,
        "oauth": {
            "client_id": None,
            "authorization_url": None,
            "token_url": None,
            "fulfillment_url": None,
            # client_secret never stored here
        },
        "provision_preview": None,
        "provision_result": None,
        # Local wizard state is not evidence that the remote Google Home account is unlinked.
        "link_confirmed": None if existing else False,
        "last_test": None,
        "updated_at": None,
        "errors": {},
    }


def load_setup() -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return default_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        base = default_state()
        base.update(data)
        data = base
    except Exception as exc:
        log.warning("Could not read google home setup state: %s", exc)
        data = default_state()
    if existing_installation_detected():
        data["mode"] = detect_installation_operation()
        data["installation_detected"] = True
        if not data.get("completed_steps"):
            data["current_step"] = "connection_test"
        if data.get("link_confirmed") is not True:
            data["link_confirmed"] = None
    else:
        data["installation_detected"] = False
    return data


def save_setup(patch: dict[str, Any]) -> dict[str, Any]:
    data = load_setup()
    data.update(patch)
    if existing_installation_detected():
        data["mode"] = detect_installation_operation()
        data["installation_detected"] = True
        if data.get("link_confirmed") is not True:
            data["link_confirmed"] = None
    data["updated_at"] = _utcnow()
    # Never persist secrets if a caller accidentally includes them.
    for key in ("client_secret", "admin_passphrase", "oauth_signing_secret", "catalog_write_key"):
        data.pop(key, None)
        if isinstance(data.get("oauth"), dict):
            data["oauth"].pop("client_secret", None)
    path = _path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return data


def mark_step_complete(step: str) -> dict[str, Any]:
    data = load_setup()
    completed = list(data.get("completed_steps") or [])
    if step not in completed:
        completed.append(step)
    idx = STEPS.index(step) if step in STEPS else -1
    next_step = STEPS[min(idx + 1, len(STEPS) - 1)] if idx >= 0 else data.get("current_step")
    return save_setup({"completed_steps": completed, "current_step": next_step})


def public_status() -> dict[str, Any]:
    data = load_setup()
    verified = {step: bool(data.get(f"verified_{step}")) for step in STEPS}
    canonical = dict(data.get("canonical") or CANONICAL)
    if data.get("installation_detected") and not data.get("project_id"):
        # Avoid presenting the example project as the installed project's identity.
        canonical["project_id"] = None
    return {
        "current_step": data.get("current_step"),
        "completed_steps": data.get("completed_steps") or [],
        "mode": data.get("mode"),
        "project_id": data.get("project_id"),
        "cloud_run_url": data.get("cloud_run_url"),
        "oauth": {
            "client_id": (data.get("oauth") or {}).get("client_id"),
            "authorization_url": (data.get("oauth") or {}).get("authorization_url"),
            "token_url": (data.get("oauth") or {}).get("token_url"),
            "fulfillment_url": (data.get("oauth") or {}).get("fulfillment_url"),
            "client_secret_set": False,
        },
        "link_confirmed": data.get("link_confirmed"),
        "installation_detected": bool(data.get("installation_detected")),
        "recommended_operation": data.get("mode"),
        "billing_confirmed": bool(data.get("billing_confirmed")),
        "billing_url": data.get("billing_url"),
        "developer_console_done": bool(data.get("developer_console_done")),
        "provision_progress": data.get("provision_progress") or {},
        "companion_auth": data.get("companion_auth"),
        "verified": verified,
        "last_verify": data.get("last_verify"),
        "last_test": data.get("last_test"),
        "canonical": canonical,
        "updated_at": data.get("updated_at"),
        "errors": data.get("errors") or {},
        "steps": STEPS,
    }
