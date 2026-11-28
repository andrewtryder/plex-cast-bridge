"""Catalog load/save migration persistence (synthetic DATA_DIR only)."""
from __future__ import annotations

import json

from plex_cast_bridge import catalog_store


def test_load_catalog_assigns_and_persists_opaque_id(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog_store, "DATA_DIR", tmp_path)
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps(
            {
                "speakers": [
                    {
                        "speaker_name": "Garage",
                        "roomHint": "Garage",
                        "enabled": True,
                    }
                ]
            }
        )
    )
    first = catalog_store.load_catalog_data()
    first_id = first["speakers"][0]["google_home_device_id"]
    assert first_id.startswith("plex-jukebox:")
    on_disk = json.loads(path.read_text())
    assert on_disk["speakers"][0]["google_home_device_id"] == first_id
    second = catalog_store.load_catalog_data()
    assert second["speakers"][0]["google_home_device_id"] == first_id


def test_in_memory_migrate_without_persist_can_differ():
    """Ephemeral migrate_speaker_record without id/uuid is unstable until catalog save."""
    from unittest.mock import patch

    from plex_cast_bridge.speaker_registry import migrate_speaker_record

    raw = {"speaker_name": "Loft", "roomHint": "Loft"}
    with patch("plex_cast_bridge.speaker_registry.secrets.token_hex", side_effect=["aaa", "bbb"]):
        a = migrate_speaker_record(raw)
        b = migrate_speaker_record(raw)
    assert a["google_home_device_id"] != b["google_home_device_id"]


def test_voice_cold_start_defaults_off_and_survives_rename(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog_store, "DATA_DIR", tmp_path)
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps(
            {
                "speakers": [
                    {
                        "speaker_name": "Bedroom",
                        "google_home_device_id": "plex-jukebox:bedroom",
                        "cast_uuid": "uuid-1",
                        "enabled": True,
                        "google_home_visible": True,
                    }
                ]
            }
        )
    )
    migrated = catalog_store.list_speaker_records()[0]
    assert migrated["voice_cold_start_enabled"] is False
    catalog_store.update_speaker_record(
        "plex-jukebox:bedroom",
        {
            "voice_cold_start_enabled": True,
            "voice_cold_start_selection": "artist",
            "voice_cold_start_artist": "DJ Shadow",
            "google_home_name": "Main Bedroom Plex",
        },
    )
    # A new process/read sees the same per-device setting after a friendly-name change.
    reloaded = catalog_store.list_speaker_records()[0]
    assert reloaded["google_home_device_id"] == "plex-jukebox:bedroom"
    assert reloaded["voice_cold_start_enabled"] is True
    assert reloaded["voice_cold_start_selection"] == "artist"
    assert reloaded["voice_cold_start_artist"] == "DJ Shadow"
    class CastManager:
        @staticmethod
        def is_disabled(_speaker):
            return False

    catalog_store.persist_discovered_speakers_to_catalog(
        [{"name": "Bedroom", "room": "Bedroom", "uuid": "uuid-1", "host": "192.0.2.4"}],
        CastManager(),
    )
    rediscovered = catalog_store.list_speaker_records()[0]
    assert rediscovered["google_home_device_id"] == "plex-jukebox:bedroom"
    assert rediscovered["voice_cold_start_enabled"] is True
    assert rediscovered["voice_cold_start_artist"] == "DJ Shadow"


def test_local_voice_policy_is_not_copied_to_cloud_run_catalog(tmp_path, monkeypatch):
    source = tmp_path / "local.json"
    target_dir = tmp_path / "gateway"
    target_dir.mkdir()
    target = target_dir / "catalog.json"
    source.write_text(json.dumps({
        "speakers": [{"google_home_device_id": "plex-jukebox:room"}],
        "voice_cold_start": {"plex-jukebox:room": {"artist": "Private Artist"}},
    }))
    monkeypatch.setattr(catalog_store, "CLOUD_RUN_CATALOG", target)
    catalog_store._mirror_cloud_run_catalog(source)
    copied = json.loads(target.read_text())
    assert "voice_cold_start" not in copied
    assert copied["speakers"][0]["google_home_device_id"] == "plex-jukebox:room"
