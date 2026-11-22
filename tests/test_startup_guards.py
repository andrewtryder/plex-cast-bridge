
import pytest

from plex_cast_bridge.startup_guards import (
    ensure_persistent_data_mount,
    ensure_pubsub_credentials,
    has_existing_configuration,
    require_existing_configuration,
)


def test_has_existing_configuration_detects_catalog(tmp_path):
    (tmp_path / "catalog.json").write_text("{}")
    assert has_existing_configuration(tmp_path)


def test_require_existing_configuration_refuses_empty_mount(tmp_path, monkeypatch):
    monkeypatch.setenv("PCB_REQUIRE_EXISTING_DATA", "1")
    with pytest.raises(RuntimeError, match="does not contain existing"):
        require_existing_configuration(tmp_path)


def test_require_existing_configuration_allows_marked_data(tmp_path, monkeypatch):
    monkeypatch.setenv("PCB_UPGRADE", "true")
    (tmp_path / "plex_connection.json").write_text("{}")
    require_existing_configuration(tmp_path)


def test_ensure_persistent_data_mount_skips_non_data_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKER_CONTAINER", "1")
    monkeypatch.setattr("plex_cast_bridge.startup_guards.os.path.ismount", lambda _p: False)
    ensure_persistent_data_mount(tmp_path)


def test_ensure_pubsub_credentials_ignores_when_disabled():
    ensure_pubsub_credentials(False, None)


def test_ensure_pubsub_credentials_rejects_empty_file(tmp_path):
    creds = tmp_path / "empty.json"
    creds.write_text("")
    with pytest.raises(RuntimeError, match="GOOGLE_APPLICATION_CREDENTIALS"):
        ensure_pubsub_credentials(True, str(creds))
