#!/usr/bin/env python3
"""Inspect an existing Compose installation and prepare a private upgrade plan."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

from upgrade_mounts import resolve_data_mount
from release_acceptance import _configuration_drift_capture


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


def inspect(compose: Path, service: str | None) -> dict[str, Any]:
    if not compose.is_file():
        raise ValueError("Compose file does not exist")
    compose = compose.resolve()
    prefix = ("compose", "-f", str(compose))
    config = json.loads(docker(*prefix, "config", "--format", "json"))
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
    image_info = json.loads(docker("image", "inspect", container["Image"]))[0]
    drift = _configuration_drift_capture(ids[0])
    drift_summary = {"status": "PASS" if drift.get("available") else "NOT TESTED",
                     "warnings": (drift.get("report") or {}).get("release_preflight_warnings", [])}
    identity = {name: container.get(name) for name in ("Id", "Image", "Config", "HostConfig", "Mounts")}
    # Configuration is retained only in memory and included in a keyed combined
    # comparison; no individual credential values or hashes are printed.
    protected_files = {}
    for name in ("plex_connection.json", "app_settings.json", "catalog.json", "google_home_sync.json", "google_home_setup.json", "plex_device_private.pem", "plex_device_public.pem"):
        path = data_source / name
        try:
            protected_files[name] = path.read_bytes().hex()
        except FileNotFoundError:
            protected_files[name] = None
        except OSError as exc:
            raise RuntimeError("A persistent configuration file is not readable") from exc
    env = dict(item.split("=", 1) for item in (container.get("Config") or {}).get("Env", []) if "=" in item)
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
        except OSError as exc:
            raise RuntimeError("The mounted subscriber credential file cannot be read") from exc
    return {"compose": str(compose), "service": service, "compose_image": image,
            "container_id": container["Id"], "current_image_id": container["Image"],
            "current_image_digests": image_info.get("RepoDigests") or [],
            "current_version": ((image_info.get("Config") or {}).get("Labels") or {}).get("org.opencontainers.image.version") or "unknown",
            "data_dir": data_dir, "data_source": str(data_source), "configuration_drift": drift_summary,
            "subscriber_credentials": "mounted" if credentials else "not configured",
            "_protected": {"compose": config, "container": identity, "files": protected_files}}


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


def plan(state: dict[str, Any], target: str, plan_file: Path) -> dict[str, Any]:
    import re
    if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", target):
        raise ValueError("Target must be an immutable image reference ending in @sha256:<64 hex digits>")
    docker("buildx", "imagetools", "inspect", target)
    if state["configuration_drift"]["status"] != "PASS":
        raise ValueError("Configuration drift diagnostic is NOT TESTED; resolve this before planning an upgrade")
    if plan_file.exists():
        if plan_file.stat().st_mode & 0o077:
            raise ValueError("Existing plan file must be private")
        try:
            content = json.loads(plan_file.read_text(encoding="utf-8"))
            key = bytes.fromhex(content["key"])
            if (len(key) != 32 or content.get("schema") != 1 or content.get("target") != target
                    or content.get("installation") != public(state)
                    or not hmac.compare_digest(content["protected_state"], _signature(state["_protected"], key))):
                raise ValueError("Existing plan no longer matches the installation; choose a new plan file")
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Existing plan file is invalid") from exc
    else:
        key = secrets.token_bytes(32)
        content = {"schema": 1, "installation": public(state), "target": target,
                   "key": key.hex(), "protected_state": _signature(state["_protected"], key)}
        plan_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(plan_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(content, stream, indent=2)
            stream.write("\n")
    return {"installation": public(state), "target": target, "plan_file": str(plan_file),
            "actions": ["Pull the reviewed image before stopping the bridge", "Stop the bridge and verify an isolated backup restore",
                        "Replace the local image and check health", "Verify persistent state against the captured baseline"],
            "review": (["Gateway environment conflicts require review before upgrade."] if state["configuration_drift"]["warnings"] else []) + [
                       "Other host processes must not write the data directory during backup.",
                       "Cloud Run is separate and will not be deployed.",
                       "This plan is not yet executable by the draft upgrade workflow."]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("doctor", "plan"):
        command = sub.add_parser(action)
        command.add_argument("--compose-file", required=True, type=Path)
        command.add_argument("--service")
        if action == "doctor":
            command.add_argument("--check-updates", action="store_true", help="Check the latest official release")
        if action == "plan":
            command.add_argument("--target", required=True)
            command.add_argument("--plan-file", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
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
