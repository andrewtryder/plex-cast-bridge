"""Shared fake Firestore transaction backend; no Google services are contacted."""
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from google.cloud import firestore

from cloud_gateway.catalog_sync import CatalogCloudStore


@pytest.fixture
def stores(monkeypatch):
    lock = threading.RLock()
    docs = {}
    blobs = {}

    class Doc:
        def __init__(self, name):
            self.name = name

        def get(self, transaction=None):
            data = copy.deepcopy(docs.get(self.name))
            return SimpleNamespace(exists=data is not None, to_dict=lambda: data)

    class Tx:
        def set(self, ref, data, merge=False):
            docs[ref.name] = {**(docs.get(ref.name, {}) if merge else {}), **copy.deepcopy(data)}

    class DB:
        def collection(self, name):
            return self

        def document(self, name):
            return Doc(name)

        def transaction(self):
            return Tx()

    class Blob:
        def __init__(self, name):
            self.name = name

        def upload_from_string(self, payload, **kwargs):
            assert kwargs['if_generation_match'] == 0
            blobs[self.name] = payload

        def download_as_text(self):
            return blobs[self.name].decode()

    class Bucket:
        def blob(self, name):
            return Blob(name)

    monkeypatch.setattr(firestore, 'transactional', lambda fn: lambda tx: run(tx, fn))

    def run(tx, fn):
        with lock:
            return fn(tx)

    def make():
        store = CatalogCloudStore('test', mock=True)
        store.mock = False
        store._fs = DB()
        store._gcs = SimpleNamespace(bucket=lambda name: Bucket())
        return store

    return make(), make()


def test_concurrent_uploads_never_replace_newer(stores):
    a, b = stores
    def put(store, version):
        try:
            store.put_catalog({'version': version}, version)
        except ValueError:
            pass
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda args: put(*args), [(a, 1), (b, 2)]))
    assert a.get_meta().version == 2
    assert a.load_catalog() == {'version': 2}
    assert b.put_catalog({'version': 2}, 2).version == 2
    with pytest.raises(ValueError):
        b.put_catalog({'version': 'conflict'}, 2)


def test_stale_completion_cannot_clear_new_generation(stores):
    a, b = stores
    a.set_linked(True)
    a.put_catalog({'version': 1}, 1)
    def call():
        b.put_catalog({'version': 2}, 2)
        return {'state': 'accepted'}
    a._call_homegraph = call
    assert a.reconcile()['state'] == 'pending'
    assert b.get_meta().pending_sync
    b._call_homegraph = lambda: {'state': 'accepted'}
    assert b.reconcile()['state'] == 'accepted'
    assert b.get_meta().accepted_generation == b.get_meta().sync_generation


def test_unlink_in_flight_ignores_response_and_relink_retries(stores):
    a, b = stores
    a.set_linked(True)
    a.put_catalog({}, 1)
    def call():
        b.set_linked(False)
        return {'state': 'accepted'}
    a._call_homegraph = call
    assert a.reconcile()['state'] == 'skipped'
    assert not b.get_meta().pending_sync
    b.set_linked(True)
    assert b.get_meta().pending_sync
    b._call_homegraph = lambda: {'state': 'accepted'}
    assert b.reconcile()['state'] == 'accepted'


def test_failed_request_persists_backoff_across_instances(stores, monkeypatch):
    a, b = stores
    a.set_linked(True)
    a.put_catalog({}, 1)
    monkeypatch.setattr('cloud_gateway.catalog_sync.time.time', lambda: 1000)
    a._call_homegraph = lambda: {'state': 'failed', 'detail': 'HomeGraph HTTP 429'}
    assert a.reconcile()['state'] == 'failed'
    assert b.get_meta().retry_after == 1005
    assert b.reconcile()['detail'] == 'backoff'
    monkeypatch.setattr('cloud_gateway.catalog_sync.time.time', lambda: 1006)
    b._call_homegraph = lambda: {'state': 'accepted'}
    assert b.reconcile()['state'] == 'accepted'


def test_live_lease_excludes_other_instance_and_expires_after_restart(stores, monkeypatch):
    a, b = stores
    a.set_linked(True)
    a.put_catalog({}, 1)
    monkeypatch.setattr('cloud_gateway.catalog_sync.time.time', lambda: 1000)
    def call():
        assert b.reconcile()['detail'] == 'leased'
        raise SystemExit('simulated process termination')
    a._call_homegraph = call
    with pytest.raises(SystemExit):
        a.reconcile()
    assert b.reconcile()['detail'] == 'leased'
    monkeypatch.setattr('cloud_gateway.catalog_sync.time.time', lambda: 1091)
    b._call_homegraph = lambda: {'state': 'accepted'}
    assert b.reconcile()['state'] == 'accepted'


def test_server_error_is_retained_for_retry(stores, monkeypatch):
    a, b = stores
    a.set_linked(True)
    monkeypatch.setattr('cloud_gateway.catalog_sync.time.time', lambda: 2000)
    a._call_homegraph = lambda: {'state': 'failed', 'detail': 'HomeGraph HTTP 500'}
    assert a.reconcile()['state'] == 'failed'
    assert b.get_meta().pending_sync
    assert b.get_meta().last_sync_detail == 'HomeGraph HTTP 500'
    assert b.get_meta().retry_attempt == 1


@pytest.mark.parametrize('http_status', [429, 500])
def test_homegraph_http_errors_remain_pending(stores, monkeypatch, http_status):
    import google.auth
    import google.auth.transport.requests

    a, b = stores
    a.set_linked(True)
    monkeypatch.setattr(google.auth, 'default', lambda **kwargs: (object(), 'test'))
    monkeypatch.setattr(google.auth.transport.requests, 'AuthorizedSession', lambda credentials:
                        SimpleNamespace(post=lambda *args, **kwargs: SimpleNamespace(status_code=http_status)))
    result = a.reconcile()
    assert result == {'state': 'failed', 'detail': f'HomeGraph HTTP {http_status}'}
    assert b.get_meta().pending_sync
    assert b.get_meta().retry_attempt == 1
