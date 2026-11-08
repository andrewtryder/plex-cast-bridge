"""Unit tests for app_settings_store and /api/settings."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from plex_cast_bridge import app_settings_store as store
from plex_cast_bridge.main import app


@pytest.fixture()
def tmp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    return tmp_path


def test_normalize_clamps_and_orders(tmp_data_dir: Path):
    saved = store.save({"default_cast_volume": 1.5, "max_cast_volume": 0.5})
    assert saved["max_cast_volume"] == 0.5
    assert saved["default_cast_volume"] == 0.5
    assert (tmp_data_dir / "app_settings.json").exists()


def test_load_empty_when_missing(tmp_data_dir: Path):
    assert store.load() == {}
    pub = store.public_dict()
    assert pub["configured"] is False


def test_night_volume_optional(tmp_data_dir: Path):
    saved = store.save(
        {"default_cast_volume": 0.1, "max_cast_volume": 0.4, "night_cast_volume": 0.2}
    )
    assert saved["night_cast_volume"] == 0.2
    again = store.load()
    assert again["night_cast_volume"] == 0.2


def test_api_get_and_patch_settings(tmp_data_dir: Path):
    with TestClient(app) as client:
        r = client.get("/api/settings")
        assert r.status_code == 200
        body = r.json()
        assert "volumes" in body
        assert "library" in body
        assert body["volumes"]["default_cast_volume"] is not None

        r2 = client.patch(
            "/api/settings",
            json={"default_cast_volume": 0.25, "max_cast_volume": 0.7},
        )
        assert r2.status_code == 200
        assert r2.json()["volumes"]["default_cast_volume"] == 0.25
        assert r2.json()["volumes"]["max_cast_volume"] == 0.7
        assert (tmp_data_dir / "app_settings.json").exists()
        assert client.app.state.cast.default_volume == 0.25
        assert client.app.state.cast.max_volume == 0.7

        bad = client.patch(
            "/api/settings",
            json={"default_cast_volume": 0.9, "max_cast_volume": 0.2},
        )
        assert bad.status_code == 400
