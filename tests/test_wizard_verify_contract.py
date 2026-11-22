"""Contract tests for wizard verify response shapes and per-step dispatch."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from plex_cast_bridge import catalog_sync, wizard_verify
from plex_cast_bridge import google_home_setup as setup

RESPONSE_KEYS = frozenset({"step", "ok", "checks", "setup"})
CHECK_KEYS = frozenset({"name", "ok", "detail", "recovery"})


@pytest.fixture
def wizard_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("DATA_DIR", str(data_dir))
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


def _app(plex=object(), speakers=None):
    speakers = speakers if speakers is not None else [{"name": "Kitchen"}]
    cast = SimpleNamespace(last_discovered=speakers)
    return SimpleNamespace(plex=plex, cast=cast)


def _assert_verify_contract(body: dict, step: str) -> None:
    assert set(body) == RESPONSE_KEYS
    assert body["step"] == step
    assert isinstance(body["ok"], bool)
    assert isinstance(body["checks"], list)
    assert isinstance(body["setup"], dict)
    for item in body["checks"]:
        assert set(item) == CHECK_KEYS
        assert isinstance(item["name"], str)
        assert isinstance(item["ok"], bool)
        assert isinstance(item["detail"], str)
        assert item["recovery"] is None or isinstance(item["recovery"], str)


def test_verify_response_contract_welcome(wizard_env):
    result = wizard_verify.verify_step("welcome", _app())
    _assert_verify_contract(result, "welcome")
    assert any(c["name"] == "Plex library" for c in result["checks"])


def test_verify_response_contract_cloud_auth(wizard_env):
    result = wizard_verify.verify_step("cloud_auth", _app(), patch={"mode": "attach_existing"})
    _assert_verify_contract(result, "cloud_auth")


def test_verify_response_contract_prepare_services(wizard_env):
    setup.save_setup({"project_id": "p", "provision_progress": {}})
    result = wizard_verify.verify_step("prepare_services", _app())
    _assert_verify_contract(result, "prepare_services")


def test_verify_response_contract_subscriber_auth(wizard_env):
    result = wizard_verify.verify_step("subscriber_auth", _app())
    _assert_verify_contract(result, "subscriber_auth")


def test_verify_response_contract_deploy_gateway(wizard_env):
    setup.save_setup({"cloud_run_url": ""})
    result = wizard_verify.verify_step("deploy_gateway", _app())
    _assert_verify_contract(result, "deploy_gateway")


def test_verify_response_contract_developer_console(wizard_env):
    setup.save_setup({"oauth": {}, "developer_console_done": False})
    result = wizard_verify.verify_step("developer_console", _app())
    _assert_verify_contract(result, "developer_console")


def test_verify_response_contract_link_home(wizard_env):
    result = wizard_verify.verify_step("link_home", _app(), patch={"link_confirmed": True})
    _assert_verify_contract(result, "link_home")


def test_verify_response_contract_connection_test(wizard_env):
    setup.save_setup({"last_test": {}})
    result = wizard_verify.verify_step("connection_test", _app())
    _assert_verify_contract(result, "connection_test")


def test_verify_unknown_step(wizard_env):
    result = wizard_verify.verify_step("not_a_real_step", _app())
    _assert_verify_contract(result, "not_a_real_step")
    assert result["ok"] is False
    assert result["checks"] == [
        {"name": "step", "ok": False, "detail": "Unknown step not_a_real_step", "recovery": None}
    ]
    assert setup.load_setup().get("verified_not_a_real_step") is False


def test_patch_filters_secrets_from_persisted_setup(wizard_env):
    wizard_verify.verify_step(
        "subscriber_auth",
        _app(),
        patch={
            "catalog_write_key": "super-secret-key",
            "client_secret": "oauth-secret",
            "billing_confirmed": True,
        },
    )
    raw = setup._path().read_text()
    assert "super-secret-key" not in raw
    assert "oauth-secret" not in raw
    assert setup.load_setup().get("billing_confirmed") is True
    assert catalog_sync.load_sync_state().get("catalog_write_key") == "super-secret-key"
