#!/usr/bin/env bash
set -euo pipefail
# Restrictive umask before any secret files (.env, temp env YAML) are created.
umask 077

# ==============================================================================
# Deploy Script: Plex Google Home Fulfillment on GCP Cloud Run
# ==============================================================================

# Resource attribution for Google Cloud metrics
export CLOUDSDK_METRICS_ENVIRONMENT="${CLOUDSDK_METRICS_ENVIRONMENT:+$CLOUDSDK_METRICS_ENVIRONMENT }datacloud.antigravity"

# Default configuration
DEFAULT_PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || echo "plex-cast-bridge-home")}"
DEFAULT_REGION="${GCP_REGION:-us-central1}"
DEFAULT_TOPIC="plex-commands"
DEFAULT_SUBSCRIPTION="plex-commands-local-worker"
DEFAULT_SERVICE="plex-google-home-fulfillment"
DEFAULT_SA="plex-fulfillment-sa"

echo "============================================================"
echo " Plex Google Home Fulfillment - GCP Cloud Run Deployment"
echo "============================================================"

PROJECT_ID="${1:-$DEFAULT_PROJECT_ID}"
REGION="${2:-$DEFAULT_REGION}"
TOPIC_ID="${3:-$DEFAULT_TOPIC}"
SUBSCRIPTION_ID="${4:-$DEFAULT_SUBSCRIPTION}"
SERVICE_NAME="${5:-$DEFAULT_SERVICE}"
SA_NAME="${6:-$DEFAULT_SA}"

echo "Configuration:"
echo "  GCP Project ID:       $PROJECT_ID"
echo "  GCP Region:           $REGION"
echo "  Pub/Sub Topic:        $TOPIC_ID"
echo "  Pub/Sub Subscription: $SUBSCRIPTION_ID"
echo "  Cloud Run Service:    $SERVICE_NAME"
echo "  Service Account:      $SA_NAME"
echo "------------------------------------------------------------"

# 1. Verify gcloud authentication
echo "[1/8] Verifying gcloud authentication..."
ACTIVE_ACCOUNT=$(gcloud auth list --filter=status:ACTIVE --format="value(account)")
if [ -z "$ACTIVE_ACCOUNT" ]; then
    echo "ERROR: No active gcloud account found. Run 'gcloud auth login' first."
    exit 1
fi
echo "  Authenticated as: $ACTIVE_ACCOUNT"

# Set project in gcloud
gcloud config set project "$PROJECT_ID" >/dev/null

# 2. Enable necessary APIs and verify Firestore Native Database
echo "[2/8] Enabling required GCP APIs (Cloud Run, Pub/Sub, Cloud Build, Firestore)..."
gcloud services enable \
    run.googleapis.com \
    pubsub.googleapis.com \
    cloudbuild.googleapis.com \
    iam.googleapis.com \
    firestore.googleapis.com \
    --project="$PROJECT_ID"

echo "  Checking Firestore (default) database exists and is active..."
if gcloud firestore databases describe --database="(default)" --project="$PROJECT_ID" >/dev/null 2>&1; then
    echo "  Firestore (default) database already exists."
else
    echo "  Creating Firestore (default) native database in $REGION..."
    gcloud firestore databases create \
        --location="$REGION" \
        --type=firestore-native \
        --project="$PROJECT_ID"
    echo "  Created Firestore (default) native database."
fi

# 3. Provision Pub/Sub Topic and Dead-Letter Queue (DLQ) Topic
DLQ_TOPIC="${TOPIC_ID}-dlq"
echo "[3/8] Provisioning Pub/Sub topics ('$TOPIC_ID' and '$DLQ_TOPIC')..."
if gcloud pubsub topics describe "$TOPIC_ID" --project="$PROJECT_ID" >/dev/null 2>&1; then
    echo "  Topic '$TOPIC_ID' already exists."
else
    gcloud pubsub topics create "$TOPIC_ID" --project="$PROJECT_ID"
    echo "  Created topic '$TOPIC_ID'."
fi

if gcloud pubsub topics describe "$DLQ_TOPIC" --project="$PROJECT_ID" >/dev/null 2>&1; then
    echo "  Topic '$DLQ_TOPIC' already exists."
else
    gcloud pubsub topics create "$DLQ_TOPIC" --project="$PROJECT_ID"
    echo "  Created DLQ topic '$DLQ_TOPIC'."
fi

# Grant Pub/Sub system agent rights to forward dead-letter messages
PROJECT_NUM=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
PUBSUB_SERVICE_ACCOUNT="service-${PROJECT_NUM}@gcp-sa-pubsub.iam.gserviceaccount.com"
echo "  Configuring Pub/Sub service agent IAM on DLQ topic..."
gcloud pubsub topics add-iam-policy-binding "$DLQ_TOPIC" \
    --member="serviceAccount:$PUBSUB_SERVICE_ACCOUNT" \
    --role="roles/pubsub.publisher" \
    --project="$PROJECT_ID" >/dev/null

# 4. Provision Pub/Sub Subscription for local worker with durable DLQ policy
echo "[4/8] Provisioning Pub/Sub subscription '$SUBSCRIPTION_ID' with DLQ policy..."
if gcloud pubsub subscriptions describe "$SUBSCRIPTION_ID" --project="$PROJECT_ID" >/dev/null 2>&1; then
    echo "  Subscription '$SUBSCRIPTION_ID' already exists. Updating dead-letter policy..."
    gcloud pubsub subscriptions update "$SUBSCRIPTION_ID" \
        --dead-letter-topic="$DLQ_TOPIC" \
        --max-delivery-attempts=5 \
        --project="$PROJECT_ID"
else
    gcloud pubsub subscriptions create "$SUBSCRIPTION_ID" \
        --topic="$TOPIC_ID" \
        --ack-deadline=30 \
        --dead-letter-topic="$DLQ_TOPIC" \
        --max-delivery-attempts=5 \
        --project="$PROJECT_ID"
    echo "  Created subscription '$SUBSCRIPTION_ID' with dead-letter topic '$DLQ_TOPIC'."
fi

echo "  Configuring Pub/Sub service agent subscriber role on subscription..."
gcloud pubsub subscriptions add-iam-policy-binding "$SUBSCRIPTION_ID" \
    --member="serviceAccount:$PUBSUB_SERVICE_ACCOUNT" \
    --role="roles/pubsub.subscriber" \
    --project="$PROJECT_ID" >/dev/null

# Verify effective DLQ subscription configuration (fail fast if mismatch)
echo "  Verifying effective subscription DLQ policy..."
EFFECTIVE_DLQ=$(gcloud pubsub subscriptions describe "$SUBSCRIPTION_ID" \
    --project="$PROJECT_ID" \
    --format="value(deadLetterPolicy.deadLetterTopic)")
EFFECTIVE_ATTEMPTS=$(gcloud pubsub subscriptions describe "$SUBSCRIPTION_ID" \
    --project="$PROJECT_ID" \
    --format="value(deadLetterPolicy.maxDeliveryAttempts)")

EXPECTED_DLQ="projects/${PROJECT_ID}/topics/${DLQ_TOPIC}"
if [ "$EFFECTIVE_DLQ" != "$EXPECTED_DLQ" ] || [ "${EFFECTIVE_ATTEMPTS:-0}" -lt 5 ]; then
    echo "ERROR: Pub/Sub DLQ policy verification failed!"
    echo "  Expected DLQ Topic: $EXPECTED_DLQ, Got: $EFFECTIVE_DLQ"
    echo "  Expected Attempts:  >= 5, Got: $EFFECTIVE_ATTEMPTS"
    exit 1
fi
echo "  ✓ Subscription DLQ verified: topic=$EFFECTIVE_DLQ, maxDeliveryAttempts=$EFFECTIVE_ATTEMPTS"

# 5. Provision Service Account for Cloud Run
echo "[5/8] Provisioning Service Account '$SA_NAME'..."
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
if gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT_ID" >/dev/null 2>&1; then
    echo "  Service account '$SA_EMAIL' already exists."
else
    gcloud iam service-accounts create "$SA_NAME" \
        --display-name="Plex Google Home Fulfillment SA" \
        --project="$PROJECT_ID"
    echo "  Created service account '$SA_EMAIL'."
fi

# Grant Pub/Sub Publisher and Firestore User roles to Service Account
echo "  Granting roles/pubsub.publisher to $SA_EMAIL..."
gcloud pubsub topics add-iam-policy-binding "$TOPIC_ID" \
    --member="serviceAccount:$SA_EMAIL" \
    --role="roles/pubsub.publisher" \
    --project="$PROJECT_ID" >/dev/null

echo "  Granting roles/datastore.user to $SA_EMAIL (distributed OAuth JTI single-use)..."
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$SA_EMAIL" \
    --role="roles/datastore.user" \
    --condition=None \
    --project="$PROJECT_ID" >/dev/null

# 6. Check environment variables
echo "[6/8] Checking environment configuration..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

if [ ! -f "$ENV_FILE" ]; then
    echo "  Notice: .env not found at $ENV_FILE. Creating from .env.example with random keys..."
    cp "$SCRIPT_DIR/.env.example" "$ENV_FILE"
    # Generate random secrets
    GEN_CLIENT_SECRET=$(openssl rand -hex 16)
    GEN_PASSPHRASE=$(openssl rand -hex 8)
    GEN_SIGNING_SECRET=$(openssl rand -hex 32)
    sed -i '' "s/GCP_PROJECT_ID=.*/GCP_PROJECT_ID=$PROJECT_ID/" "$ENV_FILE" 2>/dev/null || sed -i "s/GCP_PROJECT_ID=.*/GCP_PROJECT_ID=$PROJECT_ID/" "$ENV_FILE"
    sed -i '' "s/GOOGLE_CLIENT_SECRET=.*/GOOGLE_CLIENT_SECRET=$GEN_CLIENT_SECRET/" "$ENV_FILE" 2>/dev/null || sed -i "s/GOOGLE_CLIENT_SECRET=.*/GOOGLE_CLIENT_SECRET=$GEN_CLIENT_SECRET/" "$ENV_FILE"
    sed -i '' "s/ADMIN_PASSPHRASE=.*/ADMIN_PASSPHRASE=$GEN_PASSPHRASE/" "$ENV_FILE" 2>/dev/null || sed -i "s/ADMIN_PASSPHRASE=.*/ADMIN_PASSPHRASE=$GEN_PASSPHRASE/" "$ENV_FILE"
    sed -i '' "s/OAUTH_SIGNING_SECRET=.*/OAUTH_SIGNING_SECRET=$GEN_SIGNING_SECRET/" "$ENV_FILE" 2>/dev/null || sed -i "s/OAUTH_SIGNING_SECRET=.*/OAUTH_SIGNING_SECRET=$GEN_SIGNING_SECRET/" "$ENV_FILE"
    echo "  Generated default secrets in $ENV_FILE."
fi
chmod 600 "$ENV_FILE"

# 7. Deploy to Cloud Run
echo "[7/8] Deploying '$SERVICE_NAME' to Cloud Run..."
cd "$SCRIPT_DIR"

# Sync canonical catalog.json from repository root if present
if [ -f "$SCRIPT_DIR/../catalog.json" ]; then
    cp "$SCRIPT_DIR/../catalog.json" "$SCRIPT_DIR/catalog.json"
    echo "  Synced canonical catalog.json from repository root."
fi

TMP_ENV_YAML=$(mktemp)
trap 'rm -f "$TMP_ENV_YAML"' EXIT

python3 -c "
import json
import re
from pathlib import Path

env_path = Path(r'''$ENV_FILE''')
key_re = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
for raw in env_path.read_text(encoding='utf-8').splitlines():
    line = raw.strip()
    if not line or line.startswith('#') or '=' not in line:
        continue
    key, value = line.split('=', 1)
    key = key.strip()
    if not key_re.match(key):
        raise SystemExit(f'Invalid environment variable name in .env: {key!r}')
    # JSON-encode the value so quotes, newlines, and YAML-significant chars are safe.
    print(f'{key}: {json.dumps(value.strip())}')
" > "$TMP_ENV_YAML"

gcloud run deploy "$SERVICE_NAME" \
    --source . \
    --region "$REGION" \
    --project "$PROJECT_ID" \
    --service-account "$SA_EMAIL" \
    --env-vars-file "$TMP_ENV_YAML" \
    --allow-unauthenticated \
    --port 8080

rm -f "$TMP_ENV_YAML"
trap - EXIT

# 8. Prune old Artifact Registry images (keep latest 2 to stay within 0.5GB free tier)
echo "[8/8] Pruning old container images in Artifact Registry (keeping 2 most recent)..."
AR_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/cloud-run-source-deploy/${SERVICE_NAME}"
OLD_DIGESTS=$(gcloud artifacts docker images list "$AR_IMAGE" \
    --project="$PROJECT_ID" \
    --sort-by="~UPDATE_TIME" \
    --format="value(version)" 2>/dev/null | tail -n +3 || true)

if [ -n "$OLD_DIGESTS" ]; then
    for DIGEST in $OLD_DIGESTS; do
        gcloud artifacts docker images delete "${AR_IMAGE}@${DIGEST}" --delete-tags --quiet --project="$PROJECT_ID" >/dev/null 2>&1 || true
        echo "  Deleted old image digest: $DIGEST"
    done
else
    echo "  No old container images to prune."
fi

# Retrieve deployed service URL
SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" --region "$REGION" --project "$PROJECT_ID" --format='value(status.url)')

echo ""
echo "============================================================"
echo " Deployment Complete!"
echo "============================================================"
echo " Cloud Run Service URL: $SERVICE_URL"
echo ""
echo " Google Home Developer Console Account Linking Settings:"
echo " ------------------------------------------------------------"
echo " Client ID:         $(grep GOOGLE_CLIENT_ID "$ENV_FILE" | cut -d '=' -f2)"
echo " Client Secret:     [configured in .env]"
echo " Authorization URL: ${SERVICE_URL}/oauth/authorize"
echo " Token URL:         ${SERVICE_URL}/oauth/token"
echo " Fulfillment URL:   ${SERVICE_URL}/fulfillment"
echo ""
echo " Local plex-cast-bridge .env Settings:"
echo " ------------------------------------------------------------"
echo " GCP_PROJECT_ID=$PROJECT_ID"
echo " GCP_PUBSUB_SUBSCRIPTION_ID=$SUBSCRIPTION_ID"
echo " ENABLE_PUBSUB_LISTENER=true"
echo "============================================================"
