#!/usr/bin/env python3
"""
Test Live Cast to Physical Hardware ("Master Bedroom")
Directly verifies the local chain (Plex -> Gemini -> pychromecast -> Master Bedroom)
bypassing Google Home and Pub/Sub.
"""

import argparse
import logging
import sys
import time

from plex_cast_bridge.ai_router import SmartDJ
from plex_cast_bridge.cast_manager import CastManager
from plex_cast_bridge.config import Settings
from plex_cast_bridge.plex_service import PlexService

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("test_live_cast")


def set_volume_and_verify(speaker, volume: float, timeout: float = 3.0) -> bool:
    """Set volume directly and wait until this Cast connection reports the target."""
    try:
        client = getattr(speaker.cast, "socket_client", None)
        if not getattr(client, "is_connected", False):
            return False
        status = getattr(speaker.cast, "status", None)
        if getattr(status, "volume_level", None) is None:
            return False
        target = min(float(volume), float(speaker.max_volume))
        speaker.cast.set_volume(target)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not getattr(client, "is_connected", False):
                return False
            current = getattr(getattr(speaker.cast, "status", None), "volume_level", None)
            if current is not None and abs(float(current) - target) <= 0.02:
                return True
            time.sleep(0.1)
        return False
    except Exception:  # noqa: BLE001 - PyChromecast failures vary by connection state.
        return False


def restore_original_volume(speaker, volume: float) -> bool:
    """Restore only when the same connected Cast reports a readable volume."""
    return set_volume_and_verify(speaker, volume)


def main():
    parser = argparse.ArgumentParser(description="Disruptive physical Cast playback check")
    parser.add_argument(
        "--allow-disruptive", action="store_true",
        help="explicitly authorize playback, pause, and a temporary volume change",
    )
    parser.add_argument("--device-uuid", required=True, help="exact Cast UUID from the private baseline")
    parser.add_argument("--query", required=True, help="known library query; never printed or logged")
    args = parser.parse_args()
    if not args.allow_disruptive:
        parser.error("physical playback requires --allow-disruptive")

    print("\n" + "=" * 65)
    print(" Live Physical Cast Verification: Master Bedroom")
    print("=" * 65)
    print(f"Target Cast UUID: {args.device_uuid}")
    print("Known library query: provided (redacted)")
    print("-" * 65)

    settings = Settings()
    log.info("Connecting to Plex Media Server...")
    plex = PlexService(
        settings.plex_url,
        settings.plex_token.get_secret_value(),
        settings.plex_music_section,
    )
    plex.refresh()
    log.info("Plex loaded with %d artists", len(plex.artists))

    log.info("Initializing SmartDJ with Gemini...")
    dj = SmartDJ(settings.gemini_api_key.get_secret_value())

    from plex_cast_bridge.catalog_store import list_speaker_records

    cm = CastManager(default_volume=0.15, default_speaker_host=settings.default_speaker_host)
    try:
        records = [
            record for record in list_speaker_records()
            if str(record.get("cast_uuid") or record.get("uuid") or "").casefold()
            == args.device_uuid.casefold()
        ]
        if len(records) != 1:
            raise ValueError("Cast UUID does not resolve to exactly one configured speaker")
        speaker = cm.get_device(records[0].get("google_home_device_id") or records[0].get("id"))
    except Exception as exc:  # noqa: BLE001 - connection errors are surfaced without payload data.
        log.error("Could not connect to the selected Cast UUID: %s", type(exc).__name__)
        return 1

    original_volume = getattr(getattr(speaker.cast, "status", None), "volume_level", None)
    original_state = getattr(speaker.cast.media_controller.status, "player_state", "UNKNOWN")
    if original_volume is None or original_state not in {"IDLE", "STOPPED"}:
        log.error("Refusing test: receiver state/volume is unknown or playback is already active.")
        cm.close()
        return 1
    safe_test_volume = min(float(original_volume), 0.15, float(settings.max_cast_volume))

    print("\n[1/4] Connected to Speaker:")
    print(f"      Name:   {speaker.cast.name}")
    print(f"      Model:  {speaker.cast.model_name}")
    print(f"      Initial state: {original_state}")
    print(f"      Initial volume: {int(original_volume * 100)}%")
    print(f"      Test volume cap: {int(safe_test_volume * 100)}%\n")

    print("[2/4] Parsing intent with Gemini SmartDJ...")
    intent = dj.parse(args.query, plex.catalog)
    print("      Query parsed; private intent details redacted.\n")

    print("[3/4] Resolving tracks from Plex library...")
    tracks = plex.resolve(intent)
    if not tracks:
        log.error("No tracks resolved from Plex for intent")
        cm.close()
        return 1

    print(f"      Resolved {len(tracks)} track(s); titles and artist names redacted.\n")

    print(f"[4/4] Starting playback on '{speaker.cast.name}'...")
    outcome = 2
    try:
        # Lower only; never raise volume to begin a test.
        if not set_volume_and_verify(speaker, safe_test_volume):
            log.error("Could not verify safe test volume; refusing to start playback.")
            outcome = 1
            return outcome
        speaker.replace(tracks, repeat=intent.mode in ("artist_radio", "theme_mix"))

        print("      Waiting for speaker to begin streaming...")
        for attempt in range(15):
            time.sleep(1)
            st = speaker.cast.media_controller.status
            player_state = getattr(st, "player_state", "UNKNOWN")
            print(f"      [{attempt + 1}s] Receiver state: {player_state}")
            if player_state in ("PLAYING", "BUFFERING"):
                print("      Receiver entered playback; track metadata redacted.")
                time.sleep(2)
                speaker.pause()
                print("      Receiver pause command issued.")
                outcome = 0
                break
        if outcome:
            print("      Receiver did not report PLAYING within 15s.")
    except Exception as exc:  # noqa: BLE001 - external Cast errors are reported without track data.
        log.error("Physical Cast test failed (%s)", type(exc).__name__)
        outcome = 1
    finally:
        restored = restore_original_volume(speaker, original_volume)
        if restored:
            print(f"      Original volume restored to {int(original_volume * 100)}%.")
        else:
            print("      Original volume was not restored: receiver connection/state was not safe to verify.")
        cm.close()
    return outcome


if __name__ == "__main__":
    sys.exit(main())
