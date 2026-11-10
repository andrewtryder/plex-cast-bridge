"""OAuth client_secret and open-linking guards."""

from __future__ import annotations


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
    token = client.post('/oauth/token', data={
        'grant_type': 'authorization_code', 'code': query['code'][0], 'redirect_uri': params['redirect_uri'],
        'client_id': test_settings.google_client_id, 'client_secret': test_settings.google_client_secret.get_secret_value(),
    })
    assert token.status_code == 200
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
