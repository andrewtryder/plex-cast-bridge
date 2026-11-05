#!/usr/bin/env python3
"""
Sync Plex Music Library and Local Cast Speakers to Canonical catalog.json
Extracts all unique artists, genres, and top moods from the local Plex Media Server,
discovers all Google Cast speakers/displays on the LAN, preserves enabled/disabled
states, and writes catalog.json for both the local bridge and GCP Cloud Run gateway.
"""

import json
import logging
import shutil
import sys
from pathlib import Path

from plex_cast_bridge.cast_manager import CastManager, extract_room_name
from plex_cast_bridge.config import Settings
from plex_cast_bridge.plex_service import PlexService

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("sync_plex_catalog")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CANONICAL_CATALOG = PROJECT_ROOT / "catalog.json"
CLOUD_RUN_CATALOG = PROJECT_ROOT / "gcp-cloud-run" / "catalog.json"


def load_existing_speaker_states() -> dict[str, bool]:
    """Read existing catalog.json so user-configured enabled/disabled speaker toggles persist."""
    states: dict[str, bool] = {}
    if CANONICAL_CATALOG.exists():
        try:
            with open(CANONICAL_CATALOG, "r", encoding="utf-8") as f:
                data = json.load(f)
                for spk in data.get("speakers", []):
                    name = (spk.get("speaker_name") or "").strip().casefold()
                    room = (spk.get("roomHint") or "").strip().casefold()
                    enabled = bool(spk.get("enabled", True))
                    if name:
                        states[name] = enabled
                    if room:
                        states[room] = enabled
        except Exception as exc:
            log.warning("Could not read existing speaker states from %s: %s", CANONICAL_CATALOG, exc)
    return states


def build_speakers_catalog(discovered_devices: list[dict], existing_states: dict[str, bool]) -> list[dict]:
    speakers_list = []
    seen_rooms = set()

    for dev in discovered_devices:
        name = dev["name"]
        # Skip the synthetic 'test speaker' from Google Home Cloud SYNC (keep it for local Web UI)
        if name.casefold() == "test speaker":
            continue

        room = dev.get("room") or extract_room_name(name)
        if room.casefold() in seen_rooms:
            continue
        seen_rooms.add(room.casefold())

        enabled = existing_states.get(
            name.casefold(),
            existing_states.get(room.casefold(), True),
        )

        speakers_list.append({
            "id": f"plex-jukebox:{room}",
            "name": "Plex",
            "nicknames": [
                f"{room} Plex",
                "Plex",
                "Plex Jukebox",
                "Plex Radio",
            ],
            "roomHint": room,
            "speaker_name": name,
            "model": dev.get("model", "Google Cast"),
            "host": dev.get("host", ""),
            "port": dev.get("port", 8009),
            "uuid": dev.get("uuid", ""),
            "enabled": enabled,
        })

    if not speakers_list:
        speakers_list.append({
            "id": "plex-jukebox:Master Bedroom",
            "name": "Plex",
            "nicknames": ["Master Bedroom Plex", "Plex", "Plex Jukebox"],
            "roomHint": "Master Bedroom",
            "speaker_name": "Master Bedroom display",
            "model": "Google Nest Hub",
            "host": "",
            "port": 8009,
            "uuid": "",
            "enabled": True,
        })

    return speakers_list


def main():
    settings = Settings()
    existing_states = load_existing_speaker_states()

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

    log.info("Scanning LAN for Google Cast speakers and displays...")
    cast_mgr = CastManager(
        default_volume=settings.default_cast_volume,
        default_speaker_host=settings.default_speaker_host,
    )
    discovered = cast_mgr.discover()
    speakers_catalog = build_speakers_catalog(discovered, existing_states)
    log.info(
        "Discovered %d Cast speakers/rooms: %s",
        len(speakers_catalog),
        ", ".join(f"{s['roomHint']} ({s['speaker_name']}, enabled={s['enabled']})" for s in speakers_catalog),
    )

    curated_themes = [
        "90s Trip Hop",
        "90s Alternative Rock",
        "Instrumental Hip Hop",
        "Chill Electronic",
        "Late Night Downtempo",
        "Post-Hardcore Classics",
    ]

    inputs = [s["roomHint"] for s in speakers_catalog if s.get("enabled", True)]
    if settings.default_speaker_name and settings.default_speaker_name not in inputs:
        inputs.insert(0, settings.default_speaker_name)
    unique_inputs = list(dict.fromkeys(i for i in inputs if i))

    catalog_payload = {
        "artists": artists,
        "genres": genres,
        "themes": curated_themes,
        "moods": moods[:50],
        "inputs": unique_inputs,
        "speakers": speakers_catalog,
    }

    with open(CANONICAL_CATALOG, "w", encoding="utf-8") as f:
        json.dump(catalog_payload, f, indent=2, ensure_ascii=False)
        f.write("\n")

    log.info(
        "Wrote canonical catalog to %s (%d artists, %d genres, %d speakers)",
        CANONICAL_CATALOG,
        len(artists),
        len(genres),
        len(speakers_catalog),
    )

    if CLOUD_RUN_CATALOG.parent.exists():
        shutil.copyfile(CANONICAL_CATALOG, CLOUD_RUN_CATALOG)
        log.info("Synced catalog copy to %s for Cloud Run builds", CLOUD_RUN_CATALOG)

    return 0


if __name__ == "__main__":
    sys.exit(main())
