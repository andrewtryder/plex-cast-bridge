"""Backup export and restore safety."""

from __future__ import annotations

import io
import json
import zipfile

from fastapi.testclient import TestClient

from plex_cast_bridge.main import app


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_backup_and_restore_preview_rejects_traversal():
    with TestClient(app) as client:
        r = client.get("/api/backup")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/zip")

        evil = _zip_bytes(
            {
                "../etc/passwd": b"nope",
                "backup_meta.json": b'{"schema_version":1,"includes_secrets":false}',
            }
        )
        res = client.post(
            "/api/backup/restore/preview",
            files={"file": ("evil.zip", evil, "application/zip")},
        )
        assert res.status_code == 400


def test_restore_requires_confirm(tmp_path, monkeypatch):
    from plex_cast_bridge import catalog_store
    import plex_cast_bridge.routes as routes

    monkeypatch.setattr(catalog_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(routes, "DATA_DIR", tmp_path)
    payload = _zip_bytes(
        {
            "backup_meta.json": json.dumps(
                {"schema_version": 1, "includes_secrets": False}
            ).encode(),
            "app_settings.json": b'{"schema_version":1,"default_cast_volume":0.2,"max_cast_volume":0.5}\n',
        }
    )
    with TestClient(app) as client:
        denied = client.post(
            "/api/backup/restore",
            files={"file": ("b.zip", payload, "application/zip")},
            data={},
        )
        assert denied.status_code == 400
        ok = client.post(
            "/api/backup/restore",
            files={"file": ("b.zip", payload, "application/zip")},
            data={"confirm": "true"},
        )
        assert ok.status_code == 200
        assert "app_settings.json" in ok.json()["restored"]
        assert (tmp_path / "app_settings.json").exists()
