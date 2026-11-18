#!/usr/bin/env python3
"""Read-only release baseline and smoke checks for an existing installation.

Run on the Docker host (or inside the LXC) so the default API URL is local.
Reports and the HMAC key are kept outside the repository with private modes.
No playback or provisioning command is issued by this tool.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_STATE_DIR = Path.home() / ".local/share/plex-cast-bridge/release-acceptance"
PROTECTED_ENV_KEYS = (
    "OAUTH_SIGNING_SECRET",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "ADMIN_PASSPHRASE",
    "WEBHOOK_API_KEY",
    "CATALOG_WRITE_KEY",
    "CATALOG_BUCKET",
    "GCP_PROJECT_ID",
    "GCP_CATALOG_BUCKET",
    "GCP_PUBSUB_SUBSCRIPTION_ID",
    "GCP_PUBSUB_TOPIC_ID",
    "PLEX_URL",
    "PLEX_TOKEN",
    "PLEX_MUSIC_SECTION",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "CLOUD_GATEWAY_URL",
    "CLOUD_RUN_URL",
    "FULFILLMENT_URL",
    "GOOGLE_HOME_FULFILLMENT_URL",
    "AGENT_USER_ID",
    "GOOGLE_HOME_AGENT_USER_ID",
    "DEFAULT_SPEAKER_NAME",
    "DEFAULT_SPEAKER_HOST",
    "DEFAULT_CAST_VOLUME",
    "MAX_CAST_VOLUME",
    "NIGHT_CAST_VOLUME",
    "ENABLE_PUBSUB_LISTENER",
    "USE_CLOUD_CATALOG",
    "MOCK_PUBSUB",
    "TOKEN_EXPIRE_DAYS",
    "AUTH_CODE_EXPIRE_MINUTES",
    "ENVIRONMENT",
)
API_PATHS = (
    "/health",
    "/api/diagnostics",
    "/api/status",
    "/api/settings",
)


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _key(state_dir: Path) -> bytes:
    path = state_dir / "fingerprint.key"
    if path.exists():
        os.chmod(path, 0o600)
        value = path.read_bytes()
        if len(value) < 32:
            raise RuntimeError(f"Fingerprint key is invalid: {path}")
        return value
    value = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(value)
    return value


def _fingerprint(key: bytes, value: str) -> str:
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def _fingerprint_bytes(key: bytes, value: bytes) -> str:
    return hmac.new(key, value, hashlib.sha256).hexdigest()


def _read_env(path: Path | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path or not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip().strip("\"'")
    return values


def _protected_fingerprints(key: bytes, values: dict[str, str]) -> dict[str, str]:
    result = {}
    for name in PROTECTED_ENV_KEYS:
        value = values.get(name, "")
        if value:
            result[name] = _fingerprint(key, value)
    return result


def _run_json(argv: list[str], timeout: int = 25) -> Any | None:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        if proc.returncode != 0:
            return None
        value = json.loads(proc.stdout)
        return value if isinstance(value, (dict, list)) else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def _get_json(url: str, token: str | None, timeout: int) -> tuple[int | None, int, Any, str | None]:
    headers = {"Accept": "application/json"}
    if token:
        headers["X-API-Key"] = token
    started = time.monotonic()
    try:
        with urlopen(Request(url, headers=headers), timeout=timeout) as response:
            raw = response.read()
            status = response.status
        elapsed = round((time.monotonic() - started) * 1000)
        try:
            body = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = None
        return status, elapsed, body, None
    except HTTPError as exc:
        return exc.code, round((time.monotonic() - started) * 1000), None, "http_error"
    except (URLError, TimeoutError, OSError):
        return None, round((time.monotonic() - started) * 1000), None, "unreachable"


def _api_capture(base_url: str, token: str | None, timeout: int) -> dict[str, Any]:
    results: dict[str, Any] = {}
    bodies: dict[str, Any] = {}
    for path in API_PATHS:
        status, elapsed, body, error = _get_json(base_url.rstrip("/") + path, token, timeout)
        results[path] = {"http_status": status, "elapsed_ms": elapsed, "error": error}
        if body is not None:
            bodies[path] = body

    diag = bodies.get("/api/diagnostics", {})
    if isinstance(diag, dict):
        # Keep diagnostic outcomes only; error text can contain deployment details.
        checks = diag.get("checks", [])
        if isinstance(checks, list):
            results["/api/diagnostics"]["checks"] = [
                {"id": item.get("id"), "status": item.get("status")}
                for item in checks if isinstance(item, dict)
            ]

    settings_body = bodies.get("/api/settings", {})
    if isinstance(settings_body, dict):
        library = settings_body.get("library") if isinstance(settings_body.get("library"), dict) else {}
        volumes = settings_body.get("volumes") if isinstance(settings_body.get("volumes"), dict) else {}
        results["plex"] = {k: library.get(k) for k in ("linked", "section", "tracks", "artists", "albums") if k in library}
        results["speaker_config"] = {k: volumes.get(k) for k in (
            "default_cast_volume", "max_cast_volume", "night_cast_volume",
        ) if k in volumes}

    # Playback is mutable; retain only current state/count metadata, no media names.
    status_body = bodies.get("/api/status", {})
    if isinstance(status_body, dict):
        results["playback"] = {
            str(name): {
                k: value.get(k) for k in ("state", "volume", "max_volume", "queue_length", "repeat")
                if k in value
            } | {"current_track_present": bool(value.get("current_track"))}
            for name, value in status_body.items() if isinstance(value, dict)
        }
    return results


def _catalog_snapshot_capture(container: str) -> dict[str, Any]:
    source = Path(__file__).resolve().parent / "release_catalog_snapshot.py"
    try:
        proc = subprocess.run(
            ["docker", "exec", "-i", container, "python", "-c", "exec(compile(__import__('sys').stdin.read(), 'release_catalog_snapshot.py', 'exec'))"],
            input=source.read_text(encoding="utf-8"), capture_output=True, text=True, timeout=25, check=False,
        )
        value = json.loads(proc.stdout) if proc.returncode == 0 else None
        if not isinstance(value, dict) or value.get("available") is not True or not isinstance(value.get("speakers"), list):
            return {"available": False}
        return value
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {"available": False}


def _valid_drift(report: Any) -> bool:
    if not isinstance(report, dict) or not isinstance(report.get("files"), dict):
        return False
    files = report["files"]
    required = ("plex_connection.json", "app_settings.json", "catalog.json", "google_home_sync.json", "google_home_setup.json", "music_index.sqlite3")
    if any(name not in files or not isinstance(files[name], str) for name in required):
        return False
    if any("invalid" in files[name] or "unreadable" in files[name] for name in required):
        return False
    settings = report.get("settings")
    warnings = report.get("release_preflight_warnings")
    if not isinstance(settings, dict) or not isinstance(warnings, list):
        return False
    for name in ("cloud_gateway_url", "catalog_write_key"):
        entry = settings.get(name)
        if not isinstance(entry, dict) or entry.get("classification") not in {
            "ok", "informational", "release_preflight_warning"
        } or not isinstance(entry.get("conflict"), bool):
            return False
    return all(name in ("cloud_gateway_url", "catalog_write_key") for name in warnings) and set(warnings) == {
        name for name in ("cloud_gateway_url", "catalog_write_key")
        if settings[name]["classification"] == "release_preflight_warning"
    }


def _configuration_drift_capture(container: str) -> dict[str, Any]:
    """Run this checkout's diagnostic in the existing container, including old images."""
    source = Path(__file__).resolve().parents[1] / "src/plex_cast_bridge/config_drift.py"
    try:
        proc = subprocess.run(
            ["docker", "exec", "-i", container, "python", "-c",
             'import sys; __package__="plex_cast_bridge"; exec(compile(sys.stdin.read(), "config_drift.py", "exec"))'],
            input=source.read_text(encoding="utf-8"), capture_output=True, text=True, timeout=25, check=False,
        )
        if proc.returncode:
            return {"available": False}
        report = json.loads(proc.stdout)
        if not _valid_drift(report):
            return {"available": False}
        return {"available": True, "report": report}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {"available": False}


def _container_capture(container: str, key: bytes) -> dict[str, Any] | None:
    raw = _run_json(["docker", "inspect", container])
    if not raw:
        return None
    # docker inspect contains plaintext env values. Read them only in memory and
    # emit fingerprints, never the values.
    item = raw[0] if isinstance(raw, list) and raw else raw
    config = item.get("Config") or {}
    image_id = item.get("Image")
    inspected_image = _run_json(["docker", "image", "inspect", image_id]) if image_id else None
    image_info = inspected_image[0] if isinstance(inspected_image, list) and inspected_image else inspected_image or {}
    image_config = image_info.get("Config") or {}
    env_values = {}
    for pair in config.get("Env", []) or []:
        name, sep, value = pair.partition("=")
        if sep and name in PROTECTED_ENV_KEYS:
            env_values[name] = value
    labels = image_config.get("Labels") or config.get("Labels") or {}
    repo_tags = image_info.get("RepoTags") or []
    repo_digests = image_info.get("RepoDigests") or []
    commit_hint = None
    for tag in repo_tags:
        match = re.search(r"(?:sha-|pr\d+-)([0-9a-f]{7,40})$", tag)
        if match:
            commit_hint = match.group(1)
            break
    state = item.get("State") or {}
    mounts = item.get("Mounts") or []
    return {
        "image_id": image_id,
        "published_image_digest": next((d for d in repo_digests if "@sha256:" in d), None),
        "image_ref": config.get("Image"),
        "application_commit": labels.get("org.opencontainers.image.revision"),
        "application_commit_hint_from_tag": commit_hint,
        "repo_tags": repo_tags,
        "labels": {k: labels.get(k) for k in ("org.opencontainers.image.version", "org.opencontainers.image.created") if labels.get(k)},
        "running": state.get("Running"),
        "health": (state.get("Health") or {}).get("Status") if isinstance(state.get("Health"), dict) else None,
        "protected_config_fingerprints": _protected_fingerprints(key, env_values),
        "mounts": sorted(
            [{"destination": m.get("Destination"), "type": m.get("Type"),
              "source_present": bool(m.get("Source") and Path(m["Source"]).exists()),
              "source": m.get("Source")}
             for m in mounts], key=lambda m: str(m.get("destination"))
        ),
    }


def _cloud_capture(project: str, service: str, region: str, key: bytes) -> dict[str, Any]:
    raw = _run_json([
        "gcloud", "run", "services", "describe", service, "--project", project,
        "--region", region, "--format=json",
    ])
    if not raw:
        return {"available": False}
    spec = raw.get("spec", {}).get("template", {}).get("spec", {})
    status = raw.get("status", {})
    traffic = status.get("traffic") or []
    active = []
    for target in traffic:
        revision = target.get("revisionName")
        if not revision:
            continue
        rev = _run_json([
            "gcloud", "run", "revisions", "describe", revision, "--project", project,
            "--region", region, "--format=json",
        ]) or {}
        rev_spec = rev.get("spec") or {}
        rev_containers = rev_spec.get("containers") or []
        container = rev_containers[0] if rev_containers else {}
        active.append({
            "revision": revision,
            "percent": target.get("percent"),
            "image": container.get("image"),
            "service_account": rev_spec.get("serviceAccountName"),
        })
    # Fingerprint the deployment template's protected environment. Secret Manager
    # values are represented by their reference (secret + version), never fetched.
    template_containers = spec.get("containers") or []
    template_env = template_containers[0].get("env", []) if template_containers else []
    env = {}
    for pair in template_env:
        if pair.get("name") not in PROTECTED_ENV_KEYS:
            continue
        value = pair.get("value")
        source = pair.get("valueSource") or {}
        if value is not None:
            env[pair["name"]] = str(value)
        elif source:
            env[pair["name"]] = json.dumps(source, sort_keys=True)
    return {
        "available": True,
        "project_id": project,
        "service": service,
        "region": region,
        "latest_ready_revision": status.get("latestReadyRevisionName"),
        "latest_created_revision": status.get("latestCreatedRevisionName"),
        "image": next((r["image"] for r in active if r.get("revision") == status.get("latestReadyRevisionName")), None),
        "service_account": spec.get("serviceAccountName"),
        "url": status.get("url"),
        "traffic": active,
        "protected_config_fingerprints": _protected_fingerprints(key, env),
    }


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
        image_cached = bool(previous_image) and subprocess.run(
            ["docker", "inspect", previous_image], capture_output=True, check=False, timeout=20
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        image_cached = False
    archive_valid = False
    archive_contains_data = False
    if present["data.tgz"]:
        try:
            archive_result = subprocess.run(
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
            restore_verified = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parent / "upgrade_backup.py"), "verify", str(latest / "data.tgz")],
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


def capture(args: argparse.Namespace) -> dict[str, Any]:
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
    api = _api_capture(args.api_url, args.api_key, args.timeout)
    docker = _container_capture(args.container, key)
    catalog = _catalog_snapshot_capture(args.container)
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
            "configuration_drift": _configuration_drift_capture(args.container),
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
        "cloud_run": _cloud_capture(args.project, args.cloud_run_service, args.region, key) if args.project else {"available": False, "status": "NOT_APPLICABLE" if not sync_state.get("present") else "NOT_TESTED"},
        "api": api,
    }
    report_path = state_dir / f"baseline-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(report_path, 0o600)
    return {"report_path": str(report_path), "report": report}


def capture_cloud(args: argparse.Namespace) -> dict[str, Any]:
    state_dir = _private_dir(args.state_dir)
    key = _key(state_dir)
    cloud = _cloud_capture(args.project, args.cloud_run_service, args.region, key)
    report = {"schema_version": 1, "captured_at": datetime.now(UTC).isoformat(), "cloud_run": cloud}
    report_path = state_dir / f"cloud-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(report_path, 0o600)
    return {"report_path": str(report_path), "report": report}


LOCAL_IDENTITY_PATHS = (
    "local.protected_config_fingerprints",
    "local.compose_config_fingerprint",
    "local.google_home_sync_state.protected_config_fingerprints",
    "local.plex_connection_state",
    "local.docker.mounts",
    "local.installation_files",
    "local.music_index_sqlite.integrity",
    "local.backup.rollback_ready",
    "api.speaker_identity",
    "api.saved_mix_ids",
    "api.plex.linked",
    "api.plex.section",
    "api.speaker_config",
)
CLOUD_IDENTITY_PATHS = (
    "cloud_run.project_id",
    "cloud_run.service",
    "cloud_run.region",
    "cloud_run.url",
    "cloud_run.service_account",
    "cloud_run.protected_config_fingerprints",
)


def _at(obj: dict[str, Any], dotted: str) -> Any:
    value: Any = obj
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _speaker_identity(report: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Project stable fields from new or legacy speaker snapshots."""
    identity = _at(report, "api.speaker_identity")
    if isinstance(identity, list):
        return identity
    speakers = _at(report, "api.speakers")
    if not isinstance(speakers, list):
        return None
    fields = (
        "id", "google_home_device_id", "uuid", "name", "speaker_name", "room",
        "kind", "enabled", "google_home_visible", "is_group",
    )
    return [
        {key: speaker[key] for key in fields if key in speaker}
        for speaker in speakers if isinstance(speaker, dict)
    ]


def _missing_scope_evidence(report: dict[str, Any], scope: str) -> list[str]:
    missing: list[str] = []
    if scope == "local":
        if not isinstance(report.get("local"), dict) or not isinstance(report.get("api"), dict):
            return ["local/API baseline sections missing"]
        if _speaker_identity(report) is None:
            missing.append("speaker identity snapshot missing")
        for dotted in (
            "local.docker.image_id", "local.docker.running", "local.docker.mounts",
            "local.protected_config_fingerprints", "local.compose_config_fingerprint",
            "local.installation_files.data_dir_present", "api.plex.linked",
            "api.speaker_config", "api.saved_mix_ids",
        ):
            if _at(report, dotted) is None:
                missing.append(dotted)
        if _at(report, "local.installation_files.data_dir_present") is not True:
            missing.append("persistent data directory is absent")
        if _at(report, "local.backup.rollback_ready") is not True:
            missing.append("verified rollback backup unavailable")
        index = _at(report, "local.music_index_sqlite")
        if not isinstance(index, dict) or (index.get("present") and index.get("integrity") != "ok"):
            missing.append("SQLite integrity NOT TESTED or failed")
        sync = _at(report, "local.google_home_sync_state")
        if not isinstance(sync, dict):
            missing.append("Google Home configuration evidence missing")
        elif sync.get("present"):
            if sync.get("valid_json") is not True:
                missing.append("Google Home configuration is invalid")
            elif sync.get("linked"):
                fingerprints = sync.get("protected_config_fingerprints") or {}
                if any(name not in fingerprints for name in ("cloud_gateway_url", "catalog_write_key")):
                    missing.append("Google Home gateway configuration is incomplete")
        for endpoint in API_PATHS:
            result = report.get("api", {}).get(endpoint)
            if not isinstance(result, dict) or result.get("http_status") != 200:
                missing.append(f"GET {endpoint} did not return HTTP 200")
    elif scope == "cloud":
        cloud = report.get("cloud_run")
        if not isinstance(cloud, dict) or cloud.get("available") is not True:
            return ["Cloud Run metadata unavailable"]
        for dotted in (
            "cloud_run.project_id", "cloud_run.service", "cloud_run.region",
            "cloud_run.latest_ready_revision", "cloud_run.image", "cloud_run.service_account",
            "cloud_run.url", "cloud_run.protected_config_fingerprints",
        ):
            if _at(report, dotted) is None:
                missing.append(dotted)
        traffic = cloud.get("traffic") or []
        has_routed_traffic = any(
            isinstance(target, dict)
            and isinstance(target.get("percent"), (int, float))
            and not isinstance(target.get("percent"), bool)
            and target["percent"] > 0
            for target in traffic
        )
        if not has_routed_traffic:
            missing.append("Cloud Run has no verified traffic revision")
        cloud_fingerprints = cloud.get("protected_config_fingerprints") or {}
        for name in ("GCP_PROJECT_ID", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
                     "ADMIN_PASSPHRASE", "OAUTH_SIGNING_SECRET"):
            if name not in cloud_fingerprints:
                missing.append(f"Cloud Run {name} fingerprint is missing")
    return missing


def compare(before_path: Path, after_path: Path, scope: str = "all") -> tuple[bool, list[dict[str, Any]]]:
    before = json.loads(before_path.read_text(encoding="utf-8"))
    after = json.loads(after_path.read_text(encoding="utf-8"))
    differences = []
    if scope == "all":
        optional_cloud = _at(before, "cloud_run.status") == "NOT_APPLICABLE"
        scopes = ("local",) if optional_cloud else ("local", "cloud")
        if optional_cloud and _at(after, "cloud_run.status") != "NOT_APPLICABLE":
            differences.append({"scope": "cloud", "path": "cloud_run.status", "reason": "cloud configuration changed; review required"})
    else:
        scopes = (scope,)
    for check_scope in scopes:
        for missing in _missing_scope_evidence(before, check_scope):
            differences.append({"scope": check_scope, "path": "before", "reason": missing})
        for missing in _missing_scope_evidence(after, check_scope):
            differences.append({"scope": check_scope, "path": "after", "reason": missing})
    if "local" in scopes:
        drift_before = _at(before, "local.configuration_drift")
        drift_after = _at(after, "local.configuration_drift")
        for label, drift in (("before", drift_before), ("after", drift_after)):
            if not isinstance(drift, dict) or drift.get("available") is not True or not _valid_drift(drift.get("report")):
                differences.append({"scope": "local", "path": label, "reason": "configuration drift diagnostic NOT TESTED or invalid"})
        if (isinstance(drift_before, dict) and isinstance(drift_after, dict)
                and _valid_drift(drift_before.get("report")) and _valid_drift(drift_after.get("report"))):
            old, new = drift_before["report"], drift_after["report"]
            for name in ("cloud_gateway_url", "catalog_write_key"):
                if old["settings"][name]["classification"] != new["settings"][name]["classification"]:
                    differences.append({"scope": "local", "path": "configuration_drift." + name,
                                        "reason": "gateway drift classification changed; review required"})
            for name in old["files"].keys() | new["files"].keys():
                if old["files"].get(name) != new["files"].get(name):
                    differences.append({"scope": "local", "path": "configuration_drift.files." + name,
                                        "reason": "persisted file presence or validity changed"})
    selected_paths = []
    if "local" in scopes:
        selected_paths.extend(LOCAL_IDENTITY_PATHS)
    if "cloud" in scopes:
        selected_paths.extend(CLOUD_IDENTITY_PATHS)
    for path in selected_paths:
        if path == "api.speaker_identity":
            left, right = _speaker_identity(before), _speaker_identity(after)
        else:
            left, right = _at(before, path), _at(after, path)
        if left != right:
            differences.append({"scope": "identity", "path": path, "before": left, "after": right})
    return not differences, differences


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    cap = sub.add_parser("capture", help="Run GET-only API smoke checks and save a private baseline")
    cap.add_argument("--api-url", default="http://127.0.0.1:8000")
    cap.add_argument("--api-key", default=os.environ.get("API_KEY"), help="API key sent only as X-API-Key")
    cap.add_argument("--timeout", type=int, default=8)
    cap.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    cap.add_argument("--env-file", type=Path, default=Path("/opt/plex-cast-bridge/.env"))
    cap.add_argument("--credentials-file", type=Path, default=Path("/opt/plex-cast-bridge/gcp-credentials.json"))
    cap.add_argument("--compose-file", type=Path, default=Path("/opt/plex-cast-bridge/deploy/docker-compose.yml"))
    cap.add_argument("--container", default="plex-cast-bridge")
    cap.add_argument("--data-dir", type=Path, default=Path("/opt/plex-cast-bridge/data"))
    cap.add_argument("--backup-dir", type=Path, default=Path("/opt/plex-cast-bridge/backups"))
    cap.add_argument("--project", default="")
    cap.add_argument("--cloud-run-service", default="plex-google-home-fulfillment")
    cap.add_argument("--region", default="us-central1")
    cloud = sub.add_parser("capture-cloud", help="Capture Cloud Run metadata from a workstation with gcloud access")
    cloud.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    cloud.add_argument("--project", required=True)
    cloud.add_argument("--cloud-run-service", default="plex-google-home-fulfillment")
    cloud.add_argument("--region", default="us-central1")
    cmp = sub.add_parser("compare", help="Fail closed when protected identities differ")
    cmp.add_argument("before", type=Path)
    cmp.add_argument("after", type=Path)
    cmp.add_argument("--scope", choices=("all", "local", "cloud"), default="all",
                     help="all requires complete local and Cloud Run evidence")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "capture":
        result = capture(args)
        print(json.dumps({"report_path": result["report_path"], "summary": {
            "api_http_statuses": {
                path: value.get("http_status")
                for path, value in result["report"]["api"].items()
                if isinstance(value, dict) and "http_status" in value
            },
            "configuration_drift": {
                "available": result["report"]["local"]["configuration_drift"]["available"],
                "release_preflight_warnings": result["report"]["local"]["configuration_drift"].get("report", {}).get("release_preflight_warnings", []),
            },
            "speaker_count": len(result["report"]["api"].get("speakers", [])),
            "google_home_linked": result["report"]["api"].get("google_home", {}).get("cloud_linked"),
            "local_plex_linked": result["report"]["api"].get("google_home", {}).get("local_plex_linked"),
            "saved_mix_count": len(result["report"]["api"].get("saved_mix_ids", [])),
            "docker": {k: (result["report"]["local"]["docker"] or {}).get(k) for k in (
                "image_id", "published_image_digest", "application_commit",
                "application_commit_hint_from_tag", "running", "health",
            )},
            "cloud_run": {k: result["report"]["cloud_run"].get(k) for k in ("project_id", "latest_ready_revision", "image", "service_account")},
        }}, indent=2, sort_keys=True))
        return 0
    if args.command == "capture-cloud":
        result = capture_cloud(args)
        cloud_run = result["report"]["cloud_run"]
        print(json.dumps({"report_path": result["report_path"], "summary": {
            k: cloud_run.get(k) for k in (
                "available", "project_id", "service", "region", "latest_ready_revision",
                "latest_created_revision", "image", "service_account", "url", "traffic",
            )
        }}, indent=2, sort_keys=True))
        return 0 if cloud_run.get("available") else 2
    ok, differences = compare(args.before, args.after, scope=args.scope)
    print(json.dumps({"safe": ok, "differences": differences}, indent=2, sort_keys=True))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
