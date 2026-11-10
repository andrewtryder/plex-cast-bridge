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

For an existing service, deploy a tested image to the **same** Cloud Run service without replacing environment variables or changing IAM. Record the current revision first and keep it available for rollback:

```bash
gcloud run services describe <service> --project <project> --region <region> \
  --format='value(status.latestReadyRevisionName)'
gcloud run services update <service> --project <project> --region <region> \
  --image <tested-image-by-digest>
```

See [persistent state and upgrades](../docs/persistent-state.md) for the local and cloud storage paths. Keep the existing `OAUTH_SIGNING_SECRET`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, project, service, Firestore and catalog bucket. Changing these can invalidate the link or its tokens. Dashboard sessions and the local catalog also require the existing `data/` mount. Normal code updates should not require relinking; Google may still require a new link after expiry, revocation, or an unrelated integration problem. Verify linking and Request Sync separately after an upgrade.

For first-time provisioning only, `./gcp-cloud-run/deploy.sh` requires an explicitly prepared `.env` and performs infrastructure changes. It automatically:
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
