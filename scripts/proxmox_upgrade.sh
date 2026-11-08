#!/usr/bin/env bash
# Upgrade helper for Docker-in-LXC deployments of plex-cast-bridge with rollback.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/plex-cast-bridge}"
COMPOSE_FILE="${COMPOSE_FILE:-$APP_DIR/deploy/docker-compose.yml}"
BACKUP_ROOT="${BACKUP_ROOT:-$APP_DIR/backups}"
SERVICE_NAME="${SERVICE_NAME:-plex-cast-bridge}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health}"
DIAG_URL="${DIAG_URL:-http://127.0.0.1:8000/api/diagnostics}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="$BACKUP_ROOT/$STAMP"
API_KEY="${LOCAL_API_KEY:-}"

echo "== plex-cast-bridge proxmox upgrade =="
echo "APP_DIR=$APP_DIR"
echo "BACKUP_DIR=$BACKUP_DIR"

require() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: required command not found: $1" >&2
    exit 1
  }
}

require docker
require curl
require tar

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "ERROR: compose file not found: $COMPOSE_FILE" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"

echo "Capturing current image id…"
PREV_IMAGE=""
if docker compose -f "$COMPOSE_FILE" ps -q "$SERVICE_NAME" >/dev/null 2>&1; then
  cid="$(docker compose -f "$COMPOSE_FILE" ps -q "$SERVICE_NAME" | head -n1 || true)"
  if [[ -n "${cid:-}" ]]; then
    PREV_IMAGE="$(docker inspect -f '{{.Image}}' "$cid" 2>/dev/null || true)"
  fi
fi
printf '%s\n' "$PREV_IMAGE" >"$BACKUP_DIR/previous_image.txt"
echo "previous_image=$PREV_IMAGE"

if [[ -d "$APP_DIR/data" ]]; then
  tar czf "$BACKUP_DIR/data.tgz" -C "$APP_DIR" data
  echo "Backed up data/ -> $BACKUP_DIR/data.tgz"
fi
if [[ -f "$APP_DIR/.env" ]]; then
  cp -a "$APP_DIR/.env" "$BACKUP_DIR/.env"
  echo "Backed up .env"
fi

echo "Validating compose…"
docker compose -f "$COMPOSE_FILE" config >/dev/null

echo "Building / pulling candidate…"
docker compose -f "$COMPOSE_FILE" build
docker compose -f "$COMPOSE_FILE" up -d --force-recreate

rollback() {
  echo "Attempting rollback…" >&2
  if [[ -n "$PREV_IMAGE" ]]; then
    # Best-effort: retag previous image as the compose service image name if available.
    svc_image="$(docker compose -f "$COMPOSE_FILE" config --images | head -n1 || true)"
    if [[ -n "$svc_image" ]]; then
      docker tag "$PREV_IMAGE" "$svc_image" 2>/dev/null || true
    fi
  fi
  if [[ -f "$BACKUP_DIR/data.tgz" ]]; then
    tar xzf "$BACKUP_DIR/data.tgz" -C "$APP_DIR"
  fi
  if [[ -f "$BACKUP_DIR/.env" ]]; then
    cp -a "$BACKUP_DIR/.env" "$APP_DIR/.env"
  fi
  docker compose -f "$COMPOSE_FILE" up -d || true
  echo "Rollback attempted using $BACKUP_DIR. If health is still bad, restore manually from that directory." >&2
}

echo "Waiting for /health…"
ok=0
for _ in $(seq 1 40); do
  if curl -sf "$HEALTH_URL" >/dev/null; then
    ok=1
    break
  fi
  sleep 1
done

if [[ "$ok" -ne 1 ]]; then
  echo "ERROR: health check failed." >&2
  rollback
  exit 2
fi

echo "Layered check: diagnostics (non-secret)…"
diag_ok=1
hdr=()
if [[ -n "$API_KEY" ]]; then
  hdr=(-H "X-API-Key: $API_KEY")
fi
if ! curl -sf "${hdr[@]}" "$DIAG_URL" >/dev/null; then
  echo "WARN: diagnostics endpoint not reachable with current auth; health passed but layered check skipped." >&2
  diag_ok=0
fi

curl -sS "$HEALTH_URL" || true
echo
echo "Upgrade complete. Backup at $BACKUP_DIR (layered_diag=$diag_ok)"
echo "Manual rollback if needed:"
echo "  tar xzf $BACKUP_DIR/data.tgz -C $APP_DIR"
echo "  cp -a $BACKUP_DIR/.env $APP_DIR/.env"
echo "  # previous image id: $(cat "$BACKUP_DIR/previous_image.txt")"
echo "  docker compose -f $COMPOSE_FILE up -d"
