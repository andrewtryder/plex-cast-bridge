"""Frozen-catalog differential snapshots for reconcile_speakers (synthetic only)."""
from __future__ import annotations

import copy

from plex_cast_bridge.speaker_registry import (
    google_home_sync_fingerprint,
    migrate_speaker_record,
    reconcile_speakers,
)

FIXED_NOW = "2026-06-01T12:00:00Z"


def _snap(speakers: list[dict]) -> list[dict]:
    return [
        {
            "google_home_device_id": s.get("google_home_device_id"),
            "cast_uuid": s.get("cast_uuid"),
            "speaker_name": s.get("speaker_name"),
            "room_hint": s.get("room_hint"),
            "enabled": s.get("enabled"),
            "google_home_visible": s.get("google_home_visible"),
            "removed": s.get("removed"),
            "kind": s.get("kind"),
            "nicknames": list(s.get("nicknames") or []),
        }
        for s in speakers
    ]


def test_twice_reconcile_preserves_identity_and_user_prefs():
    existing = [
        migrate_speaker_record(
            {
                "id": "plex-jukebox:Kitchen",
                "speaker_name": "Kitchen Hub",
                "room_hint": "Custom Kitchen",
                "uuid": "abcd1234abcd1234",
                "enabled": True,
                "google_home_visible": True,
                "nicknames": ["Custom Kitchen Plex", "Hey Kitchen"],
            }
        )
    ]
    existing[0]["enabled"] = False
    disc = [{"name": "Kitchen Hub", "uuid": "abcd1234abcd1234", "host": "10.0.0.8", "model": "Nest Hub"}]
    first, _ = reconcile_speakers(existing, disc, now_iso=FIXED_NOW)
    second, summary = reconcile_speakers(first, disc, now_iso=FIXED_NOW)
    assert _snap(first) == _snap(second)
    assert summary["unchanged"] == 1
    assert second[0]["enabled"] is False
    assert second[0]["google_home_visible"] is True
    assert first[0]["google_home_device_id"] == "plex-jukebox:Kitchen"
    assert google_home_sync_fingerprint(first) == google_home_sync_fingerprint(second)


def test_new_speaker_hidden_by_default_snapshot():
    speakers, summary = reconcile_speakers(
        [],
        [{"name": "Sunroom", "uuid": "1111222233334444", "host": "1.2.3.4", "model": "Nest Mini"}],
        now_iso=FIXED_NOW,
    )
    assert summary["new"] == 1
    snap = _snap(speakers)
    assert snap == [
        {
            "google_home_device_id": "plex-jukebox:cast-1111222233334444",
            "cast_uuid": "1111222233334444",
            "speaker_name": "Sunroom",
            "room_hint": "Sunroom",
            "enabled": True,
            "google_home_visible": False,
            "removed": False,
            "kind": "speaker",
            "nicknames": ["Sunroom Plex"],
        }
    ]


def test_reconcile_result_matches_migrate_expectations():
    speakers, _ = reconcile_speakers(
        [],
        [{"name": "Office", "room": "Office", "uuid": "abcdabcdabcdabcd", "host": "9.9.9.9"}],
        now_iso=FIXED_NOW,
    )
    trimmed = copy.deepcopy(speakers[0])
    for key in ("host", "last_seen", "port", "model", "availability", "name", "id", "roomHint"):
        trimmed.pop(key, None)
    expected = migrate_speaker_record(
        {
            "speaker_name": "Office",
            "room_hint": "Office",
            "cast_uuid": "abcdabcdabcdabcd",
            "google_home_visible": False,
            "enabled": True,
            "last_seen": FIXED_NOW,
            "availability": "online",
        },
        previously_exposed=False,
    )
    for key in ("host", "last_seen", "port", "model", "availability", "name", "id", "roomHint"):
        expected.pop(key, None)
    assert trimmed == expected
