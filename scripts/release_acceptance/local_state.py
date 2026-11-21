import json
import sqlite3
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

from .paths import SCRIPTS_DIR
from .security import _fingerprint
from ._facade import package

def _sqlite_integrity(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"present": False, "integrity": "NOT_TESTED"}
    # Immutable opens do not create journal/shm files. An extant WAL cannot be
    # read safely this way, so report the check as untested instead of ignoring it.
    if path.with_name(path.name + "-wal").exists() or path.with_name(path.name + "-journal").exists():
        return {"present": True, "integrity": "NOT_TESTED", "reason": "active journal or WAL"}
    try:
        before = path.stat()
        db = sqlite3.connect(path.resolve().as_uri() + "?immutable=1", uri=True, timeout=5)
        try:
            check = db.execute("PRAGMA integrity_check").fetchone()[0]
            counts = {}
            for table in ("tracks", "artists", "albums", "mixes"):
                try:
                    counts[table] = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except sqlite3.Error:
                    counts[table] = None
            try:
                mix_ids = sorted((row[0] for row in db.execute("SELECT id FROM mixes")), key=str)
            except sqlite3.Error:
                mix_ids = None
            after = path.stat()
            if ((before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
                    or path.with_name(path.name + "-wal").exists()
                    or path.with_name(path.name + "-journal").exists()):
                return {"present": True, "integrity": "NOT_TESTED", "reason": "database changed during inspection"}
            return {"present": True, "integrity": check, "counts": counts, "mix_ids": mix_ids}
        finally:
            db.close()
    except (sqlite3.Error, OSError) as exc:
        return {"present": True, "integrity": "ERROR", "error_type": type(exc).__name__}


def _google_home_sync_capture(path: Path, key: bytes) -> dict[str, Any]:
    if not path.is_file():
        return {"present": False}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"present": True, "valid_json": False}
    if not isinstance(state, dict):
        return {"present": True, "valid_json": False}
    return {
        "present": True,
        "valid_json": True,
        "linked": bool(state.get("cloud_gateway_url")),
        "configured": any(state.get(name) for name in ("cloud_gateway_url", "catalog_write_key", "version", "last_cloud_meta")),
        "status": state.get("last_push_status"),
        # Mutable version/status/timestamps are useful evidence but excluded from
        # identity comparison. URL and key remain HMAC-only.
        "version": state.get("version"),
        "protected_config_fingerprints": {
            name: _fingerprint(key, str(state[name]))
            for name in ("cloud_gateway_url", "catalog_write_key") if state.get(name)
        },
        "agent_user_id": state.get("agent_user_id"),
    }


def _plex_connection_capture(path: Path, key: bytes) -> dict[str, Any]:
    if not path.is_file():
        return {"present": False}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"present": True, "valid_json": False}
    if not isinstance(state, dict):
        return {"present": True, "valid_json": False}
    return {
        "present": True,
        "valid_json": True,
        "mode": state.get("mode"),
        "jwt_present": bool(state.get("jwt_token")),
        "music_section": state.get("music_section"),
        "client_identifier_fingerprint": _fingerprint(key, str(state["client_identifier"])) if state.get("client_identifier") else None,
        "server_identity_fingerprint": _fingerprint(
            key, "\n".join(str(state.get(name) or "") for name in ("server_url", "server_name"))
        ) if state.get("server_url") or state.get("server_name") else None,
        "private_key_present": path.with_name("plex_device_private.pem").is_file(),
        "public_key_present": path.with_name("plex_device_public.pem").is_file(),
    }


def _credentials_capture(path: Path, key: bytes) -> dict[str, Any]:
    if not path.is_file():
        return {"present": False}
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"present": True, "valid_json": False}
    if not isinstance(values, dict):
        return {"present": True, "valid_json": False}
    return {
        "present": True,
        "valid_json": True,
        "valid_service_account": (
            values.get("type") == "service_account"
            and bool(values.get("client_email"))
            and bool(values.get("private_key"))
        ),
        "project_id": values.get("project_id"),
        "client_email": values.get("client_email"),
        "private_key_id": values.get("private_key_id"),
        "private_key_fingerprint": _fingerprint(key, str(values.get("private_key", ""))) if values.get("private_key") else None,
    }


def _backup_capture(path: Path | None, *, require_env: bool = False, require_credentials: bool = False) -> dict[str, Any]:
    if not path:
        return {"configured": False, "available": False}
    if not path.exists():
        return {"configured": True, "available": False}
    dirs = [p for p in path.iterdir() if p.is_dir()] if path.is_dir() else [path.parent]
    dirs = [p for p in dirs if (p / "previous_image_digest.txt").exists() or (p / "docker-compose.yml").exists()]
    if not dirs:
        files = [p for p in path.iterdir() if p.is_file()] if path.is_dir() else [path]
        return {"configured": True, "available": bool(files), "file_count": len(files), "rollback_ready": False}
    latest = max(dirs, key=lambda p: p.stat().st_mtime_ns)
    expected = ("data.tgz", "data.tgz.sha256", ".env", "docker-compose.yml", "gcp-credentials.json",
                "previous_image.txt", "previous_image_digest.txt")
    present = {name: (latest / name).is_file() for name in expected}
    previous_image = ""
    for name in ("previous_image_digest.txt", "previous_image.txt"):
        try:
            previous_image = (latest / name).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if previous_image:
            break
    try:
        image_cached = bool(previous_image) and package().subprocess.run(
            ["docker", "inspect", previous_image], capture_output=True, check=False, timeout=20
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        image_cached = False
    archive_valid = False
    archive_contains_data = False
    if present["data.tgz"]:
        try:
            archive_result = package().subprocess.run(
                ["tar", "-tzf", str(latest / "data.tgz")],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            archive_valid = archive_result.returncode == 0
            archive_contains_data = archive_valid and any(
                PurePosixPath(member).parts[:1] == ("data",)
                for member in archive_result.stdout.splitlines()
            )
        except (OSError, subprocess.TimeoutExpired):
            archive_valid = False
    restore_verified = False
    if archive_valid and archive_contains_data and present["data.tgz.sha256"]:
        try:
            restore_verified = package().subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "upgrade_backup.py"), "verify", str(latest / "data.tgz")],
                capture_output=True, check=False, timeout=120,
            ).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            restore_verified = False
    required_names = ("data.tgz", "data.tgz.sha256", "docker-compose.yml")
    if require_env:
        required_names += (".env",)
    if require_credentials:
        required_names += ("gcp-credentials.json",)
    required_nonempty = {
        name: bool(present.get(name) and (latest / name).stat().st_size > 0)
        for name in required_names
    }
    required_present = all(present[name] for name in required_names)
    return {
        "configured": True,
        "available": True,
        "backup_directory": str(latest),
        "files": present,
        "required_nonempty_files": required_nonempty,
        "data_archive_valid": archive_valid,
        "data_archive_contains_data": archive_contains_data,
        "restore_verified": restore_verified,
        "previous_image_cached": image_cached,
        "rollback_ready": (
            required_present and all(required_nonempty.values())
            and archive_valid and archive_contains_data and restore_verified and image_cached
        ),
    }
