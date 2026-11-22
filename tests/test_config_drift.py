import json

import pytest

from plex_cast_bridge import config_drift as drift


def test_drift_reports_overwrite_and_redacts_without_writes(tmp_path):
    secret = "highly-private-write-key"
    state = {"cloud_gateway_url": "https://saved.example", "catalog_write_key": secret}
    path = tmp_path / "google_home_sync.json"
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    result = drift.diagnose({"CLOUD_GATEWAY_URL": "https://env.example", "CATALOG_WRITE_KEY": "different-secret"}, {}, tmp_path)
    assert result["settings"]["cloud_gateway_url"]["conflict"]
    assert result["settings"]["catalog_write_key"]["conflict"]
    assert "copied to sync file" in result["settings"]["catalog_write_key"]["source"]
    assert secret not in json.dumps(result)
    assert "different-secret" not in json.dumps(result)
    assert path.read_bytes() == before
    assert len(list(tmp_path.iterdir())) == 1


def test_partial_gateway_pair_and_numeric_volumes(tmp_path):
    (tmp_path / "google_home_sync.json").write_text('{"cloud_gateway_url":"https://saved"}')
    (tmp_path / "app_settings.json").write_text('{"default_cast_volume":0.4}')
    result = drift.diagnose({"CLOUD_GATEWAY_URL": "https://other", "DEFAULT_CAST_VOLUME": "0.40"}, {}, tmp_path)
    assert result["settings"]["cloud_gateway_url"]["source"] == "google_home_sync.json"
    assert result["settings"]["cloud_gateway_url"]["conflict"]
    assert not result["settings"]["default_cast_volume"]["conflict"]
    assert result["settings"]["default_cast_volume"]["source"] == "app_settings.json"


def test_explicit_volume_keys_and_plex_compatibility(tmp_path):
    (tmp_path / "app_settings.json").write_text('{"night_cast_volume":0.2}')
    (tmp_path / "plex_connection.json").write_text('{"mode":"jwt","jwt_token":"jwt-secret"}')
    result = drift.diagnose({"DEFAULT_CAST_VOLUME": "0.8", "PLEX_TOKEN": "legacy-secret"}, {}, tmp_path)
    assert result["settings"]["default_cast_volume"]["source"] == "app_settings.json (normalized default)"
    assert not result["settings"]["default_cast_volume"]["conflict"]
    assert not result["settings"]["plex_token"]["conflict"]
    assert result["release_preflight_warnings"] == []


def test_gateway_warning_uses_effective_environment_only(tmp_path):
    (tmp_path / "google_home_sync.json").write_text('{"catalog_write_key":"saved"}')
    result = drift.diagnose({"CATALOG_WRITE_KEY": "saved"}, {"CATALOG_WRITE_KEY": "stale"}, tmp_path)
    assert result["release_preflight_warnings"] == []
    result = drift.diagnose({"CATALOG_WRITE_KEY": "different"}, {}, tmp_path)
    assert result["release_preflight_warnings"] == []
    assert result["settings"]["catalog_write_key"]["classification"] == "informational"


def test_invalid_existing_catalog_is_selected(tmp_path):
    (tmp_path / "catalog.json").write_text("not json")
    result = drift.diagnose({}, {}, tmp_path)
    assert result["catalog_source"] == "data catalog"
    assert result["files"]["catalog.json"] == "unreadable or invalid"


@pytest.mark.parametrize(
    ("environ", "app_content", "expect_conflict", "expect_classification"),
    [
        ({}, '{"default_cast_volume":0.4}', False, "ok"),
        ({"DEFAULT_CAST_VOLUME": "0.50"}, '{"default_cast_volume":0.4}', True, "override"),
        ({"DEFAULT_CAST_VOLUME": "0.40"}, '{"default_cast_volume":0.4}', False, "ok"),
        ({"DEFAULT_CAST_VOLUME": "not-a-number"}, '{"default_cast_volume":0.4}', False, "unknown"),
    ],
)
def test_volume_provenance_conflicts(tmp_path, environ, app_content, expect_conflict, expect_classification):
    (tmp_path / "app_settings.json").write_text(app_content)
    result = drift.diagnose(environ, {}, tmp_path)
    entry = result["settings"]["default_cast_volume"]
    assert entry["conflict"] is expect_conflict
    assert entry["classification"] == expect_classification


def test_absent_catalog_file_classification(tmp_path):
    result = drift.diagnose({}, {}, tmp_path)
    assert result["files"]["catalog.json"] == "absent"


@pytest.mark.parametrize(
    ("content", "expect_state"),
    [
        ("[]", "invalid"),
        ("{", "unreadable or invalid"),
    ],
)
def test_malformed_catalog_file_classification(tmp_path, content, expect_state):
    (tmp_path / "catalog.json").write_text(content)
    result = drift.diagnose({}, {}, tmp_path)
    assert result["files"]["catalog.json"] == expect_state


@pytest.mark.parametrize(
    ("sync", "environ", "warnings"),
    [
        ({}, {"CLOUD_GATEWAY_URL": "https://a", "CATALOG_WRITE_KEY": "k"}, []),
        (
            {"cloud_gateway_url": "https://saved", "catalog_write_key": "saved"},
            {"CLOUD_GATEWAY_URL": "https://other", "CATALOG_WRITE_KEY": "saved"},
            ["cloud_gateway_url"],
        ),
    ],
)
def test_gateway_release_preflight_warnings(tmp_path, sync, environ, warnings):
    (tmp_path / "google_home_sync.json").write_text(json.dumps(sync))
    result = drift.diagnose(environ, {}, tmp_path)
    assert result["release_preflight_warnings"] == warnings
