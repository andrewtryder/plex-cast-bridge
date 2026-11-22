"""Parametric differential fixtures for reconcile_speakers (locked semantics)."""
from __future__ import annotations

import copy

import pytest

from plex_cast_bridge.speaker_registry import migrate_speaker_record, reconcile_speakers

FIXED_NOW = "2026-07-01T15:00:00Z"


def _semantic_rows(speakers: list[dict]) -> list[dict]:
    return [
        {
            "google_home_device_id": s["google_home_device_id"],
            "cast_uuid": s.get("cast_uuid"),
            "speaker_name": s.get("speaker_name"),
            "enabled": s.get("enabled"),
            "google_home_visible": s.get("google_home_visible"),
            "removed": s.get("removed"),
            "kind": s.get("kind"),
        }
        for s in speakers
    ]


DIFF_CASES = [
    pytest.param([], [], {"new": 0, "total": 0}, id="empty"),
    pytest.param(
        [],
        [{"name": "Den", "uuid": "1111222233334444", "host": "1.2.3.4"}],
        {"new": 1, "total": 1},
        id="single_new_hidden",
    ),
    pytest.param(
        [migrate_speaker_record({"id": "plex-jukebox:Den", "speaker_name": "Den", "uuid": "aaaabbbbccccdddd"})],
        [{"name": "Den display", "uuid": "aaaabbbbccccdddd", "host": "9.9.9.9"}],
        {"renamed": 1, "total": 1},
        id="uuid_match_rename",
    ),
    pytest.param(
        [],
        [
            {"name": "Speaker", "uuid": "0123456789abcdef1111", "host": "1.1.1.1"},
            {"name": "Speaker", "uuid": "0123456789abcdef2222", "host": "1.1.1.2"},
        ],
        {"new": 2, "total": 2},
        id="duplicate_names_distinct_uuid",
    ),
]


@pytest.mark.parametrize("existing_raw,discovered,expected_counts", DIFF_CASES)
def test_reconcile_parametric_summary(existing_raw, discovered, expected_counts):
    existing = copy.deepcopy(existing_raw)
    speakers, summary = reconcile_speakers(existing, discovered, now_iso=FIXED_NOW)
    for key, value in expected_counts.items():
        assert summary[key] == value, f"{key}: {summary[key]} != {value}"
    # idempotence on same input
    again, summary2 = reconcile_speakers(speakers, discovered, now_iso=FIXED_NOW)
    assert _semantic_rows(again) == _semantic_rows(speakers)
    assert summary2.get("unchanged", 0) >= 1 or summary2["new"] == 0


def test_discovery_order_permutations_stable_ids():
    a = {"name": "Alpha", "uuid": "aaaaaaaaaaaaaaaa", "host": "1.0.0.1"}
    b = {"name": "Beta", "uuid": "bbbbbbbbbbbbbbbb", "host": "1.0.0.2"}
    ab, _ = reconcile_speakers([], [a, b], now_iso=FIXED_NOW)
    ba, _ = reconcile_speakers([], [b, a], now_iso=FIXED_NOW)
    by_uuid_ab = {s["cast_uuid"]: s["google_home_device_id"] for s in ab}
    by_uuid_ba = {s["cast_uuid"]: s["google_home_device_id"] for s in ba}
    assert by_uuid_ab == by_uuid_ba
