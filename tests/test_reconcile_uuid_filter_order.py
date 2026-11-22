"""Regression: invalid discovery rows must not consume UUID dedupe slots (main ordering)."""
from __future__ import annotations

import copy
import json

import pytest

from plex_cast_bridge.speaker_registry import migrate_speaker_record, reconcile_speakers

FIXED_NOW = "2026-07-01T15:00:00Z"
UUID = "aabbccddeeff0011"


def _full_snapshot(speakers: list[dict], summary: dict) -> str:
    """Stable JSON for differential comparisons (exclude volatile host-only if needed)."""
    rows = []
    for s in speakers:
        rows.append(
            {
                k: s.get(k)
                for k in (
                    "google_home_device_id",
                    "cast_uuid",
                    "speaker_name",
                    "room_hint",
                    "enabled",
                    "google_home_visible",
                    "removed",
                    "kind",
                    "availability",
                    "last_seen",
                )
            }
        )
    return json.dumps({"speakers": rows, "summary": summary}, sort_keys=True)


@pytest.mark.parametrize(
    "leading",
    [
        pytest.param({"name": "", "uuid": UUID}, id="empty_name"),
        pytest.param({"name": "   ", "uuid": UUID}, id="whitespace_name"),
        pytest.param({"name": "test speaker", "uuid": UUID}, id="test_speaker_literal"),
        pytest.param({"name": "  ", "uuid": UUID, "host": "1.2.3.4"}, id="whitespace_with_host"),
    ],
)
def test_invalid_row_before_valid_same_uuid_still_creates_speaker(leading):
    valid = {"name": "Kitchen", "uuid": UUID, "host": "10.0.0.1", "model": "Nest Mini"}
    speakers, summary = reconcile_speakers([], [leading, valid], now_iso=FIXED_NOW)
    assert summary["new"] == 1
    assert len(speakers) == 1
    assert speakers[0]["cast_uuid"] == UUID
    assert speakers[0]["speaker_name"] == "Kitchen"


def test_two_valid_same_uuid_only_one_created():
    a = {"name": "One", "uuid": UUID, "host": "1.1.1.1"}
    b = {"name": "Two", "uuid": UUID, "host": "1.1.1.2"}
    speakers, summary = reconcile_speakers([], [a, b], now_iso=FIXED_NOW)
    assert summary["new"] == 1
    assert len(speakers) == 1


def test_valid_then_invalid_same_uuid_keeps_speaker():
    valid = {"name": "Office", "uuid": UUID, "host": "2.2.2.2"}
    invalid = {"name": "", "uuid": UUID}
    speakers, summary = reconcile_speakers([], [valid, invalid], now_iso=FIXED_NOW)
    assert summary["new"] == 1
    assert speakers[0]["speaker_name"] == "Office"


def test_uuidless_invalid_then_valid():
    speakers, summary = reconcile_speakers(
        [],
        [{"name": "  "}, {"name": "Patio", "uuid": "1111222233334444", "host": "3.3.3.3"}],
        now_iso=FIXED_NOW,
    )
    assert summary["new"] == 1
    assert len(speakers) == 1


def test_identical_names_different_uuids_both_created():
    discovered = [
        {"name": "Speaker", "uuid": "1111111111111111"},
        {"name": "Speaker", "uuid": "2222222222222222"},
    ]
    speakers, summary = reconcile_speakers([], discovered, now_iso=FIXED_NOW)
    assert summary["new"] == 2
    assert len({s["cast_uuid"] for s in speakers}) == 2


def test_existing_identity_preserved_with_invalid_prefix_discovery():
    existing = [
        migrate_speaker_record(
            {
                "id": "plex-jukebox:Den",
                "speaker_name": "Den",
                "uuid": UUID,
                "google_home_visible": True,
                "enabled": True,
            }
        )
    ]
    discovered = [{"name": "", "uuid": UUID}, {"name": "Den", "uuid": UUID, "host": "9.9.9.9"}]
    before = copy.deepcopy(existing)
    speakers, summary = reconcile_speakers(before, discovered, now_iso=FIXED_NOW)
    assert summary.get("unchanged", 0) + summary.get("renamed", 0) == 1
    assert speakers[0]["google_home_device_id"] == "plex-jukebox:Den"
    assert _full_snapshot(speakers, summary)  # snapshot hook for future golden extension
