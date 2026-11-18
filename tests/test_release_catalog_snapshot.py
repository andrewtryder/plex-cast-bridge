import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.release_catalog_snapshot import snapshot


def test_snapshot_never_migrates_catalog_or_exposes_unlisted_fields(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"speakers": [{"id": "old-id", "uuid": "cast-id", "token": "secret"}]}))
    before = path.read_bytes(), path.stat().st_mtime_ns
    result = snapshot(path)
    assert result == {"available": True, "speakers": [{"id": "old-id", "uuid": "cast-id"}]}
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert list(tmp_path.iterdir()) == [path]


def test_invalid_catalog_is_unavailable_not_empty(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text("invalid")
    assert snapshot(path) == {"available": False}
