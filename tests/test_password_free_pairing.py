"""Linking requires an authenticated dashboard approval matching the phone code."""
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import SecretStr

from plex_cast_bridge import catalog_store
from plex_cast_bridge.main import app as local_app


def test_dashboard_to_gateway_pairing_flow(client, test_settings, tmp_path, monkeypatch):
    monkeypatch.setattr(catalog_store, 'DATA_DIR', tmp_path)
    params = {'client_id': test_settings.google_client_id,
              'redirect_uri': 'https://oauth-redirect.googleusercontent.com/r/test', 'state': 'state123'}
    page = client.get('/oauth/authorize', params=params)
    assert page.status_code == 200
    assert 'type="password"' not in page.text
    calls = []
    def gateway(path, body=None):
        calls.append(path)
        headers = {'X-API-Key': 'test-catalog-key'}
        result = client.get(path, headers=headers) if body is None else client.post(path, headers=headers, json=body)
        assert result.status_code == 200
        return result.json()
    with patch('plex_cast_bridge.catalog_sync.pairing_gateway_request', side_effect=gateway):
        with TestClient(local_app) as dashboard:
            local_app.state.settings.local_api_key = SecretStr('dashboard-key')
            assert dashboard.get('/api/google-home/pairing').status_code == 401
            headers = {'X-API-Key': 'dashboard-key'}
            pending = dashboard.get('/api/google-home/pairing', headers=headers).json()['pending']
            assert pending['code'] in page.text
            assert 'test-catalog-key' not in dashboard.get('/api/google-home/pairing', headers=headers).text
            assert dashboard.post('/api/google-home/pairing/approve', headers=headers, json=pending).status_code == 200
    assert calls == ['/internal/oauth/pending', '/internal/oauth/pending', '/internal/oauth/approve']
    status = client.post('/oauth/pairing/status', json={'ticket': pending['ticket']})
    assert status.json()['status'] == 'approved'
    done = client.post('/oauth/pairing/complete', data={'ticket': pending['ticket']}, follow_redirects=False)
    assert done.status_code == 302
    q = parse_qs(urlsplit(done.headers['location']).query)
    assert q['state'] == ['state123']
    assert client.post('/oauth/pairing/complete', data={'ticket': pending['ticket']}).status_code == 400


def test_shared_firestore_approval_can_be_consumed_once(monkeypatch):
    import copy
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from google.cloud import firestore
    from cloud_gateway.approval_store import ApprovalStore

    lock = threading.RLock()
    state = {}
    class Ref:
        def get(self, transaction=None):
            data = copy.deepcopy(state)
            return SimpleNamespace(exists=bool(data), to_dict=lambda: data)
    ref = Ref()
    class Tx:
        def set(self, _ref, data):
            state.clear()
            state.update(copy.deepcopy(data))
    class DB:
        def collection(self, _name): return self
        def document(self, _name): return ref
        def transaction(self): return Tx()
    def transactional(fn):
        def run(tx):
            with lock:
                return fn(tx)
        return run
    monkeypatch.setattr(firestore, 'transactional', transactional)
    a, b = ApprovalStore(DB()), ApprovalStore(DB())
    request = a.create(client_id='client', redirect_uri='https://oauth-redirect.googleusercontent.com/r/test',
                       state='s', scope='')
    assert b.approve(request['ticket'], request['code'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda store: store.consume(request['ticket']), (a, b)))
    assert sum(item is not None for item in results) == 1
