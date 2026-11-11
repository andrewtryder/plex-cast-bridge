#!/usr/bin/env python3
"""Image-only Cloud Run upgrade with OAuth/config identity preflight."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gcp-cloud-run/src"))
from cloud_gateway.catalog_sync import AGENT_USER_ID


def _sha(value: str, key: bytes) -> str:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _normalized_traffic(service: dict[str, Any]) -> list[dict[str, Any]]:
    rows = service.get("spec", {}).get("traffic", []) or []
    normalized = []
    for row in rows:
        normalized.append({
            key: row[key]
            for key in ("percent", "revisionName", "latestRevision", "tag")
            if key in row
        })
    return sorted(normalized, key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")))


def _traffic_update_args(traffic: list[dict[str, Any]], revision: str) -> list[str]:
    """Move a simple full-traffic service to the new revision and preserve its tags.

    Refuse canary or split configurations: their intent needs a human-selected
    traffic plan. The preflight still records the full traffic configuration so
    any routing edits between review and apply are detected.
    """
    assigned = [row for row in traffic if int(row.get("percent") or 0) > 0]
    if not assigned:
        route_args = ["--to-latest"]
    elif len(assigned) == 1 and int(assigned[0].get("percent") or 0) == 100:
        route_args = [f"--to-revisions={revision}=100"]
    else:
        raise RuntimeError("Cloud Run has split traffic; refusing to guess a rollout percentage")

    tags = sorted({str(row["tag"]) for row in traffic if row.get("tag")})
    if tags:
        route_args.append(f"--update-tags={','.join(f'{tag}={revision}' for tag in tags)}")
    return route_args


def _traffic_targets_revision(traffic: list[dict[str, Any]], revision: str) -> bool:
    assigned = [row for row in traffic if int(row.get("percent") or 0) > 0]
    tags = [row for row in traffic if row.get("tag")]
    def targets(row: dict[str, Any]) -> bool:
        return row.get("revisionName") == revision or row.get("latestRevision") is True

    return (
        len(assigned) == 1
        and int(assigned[0].get("percent") or 0) == 100
        and targets(assigned[0])
        and all(targets(row) for row in tags)
    )


def service_fingerprint(
    service: dict[str, Any], project: str, name: str, region: str, fingerprint_key: bytes
) -> dict[str, Any]:
    """Return identity and hashes only; never return plaintext environment values."""
    spec = service.get("spec", {})
    template = spec.get("template", service.get("template", {}))
    template_spec = template.get("spec", template)
    containers = template_spec.get("containers", [])
    if not containers:
        raise ValueError("Cloud Run service has no container configuration")
    container = containers[0]
    env = {row.get("name"): row for row in container.get("env", []) if row.get("name")}
    required_env = {"GCP_PROJECT_ID", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "OAUTH_SIGNING_SECRET"}
    missing = required_env - env.keys()
    if missing:
        raise ValueError(f"Cloud Run is missing protected configuration: {', '.join(sorted(missing))}")
    env_fingerprints: dict[str, str] = {}
    for key, row in env.items():
        if "value" in row:
            env_fingerprints[key] = _sha(str(row["value"]), fingerprint_key)
        else:
            ref = row.get("valueSource", {}).get("secretKeyRef") or row.get("valueFrom", {}).get("secretKeyRef") or {}
            if not ref:
                raise ValueError(f"Cloud Run configuration {key} has an unsupported secret reference")
            secret_name = ref.get("secret") or ref.get("name")
            # Cloud Run v2 uses `version`; the older v1 representation calls
            # this field `key`. `latest` and aliases can change their resolved
            # value without changing the service template, so they cannot be
            # safely fingerprinted without reading secret material. Fail closed
            # rather than claiming an upgrade preflight protects such a value.
            secret_version = ref.get("version") or ref.get("key")
            if not secret_name or not isinstance(secret_version, str) or not secret_version.isdecimal():
                raise ValueError(
                    f"Cloud Run secret reference for {key} must use a numeric, immutable version"
                )
            reference = json.dumps(
                {"secret": secret_name, "version": secret_version}, sort_keys=True, separators=(",", ":")
            )
            env_fingerprints[key] = _sha(reference, fingerprint_key)

    project_value = env.get("GCP_PROJECT_ID", {}).get("value")
    if project_value != project:
        raise ValueError("Runtime GCP_PROJECT_ID differs from the selected project")
    service_account = template_spec.get("serviceAccountName") or template_spec.get("serviceAccount")
    if not service_account:
        raise ValueError("Cloud Run execution service account is missing")
    uri = service.get("status", {}).get("url") or service.get("uri")
    if not uri:
        raise ValueError("Cloud Run service URL is missing")

    identity = {
        "project": project,
        "region": region,
        "service": name,
        "url": uri,
        "execution_service_account": service_account,
        "agent_user_id": AGENT_USER_ID,
        "oauth_authorize_url": f"{uri.rstrip('/')}/oauth/authorize",
        "oauth_token_url": f"{uri.rstrip('/')}/oauth/token",
        "fulfillment_url": f"{uri.rstrip('/')}/fulfillment",
        "env_fingerprints": env_fingerprints,
        "catalog_bucket": env.get("CATALOG_BUCKET", {}).get("value") or f"{project}-catalog",
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return {
        **identity,
        "configuration_fingerprint": _sha(canonical, fingerprint_key),
        "traffic_fingerprint": _sha(
            json.dumps(_normalized_traffic(service), sort_keys=True, separators=(",", ":")),
            fingerprint_key,
        ),
        "image": container.get("image"),
        "created_revision": service.get("status", {}).get("latestCreatedRevisionName"),
        "ready_revision": service.get("status", {}).get("latestReadyRevisionName"),
    }


def _gcloud_json(args: list[str]) -> dict[str, Any]:
    proc = subprocess.run(["gcloud", *args, "--format=json"], capture_output=True, text=True, check=False)
    if proc.returncode:
        raise RuntimeError((proc.stderr or proc.stdout or "gcloud command failed").strip()[:500])
    return json.loads(proc.stdout or "{}")


def _describe(project: str, region: str, service: str) -> dict[str, Any]:
    return _gcloud_json([
        "run", "services", "describe", service,
        f"--project={project}", f"--region={region}",
    ])


def _revision_is_ready(project: str, region: str, revision: str, image: str) -> bool:
    state = _gcloud_json([
        "run", "revisions", "describe", revision,
        f"--project={project}", f"--region={region}",
    ])
    conditions = state.get("status", {}).get("conditions", [])
    ready = any(row.get("type") == "Ready" and row.get("status") == "True" for row in conditions)
    return ready and state.get("spec", {}).get("containers", [{}])[0].get("image") == image


def _read_link_state(project: str) -> dict[str, Any]:
    """Read the linked-account identity fields without returning tokens or credentials."""
    try:
        from google.cloud import firestore

        client = firestore.Client(project=project)
        collection = client.collection("plex_cast_bridge")
        meta_snapshot = collection.document("catalog_meta").get()
        legacy_link_snapshot = collection.document("home_link").get()
    except Exception as exc:
        raise RuntimeError(f"Could not read Firestore linked-account state ({type(exc).__name__})") from exc
    meta = meta_snapshot.to_dict() if meta_snapshot.exists else {}
    legacy_link = legacy_link_snapshot.to_dict() if legacy_link_snapshot.exists else {}
    linked = meta.get("linked") if "linked" in meta else legacy_link.get("linked", False)
    return {
        "linked": bool(linked),
        "link_epoch": int(meta.get("link_epoch") or 0),
        "agent_user_id": str(meta.get("agent_user_id") or AGENT_USER_ID),
        "catalog_version": int(meta.get("version") or 0),
    }


def _write_preflight(path: Path, manifest: dict[str, Any]) -> None:
    """Persist the reviewed baseline without storing any plaintext env values."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2, sort_keys=True)
        stream.write("\n")


def _read_preflight(path: Path) -> dict[str, Any]:
    try:
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError("preflight manifest permissions must not grant group or other access")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("preflight manifest must be a JSON object")
        if manifest.get("schema") != 2:
            raise ValueError("unsupported preflight manifest schema")
        if not isinstance(manifest.get("request"), dict):
            raise ValueError("preflight request identity is missing")
        if not isinstance(manifest.get("configuration_fingerprint"), str):
            raise ValueError("configuration fingerprint is missing")
        if not isinstance(manifest.get("link_state_fingerprint"), str):
            raise ValueError("Firestore link-state fingerprint is missing")
        if not isinstance(manifest.get("traffic_fingerprint"), str):
            raise ValueError("Cloud Run traffic fingerprint is missing")
        if not isinstance(manifest.get("current_image"), str) or not manifest["current_image"]:
            raise ValueError("current image identity is missing")
        key = bytes.fromhex(manifest["fingerprint_key"])
        if len(key) != 32:
            raise ValueError("invalid preflight fingerprint key")
        return manifest
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read Cloud Run preflight manifest: {exc}") from exc
    except ValueError as exc:
        raise RuntimeError(f"Invalid Cloud Run preflight manifest: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", default="us-central1")
    parser.add_argument("--service", default="plex-google-home-fulfillment")
    parser.add_argument("--image", required=True, help="Immutable image reference containing @sha256:")
    parser.add_argument(
        "--preflight-file", required=True, type=Path,
        help="Private manifest path reused for the reviewed preflight and the later apply",
    )
    parser.add_argument("--apply", action="store_true", help="Deploy image only after a passing preflight")
    args = parser.parse_args()
    if "@sha256:" not in args.image:
        parser.error("--image must be pinned to an immutable sha256 digest")
    try:
        if args.apply:
            manifest = _read_preflight(args.preflight_file)
            expected_identity = {
                "project": args.project,
                "region": args.region,
                "service": args.service,
                "target_image": args.image,
            }
            if manifest.get("request") != expected_identity:
                raise RuntimeError("Preflight manifest does not match the requested project, service, region, or image")
            fingerprint_key = bytes.fromhex(manifest["fingerprint_key"])
        else:
            if args.preflight_file.exists():
                raise RuntimeError(
                    f"Preflight manifest already exists: {args.preflight_file}; use a new path for a new review"
                )
            fingerprint_key = secrets.token_bytes(32)
        before_raw = _describe(args.project, args.region, args.service)
        before = service_fingerprint(
            before_raw, args.project, args.service, args.region, fingerprint_key,
        )
        link_state_before = _read_link_state(args.project)
        link_state_fingerprint_before = _sha(
            json.dumps(link_state_before, sort_keys=True, separators=(",", ":")), fingerprint_key
        )
        if args.apply:
            if before["configuration_fingerprint"] != manifest.get("configuration_fingerprint"):
                raise RuntimeError("Cloud Run configuration changed since the reviewed preflight; refusing image update")
            if before["image"] != manifest.get("current_image"):
                raise RuntimeError("Cloud Run image changed since the reviewed preflight; refusing image update")
            if before["traffic_fingerprint"] != manifest.get("traffic_fingerprint"):
                raise RuntimeError("Cloud Run traffic changed since the reviewed preflight; refusing image update")
            if link_state_fingerprint_before != manifest.get("link_state_fingerprint"):
                raise RuntimeError("Firestore link state changed since the reviewed preflight; refusing image update")
            # Refuse split/canary routing before changing the service template.
            _traffic_update_args(_normalized_traffic(before_raw), "candidate")
        else:
            # A reviewed preflight must already have a rollout plan that is safe
            # to execute. Do not create a manifest for canary/split traffic.
            _traffic_update_args(_normalized_traffic(before_raw), "candidate")
            manifest = {
                "schema": 2,
                "request": {
                    "project": args.project,
                    "region": args.region,
                    "service": args.service,
                    "target_image": args.image,
                },
                "fingerprint_key": fingerprint_key.hex(),
                "configuration_fingerprint": before["configuration_fingerprint"],
                "traffic_fingerprint": before["traffic_fingerprint"],
                "link_state_fingerprint": link_state_fingerprint_before,
                "current_image": before["image"],
            }
            _write_preflight(args.preflight_file, manifest)
        print(json.dumps({
            "mode": "apply" if args.apply else "preflight-only",
            "project": before["project"], "region": before["region"], "service": before["service"],
            "url": before["url"], "execution_service_account": before["execution_service_account"],
            "agent_user_id": before["agent_user_id"], "ready_revision": before["ready_revision"],
            "configuration_fingerprint": before["configuration_fingerprint"],
            "traffic_fingerprint": before["traffic_fingerprint"],
            "firestore_link_state": link_state_before,
            "link_state_fingerprint": link_state_fingerprint_before,
            "protected_environment_fingerprints": before["env_fingerprints"],
            "current_image": before["image"], "target_image": args.image,
            "preflight_file": str(args.preflight_file),
        }, indent=2, sort_keys=True))
        if not args.apply:
            return 0

        update = subprocess.run([
            "gcloud", "run", "services", "update", args.service,
            f"--project={args.project}", f"--region={args.region}", f"--image={args.image}",
        ], capture_output=True, text=True, check=False)
        if update.returncode:
            raise RuntimeError((update.stderr or update.stdout or "Cloud Run image update failed").strip()[:500])

        after_raw = _describe(args.project, args.region, args.service)
        after = service_fingerprint(
            after_raw, args.project, args.service, args.region, fingerprint_key,
        )
        link_state_after = _read_link_state(args.project)
        link_state_fingerprint_after = _sha(
            json.dumps(link_state_after, sort_keys=True, separators=(",", ":")), fingerprint_key
        )
        if before["configuration_fingerprint"] != after["configuration_fingerprint"]:
            raise RuntimeError("Post-upgrade configuration fingerprint changed; inspect the service before further rollout")
        if link_state_fingerprint_before != link_state_fingerprint_after:
            raise RuntimeError("Post-upgrade Firestore link state changed; inspect account-link state before further rollout")
        if after["image"] != args.image:
            raise RuntimeError("Post-upgrade service image does not match the requested immutable digest")
        revision = after["created_revision"]
        if not revision or revision == before["created_revision"]:
            raise RuntimeError("Cloud Run did not create a new revision for the requested image")
        if not _revision_is_ready(args.project, args.region, revision, args.image):
            raise RuntimeError(
                f"New Cloud Run revision {revision} is not ready; previous traffic remains assigned"
            )
        if before["traffic_fingerprint"] != after["traffic_fingerprint"]:
            raise RuntimeError("Cloud Run traffic changed during image update; inspect routing before rollout")

        traffic_args = _traffic_update_args(_normalized_traffic(after_raw), revision)
        traffic_update = subprocess.run([
            "gcloud", "run", "services", "update-traffic", args.service,
            f"--project={args.project}", f"--region={args.region}", *traffic_args,
        ], capture_output=True, text=True, check=False)
        if traffic_update.returncode:
            raise RuntimeError(
                (traffic_update.stderr or traffic_update.stdout or "Cloud Run traffic update failed").strip()[:500]
            )

        final_raw = _describe(args.project, args.region, args.service)
        after = service_fingerprint(final_raw, args.project, args.service, args.region, fingerprint_key)
        if not _traffic_targets_revision(_normalized_traffic(final_raw), revision):
            raise RuntimeError(
                f"Cloud Run traffic did not move fully to {revision}; inspect the service and restore prior routing if needed"
            )
        if after["configuration_fingerprint"] != before["configuration_fingerprint"]:
            raise RuntimeError("Final Cloud Run configuration fingerprint changed; inspect the service before further rollout")
        if after["image"] != args.image:
            raise RuntimeError("Final Cloud Run service image does not match the requested immutable digest")
        if after["ready_revision"] != revision:
            raise RuntimeError(f"Cloud Run revision {revision} did not become the latest ready revision")
        link_state_after = _read_link_state(args.project)
        link_state_fingerprint_after = _sha(
            json.dumps(link_state_after, sort_keys=True, separators=(",", ":")), fingerprint_key
        )
        if link_state_fingerprint_before != link_state_fingerprint_after:
            raise RuntimeError("Post-traffic Firestore link state changed; inspect account-link state before further rollout")
        print(json.dumps({
            "status": "upgraded",
            "ready_revision": revision,
            "configuration_fingerprint": after["configuration_fingerprint"],
            "firestore_link_state": link_state_after,
            "link_state_fingerprint": link_state_fingerprint_after,
            "image": after["image"],
        }, indent=2, sort_keys=True))
        return 0
    except (RuntimeError, ValueError, json.JSONDecodeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
