# Deploying Plex Cast Bridge on Proxmox VE

This guide walks through deploying **Plex Cast Bridge** on a dedicated Linux Virtual Machine (VM) within [Proxmox VE](https://www.proxmox.com/).

By offloading the 24/7 Cast orchestrator and Google Home Pub/Sub listener to Proxmox, you keep your home audio automation running continuously while retaining your Mac as a personal workstation, audio emulator, and development environment.

---

## 1. Architecture & Role Separation

```
[ Google Assistant / Nest Speaker / Home App ]
                       │
                       │ 1. Voice query (Cloud-to-Cloud)
                       ▼
         [ GCP Cloud Run Fulfillment ]
                       │
                       │ 2. Publishes command
                       ▼
           [ GCP Pub/Sub Topic ]
                       │
                       │ 3. StreamingPull (Outbound HTTPS only)
                       ▼
       ┌───────────────────────────────┐
       │     Proxmox VE Hypervisor     │
       │  ┌─────────────────────────┐  │
       │  │ Debian / Ubuntu Linux VM│  │
       │  │ ┌─────────────────────┐ │  │
       │  │ │ Docker Compose      │ │  │
       │  │ │ • Pub/Sub Worker    │ │  │
       │  │ │ • Gemini SmartDJ    │ │  │
       │  │ │ • Cast Controller   │ │  │
       │  │ │ • Web Dashboard     │ │  │
       │  │ └─────────┬───────────┘ │  │
       │  └───────────┼─────────────┘  │
       └──────────────┼────────────────┘
                      │ 4. Direct LAN Discovery & Casting
         ┌────────────┴────────────┐
         ▼                         ▼
 [ Plex Media Server ]    [ Google Cast Speakers ]
   http://192.168.1.50      (Kitchen, Living Room, etc.)
```

### Environment Comparison

| Capability | Mac (Local Dev & Testing) | Proxmox VM (24/7 Production) |
| :--- | :--- | :--- |
| **Primary Role** | Development, code authoring, and audio emulation | 24/7 always-on speaker orchestrator |
| **Plex Library Resolution** | Yes | Yes |
| **Gemini Intent Routing** | Yes | Yes |
| **Google Cast Streaming** | Yes | Yes |
| **Computer Audio Output** | Yes (audible output via macOS `afplay`) | Headless (directs output to Cast speakers) |
| **Google Home Pub/Sub Worker**| Available for testing (`ENABLE_PUBSUB_LISTENER`) | **Active 24/7** |
| **Deployment Mode** | Native Python via `uv` | Docker Compose (containerized non-root) |

> [!WARNING]
> **Single Pub/Sub Consumer Rule:** Never run `ENABLE_PUBSUB_LISTENER=true` simultaneously on both your Mac and Proxmox. Google Cloud Pub/Sub load-balances messages across all active subscribers on the subscription; running both concurrently will cause commands to split randomly between your Mac and the server.

---

## 2. Proxmox Host Sizing & Setup (VM or Unprivileged LXC)

Plex Cast Bridge can be deployed either in a dedicated Linux Virtual Machine (VM) or inside an **unprivileged Linux Container (LXC)** on Proxmox VE.

### Option A: Dedicated Linux VM
- **OS:** Debian 12 (Bookworm), Debian 13 (Trixie), or Ubuntu Server 24.04 LTS
- **vCPU:** 2 Cores
- **RAM:** 2048 MB (2 GB)
- **Disk:** 16–20 GB (VirtIO SCSI, discard enabled)
- **Network Interface:** `vmbr0` (Bridged to your primary home LAN)

### Option B: Unprivileged LXC Container (Lightweight & Recommended)
Running Docker inside an **unprivileged Debian LXC** provides container isolation with minimal RAM and CPU overhead (~50 MB base RAM):
1. Create a Debian 12 (Bookworm) or 13 (Trixie) LXC container.
2. In Proxmox Web UI, navigate to **LXC -> Options -> Features** and enable:
   - **Nesting** (`nesting=1`) — Required for Docker daemon inside LXC.
   - **Keyctl** (`keyctl=1`) — Required for Docker credential storage and systemd.
   *(Alternatively, verify `features: nesting=1,keyctl=1` in `/etc/pve/lxc/<CTID>.conf` on your Proxmox node).*
3. **Security Note:** You do **not** need to convert the container to privileged (`privileged=1`) or disable AppArmor. On modern Proxmox kernels (PVE 7.x / 8.x / 9.x), Docker Engine with the overlayfs storage driver works cleanly in unprivileged LXC with non-root mapped UIDs.

> [!IMPORTANT]
> **Host Networking & Subnet Alignment:** Ensure the VM or LXC network interface connects to `vmbr0` so it shares the same broadcast domain and IP subnet as your Google Cast devices and Plex Media Server. Docker Compose runs with `network_mode: host` to allow zero-configuration Chromecast mDNS discovery (`_googlecast._tcp.local.`) without NAT traversal barriers.

---

## 3. Install Docker Engine & Compose on the Host

SSH into your newly created Debian/Ubuntu host (VM or LXC):

```bash
# Update repositories and install prerequisite tools
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg

# Add Docker's official GPG key
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

# Add Docker repository to Apt sources
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker Engine and the Docker Compose plugin
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Grant your user docker execution rights (if not root)
sudo usermod -aG docker $USER
newgrp docker
```

---

## 4. Directory Structure & Persistent Data

Plex Cast Bridge stores mutable state (speaker enable/disable preferences, room assignments, Plex link credentials, and dead-letter queue records) in a dedicated host directory mounted into the container at `/data`. Compose resolves `../data` relative to [`deploy/docker-compose.yml`](../deploy/docker-compose.yml), so with the layout below that is `~/plex-cast-bridge/data`. Treat `/data` as required persistence — not optional scratch space — so Settings → Connect with Plex and speaker toggles survive image rebuilds and `--force-recreate`.

On the VM, create the project directory:

```bash
mkdir -p ~/plex-cast-bridge/data
cd ~/plex-cast-bridge
```

Set appropriate directory ownership for the container's non-root `appuser` (UID `10001`):

```bash
sudo chown -R 10001:10001 ~/plex-cast-bridge/data
```

### Files Directory Layout
```
~/plex-cast-bridge/
├── .env                     # Runtime secrets and connection details
├── gcp-credentials.json     # Google Cloud Service Account credentials
├── data/                    # Persistent volume mount (DATA_DIR=/data)
│   ├── catalog.json         # Speaker metadata and toggles (persisted across container updates)
│   ├── dead_letter_queue.jsonl # Poison pill command audit log
│   ├── plex_connection.json # Plex link status + JWT/access tokens (mode 0600; never expose via API)
│   ├── plex_device_private.pem # Device key material for Plex JWT login (mode 0600)
│   └── plex_device_public.pem  # Matching public key for Plex JWT login (mode 0600)
└── deploy/
    └── docker-compose.yml   # Compose orchestrator configuration
```

---

## 5. Configuration & Secret Provisioning

### 1. Download Compose Configuration
You can either clone the git repository or copy the `deploy/docker-compose.yml` file:

```bash
git clone https://github.com/andrewtryder/plex-cast-bridge.git ~/plex-cast-bridge-repo
cp -r ~/plex-cast-bridge-repo/deploy ~/plex-cast-bridge/
```

### 2. Configure `.env`
Create `.env` inside `~/plex-cast-bridge/`:

```bash
cat << EOF > ~/plex-cast-bridge/.env
# Plex Media Server Settings (optional if you use Settings → Connect with Plex)
PLEX_URL=http://192.168.1.50:32400
PLEX_TOKEN=your_plex_x_token_here
PLEX_MUSIC_SECTION=Music

# Target Speaker & Volume Defaults
DEFAULT_SPEAKER_NAME=Kitchen
DEFAULT_SPEAKER_HOST=
DEFAULT_CAST_VOLUME=0.20

# Google Gemini SmartDJ API Key
GEMINI_API_KEY=your_gemini_api_key_here

# Google Cloud Platform Pub/Sub Settings (optional until Google Home voice is enabled)
GCP_PROJECT_ID=<your-project-id>
GCP_PUBSUB_SUBSCRIPTION_ID=plex-commands-local-worker
ENABLE_PUBSUB_LISTENER=true
EOF
chmod 600 ~/plex-cast-bridge/.env
```

> [!IMPORTANT]
> The dashboard and local APIs have no login: anyone who can reach them can control playback and use local administrative actions. Restrict access with a firewall, VPN, or a secured reverse proxy; do not expose the service directly to the public Internet.

### 3. Copy GCP Service Account Credentials

Create a dedicated **Pub/Sub subscriber** service account (not the Cloud Run publisher). See [SETUP_GUIDE.md §5](setup/SETUP_GUIDE.md). From your workstation:

```bash
scp ./gcp-credentials.json user@<PROXMOX_VM_IP>:~/plex-cast-bridge/
```

For dashboard-only installs (no voice yet), a placeholder is enough:

```bash
echo '{}' > ~/plex-cast-bridge/gcp-credentials.json
```

On the VM, configure permissions so the container's non-root `appuser` (GID `10001`) can read the credentials while preventing world-read:
```bash
sudo chown root:10001 ~/plex-cast-bridge/gcp-credentials.json
sudo chmod 640 ~/plex-cast-bridge/gcp-credentials.json
```

---

## 6. Running the Service

The project publishes official multi-architecture (`linux/amd64` and `linux/arm64`) images to GitHub Container Registry (**GHCR**). You can pull and run directly without needing local toolchain compilation on your VM.

```bash
cd ~/plex-cast-bridge

# If your GHCR package is private, authenticate first with a GitHub Personal Access Token (PAT):
# echo $GHCR_TOKEN | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin

# Pull the pre-built multi-arch image from GHCR
docker compose -f deploy/docker-compose.yml pull

# Start the service in detached mode
docker compose -f deploy/docker-compose.yml up -d

# Inspect startup logs
docker compose -f deploy/docker-compose.yml logs -f
```

### Check Container Health Status
The container includes a built-in health check testing `http://localhost:8000/health`:

```bash
docker compose -f deploy/docker-compose.yml ps
```

You should see:
```
NAME                IMAGE                                           COMMAND                  SERVICE             CREATED         STATUS                   PORTS
plex-cast-bridge    ghcr.io/andrewtryder/plex-cast-bridge:latest   "uvicorn plex_cast_b…"   plex-cast-bridge    10 seconds ago   Up 9 seconds (healthy)
```

---

## 7. Verification & Cutover to Always-On Voice Control

### Step 1: Open the Web Dashboard
From any browser on your home LAN, open:
```
http://<BRIDGE_IP>:8000/
```
The local API is available to clients on the trusted network without a key.

- Confirm your Google Cast speakers appear under **Speakers**.
- Toggle **Playback** and **Show in Google Home** independently; changes persist in `data/catalog.json` with stable Google Home device IDs.
- After image upgrades, keep the `data/` volume mounted — speaker settings, Plex JWT, and cloud credentials migrate in place without rerunning the Google Home wizard.

### Step 2: Trigger a Direct Test Command via API
From your terminal, test direct playback on your Kitchen speaker:

```bash
curl -X POST "http://<PROXMOX_HOST_IP>:8000/api/command" \
  -H "Content-Type: application/json" \
  -d '{"query":"DJ Shadow","speaker":"Kitchen"}'
```

Verify that:
1. Gemini routes the query to an artist radio / album session.
2. The Kitchen Google Home speaker plays the audio track from Plex.

### Step 3: Transition Voice Control from Mac to Proxmox
1. On your **Mac**, ensure any running instance of `plex-cast-bridge` has `ENABLE_PUBSUB_LISTENER=false` or is stopped.
2. On your **Proxmox VM**, edit `.env`:
   ```bash
   sed -i 's/ENABLE_PUBSUB_LISTENER=false/ENABLE_PUBSUB_LISTENER=true/' ~/plex-cast-bridge/.env
   ```
3. Recreate the container to apply the updated environment variables (note: `restart` does not reload `.env`):
   ```bash
   docker compose -f deploy/docker-compose.yml up -d --force-recreate
   ```
4. Speak to your Google Home / Nest device:
   > *"Hey Google, play Pink Floyd on Plex in the Kitchen"*
5. Confirm the playback starts immediately!

---

## 8. Network Troubleshooting & Resilience

1. **Plex Reachability from Speakers:**
   - Google Cast speakers fetch audio streams directly from `PLEX_URL`. Ensure your Cast speakers can connect to `http://<PLEX_IP>:32400` across your local firewall and subnet boundaries.
2. **Resilient Direct IP Fallback (WiFi/Mesh mDNS Filtering):**
   - Certain home mesh routers, APs, or managed switches filter or drop mDNS multicast packets (`224.0.0.251:5353`) crossing between wired Proxmox bridges and wireless APs.
   - `CastManager` automatically uses known IP addresses recorded in `data/catalog.json` as a direct fallback whenever dynamic mDNS discovery misses a target speaker, providing instant unicast TCP (`8009`) connections.
3. **Headless Linux Audio Abstraction:**
   - On macOS, Plex Cast Bridge uses native `afplay` for the local test speaker.
   - On Linux hosts (such as your Proxmox Debian LXC), the service operates in headless mode: it never invokes `afplay`, does not clutter speaker discovery with non-existent audio devices, and channels all playback directly to your physical Google Cast speakers. To enable headless simulation testing without physical speakers, set `ENABLE_TEST_SPEAKER=true` in `.env`.
4. **Static Speaker Host Fallback:**
   - If a specific speaker cannot be discovered dynamically, set `DEFAULT_SPEAKER_HOST=<speaker-ip>` in `.env` to route all unspecified speaker requests directly to that IP.
5. **Dead-Letter Queue Inspection:**
   - If a voice command fails persistently, inspect `data/dead_letter_queue.jsonl`:
     ```bash
     cat ~/plex-cast-bridge/data/dead_letter_queue.jsonl
     ```
