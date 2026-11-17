"""Read speaker identities from the selected catalog without invoking migration.

This module is also executed inside older bridge containers by release tooling.
It never invokes catalog_store.load_catalog_data(), which may rewrite the catalog.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


FIELDS = ("id", "google_home_device_id", "uuid", "cast_uuid", "name", "speaker_name", "room", "room_hint", "roomHint", "google_home_name",
          "kind", "enabled", "google_home_visible", "is_group")


def snapshot(path: Path) -> dict[str, Any]:
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(catalog, dict) or not isinstance(catalog.get("speakers", []), list):
            raise ValueError("invalid catalog")
        speakers = [{key: item[key] for key in FIELDS if key in item}
                    for item in catalog.get("speakers", [])
                    if isinstance(item, dict) and not item.get("removed")]
        speakers.sort(key=lambda item: json.dumps(item, sort_keys=True))
        return {"available": True, "speakers": speakers}
    except (OSError, ValueError):
        return {"available": False}


def main() -> None:
    from plex_cast_bridge.catalog_store import get_catalog_path
    print(json.dumps(snapshot(get_catalog_path(for_writing=False))))


if __name__ == "__main__":
    main()
