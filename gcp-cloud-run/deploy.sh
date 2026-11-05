#!/usr/bin/env bash
set -euo pipefail

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
echo "[1/7] Verifying gcloud authentication..."
ACTIVE_ACCOUNT=$(gcloud auth list --filter=status:ACTIVE --format="value(account)")
if [ -z "$ACTIVE_ACCOUNT" ]; then
    echo "ERROR: No active gcloud account found. Run 'gcloud auth login' first."
    exit 1
fi
echo "  Authenticated as: $ACTIVE_ACCOUNT"

# Set project in gcloud
gcloud config set project "$PROJECT_ID" >/dev/null

# 2. Enable necessary APIs
echo "[2/7] Enabling required GCP APIs (Cloud Run, Pub/Sub, Cloud Build, Firestore)..."
gcloud services enable \
    run.googleapis.com \
    pubsub.googleapis.com \
    cloudbuild.googleapis.com \
    iam.googleapis.com \
    firestore.googleapis.com \
    --project="$PROJECT_ID"

# 3. Provision Pub/Sub Topic and Dead-Letter Queue (DLQ) Topic
echo "[3/7] Provisioning Pub/Sub topics ( and -dlq)..."
if gcloud pubsub topics describe "$TOPIC_ID" --project="$PROJECT_ID" >/dev/null 2>&1; then
    echo "  Topic '$TOPIC_ID' already exists."
else
    gcloud pubsub topics create "$TOPIC_ID" --project="$PROJECT_ID"
    echo "  Created topic '$TOPIC_ID'."
fi

DLQ_TOPIC="${TOPIC_ID}-dlq"
if gcloud pubsub topics describe "$DLQ_TOPIC" --project="$PROJECT_ID" >/dev/null 2>&1; then
    echo "  Topic '$DLQ_TOPIC' already exists."
else
    gcloud pubsub topics create "$DLQ_TOPIC" --project="$PROJECT_ID"
    echo "  Created DLQ topic '$DLQ_TOPIC'."
fi

# Grant Pub/Sub system agent rights to forward dead-letter messages
PROJECT_NUM=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)" 2>/dev/null || true)
if [ -n "$PROJECT_NUM" ]; then
    PUBSUB_SERVICE_ACCOUNT="service-${PROJECT_NUM}@gcp-sa-pubsub.iam.gserviceaccount.com"
    gcloud pubsub topics add-iam-policy-binding "$DLQ_TOPIC" \
        --member="serviceAccount:$PUBSUB_SERVICE_ACCOUNT" \
        --role="roles/pubsub.publisher" \
        --project="$PROJECT_ID" >/dev/null 2>&1 || true
fi

# 4. Provision Pub/Sub Subscription for local worker with durable DLQ policy
echo "[4/7] Provisioning Pub/Sub subscription '$SUBSCRIPTION_ID' with DLQ policy..."
if gcloud pubsub subscriptions describe "$SUBSCRIPTION_ID" --project="$PROJECT_ID" >/dev/null 2>&1; then
    echo "  Subscription '$SUBSCRIPTION_ID' already exists. Updating dead-letter policy..."
    gcloud pubsub subscriptions update "$SUBSCRIPTION_ID" \
        --dead-letter-topic="$DLQ_TOPIC" \
        --max-delivery-attempts=5 \
        --project="$PROJECT_ID" >/dev/null 2>&1 || true
else
    gcloud pubsub subscriptions create "$SUBSCRIPTION_ID" \
        --topic="$TOPIC_ID" \
        --ack-deadline=30 \
        --dead-letter-topic="$DLQ_TOPIC" \
        --max-delivery-attempts=5 \
        --project="$PROJECT_ID"
    echo "  Created subscription '$SUBSCRIPTION_ID' with dead-letter topic '$DLQ_TOPIC'."
fi

if [ -n "${PROJECT_NUM:-}" ]; then
    gcloud pubsub subscriptions add-iam-policy-binding "$SUBSCRIPTION_ID" \
        --member="serviceAccount:$PUBSUB_SERVICE_ACCOUNT" \
        --role="roles/pubsub.subscriber" \
        --project="$PROJECT_ID" >/dev/null 2>&1 || true
fi

# 5. Provision Service Account for Cloud Run
echo "[5/7] Provisioning Service Account '$SA_NAME'..."
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
    --project="$PROJECT_ID" >/dev/null 2>&1 || true

# 6. Check environment variables
echo "[6/7] Checking environment configuration..."
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

# 7. Deploy to Cloud Run
echo "[7/8] Deploying '$SERVICE_NAME' to Cloud Run..."
cd "$SCRIPT_DIR"

# Sync canonical catalog.json from repository root if present
if [ -f "$SCRIPT_DIR/../catalog.json" ]; then
    cp "$SCRIPT_DIR/../catalog.json" "$SCRIPT_DIR/catalog.json"
    echo "  Synced canonical catalog.json from repository root."
fi

TMP_ENV_YAML=$(mktemp)
python3 -c "
with open('$ENV_FILE') as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            print(f'{k.strip()}: \"{v.strip()}\"')
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

