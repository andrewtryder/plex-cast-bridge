"""Extended synthetic lifespan integration (no network)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from plex_cast_bridge import main


@pytest.fixture
def discovery_off(monkeypatch):
    monkeypatch.setenv("PCB_DISABLE_BACKGROUND_DISCOVERY", "1")
    monkeypatch.setenv("PCB_PLEX_STARTUP_ATTEMPTS", "1")
    monkeypatch.setenv("ENABLE_PUBSUB_LISTENER", "false")


def _stubs(monkeypatch):
    monkeypatch.setattr("plex_cast_bridge.main.init_plex_service", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(main.CastManager, "discover", lambda self: [])
    monkeypatch.setattr(main, "_persist_discovered_speakers_to_catalog", lambda *_a: None)
    monkeypatch.setattr(main, "_load_disabled_speakers_from_catalog", lambda: set())
    monkeypatch.setattr(main.catalog_sync, "resume_pending_push", lambda: None)
    monkeypatch.setattr(main.catalog_sync, "stop_pending_push", lambda: None)
    monkeypatch.setattr(main.catalog_sync, "reconcile_cloud", lambda: {"state": "ok"})
    monkeypatch.setattr(main.music_index_scan, "cancel", lambda: None)


def test_interrupted_scan_recovery_when_database_present(discovery_off, monkeypatch, tmp_path):
    db = tmp_path / "music_index.sqlite3"
    db.write_bytes(b"x")
    called = {"n": 0}
    monkeypatch.setattr(main.music_index, "database_path", lambda: db)
    monkeypatch.setattr(main.music_index_scan, "recover_interrupted", lambda: called.__setitem__("n", 1))
    _stubs(monkeypatch)
    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200
    assert called["n"] == 1


def test_repeated_startup_shutdown_cycles(discovery_off, monkeypatch):
    monkeypatch.setattr(main.music_index, "database_path", lambda: Path("/nonexistent/index.sqlite3"))
    monkeypatch.setattr(main.music_index_scan, "recover_interrupted", lambda: None)
    _stubs(monkeypatch)
    for _ in range(3):
        with TestClient(main.app) as client:
            assert client.get("/health").status_code == 200


def test_health_available_immediately_after_ready(discovery_off, monkeypatch):
    monkeypatch.setattr(main.music_index, "database_path", lambda: Path("/nonexistent/index.sqlite3"))
    monkeypatch.setattr(main.music_index_scan, "recover_interrupted", lambda: None)
    _stubs(monkeypatch)
    with TestClient(main.app) as client:
        for _ in range(5):
            assert client.get("/health").status_code == 200
