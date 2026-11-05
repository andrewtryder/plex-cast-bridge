#!/usr/bin/env python3
"""
Sync Plex Music Library to Canonical catalog.json
Extracts all unique artists, genres, and top moods from the local Plex Media Server
and formats them into catalog.json for both the local bridge and GCP Cloud Run gateway.
"""

import json
import logging
import shutil
import sys
from pathlib import Path

from plex_cast_bridge.config import Settings
from plex_cast_bridge.plex_service import PlexService

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("sync_plex_catalog")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CANONICAL_CATALOG = PROJECT_ROOT / "catalog.json"
CLOUD_RUN_CATALOG = PROJECT_ROOT / "gcp-cloud-run" / "catalog.json"


def main():
    settings = Settings()
    log.info("Connecting to Plex Media Server at %s (section: '%s')...", settings.plex_url, settings.plex_music_section)

    plex = PlexService(
        settings.plex_url,
        settings.plex_token.get_secret_value(),
        settings.plex_music_section,
    )
    raw_catalog = plex.refresh()

    artists = raw_catalog.get("artists", [])
    genres = raw_catalog.get("genres", [])
    moods = raw_catalog.get("moods", [])

    curated_themes = [
        "90s Trip Hop",
        "90s Alternative Rock",
        "Instrumental Hip Hop",
        "Chill Electronic",
        "Late Night Downtempo",
        "Post-Hardcore Classics",
    ]

    inputs = [
        settings.default_speaker_name or "Master Bedroom",
        "Living Room",
        "Office",
        "Mac Speaker",
    ]
    # Deduplicate inputs while preserving order
    unique_inputs = list(dict.fromkeys(i for i in inputs if i))

    catalog_payload = {
        "artists": artists,
        "genres": genres,
        "themes": curated_themes,
        "moods": moods[:50],
        "inputs": unique_inputs,
    }

    with open(CANONICAL_CATALOG, "w", encoding="utf-8") as f:
        json.dump(catalog_payload, f, indent=2, ensure_ascii=False)
        f.write("\n")

    log.info(
        "Wrote canonical catalog to %s (%d artists, %d genres, %d moods)",
        CANONICAL_CATALOG,
        len(artists),
        len(genres),
        len(catalog_payload["moods"]),
    )

    if CLOUD_RUN_CATALOG.parent.exists():
        shutil.copyfile(CANONICAL_CATALOG, CLOUD_RUN_CATALOG)
        log.info("Synced catalog copy to %s for Cloud Run builds", CLOUD_RUN_CATALOG)

    return 0


if __name__ == "__main__":
    sys.exit(main())
