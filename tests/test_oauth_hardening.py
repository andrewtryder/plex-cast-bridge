"""OAuth client_secret and open-linking guards."""

from __future__ import annotations


def test_firestore_client_retries_after_transient_initialization_failure(monkeypatch):
    from google.cloud import firestore
    from cloud_gateway import auth

    monkeypatch.setattr(auth, "_firestore_client", None)
    monkeypatch.setattr(auth, "_firestore_checked", False)
    expected_client = object()
    calls = 0

    def create_client():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient Firestore startup failure")
        return expected_client

    monkeypatch.setattr(firestore, "Client", create_client)

    assert auth.get_firestore_client() is None
    assert auth._firestore_checked is False
    assert auth.get_firestore_client() is expected_client
    assert auth.get_firestore_client() is expected_client
    assert calls == 2


def test_token_rejects_wrong_client_secret(client, test_settings):
    # Even if REQUIRE_ADMIN_PASSPHRASE were false, secret must still match.
    test_settings.require_admin_passphrase = False
    client.app.state.settings = test_settings
    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": "not-a-real-code",
            "redirect_uri": "https://oauth-redirect.googleusercontent.com/r/test",
            "client_id": test_settings.google_client_id,
            "client_secret": "definitely-wrong-secret",
        },
    )
    assert r.status_code == 401
    assert r.json().get("error") == "invalid_client"


def test_authorize_get_requires_dashboard_approval_when_enabled(client, test_settings):
    test_settings.require_admin_passphrase = True
    client.app.state.settings = test_settings
    r = client.get(
        "/oauth/authorize",
        params={
            "client_id": test_settings.google_client_id,
            "redirect_uri": "https://oauth-redirect.googleusercontent.com/r/test",
            "state": "xyz",
            "response_type": "code",
        },
    )
    assert r.status_code == 200
    assert "dashboard" in r.text.lower()
    assert 'type="password"' not in r.text


def test_authorization_requires_approval_even_when_setting_disabled(client, test_settings):
    test_settings.require_admin_passphrase = False
    client.app.state.settings = test_settings
    params = {
        "client_id": test_settings.google_client_id,
        "redirect_uri": "https://oauth-redirect.googleusercontent.com/r/test",
        "state": "xyz",
        "response_type": "code",
    }
    response = client.get("/oauth/authorize", params=params, follow_redirects=False)
    assert response.status_code == 200
    assert "dashboard" in response.text.lower()
    response = client.post("/oauth/authorize", data=params, follow_redirects=False)
    assert response.status_code == 401
    assert "location" not in response.headers


def test_password_free_pairing_requires_matching_authenticated_approval(client, test_settings):
    from urllib.parse import parse_qs, urlsplit

    params = {'client_id': test_settings.google_client_id,
              'redirect_uri': 'https://oauth-redirect.googleusercontent.com/r/test',
              'state': 'pair-state', 'response_type': 'code'}
    start = client.get('/oauth/authorize', params=params, follow_redirects=False)
    assert start.status_code == 200
    pending_response = client.get('/internal/oauth/pending', headers={'X-API-Key': 'test-catalog-key'})
    pending = pending_response.json()['pending']
    assert pending['code'] in start.text
    assert client.get('/internal/oauth/pending').status_code == 401
    assert client.get('/internal/oauth/pending', headers={'X-API-Key': 'test-webhook-key'}).status_code == 401
    assert client.post('/internal/oauth/approve', json=pending).status_code == 401
    assert client.post('/oauth/pairing/complete', data={'ticket': pending['ticket']}).status_code == 400
    bad = {**pending, 'code': '999999' if pending['code'] != '999999' else '000000'}
    assert client.post('/internal/oauth/approve', headers={'X-API-Key': 'test-catalog-key'}, json=bad).status_code == 409
    assert client.post('/internal/oauth/approve', headers={'X-API-Key': 'test-catalog-key'}, json=pending).status_code == 200
    done = client.post('/oauth/pairing/complete', data={'ticket': pending['ticket']}, follow_redirects=False)
    assert done.status_code == 302
    query = parse_qs(urlsplit(done.headers['location']).query)
    assert query['state'] == ['pair-state']
    previous_refresh = client.app.state.oauth_token_store.issue()
    client.app.state.catalog_store.set_linked(True)
    token = client.post('/oauth/token', data={
        'grant_type': 'authorization_code', 'code': query['code'][0], 'redirect_uri': params['redirect_uri'],
        'client_id': test_settings.google_client_id, 'client_secret': test_settings.google_client_secret.get_secret_value(),
    })
    assert token.status_code == 200
    assert token.json()["refresh_token"].startswith("pcb1.")
    assert client.app.state.oauth_token_store.is_active(token.json()["refresh_token"], "opaque-v1")
    assert not client.app.state.oauth_token_store.is_active(previous_refresh, "opaque-v1")
    assert client.post('/oauth/pairing/complete', data={'ticket': pending['ticket']}).status_code == 400
    assert client.post('/internal/oauth/approve', headers={'X-API-Key': 'test-catalog-key'}, json=pending).status_code == 409


def test_pairing_expires_and_new_request_supersedes_old(client, test_settings, monkeypatch):
    from cloud_gateway import approval_store

    now = [1000.0]
    monkeypatch.setattr(approval_store.time, 'time', lambda: now[0])
    params = {'client_id': test_settings.google_client_id,
              'redirect_uri': 'https://oauth-redirect.googleusercontent.com/r/test', 'state': 'x'}
    client.get('/oauth/authorize', params=params)
    old = client.get('/internal/oauth/pending', headers={'X-API-Key': 'test-catalog-key'}).json()['pending']
    client.get('/oauth/authorize', params=params)
    new = client.get('/internal/oauth/pending', headers={'X-API-Key': 'test-catalog-key'}).json()['pending']
    assert client.post('/internal/oauth/approve', headers={'X-API-Key': 'test-catalog-key'}, json=old).status_code == 409
    now[0] += approval_store.LIFETIME_SECONDS + 1
    assert client.post('/oauth/pairing/status', json={'ticket': new['ticket']}).json()['status'] == 'expired'
    assert client.post('/internal/oauth/approve', headers={'X-API-Key': 'test-catalog-key'}, json=new).status_code == 409


def test_refresh_token_survives_gateway_settings_reload(client, test_settings):
    """A legacy JWT migrates once and remains valid across simulated revisions."""
    from cloud_gateway.auth import create_legacy_token_pair
    from cloud_gateway.refresh_tokens import MemoryRefreshTokenStore

    original_tokens = create_legacy_token_pair(test_settings)
    client.app.state.catalog_store.set_linked(True)
    shared_state = client.app.state.oauth_token_store.state
    reloaded_settings = test_settings.model_copy(deep=True)
    client.app.state.settings = reloaded_settings
    client.app.state.oauth_token_store = MemoryRefreshTokenStore(shared_state)

    def refresh():
        return client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": original_tokens["refresh_token"],
                "client_id": reloaded_settings.google_client_id,
                "client_secret": reloaded_settings.google_client_secret.get_secret_value(),
            },
        )

    response = refresh()
    assert response.status_code == 200
    assert response.json()["refresh_token"] == original_tokens["refresh_token"]
    assert "token_hash" in shared_state and original_tokens["refresh_token"] not in str(shared_state)
    # A second store object represents another Cloud Run revision using Firestore.
    client.app.state.oauth_token_store = MemoryRefreshTokenStore(shared_state)
    response = refresh()
    assert response.status_code == 200
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    sync = client.post(
        "/fulfillment",
        headers=headers,
        json={"requestId": "upgrade-test", "inputs": [{"intent": "action.devices.SYNC"}]},
    )
    assert sync.status_code == 200
    assert sync.json()["payload"]["agentUserId"] == "plex-personal-user"


def test_authorization_code_retry_recovers_after_token_write_response_is_lost(client, test_settings, monkeypatch):
    from cloud_gateway.auth import create_authorization_code

    code = create_authorization_code(
        test_settings.google_client_id,
        "https://oauth-redirect.googleusercontent.com/r/test",
        test_settings,
    )
    store = client.app.state.oauth_token_store
    issue = store.issue_for_code
    calls = 0

    def commit_then_lose_response(token, code_jti, issued_at_us, *, allow_replace):
        nonlocal calls
        issue(token, code_jti, issued_at_us, allow_replace=allow_replace)
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated response loss after Firestore commit")

    monkeypatch.setattr(store, "issue_for_code", commit_then_lose_response)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "https://oauth-redirect.googleusercontent.com/r/test",
        "client_id": test_settings.google_client_id,
        "client_secret": test_settings.google_client_secret.get_secret_value(),
    }

    failed = client.post("/oauth/token", data=data)
    assert failed.status_code == 503
    recovered = client.post("/oauth/token", data=data)
    assert recovered.status_code == 200
    assert recovered.json()["refresh_token"].startswith("pcb1.")
    assert store.is_active(recovered.json()["refresh_token"], "opaque-v1")


def test_replayed_older_exchange_cannot_replace_a_newer_refresh_token(client, test_settings):
    from cloud_gateway.auth import create_authorization_code

    redirect_uri = "https://oauth-redirect.googleusercontent.com/r/test"

    def exchange(code):
        return client.post("/oauth/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": test_settings.google_client_id,
            "client_secret": test_settings.google_client_secret.get_secret_value(),
        })

    older = create_authorization_code(test_settings.google_client_id, redirect_uri, test_settings)
    older_response = exchange(older)
    assert older_response.status_code == 200
    newer = create_authorization_code(test_settings.google_client_id, redirect_uri, test_settings)
    newer_response = exchange(newer)
    assert newer_response.status_code == 200

    stale_replay = exchange(older)
    assert stale_replay.status_code == 400
    assert stale_replay.json()["error"] == "invalid_grant"
    store = client.app.state.oauth_token_store
    assert store.is_active(newer_response.json()["refresh_token"], "opaque-v1")
    assert not store.is_active(older_response.json()["refresh_token"], "opaque-v1")


def test_delayed_first_exchange_cannot_replace_a_newer_code(client, test_settings):
    from cloud_gateway.auth import create_authorization_code

    redirect_uri = "https://oauth-redirect.googleusercontent.com/r/test"

    def exchange(code):
        return client.post("/oauth/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": test_settings.google_client_id,
            "client_secret": test_settings.google_client_secret.get_secret_value(),
        })

    older = create_authorization_code(test_settings.google_client_id, redirect_uri, test_settings)
    newer = create_authorization_code(test_settings.google_client_id, redirect_uri, test_settings)
    newer_response = exchange(newer)
    assert newer_response.status_code == 200

    delayed_older_response = exchange(older)
    assert delayed_older_response.status_code == 400
    assert delayed_older_response.json()["error"] == "invalid_grant"
    assert client.app.state.oauth_token_store.is_active(newer_response.json()["refresh_token"], "opaque-v1")


def test_exchange_order_stays_monotonic_when_wall_clock_moves_back(monkeypatch, test_settings):
    from cloud_gateway import auth

    monkeypatch.setattr(auth, "get_firestore_client", lambda: None)
    clock = [2_000_000]
    monkeypatch.setattr(auth, "_wall_clock_exchange_order", lambda: clock[0])
    redirect_uri = "https://oauth-redirect.googleusercontent.com/r/test"
    first = auth.create_authorization_code(test_settings.google_client_id, redirect_uri, test_settings)
    first_order = auth.jwt.decode(
        first, test_settings.oauth_signing_secret.get_secret_value(), algorithms=["HS256"]
    )["exchange_order"]

    clock[0] = 100
    second = auth.create_authorization_code(test_settings.google_client_id, redirect_uri, test_settings)
    second_order = auth.jwt.decode(
        second, test_settings.oauth_signing_secret.get_secret_value(), algorithms=["HS256"]
    )["exchange_order"]
    assert second_order > first_order


def test_firestore_exchange_order_uses_persistent_sequence_and_active_token(monkeypatch, test_settings):
    from google.cloud import firestore

    from cloud_gateway import auth

    class Snapshot:
        def __init__(self, state):
            self.state = state
            self.exists = bool(state)

        def to_dict(self):
            return dict(self.state)

    class Document:
        def __init__(self, state=None):
            self.state = state or {}

        def get(self, transaction=None):
            return Snapshot(self.state)

    class Transaction:
        def set(self, document, value):
            document.state.clear()
            document.state.update(value)

    class Collection:
        def __init__(self):
            self.documents = {}

        def document(self, name):
            return self.documents.setdefault(name, Document())

    class Client:
        def __init__(self):
            self.collections = {}

        def collection(self, name):
            return self.collections.setdefault(name, Collection())

        def transaction(self):
            return Transaction()

    db = Client()
    db.collection("oauth_exchange_state").document("authorization_sequence").state["last_order"] = 100
    db.collection("plex_cast_bridge").document("oauth_refresh_state").state["exchange_issued_at_us"] = 200
    monkeypatch.setattr(auth, "get_firestore_client", lambda: db)
    monkeypatch.setattr(auth, "_wall_clock_exchange_order", lambda: 50)
    monkeypatch.setattr(firestore, "transactional", lambda function: function)

    assert auth._allocate_exchange_order(test_settings) == 201
    assert auth._allocate_exchange_order(test_settings) == 202


def test_previous_revision_authorization_code_order_remains_exchangeable(client, test_settings):
    import secrets
    from datetime import UTC, datetime, timedelta

    import jwt

    now = datetime.now(UTC)
    redirect_uri = "https://oauth-redirect.googleusercontent.com/r/test"
    legacy_code = jwt.encode({
        "sub": "admin",
        "type": "authorization_code",
        "client_id": test_settings.google_client_id,
        "redirect_uri": redirect_uri,
        "scope": "smart-home",
        "iat": int(now.timestamp()),
        "exchange_issued_at_us": int(now.timestamp()) * 1_000_000 + now.microsecond,
        "exp": int((now + timedelta(minutes=10)).timestamp()),
        "jti": secrets.token_hex(16),
    }, test_settings.oauth_signing_secret.get_secret_value(), algorithm="HS256")

    response = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": legacy_code,
        "redirect_uri": redirect_uri,
        "client_id": test_settings.google_client_id,
        "client_secret": test_settings.google_client_secret.get_secret_value(),
    })
    assert response.status_code == 200
    assert response.json()["refresh_token"].startswith("pcb1.")


def test_revoked_and_unlinked_refresh_tokens_fail(client, test_settings):
    from cloud_gateway.auth import create_legacy_token_pair

    client.app.state.catalog_store.set_linked(True)
    refresh = create_legacy_token_pair(test_settings)["refresh_token"]
    store = client.app.state.oauth_token_store
    assert store.migrate_legacy(refresh)
    store.revoke_current()
    response = client.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": refresh,
        "client_id": test_settings.google_client_id,
        "client_secret": test_settings.google_client_secret.get_secret_value(),
    })
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"

    forged = client.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": "not-a-signed-token",
        "client_id": test_settings.google_client_id,
        "client_secret": test_settings.google_client_secret.get_secret_value(),
    })
    assert forged.status_code == 400
    assert forged.json()["error"] == "invalid_grant"


def test_expired_legacy_token_only_works_after_persisted_migration(client, test_settings):
    from datetime import UTC, datetime, timedelta

    import jwt

    secret = test_settings.oauth_signing_secret.get_secret_value()
    payload = {
        "sub": "admin", "type": "refresh", "iat": int((datetime.now(UTC) - timedelta(days=90)).timestamp()),
        "exp": int((datetime.now(UTC) - timedelta(days=30)).timestamp()), "jti": "old-valid-jti",
    }
    expired = jwt.encode(payload, secret, algorithm="HS256")
    client.app.state.catalog_store.set_linked(True)
    data = {
        "grant_type": "refresh_token", "refresh_token": expired,
        "client_id": test_settings.google_client_id,
        "client_secret": test_settings.google_client_secret.get_secret_value(),
    }
    rejected = client.post("/oauth/token", data=data)
    assert rejected.status_code == 400
    client.app.state.oauth_token_store.activate(expired, "legacy-jwt")
    accepted = client.post("/oauth/token", data=data)
    assert accepted.status_code == 200
    assert accepted.json()["refresh_token"] == expired


def test_access_token_expiry_remains_enforced(client, test_settings):
    import jwt
    from datetime import UTC, datetime, timedelta

    token = jwt.encode({
        "sub": "admin", "type": "access", "iat": int((datetime.now(UTC) - timedelta(days=2)).timestamp()),
        "exp": int((datetime.now(UTC) - timedelta(days=1)).timestamp()), "jti": "expired-access",
    }, test_settings.oauth_signing_secret.get_secret_value(), algorithm="HS256")
    response = client.post("/fulfillment", headers={"Authorization": f"Bearer {token}"}, json={
        "requestId": "expired-access", "inputs": [{"intent": "action.devices.SYNC"}],
    })
    assert response.status_code == 401


def test_firestore_refresh_state_survives_store_recreation_and_revokes():
    from cloud_gateway.refresh_tokens import FirestoreRefreshTokenStore
    from google.api_core.exceptions import AlreadyExists

    class Snapshot:
        def __init__(self, data):
            self._data = data
            self.exists = data is not None

        def to_dict(self):
            return self._data

    class Document:
        def __init__(self, state):
            self.state = state

        def get(self):
            return Snapshot(dict(self.state) if self.state else None)

        def set(self, value):
            self.state.clear()
            self.state.update(value)

        def create(self, value):
            if self.state:
                raise AlreadyExists("exists")
            self.state.update(value)

        def update(self, value):
            self.state.update(value)

    class Collection:
        def __init__(self, doc):
            self.doc = doc

        def document(self, _name):
            return self.doc

    class Client:
        def __init__(self):
            self.doc = Document({})

        def collection(self, _name):
            return Collection(self.doc)

    firestore = Client()
    first_revision = FirestoreRefreshTokenStore(firestore)
    token = first_revision.issue()
    second_revision = FirestoreRefreshTokenStore(firestore)
    assert second_revision.is_active(token, "opaque-v1")
    assert token not in str(firestore.doc.state)
    first_revision.revoke_current()
    assert not second_revision.is_active(token, "opaque-v1")


def test_legacy_refresh_token_first_use_migrates_across_firestore_store_recreation(client, test_settings):
    import hashlib

    from google.api_core.exceptions import AlreadyExists

    from cloud_gateway.auth import create_legacy_token_pair
    from cloud_gateway.refresh_tokens import FirestoreRefreshTokenStore, validate_refresh_token

    class Snapshot:
        def __init__(self, state):
            self.state = state
            self.exists = bool(state)

        def to_dict(self):
            return dict(self.state)

    class Document:
        def __init__(self):
            self.state = {}

        def create(self, value):
            if self.state:
                raise AlreadyExists("exists")
            self.state.update(value)

        def get(self):
            return Snapshot(self.state)

    class Collection:
        def __init__(self, document):
            self.document_ref = document

        def document(self, _name):
            return self.document_ref

    class Client:
        def __init__(self):
            self.document = Document()

        def collection(self, _name):
            return Collection(self.document)

    firestore_client = Client()
    refresh_token = create_legacy_token_pair(test_settings)["refresh_token"]
    first_revision = FirestoreRefreshTokenStore(firestore_client)
    validate_refresh_token(refresh_token, test_settings, first_revision)
    assert firestore_client.document.state["token_format"] == "legacy-jwt"
    assert firestore_client.document.state["token_hash"] == hashlib.sha256(refresh_token.encode()).hexdigest()
    assert refresh_token not in str(firestore_client.document.state)

    next_revision = FirestoreRefreshTokenStore(firestore_client)
    validate_refresh_token(refresh_token, test_settings, next_revision)
    assert next_revision.is_active(refresh_token, "legacy-jwt")


def test_firestore_code_exchange_retry_survives_store_recreation(monkeypatch):
    import pytest

    from google.cloud import firestore

    from cloud_gateway.refresh_tokens import FirestoreRefreshTokenStore, RefreshTokenConflict

    class Snapshot:
        def __init__(self, state):
            self.state = state
            self.exists = bool(state)

        def to_dict(self):
            return dict(self.state)

    class Document:
        def __init__(self):
            self.state = {}

        def get(self, transaction=None):
            return Snapshot(self.state)

    class Transaction:
        def set(self, document, value):
            document.state.clear()
            document.state.update(value)

    class Collection:
        def __init__(self, document):
            self._document = document

        def document(self, _name):
            return self._document

    class Client:
        def __init__(self):
            self.document = Document()

        def collection(self, _name):
            return Collection(self.document)

        def transaction(self):
            return Transaction()

    monkeypatch.setattr(firestore, "transactional", lambda function: function)
    client = Client()
    token = "pcb1.durable-test-token"
    first_revision = FirestoreRefreshTokenStore(client)
    first_revision.issue_for_code(token, "code-jti-1", 100, allow_replace=True)

    next_revision = FirestoreRefreshTokenStore(client)
    next_revision.issue_for_code(token, "code-jti-1", 100, allow_replace=False)
    assert next_revision.is_active(token, "opaque-v1")
    with pytest.raises(RefreshTokenConflict):
        next_revision.issue_for_code("pcb1.old-code-token", "code-jti-old", 50, allow_replace=True)
    assert next_revision.is_active(token, "opaque-v1")
