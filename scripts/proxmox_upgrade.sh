#!/usr/bin/env bash
# Idempotent upgrade helper for Docker-in-LXC deployments of plex-cast-bridge.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/plex-cast-bridge}"
COMPOSE_FILE="${COMPOSE_FILE:-$APP_DIR/deploy/docker-compose.yml}"
BACKUP_ROOT="${BACKUP_ROOT:-$APP_DIR/backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="$BACKUP_ROOT/$STAMP"

echo "== plex-cast-bridge proxmox upgrade =="
echo "APP_DIR=$APP_DIR"

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "ERROR: compose file not found: $COMPOSE_FILE" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
if [[ -d "$APP_DIR/data" ]]; then
  tar czf "$BACKUP_DIR/data.tgz" -C "$APP_DIR" data
  echo "Backed up data/ -> $BACKUP_DIR/data.tgz"
fi
if [[ -f "$APP_DIR/.env" ]]; then
  cp -a "$APP_DIR/.env" "$BACKUP_DIR/.env"
fi

echo "Validating compose…"
docker compose -f "$COMPOSE_FILE" config >/dev/null

echo "Rebuilding and recreating…"
docker compose -f "$COMPOSE_FILE" up -d --build --force-recreate

echo "Waiting for health…"
ok=0
for i in $(seq 1 40); do
  if curl -sf http://127.0.0.1:8000/health >/dev/null; then
    ok=1
    break
  fi
  sleep 1
done

if [[ "$ok" -ne 1 ]]; then
  echo "ERROR: health check failed. Rollback: restore $BACKUP_DIR/data.tgz and previous image tag, then compose up." >&2
  exit 2
fi

curl -sS http://127.0.0.1:8000/health
echo
echo "Upgrade complete. Backup at $BACKUP_DIR"
echo "Rollback: tar xzf $BACKUP_DIR/data.tgz -C $APP_DIR && docker compose -f $COMPOSE_FILE up -d"
