"""Dashboard enrollment, expiry, and same-origin protections."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from plex_cast_bridge import dashboard_sessions as store
from plex_cast_bridge.main import app

BASE = "http://127.0.0.1:8000"
ORIGIN = {"Origin": BASE}


@pytest.fixture
def dashboard(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app.state, "settings", SimpleNamespace(local_api_key=SecretStr("secret-key")), raising=False)
    return TestClient(app, base_url=BASE)


def migrate(client):
    return client.post("/api/session/migrate", headers={**ORIGIN, "X-API-Key": "secret-key"})


def test_migration_session_origin_logout_and_header_access(dashboard):
    assert dashboard.get("/api/activity").status_code == 401
    assert dashboard.get("/api/activity?api_key=secret-key").status_code == 400
    assert dashboard.get("/?api_key=secret-key").status_code == 400
    assert dashboard.get("/api/activity", headers={"X-API-Key": "secret-key"}).status_code == 200
    assert dashboard.get("/api/activity", headers={"Authorization": "Bearer secret-key"}).status_code == 200
    response = migrate(dashboard)
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"] and "SameSite=strict" in response.headers["set-cookie"]
    assert dashboard.get("/api/activity").status_code == 200
    assert dashboard.post("/api/session/logout", headers={"Origin": "http://evil.example"}).status_code == 403
    assert dashboard.post("/api/session/logout", headers=ORIGIN).status_code == 200
    assert dashboard.get("/api/activity").status_code == 401


def test_existing_browser_approves_new_browser_once_and_sessions_survive_reopen(dashboard):
    assert migrate(dashboard).status_code == 200
    other = TestClient(app, base_url=BASE)
    request = other.post("/api/session/request", headers=ORIGIN)
    code = request.json()["code"]
    assert other.post("/api/session/claim", headers=ORIGIN).json()["status"] == "pending"
    assert dashboard.get("/api/session/pending").json()["requests"][0]["code"] == code
    assert dashboard.post("/api/session/approve", headers=ORIGIN, json={"code": code}).status_code == 200
    assert dashboard.post("/api/session/approve", headers=ORIGIN, json={"code": code}).status_code == 404
    assert other.post("/api/session/claim", headers=ORIGIN).json()["status"] == "authenticated"
    assert other.post("/api/session/claim", headers=ORIGIN).json()["status"] == "pending"
    reopened = TestClient(app, base_url=BASE, cookies=other.cookies)
    assert reopened.get("/api/activity").status_code == 200
    assert dashboard.post("/api/session/revoke-others", headers=ORIGIN).status_code == 200
    assert reopened.get("/api/activity").status_code == 401


def test_cli_approval_rate_limit_and_expiration(dashboard, monkeypatch):
    now = [1000000.0]
    monkeypatch.setattr(store.time, "time", lambda: now[0])
    requester = TestClient(app, base_url=BASE)
    code = requester.post("/api/session/request", headers=ORIGIN).json()["code"]
    assert store.approve(code)
    assert requester.post("/api/session/claim", headers=ORIGIN).json()["status"] == "authenticated"
    now[0] += store.IDLE_SECONDS + 1
    assert requester.get("/api/activity").status_code == 401
    now[0] += 1
    for _ in range(5):
        assert requester.post("/api/session/request", headers=ORIGIN).status_code == 200
    assert requester.post("/api/session/request", headers=ORIGIN).status_code == 429
    now[0] += store.PAIR_SECONDS + 1
    assert store.pending_pairings() == []


def test_http_origin_and_cross_site_migration_rejected(dashboard):
    assert dashboard.post("/api/session/request", headers={"Origin": "http://evil.example"}).status_code == 403
    assert dashboard.post("/api/session/migrate", headers={**ORIGIN, "Sec-Fetch-Site": "cross-site", "X-API-Key": "secret-key"}).status_code == 403
    assert dashboard.post("/api/session/migrate", headers=ORIGIN).status_code == 401


def test_https_cookie_and_absolute_expiry(dashboard, monkeypatch):
    now = [2000000.0]
    monkeypatch.setattr(store.time, "time", lambda: now[0])
    secure = TestClient(app, base_url="https://127.0.0.1:8000")
    response = secure.post("/api/session/migrate", headers={"Origin": "https://127.0.0.1:8000", "X-API-Key": "secret-key"})
    assert "Secure" in response.headers["set-cookie"]
    for _ in range(4):
        now[0] += store.IDLE_SECONDS - 1
        assert secure.get("/api/activity").status_code == 200
    now[0] += store.IDLE_SECONDS - 1
    assert secure.get("/api/activity").status_code == 401


def test_local_cli_approves_pending_request(dashboard, monkeypatch, capsys):
    code = dashboard.post("/api/session/request", headers=ORIGIN).json()["code"]
    monkeypatch.setattr("sys.argv", ["dashboard_sessions", "list"])
    store.main()
    assert code in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["dashboard_sessions", "approve", code])
    store.main()
    assert dashboard.post("/api/session/claim", headers=ORIGIN).json()["status"] == "authenticated"
