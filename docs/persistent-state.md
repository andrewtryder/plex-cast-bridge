# Persistent state and upgrades

A routine code or container upgrade should not require a new Google Home link. Preserve the existing local `data/` directory, `.env`, and Google subscriber credential file, and keep the existing Cloud Run service configuration, OAuth signing secret and client credentials, Firestore data, and catalog bucket. Deploy an image to the existing service without replacing its environment. Do not rerun first-install provisioning as an upgrade. New refresh tokens have no fixed expiry and are revocable through persistent Firestore state. A valid legacy JWT is migrated by hash on first use; that exact token can then remain valid past its embedded expiry. Access-token expiry validation remains enabled. Authorization-code exchanges are retry-safe for the same signed code during its short validity window: if a response is lost after persistence, retrying returns the same opaque token, while an older code cannot replace a newer exchange. A transactionally allocated Firestore order prevents Cloud Run instance clock skew from reversing authorization-code order. Firestore stores only a hash of the active refresh token. Relinking should only be needed for an explicit unlink/revocation, credential change, or unrelated Google Home account problem.

### Confirm the first live refresh migration

Do not relink or exchange a new authorization code just to test this migration. Google performs refreshes when needed; the bridge cannot safely force one without the refresh token held by Google. After a natural refresh, inspect only the Firestore metadata (never print or copy the token hash):

```bash
PROJECT_ID=your-existing-gcp-project-id uv run python - <<'PY'
import os
from google.cloud import firestore

doc = firestore.Client(project=os.environ["PROJECT_ID"]).collection("plex_cast_bridge").document("oauth_refresh_state").get()
state = doc.to_dict() if doc.exists else {}
active = bool(state.get("token_hash")) and not state.get("revoked_at")
print({
    "record_exists": doc.exists,
    "active": active,
    "token_format": state.get("token_format"),
    "created_at": state.get("created_at"),
})
PY
```

If the document is absent, no successful refresh migration has been recorded; keep the existing Home link and check again after Google next refreshes. An active `legacy-jwt` record with a hash confirms the old token was migrated. An active `opaque-v1` record confirms a newer authorization-code exchange instead, so it does not by itself prove legacy-token migration. Do not share the full Firestore document or token hash.

For Cloud Run code releases, use `uv run python scripts/cloud_run_upgrade.py` with the existing project, region, service, a digest-pinned image, and a new private `--preflight-file` path. Review the preflight output, then pass the same manifest path to a later `--apply` command. The apply refuses to deploy if the project, service, target image, current image, protected configuration, traffic assignment, or Firestore link identity changed after review. It checks the link flag, epoch, `agentUserId`, catalog version, and catalog content digest before and after the image update and traffic switch. The candidate revision is created with no traffic; only after it is ready and the configuration and link state still match does the helper route 100% of traffic and existing tags to it. Split/canary traffic fails closed for a manual rollout decision. The file contains a local HMAC key and hashes, not plaintext environment values; keep it private and remove it after validation. For LXC, use `scripts/proxmox_upgrade.sh`; it backs up state and checks a protected configuration fingerprint after replacement. Keep the previous digest and backup until health and Request Sync are confirmed.

There is no automatic OAuth credential-rotation operation. If rotation is ever required, treat it as a separately reviewed migration: preserve the current signing and Google client credentials until a replacement link/token is confirmed, then explicitly revoke the old credential. Never rotate credentials as a side effect of provisioning or an image upgrade.

First-time setup and upgrades are deliberately separate. `gcp-cloud-run/deploy.sh` is for a new service only and now stops if that service already exists. The dashboard wizard detects the persisted gateway URL even if `google_home_setup.json` is missing; it treats link state as unknown unless the cloud state confirms it and disables project creation and credential minting for an established installation. Do not use reset or first-install actions to recover or upgrade an existing installation.

The [Cloud Run guide](../gcp-cloud-run/README.md) describes the image-only update. Before an upgrade, back up persistent data and configuration, record the current image and Cloud Run revision, and check rollback availability. Backups contain credentials and should be private.

## Local files

The root `catalog.json` is a tracked seed for new installations and image builds. On Docker installations, the working catalog is `data/catalog.json`, mounted as `/data/catalog.json`; it changes as speakers and library data change. An explicit `CATALOG_PATH` overrides the usual location, so include that path in your backup if configured.

`google_home_sync.json` holds mutable gateway configuration and sync state, including a catalog write credential. It normally lives in `DATA_DIR`, which is `/data` in Docker. Its permissions and backups should be treated as secret. Plex connection credentials, setup state, settings, and the optional [music index](music-index.md) also live under `DATA_DIR` and must survive a container replacement. A legacy `dashboard_sessions.sqlite3` may remain after upgrade; the application no longer uses or deletes it.

Outside Docker, `DATA_DIR` selects the runtime directory. If unset, the application uses `/data` when present, otherwise the repository's `data/` directory when it exists, and otherwise the project root. A root-level `google_home_sync.json` can therefore be active runtime state; do not delete it without checking the configured path and migrating its contents. The project-root catalog can also become mutable under that fallback. Prefer an explicitly configured `DATA_DIR` outside the source checkout for non-container installations.

## Cloud state

The gateway stores catalog objects in Cloud Storage and synchronization/link metadata in Firestore. Google retains the OAuth tokens; the gateway validates them using its signing secret. The local sync file is not a replacement for that cloud state. A successful initial Google SYNC does not prove that later HomeGraph Request Sync calls work: inspect both outcomes separately.
