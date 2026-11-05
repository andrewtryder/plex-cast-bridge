#!/usr/bin/env python3
"""
Test Live Cast to Physical Hardware ("Master Bedroom")
Directly verifies the local chain (Plex -> Gemini -> pychromecast -> Master Bedroom)
bypassing Google Home and Pub/Sub.
"""

import logging
import sys
import time

from plex_cast_bridge.ai_router import SmartDJ
from plex_cast_bridge.cast_manager import CastManager
from plex_cast_bridge.config import Settings
from plex_cast_bridge.plex_service import PlexService

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("test_live_cast")


def main():
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Play DJ Shadow"
    speaker_name = sys.argv[2] if len(sys.argv) > 2 else "Master Bedroom"

    print("\n" + "=" * 65)
    print(" Live Physical Cast Verification: Master Bedroom")
    print("=" * 65)
    print(f"Target Speaker: {speaker_name}")
    print(f"Test Prompt:    '{prompt}'")
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

    log.info("Connecting to speaker '%s'...", speaker_name)
    cm = CastManager(default_volume=0.15, default_speaker_host=settings.default_speaker_host)
    try:
        speaker = cm.get(speaker_name)
    except Exception as exc:
        log.error("Could not connect to speaker '%s': %s", speaker_name, exc)
        return 1

    host = getattr(speaker.cast.cast_info, "host", "LAN")
    print("\n[1/4] Connected to Speaker:")
    print(f"      Name:   {speaker.cast.name}")
    print(f"      Model:  {speaker.cast.model_name}")
    print(f"      Host:   {host}")
    print(f"      Volume: {int(speaker.cast.status.volume_level * 100)}% (clamped to 15%)\n")

    # Set 15% volume explicitly
    speaker.set_volume(0.15)

    print("[2/4] Parsing intent with Gemini SmartDJ...")
    intent = dj.parse(prompt, plex.catalog)
    print(f"      Mode:          {intent.mode}")
    print(f"      Target Artist: {intent.target_artist}")
    print(f"      Target Album:  {intent.target_album}")
    print(f"      Target Track:  {intent.target_track}")
    print(f"      Matched Genres:{intent.matched_genres}")
    print(f"      Seed Artists:  {intent.seed_artists}\n")

    print("[3/4] Resolving tracks from Plex library...")
    tracks = plex.resolve(intent)
    if not tracks:
        log.error("No tracks resolved from Plex for intent")
        return 1

    print(f"      Found {len(tracks)} tracks for queue:")
    for idx, t in enumerate(tracks[:5], 1):
        artist = getattr(t, "grandparentTitle", "Unknown")
        print(f"        {idx}. {t.title} - {artist}")
    if len(tracks) > 5:
        print(f"        ... and {len(tracks) - 5} more tracks in radio station queue\n")

    print(f"[4/4] Starting playback on '{speaker.cast.name}'...")
    speaker.replace(tracks, repeat=intent.mode in ("artist_radio", "theme_mix"))

    # Monitor playback state transition
    print("      Waiting for speaker to begin streaming...")
    playing = False
    for attempt in range(15):
        time.sleep(1)
        st = speaker.cast.media_controller.status
        player_state = getattr(st, "player_state", "UNKNOWN")
        print(f"      [{attempt + 1}s] Speaker state: {player_state}")
        if player_state in ("PLAYING", "BUFFERING"):
            playing = True
            break

    if playing:
        print("\n" + "=" * 65)
        print(" SUCCESS! 'Master Bedroom' is actively streaming Plex music!")
        print(f" Currently Playing: {speaker.current.title} by {getattr(speaker.current, 'grandparentTitle', '')}")
        print(" Volume: 15%")
        print("=" * 65 + "\n")
        print("Playback is ongoing. Pausing in 5 seconds to avoid disturbing the room...")
        time.sleep(5)
        speaker.pause()
        print("Playback paused successfully.")
        return 0
    else:
        print("\n[WARNING] Speaker did not report PLAYING within 15s. Check network audio routes.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
