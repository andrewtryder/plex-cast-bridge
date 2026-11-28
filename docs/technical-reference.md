# Plex Cast Bridge — Technical Reference

For a first-time installation, start with the [README](../README.md) and the [Docker & Portainer guide](install-docker.md). This document collects technical background, deployment options, and developer commands.

## Features

- **Artist-first & station streaming:** Play an artist strictly from your library, or build Gemini-assisted radio / theme mixes.
- **Per-speaker virtual Google Home devices:** One shared Plex library; each Cast destination can advertise its own virtual Plex device (for example Kitchen Plex) with a stable ID that survives renames and IP changes.
- **Local web dashboard:** Refresh speakers, allow playback independently of Google Home visibility, edit virtual names, forget destinations, and sync to HomeGraph.
- **Optional library index and mixes:** Enable the persistent SQLite index in Settings → Library to preview and save bridge-managed mixes. AI-assisted selection is off by default. [Music index guide](music-index.md).
- **Gemini Smart DJ:** Natural-language intents (`artist`, `artist_radio`, `album`, `track`, `theme_mix`) constrained to your catalog.
- **Optional Google Home gateway (`gcp-cloud-run/`):** OAuth 2.0 account linking and Smart Home traits over Cloud Run + Pub/Sub (no inbound ports to your LAN).
- **Cast discovery & volume safety:** `pychromecast` with macOS `dns-sd` fallback; startup volume clamp (default 20%).
- **Mac speaker emulator:** Test without hardware via `afplay`.
- **Production packaging:** Multi-stage Dockerfiles, host-network Compose, systemd unit, Proxmox guide.

## Architecture

```text
 ┌──────────────────────┐
 │  Google Home /       │
 │  Assistant controls  │   (optional Google Home path)
 └──────────┬───────────┘
            ▼
 ┌─────────────────────────────────────────────────────────┐
 │ GCP Cloud Run (cloud_gateway)                           │
 │ OAuth + Smart Home SYNC / QUERY / EXECUTE / DISCONNECT  │
 └──────────┬──────────────────────────────────────────────┘
            │ Pub/Sub
            ▼
 ┌─────────────────────────────────────────────────────────┐
 │ Local bridge (Docker / Mac / Proxmox)                   │
 │ Dashboard · Gemini · Plex · Chromecast                  │
 └─────────────────────────────────────────────────────────┘
```

Dashboard-only installs skip Cloud Run and Pub/Sub entirely.

## Repository Structure

```text
plex-cast-bridge/
├── pyproject.toml
├── catalog.json                     # Sample catalog (replace via sync_plex_catalog.py)
├── LICENSE
├── assets/                          # Icons (incl. 144×144 Google Home upload)
├── src/plex_cast_bridge/            # Local bridge (FastAPI + Cast + Pub/Sub worker)
├── gcp-cloud-run/                   # Optional Cloud Run OAuth + fulfillment
├── deploy/                          # Dockerfile, compose, systemd
├── docs/
│   ├── install-docker.md            # Beginner Docker path
│   ├── proxmox-deployment.md
│   └── setup/SETUP_GUIDE.md         # Advanced Google Home path
├── scripts/                         # sync, preflight, emulator, e2e harnesses
└── tests/
```

## Local Mac Emulator

```bash
uv sync
cp .env.example .env
# Optionally set PLEX_URL and PLEX_TOKEN for headless setup
uv run python scripts/preflight.py --skip-plex   # or full check without --skip-plex
uv run python scripts/emulate_speaker.py
```

## Speakers vs virtual Plex devices

- **Cast speakers / Nest devices** are physical (or Cast Group) destinations on your LAN.
- **Virtual Plex devices** are what Google Home lists after account linking. Each visible destination maps to one virtual device ID.
- **Show in Google Home** opts a destination into SYNC. New discoveries default to **off**.
- **Google Home room assignments** are managed in the Google Home app. The bridge `room_hint` is only a suggestion and does not override your existing rooms.
- Pause and next have been observed on the existing deployment; artist-search phrases have not been verified. Use the dashboard to select music.
- This integration does **not** reveal which Nest microphone heard a command, and it is **not** a native Google Assistant music provider. Voice routes to the virtual device you name (or the room you assigned that virtual device to).

After adding or renaming a Nest speaker: **Refresh speakers** → optionally enable **Show in Google Home** → **Refresh & Sync Google Home**. You do not need to rerun the setup wizard or rotate cloud credentials for ordinary speaker changes.

## Google Home (advanced)

See the step-by-step guide: [Google Home setup guide](setup/SETUP_GUIDE.md).

Summary:

1. Sync catalog → deploy `gcp-cloud-run/`
2. Create a Cloud-to-cloud integration in the **Google Home Developer Console**
3. Create a Pub/Sub **subscriber** service account JSON for the local worker
4. Link the **`[test]`** integration in the Google Home app

This repo targets personal / test integrations. Public Google certification is out of scope unless you pursue it separately.

## Production Deployment

- **Docker / Proxmox:** [Docker installation](install-docker.md) and [Proxmox deployment](proxmox-deployment.md).
- **Persistent state and upgrades:** [Persistent state](persistent-state.md) and [upgrade guide](upgrading.md). Keep the original data mount on upgrades.
- **Maintainer release gates:** [Verified release process](release-process.md) and [release acceptance](release-acceptance.md).
- **Image:** `ghcr.io/andrewtryder/plex-cast-bridge:latest` (and version tags from GitHub Releases). The package must be public in GHCR package settings for anonymous pulls.
- **Systemd:** [service definition](../deploy/plex-cast-bridge.service).
- **Portainer:** use `deploy/docker-compose.portainer.yml` for Git stacks, with a stable absolute `PCB_DATA_DIR` as described in the installation guide.
- **Source builds:** use the Compose build override documented in the installation guide; normal installs use prebuilt multi-architecture images (amd64 and arm64).

## Security and optional cloud costs

The local web dashboard and API have no built-in login. Restrict access with a firewall, VPN, or secured reverse proxy; never expose them directly to the public Internet.

Dashboard-only installs do not use Cloud Run or Pub/Sub. Optional Google Home integration requires a Google Cloud project with billing enabled. Gemini-assisted music selection requires a Gemini API key. The previous project estimate for light personal use (around 20 voice commands per day) was $0.00–$0.06 per month with free-tier usage, Cloud Run minimum instances set to zero, and pruned Artifact Registry storage. This is illustrative, not a guarantee: actual costs depend on current pricing, usage, region, and configuration.

## Testing

```bash
uv run pytest
uv run python scripts/preflight.py
```

## License

[MIT](../LICENSE).
