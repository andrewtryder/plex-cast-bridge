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
_PUSH_LOCK = threading.Lock()
_STATE_FILE = "google_home_sync.json"
_pending_timer: threading.Timer | None = None
_retry_attempt = 0
_retry_stopped = False
_RETRY_BASE_SEC = 5.0
_RETRY_MAX_SEC = 300.0


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


def _schedule_push(delay_sec: float) -> None:
    global _pending_timer
    with _STATE_LOCK:
        if _retry_stopped:
            return
        if _pending_timer is not None:
            _pending_timer.cancel()
        _pending_timer = threading.Timer(delay_sec, _retry_push)
        _pending_timer.daemon = True
        _pending_timer.start()


def _retry_push() -> None:
    global _retry_attempt
    try:
        state = push_catalog_now()
    except Exception:
        log.exception("Scheduled catalog push failed")
        state = save_sync_state({"last_push_status": "failed", "last_error": "Scheduled push failed"})
    with _STATE_LOCK:
        current = load_sync_state()
        if current.get("version") != state.get("version"):
            return  # A newer update already scheduled its own push.
        status = current.get("last_push_status")
        if status in {"failed", "pending"} and current.get("last_catalog_upload") != "succeeded":
            _retry_attempt += 1
            delay = min(_RETRY_MAX_SEC, _RETRY_BASE_SEC * (2 ** min(_retry_attempt - 1, 10)))
            _schedule_push(delay)
        else:
            _retry_attempt = 0


def bump_and_schedule_push(delay_sec: float = 2.0) -> dict[str, Any]:
    """Persist the pending update before scheduling a debounced push."""
    with _STATE_LOCK:
        state = load_sync_state()
        state = save_sync_state({"version": int(state.get("version") or 0) + 1,
                                 "last_push_status": "pending", "last_catalog_upload": "pending", "last_error": None})
        _schedule_push(delay_sec)
        return state


def stop_pending_push() -> None:
    global _pending_timer, _retry_stopped
    with _STATE_LOCK:
        _retry_stopped = True
        if _pending_timer is not None:
            _pending_timer.cancel()
            _pending_timer = None


def push_catalog_now(force_sync: bool = True) -> dict[str, Any]:
    with _PUSH_LOCK:
        return _push_catalog_now(force_sync)


def _push_catalog_now(force_sync: bool = True) -> dict[str, Any]:
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
    # Reserve the version and read the snapshot without racing the pending marker.
    with _STATE_LOCK:
        catalog = json.loads(cat_path.read_text(encoding="utf-8"))
        version = int(load_sync_state().get("version") or 0) + 1
        save_sync_state({"version": version, "last_push_status": "pending", "last_catalog_upload": "pending"})
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
    def save_result(patch: dict[str, Any]) -> dict[str, Any]:
        with _STATE_LOCK:
            if int(load_sync_state().get("version") or 0) != version:
                return load_sync_state()
            return save_sync_state(patch)

    try:
        with request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        sync = (payload.get("request_sync") or {}).get("state")
        upload_ok = bool(payload.get("meta"))
        sync_detail = (payload.get("request_sync") or {}).get("detail")
        if not upload_ok or sync == "failed":
            overall = "failed"
        elif sync == "pending":
            overall = "pending"
        elif force_sync and sync not in {"succeeded", "accepted", "skipped"}:
            overall = "pending"
        else:
            overall = "succeeded"
        return save_result(
            {
                "last_push_status": overall,
                "last_catalog_upload": "succeeded" if upload_ok else "failed",
                "last_error": None if sync in {None, "succeeded", "accepted", "pending", "skipped"} else sync_detail,
                "last_cloud_meta": payload.get("meta"),
                "last_request_sync": payload.get("request_sync"),
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        log.warning("Catalog push HTTP %s: %s", exc.code, detail)
        return save_result(
            {
                "last_push_status": "failed",
                "last_catalog_upload": "failed",
                "last_error": f"HTTP {exc.code}: {detail}",
            }
        )
    except Exception as exc:
        log.warning("Catalog push failed: %s", exc)
        return save_result({"last_push_status": "failed", "last_catalog_upload": "failed", "last_error": str(exc)})


def reconcile_cloud() -> dict[str, Any]:
    """Wake the existing gateway and reconcile its persisted pending generation."""
    state = load_sync_state()
    url = (state.get("cloud_gateway_url") or "").rstrip("/")
    key = state.get("catalog_write_key") or ""
    if not url or not key:
        return {"state": "not_configured"}
    req = request.Request(f"{url}/internal/reconcile", data=b"", method="POST", headers={"X-API-Key": key})
    try:
        with request.urlopen(req, timeout=40) as response:
            result = json.loads(response.read().decode("utf-8"))
        with _STATE_LOCK:
            current = load_sync_state()
            if current.get("version") == state.get("version"):
                patch = {} if result.get("state") == "idle" else {"last_request_sync": result}
                cloud_version = int(result.get("catalog_version") or 0)
                if cloud_version > int(current.get("version") or 0):
                    patch["version"] = cloud_version
                if (result.get("state") == "accepted" and current.get("last_catalog_upload") == "succeeded"
                        and cloud_version == current.get("version")
                        and (current.get("last_cloud_meta") or {}).get("version") == cloud_version):
                    patch.update({"last_push_status": "succeeded", "last_error": None})
                elif result.get("state") == "failed":
                    patch.update({"last_push_status": "failed", "last_error": result.get("detail")})
                if patch:
                    save_sync_state(patch)
        return result
    except Exception as exc:
        log.warning("Cloud reconciliation failed: %s", exc)
        return {"state": "failed", "detail": "Cloud reconciliation unavailable"}


def configure_gateway(cloud_gateway_url: str, catalog_write_key: str) -> dict[str, Any]:
    return save_sync_state({
        "cloud_gateway_url": cloud_gateway_url.rstrip("/"),
        "catalog_write_key": catalog_write_key,
    })


def resume_pending_push() -> None:
    """Resume durable pending or failed work after a bridge restart."""
    global _retry_stopped
    with _STATE_LOCK:
        _retry_stopped = False
        state = load_sync_state()
        if state.get("last_push_status") in {"pending", "failed"} and state.get("last_catalog_upload") != "succeeded":
            _schedule_push(2.0)



def public_sync_status() -> dict[str, Any]:
    state = load_sync_state()
    status = state.get("last_push_status", "idle")
    req = state.get("last_request_sync") or {}
    linked = bool((state.get("last_cloud_meta") or {}).get("linked"))
    if status == "pending":
        label = "Changes pending"
    elif status == "not_configured":
        label = "Waiting for Google Home setup"
    elif status == "failed":
        label = "Sync failed — retry"
    elif req.get("state") == "skipped" and "unlink" in str(req.get("detail") or "").lower():
        label = "Waiting for account linking"
    elif req.get("state") == "accepted":
        label = "Google Home sync requested"
    elif status in {"succeeded", "configured"} and req.get("state") in {None, "succeeded", "skipped", "idle"}:
        label = "Catalog uploaded"
    else:
        label = status or "idle"
    return {
        "version": state.get("version", 0),
        "status": status,
        "label": label,
        "last_error": state.get("last_error"),
        "cloud_gateway_configured": bool(state.get("cloud_gateway_url")),
        "updated_at": state.get("updated_at"),
        "last_request_sync": state.get("last_request_sync"),
        "last_catalog_upload": state.get("last_catalog_upload"),
        "linked": linked,
        "note": (
            "Cast speakers are physical destinations. Virtual Plex devices are what Google Home lists. "
            "Room assignment for those virtual devices is managed in the Google Home app; room_hint is only a suggestion."
        ),
    }
