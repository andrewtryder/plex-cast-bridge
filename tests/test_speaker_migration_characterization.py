"""Migration and identity churn characterization (Phase 10 prep, no production writes)."""
from __future__ import annotations

from unittest.mock import patch

from plex_cast_bridge.speaker_registry import (
    migrate_catalog_speakers,
    migrate_speaker_record,
    new_google_home_device_id,
    reconcile_speakers,
)


FIXED_NOW = "2026-01-01T00:00:00Z"


def test_new_google_home_device_id_uses_cast_uuid_token():
    assert new_google_home_device_id("abcd-1234-efGH") == "plex-jukebox:cast-abcd1234efgh"


def test_new_google_home_device_id_random_when_uuid_short():
    with patch("plex_cast_bridge.speaker_registry.secrets.token_hex", return_value="cafebabe"):
        assert new_google_home_device_id("ab") == "plex-jukebox:id-cafebabe"


def test_migrate_without_uuid_assigns_stable_random_per_call():
    with patch("plex_cast_bridge.speaker_registry.secrets.token_hex", side_effect=["aaaa", "bbbb"]):
        a = migrate_speaker_record({"speaker_name": "Den", "roomHint": "Den"})
        b = migrate_speaker_record({"speaker_name": "Den", "roomHint": "Den"})
    assert a["google_home_device_id"] != b["google_home_device_id"]


def test_repeated_catalog_migration_idempotent():
    catalog = {
        "speakers": [
            {
                "id": "plex-jukebox:Kitchen",
                "speaker_name": "Kitchen Hub",
                "roomHint": "Kitchen",
                "uuid": "1111111111111111",
                "google_home_visible": True,
                "nicknames": ["Custom Kitchen"],
            }
        ],
        "inputs": ["Kitchen"],
    }
    first, changed1 = migrate_catalog_speakers(catalog)
    second, changed2 = migrate_catalog_speakers(first)
    assert changed1 in (True, False)
    assert changed2 is False
    assert first == second
    speaker = second["speakers"][0]
    assert speaker["google_home_device_id"] == "plex-jukebox:Kitchen"
    assert "Custom Kitchen" in speaker["nicknames"]


def test_migrate_hidden_device_stays_hidden():
    rec = migrate_speaker_record(
        {
            "id": "plex-jukebox:Bedroom",
            "speaker_name": "Bedroom",
            "google_home_visible": False,
            "enabled": True,
        },
        previously_exposed=False,
    )
    assert rec["google_home_visible"] is False


def test_migrate_removed_device_preserved():
    rec = migrate_speaker_record(
        {"id": "plex-jukebox:Attic", "speaker_name": "Attic", "removed": True, "enabled": False},
    )
    assert rec["removed"] is True


def test_reconcile_preserves_manual_room_hint():
    base = migrate_speaker_record(
        {
            "id": "plex-jukebox:Kitchen",
            "speaker_name": "Old name",
            "room_hint": "Custom Room",
            "uuid": "aaaa1111bbbb2222",
        }
    )
    speakers, _ = reconcile_speakers(
        [base],
        [{"name": "New Hub", "uuid": "aaaa1111bbbb2222", "host": "9.9.9.9", "room": "Kitchen"}],
        now_iso=FIXED_NOW,
    )
    assert speakers[0]["room_hint"] == "Custom Room"


def test_empty_discovery_name_skipped():
    speakers, summary = reconcile_speakers(
        [],
        [
            {"name": "   ", "uuid": "1111222233334444"},
            {"name": "Valid", "uuid": "5555666677778888", "host": "1.1.1.1"},
        ],
        now_iso=FIXED_NOW,
    )
    assert summary["new"] == 1
    assert len(speakers) == 1
    assert speakers[0]["speaker_name"] == "Valid"


def test_offline_speaker_returns_online():
    existing = [
        migrate_speaker_record(
            {
                "id": "plex-jukebox:Patio",
                "speaker_name": "Patio",
                "uuid": "abcdabcdabcdabcd",
                "availability": "offline",
            }
        )
    ]
    existing[0]["availability"] = "offline"
    speakers, summary = reconcile_speakers(
        existing,
        [{"name": "Patio", "uuid": "abcdabcdabcdabcd", "host": "3.3.3.3"}],
        now_iso=FIXED_NOW,
    )
    assert summary["returned"] == 1
    assert speakers[0]["availability"] == "online"
    assert speakers[0]["last_seen"] == FIXED_NOW


def test_duplicate_existing_virtual_ids_get_new_id_and_review():
    raw = {
        "speakers": [
            {"id": "plex-jukebox:Same", "speaker_name": "One", "uuid": "1111111111111111"},
            {"id": "plex-jukebox:Same", "speaker_name": "Two", "uuid": "2222222222222222"},
        ]
    }
    migrated, changed = migrate_catalog_speakers(raw)
    assert changed
    ids = [s["google_home_device_id"] for s in migrated["speakers"]]
    assert len(set(ids)) == 2
    assert all(s["needs_review"] for s in migrated["speakers"])
