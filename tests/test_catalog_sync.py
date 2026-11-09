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
    # Unlinked by default — upload must not imply link / Request Sync success.
    assert r1.json()["request_sync"]["state"] in {"succeeded", "pending", "skipped"}
    assert r1.json()["meta"].get("linked") is False

    r2 = client.put(
        "/internal/catalog",
        headers=headers,
        json={"version": 1, "catalog": catalog, "request_sync": False},
    )
    assert r2.status_code == 200
    assert r2.json()["meta"]["version"] == 1


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
    assert devices[0]["name"]["name"] in {"Kitchen Plex", "Plex"}
    assert devices[0]["type"] == "action.devices.types.SPEAKER"


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


def test_request_sync_accepts_only_current_generation(client):
    store: CatalogCloudStore = client.app.state.catalog_store
    store.set_linked(True)
    first = store.request_sync(force=False)
    assert first["state"] == "accepted"
    assert store.get_meta().pending_sync is False
    assert store.get_meta().accepted_generation == store.get_meta().sync_generation
    assert store.request_sync()["state"] == "idle"


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


def test_failed_request_sync_is_not_reported_as_success(monkeypatch, tmp_path):
    from plex_cast_bridge import catalog_sync

    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text('{}')
    state = {"cloud_gateway_url": "https://example.test", "catalog_write_key": "key"}
    monkeypatch.setattr(catalog_sync, "load_sync_state", lambda: state.copy())
    monkeypatch.setattr(catalog_sync, "save_sync_state", lambda patch: state.update(patch) or state.copy())
    monkeypatch.setattr(catalog_sync.store, "get_catalog_path", lambda **kwargs: catalog_path)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps({"meta": {"version": 1}, "request_sync": {"state": "failed", "detail": "offline"}}).encode()

    monkeypatch.setattr(catalog_sync.request, "urlopen", lambda *args, **kwargs: Response())
    result = catalog_sync.push_catalog_now()
    assert result["last_catalog_upload"] == "succeeded"
    assert result["last_push_status"] == "failed"
    assert result["last_error"] == "offline"


def test_sync_does_not_relink_after_disconnect(client, auth_headers):
    store = client.app.state.catalog_store
    store.set_linked(True)
    client.post("/fulfillment", headers=auth_headers, json={"requestId": "d", "inputs": [{"intent": "action.devices.DISCONNECT"}]})
    response = client.post("/fulfillment", headers=auth_headers, json={"requestId": "s", "inputs": [{"intent": "action.devices.SYNC"}]})
    assert response.status_code == 200
    assert store.is_linked() is False


def test_local_retry_backoff_and_restart(monkeypatch, tmp_path):
    from plex_cast_bridge import catalog_sync as sync

    monkeypatch.setattr(sync.store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(sync, "_retry_stopped", False)
    monkeypatch.setattr(sync, "_retry_attempt", 0)
    scheduled = []
    monkeypatch.setattr(sync, "_schedule_push", scheduled.append)
    sync.save_sync_state({"version": 1, "last_push_status": "failed"})
    sync.resume_pending_push()
    assert scheduled == [2.0]
    monkeypatch.setattr(sync, "push_catalog_now", lambda: sync.load_sync_state())
    sync._retry_push()
    sync._retry_push()
    assert scheduled[-2:] == [5.0, 10.0]
    sync.save_sync_state({"last_push_status": "succeeded"})
    sync._retry_push()
    assert sync._retry_attempt == 0
    assert len(scheduled) == 3


def test_reconcile_endpoint_requires_key_and_does_not_relink(client):
    response = client.post('/internal/reconcile')
    assert response.status_code == 401
    response = client.post('/internal/reconcile', headers={'X-API-Key': 'test-webhook-key'})
    assert response.status_code == 200
    assert response.json()['state'] == 'skipped'
    assert response.json()['catalog_version'] == 0
    assert not client.app.state.catalog_store.is_linked()


def test_bridge_reconciliation_wakes_gateway_and_advances_cloud_version(monkeypatch, tmp_path):
    from plex_cast_bridge import catalog_sync as sync

    monkeypatch.setattr(sync.store, 'DATA_DIR', tmp_path)
    sync.save_sync_state({'cloud_gateway_url': 'https://gateway.test', 'catalog_write_key': 'secret',
                          'version': 3, 'last_push_status': 'failed', 'last_catalog_upload': 'succeeded',
                          'last_cloud_meta': {'version': 3}})
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return json.dumps({'state': 'accepted', 'catalog_version': 3}).encode()
    def send(req, timeout):
        assert req.full_url == 'https://gateway.test/internal/reconcile'
        assert req.get_method() == 'POST'
        assert req.get_header('X-api-key') == 'secret'
        return Response()
    monkeypatch.setattr(sync.request, 'urlopen', send)
    assert sync.reconcile_cloud()['state'] == 'accepted'
    assert sync.load_sync_state()['version'] == 3
    assert sync.public_sync_status()['label'] == 'Google Home sync requested'


def test_successful_upload_with_failed_homegraph_does_not_reupload(monkeypatch, tmp_path):
    from plex_cast_bridge import catalog_sync as sync

    monkeypatch.setattr(sync.store, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(sync, '_retry_attempt', 0)
    monkeypatch.setattr(sync, '_retry_stopped', False)
    scheduled = []
    monkeypatch.setattr(sync, '_schedule_push', scheduled.append)
    sync.save_sync_state({'version': 5, 'last_push_status': 'failed', 'last_catalog_upload': 'succeeded'})
    monkeypatch.setattr(sync, 'push_catalog_now', lambda: sync.load_sync_state())
    sync._retry_push()
    sync.resume_pending_push()
    assert scheduled == []
    assert sync.load_sync_state()['last_push_status'] == 'failed'


def test_new_catalog_change_rearms_upload_after_previous_success(monkeypatch, tmp_path):
    from plex_cast_bridge import catalog_sync as sync

    monkeypatch.setattr(sync.store, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(sync, '_retry_stopped', False)
    scheduled = []
    monkeypatch.setattr(sync, '_schedule_push', scheduled.append)
    sync.save_sync_state({'version': 5, 'last_push_status': 'succeeded', 'last_catalog_upload': 'succeeded'})
    state = sync.bump_and_schedule_push()
    assert state['last_catalog_upload'] == 'pending'
    assert scheduled == [2.0]
