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

# 1. Stop active writers and take a consistent snapshot backup
docker compose -f deploy/docker-compose.yml stop
tar -czf "plex-data-backup-$(date +%Y%m%d%H%M%S).tar.gz" data/
docker compose -f deploy/docker-compose.yml up -d

# 2. Pull the latest verified official release from GHCR
docker compose -f deploy/docker-compose.yml pull

# 3. Recreate the container with the updated image
docker compose -f deploy/docker-compose.yml up -d

# 4. Verify container health
docker compose -f deploy/docker-compose.yml ps
```

> [!IMPORTANT]
> **Safe SQLite backup practice:**
> `music_index.sqlite3` uses SQLite Write-Ahead Logging (WAL). Archiving (`tar`) or copying `/data` while the bridge is actively running can result in torn pages or an incomplete snapshot if writes are in-flight. Always stop the container before archiving, or use SQLite's online backup API:
> ```bash
> sqlite3 data/music_index.sqlite3 ".backup 'data/music_index.backup.sqlite3'"
> ```

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
2. Set Compose path to `deploy/docker-compose.portainer.yml` (or `deploy/docker-compose.yml`).
3. Click **Update the stack**.
4. Toggle **Re-pull image** to **ON**.
5. Click **Update**.

> [!IMPORTANT]
> **Portainer Git-stack persistent storage requirement:**
> Ensure your Git stack defines `PCB_DATA_DIR` in its **Environment variables** pointing to a permanent host path (e.g. `PCB_DATA_DIR=/opt/plex-cast-bridge/data`).
> Portainer clones Git repositories to an internal directory (`/data/compose/<id>/`) that can be wiped or replaced upon Git pull or stack update. The supported `deploy/docker-compose.portainer.yml` template explicitly requires `PCB_DATA_DIR` and will refuse to deploy without it, ensuring settings survive Git stack updates.
>
> **Fail-closed upgrade protection (`PCB_REQUIRE_EXISTING_DATA`):**
> When upgrading an existing Portainer stack, add `PCB_REQUIRE_EXISTING_DATA=true` to your stack environment variables. If an operator accidentally provides an invalid path or a typo (e.g. `/opt/pcb/data` instead of `/opt/plex-cast-bridge/data`), the container will fail fast on startup with a descriptive error instead of silently initializing with blank defaults.

---

## Automatic-Update Behavior

When a new verified image is published to `ghcr.io/andrewtryder/plex-cast-bridge:latest`, container environments behave as follows:

| Deployment Method | Automatic Update Behavior | Action Required |
|---|---|---|
| **Manual Docker Compose** | Does **not** update automatically. The running container continues on its current image digest indefinitely. | Run `docker compose pull && docker compose up -d` when you choose to update. |
| **Portainer (Manual Stacks)** | Does **not** update automatically. Publishing an image to GHCR does not notify Portainer. | Trigger update manually via **Update the stack** with **Re-pull image**. |
| **Portainer Git-stack Polling** | Polling checks for **Git repository commits**, *not* image registry updates. A new image tag published without a git commit will not trigger a stack redeploy. | Re-pull manually or configure a webhook. |
| **Portainer Webhooks** | If you configured a Portainer stack webhook, triggering the webhook endpoint causes Portainer to re-pull the image and redeploy the stack. | Automated via your CI or trigger script. |
| **Automatic Tools (Watchtower, Keel)** | Automatically detects image digest changes on GHCR, pulls the candidate, and restarts the container with existing flags and mounts. | Fully automated. See unattended limitations below. |
| **Version-Pinned (`:0.3.0`)** | Does **not** update when `:latest` changes. Pinned strictly to the specified release. | Change the tag in your compose file when ready. |
| **Digest-Pinned (`@sha256:...`)** | Completely immutable. Immune to tag movements or registry modifications. | Update digest explicitly after review. |

### Limitations & Risks of Unattended Automatic Updates (Watchtower)
While automatic tools like Watchtower provide hands-off convenience:
- **No Automatic Backups**: Unattended updates do **not** take a quiesced snapshot of `/data` before recreation.
- **No Automated Rollback**: If a newer version contains an unexpected regression or schema upgrade, automatic tools cannot roll back automatically.
- **Recommended Practice**: If you run Watchtower, configure an automated daily/weekly backup of your host data directory (`cron` + quiesced snapshot), or pin your stack to a specific version tag.

---

## Simple Image Rollback & Limitations

If a newly deployed image introduces an issue or regression, rolling back the container image is straightforward:

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

> [!WARNING]
> **Database schema migration limitation:**
> Rolling back only the container image reverts the application code. However, an older image may not understand or run with database schemas updated by a newer version in `music_index.sqlite3`.
> If the older image fails on startup or reports database errors after an image rollback, you must restore your pre-upgrade database backup. Never extract a database backup over a running container.

---

## Reviewed Upgrade and Data Recovery Procedures

For mission-critical, enterprise, or Proxmox LXC environments, the reviewed upgrade CLI in `scripts/upgrade.py` provides preflight checks, quiesced snapshots, verification, and full data recovery:

### 1. Preflight Doctor Check
Verify current and target releases, persistent mounts, and configuration drift before updating:
```bash
python3 scripts/upgrade.py doctor --compose-file deploy/docker-compose.yml --check-updates
```

### 2. Managed Image Selection
Prepare and confirm a managed Compose override:
```bash
python3 scripts/upgrade.py prepare-compose-migration --compose-file deploy/docker-compose.yml --service plex-cast-bridge --review-file /path/to/review.json
python3 scripts/upgrade.py migrate-compose --review-file /path/to/review.json --confirm
```

### 3. Review Plan and Apply
Generate a plan with an offline quiesced backup and apply the verified update:
```bash
python3 scripts/upgrade.py plan --compose-file deploy/docker-compose.yml --compose-file deploy/docker-compose.override.yml --service plex-cast-bridge --target 'ghcr.io/andrewtryder/plex-cast-bridge@sha256:<digest>' --plan-file /path/to/upgrade-review.json
python3 scripts/upgrade.py apply --plan-file /path/to/upgrade-review.json --confirm-exclusive-data
```

### 4. Post-Upgrade Verification
Run live online SQLite snapshot verification and container health diagnostics:
```bash
python3 scripts/upgrade.py verify --plan-file /path/to/upgrade-review.json
```

### 5. Staged Restore and Data Recovery
If image rollback fails due to schema incompatibilities, inspect or restore the pre-upgrade backup:
```bash
# Safely inspect backup without changing active data:
mkdir -p -m 700 /path/to/restore-review
python3 scripts/upgrade.py stage-restore --plan-file /path/to/upgrade-review.json --destination /path/to/restore-review/staged-data

# Restore data and restart previous image:
python3 scripts/upgrade.py restore-data --plan-file /path/to/upgrade-review.json --confirm-overwrite-data --confirm-exclusive-data
```

---

## Unsupported or Risky Installation Layouts

To avoid unexpected data loss during updates, avoid these configurations:

1. **Unmounted `/data`**: Running the container without mounting `/data` causes mutable state to be written to the container's overlay layer. The bridge detects unmounted `/data` in container environments and will refuse to start to prevent silent data loss.
2. **Ephemeral Git checkout storage**: Storing data inside Portainer's internal Git checkout directory without setting `PCB_DATA_DIR` to a permanent host path. Use `deploy/docker-compose.portainer.yml` which enforces this requirement.
3. **Silent volume switching**: Switching an existing deployment from a host bind mount (`./data`) to a named volume (`plex_data:/data`) without copying files first. Docker named volumes initialize empty and will cause the bridge to start unlinked.
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
  Local builds are tagged as `plex-cast-bridge:local` by default to prevent colliding with or overwriting the official release image.
- **Custom GHCR images / Forks**: Set `PCB_CUSTOM_IMAGE` (or `image:` in your Compose file) to your custom repository (e.g. `PCB_CUSTOM_IMAGE=ghcr.io/yourusername/plex-cast-bridge:latest`). The image-only deployment works identically with any compliant container image.

---

## Native Python Installations (Non-Docker)

Native installations running directly on host Python (outside Docker) require Python 3.14 (`requires-python = ">=3.14,<3.15"`).
- **With `uv`**: Run `uv sync` in the repository. `uv` automatically downloads and uses a standalone Python 3.14 runtime without modifying system packages.
- **Without `uv`**: Systems with host Python older than 3.14 (e.g. Debian 12 / Ubuntu 24.04) cannot run the bridge natively and must install Python 3.14 or switch to the supported Docker container.

---

## Google Home and Cloud Run

Cloud Run voice gateway updates are completely independent of local Docker container upgrades. Existing Google Home users should keep their current Cloud Run service, secrets, Firestore records, and linked devices. You do **not** need to redeploy Cloud Run or relink Google Home when upgrading your local bridge container.
