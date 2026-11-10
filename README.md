<p align="center">
  <img src="assets/icon.png" width="120" height="120" alt="Plex Cast Bridge Logo" style="border-radius: 26px; box-shadow: 0 8px 24px rgba(0,0,0,0.3);">
</p>

<h1 align="center">Plex Cast Bridge</h1>

<p align="center">
  <strong>Self-hosted Plex music streaming for Google Home and Chromecast speakers, with Gemini-powered radio, voice control, and a Docker web dashboard.</strong>
</p>

<p align="center">
  <a href="https://github.com/andrewtryder/plex-cast-bridge/actions/workflows/ci.yml"><img src="https://github.com/andrewtryder/plex-cast-bridge/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License"></a>
  <a href="https://github.com/andrewtryder/plex-cast-bridge/pkgs/container/plex-cast-bridge"><img src="https://img.shields.io/badge/ghcr.io-plex--cast--bridge-green" alt="GHCR"></a>
</p>

<p align="center">
  <a href="#quick-start-docker">Docker Quick Start</a> &bull;
  <a href="#features">Features</a> &bull;
  <a href="#architecture">Architecture</a> &bull;
  <a href="docs/install-docker.md">Install Guide</a> &bull;
  <a href="docs/setup/SETUP_GUIDE.md">Google Home (advanced)</a> &bull;
  <a href="#license">License</a>
</p>

---

## Quick Start (Docker)

The easiest path is the prebuilt multi-arch image (`linux/amd64` + `linux/arm64`):

```bash
git clone https://github.com/andrewtryder/plex-cast-bridge.git
cd plex-cast-bridge
cp .env.example .env
# Optionally set PLEX_URL and PLEX_TOKEN for headless setup
echo '{}' > gcp-credentials.json   # placeholder until Google Home voice is enabled

docker compose -f deploy/docker-compose.yml pull
docker compose -f deploy/docker-compose.yml up -d
```

Open `http://<host>:8000/`, connect Plex, discover speakers, and play. The dashboard and local APIs have no login: anyone who can reach them can control playback and use local administrative actions. Restrict access with a firewall, VPN, or a secured reverse proxy; do not expose the service directly to the public Internet.

Full steps, preflight checks, and troubleshooting: **[docs/install-docker.md](docs/install-docker.md)**

![Dashboard](docs/setup/screenshots/dashboard.png)

Google Home / Cloud Run voice control is **optional** and documented separately: **[docs/setup/SETUP_GUIDE.md](docs/setup/SETUP_GUIDE.md)**.

---

## Features

- **Artist-first & station streaming:** Play an artist strictly from your library, or build Gemini-assisted radio / theme mixes.
- **Per-speaker virtual Google Home devices:** One shared Plex library; each Cast destination can advertise its own virtual Plex device (for example Kitchen Plex) with a stable ID that survives renames and IP changes.
- **Local web dashboard:** Refresh speakers, allow playback independently of Google Home visibility, edit virtual names, forget destinations, and sync to HomeGraph.
- **Gemini Smart DJ:** Natural-language intents (`artist`, `artist_radio`, `album`, `track`, `theme_mix`) constrained to your catalog.
- **Optional Google Home gateway (`gcp-cloud-run/`):** OAuth 2.0 account linking and Smart Home traits over Cloud Run + Pub/Sub (no inbound ports to your LAN).
- **Cast discovery & volume safety:** `pychromecast` with macOS `dns-sd` fallback; startup volume clamp (default 20%).
- **Mac speaker emulator:** Test without hardware via `afplay`.
- **Production packaging:** Multi-stage Dockerfiles, host-network Compose, systemd unit, Proxmox guide.

---

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

---

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

---

## Local Mac Emulator

```bash
uv sync
cp .env.example .env
# Optionally set PLEX_URL and PLEX_TOKEN for headless setup
uv run python scripts/preflight.py --skip-plex   # or full check without --skip-plex
uv run python scripts/emulate_speaker.py
```

---

## Speakers vs virtual Plex devices

- **Cast speakers / Nest devices** are physical (or Cast Group) destinations on your LAN.
- **Virtual Plex devices** are what Google Home lists after account linking. Each visible destination maps to one virtual device ID.
- **Show in Google Home** opts a destination into SYNC. New discoveries default to **off**.
- **Google Home room assignments** are managed in the Google Home app. The bridge `room_hint` is only a suggestion and does not override your existing rooms.
- Pause and next have been observed on the existing deployment; artist-search phrases have not been verified. Use the dashboard to select music.
- This integration does **not** reveal which Nest microphone heard a command, and it is **not** a native Google Assistant music provider. Voice routes to the virtual device you name (or the room you assigned that virtual device to).

After adding or renaming a Nest speaker: **Refresh speakers** → optionally enable **Show in Google Home** → **Refresh & Sync Google Home**. You do not need to rerun the setup wizard or rotate cloud credentials for ordinary speaker changes.

## Google Home (advanced)

See the step-by-step guide: [docs/setup/SETUP_GUIDE.md](docs/setup/SETUP_GUIDE.md).

Summary:

1. Sync catalog → deploy `gcp-cloud-run/`
2. Create a Cloud-to-cloud integration in the **Google Home Developer Console**
3. Create a Pub/Sub **subscriber** service account JSON for the local worker
4. Link the **`[test]`** integration in the Google Home app

This repo targets personal / test integrations. Public Google certification is out of scope unless you pursue it separately.

---

## Production Deployment

- **Docker / Proxmox:** [docs/install-docker.md](docs/install-docker.md) and [docs/proxmox-deployment.md](docs/proxmox-deployment.md)
- **Image:** `ghcr.io/andrewtryder/plex-cast-bridge:latest` (and version tags from GitHub Releases). The package must be marked **Public** in GHCR package settings for anonymous pulls.
- **Systemd:** `deploy/plex-cast-bridge.service`

---

## Monthly Cost Estimate

Personal use (~20 voice commands/day) is typically **\$0.00–\$0.06/month** within GCP/Gemini free tiers (Cloud Run min-instances=0, Pub/Sub, pruned Artifact Registry).

---

## Testing

```bash
uv run pytest
uv run python scripts/preflight.py
```

---

## License

[MIT](LICENSE). Open source for personal, self-hosted media setups.
