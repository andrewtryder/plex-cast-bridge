# GCP Cloud Run Gateway (`cloud_gateway`)

A lightweight Python 3.12+ FastAPI Google Home Cloud-to-Cloud fulfillment webhook and personal OAuth 2.0 server designed for **GCP Cloud Run**. It bridges Google Home voice commands to your local home server (`plex-cast-bridge`) via **GCP Cloud Pub/Sub**.

---

## Directory Layout

```text
gcp-cloud-run/
├── Dockerfile                   # Multi-stage Cloud Run container image
├── deploy.sh                    # Automated Cloud Run + Pub/Sub + Artifact Registry pruning
├── .env.example                 # Template for Cloud Run environment variables
└── src/
    └── cloud_gateway/
        ├── main.py              # FastAPI app (/oauth/*, /fulfillment, /webhook/query)
        ├── config.py            # Cloud Run environment validation
        ├── auth.py              # Personal OAuth 2.0 & JWT signing/verification
        ├── smart_home.py        # Google Home SYNC / QUERY / EXECUTE trait handlers
        ├── pubsub_publisher.py  # GCP Pub/Sub publisher client
        └── static/icon.png      # OAuth login screen icon
```

---

## Configuration (`gcp-cloud-run/.env`)

For a new installation, create `.env` from `.env.example` and set the credentials explicitly. For an existing installation, restore its original configuration rather than generating replacements:

| Variable | Description |
|---|---|
| `GCP_PROJECT_ID` | GCP Project ID (e.g. `plex-cast-bridge-home`) |
| `GCP_PUBSUB_TOPIC_ID` | GCP Pub/Sub Topic ID (default: `plex-commands`) |
| `GOOGLE_CLIENT_ID` | Client ID configured in Google Home Developer Console |
| `GOOGLE_CLIENT_SECRET` | Client Secret configured in Google Home Developer Console |
| `ADMIN_PASSPHRASE` | Server secret retained for existing installations; normal linking uses authenticated dashboard approval |
| `OAUTH_SIGNING_SECRET` | 64-char random hex key used for signing JWT tokens (`openssl rand -hex 32`) |
| `WEBHOOK_API_KEY` | Optional key for `/webhook/query` (defaults to `ADMIN_PASSPHRASE`) |
| `DEFAULT_SPEAKER_NAME` | Default target Cast speaker if not specified in command |
| `CATALOG_PATH` | Path to `catalog.json` (defaults to `catalog.json`) |

---

## Dependencies

The root [`pyproject.toml`](../pyproject.toml) `cloud-gateway` dependency group is the **source of truth**.
[`requirements.txt`](requirements.txt) is generated for the Cloud Run Docker/pip install path—do not hand-edit it.

```bash
# From repository root, after changing cloud-gateway deps:
uv lock
uv export --only-group cloud-gateway --no-emit-project --no-dev --no-hashes \
  -o gcp-cloud-run/requirements.txt
```

CI fails if the committed `requirements.txt` drifts from this export.

---

## Local Testing

From the repository root:

```bash
# Run the unified test suite (includes tests/test_cloud_gateway.py)
uv run pytest

# Start the Cloud Run gateway locally on port 8080
uv run uvicorn cloud_gateway.main:app --host 127.0.0.1 --port 8080 --reload
```

---

## Deploying to GCP Cloud Run

For an existing service, use the image-only helper. It requires an immutable image digest and a private preflight manifest. The first command records the reviewed project, service, current image, keyed fingerprints of protected settings, Firestore link identity (`linked`, link epoch, `agentUserId`, and catalog version), and traffic routing. The apply command uses that same manifest and refuses to deploy if any of those values changed since review. It updates only the image, waits for the new revision to become ready, then routes 100% of traffic and existing tags to it. It refuses split or canary traffic because it cannot infer the intended rollout plan. It never sets environment variables or IAM:

```bash
uv run python scripts/cloud_run_upgrade.py \
  --project <existing-project-id> \
  --region <existing-region> \
  --service <existing-service> \
  --image <tested-image@sha256:digest> \
  --preflight-file /private/path/cloud-run-preflight.json # read-only preflight

# Review the printed identity/fingerprints, then repeat with the same arguments plus:
#   --apply
```

The preflight file is created with mode `0600`; it contains a local HMAC key and fingerprints, not plaintext environment values. Do not commit or share it. Keep it through deployment validation, then remove it. Retain the previous image digest and revision until health and Request Sync are confirmed. For rollback, restore the old image and route traffic (including tags) back to the recorded ready revision, then verify health and link state.

See [persistent state and upgrades](../docs/persistent-state.md) for the local and cloud storage paths. Keep the existing `OAUTH_SIGNING_SECRET`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, project, service, Firestore and catalog bucket. Changing these can invalidate the link or its tokens. Cloud Run stores only a hash of the active refresh token in Firestore; new refresh tokens are opaque and revocable. Authorization-code exchange retries return the same token after an ambiguous persistence response, and stale codes cannot replace a newer token. Exchange order is allocated transactionally in Firestore so skewed instance clocks cannot reverse it. Existing signed tokens are migrated on their first valid refresh and kept active by their hash without globally disabling JWT expiration checks. Keep the gateway linked state and stable `agentUserId` unchanged. Normal code and image updates should not require relinking.

`./gcp-cloud-run/deploy.sh` is a **first-time provisioning tool only**. It refuses to deploy when the Cloud Run service already exists and verifies `.env` project identity before enabling APIs, creating resources, IAM changes, or setting environment variables. Never use it for an upgrade or existing-installation recovery. The wizard likewise disables provisioning and secret minting when persistent gateway configuration is detected.

For a genuinely new installation, after configuring a new `.env`, pass the explicit first-install flag. The dashboard adds it only after local state detection and the service-existence check pass:

```bash
./gcp-cloud-run/deploy.sh <project> us-central1 plex-commands plex-commands-local-worker plex-google-home-fulfillment plex-fulfillment-sa --first-install-confirmed
```

The script performs infrastructure changes. It automatically:
1. Copies the canonical root `catalog.json` into `gcp-cloud-run/catalog.json` for the Docker build context.
2. Enables required GCP APIs (`run.googleapis.com`, `pubsub.googleapis.com`, `cloudbuild.googleapis.com`).
3. Ensures the Pub/Sub topic (`plex-commands`) and subscription (`plex-commands-local-worker`) exist.
4. Provisions the least-privilege service account (`plex-fulfillment-sa`) with `roles/pubsub.publisher`.
5. Builds and deploys the container to Cloud Run (`min-instances=0`, `256Mi`).
6. Prunes older container images in Artifact Registry (keeping only the 2 most recent images to stay within the 500 MB free tier).

### Durable Google Home reconciliation

The bridge polls the authenticated `POST /internal/reconcile` endpoint using the
existing catalog write key (the webhook key is also accepted). Set
`CATALOG_RECONCILE_INTERVAL_SEC` on the bridge to control the polling interval;
the default is 60 seconds and the minimum is 10 seconds. Failed bridge polls
back off exponentially up to 300 seconds. Polling wakes the existing Cloud Run
service after scale-to-zero; if the bridge is stopped, recovery waits for its
restart. No scheduler or additional cloud service is required.

Each catalog upload creates a unique immutable Cloud Storage object. A Firestore
transaction selects the authoritative monotonic catalog version and advances its
sync generation. Duplicate uploads of the same version and content are
idempotent; different content for an existing version is rejected. Losing uploads
may leave unreferenced objects in the bucket. Firestore also stores linkage,
pending generations, retry deadlines, and a time-limited reconciliation lease.
The Cloud Run request claims a lease transactionally, calls HomeGraph outside the
transaction, and transactionally records acceptance only if the lease, link epoch,
and generation still match. Failures remain pending with capped exponential
backoff. A dead instance's lease expires after 90 seconds.

`accepted` means HomeGraph accepted Request Sync, not that Google completed a
subsequent SYNC. DISCONNECT invalidates the lease and prevents new claims. A
HomeGraph request already in flight cannot be recalled; its result cannot mark a
newer generation or a disconnected account accepted. The gateway service
account must already have the Firestore, bucket, and HomeGraph permissions used
by the existing integration. This change does not provision resources or IAM.
