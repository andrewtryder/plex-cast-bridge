#!/usr/bin/env python3
"""
Interactive Google Home Speaker Emulator for Mac
================================================
Emulates a physical Google Home speaker locally on your Mac using native macOS audio (afplay).
Lets you test the full Plex + Gemini + Smart DJ pipeline without needing a physical speaker.

Usage:
  Interactive Mode:
    uv run python scripts/emulate_speaker.py

  Quick Demo Mode (plays 8 seconds then pauses):
    uv run python scripts/emulate_speaker.py "Play some 90s trip hop like DJ Shadow"
"""

import logging
import readline  # Enable arrow keys & history in input()
import sys
import time

from plex_cast_bridge.ai_router import SmartDJ
from plex_cast_bridge.config import Settings
from plex_cast_bridge.local_speaker import LocalSpeaker
from plex_cast_bridge.plex_service import PlexService

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("emulator")


def print_banner():
    print("\n" + "=" * 65)
    print("  Google Home Speaker Emulator for macOS (Plex + Gemini)")
    print("=" * 65)
    print(" Audio Output: Built-in Mac Speakers (afplay)")
    print(" Commands:")
    print("   <query>        -> Voice request (e.g. 'play DJ Shadow', 'play 90s hip hop')")
    print("   pause          -> Pause playback")
    print("   resume         -> Resume playback")
    print("   next / skip    -> Skip to next track in radio queue")
    print("   vol <0-100>    -> Adjust volume (e.g. 'vol 25')")
    print("   status         -> Print current playback and queue state")
    print("   stop           -> Stop music and clear queue")
    print("   exit / quit    -> Exit emulator")
    print("=" * 65 + "\n")


def execute_voice_command(query: str, dj: SmartDJ, plex: PlexService, speaker: LocalSpeaker):
    print(f"\n[AI Router] Interpreting request: '{query}'...")
    try:
        intent = dj.parse(query, plex.catalog)
    except Exception as exc:
        print(f"[-] AI parsing failed: {exc}")
        return

    print(f"      Intent Mode:    {intent.mode}")
    print(f"      Target Artist:  {intent.target_artist}")
    print(f"      Target Album:   {intent.target_album}")
    print(f"      Target Track:   {intent.target_track}")
    print(f"      Matched Genres: {intent.matched_genres}")
    print(f"      Seed Artists:   {intent.seed_artists}")

    print("[Plex] Resolving matching tracks from library...")
    tracks = plex.resolve(intent)
    if not tracks:
        print("[-] No tracks found in your Plex library matching that query.")
        return

    print(f"[Plex] Queued {len(tracks)} tracks for station playback:")
    for idx, t in enumerate(tracks[:3], 1):
        artist = getattr(t, "grandparentTitle", "Unknown")
        print(f"       {idx}. {t.title} - {artist}")
    if len(tracks) > 3:
        print(f"       ... and {len(tracks) - 3} more tracks in radio queue")

    print("[Speaker] Starting playback on Mac speakers...")
    speaker.replace(tracks, repeat=intent.mode in ("artist_radio", "theme_mix"))
    st = speaker.status()
    curr = st.get("current_track") or {}
    print(f"[Playing] '{curr.get('title')}' by '{curr.get('artist')}' at {int(st.get('volume', 0.25) * 100)}% volume\n")


def main():
    settings = Settings()

    print("[1/3] Connecting to Plex Media Server...")
    plex = PlexService(
        settings.plex_url,
        settings.plex_token.get_secret_value(),
        settings.plex_music_section,
    )
    plex.refresh()
    print(f"      Loaded {len(plex.artists)} artists from Plex Music library.")

    print("[2/3] Initializing Gemini SmartDJ router...")
    dj = SmartDJ(settings.gemini_api_key.get_secret_value())

    print("[3/3] Initializing Mac Audio Output...")
    speaker = LocalSpeaker(enable_playback=True)
    speaker.set_volume(0.20)  # Safe 20% initial volume

    # One-shot command line test mode
    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
        print_banner()
        execute_voice_command(query, dj, plex, speaker)
        print("Demo mode: Playing for 8 seconds through your Mac speakers...")
        time.sleep(8)
        speaker.pause()
        print("Demo completed. Playback paused.")
        return 0

    # Interactive REPL mode
    print_banner()
    while True:
        try:
            line = input("Google Home > ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting emulator.")
            speaker.stop()
            break

        if not line:
            continue

        cmd = line.lower()
        if cmd in ("exit", "quit", "q"):
            speaker.stop()
            print("Goodbye.")
            break
        elif cmd == "pause":
            speaker.pause()
        elif cmd in ("resume", "play"):
            if speaker.state == "PAUSED":
                speaker.resume()
            else:
                print("Nothing currently paused. Give a request like 'play DJ Shadow'.")
        elif cmd in ("next", "skip"):
            speaker.skip()
        elif cmd == "stop":
            speaker.stop()
            print("Stopped.")
        elif cmd.startswith("vol"):
            parts = cmd.split()
            if len(parts) > 1:
                try:
                    raw_val = parts[1].replace("%", "")
                    val = float(raw_val)
                    if val > 1.0:
                        val = val / 100.0
                    speaker.set_volume(val)
                except ValueError:
                    print("Usage: vol 20 (for 20%)")
            else:
                print(f"Current volume: {int(speaker.volume * 100)}%")
        elif cmd == "status":
            st = speaker.status()
            print(f"State:        {st['state']}")
            curr = st.get("current_track")
            if curr:
                print(f"Now Playing:  {curr['title']} - {curr['artist']} ({curr['album']})")
            print(f"Queue Length: {st['queue_length']}")
            print(f"Volume:       {int(st['volume'] * 100)}%")
        elif cmd == "help":
            print_banner()
        else:
            execute_voice_command(line, dj, plex, speaker)

    return 0


if __name__ == "__main__":
    sys.exit(main())

