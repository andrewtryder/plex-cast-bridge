"""Synthetic async characterization for application lifespan (no real network)."""
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


def _lifespan_stubs(monkeypatch, *, plex_init, pubsub_factory=None):
    monkeypatch.setattr("plex_cast_bridge.main.init_plex_service", plex_init)
    monkeypatch.setattr(main.CastManager, "discover", lambda self: [])
    monkeypatch.setattr(main, "_persist_discovered_speakers_to_catalog", lambda *_a: None)
    monkeypatch.setattr(main, "_load_disabled_speakers_from_catalog", lambda: set())
    monkeypatch.setattr(main.music_index, "database_path", lambda: Path("/nonexistent/index.sqlite3"))
    monkeypatch.setattr(main.music_index_scan, "recover_interrupted", lambda: None)
    monkeypatch.setattr(main.music_index_scan, "cancel", lambda: None)
    monkeypatch.setattr(main.catalog_sync, "resume_pending_push", lambda: None)
    monkeypatch.setattr(main.catalog_sync, "stop_pending_push", lambda: None)
    monkeypatch.setattr(main.catalog_sync, "reconcile_cloud", lambda: {"state": "ok"})
    if pubsub_factory is not None:
        monkeypatch.setattr(main, "PubSubWorker", pubsub_factory)


def test_lifespan_starts_and_stops_with_discovery_disabled(discovery_off, monkeypatch):
    plex = MagicMock()
    _lifespan_stubs(monkeypatch, plex_init=lambda *_a, **_k: plex)
    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200
    assert main.app.state.plex is plex


def test_lifespan_continues_when_plex_init_fails(discovery_off, monkeypatch):
    _lifespan_stubs(monkeypatch, plex_init=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("plex down")))
    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200
        assert main.app.state.plex is None


def test_lifespan_pubsub_worker_failure_cleans_background_tasks(discovery_off, monkeypatch):
    """Regression: reconcile/index tasks must not outlive failed Pub/Sub init."""
    from plex_cast_bridge import startup_lifecycle

    class FailWorker:
        def __init__(self, *_args, **_kwargs):
            raise ValueError("missing subscription")

    aborted: list[tuple] = []
    real_abort = startup_lifecycle.abort_startup_after_background_tasks

    async def spy_abort(tasks, **kwargs):
        aborted.append((tasks, kwargs))
        await real_abort(tasks, **kwargs)

    _lifespan_stubs(monkeypatch, plex_init=lambda *_a, **_k: MagicMock(), pubsub_factory=FailWorker)
    monkeypatch.setattr(main, "abort_startup_after_background_tasks", spy_abort)
    monkeypatch.setenv("ENABLE_PUBSUB_LISTENER", "true")
    monkeypatch.setenv("GCP_PROJECT_ID", "proj")
    monkeypatch.setenv("GCP_PUBSUB_SUBSCRIPTION_ID", "sub")
    with pytest.raises(ValueError, match="missing subscription"):
        with TestClient(main.app):
            pass
    assert len(aborted) == 1
    tasks, kwargs = aborted[0]
    assert tasks.music_index is not None
    assert tasks.google_home_reconcile is not None
    assert kwargs.get("close_cast") is not None


def test_lifespan_pubsub_enabled_synthetic_worker(discovery_off, monkeypatch):
    closed = {"n": 0}

    class FakeWorker:
        def __init__(self, settings, loop, dispatch):
            self.dispatch = dispatch

        def close(self):
            closed["n"] += 1

    _lifespan_stubs(monkeypatch, plex_init=lambda *_a, **_k: MagicMock(), pubsub_factory=FakeWorker)
    original_settings = main.Settings

    def settings_with_pubsub(*_a, **_k):
        s = original_settings(*_a, **_k)
        s.enable_pubsub_listener = True
        s.gcp_project_id = "test-proj"
        s.gcp_pubsub_subscription_id = "sub"
        return s

    monkeypatch.setattr(main, "Settings", settings_with_pubsub)
    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200
        assert isinstance(main.app.state.worker, FakeWorker)
    assert closed["n"] == 1


def test_initial_discovery_failure_still_ready(discovery_off, monkeypatch):
    monkeypatch.delenv("PCB_DISABLE_BACKGROUND_DISCOVERY", raising=False)
    monkeypatch.setenv("PCB_DISABLE_BACKGROUND_DISCOVERY", "0")
    monkeypatch.setenv("SPEAKER_DISCOVERY_INTERVAL_SEC", "0")

    def boom_discover(self):
        raise OSError("mdns down")

    _lifespan_stubs(monkeypatch, plex_init=lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(main.CastManager, "discover", boom_discover)
    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200
