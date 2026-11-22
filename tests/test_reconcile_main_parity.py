"""Byte-level parity cases against main-branch reconcile semantics (frozen fixtures)."""
from __future__ import annotations

import json
from pathlib import Path

from plex_cast_bridge.speaker_registry import reconcile_speakers

FIXED_NOW = "2026-07-01T15:00:00Z"
_GOLDEN = Path(__file__).resolve().parent / "fixtures" / "reconcile_main_golden.json"


def _norm_output(speakers: list[dict], summary: dict) -> dict:
    rows = []
    for s in speakers:
        rows.append(
            {
                k: s.get(k)
                for k in sorted(s)
                if k not in ("name", "id", "roomHint", "google_home_device_id")
            }
        )
    return {"speakers": rows, "summary": summary}


def test_reconcile_matches_main_golden_fixtures():
    """Golden captured from main @ ee152d7 with fixed now_iso and mocked paths."""
    cases = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    for case in cases:
        speakers, summary = reconcile_speakers(
            case["existing"],
            case["discovered"],
            now_iso=FIXED_NOW,
        )
        got = _norm_output(speakers, summary)
        assert got == case["expected"], case["id"]
