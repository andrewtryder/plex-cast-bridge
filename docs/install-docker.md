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
| `GEMINI_API_KEY` | No | Needed for theme / mood mixes |
| `ENABLE_PUBSUB_LISTENER` | No | Leave `false` until Google Home is set up |
| `DATA_DIR` | No | Defaults to `/data` in Docker; stores `plex_connection.json`, optional `app_settings.json`, device keys |
| `DEFAULT_CAST_VOLUME` / `MAX_CAST_VOLUME` | No | Bootstrap volumes (0–1). Overridden after **Settings → Playback → Save** writes `app_settings.json` |

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

## 3. Start the bridge (Docker Compose)

The default `deploy/docker-compose.yml` is an **image-only deployment** configured to pull the verified official GHCR release channel (`ghcr.io/andrewtryder/plex-cast-bridge:latest`) without building local source:

```bash
docker compose -f deploy/docker-compose.yml pull
docker compose -f deploy/docker-compose.yml up -d
docker compose -f deploy/docker-compose.yml ps
```

Host networking is used so Chromecast mDNS discovery works on your LAN.

If `docker pull` fails with unauthorized / denied, the GHCR package may still be private. Package owners: open
[plex-cast-bridge package settings](https://github.com/users/andrewtryder/packages/container/plex-cast-bridge/settings),
set **Visibility → Public**, then retry the pull. A public GitHub repo does **not** automatically make its container package public.

### Developers and forks (building from source)

If you are modifying source code locally or maintaining a fork, include the build override:

```bash
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.build.yml up -d --build
```

---

## Alternative: Portainer Deployment

Plex Cast Bridge is fully compatible with Portainer. You can deploy it either as a **Git Repository Stack** or directly through the **Portainer Web Editor**.

### Method A: Portainer Git Repository Stack

1. In Portainer, navigate to **Stacks** → **Add stack**.
2. Select **Repository**.
3. Set **Repository URL**: `https://github.com/andrewtryder/plex-cast-bridge.git`
4. Set **Repository reference**: `refs/heads/main` (or a release tag)
5. Set **Compose path**: `deploy/docker-compose.portainer.yml`
6. In **Environment variables**, define:
   - `PCB_DATA_DIR`: **(Required)** a stable host path, e.g. `/opt/plex-cast-bridge/data` or `/var/lib/plex-cast-bridge/data`
   - (Optional) `PLEX_URL`, `PLEX_TOKEN`, `PLEX_MUSIC_SECTION`, `GEMINI_API_KEY`, etc.

> [!IMPORTANT]
> **Why `PCB_DATA_DIR` is required for Portainer Git Stacks:**
> Portainer clones Git repositories into an internal ephemeral directory (`/data/compose/<id>/`). If persistent storage defaults to relative paths within the checkout, redeploying the stack, pulling a newer commit, or modifying stack settings will wipe or detach the checkout directory. The supported `deploy/docker-compose.portainer.yml` template explicitly requires `PCB_DATA_DIR` and refuses to deploy without a stable host path, ensuring your Plex authentication, speaker IDs, Google Home sync state, settings, and saved mixes remain safe and persistent across stack redeployments.

Make sure the host data directory exists and is accessible before deploying:
```bash
sudo mkdir -p /opt/plex-cast-bridge/data
sudo chown -R 10001:10001 /opt/plex-cast-bridge/data
```

7. Click **Deploy the stack**.

### Method B: Portainer Web Editor (Stack or Custom Template)

If you prefer not to connect Portainer to Git, you can paste the stack definition directly into the Portainer Web Editor:

```yaml
services:
  plex-cast-bridge:
    image: ghcr.io/andrewtryder/plex-cast-bridge:latest
    container_name: plex-cast-bridge
    restart: unless-stopped
    network_mode: host
    environment:
      - PORT=8000
      - DATA_DIR=/data
      - PLEX_URL=http://192.168.1.50:32400
      - PLEX_TOKEN=your-token-if-not-using-web-login
      - PLEX_MUSIC_SECTION=Music
    volumes:
      # Stable persistent host bind mount (recommended for direct file access and simple backups):
      - /opt/plex-cast-bridge/data:/data
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

#### Using Named Volumes in Portainer

If you prefer Docker named volumes over host bind mounts:

```yaml
services:
  plex-cast-bridge:
    image: ghcr.io/andrewtryder/plex-cast-bridge:latest
    container_name: plex-cast-bridge
    restart: unless-stopped
    network_mode: host
    environment:
      - PORT=8000
      - DATA_DIR=/data
    volumes:
      - plex_cast_bridge_data:/data

volumes:
  plex_cast_bridge_data:
    name: plex-cast-bridge-data
```

> [!WARNING]
> **Preserve existing storage:**
> Never change an existing installation from a host bind mount (`./data` or `/opt/...`) to a named volume without copying your existing data first! A new named volume starts completely empty, causing the bridge to start with unlinked defaults. Existing users should keep their existing data mount.

---

## 4. Open the dashboard

Visit `http://<host>:8000/`. The dashboard and local APIs have no login: anyone who can reach them can control playback and use local administrative actions. Restrict access with a firewall, VPN, or a secured reverse proxy; do not expose the service directly to the public Internet.

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
