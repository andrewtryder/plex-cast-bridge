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
