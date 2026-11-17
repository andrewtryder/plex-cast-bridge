#!/usr/bin/env python3
"""Inspect an existing Compose installation and prepare a private upgrade plan."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

from upgrade_mounts import resolve_data_mount
from release_acceptance import PROTECTED_ENV_KEYS, _configuration_drift_capture, _catalog_snapshot_capture
from upgrade_inputs import create_inputs_backup, verify_inputs_backup
from upgrade_backup import restored_tree, verify as verify_backup


def docker(*args: str) -> str:
    try:
        result = subprocess.run(["docker", *args], text=True, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Docker command unavailable or timed out") from exc
    if result.returncode:
        # Docker output may contain interpolated environment or registry credentials.
        raise RuntimeError("Docker could not complete the requested read-only inspection")
    return result.stdout


def _signature(value: Any, key: bytes) -> str:
    return hmac.new(key, json.dumps(value, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()


def _catalog_relative(data_dir: str, catalog_path: str | None, workdir: str) -> Path:
    if not catalog_path:
        return Path("catalog.json")
    requested = PurePosixPath(catalog_path)
    if not requested.is_absolute():
        requested = PurePosixPath(workdir) / requested
    if ".." in requested.parts:
        raise ValueError("Custom catalog paths containing parent traversal require manual review")
    try:
        return Path(str(requested.relative_to(PurePosixPath(data_dir))))
    except ValueError as exc:
        raise ValueError("Custom catalog is outside the data mount; its backup needs manual review") from exc


def _read_recorded_inputs(paths: list[str]) -> dict[str, str | None]:
    result = {}
    for name in paths:
        path = Path(name)
        try:
            result[str(path)] = path.read_bytes().hex()
        except FileNotFoundError:
            result[str(path)] = None
        except OSError as exc:
            raise RuntimeError("A Compose configuration input cannot be read") from exc
    return result


def _configuration_inputs(files: list[Path], unresolved: dict[str, Any], environ: dict[str, str]) -> dict[str, str | None]:
    """Capture Compose source files and referenced env/secrets without emitting contents."""
    paths = set(files)
    paths.add(files[0].parent / ".env")
    for value in environ.get("COMPOSE_ENV_FILES", "").split(","):
        if value.strip():
            path = Path(value.strip())
            paths.add(path if path.is_absolute() else files[0].parent / path)
    for service in (unresolved.get("services") or {}).values():
        for item in service.get("env_file") or []:
            path = item.get("path") if isinstance(item, dict) else item
            if path:
                candidate = Path(path)
                paths.add(candidate if candidate.is_absolute() else files[0].parent / candidate)
    for kind in ("secrets", "configs"):
        for item in (unresolved.get(kind) or {}).values():
            if isinstance(item, dict) and item.get("file"):
                candidate = Path(item["file"])
                paths.add(candidate if candidate.is_absolute() else files[0].parent / candidate)
    return _read_recorded_inputs(sorted(str(path.resolve()) for path in paths))


def _require_backed_mounts(container: dict[str, Any], data_dir: str) -> None:
    writable = [mount for mount in container.get("Mounts") or [] if mount.get("RW")]
    if len(writable) != 1 or writable[0].get("Destination") != data_dir:
        raise ValueError("Additional or parent writable mounts need backup review before upgrading")


def inspect(compose: Path | list[Path], service: str | None) -> dict[str, Any]:
    files = [compose] if isinstance(compose, Path) else compose
    if not files or any(not path.is_file() for path in files):
        raise ValueError("Compose file does not exist")
    files = [path.resolve() for path in files]
    prefix = ("compose", *(item for path in files for item in ("-f", str(path))))
    config = json.loads(docker(*prefix, "config", "--format", "json"))
    unresolved = json.loads(docker(*prefix, "config", "--format", "json", "--no-env-resolution"))
    inputs = _configuration_inputs(files, unresolved, dict(os.environ))
    services = config.get("services") or {}
    if service is None:
        service = "plex-cast-bridge" if "plex-cast-bridge" in services else next(iter(services)) if len(services) == 1 else None
    if not service or service not in services:
        raise ValueError("Select the bridge service with --service")
    image = services[service].get("image")
    if not image:
        raise ValueError("The bridge service must specify an image")
    ids = docker(*prefix, "ps", "-q", service).split()
    if len(ids) != 1:
        raise ValueError("Exactly one running bridge container is required")
    container = json.loads(docker("inspect", ids[0]))[0]
    if not (container.get("State") or {}).get("Running"):
        raise ValueError("The bridge container is not running")
    data_dir = docker("exec", ids[0], "python", "-c", "from plex_cast_bridge.catalog_store import DATA_DIR; print(DATA_DIR)").strip()
    running = [cid for cid in docker("ps", "-q").split() if not container["Id"].startswith(cid)]
    others = json.loads(docker("inspect", *running)) if running else []
    data_source = resolve_data_mount(container, others, data_dir)
    _require_backed_mounts(container, data_dir)
    image_info = json.loads(docker("image", "inspect", container["Image"]))[0]
    drift = _configuration_drift_capture(ids[0])
    catalog = _catalog_snapshot_capture(ids[0])
    if not catalog.get("available"):
        raise RuntimeError("The selected catalog could not be inspected without changing it")
    drift_summary = {"status": "PASS" if drift.get("available") else "NOT TESTED",
                     "warnings": (drift.get("report") or {}).get("release_preflight_warnings", [])}
    identity = {name: container.get(name) for name in ("Id", "Image", "Config", "HostConfig", "Mounts")}
    # Configuration is retained only in memory and included in a keyed combined
    # comparison; no individual credential values or hashes are printed.
    env = dict(item.split("=", 1) for item in (container.get("Config") or {}).get("Env", []) if "=" in item)
    catalog_relative = _catalog_relative(data_dir, env.get("CATALOG_PATH"),
                                         (container.get("Config") or {}).get("WorkingDir") or "/")
    protected_files = {}
    for name in ("plex_connection.json", "app_settings.json", "catalog.json", "google_home_sync.json", "google_home_setup.json", "plex_device_private.pem", "plex_device_public.pem"):
        path = data_source / (catalog_relative if name == "catalog.json" else name)
        try:
            protected_files[name] = path.read_bytes().hex()
        except FileNotFoundError:
            protected_files[name] = None
        except OSError as exc:
            raise RuntimeError("A persistent configuration file is not readable") from exc
    credentials = env.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if credentials:
        target = PurePosixPath(credentials)
        candidates = [m for m in container.get("Mounts") or [] if m.get("Destination") and
                      (target == PurePosixPath(m["Destination"]) or PurePosixPath(m["Destination"]) in target.parents)]
        if not target.is_absolute() or not candidates:
            raise ValueError("Subscriber credential path is not on a persistent mount")
        chosen = max(candidates, key=lambda item: len(PurePosixPath(item["Destination"]).parts))
        path = Path(chosen["Source"]) / str(target.relative_to(PurePosixPath(chosen["Destination"])))
        try:
            protected_files["subscriber_credentials"] = path.read_bytes().hex()
            inputs[str(path.resolve())] = protected_files["subscriber_credentials"]
        except OSError as exc:
            raise RuntimeError("The mounted subscriber credential file cannot be read") from exc
    return {"compose": str(files[0]), "compose_files": [str(path) for path in files], "service": service, "compose_image": image,
            "container_id": container["Id"], "current_image_id": container["Image"],
            "current_image_digests": image_info.get("RepoDigests") or [],
            "current_version": ((image_info.get("Config") or {}).get("Labels") or {}).get("org.opencontainers.image.version") or "unknown",
            "data_dir": data_dir, "data_source": str(data_source), "configuration_drift": drift_summary,
            "mounts": [{name: mount.get(name) for name in ("Type", "Name", "Source", "Destination", "RW")}
                       for mount in container.get("Mounts") or []],
            "subscriber_credentials": "mounted" if credentials else "not configured",
            "configuration_input_paths": sorted(inputs),
            "music_index_present": (data_source / "music_index.sqlite3").exists(),
            "_protected": {"compose": config, "inputs": inputs, "container": identity, "files": protected_files,
                           "catalog_snapshot": catalog["speakers"]}}


def latest_release() -> dict[str, str]:
    """Discover an official release and resolve its published OCI digest."""
    import re
    from urllib.request import Request, urlopen
    url = "https://api.github.com/repos/andrewtryder/plex-cast-bridge/releases/latest"
    try:
        with urlopen(Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "plex-cast-bridge-upgrade"}), timeout=20) as response:
            release = json.load(response)
    except (OSError, ValueError) as exc:
        raise RuntimeError("Could not check the latest published release") from exc
    if not isinstance(release, dict):
        raise RuntimeError("Latest release metadata is invalid")
    match = re.fullmatch(r"(?:plex-cast-bridge-)?v(\d+\.\d+\.\d+)", str(release.get("tag_name") or ""))
    if not match or release.get("prerelease") or release.get("draft"):
        raise RuntimeError("The latest release does not have a supported stable version")
    version = match.group(1)
    image = "ghcr.io/andrewtryder/plex-cast-bridge"
    metadata = docker("buildx", "imagetools", "inspect", f"{image}:{version}")
    digest = re.search(r"(?m)^Digest:\s*(sha256:[0-9a-f]{64})\s*$", metadata)
    if not digest:
        raise RuntimeError("Could not determine an immutable image digest for the latest release")
    return {"version": version, "target": f"{image}@{digest.group(1)}"}


def public(state: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in state.items() if not k.startswith("_")}


def _persistent_identity(files: dict[str, str | None]) -> dict[str, Any]:
    def obj(name: str) -> dict[str, Any] | None:
        raw = files.get(name)
        if raw is None:
            return None
        try:
            value = json.loads(bytes.fromhex(raw))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("A persistent configuration file is invalid") from exc
        if not isinstance(value, dict):
            raise ValueError("A persistent configuration file is invalid")
        return value

    plex, app, catalog = (obj(name) for name in ("plex_connection.json", "app_settings.json", "catalog.json"))
    sync, setup = (obj(name) for name in ("google_home_sync.json", "google_home_setup.json"))
    plex_identity = None if plex is None else {key: plex.get(key) for key in ("mode", "client_identifier", "server_client_id", "server_url", "music_section")}
    if plex_identity is not None:
        plex_identity["jwt_present"] = bool(plex.get("jwt_token"))
        plex_identity["access_token_present"] = bool(plex.get("access_token"))
    sync_identity = None if sync is None else {key: sync.get(key) for key in ("cloud_gateway_url", "catalog_write_key", "agent_user_id")}
    setup_identity = None if setup is None else {key: setup.get(key) for key in ("project_id", "cloud_run_url", "oauth")}
    catalog_identity = None
    if catalog is not None:
        speakers = catalog.get("speakers", [])
        if not isinstance(speakers, list) or any(not isinstance(speaker, dict) for speaker in speakers):
            raise ValueError("The persisted catalog is invalid")
        fields = ("id", "google_home_device_id", "uuid", "cast_uuid", "name", "speaker_name", "google_home_name",
                  "enabled", "google_home_visible", "room_hint", "roomHint", "kind", "removed")
        projected = [{key: speaker[key] for key in fields if key in speaker} for speaker in speakers]
        projected.sort(key=lambda value: json.dumps(value, sort_keys=True))
        catalog_identity = {"speakers": projected}
    return {"plex": plex_identity, "settings": app, "catalog": catalog_identity, "gateway": sync_identity,
            "wizard": setup_identity, "private_key": files.get("plex_device_private.pem"),
            "public_key": files.get("plex_device_public.pem"), "subscriber_credentials": files.get("subscriber_credentials")}


def _mount_signature(container: dict[str, Any], key: bytes) -> str:
    mounts = [{name: mount.get(name) for name in ("Type", "Name", "Source", "Destination", "RW")}
              for mount in container.get("Mounts") or []]
    mounts.sort(key=lambda mount: str(mount.get("Destination")))
    return _signature(mounts, key)


def _verification_signatures(state: dict[str, Any], key: bytes) -> dict[str, str]:
    protected = state["_protected"]
    container = protected["container"]

    raw_env = (container.get("Config") or {}).get("Env") or []
    runtime = dict(item.split("=", 1) for item in raw_env if "=" in item)
    keys = set(PROTECTED_ENV_KEYS) | {"DATA_DIR", "CATALOG_PATH", "GEMINI_API_KEY", "LOCAL_API_KEY",
                                       "SPEAKER_DISCOVERY_INTERVAL_SEC", "CATALOG_RECONCILE_INTERVAL_SEC",
                                       "PCB_DISABLE_BACKGROUND_DISCOVERY"}
    runtime = {name: runtime[name] for name in keys if name in runtime}
    return {"persistent_identity": _signature({"files": _persistent_identity(protected["files"]),
                                                "selected_catalog": protected.get("catalog_snapshot")}, key),
            "compose_configuration": _signature({"rendered": protected["compose"], "inputs": protected.get("inputs", {})}, key),
            "runtime_configuration": _signature(runtime, key),
            "mounts": _mount_signature(container, key)}


def verify_plan(content: dict[str, Any], state: dict[str, Any]) -> list[str]:
    """Return non-secret reasons why a post-upgrade inspection is not verified."""
    try:
        key = bytes.fromhex(content["key"])
        if content.get("schema") != 5 or len(key) != 32:
            raise ValueError
        baseline = content["verification"]
        installation = content["installation"]
        if not isinstance(baseline, dict) or not isinstance(installation, dict) or not isinstance(installation.get("configuration_drift"), dict):
            raise ValueError
        current = _verification_signatures(state, key)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Plan lacks valid verification evidence; prepare a new plan") from exc
    failures = []
    for label, reason in (("persistent_identity", "persistent identities or credentials changed"),
                          ("compose_configuration", "Compose configuration changed"),
                          ("runtime_configuration", "runtime configuration changed"),
                          ("mounts", "persistent mounts changed")):
        if not isinstance(baseline.get(label), str) or not hmac.compare_digest(baseline[label], current[label]):
            failures.append(reason)
    if state.get("configuration_drift", {}).get("status") != "PASS":
        failures.append("configuration drift diagnostic NOT TESTED")
    elif state["configuration_drift"].get("warnings") != installation["configuration_drift"].get("warnings"):
        failures.append("gateway drift warnings changed")
    if content.get("target") not in state.get("current_image_digests", []):
        failures.append("running image does not have the reviewed digest")
    return failures


def plan(state: dict[str, Any], target: str, plan_file: Path) -> dict[str, Any]:
    import re
    if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", target):
        raise ValueError("Target must be an immutable image reference ending in @sha256:<64 hex digits>")
    docker("buildx", "imagetools", "inspect", target)
    backup_dir = plan_file.with_suffix(".backup").resolve()
    data_source = Path(state.get("data_source", "/nonexistent")).resolve()
    if backup_dir == data_source or data_source in backup_dir.parents:
        raise ValueError("Backup location must be outside the persistent data directory")
    if state["configuration_drift"]["status"] != "PASS":
        raise ValueError("Configuration drift diagnostic is NOT TESTED; resolve this before planning an upgrade")
    if plan_file.exists():
        if plan_file.stat().st_mode & 0o077:
            raise ValueError("Existing plan file must be private")
        try:
            content = load_plan(plan_file)
            key = bytes.fromhex(content["key"])
            if (len(key) != 32 or content.get("schema") != 5 or content.get("target") != target
                    or content.get("installation") != public(state) or content.get("backup_dir") != str(backup_dir)
                    or not hmac.compare_digest(content["protected_state"], _signature({"protected": state["_protected"], "target": target, "installation": public(state), "backup_dir": str(backup_dir)}, key))):
                raise ValueError("Existing plan no longer matches the installation; choose a new plan file")
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Existing plan file is invalid") from exc
    else:
        key = secrets.token_bytes(32)
        content = {"schema": 5, "installation": public(state), "target": target, "backup_dir": str(backup_dir),
                   "key": key.hex(), "protected_state": _signature({"protected": state["_protected"], "target": target, "installation": public(state), "backup_dir": str(backup_dir)}, key),
                   "verification": _verification_signatures(state, key)}
        content["plan_auth"] = _plan_auth(content, key)
        plan_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(plan_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(content, stream, indent=2)
            stream.write("\n")
    return {"installation": public(state), "target": target, "plan_file": str(plan_file), "backup_dir": str(backup_dir),
            "actions": ["Pull the reviewed image before stopping the bridge", "Stop the bridge and verify an isolated backup restore",
                        "Replace the local image and check health", "Verify persistent state against the captured baseline"],
            "review": (["Gateway environment conflicts require review before upgrade."] if state["configuration_drift"]["warnings"] else []) + [
                       "Other host processes must not write the data directory during backup.",
                       "Cloud Run is separate and will not be deployed.",
                       "Apply is experimental; review all actions and keep the backup until verification completes."]}


def _plan_auth(content: dict[str, Any], key: bytes) -> str:
    return _signature({name: content.get(name) for name in ("schema", "installation", "target", "backup_dir", "protected_state", "verification")}, key)


def load_plan(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_mode & 0o077:
            raise ValueError("Plan file must be private")
        content = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(content, dict) or content.get("schema") != 5:
            raise ValueError("Plan file is invalid or obsolete")
        key = bytes.fromhex(content["key"])
        if len(key) != 32 or not isinstance(content.get("plan_auth"), str) or not hmac.compare_digest(content["plan_auth"], _plan_auth(content, key)):
            raise ValueError("Plan file is invalid or has changed")
        installation = content["installation"]
        if not isinstance(installation, dict) or not isinstance(installation.get("compose_files"), list):
            raise ValueError("Plan file is invalid")
        return content
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Could not read a valid private plan") from exc


def check_plan_file(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    content = load_plan(path)
    saved = content["installation"]
    state = inspect([Path(value) for value in saved["compose_files"]], saved["service"])
    key = bytes.fromhex(content["key"])
    if (public(state) != saved or not isinstance(content.get("protected_state"), str)
            or not hmac.compare_digest(content["protected_state"], _signature({"protected": state["_protected"], "target": content.get("target"), "installation": saved, "backup_dir": content.get("backup_dir")}, key))):
        raise ValueError("Reviewed plan no longer matches the installation; create a new plan")
    return content, state


def _music_snapshot(path: Path) -> dict[str, Any] | None:
    """Read stable saved-mix rows without creating or updating SQLite sidecars."""
    if not path.exists():
        return None
    journal = path.with_name(path.name + "-journal")
    wal = path.with_name(path.name + "-wal")
    if journal.exists() or wal.exists():
        raise RuntimeError("music index verification NOT TESTED while a journal or WAL exists")
    try:
        before = path.stat()
        db = sqlite3.connect(path.resolve().as_uri() + "?immutable=1", uri=True, timeout=5)
        try:
            if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise RuntimeError("music index integrity check failed")
            mixes = db.execute("SELECT * FROM mixes ORDER BY id").fetchall()
            tracks = db.execute("SELECT * FROM mix_tracks ORDER BY mix_id, position").fetchall()
            index_ids = {
                "tracks": set(db.execute("SELECT server_id, library_id, rating_key FROM tracks")),
                "artists": set(db.execute("SELECT server_id, library_id, artist_key FROM artists")),
                "albums": set(db.execute("SELECT server_id, library_id, album_key FROM albums")),
            }
        finally:
            db.close()
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or journal.exists() or wal.exists():
            raise RuntimeError("music index verification NOT TESTED because the database changed")
        return {"mixes": mixes, "mix_tracks": tracks, "index_ids": index_ids}
    except (OSError, sqlite3.Error) as exc:
        raise RuntimeError("music index verification NOT TESTED") from exc


def verify_music_backup(backup: Path, data_source: Path) -> list[str]:
    try:
        with restored_tree(backup) as restored:
            before = _music_snapshot(restored / "music_index.sqlite3")
        after = _music_snapshot(data_source / "music_index.sqlite3")
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        return [str(exc) if isinstance(exc, RuntimeError) else "music index verification NOT TESTED"]
    if before is not None:
        if after is None or before["mixes"] != after["mixes"] or before["mix_tracks"] != after["mix_tracks"]:
            return ["music index or saved mixes changed from the verified backup"]
        if any(not before["index_ids"][name].issubset(after["index_ids"][name]) for name in ("tracks", "artists", "albums")):
            return ["music index or saved mixes changed from the verified backup"]
    return []


def _archive_mac(path: Path, key: bytes) -> str:
    digest = hmac.new(key, digestmod=hashlib.sha256)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _receipt(content: dict[str, Any], backup_dir: Path) -> dict[str, Any]:
    key = bytes.fromhex(content["key"])
    return {"schema": 2, "plan_auth": content["plan_auth"], "target": content["target"],
            "data_archive": _archive_mac(backup_dir / "data.tgz", key),
            "configuration_archive": _archive_mac(backup_dir / "configuration.tgz", key)}


def apply_plan(plan_path: Path, *, confirm_exclusive: bool, acknowledge_drift: bool, health_url: str) -> int:
    content, state = check_plan_file(plan_path)
    if not confirm_exclusive:
        raise ValueError("Confirm that all non-container writers to the data directory are stopped")
    if state["configuration_drift"]["warnings"] and not acknowledge_drift:
        raise ValueError("Gateway drift warnings require explicit acknowledgement after review")
    backup_dir = Path(content["backup_dir"])
    if backup_dir.exists():
        raise ValueError("The planned backup directory already exists; inspect it before retrying")
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        create_inputs_backup(state["_protected"]["inputs"], backup_dir / "configuration.tgz")
        verify_inputs_backup(state["_protected"]["inputs"], backup_dir / "configuration.tgz")
        check_plan_file(plan_path)
    except (OSError, ValueError, RuntimeError):
        # Keep the private directory for inspection; never overwrite a partial backup.
        raise
    compose_files = state["compose_files"]
    first = Path(compose_files[0])
    app_dir = first.parent.parent if first.parent.name == "deploy" else first.parent
    env = {**os.environ, "APP_DIR": str(app_dir), "COMPOSE_FILE": str(first),
           "PCB_COMPOSE_FILES_JSON": json.dumps(compose_files), "SERVICE_NAME": state["service"],
           "TARGET_IMAGE": content["target"], "PCB_REVIEW_PLAN": str(plan_path.resolve()),
           "PCB_BACKUP_DIR": str(backup_dir), "PCB_DRIFT_MODULE": str(Path(__file__).resolve().parents[1] / "src/plex_cast_bridge/config_drift.py"),
           "HEALTH_URL": health_url, "ACK_CONFIG_DRIFT": "1" if acknowledge_drift else "0"}
    result = subprocess.run(["bash", str(Path(__file__).resolve().parent / "proxmox_upgrade.sh"), "upgrade"], env=env, check=False)
    if result.returncode:
        print(f"Upgrade did not complete; preserve the backup at {backup_dir} and review image rollback instructions.", file=sys.stderr)
        return 2
    verify_backup(backup_dir / "data.tgz")
    verify_backup(backup_dir / "configuration.tgz")
    receipt = _receipt(content, backup_dir)
    receipt_path = backup_dir / "apply-completed.json"
    fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(receipt, stream)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return verify_installation(plan_path, health_url=health_url)


def verify_installation(plan_path: Path, *, health_url: str) -> int:
    from urllib.error import URLError
    from urllib.request import urlopen

    content = load_plan(plan_path)
    backup_dir = Path(content["backup_dir"])
    try:
        receipt = json.loads((backup_dir / "apply-completed.json").read_text(encoding="utf-8"))
        if receipt != _receipt(content, backup_dir):
            raise ValueError("Apply receipt or backup contents do not match the reviewed plan")
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Apply completion was not recorded; inspect the backup before recovery") from exc
    verify_backup(backup_dir / "data.tgz")
    saved = content["installation"]
    after = inspect([Path(value) for value in saved["compose_files"]], saved["service"])
    failures = verify_plan(content, after)
    try:
        verify_inputs_backup(after["_protected"]["inputs"], backup_dir / "configuration.tgz")
    except (OSError, ValueError):
        failures.append("configuration backup does not match the reviewed inputs")
    failures.extend(verify_music_backup(backup_dir / "data.tgz", Path(after["data_source"])))
    try:
        with urlopen(health_url, timeout=5) as response:
            if response.status != 200:
                failures.append("health check failed")
    except (OSError, URLError, ValueError):
        failures.append("health check NOT TESTED or unreachable")
    if failures:
        print("Post-upgrade verification incomplete: " + "; ".join(failures), file=sys.stderr)
        print(f"Keep the backup at {backup_dir}; do not restore over a running writer.", file=sys.stderr)
        return 2
    print(f"Upgrade identity and backup checks passed. Keep the verified backup at {backup_dir}.")
    return 0


def rollback_plan(plan_path: Path, *, confirm: bool, health_url: str) -> int:
    if not confirm:
        raise ValueError("Confirm image rollback; the existing data and configuration will be kept")
    content = load_plan(plan_path)
    saved = content["installation"]
    backup_dir = Path(content["backup_dir"])
    verify_backup(backup_dir / "data.tgz")
    verify_backup(backup_dir / "configuration.tgz")
    receipt_path = backup_dir / "apply-completed.json"
    if receipt_path.exists():
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Apply receipt is invalid; inspect the backup manually") from exc
        if receipt != _receipt(content, backup_dir):
            raise ValueError("Backup contents do not match the recorded upgrade")
    old_id = (backup_dir / "previous_image.txt").read_text(encoding="utf-8").strip()
    old_digest = (backup_dir / "previous_image_digest.txt").read_text(encoding="utf-8").strip()
    if old_id != saved["current_image_id"] or (old_digest and old_digest not in saved["current_image_digests"] + [old_id]):
        raise ValueError("Backup image identity does not match the reviewed baseline")
    files = saved["compose_files"]
    prefix = ("compose", *(item for path in files for item in ("-f", path)))
    ids = docker(*prefix, "ps", "-a", "-q", saved["service"]).split()
    if len(ids) > 1:
        raise ValueError("The bridge container cannot be uniquely identified for rollback")
    if ids:
        current = json.loads(docker("inspect", ids[0]))[0]
    else:
        # A failed recreation may have removed the old container before making a
        # replacement. Recheck the reviewed mounts and Compose inputs directly.
        if not isinstance(saved.get("mounts"), list):
            raise ValueError("Reviewed mount evidence is unavailable for recovery")
        config = json.loads(docker(*prefix, "config", "--format", "json"))
        paths = saved.get("configuration_input_paths")
        if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
            raise ValueError("Reviewed configuration input evidence is unavailable")
        inputs = _read_recorded_inputs(paths)
        actual = _signature({"rendered": config, "inputs": inputs}, bytes.fromhex(content["key"]))
        if not hmac.compare_digest(content["verification"]["compose_configuration"], actual):
            raise ValueError("Compose inputs changed; image rollback requires manual review")
        current = {"Id": "", "Mounts": saved["mounts"]}
    running = [cid for cid in docker("ps", "-q").split() if not current["Id"].startswith(cid)]
    others = json.loads(docker("inspect", *running)) if running else []
    source = resolve_data_mount(current, others, saved["data_dir"])
    key = bytes.fromhex(content["key"])
    if str(source) != saved["data_source"] or not hmac.compare_digest(content["verification"]["mounts"], _mount_signature(current, key)):
        raise ValueError("Persistent mounts changed; image rollback requires manual review")
    files = saved["compose_files"]
    first = Path(files[0])
    env = {**os.environ, "COMPOSE_FILE": str(first), "PCB_COMPOSE_FILES_JSON": json.dumps(files),
           "SERVICE_NAME": saved["service"], "HEALTH_URL": health_url}
    print("Rolling back the image only; current data and configuration will not be restored from backup.")
    result = subprocess.run(["bash", str(Path(__file__).resolve().parent / "proxmox_upgrade.sh"),
                             "rollback", str(backup_dir)], env=env, check=False)
    if result.returncode:
        print("Image rollback did not complete; preserve current data and the backup for manual recovery.", file=sys.stderr)
        return 2
    after = inspect([Path(value) for value in files], saved["service"])
    if after["current_image_id"] != old_id:
        print("Rollback image identity could not be verified; inspect the service.", file=sys.stderr)
        return 2
    print("Previous image is running; current data and configuration were preserved.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("doctor", "plan"):
        command = sub.add_parser(action)
        command.add_argument("--compose-file", required=True, type=Path, action="append", help="Compose file; repeat for overrides")
        command.add_argument("--service")
        if action == "doctor":
            command.add_argument("--check-updates", action="store_true", help="Check the latest official release")
        if action == "plan":
            command.add_argument("--target", required=True)
            command.add_argument("--plan-file", required=True, type=Path)
    check = sub.add_parser("check-plan", help=argparse.SUPPRESS)
    check.add_argument("--plan-file", required=True, type=Path)
    check.add_argument("--target", required=True)
    check.add_argument("--backup-dir", required=True, type=Path)
    check.add_argument("--config-backup", required=True, type=Path)
    apply = sub.add_parser("apply", help="Apply a reviewed private plan")
    apply.add_argument("--plan-file", required=True, type=Path)
    apply.add_argument("--confirm-exclusive-data", action="store_true", help="Confirm non-container data writers are stopped")
    apply.add_argument("--acknowledge-gateway-drift", action="store_true")
    apply.add_argument("--health-url", default="http://127.0.0.1:8000/health")
    verify = sub.add_parser("verify", help="Verify a completed upgrade against its reviewed plan")
    verify.add_argument("--plan-file", required=True, type=Path)
    verify.add_argument("--health-url", default="http://127.0.0.1:8000/health")
    rollback = sub.add_parser("rollback", help="Restore the previous image without overwriting current data")
    rollback.add_argument("--plan-file", required=True, type=Path)
    rollback.add_argument("--confirm-image-rollback", action="store_true")
    rollback.add_argument("--health-url", default="http://127.0.0.1:8000/health")
    args = parser.parse_args(argv)
    try:
        if args.action == "rollback":
            return rollback_plan(args.plan_file, confirm=args.confirm_image_rollback, health_url=args.health_url)
        if args.action == "verify":
            return verify_installation(args.plan_file, health_url=args.health_url)
        if args.action == "apply":
            return apply_plan(args.plan_file, confirm_exclusive=args.confirm_exclusive_data,
                              acknowledge_drift=args.acknowledge_gateway_drift, health_url=args.health_url)
        if args.action == "check-plan":
            reviewed, current = check_plan_file(args.plan_file)
            if reviewed.get("target") != args.target or reviewed.get("backup_dir") != str(args.backup_dir.resolve()):
                raise ValueError("Target image or backup location does not match the reviewed plan")
            if args.config_backup.resolve() != args.backup_dir.resolve() / "configuration.tgz":
                raise ValueError("Configuration backup path does not match the reviewed location")
            verify_inputs_backup(current["_protected"]["inputs"], args.config_backup)
            print("Reviewed installation and configuration backup match the plan.")
            return 0
        state = inspect(args.compose_file, args.service)
        output = public(state) if args.action == "doctor" else plan(state, args.target, args.plan_file)
        installation = output["installation"] if args.action == "plan" else output
        print(f"Service: {installation['service']}")
        print(f"Current version: {installation['current_version']}")
        print(f"Current image: {installation['current_image_id']}")
        for digest in installation["current_image_digests"]:
            print(f"Current published digest: {digest}")
        print(f"Compose image: {installation['compose_image']}")
        print(f"Persistent data: {installation['data_source']}")
        drift = installation["configuration_drift"]
        print(f"Configuration diagnostic: {drift['status']}")
        if drift["warnings"]:
            print("Gateway configuration conflicts require review: " + ", ".join(drift["warnings"]))
        if args.action == "doctor" and args.check_updates:
            try:
                configured = installation["compose_image"].split("@", 1)[0]
                if configured.rfind(":") > configured.rfind("/"):
                    configured = configured.rsplit(":", 1)[0]
                if configured.lower() != "ghcr.io/andrewtryder/plex-cast-bridge":
                    raise RuntimeError("Automatic update lookup is only available for the official image")
                available = latest_release()
                print(f"Latest official release: {available['version']}")
                print(f"Available image: {available['target']}")
            except RuntimeError as exc:
                print(f"Update check NOT TESTED: {exc}")
        if args.action == "plan":
            print(f"Target image: {output['target']}")
            print(f"Private plan: {output['plan_file']}")
            print(f"Backup directory: {output['backup_dir']}")
            print("Proposed actions:")
            for action in output["actions"]:
                print(f"  - {action}")
            for note in output["review"]:
                print(f"Review: {note}")
        return 0
    except (ValueError, RuntimeError, OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
