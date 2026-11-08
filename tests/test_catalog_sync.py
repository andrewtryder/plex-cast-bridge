"""Cloud catalog versioning, Request Sync debounce, and DISCONNECT handling."""

from __future__ import annotations

import json
import time

from cloud_gateway.catalog_sync import CatalogCloudStore


def test_catalog_put_rejects_stale_version(client, auth_headers, test_settings):
    catalog = json.loads(open(test_settings.catalog_path).read())
    headers = {"X-API-Key": "test-webhook-key"}
    r1 = client.put(
        "/internal/catalog",
        headers=headers,
        json={"version": 1, "catalog": catalog, "request_sync": True},
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["meta"]["version"] == 1
    # Unlinked by default — upload must not imply link / Request Sync success.
    assert r1.json()["request_sync"]["state"] in {"succeeded", "pending", "skipped"}
    assert r1.json()["meta"].get("linked") is False

    r2 = client.put(
        "/internal/catalog",
        headers=headers,
        json={"version": 1, "catalog": catalog, "request_sync": False},
    )
    assert r2.status_code == 409


def test_catalog_upload_does_not_relink(client, test_settings):
    store: CatalogCloudStore = client.app.state.catalog_store
    store.set_linked(False)
    catalog = json.loads(open(test_settings.catalog_path).read())
    r = client.put(
        "/internal/catalog",
        headers={"X-API-Key": "test-webhook-key"},
        json={"version": 3, "catalog": catalog, "request_sync": True},
    )
    assert r.status_code == 200
    assert store.is_linked() is False
    assert r.json()["request_sync"]["state"] == "skipped"
    assert r.json()["catalog_upload"] == "succeeded"


def test_sync_uses_cloud_catalog_after_put(client, auth_headers, test_settings):
    catalog = json.loads(open(test_settings.catalog_path).read())
    catalog["speakers"] = [
        s for s in catalog["speakers"] if s.get("roomHint") == "Kitchen"
    ]
    catalog["inputs"] = ["Kitchen"]
    client.put(
        "/internal/catalog",
        headers={"X-API-Key": "test-webhook-key"},
        json={"version": 20, "catalog": catalog, "request_sync": False},
    )
    res = client.post(
        "/fulfillment",
        headers=auth_headers,
        json={
            "requestId": "r1",
            "inputs": [{"intent": "action.devices.SYNC", "payload": {}}],
        },
    )
    assert res.status_code == 200
    devices = res.json()["payload"]["devices"]
    ids = [d["id"] for d in devices]
    assert ids == ["plex-jukebox:Kitchen"]


def test_disconnect_stops_request_sync(client, auth_headers):
    store: CatalogCloudStore = client.app.state.catalog_store
    store.set_linked(True)
    client.post(
        "/fulfillment",
        headers=auth_headers,
        json={
            "requestId": "d1",
            "inputs": [{"intent": "action.devices.DISCONNECT", "payload": {}}],
        },
    )
    assert store.is_linked() is False
    result = store.request_sync(force=True)
    assert result["state"] == "skipped"


def test_request_sync_debounce_flushes_pending(client):
    store: CatalogCloudStore = client.app.state.catalog_store
    store.set_linked(True)
    store._sync_debounce_sec = 0.2
    first = store.request_sync(force=True)
    second = store.request_sync(force=False)
    assert first["state"] == "succeeded"
    assert second["state"] == "pending"
    assert store._pending_sync is True
    time.sleep(0.35)
    assert store._pending_sync is False
    assert store._meta.last_sync_state == "succeeded"


def test_production_init_does_not_silently_mock(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name.startswith("google.cloud"):
            raise ImportError("blocked for test")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    store = CatalogCloudStore(project_id="x", mock=False, allow_memory_fallback=False)
    assert store.mock is False
    assert store.storage_ok is False
    health = store.health()
    assert health["storage_ok"] is False
