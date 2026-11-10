# Persistent state and upgrades

A routine code or container upgrade should not require a new Google Home link. Preserve the existing local `data/` directory, `.env`, and Google subscriber credential file, and keep the existing Cloud Run service configuration, OAuth signing secret and client credentials, Firestore data, and catalog bucket. Deploy an image to the existing service without replacing its environment. Do not rerun first-install provisioning as an upgrade. A user may still need to link again after token expiration or revocation, a secret change, or a Google Home integration problem.

The [Cloud Run guide](../gcp-cloud-run/README.md) describes the image-only update. Before an upgrade, back up persistent data and configuration, record the current image and Cloud Run revision, and check rollback availability. Backups contain credentials and should be private.

## Local files

The root `catalog.json` is a tracked seed for new installations and image builds. On Docker installations, the working catalog is `data/catalog.json`, mounted as `/data/catalog.json`; it changes as speakers and library data change. An explicit `CATALOG_PATH` overrides the usual location, so include that path in your backup if configured.

`google_home_sync.json` holds mutable gateway configuration and sync state, including a catalog write credential. It normally lives in `DATA_DIR`, which is `/data` in Docker. Its permissions and backups should be treated as secret. `dashboard_sessions.sqlite3`, Plex connection credentials, setup state, and settings also live under `DATA_DIR` and must survive a container replacement. Browser sessions have idle and absolute expiry even when their database is preserved.

Outside Docker, `DATA_DIR` selects the runtime directory. If unset, the application uses `/data` when present, otherwise the repository's `data/` directory when it exists, and otherwise the project root. A root-level `google_home_sync.json` can therefore be active runtime state; do not delete it without checking the configured path and migrating its contents. The project-root catalog can also become mutable under that fallback. Prefer an explicitly configured `DATA_DIR` outside the source checkout for non-container installations.

## Cloud state

The gateway stores catalog objects in Cloud Storage and synchronization/link metadata in Firestore. Google retains the OAuth tokens; the gateway validates them using its signing secret. The local sync file is not a replacement for that cloud state. A successful initial Google SYNC does not prove that later HomeGraph Request Sync calls work: inspect both outcomes separately.
