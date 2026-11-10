#!/usr/bin/env bash
# Upgrade helper for Docker-in-LXC deployments of plex-cast-bridge with rollback.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/plex-cast-bridge}"
COMPOSE_FILE="${COMPOSE_FILE:-$APP_DIR/deploy/docker-compose.yml}"
BACKUP_ROOT="${BACKUP_ROOT:-$APP_DIR/backups}"
SERVICE_NAME="${SERVICE_NAME:-plex-cast-bridge}"
TARGET_IMAGE="${TARGET_IMAGE:-}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health}"
DIAG_URL="${DIAG_URL:-http://127.0.0.1:8000/api/diagnostics}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="$BACKUP_ROOT/$STAMP"
API_KEY="${LOCAL_API_KEY:-}"
VERIFY_ROLLBACK="${VERIFY_ROLLBACK:-1}"

ACTION="${1:-upgrade}"

require() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: required command not found: $1" >&2
    exit 1
  }
}

resolve_google_creds() {
  local creds="${GOOGLE_APPLICATION_CREDENTIALS:-}"
  if [[ -z "$creds" && -f "$APP_DIR/.env" ]]; then
    creds="$(grep -E '^GOOGLE_APPLICATION_CREDENTIALS=' "$APP_DIR/.env" | cut -d= -f2- | tr -d '\"'\'' ' || true)"
  fi
  if [[ -n "$creds" && -f "$creds" ]]; then
    echo "$creds"
  elif [[ -f "$APP_DIR/gcp-credentials.json" ]]; then
    echo "$APP_DIR/gcp-credentials.json"
  fi
}

verify_rollback() {
  local dir="$1"
  echo "Verifying rollback viability before relying on it: $dir"
  if [[ ! -d "$dir" ]]; then
    echo "ERROR: rollback directory does not exist: $dir" >&2
    return 1
  fi
  if [[ -f "$dir/data.tgz" ]]; then
    tar -tzf "$dir/data.tgz" >/dev/null || {
      echo "ERROR: backup data.tgz is corrupted" >&2
      return 1
    }
  fi
  if [[ ! -f "$dir/docker-compose.yml" ]]; then
    echo "ERROR: backup is missing compose configuration" >&2
    return 1
  fi
  if [[ ! -f "$dir/previous_image.txt" ]]; then
    echo "ERROR: backup is missing previous_image.txt" >&2
    return 1
  fi
  local prev_img
  prev_img="$(cat "$dir/previous_image.txt")"
  if [[ -n "$prev_img" ]]; then
    if ! docker inspect "$prev_img" >/dev/null 2>&1; then
      echo "ERROR: previous image $prev_img not present in local docker cache for rollback" >&2
      return 1
    fi
  fi
  echo "Rollback verification passed."
  return 0
}

rollback() {
  local target_backup="${1:-$BACKUP_DIR}"
  echo "Attempting rollback using $target_backup…" >&2

  local prev_img=""
  local prev_dig=""
  if [[ -f "$target_backup/previous_image.txt" ]]; then
    prev_img="$(cat "$target_backup/previous_image.txt")"
  fi
  if [[ -f "$target_backup/previous_image_digest.txt" ]]; then
    prev_dig="$(cat "$target_backup/previous_image_digest.txt")"
  fi

  local restore_image="${prev_dig:-$prev_img}"
  if [[ -n "$restore_image" ]]; then
    local svc_image
    svc_image="$(docker compose -f "$COMPOSE_FILE" config --images 2>/dev/null | head -n1 || true)"
    if [[ -n "$svc_image" ]]; then
      echo "Retagging previous image: $restore_image -> $svc_image" >&2
      docker tag "$restore_image" "$svc_image" 2>/dev/null || true
    fi
  fi

  if [[ -f "$target_backup/docker-compose.yml" ]]; then
    cp -a "$target_backup/docker-compose.yml" "$COMPOSE_FILE"
    echo "Restored docker-compose.yml" >&2
  fi

  if [[ -f "$target_backup/.env" ]]; then
    cp -a "$target_backup/.env" "$APP_DIR/.env"
    echo "Restored .env" >&2
  fi

  local gcp_creds
  gcp_creds="$(resolve_google_creds)"
  local gcp_target="${gcp_creds:-$APP_DIR/gcp-credentials.json}"
  if [[ -f "$target_backup/gcp-credentials.json" ]]; then
    cp -a "$target_backup/gcp-credentials.json" "$gcp_target"
    echo "Restored gcp-credentials.json" >&2
  fi

  if [[ -f "$target_backup/data.tgz" ]]; then
    tar xzf "$target_backup/data.tgz" -C "$APP_DIR"
    echo "Restored data/" >&2
  fi

  docker compose -f "$COMPOSE_FILE" up -d --force-recreate || true
  echo "Rollback attempted using $target_backup." >&2
}

if [[ "$ACTION" == "rollback" ]]; then
  require docker
  require tar
  target="${2:-}"
  if [[ -z "$target" ]]; then
    target="$(ls -dt "$BACKUP_ROOT"/* 2>/dev/null | head -n1 || true)"
  fi
  if [[ -z "$target" || ! -d "$target" ]]; then
    echo "ERROR: no backup directory found to rollback" >&2
    exit 1
  fi
  verify_rollback "$target"
  rollback "$target"
  echo "Waiting for /health after manual rollback…"
  ok=0
  for _ in $(seq 1 40); do
    if curl -sf "$HEALTH_URL" >/dev/null; then
      ok=1
      break
    fi
    sleep 1
  done
  if [[ "$ok" -ne 1 ]]; then
    echo "ERROR: health check failed after rollback." >&2
    exit 2
  fi
  echo "Rollback succeeded and health check passed."
  exit 0
fi

if [[ "$ACTION" == "verify-rollback" ]]; then
  require docker
  target="${2:-}"
  if [[ -z "$target" ]]; then
    target="$(ls -dt "$BACKUP_ROOT"/* 2>/dev/null | head -n1 || true)"
  fi
  if [[ -z "$target" || ! -d "$target" ]]; then
    echo "ERROR: no backup directory found to verify" >&2
    exit 1
  fi
  verify_rollback "$target"
  exit 0
fi

echo "== plex-cast-bridge proxmox upgrade =="
echo "APP_DIR=$APP_DIR"
echo "BACKUP_DIR=$BACKUP_DIR"

require docker
require curl
require tar

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "ERROR: compose file not found: $COMPOSE_FILE" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"

echo "Capturing current image id and digest…"
PREV_IMAGE=""
PREV_DIGEST=""
if docker compose -f "$COMPOSE_FILE" ps -q "$SERVICE_NAME" >/dev/null 2>&1; then
  cid="$(docker compose -f "$COMPOSE_FILE" ps -q "$SERVICE_NAME" | head -n1 || true)"
  if [[ -n "${cid:-}" ]]; then
    PREV_IMAGE="$(docker inspect -f '{{.Image}}' "$cid" 2>/dev/null || true)"
    if [[ -n "$PREV_IMAGE" ]]; then
      PREV_DIGEST="$(docker inspect -f '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}' "$PREV_IMAGE" 2>/dev/null || true)"
    fi
  fi
fi
printf '%s\n' "$PREV_IMAGE" >"$BACKUP_DIR/previous_image.txt"
printf '%s\n' "$PREV_DIGEST" >"$BACKUP_DIR/previous_image_digest.txt"
echo "previous_image=$PREV_IMAGE"
echo "previous_image_digest=$PREV_DIGEST"

# Back up data/
if [[ -d "$APP_DIR/data" ]]; then
  tar czf "$BACKUP_DIR/data.tgz" -C "$APP_DIR" data
  echo "Backed up data/ -> $BACKUP_DIR/data.tgz"
fi

# Back up .env
if [[ -f "$APP_DIR/.env" ]]; then
  cp -a "$APP_DIR/.env" "$BACKUP_DIR/.env"
  echo "Backed up .env -> $BACKUP_DIR/.env"
fi

# Back up Compose configuration
cp -a "$COMPOSE_FILE" "$BACKUP_DIR/docker-compose.yml"
echo "Backed up compose configuration -> $BACKUP_DIR/docker-compose.yml"

# Back up Google credential file
GCP_CREDS="$(resolve_google_creds)"
if [[ -n "$GCP_CREDS" && -f "$GCP_CREDS" ]]; then
  cp -a "$GCP_CREDS" "$BACKUP_DIR/gcp-credentials.json"
  echo "Backed up Google credentials ($GCP_CREDS) -> $BACKUP_DIR/gcp-credentials.json"
fi

# Verify rollback before relying on it
if [[ "$VERIFY_ROLLBACK" == "1" ]]; then
  verify_rollback "$BACKUP_DIR"
fi

echo "Validating compose…"
docker compose -f "$COMPOSE_FILE" config >/dev/null

# Pull the intended published image explicitly
COMPOSE_IMAGE="$(docker compose -f "$COMPOSE_FILE" config --images 2>/dev/null | head -n1 || true)"
INTENDED_IMAGE="${TARGET_IMAGE:-$COMPOSE_IMAGE}"
if [[ -z "$INTENDED_IMAGE" ]]; then
  INTENDED_IMAGE="ghcr.io/andrewtryder/plex-cast-bridge:latest"
fi

echo "Explicitly pulling intended published image: $INTENDED_IMAGE…"
docker pull "$INTENDED_IMAGE"

# If an explicit tag was supplied that differs from compose image, tag it to match
if [[ -n "$COMPOSE_IMAGE" && "$INTENDED_IMAGE" != "$COMPOSE_IMAGE" ]]; then
  echo "Tagging $INTENDED_IMAGE as $COMPOSE_IMAGE for compose…"
  docker tag "$INTENDED_IMAGE" "$COMPOSE_IMAGE"
fi

echo "Recreating container with published image…"
docker compose -f "$COMPOSE_FILE" up -d --force-recreate

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
  echo "ERROR: health check failed. Executing rollback…" >&2
  rollback "$BACKUP_DIR"
  echo "Waiting for /health to recover after rollback…" >&2
  for _ in $(seq 1 40); do
    if curl -sf "$HEALTH_URL" >/dev/null; then
      echo "Service restored after rollback." >&2
      exit 2
    fi
    sleep 1
  done
  echo "FATAL: service did not recover after rollback." >&2
  exit 2
fi

echo "Layered check: diagnostics (non-secret)…"
diag_ok=1
if [[ -z "$API_KEY" && -f "$APP_DIR/.env" ]]; then
  API_KEY="$(grep -E '^LOCAL_API_KEY=' "$APP_DIR/.env" | cut -d= -f2- | tr -d '\"'\'' ' || true)"
fi

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
echo "  bash $0 rollback $BACKUP_DIR"
