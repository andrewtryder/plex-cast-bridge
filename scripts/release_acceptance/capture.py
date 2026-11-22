import json
import os
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._facade import package
from .constants import PROTECTED_ENV_KEYS
from .local_state import (
    _backup_capture,
    _credentials_capture,
    _google_home_sync_capture,
    _plex_connection_capture,
    _sqlite_integrity,
)
from .security import _fingerprint_bytes, _key, _private_dir, _protected_fingerprints, _read_env


def capture(args: Namespace) -> dict[str, Any]:
    ra = package()
    state_dir = _private_dir(args.state_dir)
    key = _key(state_dir)
    env_values = _read_env(args.env_file)
    env_values.update({k: v for k, v in os.environ.items() if k in PROTECTED_ENV_KEYS})
    creds_path = Path(args.credentials_file)
    data_path = Path(args.data_dir)
    sync_state = _google_home_sync_capture(data_path / "google_home_sync.json", key)
    plex_connection = _plex_connection_capture(data_path / "plex_connection.json", key)
    compose_fingerprint = None
    if args.compose_file.is_file():
        compose_fingerprint = _fingerprint_bytes(key, args.compose_file.read_bytes())
    api = ra._api_capture(args.api_url, args.api_key, args.timeout)
    docker = ra._container_capture(args.container, key)
    catalog = ra._catalog_snapshot_capture(args.container)
    if catalog.get("available"):
        api["speakers"] = catalog["speakers"]
        api["speaker_identity"] = catalog["speakers"]
    mounts = (docker or {}).get("mounts", [])
    credentials = _credentials_capture(creds_path, key)
    index_state = _sqlite_integrity(data_path / "music_index.sqlite3")
    api["saved_mix_ids"] = index_state.get("mix_ids") if index_state.get("present") else []
    report = {
        "schema_version": 1,
        "captured_at": datetime.now(UTC).isoformat(),
        "local": {
            "docker": docker,
            "catalog_snapshot": catalog,
            "configuration_drift": ra._configuration_drift_capture(args.container),
            "protected_config_fingerprints": _protected_fingerprints(key, env_values),
            "compose_config_fingerprint": compose_fingerprint,
            "google_home_sync_state": sync_state,
            "plex_connection_state": plex_connection,
            "installation_files": {
                ".env_present": bool(args.env_file and args.env_file.is_file()),
                "subscriber_credentials": credentials,
                "data_dir_present": data_path.is_dir(),
                "data_mount_configured": any(
                    m.get("destination") == "/data" and m.get("source_present")
                    for m in mounts
                ),
                "subscriber_credentials_mount_configured": any(
                    m.get("destination") == "/app/gcp-credentials.json" and m.get("source_present")
                    for m in mounts
                ),
            },
            "music_index_sqlite": index_state,
            "backup": _backup_capture(args.backup_dir, require_env=bool(args.env_file and args.env_file.is_file()),
                                      require_credentials=creds_path.is_file()),
        },
        "cloud_run": ra._cloud_capture(args.project, args.cloud_run_service, args.region, key) if args.project else {"available": False, "status": "NOT_APPLICABLE" if not sync_state.get("present") else "NOT_TESTED"},
        "api": api,
    }
    report_path = state_dir / f"baseline-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(report_path, 0o600)
    return {"report_path": str(report_path), "report": report}


def capture_cloud(args: Namespace) -> dict[str, Any]:
    ra = package()
    state_dir = _private_dir(args.state_dir)
    key = _key(state_dir)
    cloud = ra._cloud_capture(args.project, args.cloud_run_service, args.region, key)
    report = {"schema_version": 1, "captured_at": datetime.now(UTC).isoformat(), "cloud_run": cloud}
    report_path = state_dir / f"cloud-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(report_path, 0o600)
    return {"report_path": str(report_path), "report": report}
