# Install Plex Cast Bridge with Docker

Beginner path: stream your Plex music library to Chromecast / Google Nest speakers from a local web dashboard. **Google Home voice control is optional** — see [SETUP_GUIDE.md](setup/SETUP_GUIDE.md) after this works.

## Requirements

- Docker + Docker Compose
- A Plex Media Server with a Music library on your LAN
- Chromecast / Nest speakers on the same LAN (or use the Mac emulator without Docker — see README)

## 1. Clone and configure

```bash
git clone https://github.com/andrewtryder/plex-cast-bridge.git
cd plex-cast-bridge
cp .env.example .env
```

Edit `.env`:

| Variable | Required | Notes |
|---|---|---|
| `PLEX_URL` | No† | e.g. `http://192.168.1.50:32400` |
| `PLEX_TOKEN` | No† | Plex `X-Plex-Token` (legacy / headless) |
| `PLEX_MUSIC_SECTION` | No† | Usually `Music` |
| `LOCAL_API_KEY` | Yes* | `openssl rand -hex 32` |
| `GEMINI_API_KEY` | No | Needed for theme / mood mixes |
| `ENABLE_PUBSUB_LISTENER` | No | Leave `false` until Google Home is set up |
| `DATA_DIR` | No | Defaults to `/data` in Docker; stores `plex_connection.json`, optional `app_settings.json`, device keys |
| `DEFAULT_CAST_VOLUME` / `MAX_CAST_VOLUME` | No | Bootstrap volumes (0–1). Overridden after **Settings → Playback → Save** writes `app_settings.json` |

\* Or set `ALLOW_UNAUTHENTICATED_LOCAL_API=true` only on a trusted home LAN.

† Preferred setup: open the dashboard Settings gear → **Connect with Plex**, authorize on plex.tv, then pick your server and Music library. Env `PLEX_TOKEN` remains supported for existing installs.

Volume and library summary details: **[setup/APP_SETTINGS.md](setup/APP_SETTINGS.md)**.

Pub/Sub credentials are **not** required for dashboard-only installs. Compose still mounts `./gcp-credentials.json`; create an empty placeholder until you enable voice:

```bash
echo '{}' > gcp-credentials.json
chmod 600 gcp-credentials.json
```

## 2. Preflight check

```bash
uv run python scripts/preflight.py --require-docker
# or without uv / local Python tooling:
# python3 scripts/preflight.py --require-docker
```

Fix any `FAIL` lines before continuing.

## 3. Start the bridge

```bash
docker compose -f deploy/docker-compose.yml pull
docker compose -f deploy/docker-compose.yml up -d
docker compose -f deploy/docker-compose.yml ps
```

Host networking is used so Chromecast mDNS discovery works on your LAN.

If `docker pull` fails with unauthorized / denied, the GHCR package may still be private. Package owners: open
[plex-cast-bridge package settings](https://github.com/users/andrewtryder/packages/container/plex-cast-bridge/settings),
set **Visibility → Public**, then retry the pull. A public GitHub repo does **not** automatically make its container package public.

## 4. Open the dashboard

Visit `http://<host>:8000/?api_key=<LOCAL_API_KEY>` once (the key is stripped from the URL after load). Prefer sending `X-API-Key` from scripts.

From the dashboard:

1. Settings gear → **Connections** → **Connect with Plex** (unless you already set `PLEX_TOKEN`)
2. Discover Cast speakers and enable the rooms you want
3. Play a test artist or album from the Smart DJ controls
4. Optional: **Settings → Playback** to set default/max volume (creates `./data/app_settings.json`)

Persistent speaker state, optional app settings, and catalog updates land in `./data/`.

## 5. Sync your Plex catalog (recommended)

```bash
uv run python scripts/sync_plex_catalog.py
```

This fills `catalog.json` / `data/catalog.json` with artists and genres from your library. Do not commit your personal catalog.

## Optional next step: Google Home voice

When the dashboard and Cast playback work, follow the advanced guide:

→ [Google Home & Cloud Run Setup Guide](setup/SETUP_GUIDE.md)

You will create a GCP project, deploy the Cloud Run gateway, create a Pub/Sub subscriber service account, and link a **test** integration in the Google Home app.
