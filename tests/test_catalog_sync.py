"""Cloud catalog versioning, Request Sync debounce, and DISCONNECT handling."""

from __future__ import annotations

import json

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
    assert r1.json()["request_sync"]["state"] in {"succeeded", "pending", "skipped"}

    r2 = client.put(
        "/internal/catalog",
        headers=headers,
        json={"version": 1, "catalog": catalog, "request_sync": False},
    )
    assert r2.status_code == 409


def test_sync_uses_cloud_catalog_after_put(client, auth_headers, test_settings):
    catalog = json.loads(open(test_settings.catalog_path).read())
    catalog["speakers"] = [
        s for s in catalog["speakers"] if s.get("roomHint") == "Kitchen"
    ]
    catalog["inputs"] = ["Kitchen"]
    client.put(
        "/internal/catalog",
        headers={"X-API-Key": "test-webhook-key"},
        json={"version": 2, "catalog": catalog, "request_sync": False},
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


def test_request_sync_debounce(client):
    store: CatalogCloudStore = client.app.state.catalog_store
    store.set_linked(True)
    first = store.request_sync(force=True)
    second = store.request_sync(force=False)
    assert first["state"] == "succeeded"
    assert second["state"] == "pending"
