"""Tests for Plex JWT connection store, auth service, and API routes."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOW_UNAUTHENTICATED_LOCAL_API", "true")
    import plex_cast_bridge.catalog_store as catalog_store

    monkeypatch.setattr(catalog_store, "DATA_DIR", Path(tmp_path))
    return Path(tmp_path)


def test_connection_store_roundtrip_and_public_status(data_dir):
    from plex_cast_bridge import plex_connection_store as store

    saved = store.save_connection(
        {
            "mode": "jwt",
            "jwt_token": "secret-jwt",
            "server_url": "http://plex:32400",
            "music_section": "Music",
            "server_name": "Home",
            "username": "atr",
        }
    )
    assert saved["client_identifier"]
    assert store.connection_path().exists()
    mode = store.connection_path().stat().st_mode & 0o777
    assert mode == 0o600 or mode == 0o644  # some FS ignore chmod

    loaded = store.load_connection()
    assert loaded["jwt_token"] == "secret-jwt"

    public = store.public_status(loaded, plex_ready=True)
    assert public["linked"] is True
    assert public["username"] == "atr"
    assert "jwt_token" not in public
    assert "access_token" not in public

    store.clear_connection()
    assert store.load_connection() is None


def test_plex_auth_start_and_poll_authorized(data_dir, monkeypatch):
    from plex_cast_bridge.plex_auth import PlexAuthService

    fake_login = MagicMock()
    fake_login.jwtToken = None
    fake_login.finished = False
    fake_login.expired = False
    fake_login.oauthUrl.return_value = "https://app.plex.tv/auth/#/?code=abc"
    fake_login.generateKeypair = MagicMock()
    fake_login.run = MagicMock()
    fake_login.stop = MagicMock()
    ctor_kwargs = {}

    def fake_ctor(**kwargs):
        ctor_kwargs.update(kwargs)
        return fake_login

    monkeypatch.setattr("plex_cast_bridge.plex_auth.MyPlexJWTLogin", fake_ctor)

    auth = PlexAuthService()
    started = auth.start_connect(forward_url="http://localhost/#settings")
    assert started["state"] == "pending"
    assert "plex.tv" in started["auth_url"]
    assert ctor_kwargs.get("keypair") is None
    fake_login.generateKeypair.assert_called_once()

    # Second connect must reuse existing key files instead of regenerating.
    from plex_cast_bridge import plex_connection_store as store

    priv, pub = store.keypair_paths()
    priv.write_bytes(b"\0" * 32)
    pub.write_bytes(b"\1" * 32)
    ctor_kwargs.clear()
    fake_login.generateKeypair.reset_mock()
    started_again = auth.start_connect(forward_url="http://localhost/#settings")
    assert started_again["state"] == "pending"
    assert ctor_kwargs.get("keypair") == (str(priv), str(pub))
    fake_login.generateKeypair.assert_not_called()

    pending = auth.connect_status()
    assert pending["state"] == "pending"

    account = MagicMock()
    account.username = "atr"
    account.friendlyName = "ATR"
    resource = MagicMock()
    resource.provides = "server"
    resource.clientIdentifier = "srv-1"
    resource.name = "Home PMS"
    resource.owned = True
    account.resources.return_value = [resource]

    monkeypatch.setattr("plex_cast_bridge.plex_auth.MyPlexAccount", lambda token: account)
    fake_login.jwtToken = "jwt-token"
    fake_login.finished = True

    authorized = auth.connect_status()
    assert authorized["state"] == "authorized"
    servers = auth.list_servers()
    assert servers[0]["id"] == "srv-1"


def test_plex_auth_select_builds_service(data_dir, monkeypatch):
    from plex_cast_bridge.plex_auth import PlexAuthService

    auth = PlexAuthService()
    account = MagicMock()
    account.username = "atr"
    account.friendlyName = "ATR"
    resource = MagicMock()
    resource.provides = "server"
    resource.clientIdentifier = "srv-1"
    resource.name = "Home PMS"
    server = MagicMock()
    server._baseurl = "http://192.168.1.10:32400/"
    server._token = "access-tok"
    section = MagicMock()
    server.library.section.return_value = section
    resource.connect.return_value = server
    account.resources.return_value = [resource]
    auth._account = account

    fake_plex = MagicMock()
    fake_plex.refresh.return_value = {"artists": []}
    monkeypatch.setattr("plex_cast_bridge.plex_auth.PlexService", lambda *a, **k: fake_plex)

    plex = auth.select_server("srv-1", "Music")
    assert plex is fake_plex
    from plex_cast_bridge import plex_connection_store as store

    data = store.load_connection()
    assert data["server_url"] == "http://192.168.1.10:32400"
    assert data["music_section"] == "Music"
    assert data["access_token"] == "access-tok"


def test_init_plex_service_returns_none_when_unlinked(data_dir, monkeypatch):
    from plex_cast_bridge.config import Settings
    from plex_cast_bridge.plex_auth import PlexAuthService, init_plex_service

    settings = Settings(
        plex_token=None,
        local_api_key=None,
        allow_unauthenticated_local_api=True,
    )
    auth = PlexAuthService()
    assert init_plex_service(settings, auth, attempts=1) is None


def test_init_plex_service_env_token(data_dir, monkeypatch):
    from plex_cast_bridge.config import Settings
    from plex_cast_bridge.plex_auth import PlexAuthService, init_plex_service

    fake = MagicMock()
    fake.refresh.return_value = {"artists": []}
    monkeypatch.setattr("plex_cast_bridge.plex_auth.PlexService", lambda *a, **k: fake)

    settings = Settings(
        plex_url="http://plex:32400",
        plex_token=SecretStr("tok"),
        plex_music_section="Music",
        local_api_key=None,
        allow_unauthenticated_local_api=True,
    )
    auth = PlexAuthService()
    plex = init_plex_service(settings, auth, attempts=1)
    assert plex is fake


def test_command_returns_503_when_plex_unlinked(monkeypatch):
    from plex_cast_bridge.main import app

    app.state.plex = None
    app.state.plex_auth = MagicMock()
    app.state.plex_auth.public_status.return_value = {"linked": False, "plex_ready": False}
    app.state.cast = MagicMock()
    app.state.cast.speakers = {}
    app.state.cast.last_discovered = []
    app.state.cast.discover.return_value = []
    app.state.settings = SimpleNamespace(
        default_speaker_name="Room",
        local_api_key=SecretStr("k"),
    )
    app.state.dj = MagicMock()

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/api/command",
        headers={"X-API-Key": "k"},
        json={"query": "DJ Shadow", "speaker": "Room"},
    )
    assert resp.status_code == 503
    assert "not linked" in resp.json()["detail"].lower()

    status = client.get("/api/plex/status", headers={"X-API-Key": "k"})
    assert status.status_code == 200


def test_dashboard_has_settings_and_connect():
    html = Path(__file__).resolve().parents[1].joinpath(
        "src/plex_cast_bridge/templates/dashboard.html"
    ).read_text(encoding="utf-8")
    assert 'id="settingsPage"' in html
    assert 'href="#settings"' in html
    assert "Connect with Plex" in html
    assert "/api/plex/connect/start" in html
    assert 'id="playerPage"' in html
    assert 'id="djPage"' in html
    assert "Demo data only" not in html


def test_settings_allows_missing_plex_token():
    from plex_cast_bridge.config import Settings

    s = Settings(plex_token=None, local_api_key=None, allow_unauthenticated_local_api=True)
    assert s.plex_token is None
