import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cloud_run_upgrade.py"
SPEC = importlib.util.spec_from_file_location("cloud_run_upgrade", SCRIPT)
upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade)
TEST_KEY = b"test-only-key-for-fingerprint"


def _service(secret="never-print-this"):
    return {
        "spec": {"template": {"spec": {
            "serviceAccountName": "gateway@example.iam.gserviceaccount.com",
            "containers": [{"image": "registry/image@sha256:old", "env": [
                {"name": "GCP_PROJECT_ID", "value": "sample-project"},
                {"name": "GOOGLE_CLIENT_ID", "value": "client-id"},
                {"name": "GOOGLE_CLIENT_SECRET", "value": secret},
                {"name": "OAUTH_SIGNING_SECRET", "value": "signing-secret"},
                {"name": "CATALOG_BUCKET", "value": "stable-catalog"},
            ]},
        ]}}},
        "status": {"url": "https://gateway.example.run.app", "latestReadyRevisionName": "rev-1"},
    }


def test_upgrade_fingerprint_contains_only_identity_and_hashes():
    result = upgrade.service_fingerprint(_service(), "sample-project", "gateway", "us-central1", TEST_KEY)
    assert result["execution_service_account"] == "gateway@example.iam.gserviceaccount.com"
    assert result["agent_user_id"] == "plex-personal-user"
    assert "never-print-this" not in str(result)
    assert "signing-secret" not in str(result)
    assert result["env_fingerprints"]["GOOGLE_CLIENT_SECRET"] != "never-print-this"


def test_upgrade_fingerprint_changes_if_oauth_identity_changes():
    before = upgrade.service_fingerprint(_service(), "sample-project", "gateway", "us-central1", TEST_KEY)
    after = upgrade.service_fingerprint(
        _service(secret="rotated-secret"), "sample-project", "gateway", "us-central1", TEST_KEY
    )
    assert before["configuration_fingerprint"] != after["configuration_fingerprint"]


def test_upgrade_preflight_rejects_project_identity_mismatch():
    with pytest.raises(ValueError, match="GCP_PROJECT_ID"):
        upgrade.service_fingerprint(_service(), "another-project", "gateway", "us-central1", TEST_KEY)


def test_apply_changes_only_image_and_preserves_fingerprint(monkeypatch, capsys):
    before = _service()
    after = _service()
    after["spec"]["template"]["spec"]["containers"][0]["image"] = "registry/image@sha256:new"
    after["status"]["latestReadyRevisionName"] = "rev-2"
    descriptions = iter((before, after))
    monkeypatch.setattr(upgrade, "_describe", lambda *_args: next(descriptions))
    commands = []

    def run(args, **kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(upgrade.subprocess, "run", run)
    monkeypatch.setattr("sys.argv", [str(SCRIPT), "--project", "sample-project", "--service", "gateway",
                                     "--image", "registry/image@sha256:new", "--apply"])
    assert upgrade.main() == 0
    deployment = commands[0]
    assert "services" in deployment and "update" in deployment
    assert any(arg.startswith("--image=") for arg in deployment)
    assert not any("env" in arg or "service-account" in arg or "iam" in arg for arg in deployment)
    assert '"status": "upgraded"' in capsys.readouterr().out
