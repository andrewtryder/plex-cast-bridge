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


def test_authorize_get_requires_passphrase_form_when_enabled(client, test_settings):
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
    assert "passphrase" in r.text.lower() or "password" in r.text.lower() or "Admin" in r.text


def test_authorization_requires_passphrase_even_when_setting_disabled(client, test_settings):
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
    assert "passphrase" in response.text.lower()
    response = client.post("/oauth/authorize", data=params, follow_redirects=False)
    assert response.status_code == 401
    assert "location" not in response.headers
