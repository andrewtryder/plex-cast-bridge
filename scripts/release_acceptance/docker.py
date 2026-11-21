import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .constants import PROTECTED_ENV_KEYS
from .paths import REPO_ROOT, SCRIPTS_DIR
from .security import _protected_fingerprints
from ._facade import package

def _catalog_snapshot_capture(container: str) -> dict[str, Any]:
    source = SCRIPTS_DIR / "release_catalog_snapshot.py"
    try:
        proc = package().subprocess.run(
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
    source = REPO_ROOT / "src/plex_cast_bridge/config_drift.py"
    try:
        proc = package().subprocess.run(
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
    raw = package()._run_json(["docker", "inspect", container])
    if not raw:
        return None
    # docker inspect contains plaintext env values. Read them only in memory and
    # emit fingerprints, never the values.
    item = raw[0] if isinstance(raw, list) and raw else raw
    config = item.get("Config") or {}
    image_id = item.get("Image")
    inspected_image = package()._run_json(["docker", "image", "inspect", image_id]) if image_id else None
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
