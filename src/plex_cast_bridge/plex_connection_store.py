"""Server-side Plex connection credential storage under DATA_DIR."""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Any

from . import catalog_store

log = logging.getLogger(__name__)

_LOCK = threading.RLock()

CONNECTION_FILENAME = "plex_connection.json"
PRIVATE_KEY_FILENAME = "plex_device_private.pem"
PUBLIC_KEY_FILENAME = "plex_device_public.pem"

PRODUCT_NAME = "Plex Cast Bridge"


def connection_dir() -> Path:
    data_dir = catalog_store.DATA_DIR
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def connection_path() -> Path:
    return connection_dir() / CONNECTION_FILENAME


def private_key_path() -> Path:
    return connection_dir() / PRIVATE_KEY_FILENAME


def public_key_path() -> Path:
    return connection_dir() / PUBLIC_KEY_FILENAME


def keypair_paths() -> tuple[Path, Path]:
    return private_key_path(), public_key_path()


def _chmod_secret(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        log.debug("Could not chmod %s: %s", path, exc)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)
    _chmod_secret(path)


def load_connection() -> dict[str, Any] | None:
    path = connection_path()
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        return data
    except Exception as exc:
        log.warning("Could not read Plex connection store %s: %s", path, exc)
        return None


def save_connection(data: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        existing = load_connection() or {}
        merged = {**existing, **data}
        if not merged.get("client_identifier"):
            merged["client_identifier"] = str(uuid.uuid4())
        _atomic_write_json(connection_path(), merged)
        return merged


def ensure_client_identifier() -> str:
    data = load_connection()
    if data and data.get("client_identifier"):
        return str(data["client_identifier"])
    cid = str(uuid.uuid4())
    save_connection({"client_identifier": cid})
    return cid


def clear_connection() -> None:
    with _LOCK:
        for path in (connection_path(), private_key_path(), public_key_path()):
            try:
                if path.exists():
                    path.unlink()
            except OSError as exc:
                log.warning("Could not remove %s: %s", path, exc)


def public_status(data: dict[str, Any] | None = None, *, plex_ready: bool = False) -> dict[str, Any]:
    """Safe status payload for the dashboard (no tokens/keys)."""
    data = data if data is not None else load_connection()
    if not data:
        return {
            "linked": False,
            "mode": None,
            "plex_ready": plex_ready,
            "username": None,
            "friendly_name": None,
            "server_name": None,
            "music_section": None,
        }
    mode = data.get("mode")
    linked = bool(
        (mode == "jwt" and data.get("jwt_token") and data.get("server_url") and data.get("music_section"))
        or (mode == "env" and data.get("server_url") and data.get("music_section"))
        or plex_ready
    )
    return {
        "linked": linked and plex_ready,
        "mode": mode,
        "plex_ready": plex_ready,
        "username": data.get("username"),
        "friendly_name": data.get("friendly_name"),
        "server_name": data.get("server_name"),
        "music_section": data.get("music_section"),
        "updated_at": data.get("updated_at"),
    }
