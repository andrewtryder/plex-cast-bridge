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

Copy `.env.example` to `.env` before running `deploy.sh`:

| Variable | Description |
|---|---|
| `GCP_PROJECT_ID` | GCP Project ID (e.g. `plex-cast-bridge-home`) |
| `GCP_PUBSUB_TOPIC_ID` | GCP Pub/Sub Topic ID (default: `plex-commands`) |
| `GOOGLE_CLIENT_ID` | Client ID configured in Google Home Developer Console |
| `GOOGLE_CLIENT_SECRET` | Client Secret configured in Google Home Developer Console |
| `ADMIN_PASSPHRASE` | Passphrase required on the `/oauth/authorize` login page |
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

From either the repository root or `gcp-cloud-run/`:

```bash
./gcp-cloud-run/deploy.sh
```

The script automatically:
1. Copies the canonical root `catalog.json` into `gcp-cloud-run/catalog.json` for the Docker build context.
2. Enables required GCP APIs (`run.googleapis.com`, `pubsub.googleapis.com`, `cloudbuild.googleapis.com`).
3. Ensures the Pub/Sub topic (`plex-commands`) and subscription (`plex-commands-local-worker`) exist.
4. Provisions the least-privilege service account (`plex-fulfillment-sa`) with `roles/pubsub.publisher`.
5. Builds and deploys the container to Cloud Run (`min-instances=0`, `256Mi`).
6. Prunes older container images in Artifact Registry (keeping only the 2 most recent images to stay within the 500 MB free tier).
