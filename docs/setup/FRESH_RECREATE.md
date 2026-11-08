# Fresh recreate — Google Home / GCP golden path

This document records a clean wipe and rebuild of the household Google Home integration so another operator can replicate the same names and outcomes.

## Project ID reuse warning

Google Cloud **project IDs are globally unique and cannot be reused** after project deletion.

- **2026-11-08:** Project `plex-cast-bridge-home` was scheduled for deletion (`DELETE_REQUESTED`) so the household can walk the **new-user** path (wizard prefers Create new project with a new ID).
- If you still need the literal ID `plex-cast-bridge-home`, run `gcloud projects undelete plex-cast-bridge-home` within Google’s undelete window, then wipe resources inside it instead of deleting the project.
- Going forward, treat `plex-cast-bridge-home` in checklists as the **historical** name; new installs should pick a fresh project ID (display name can still be `Plex Cast Bridge`).

## Canonical names (must match)

| Resource | Value |
|---|---|
| GCP project ID | `plex-cast-bridge-home` |
| GCP project display name | `Plex Cast Bridge` |
| Region | `us-central1` |
| Cloud Run service | `plex-google-home-fulfillment` |
| Pub/Sub topic | `plex-commands` |
| Pub/Sub DLQ topic | `plex-commands-dlq` |
| Pub/Sub subscription | `plex-commands-local-worker` |
| Fulfillment service account | `plex-fulfillment-sa` |
| Google Home integration name | `Plex Cast Bridge` |
| Device type | Set-top box |
| Icon | `assets/icon-144.png` |
| `agentUserId` | `plex-personal-user` |

## Inventory captured (2026-11-08)

### GCP (`atr000@gmail.com`)

| Item | Value |
|---|---|
| Project number | `528695300545` |
| Lifecycle | ACTIVE |
| Cloud Run URL | `https://plex-google-home-fulfillment-tiubw6i3ga-uc.a.run.app` |
| Topics | `plex-commands`, `plex-commands-dlq` |
| Subscription | `plex-commands-local-worker` → `plex-commands` |
| SAs | `plex-fulfillment-sa@…`, default compute, firebase-adminsdk |
| Firestore | `(default)` native, `us-central1` |
| Notable APIs enabled | Cloud Run, Pub/Sub, Firestore, Cloud Build, Artifact Registry, Storage |
| HomeGraph API | **not** enabled at inventory time (added during wizard/sync work) |

### Local bridge (LXC 112 / `192.168.88.130`)

| Item | Value |
|---|---|
| App root | `/opt/plex-cast-bridge` |
| `ENABLE_PUBSUB_LISTENER` | `true` (disabled during wipe) |
| `GCP_PROJECT_ID` | `plex-cast-bridge-home` |
| `GCP_PUBSUB_SUBSCRIPTION_ID` | `plex-commands-local-worker` |
| Subscriber key | `/opt/plex-cast-bridge/gcp-credentials.json` |
| Preserved | `data/plex_connection.json`, device PEMs, `data/catalog.json` |
| Plex link | JWT linked (`andrewtryder` / Music) |

## Wipe checklist (operator)

1. In the Google Home app, unlink the **[test] Plex Cast Bridge** integration (manual).
2. Soft-disable the local worker: set `ENABLE_PUBSUB_LISTENER=false` (or stop compose) so the bridge stops pulling while cloud resources are deleted.
3. Delete Cloud Run service `plex-google-home-fulfillment` in `us-central1`.
4. Delete subscription `plex-commands-local-worker`, then topics `plex-commands` and `plex-commands-dlq`.
5. Optionally delete SA `plex-fulfillment-sa` (wizard recreates it). Leave Firebase/default compute alone.
6. Do **not** delete `data/plex_*` or speaker `catalog.json`.
7. Rotate/remove the old subscriber JSON only after the new key is provisioned (or move aside as `gcp-credentials.json.bak`).

## Recreate checklist (wizard / provisioner)

1. Prefer **create new project** for other households. For this golden path, select existing `plex-cast-bridge-home`.
2. Confirm billing on the project.
3. Enable APIs: Run, Pub/Sub, Cloud Build, Artifact Registry, Firestore, Storage, HomeGraph.
4. Create topic/subscription/DLQ, fulfillment SA, GCS catalog bucket, Firestore meta.
5. Deploy Cloud Run `plex-google-home-fulfillment`; note the new URL (URL suffix changes every fresh deploy).
6. Generate OAuth client id/secret + admin passphrase + signing secret; store only in `gcp-cloud-run/.env` / Secret Manager — never in browser storage.
7. In [Google Home Developer Console](https://console.home.google.com/projects): create Cloud-to-cloud integration **Plex Cast Bridge** with the fulfillment/OAuth URLs from the deploy output.
8. Link the **[test]** integration in the Google Home app; assign devices to rooms (manual; Google does not auto-assign rooms).
9. Install subscriber credentials on the LXC; set `ENABLE_PUBSUB_LISTENER=true`; verify Pub/Sub pull and a Sync Google Home action.
10. Run the in-app connection test.

## Manual Google steps that cannot be automated

- Google Cloud billing confirmation (if required).
- Google Home Developer Console Cloud-to-cloud integration create/edit.
- Google Home app account linking and room assignment.
- Unlinking an old test integration before relink.

## Rollback

- Restore bridge `.env` Pub/Sub flags and previous `gcp-credentials.json.bak`.
- Redeploy a known-good Cloud Run revision from Artifact Registry if available.
- Plex JWT under `data/` is independent of GCP wipe — music playback via the dashboard continues without Google Home.
