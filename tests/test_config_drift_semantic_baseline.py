"""Ensure refactored config_drift.diagnose matches pre-refactor main outputs."""
from __future__ import annotations

import json
from pathlib import Path

import sys

from plex_cast_bridge import config_drift as drift

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from release_acceptance.docker import _valid_drift

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "config_drift_main_baseline.json"
BASELINE = json.loads(FIXTURES.read_text(encoding="utf-8"))

_SCENARIO_ROOT = {
    "gateway_overwrite": {
        "files": {"google_home_sync.json": json.dumps({
            "cloud_gateway_url": "https://saved.example",
            "catalog_write_key": "secret",
        })},
        "env": {"CLOUD_GATEWAY_URL": "https://env.example", "CATALOG_WRITE_KEY": "different"},
        "dotenv": {},
    },
    "partial_gateway_volumes": {
        "files": {
            "google_home_sync.json": '{"cloud_gateway_url":"https://saved"}',
            "app_settings.json": '{"default_cast_volume":0.4}',
        },
        "env": {"CLOUD_GATEWAY_URL": "https://other", "DEFAULT_CAST_VOLUME": "0.40"},
        "dotenv": {},
    },
    "plex_jwt_volumes": {
        "files": {
            "plex_connection.json": json.dumps({
                "mode": "jwt",
                "jwt_token": "jwt",
                "server_url": "http://plex",
                "music_section": "Music",
            }),
            "app_settings.json": '{"night_cast_volume":0.2}',
        },
        "env": {"DEFAULT_CAST_VOLUME": "0.8", "PLEX_TOKEN": "legacy-secret"},
        "dotenv": {},
    },
    "gateway_warning_pair": {
        "files": {"google_home_sync.json": json.dumps({
            "cloud_gateway_url": "https://saved",
            "catalog_write_key": "saved-key",
        })},
        "env": {"CLOUD_GATEWAY_URL": "https://other", "CATALOG_WRITE_KEY": "saved-key"},
        "dotenv": {},
    },
    "empty_data": {"files": {}, "env": {}, "dotenv": {}},
    "invalid_catalog": {"files": {"catalog.json": "not json"}, "env": {}, "dotenv": {}},
}


def _materialize(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    for file_name, body in _SCENARIO_ROOT[name]["files"].items():
        (root / file_name).write_text(body)
    return root


def test_diagnose_matches_main_baseline_reports(tmp_path):
    for name, expected in BASELINE.items():
        root = _materialize(tmp_path, name)
        spec = _SCENARIO_ROOT[name]
        result = drift.diagnose(spec["env"], spec["dotenv"], root)
        assert result == expected, name
        if name != "invalid_catalog":
            assert _valid_drift(result), name
        assert "secret" not in json.dumps(result)
        assert "legacy-secret" not in json.dumps(result)
        assert "saved-key" not in json.dumps(result)
