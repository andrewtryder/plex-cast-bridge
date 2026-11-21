"""OpenAPI contract equivalence for the refactored local dashboard routers."""

import json

from plex_cast_bridge.main import app

LOCAL_DASHBOARD_PREFIXES = (
    "/api/activity",
    "/api/artwork/",
    "/api/backup",
    "/api/command",
    "/api/diagnostics",
    "/api/library/",
    "/api/queue",
    "/api/settings",
    "/api/speakers",
    "/api/status",
    "/api/sync-catalog",
    "/api/transport",
)


def _local_paths(schema: dict) -> dict:
    paths = {}
    for path, ops in schema.get("paths", {}).items():
        if path in LOCAL_DASHBOARD_PREFIXES or any(
            path.startswith(prefix) for prefix in LOCAL_DASHBOARD_PREFIXES
        ):
            paths[path] = ops
    return paths


def _normalize(obj):
    if isinstance(obj, dict):
        return {k: _normalize(v) for k, v in sorted(obj.items()) if k != "openapi"}
    if isinstance(obj, list):
        return [_normalize(v) for v in obj]
    return obj


def test_local_dashboard_openapi_paths_registered():
    schema = app.openapi()
    local = _local_paths(schema)
    expected = {
        "/api/activity",
        "/api/artwork/{rating_key}",
        "/api/backup",
        "/api/backup/restore",
        "/api/backup/restore/preview",
        "/api/command",
        "/api/diagnostics",
        "/api/library/browse",
        "/api/library/play",
        "/api/library/refresh",
        "/api/library/tracks",
        "/api/queue",
        "/api/settings",
        "/api/speakers",
        "/api/speakers/refresh",
        "/api/speakers/test",
        "/api/speakers/toggle",
        "/api/speakers/{device_id}",
        "/api/status",
        "/api/sync-catalog",
        "/api/transport",
    }
    assert expected.issubset(set(local.keys()))


def test_local_dashboard_openapi_snapshot_stable():
    """Regression guard: normalized local OpenAPI must match the checked snapshot."""
    schema = _normalize(_local_paths(app.openapi()))
    snapshot_path = (
        __import__("pathlib").Path(__file__).resolve().parent
        / "fixtures"
        / "local_dashboard_openapi.json"
    )
    assert snapshot_path.is_file(), (
        "Regenerate tests/fixtures/local_dashboard_openapi.json after intentional API changes"
    )
    expected = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert schema == expected
