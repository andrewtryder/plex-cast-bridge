# Upgrading Plex Cast Bridge

An ordinary local upgrade replaces the bridge container image while preserving all persistent configuration and data. You do **not** need to reconnect Plex, relink Google Home, rediscover speakers, reset playback volume settings, or rebuild saved mixes when updating the image.

Routine updates for Docker Compose, Portainer, and automatic container update tools follow standard Docker workflows and **do not require running a complex migration script** when the existing installation is compatible.

---

## What Persistent State is Preserved

All state that distinguishes your installation from defaults is stored under the persistent data mount (`DATA_DIR`, mapped to `/data` in containers). Across image updates and container recreations, the following are preserved:

| Component | Persistent File | What is Preserved |
|---|---|---|
| **Plex Authentication** | `plex_connection.json`, `plex_device_*.pem` | Authorized Plex server URL, music section, client identifier, and Plex JWT tokens. No need to reconnect to plex.tv. |
| **Cast Speakers** | `catalog.json` | Physical Cast speaker UUIDs, stable virtual device IDs, custom friendly names, room hints, and enabled/disabled toggles. |
| **Google Home Sync** | `google_home_sync.json`, `google_home_setup.json` | Cloud Gateway URL, catalog write secret, sync timestamps, and project metadata. Google Home stays linked. |
| **App Settings** | `app_settings.json` | Custom UI settings, default playback volume, and max volume clamps. |
| **Music Mixes & Index** | `music_index.sqlite3` | SQLite database storing your indexed library tracks, custom mixes, prompts, and track ordering. |
| **Environment** | `.env` | Environment configuration, Pub/Sub settings, and API keys. |

---

## Routine Docker Upgrades

### 1. Docker Compose (Standard CLI)

To update an existing installation deployed with `deploy/docker-compose.yml`:

```bash
cd ~/plex-cast-bridge

# 1. (Recommended) Take a quick snapshot backup of your persistent data
tar -czf "plex-data-backup-$(date +%Y%m%d%H%M%S).tar.gz" data/

# 2. Pull the latest verified official release from GHCR
docker compose -f deploy/docker-compose.yml pull

# 3. Recreate the container with the updated image
docker compose -f deploy/docker-compose.yml up -d

# 4. Verify container health
docker compose -f deploy/docker-compose.yml ps
```

The container restarts in seconds with the new image, immediately reconnecting to your existing `/data` mount.

---

### 2. Portainer Deployment Upgrades

#### Updating a Portainer Web-Editor Stack or Template
1. In Portainer, navigate to **Stacks** and select your `plex-cast-bridge` stack.
2. Click **Editor** (or **Update the stack**).
3. Toggle **Re-pull image** to **ON**.
4. Click **Update the stack**.
5. Portainer pulls the newest image from GHCR, stops the old container, and starts the new container attached to the same persistent volume or bind mount.

#### Updating an Individual Container in Portainer
1. Go to **Containers** → click on `plex-cast-bridge`.
2. Click **Recreate** in the top action bar.
3. Enable the **Re-pull image** toggle.
4. Click **Recreate**.

#### Updating a Portainer Git-Managed Stack
If you deployed the stack using Portainer's **Repository** option:
1. Navigate to **Stacks** → select your stack.
2. Click **Update the stack**.
3. Toggle **Re-pull image** to **ON**.
4. Click **Update**.

> [!IMPORTANT]
> **Portainer Git-stack persistent storage requirement:**
> Ensure your Git stack defines `PCB_DATA_DIR` in its **Environment variables** pointing to a permanent host path (e.g. `PCB_DATA_DIR=/opt/plex-cast-bridge/data`).
> Portainer clones Git repositories to an internal directory (`/data/compose/<id>/`) that can be wiped or replaced upon Git pull or stack update. Storing persistent data outside the checkout ensures your settings survive Git updates.

---

## Automatic-Update Behavior

When a new verified image is published to `ghcr.io/andrewtryder/plex-cast-bridge:latest`, container environments behave as follows:

| Deployment Method | Automatic Update Behavior | Action Required |
|---|---|---|
| **Manual Docker Compose** | Does **not** update automatically. The running container continues on its current image digest indefinitely. | Run `docker compose pull && docker compose up -d` when you choose to update. |
| **Portainer (Manual Stacks)** | Does **not** update automatically. Publishing an image to GHCR does not notify Portainer. | Trigger update manually via **Update the stack** with **Re-pull image**. |
| **Portainer Git-stack Polling** | Polling checks for **Git repository commits**, *not* image registry updates. A new image tag published without a git commit will not trigger a stack redeploy. | Re-pull manually or configure a webhook. |
| **Portainer Webhooks** | If you configured a Portainer stack webhook, triggering the webhook endpoint causes Portainer to re-pull the image and redeploy the stack. | Automated via your CI or trigger script. |
| **Automatic Tools (Watchtower, Ouroboros)** | Automatically detects image digest changes on GHCR, pulls the candidate, and restarts the container with existing flags and mounts. | Fully automated. See unattended limitations below. |
| **Version-Pinned (`:0.3.0`)** | Does **not** update when `:latest` changes. Pinned strictly to the specified release. | Change the tag in your compose file when ready. |
| **Digest-Pinned (`@sha256:...`)** | Completely immutable. Immune to tag movements or registry modifications. | Update digest explicitly after review. |

### Limitations & Risks of Unattended Automatic Updates (Watchtower)
While automatic tools like Watchtower provide hands-off convenience:
- **No Automatic Backups**: Unattended updates do **not** take a snapshot of `/data` before recreation.
- **No Automated Rollback**: If a newer version contains an unexpected regression or incompatible dependency, automatic tools cannot roll back automatically.
- **Recommended Practice**: If you run Watchtower, configure an automated daily/weekly backup of your host data directory (`cron` + `tar`), or pin your stack to a specific version tag.

---

## Simple Image Rollback

If a newly deployed image introduces an issue or regression, rolling back is straightforward:

1. Edit your `docker-compose.yml` or Portainer stack definition to pin the previous known-good version tag or digest:
   ```yaml
   image: ghcr.io/andrewtryder/plex-cast-bridge:0.2.1
   ```
   or using an immutable digest:
   ```yaml
   image: ghcr.io/andrewtryder/plex-cast-bridge@sha256:b0db066ed210255039515602dbe3759f697203f42b2b2a39214835dbc2b83422
   ```
2. Recreate the container:
   ```bash
   docker compose -f deploy/docker-compose.yml up -d
   ```
3. Because all settings and mixes remain in your persistent `/data` mount, the previous image starts immediately with your data intact.

---

## Unsupported or Risky Installation Layouts

To avoid unexpected data loss during updates, avoid these configurations:

1. **Unmounted `/data`**: Running the container without mounting `/data` causes all mutable state to be written to the container's overlay layer. Recreating the container will wipe all credentials and mixes.
2. **Ephemeral Git checkout storage**: Storing data inside Portainer's internal Git checkout directory without setting `PCB_DATA_DIR` to a permanent host path.
3. **Silent volume switching**: Switching an existing deployment from a host bind mount (`./data`) to a named volume (`plex_data:/data`) without copying your files first. Docker named volumes initialize empty and will cause the bridge to start unlinked.
4. **Incorrect file permissions**: If the host data directory has root-only permissions (`0700`), the container's non-root `appuser` (UID `10001`) cannot write to it, causing startup or save errors. Ensure the host folder is owned by or writable by UID 10001:
   ```bash
   sudo chown -R 10001:10001 /path/to/data
   ```

---

## Developer and Fork Workflows

- **Building from local source**: Developers who clone the repository and modify source code can build locally using the build override:
  ```bash
  docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.build.yml up -d --build
  ```
- **Custom GHCR images / Forks**: Set `image:` in your Compose file to your custom repository (e.g. `ghcr.io/yourusername/plex-cast-bridge:latest`). The image-only deployment works identically with any compliant container image.

---

## Native Python Installations (Non-Docker)

Native installations running directly on host Python (outside Docker) require Python 3.14 (`requires-python = ">=3.14,<3.15"`).
- **With `uv`**: Run `uv sync` in the repository. `uv` automatically downloads and uses a standalone Python 3.14 runtime without modifying system packages.
- **Without `uv`**: Systems with host Python older than 3.14 (e.g. Debian 12 / Ubuntu 24.04) cannot run the bridge natively and must install Python 3.14 or switch to the supported Docker container.

---

## Google Home and Cloud Run

Cloud Run voice gateway updates are completely independent of local Docker container upgrades. Existing Google Home users should keep their current Cloud Run service, secrets, Firestore records, and linked devices. You do **not** need to redeploy Cloud Run or relink Google Home when upgrading your local bridge container.

---

## Optional: Script-Assisted Reviewed Upgrade Workflow

For enterprise or Proxmox LXC environments requiring offline quiesced backup verification, preflight doctor checks, and immutable digest review before application, the script-assisted tooling in `scripts/upgrade.py` remains available:

```bash
# Optional preflight diagnostic
python3 scripts/upgrade.py doctor --compose-file deploy/docker-compose.yml --check-updates
```

See [persistent-state.md](persistent-state.md) and [proxmox-deployment.md](proxmox-deployment.md) for specialized Proxmox LXC scripts.
