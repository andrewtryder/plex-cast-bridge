import json
from pathlib import Path
from typing import Any

from .constants import API_PATHS, CLOUD_IDENTITY_PATHS, LOCAL_IDENTITY_PATHS
from .docker import _valid_drift

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
