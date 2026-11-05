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

## 2. Proxmox Virtual Machine Sizing & Setup

Per Proxmox best practices, application containers should run inside a dedicated Linux VM rather than directly on the hypervisor host.

### Recommended VM Sizing
- **OS:** Debian 12 (Bookworm), Debian 13 (Trixie), or Ubuntu Server 24.04 LTS
- **vCPU:** 2 Cores
- **RAM:** 2048 MB (2 GB)
- **Disk:** 16–20 GB (VirtIO SCSI, discard enabled)
- **Network Interface:** `vmbr0` (Bridged to your primary home LAN)

> [!IMPORTANT]
> **Host Networking & Bridging:** Ensure the VM's virtual NIC connects to `vmbr0` so it shares the same broadcast domain as your Google Cast devices. The Docker container runs with `network_mode: host` to allow Chromecast mDNS zero-configuration discovery (`_googlecast._tcp.local.`).

---

## 3. Install Docker Engine & Compose on the VM

SSH into your newly created Debian/Ubuntu VM:

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

# Grant your user docker execution rights
sudo usermod -aG docker $USER
newgrp docker
```

---

## 4. Directory Structure & Persistent Data

Plex Cast Bridge stores mutable state (speaker enable/disable preferences, room assignments, and dead-letter queue records) in a dedicated `/data` directory outside the container image.

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
├── data/                    # Persistent volume mount
│   ├── catalog.json         # Speaker metadata and toggles (persisted across container updates)
│   └── dead_letter_queue.jsonl # Poison pill command audit log
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
cat << 'EOF' > ~/plex-cast-bridge/.env
# Plex Media Server Settings
PLEX_URL=http://192.168.1.50:32400
PLEX_TOKEN=your_plex_x_token_here
PLEX_MUSIC_SECTION=Music

# Target Speaker & Volume Defaults
DEFAULT_SPEAKER_NAME=Kitchen
DEFAULT_SPEAKER_HOST=
DEFAULT_CAST_VOLUME=0.20

# Google Gemini SmartDJ API Key
GEMINI_API_KEY=your_gemini_api_key_here

# Local API & Web Dashboard Authentication
LOCAL_API_KEY=generate_with_openssl_rand_hex_32

# Google Cloud Platform Pub/Sub Settings
GCP_PROJECT_ID=plex-cast-bridge-home
GCP_PUBSUB_SUBSCRIPTION_ID=plex-commands-local-worker

# Operational listener flag (keep false until manual verification passes)
ENABLE_PUBSUB_LISTENER=false
EOF
chmod 600 ~/plex-cast-bridge/.env
```

Generate a secure random key for `LOCAL_API_KEY`:
```bash
openssl rand -hex 32
```

### 3. Copy GCP Service Account Credentials
From your **Mac terminal**, securely copy `gcp-credentials.json` to the VM:

```bash
scp ~/code/plex-cast-bridge/gcp-credentials.json user@<PROXMOX_VM_IP>:~/plex-cast-bridge/
```

On the VM, restrict permissions:
```bash
chmod 600 ~/plex-cast-bridge/gcp-credentials.json
```

---

## 6. Running the Service

The project publishes official multi-architecture (`linux/amd64` and `linux/arm64`) images to GitHub Container Registry (**GHCR**). You can pull and run directly without needing local toolchain compilation on your VM.

```bash
cd ~/plex-cast-bridge

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
http://<PROXMOX_VM_IP>:8000/?api_key=<LOCAL_API_KEY>
```

- Confirm your Google Cast speakers (e.g. `Kitchen`, `Master Bedroom`) appear in the active speaker list.
- Try toggling a speaker on/off and confirm the change persists to `data/catalog.json`.

### Step 2: Trigger a Direct Test Command via API
From your terminal, test direct playback on your Kitchen speaker:

```bash
curl -X POST "http://<PROXMOX_VM_IP>:8000/api/command" \
  -H "X-API-Key: <LOCAL_API_KEY>" \
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
3. Restart the container:
   ```bash
   docker compose -f deploy/docker-compose.yml restart
   ```
4. Speak to your Google Home / Nest device:
   > *"Hey Google, play Pink Floyd on Plex in the Kitchen"*
5. Confirm the playback starts immediately!

---

## 8. Network Troubleshooting & Multi-VLAN Setup

1. **Plex Reachability from Speakers:**
   - Google Cast speakers fetch audio streams directly from `PLEX_URL`. Ensure your Cast speakers can connect to `http://<PLEX_IP>:32400` across your local firewall.
2. **mDNS Reflection Across VLANs:**
   - If your Cast devices reside on an IoT VLAN and the Proxmox VM is on a trusted server VLAN, install an mDNS reflector like `avahi-daemon` on your router/gateway to reflect `_googlecast._tcp.local.` traffic across subnets.
3. **Static Speaker Host Fallback:**
   - If mDNS is filtered on your network, set `DEFAULT_SPEAKER_HOST=192.168.1.X` in `.env` to connect directly via IP.
4. **Dead-Letter Queue Inspection:**
   - If a voice command fails persistently, inspect `data/dead_letter_queue.jsonl`:
     ```bash
     cat ~/plex-cast-bridge/data/dead_letter_queue.jsonl
     ```
