import importlib.util
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cloud_run_upgrade.py"
SPEC = importlib.util.spec_from_file_location("cloud_run_upgrade", SCRIPT)
upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade)
TEST_KEY = hashlib.sha256(b"test-only-key-for-fingerprint").digest()
TEST_LINK_STATE = {
    "linked": True,
    "link_epoch": 4,
    "agent_user_id": "plex-personal-user",
    "catalog_version": 11,
}


def _link_fingerprint(state=TEST_LINK_STATE):
    canonical = json.dumps(state, sort_keys=True, separators=(",", ":"))
    return upgrade._sha(canonical, TEST_KEY)


def _service(secret="never-print-this"):
    return {
        "spec": {"traffic": [
            {"percent": 100, "revisionName": "rev-1"},
            {"revisionName": "rev-1", "tag": "stb"},
        ], "template": {"spec": {
            "serviceAccountName": "gateway@example.iam.gserviceaccount.com",
            "containers": [{"image": "registry/image@sha256:old", "env": [
                {"name": "GCP_PROJECT_ID", "value": "sample-project"},
                {"name": "GOOGLE_CLIENT_ID", "value": "client-id"},
                {"name": "GOOGLE_CLIENT_SECRET", "value": secret},
                {"name": "OAUTH_SIGNING_SECRET", "value": "signing-secret"},
                {"name": "CATALOG_BUCKET", "value": "stable-catalog"},
            ]},
        ]}}},
        "status": {
            "url": "https://gateway.example.run.app",
            "latestCreatedRevisionName": "rev-1",
            "latestReadyRevisionName": "rev-1",
        },
    }


def test_upgrade_fingerprint_contains_only_identity_and_hashes():
    result = upgrade.service_fingerprint(_service(), "sample-project", "gateway", "us-central1", TEST_KEY)
    assert result["execution_service_account"] == "gateway@example.iam.gserviceaccount.com"
    assert result["agent_user_id"] == "plex-personal-user"
    assert "never-print-this" not in str(result)
    assert "signing-secret" not in str(result)
    assert result["env_fingerprints"]["GOOGLE_CLIENT_SECRET"] != "never-print-this"


@pytest.mark.parametrize("version", ["latest", "current"])
def test_service_fingerprint_rejects_mutable_secret_versions(version):
    service = _service()
    secret_env = service["spec"]["template"]["spec"]["containers"][0]["env"][2]
    secret_env.pop("value")
    secret_env["valueSource"] = {
        "secretKeyRef": {"secret": "oauth-client-secret", "version": version}
    }

    with pytest.raises(ValueError, match="numeric, immutable version"):
        upgrade.service_fingerprint(service, "sample-project", "gateway", "us-central1", TEST_KEY)


def test_service_fingerprint_accepts_only_pinned_secret_version():
    service = _service()
    secret_env = service["spec"]["template"]["spec"]["containers"][0]["env"][2]
    secret_env.pop("value")
    secret_env["valueSource"] = {
        "secretKeyRef": {"secret": "oauth-client-secret", "version": "12"}
    }

    result = upgrade.service_fingerprint(service, "sample-project", "gateway", "us-central1", TEST_KEY)
    assert result["env_fingerprints"]["GOOGLE_CLIENT_SECRET"]


def test_upgrade_fingerprint_changes_if_oauth_identity_changes():
    before = upgrade.service_fingerprint(_service(), "sample-project", "gateway", "us-central1", TEST_KEY)
    after = upgrade.service_fingerprint(
        _service(secret="rotated-secret"), "sample-project", "gateway", "us-central1", TEST_KEY
    )
    assert before["configuration_fingerprint"] != after["configuration_fingerprint"]


def test_upgrade_preflight_rejects_project_identity_mismatch():
    with pytest.raises(ValueError, match="GCP_PROJECT_ID"):
        upgrade.service_fingerprint(_service(), "another-project", "gateway", "us-central1", TEST_KEY)


def test_traffic_rollout_refuses_canary_or_split_configuration():
    with pytest.raises(RuntimeError, match="split traffic"):
        upgrade._traffic_update_args([
            {"percent": 90, "revisionName": "rev-1"},
            {"percent": 10, "revisionName": "rev-0"},
        ], "rev-2")


def test_traffic_rollout_preserves_tags_and_accepts_default_latest_route():
    args = upgrade._traffic_update_args([
        {"latestRevision": True, "percent": 100},
        {"revisionName": "rev-1", "tag": "stb"},
    ], "rev-2")
    assert args == ["--to-revisions=rev-2=100", "--update-tags=stb=rev-2"]


def test_apply_changes_only_image_and_preserves_fingerprint(monkeypatch, capsys, tmp_path):
    before = _service()
    after = _service()
    after["spec"]["template"]["spec"]["containers"][0]["image"] = "registry/image@sha256:new"
    after["status"]["latestCreatedRevisionName"] = "rev-2"
    after["status"]["latestReadyRevisionName"] = "rev-1"
    preflight_file = tmp_path / "reviewed-preflight.json"
    baseline = upgrade.service_fingerprint(before, "sample-project", "gateway", "us-central1", TEST_KEY)
    preflight_file.write_text(json.dumps({
        "schema": 2,
        "request": {
            "project": "sample-project", "region": "us-central1", "service": "gateway",
            "target_image": "registry/image@sha256:new",
        },
        "fingerprint_key": TEST_KEY.hex(),
        "configuration_fingerprint": baseline["configuration_fingerprint"],
        "traffic_fingerprint": baseline["traffic_fingerprint"],
        "link_state_fingerprint": _link_fingerprint(),
        "current_image": baseline["image"],
    }))
    preflight_file.chmod(0o600)
    routed = _service()
    routed["spec"]["template"]["spec"]["containers"][0]["image"] = "registry/image@sha256:new"
    routed["spec"]["traffic"] = [
        {"percent": 100, "revisionName": "rev-2"},
        {"revisionName": "rev-2", "tag": "stb"},
    ]
    routed["status"]["latestCreatedRevisionName"] = "rev-2"
    routed["status"]["latestReadyRevisionName"] = "rev-2"
    descriptions = iter((before, after, routed))
    monkeypatch.setattr(upgrade, "_describe", lambda *_args: next(descriptions))
    monkeypatch.setattr(upgrade, "_read_link_state", lambda *_args: TEST_LINK_STATE)
    monkeypatch.setattr(upgrade, "_revision_is_ready", lambda *_args: True)
    commands = []

    def run(args, **kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(upgrade.subprocess, "run", run)
    monkeypatch.setattr("sys.argv", [str(SCRIPT), "--project", "sample-project", "--service", "gateway",
                                     "--image", "registry/image@sha256:new", "--preflight-file",
                                     str(preflight_file), "--apply"])
    assert upgrade.main() == 0
    deployment = commands[0]
    assert "services" in deployment and "update" in deployment
    assert any(arg.startswith("--image=") for arg in deployment)
    assert not any("env" in arg or "service-account" in arg or "iam" in arg for arg in deployment)
    assert commands[1][0:4] == ["gcloud", "run", "services", "update-traffic"]
    assert "--to-revisions=rev-2=100" in commands[1]
    assert "--update-tags=stb=rev-2" in commands[1]
    assert '"status": "upgraded"' in capsys.readouterr().out


def test_apply_refuses_configuration_changed_since_review(monkeypatch, tmp_path, capsys):
    reviewed = _service()
    changed = _service(secret="changed-after-review")
    baseline = upgrade.service_fingerprint(reviewed, "sample-project", "gateway", "us-central1", TEST_KEY)
    preflight_file = tmp_path / "preflight.json"
    preflight_file.write_text(json.dumps({
        "schema": 2,
        "request": {
            "project": "sample-project", "region": "us-central1", "service": "gateway",
            "target_image": "registry/image@sha256:new",
        },
        "fingerprint_key": TEST_KEY.hex(),
        "configuration_fingerprint": baseline["configuration_fingerprint"],
        "traffic_fingerprint": baseline["traffic_fingerprint"],
        "link_state_fingerprint": _link_fingerprint(),
        "current_image": baseline["image"],
    }))
    preflight_file.chmod(0o600)
    monkeypatch.setattr(upgrade, "_describe", lambda *_args: changed)
    monkeypatch.setattr(upgrade, "_read_link_state", lambda *_args: TEST_LINK_STATE)
    calls = []
    monkeypatch.setattr(upgrade.subprocess, "run", lambda args, **_kwargs: calls.append(args))
    monkeypatch.setattr("sys.argv", [str(SCRIPT), "--project", "sample-project", "--service", "gateway",
                                     "--image", "registry/image@sha256:new", "--preflight-file",
                                     str(preflight_file), "--apply"])

    assert upgrade.main() == 2
    assert calls == []
    assert "changed since the reviewed preflight" in capsys.readouterr().err


def test_apply_refuses_traffic_changed_since_review(monkeypatch, tmp_path, capsys):
    reviewed = _service()
    changed = _service()
    changed["spec"]["traffic"] = [
        {"percent": 90, "revisionName": "rev-1"},
        {"percent": 10, "revisionName": "rev-0"},
    ]
    baseline = upgrade.service_fingerprint(reviewed, "sample-project", "gateway", "us-central1", TEST_KEY)
    preflight_file = tmp_path / "preflight.json"
    preflight_file.write_text(json.dumps({
        "schema": 2,
        "request": {
            "project": "sample-project", "region": "us-central1", "service": "gateway",
            "target_image": "registry/image@sha256:new",
        },
        "fingerprint_key": TEST_KEY.hex(),
        "configuration_fingerprint": baseline["configuration_fingerprint"],
        "traffic_fingerprint": baseline["traffic_fingerprint"],
        "link_state_fingerprint": _link_fingerprint(),
        "current_image": baseline["image"],
    }))
    preflight_file.chmod(0o600)
    monkeypatch.setattr(upgrade, "_describe", lambda *_args: changed)
    monkeypatch.setattr(upgrade, "_read_link_state", lambda *_args: TEST_LINK_STATE)
    calls = []
    monkeypatch.setattr(upgrade.subprocess, "run", lambda args, **_kwargs: calls.append(args))
    monkeypatch.setattr("sys.argv", [str(SCRIPT), "--project", "sample-project", "--service", "gateway",
                                     "--image", "registry/image@sha256:new", "--preflight-file",
                                     str(preflight_file), "--apply"])

    assert upgrade.main() == 2
    assert calls == []
    assert "changed since the reviewed preflight" in capsys.readouterr().err


def test_apply_refuses_firestore_link_state_changed_since_review(monkeypatch, tmp_path, capsys):
    service = _service()
    baseline = upgrade.service_fingerprint(service, "sample-project", "gateway", "us-central1", TEST_KEY)
    preflight_file = tmp_path / "preflight.json"
    preflight_file.write_text(json.dumps({
        "schema": 2,
        "request": {
            "project": "sample-project", "region": "us-central1", "service": "gateway",
            "target_image": "registry/image@sha256:new",
        },
        "fingerprint_key": TEST_KEY.hex(),
        "configuration_fingerprint": baseline["configuration_fingerprint"],
        "traffic_fingerprint": baseline["traffic_fingerprint"],
        "link_state_fingerprint": _link_fingerprint(),
        "current_image": baseline["image"],
    }))
    preflight_file.chmod(0o600)
    changed_link = {**TEST_LINK_STATE, "link_epoch": TEST_LINK_STATE["link_epoch"] + 1}
    monkeypatch.setattr(upgrade, "_describe", lambda *_args: service)
    monkeypatch.setattr(upgrade, "_read_link_state", lambda *_args: changed_link)
    calls = []
    monkeypatch.setattr(upgrade.subprocess, "run", lambda args, **_kwargs: calls.append(args))
    monkeypatch.setattr("sys.argv", [str(SCRIPT), "--project", "sample-project", "--service", "gateway",
                                     "--image", "registry/image@sha256:new", "--preflight-file",
                                     str(preflight_file), "--apply"])

    assert upgrade.main() == 2
    assert calls == []
    assert "Firestore link state changed since the reviewed preflight" in capsys.readouterr().err


def test_apply_reports_firestore_link_state_changed_during_upgrade(monkeypatch, tmp_path, capsys):
    before = _service()
    after = _service()
    after["spec"]["template"]["spec"]["containers"][0]["image"] = "registry/image@sha256:new"
    after["status"]["latestCreatedRevisionName"] = "rev-2"
    after["status"]["latestReadyRevisionName"] = "rev-1"
    baseline = upgrade.service_fingerprint(before, "sample-project", "gateway", "us-central1", TEST_KEY)
    preflight_file = tmp_path / "preflight.json"
    preflight_file.write_text(json.dumps({
        "schema": 2,
        "request": {
            "project": "sample-project", "region": "us-central1", "service": "gateway",
            "target_image": "registry/image@sha256:new",
        },
        "fingerprint_key": TEST_KEY.hex(),
        "configuration_fingerprint": baseline["configuration_fingerprint"],
        "traffic_fingerprint": baseline["traffic_fingerprint"],
        "link_state_fingerprint": _link_fingerprint(),
        "current_image": baseline["image"],
    }))
    preflight_file.chmod(0o600)
    descriptions = iter((before, after))
    link_states = iter((TEST_LINK_STATE, {**TEST_LINK_STATE, "linked": False}))
    monkeypatch.setattr(upgrade, "_describe", lambda *_args: next(descriptions))
    monkeypatch.setattr(upgrade, "_read_link_state", lambda *_args: next(link_states))
    calls = []
    monkeypatch.setattr(upgrade.subprocess, "run", lambda args, **_kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""))
    monkeypatch.setattr("sys.argv", [str(SCRIPT), "--project", "sample-project", "--service", "gateway",
                                     "--image", "registry/image@sha256:new", "--preflight-file",
                                     str(preflight_file), "--apply"])

    assert upgrade.main() == 2
    assert calls
    assert "Post-upgrade Firestore link state changed" in capsys.readouterr().err


def test_preflight_writes_private_review_manifest(monkeypatch, tmp_path, capsys):
    preflight_file = tmp_path / "nested" / "review.json"
    monkeypatch.setattr(upgrade, "_describe", lambda *_args: _service())
    monkeypatch.setattr(upgrade, "_read_link_state", lambda *_args: TEST_LINK_STATE)
    monkeypatch.setattr("sys.argv", [str(SCRIPT), "--project", "sample-project", "--service", "gateway",
                                     "--image", "registry/image@sha256:new", "--preflight-file",
                                     str(preflight_file)])

    assert upgrade.main() == 0
    assert preflight_file.exists()
    assert preflight_file.stat().st_mode & 0o777 == 0o600
    manifest = json.loads(preflight_file.read_text())
    assert "never-print-this" not in preflight_file.read_text()
    assert manifest["current_image"] == "registry/image@sha256:old"
    state_json = json.dumps(TEST_LINK_STATE, sort_keys=True, separators=(",", ":"))
    assert manifest["link_state_fingerprint"] == upgrade._sha(
        state_json, bytes.fromhex(manifest["fingerprint_key"])
    )
    assert '"preflight_file"' in capsys.readouterr().out


def test_preflight_refuses_split_traffic_before_writing_manifest(monkeypatch, tmp_path, capsys):
    service = _service()
    service["spec"]["traffic"] = [
        {"percent": 90, "revisionName": "rev-1"},
        {"percent": 10, "revisionName": "rev-0"},
    ]
    preflight_file = tmp_path / "review.json"
    monkeypatch.setattr(upgrade, "_describe", lambda *_args: service)
    monkeypatch.setattr(upgrade, "_read_link_state", lambda *_args: TEST_LINK_STATE)
    monkeypatch.setattr("sys.argv", [str(SCRIPT), "--project", "sample-project", "--service", "gateway",
                                     "--image", "registry/image@sha256:new", "--preflight-file",
                                     str(preflight_file)])

    assert upgrade.main() == 2
    assert not preflight_file.exists()
    assert "split traffic" in capsys.readouterr().err
