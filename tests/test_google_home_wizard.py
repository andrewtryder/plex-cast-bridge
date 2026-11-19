"""Unit + smoke tests for the Google Home setup wizard (no live GCP)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from plex_cast_bridge import catalog_sync, wizard_verify
from plex_cast_bridge import google_home_setup as setup
from plex_cast_bridge.config import Settings
from plex_cast_bridge.gcp_provision.ops import build_provision_plan, run_provision_step
from plex_cast_bridge.main import app


@pytest.fixture
def wizard_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("ALLOW_UNAUTHENTICATED_LOCAL_API", "true")
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    # catalog_store.DATA_DIR is imported into setup/catalog_sync modules
    from plex_cast_bridge import catalog_store as store

    monkeypatch.setattr(store, "DATA_DIR", data_dir)
    setup.save_setup(setup.default_state())
    catalog_sync.save_sync_state(
        {
            "version": 0,
            "last_push_status": "idle",
            "cloud_gateway_url": None,
            "catalog_write_key": None,
        }
    )
    return data_dir


def _state(**kwargs):
    speakers = kwargs.pop("speakers", [{"name": "Kitchen"}])
    plex = kwargs.pop("plex", object())
    cast = SimpleNamespace(last_discovered=speakers, is_disabled=lambda _n: False)
    return SimpleNamespace(plex=plex, cast=cast, **kwargs)


def test_welcome_requires_plex_and_speakers(wizard_env):
    bad = wizard_verify.verify_step("welcome", _state(plex=None, speakers=[]))
    assert bad["ok"] is False
    names = {c["name"]: c["ok"] for c in bad["checks"]}
    assert names["Plex library"] is False
    assert names["Cast speakers"] is False

    good = wizard_verify.verify_step("welcome", _state())
    assert good["ok"] is True
    assert setup.load_setup().get("verified_welcome") is True


def test_cloud_auth_attach_existing_vs_create(wizard_env):
    # Missing project + companion + create → fail
    fail = wizard_verify.verify_step("cloud_auth", _state(), patch={"mode": "attach_existing"})
    assert fail["ok"] is False

    ok = wizard_verify.verify_step(
        "cloud_auth",
        _state(),
        patch={
            "project_id": "plex-cast-bridge",
            "mode": "attach_existing",
            "companion_auth": {"ok": True, "account": "user@example.com", "adc": True, "detail": "ready"},
            "provision_progress": {"attach_project": "exists", "create_project": "exists"},
        },
    )
    assert ok["ok"] is True
    state = setup.load_setup()
    assert state["project_id"] == "plex-cast-bridge"
    assert state["mode"] == "attach_existing"
    assert state["verified_cloud_auth"] is True


def test_prepare_services_blocks_until_provisioned(wizard_env):
    setup.save_setup({"project_id": "plex-cast-bridge", "mode": "attach_existing"})
    blocked = wizard_verify.verify_step(
        "prepare_services",
        _state(),
        patch={"billing_confirmed": True, "provision_progress": {"confirm_billing": "ok"}},
    )
    assert blocked["ok"] is False
    failed = [c["name"] for c in blocked["checks"] if not c["ok"]]
    assert "APIs enabled" in failed

    ready = wizard_verify.verify_step(
        "prepare_services",
        _state(),
        patch={
            "billing_confirmed": True,
            "provision_progress": {
                "confirm_billing": "ok",
                "enable_apis": "ok",
                "ensure_firestore": "exists",
                "ensure_pubsub": "ok",
                "ensure_service_account": "ok",
                "ensure_catalog_bucket": "ok",
            },
        },
    )
    assert ready["ok"] is True


def test_existing_gateway_detection_survives_missing_or_reset_wizard_state(wizard_env):
    from plex_cast_bridge import catalog_sync

    catalog_sync.save_sync_state({
        "version": 4,
        "cloud_gateway_url": "https://existing.example.run.app",
        "catalog_write_key": "test-key-only",
    })
    setup_path = setup._path()
    setup_path.unlink()
    status = setup.public_status()
    assert status["installation_detected"] is True
    assert status["recommended_operation"] == "application_upgrade"
    assert status["link_confirmed"] is None
    assert status["mode"] == "application_upgrade"

    reset = setup.save_setup(setup.default_state())
    assert reset["mode"] == "application_upgrade"
    assert reset["link_confirmed"] is None


def test_partial_gateway_state_without_url_fails_into_recovery(wizard_env):
    from plex_cast_bridge import catalog_sync

    # The local URL can be lost independently of the remote Home link. A key or
    # prior sync version is enough to keep first-time provisioning disabled.
    catalog_sync.save_sync_state({
        "version": 4,
        "cloud_gateway_url": None,
        "catalog_write_key": "test-key-only",
        "last_push_status": "idle",
    })
    assert setup.detect_installation_operation() == "existing_installation_recovery"
    assert setup.public_status()["installation_detected"] is True


def test_existing_gateway_blocks_provision_preview_and_execution(wizard_env):
    from plex_cast_bridge import catalog_sync

    catalog_sync.save_sync_state({"cloud_gateway_url": "https://existing.example.run.app"})
    response = TestClient(app).post("/api/google-home/provision/preview", json={"project_id": "existing"})
    assert response.status_code == 409
    response = TestClient(app).post("/api/google-home/provision/execute", json={
        "project_id": "existing", "execute_step": "mint_local_secrets", "confirm_billable": True,
    })
    assert response.status_code == 409


def test_mint_local_secrets_refuses_repeat_without_changing_existing_env(wizard_env, monkeypatch, tmp_path):
    from subprocess import CompletedProcess
    from plex_cast_bridge.gcp_provision import ops

    env_file = tmp_path / "cloud.env"
    monkeypatch.setattr(ops, "CLOUD_RUN_ENV", env_file)
    monkeypatch.setattr(ops, "CLOUD_RUN_ENV_EXAMPLE", None)
    monkeypatch.setattr(ops, "_gcloud", lambda _args: CompletedProcess([], 1, "", "NOT_FOUND"))
    plan = build_provision_plan(project_id="new-install-project")
    first = ops.run_provision_step(plan, "mint_local_secrets", dry_run=False)
    assert first["status"] == "ok"
    before = env_file.read_bytes()
    second = ops.run_provision_step(plan, "mint_local_secrets", dry_run=False)
    assert second["status"] == "failed"
    assert "refusing to overwrite" in second["detail"]
    assert env_file.read_bytes() == before


def test_deploy_cloud_run_refuses_existing_service_before_env_changes(wizard_env, monkeypatch, tmp_path):
    from subprocess import CompletedProcess

    from plex_cast_bridge.gcp_provision import ops

    env_file = tmp_path / "cloud.env"
    env_file.write_text("\n".join([
        "GCP_PROJECT_ID=existing-project", "GOOGLE_CLIENT_ID=existing-client",
        "GOOGLE_CLIENT_SECRET=existing-secret", "ADMIN_PASSPHRASE=existing-pass",
        "OAUTH_SIGNING_SECRET=existing-signing-key", "CATALOG_WRITE_KEY=existing-catalog-key",
        "CATALOG_BUCKET=custom-catalog-bucket", "",
    ]))
    monkeypatch.setattr(ops, "CLOUD_RUN_ENV", env_file)
    monkeypatch.setattr(ops, "DEPLOY_SCRIPT", tmp_path / "deploy.sh")
    ops.DEPLOY_SCRIPT.write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.setattr(ops, "_gcloud", lambda _args: CompletedProcess([], 0, "https://existing.example.run.app", ""))
    before = env_file.read_bytes()
    result = ops.run_provision_step(build_provision_plan(project_id="existing-project"), "deploy_cloud_run", dry_run=False)
    assert result["status"] == "failed"
    assert "image-only upgrade" in result["detail"]
    assert env_file.read_bytes() == before


def test_deploy_cloud_run_refuses_project_change_and_keeps_custom_bucket(wizard_env, monkeypatch, tmp_path):
    from plex_cast_bridge.gcp_provision import ops

    env_file = tmp_path / "cloud.env"
    env_file.write_text(
        "GCP_PROJECT_ID=original-project\nGOOGLE_CLIENT_ID=existing-client\n"
        "GOOGLE_CLIENT_SECRET=existing-secret\nADMIN_PASSPHRASE=existing-pass\n"
        "OAUTH_SIGNING_SECRET=existing-signing-key\nCATALOG_WRITE_KEY=existing-catalog-key\n"
        "CATALOG_BUCKET=custom-catalog-bucket\n"
    )
    monkeypatch.setattr(ops, "CLOUD_RUN_ENV", env_file)
    monkeypatch.setattr(ops, "DEPLOY_SCRIPT", tmp_path / "deploy.sh")
    ops.DEPLOY_SCRIPT.write_text("#!/bin/sh\nexit 0\n")
    before = env_file.read_bytes()
    result = ops.run_provision_step(
        build_provision_plan(project_id="different-project"), "deploy_cloud_run", dry_run=False
    )
    assert result["status"] == "failed"
    assert "refusing identity changes" in result["detail"]
    assert env_file.read_bytes() == before


def test_mint_secrets_refuses_when_cloud_run_exists_even_if_local_env_is_missing(wizard_env, monkeypatch, tmp_path):
    from subprocess import CompletedProcess
    from plex_cast_bridge.gcp_provision import ops

    monkeypatch.setattr(ops, "CLOUD_RUN_ENV", tmp_path / "missing.env")
    monkeypatch.setattr(ops, "_gcloud", lambda _args: CompletedProcess([], 0, "https://existing.run.app", ""))
    result = ops.run_provision_step(
        build_provision_plan(project_id="existing-project"), "mint_local_secrets", dry_run=False
    )
    assert result["status"] == "failed"
    assert "refusing to mint replacement" in result["detail"]
    assert not (tmp_path / "missing.env").exists()


def test_deploy_gateway_health_check(wizard_env, monkeypatch):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"status":"ok"}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    result = wizard_verify.verify_step(
        "deploy_gateway",
        _state(),
        patch={"cloud_gateway_url": "https://example.run.app"},
    )
    assert result["ok"] is True
    assert setup.load_setup()["oauth"]["fulfillment_url"] == "https://example.run.app/fulfillment"


def test_advance_requires_verify(wizard_env, monkeypatch):
    monkeypatch.setenv("ALLOW_UNAUTHENTICATED_LOCAL_API", "true")
    settings = Settings(
        plex_token=None,
        local_api_key=None,
        allow_unauthenticated_local_api=True,
    )
    app.state.settings = settings
    app.state.plex = object()
    app.state.cast = SimpleNamespace(last_discovered=[{"name": "Kitchen", "model": "Nest"}], is_disabled=lambda _n: False)
    app.state.queue_manager = None

    client = TestClient(app)
    denied = client.post("/api/google-home/wizard/advance", json={"step": "welcome", "patch": {}})
    assert denied.status_code == 400
    assert "not verified" in denied.json()["detail"]

    verified = client.post("/api/google-home/wizard/verify", json={"step": "welcome", "patch": {}})
    assert verified.status_code == 200
    assert verified.json()["ok"] is True

    advanced = client.post("/api/google-home/wizard/advance", json={"step": "welcome", "patch": {}})
    assert advanced.status_code == 200
    assert "welcome" in advanced.json()["completed_steps"]
    assert advanced.json()["current_step"] == "cloud_auth"


def test_wizard_smoke_happy_path_mocked(wizard_env, monkeypatch):
    """Drive verify→advance through all steps with mocked external deps."""
    monkeypatch.setenv("ALLOW_UNAUTHENTICATED_LOCAL_API", "true")
    settings = Settings(
        plex_token=None,
        local_api_key=None,
        allow_unauthenticated_local_api=True,
        enable_pubsub_listener=False,
    )
    app.state.settings = settings
    app.state.plex = object()
    app.state.cast = SimpleNamespace(
        last_discovered=[{"name": "Kitchen", "model": "Nest Mini"}],
        is_disabled=lambda _n: False,
        discover=lambda: [{"name": "Kitchen"}],
    )
    app.state.queue_manager = None

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"status":"ok"}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    monkeypatch.setattr(
        "plex_cast_bridge.catalog_sync.public_sync_status",
        lambda: {
            "cloud_gateway_configured": True,
            "status": "succeeded",
            "last_error": None,
        },
    )

    client = TestClient(app)
    flow = [
        ("welcome", {}),
        (
            "cloud_auth",
            {
                "project_id": "plex-cast-bridge",
                "mode": "attach_existing",
                "companion_auth": {"ok": True, "account": "u@x.com", "adc": True, "detail": "ready"},
                "provision_progress": {"create_project": "exists", "attach_project": "exists"},
            },
        ),
        (
            "prepare_services",
            {
                "billing_confirmed": True,
                "provision_progress": {
                    "enable_apis": "ok",
                    "ensure_firestore": "ok",
                    "ensure_pubsub": "ok",
                    "ensure_service_account": "ok",
                    "ensure_catalog_bucket": "ok",
                },
            },
        ),
        ("subscriber_auth", {"catalog_write_key": "catalog-secret-key-123456"}),
        ("deploy_gateway", {"cloud_gateway_url": "https://gateway.example.run.app"}),
        (
            "developer_console",
            {"client_id": "plex-gh-test", "developer_console_done": True},
        ),
        ("link_home", {"link_confirmed": True}),
    ]

    for step, patch in flow:
        v = client.post("/api/google-home/wizard/verify", json={"step": step, "patch": patch})
        assert v.status_code == 200, f"{step}: {v.text}"
        body = v.json()
        assert body["ok"] is True, f"{step}: {body}"
        a = client.post("/api/google-home/wizard/advance", json={"step": step, "patch": {}})
        assert a.status_code == 200, f"{step} advance: {a.text}"

    # connection_test runs its own diagnostic endpoint then verify
    test = client.post("/api/google-home/test", json={})
    assert test.status_code == 200, test.text
    v = client.post("/api/google-home/wizard/verify", json={"step": "connection_test", "patch": {}})
    assert v.status_code == 200, v.text
    assert v.json()["ok"] is True, v.json()


def test_confirm_billing_needs_user_when_unlinked(monkeypatch):
    plan = build_provision_plan(project_id="plex-cast-bridge", create_project=False)

    def fake_gcloud(args, check=False):
        cmd = " ".join(args)
        proc = MagicMock()
        if "services" in args and "enable" in args:
            proc.returncode = 0
            proc.stdout = ""
            proc.stderr = ""
            return proc
        if "billing" in args and "projects" in args and "describe" in args:
            proc.returncode = 0
            proc.stdout = json.dumps(
                {"billingEnabled": False, "billingAccountName": "", "projectId": "plex-cast-bridge"}
            )
            proc.stderr = ""
            return proc
        proc.returncode = 1
        proc.stdout = ""
        proc.stderr = f"unexpected: {cmd}"
        return proc

    monkeypatch.setattr("plex_cast_bridge.gcp_provision.ops._gcloud", fake_gcloud)
    result = run_provision_step(plan, "confirm_billing", dry_run=False)
    assert result["status"] == "needs_user"
    assert result["billing_enabled"] is False


def test_link_billing_requires_explicit_account(monkeypatch):
    plan = build_provision_plan(project_id="plex-cast-bridge", create_project=False)

    def fake_gcloud(args, check=False):
        proc = MagicMock()
        if "accounts" in args and "list" in args:
            proc.returncode = 0
            proc.stdout = json.dumps(
                [
                    {"name": "billingAccounts/CLOSED", "open": False},
                    {"name": "billingAccounts/OPEN123", "open": True, "displayName": "Default"},
                ]
            )
            proc.stderr = ""
            return proc
        proc.returncode = 1
        proc.stdout = ""
        proc.stderr = "unexpected"
        return proc

    monkeypatch.setattr("plex_cast_bridge.gcp_provision.ops._gcloud", fake_gcloud)
    needs = run_provision_step(plan, "link_billing", dry_run=False)
    assert needs["status"] == "needs_user"
    assert needs["billing_accounts"]

    plan.billing_account = "billingAccounts/OPEN123"

    def fake_gcloud_link(args, check=False):
        proc = MagicMock()
        if "accounts" in args and "list" in args:
            proc.returncode = 0
            proc.stdout = json.dumps(
                [{"name": "billingAccounts/OPEN123", "open": True, "displayName": "Default"}]
            )
            proc.stderr = ""
            return proc
        if "projects" in args and "link" in args:
            assert "--billing-account=billingAccounts/OPEN123" in args
            proc.returncode = 0
            proc.stdout = "{}"
            proc.stderr = ""
            return proc
        proc.returncode = 1
        proc.stdout = ""
        proc.stderr = "unexpected"
        return proc

    monkeypatch.setattr("plex_cast_bridge.gcp_provision.ops._gcloud", fake_gcloud_link)
    result = run_provision_step(plan, "link_billing", dry_run=False)
    assert result["status"] == "ok"
    assert result["billing_account"] == "billingAccounts/OPEN123"


def test_enable_apis_refuses_without_billing(monkeypatch):
    from plex_cast_bridge.gcp_provision import ops as ops_mod

    plan = build_provision_plan(project_id="plex-cast-bridge", create_project=False)
    original = ops_mod.run_provision_step

    def wrapper(plan, step, dry_run=True):
        if step == "confirm_billing":
            return {"step": step, "status": "needs_user", "detail": "no billing", "url": "https://x"}
        return original(plan, step, dry_run=dry_run)

    monkeypatch.setattr(ops_mod, "run_provision_step", wrapper)
    result = ops_mod.run_provision_step(plan, "enable_apis", dry_run=False)
    assert result["status"] == "failed"
    assert "billing" in result["detail"].lower()


def test_pairing_dashboard_forwards_approval_on_trusted_network(wizard_env, monkeypatch):

    calls = []
    ticket = 't' * 43
    def gateway(path, body=None):
        calls.append((path, body))
        return {'pending': {'ticket': ticket, 'code': '123456'}} if body is None else {'status': 'approved'}
    monkeypatch.setattr(catalog_sync, 'pairing_gateway_request', gateway)
    with TestClient(app) as browser:
        pending = browser.get('/api/google-home/pairing')
        assert pending.json()['pending']['code'] == '123456'
        result = browser.post('/api/google-home/pairing/approve',
                              json={'ticket': ticket, 'code': '123456'})
        assert result.json()['status'] == 'approved'
        assert calls[-1] == ('/internal/oauth/approve', {'ticket': ticket, 'code': '123456'})


def test_sync_response_never_exposes_gateway_key(wizard_env, monkeypatch):
    app.state.settings = Settings(plex_token=None, local_api_key=None, allow_unauthenticated_local_api=True)
    catalog_sync.save_sync_state({
        "cloud_gateway_url": "https://gateway.example",
        "catalog_write_key": "private-gateway-key",
        "last_push_status": "succeeded",
        "last_catalog_upload": "succeeded",
    })
    monkeypatch.setattr(catalog_sync, "push_catalog_now", lambda _force: catalog_sync.load_sync_state())
    response = TestClient(app).post("/api/google-home/sync")
    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    assert response.json()["last_catalog_upload"] == "succeeded"
    assert "private-gateway-key" not in response.text
    assert "catalog_write_key" not in response.json()


def test_gateway_configuration_does_not_return_secret(wizard_env):
    app.state.settings = Settings()
    response = TestClient(app).post('/api/google-home/gateway/configure', json={
        'cloud_gateway_url': 'https://gateway.example', 'catalog_write_key': 'private-key-value'
    })
    assert response.status_code == 200
    assert 'private-key-value' not in response.text
    assert catalog_sync.load_sync_state()['catalog_write_key'] == 'private-key-value'
