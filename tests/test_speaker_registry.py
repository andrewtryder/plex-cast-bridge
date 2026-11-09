"""Stable speaker identity, migration, and reconciliation."""

from __future__ import annotations

from plex_cast_bridge.speaker_registry import (
    find_by_google_home_id,
    google_home_sync_fingerprint,
    is_google_home_exposed,
    migrate_speaker_record,
    reconcile_speakers,
)


def test_migrate_preserves_legacy_room_device_id():
    rec = migrate_speaker_record(
        {
            "id": "plex-jukebox:Kitchen",
            "name": "Plex",
            "speaker_name": "Kitchen display",
            "roomHint": "Kitchen",
            "enabled": True,
            "uuid": "aabbccddeeff0011",
        }
    )
    assert rec["google_home_device_id"] == "plex-jukebox:Kitchen"
    assert rec["google_home_name"] == "Kitchen Plex"
    assert rec["google_home_visible"] is True
    assert rec["cast_uuid"] == "aabbccddeeff0011"
    assert "Plex" not in rec["nicknames"] or rec["nicknames"][0] == "Kitchen Plex"


def test_new_devices_default_hidden_from_google_home():
    speakers, summary = reconcile_speakers(
        [],
        [{"name": "Bedroom Mini", "room": "Bedroom", "uuid": "1111222233334444", "host": "1.2.3.4", "model": "Google Nest Mini"}],
    )
    assert summary["new"] == 1
    assert speakers[0]["google_home_visible"] is False
    assert speakers[0]["enabled"] is True
    assert speakers[0]["google_home_device_id"].startswith("plex-jukebox:cast-")


def test_rename_preserves_device_id():
    existing = [
        migrate_speaker_record(
            {
                "id": "plex-jukebox:Kitchen",
                "speaker_name": "Kitchen display",
                "roomHint": "Kitchen",
                "uuid": "abcd1234abcd1234",
                "enabled": True,
                "google_home_visible": True,
            }
        )
    ]
    speakers, summary = reconcile_speakers(
        existing,
        [{"name": "Kitchen Hub", "room": "Kitchen", "uuid": "abcd1234abcd1234", "host": "10.0.0.2", "model": "Google Nest Hub"}],
    )
    assert summary["renamed"] == 1
    assert len(speakers) == 1
    assert speakers[0]["google_home_device_id"] == "plex-jukebox:Kitchen"
    assert speakers[0]["speaker_name"] == "Kitchen Hub"
    assert speakers[0]["host"] == "10.0.0.2"


def test_ip_change_same_uuid_no_duplicate():
    existing = [
        migrate_speaker_record(
            {
                "id": "plex-jukebox:cast-aaaaaaaaaaaaaaaa",
                "speaker_name": "Office speaker",
                "room_hint": "Office",
                "cast_uuid": "aaaaaaaaaaaaaaaa",
                "host": "192.168.1.10",
                "enabled": True,
                "google_home_visible": False,
            }
        )
    ]
    speakers, summary = reconcile_speakers(
        existing,
        [{"name": "Office speaker", "room": "Office", "uuid": "aaaaaaaaaaaaaaaa", "host": "192.168.1.99", "model": "Google Nest Mini"}],
    )
    assert summary["unchanged"] == 1
    assert len(speakers) == 1
    assert speakers[0]["host"] == "192.168.1.99"


def test_offline_preserved_and_not_deleted():
    existing = [
        migrate_speaker_record(
            {
                "id": "plex-jukebox:Living Room",
                "speaker_name": "Living Room speaker",
                "roomHint": "Living Room",
                "enabled": True,
                "google_home_visible": True,
            }
        )
    ]
    speakers, summary = reconcile_speakers(existing, [])
    assert summary["offline"] == 1
    assert speakers[0]["availability"] == "offline"
    assert speakers[0]["google_home_visible"] is True


def test_same_room_two_devices_unique_ids():
    speakers, summary = reconcile_speakers(
        [],
        [
            {"name": "Kitchen display", "room": "Kitchen", "uuid": "aaaabbbbccccdddd", "host": "1.1.1.1", "model": "Nest Hub"},
            {"name": "Kitchen speaker", "room": "Kitchen", "uuid": "eeeeffff00001111", "host": "1.1.1.2", "model": "Nest Mini"},
        ],
    )
    assert summary["new"] == 2
    ids = {s["google_home_device_id"] for s in speakers}
    assert len(ids) == 2


def test_visibility_independent_of_enabled():
    rec = migrate_speaker_record(
        {
            "id": "plex-jukebox:Bedroom",
            "speaker_name": "Bedroom Mini",
            "roomHint": "Bedroom",
            "enabled": True,
            "google_home_visible": False,
        },
        previously_exposed=False,
    )
    assert rec["enabled"] is True
    assert is_google_home_exposed(rec) is False


def test_fingerprint_ignores_host_and_last_seen():
    a = migrate_speaker_record(
        {
            "id": "plex-jukebox:Kitchen",
            "speaker_name": "Kitchen display",
            "roomHint": "Kitchen",
            "enabled": True,
            "google_home_visible": True,
            "host": "1.1.1.1",
            "last_seen": "t1",
        }
    )
    b = dict(a)
    b["host"] = "9.9.9.9"
    b["last_seen"] = "t2"
    assert google_home_sync_fingerprint([a]) == google_home_sync_fingerprint([b])


def test_find_by_google_home_id():
    speakers = [
        migrate_speaker_record(
            {"id": "plex-jukebox:Kitchen", "speaker_name": "Kitchen display", "roomHint": "Kitchen", "enabled": True}
        )
    ]
    assert find_by_google_home_id(speakers, "plex-jukebox:Kitchen")["speaker_name"] == "Kitchen display"
    assert find_by_google_home_id(speakers, "missing") is None


def test_cast_group_kind():
    speakers, summary = reconcile_speakers(
        [],
        [{"name": "Whole House", "room": "Whole House", "uuid": "groupuuid00000001", "host": "1.2.3.4", "model": "Google Cast Group"}],
    )
    assert summary["new"] == 1
    assert speakers[0]["kind"] == "group"
    assert speakers[0]["google_home_visible"] is False


def test_four_distinct_speakers_stable_ids():
    discovered = [
        {"name": f"Speaker {i}", "room": f"Room {i}", "uuid": f"{i:016x}", "host": f"10.0.0.{i}", "model": "Nest Mini"}
        for i in range(1, 5)
    ]
    speakers, summary = reconcile_speakers([], discovered)
    assert summary["new"] == 4
    assert len({s["google_home_device_id"] for s in speakers}) == 4


def test_explicit_removal_not_in_fingerprint():
    rec = migrate_speaker_record(
        {
            "id": "plex-jukebox:Kitchen",
            "speaker_name": "Kitchen display",
            "roomHint": "Kitchen",
            "enabled": True,
            "google_home_visible": True,
            "removed": True,
        }
    )
    assert is_google_home_exposed(rec) is False
    assert google_home_sync_fingerprint([rec]) == ""


def test_visible_disabled_still_exposed_for_sync_fingerprint():
    rec = migrate_speaker_record(
        {
            "id": "plex-jukebox:Kitchen",
            "speaker_name": "Kitchen display",
            "roomHint": "Kitchen",
            "enabled": False,
            "google_home_visible": True,
        }
    )
    assert is_google_home_exposed(rec) is True
    assert "plex-jukebox:Kitchen" in google_home_sync_fingerprint([rec])
