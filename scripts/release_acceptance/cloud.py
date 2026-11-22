import json
from typing import Any

from .constants import PROTECTED_ENV_KEYS
from .http_util import _run_json
from .security import _protected_fingerprints

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
