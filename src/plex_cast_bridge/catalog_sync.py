"""Push local catalog versions to the Cloud Run gateway for Google Home SYNC."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any
from urllib import error, request

from . import catalog_store as store

log = logging.getLogger(__name__)

_STATE_LOCK = threading.RLock()
_STATE_FILE = "google_home_sync.json"
_pending_timer: threading.Timer | None = None


def _state_path() -> Path:
    store.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return store.DATA_DIR / _STATE_FILE


def load_sync_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {
            "version": 0,
            "last_push_status": "idle",
            "last_error": None,
            "cloud_gateway_url": None,
            "updated_at": None,
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 0, "last_push_status": "idle"}


def save_sync_state(patch: dict[str, Any]) -> dict[str, Any]:
    with _STATE_LOCK:
        data = load_sync_state()
        data.update(patch)
        path = _state_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return data


def bump_and_schedule_push(delay_sec: float = 2.0) -> dict[str, Any]:
    """Increment local catalog version and debounce an outbound push."""
    global _pending_timer
    with _STATE_LOCK:
        state = load_sync_state()
        version = int(state.get("version") or 0) + 1
        state = save_sync_state(
            {
                "version": version,
                "last_push_status": "pending",
                "last_error": None,
            }
        )
        if _pending_timer is not None:
            _pending_timer.cancel()
        _pending_timer = threading.Timer(delay_sec, lambda: push_catalog_now())
        _pending_timer.daemon = True
        _pending_timer.start()
        return state


def push_catalog_now(force_sync: bool = True) -> dict[str, Any]:
    state = load_sync_state()
    url = (state.get("cloud_gateway_url") or "").rstrip("/")
    write_key = state.get("catalog_write_key") or ""
    if not url or not write_key:
        return save_sync_state(
            {
                "last_push_status": "not_configured",
                "last_error": "cloud_gateway_url or catalog_write_key missing",
            }
        )

    cat_path = store.get_catalog_path(for_writing=False)
    if not cat_path.exists():
        return save_sync_state(
            {
                "last_push_status": "failed",
                "last_error": f"catalog missing: {cat_path}",
            }
        )
    catalog = json.loads(cat_path.read_text(encoding="utf-8"))
    # Always advance version on an explicit push so retries never 409 against cloud.
    version = int(state.get("version") or 0) + 1
    save_sync_state({"version": version})
    body = json.dumps(
        {
            "version": version,
            "catalog": catalog,
            "request_sync": force_sync,
        }
    ).encode("utf-8")
    req = request.Request(
        f"{url}/internal/catalog",
        data=body,
        method="PUT",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": write_key,
        },
    )
    try:
        with request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        sync = (payload.get("request_sync") or {}).get("state")
        # Catalog upload success is enough for bridge health; HomeGraph Request Sync can
        # fail when the account is not linked yet (or Home Console project differs).
        sync_ok = sync in {None, "succeeded", "pending", "skipped", "failed"}
        sync_detail = (payload.get("request_sync") or {}).get("detail")
        return save_sync_state(
            {
                "last_push_status": "succeeded" if sync_ok and payload.get("meta") else (sync or "failed"),
                "last_error": None if sync in {None, "succeeded", "pending", "skipped"} else sync_detail,
                "last_cloud_meta": payload.get("meta"),
                "last_request_sync": payload.get("request_sync"),
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        log.warning("Catalog push HTTP %s: %s", exc.code, detail)
        return save_sync_state(
            {
                "last_push_status": "failed",
                "last_error": f"HTTP {exc.code}: {detail}",
            }
        )
    except Exception as exc:
        log.warning("Catalog push failed: %s", exc)
        return save_sync_state({"last_push_status": "failed", "last_error": str(exc)})


def configure_gateway(cloud_gateway_url: str, catalog_write_key: str) -> dict[str, Any]:
    return save_sync_state(
        {
            "cloud_gateway_url": cloud_gateway_url.rstrip("/"),
            "catalog_write_key": catalog_write_key,
            "last_push_status": "configured",
        }
    )


def public_sync_status() -> dict[str, Any]:
    state = load_sync_state()
    return {
        "version": state.get("version", 0),
        "status": state.get("last_push_status", "idle"),
        "last_error": state.get("last_error"),
        "cloud_gateway_configured": bool(state.get("cloud_gateway_url")),
        "updated_at": state.get("updated_at"),
        "last_request_sync": state.get("last_request_sync"),
        "note": "Google Home room assignments are managed in the Google Home app; catalog roomHint is only a hint.",
    }
