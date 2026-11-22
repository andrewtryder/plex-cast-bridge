"""Synthetic async characterization for application lifespan (no real network)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from plex_cast_bridge import main


@pytest.fixture
def discovery_off(monkeypatch):
    monkeypatch.setenv("PCB_DISABLE_BACKGROUND_DISCOVERY", "1")
    monkeypatch.setenv("PCB_PLEX_STARTUP_ATTEMPTS", "1")
    monkeypatch.setenv("ENABLE_PUBSUB_LISTENER", "false")


def test_lifespan_starts_and_stops_with_discovery_disabled(discovery_off, monkeypatch):
    plex = MagicMock()

    def init_ok(*_args, **_kwargs):
        return plex

    monkeypatch.setattr("plex_cast_bridge.main.init_plex_service", init_ok)
    monkeypatch.setattr(main.CastManager, "discover", lambda self: [])
    monkeypatch.setattr(main, "_persist_discovered_speakers_to_catalog", lambda *_a: None)
    monkeypatch.setattr(main, "_load_disabled_speakers_from_catalog", lambda: set())
    from pathlib import Path

    monkeypatch.setattr(main.music_index, "database_path", lambda: Path("/nonexistent/index.sqlite3"))
    monkeypatch.setattr(main.music_index_scan, "recover_interrupted", lambda: None)
    monkeypatch.setattr(main.catalog_sync, "resume_pending_push", lambda: None)
    monkeypatch.setattr(main.catalog_sync, "stop_pending_push", lambda: None)
    monkeypatch.setattr(main.catalog_sync, "reconcile_cloud", lambda: {"state": "ok"})

    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200
    assert main.app.state.plex is plex


def test_lifespan_continues_when_plex_init_fails(discovery_off, monkeypatch):
    def init_fail(*_args, **_kwargs):
        raise RuntimeError("plex down")

    monkeypatch.setattr("plex_cast_bridge.main.init_plex_service", init_fail)
    monkeypatch.setattr(main.CastManager, "discover", lambda self: [])
    monkeypatch.setattr(main, "_persist_discovered_speakers_to_catalog", lambda *_a: None)
    monkeypatch.setattr(main, "_load_disabled_speakers_from_catalog", lambda: set())
    from pathlib import Path

    monkeypatch.setattr(main.music_index, "database_path", lambda: Path("/nonexistent/index.sqlite3"))
    monkeypatch.setattr(main.music_index_scan, "recover_interrupted", lambda: None)
    monkeypatch.setattr(main.catalog_sync, "resume_pending_push", lambda: None)
    monkeypatch.setattr(main.catalog_sync, "stop_pending_push", lambda: None)
    monkeypatch.setattr(main.catalog_sync, "reconcile_cloud", lambda: {"state": "ok"})

    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200
        assert main.app.state.plex is None
