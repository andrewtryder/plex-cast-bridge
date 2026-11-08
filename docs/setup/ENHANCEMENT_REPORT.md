# Google Home Wizard Enhance — implementation report

Date: 2026-11-08
Branch work: Phase 2–10 delivered as cohesive modules on top of the existing FastAPI + Cloud Run stack.

## Architecture summary

- Local bridge remains the Cast/Plex orchestrator.
- Cloud Run fulfillment now accepts versioned catalog uploads (`PUT /internal/catalog`), stores them in GCS (+ Firestore meta; mockable in tests), serves SYNC from cloud catalog, and calls HomeGraph Request Sync (debounced; stopped after DISCONNECT).
- Settings → Google Home wizard stores **non-secret** resumable state in `DATA_DIR/google_home_setup.json`.
- Admin provisioning uses `scripts/gh_setup_companion.py` + `gcp_provision` (ADC on the admin machine). Subscriber SA keys stay on the bridge; admin Google credentials do not.

## Fresh recreate status

Documented in [FRESH_RECREATE.md](FRESH_RECREATE.md).

Performed wipe (kept project ID `plex-cast-bridge-home` because GCP IDs are not reusable):

- Deleted Cloud Run `plex-google-home-fulfillment`
- Deleted Pub/Sub topic/sub/DLQ
- Deleted SA `plex-fulfillment-sa`
- Disabled `ENABLE_PUBSUB_LISTENER` on LXC 112; preserved Plex JWT + catalog

Recreate continues via wizard + companion + Developer Console (manual) + Home app link (manual).

## Features by phase

| Phase | Status |
|---|---|
| 2 Catalog sync + Request Sync | Implemented (mock-tested; live HomeGraph after redeploy + link) |
| 3 Google Home wizard UI/API | Implemented (resumable steps, preview, console values, sync, test) |
| 4 Tutorials + Grok Bot | Manifest + drawer + [GROK_BOT_TEACH_WORKFLOW.md](GROK_BOT_TEACH_WORKFLOW.md); Google videos `needs_capture` |
| 5 Player enhancements | Artwork proxy path, Up Next + queue APIs, library browse/play, group labeling, max volume |
| 6 Diagnostics | `/api/diagnostics` + Settings panel |
| 7 Backup | `/api/backup` non-secret zip |
| 8–9 Settings IA + Proxmox upgrade | Settings tabs; `scripts/proxmox_upgrade.sh` |
| 10 Tests | `64 passed` |

## Backend endpoints added/changed

- Cloud: `PUT /internal/catalog`, `POST /internal/request-sync`; SYNC/DISCONNECT updated
- Bridge: `/api/google-home/*`, `/api/artwork/{rating_key}`, `/api/queue`, `/api/library/browse`, `/api/library/play`, `/api/diagnostics`, `/api/backup`
- Status art URLs no longer emit `X-Plex-Token` when a rating key proxy is available

## Dependencies

- `google-cloud-storage` added to cloud-gateway group (export requirements before Cloud Run deploy)

## Tests

```text
uv run pytest -q
64 passed
```

Live Cloud Run redeploy + Google Home app linking remain **unverified** until wizard deploy completes and the Developer Console is filled.

## Outstanding manual Google steps

1. Billing confirmation on `plex-cast-bridge-home`
2. Run companion `--execute --confirm-billable` for APIs/Pub/Sub/SA/bucket
3. Generate secrets + `gcp-cloud-run/deploy.sh`
4. Create/edit Cloud-to-cloud integration **Plex Cast Bridge** in Developer Console
5. Link `[test]` integration in Google Home app; assign rooms
6. Configure bridge `CLOUD_GATEWAY_URL` + `CATALOG_WRITE_KEY`; enable Pub/Sub listener
7. Capture Google Console/Home app tutorial recordings (`needs_capture`)

## Proxmox upgrade / rollback

```bash
# On LXC 112
APP_DIR=/opt/plex-cast-bridge bash scripts/proxmox_upgrade.sh
# Rollback uses the stamped backup under /opt/plex-cast-bridge/backups/<stamp>/
```

## Known limitations

- No public API to create Google Home Developer Console integrations
- Workload Identity Federation not assumed on home LXC (SA key fallback)
- QUERY still stubs online=true (no Report State)
- Cast groups: discover/label/play existing groups only — no fabricated multi-room sync
- Tutorial videos for Google UIs not fabricated
