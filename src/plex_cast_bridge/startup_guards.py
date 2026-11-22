"""Startup safety checks extracted from application lifespan (read-only)."""
from __future__ import annotations

import os
from pathlib import Path

_EXISTING_DATA_MARKERS = (
    "catalog.json",
    "plex_connection.json",
    "music_index.sqlite3",
    "app_settings.json",
    "google_home_sync.json",
    "google_home_setup.json",
)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes"}


def ensure_persistent_data_mount(data_dir: Path) -> None:
    if (
        data_dir == Path("/data")
        and (Path("/.dockerenv").exists() or os.getenv("DOCKER_CONTAINER") == "1")
        and not os.path.ismount(data_dir)
        and not _truthy(os.environ.get("PCB_ALLOW_EPHEMERAL_DATA"))
    ):
        raise RuntimeError(
            "CRITICAL: /data is not a mounted persistent volume. "
            "Plex Cast Bridge requires a persistent mount for /data to prevent data loss across updates. "
            "Mount a host directory or named volume (e.g. -v /path/to/data:/data), "
            "or set PCB_ALLOW_EPHEMERAL_DATA=1 if stateless execution is intended."
        )


def has_existing_configuration(data_dir: Path) -> bool:
    return any((data_dir / name).exists() for name in _EXISTING_DATA_MARKERS)


def require_existing_configuration(data_dir: Path) -> None:
    require_existing = _truthy(os.environ.get("PCB_REQUIRE_EXISTING_DATA")) or _truthy(os.environ.get("PCB_UPGRADE"))
    if require_existing and not has_existing_configuration(data_dir):
        raise RuntimeError(
            f"CRITICAL: PCB_REQUIRE_EXISTING_DATA is set, but {data_dir} does not contain existing "
            "Plex Cast Bridge configuration (no catalog.json, plex_connection.json, music_index.sqlite3, "
            "app_settings.json, or google_home_sync.json found). "
            "Refusing to start to prevent data loss from an incorrect or empty mount path."
        )


def ensure_pubsub_credentials(enable_pubsub_listener: bool, credentials_path: str | None) -> None:
    if not enable_pubsub_listener:
        return
    if not credentials_path:
        return
    path = Path(credentials_path)
    if not path.exists() or not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(
            f"CRITICAL: ENABLE_PUBSUB_LISTENER=true is set, but GOOGLE_APPLICATION_CREDENTIALS ({credentials_path}) "
            "points to an unmounted placeholder (/dev/null), missing file, or empty key. "
            "Provide a valid Google Cloud service account key via PCB_GCP_CREDENTIALS to enable Google Home voice control."
        )
