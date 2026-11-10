"""Backup export and restore safety."""

from __future__ import annotations

import io
import json
from pathlib import Path
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


def test_proxmox_upgrade_script_syntax_and_rollback_verification(tmp_path):
    import subprocess
    import tarfile

    script = Path(__file__).resolve().parent.parent / "scripts" / "proxmox_upgrade.sh"
    assert script.exists()

    # 1. Syntax check
    res = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr

    # 2. Test verify-rollback action with valid backup
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    (backup_dir / "docker-compose.yml").write_text("services: {}\n")
    (backup_dir / ".env").write_text("KEY=val\n")
    (backup_dir / "previous_image.txt").write_text("\n")
    (backup_dir / "previous_image_digest.txt").write_text("\n")
    (backup_dir / "gcp-credentials.json").write_text("{}\n")

    tar_path = backup_dir / "data.tgz"
    with tarfile.open(tar_path, "w:gz") as tar:
        dummy = tmp_path / "dummy.txt"
        dummy.write_text("hello")
        tar.add(dummy, arcname="data/dummy.txt")

    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    res = subprocess.run(
        ["bash", str(script), "verify-rollback", str(backup_dir)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0, res.stderr
    assert "Rollback verification passed." in res.stdout

    # 3. Test failure when docker-compose.yml is missing
    (backup_dir / "docker-compose.yml").unlink()
    res = subprocess.run(
        ["bash", str(script), "verify-rollback", str(backup_dir)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode != 0
    assert "missing compose configuration" in res.stderr
