#!/usr/bin/env bash
# Upgrade helper for Docker-in-LXC deployments of plex-cast-bridge with rollback.
set -euo pipefail
umask 077

APP_DIR="${APP_DIR:-/opt/plex-cast-bridge}"
COMPOSE_FILE="${COMPOSE_FILE:-$APP_DIR/deploy/docker-compose.yml}"
BACKUP_ROOT="${BACKUP_ROOT:-$APP_DIR/backups}"
SERVICE_NAME="${SERVICE_NAME:-plex-cast-bridge}"
TARGET_IMAGE="${TARGET_IMAGE:-}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health}"
DIAG_URL="${DIAG_URL:-http://127.0.0.1:8000/api/diagnostics}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="${PCB_BACKUP_DIR:-$BACKUP_ROOT/$STAMP}"
CONFIG_HMAC_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ACTION="${1:-upgrade}"
DRIFT_MODULE="${PCB_DRIFT_MODULE:-$APP_DIR/src/plex_cast_bridge/config_drift.py}"
COMPOSE=(docker compose)
if [[ -n "${PCB_COMPOSE_FILES_JSON:-}" ]]; then
  compose_files="$(python3 -c 'import json,sys
v=json.loads(sys.argv[1]); assert isinstance(v,list) and v and all(isinstance(p,str) and "\n" not in p for p in v)
print("\n".join(v))' "$PCB_COMPOSE_FILES_JSON")" || { echo "ERROR: invalid Compose file list" >&2; exit 1; }
  while IFS= read -r path; do COMPOSE+=(-f "$path"); done <<< "$compose_files"
else
  COMPOSE+=(-f "$COMPOSE_FILE")
fi

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

protected_config_fingerprint() {
  PROTECTED_CONFIG_HMAC_KEY="$CONFIG_HMAC_KEY" python3 - "$APP_DIR" "$COMPOSE_FILE" "${1:-}" "${DATA_SOURCE:-$APP_DIR/data}" <<'PY'
import hashlib, hmac, json, os, pathlib, sys
app_dir, compose_file, credential_file, data_dir = map(pathlib.Path, sys.argv[1:])
items = {}
for label, path in (("env", app_dir / ".env"), ("compose", compose_file), ("google_credentials", credential_file)):
    if str(path) and path.is_file():
        items[label] = hashlib.sha256(path.read_bytes()).hexdigest()
    else:
        items[label] = None
sync_file = data_dir / "google_home_sync.json"
try:
    sync = json.loads(sync_file.read_text(encoding="utf-8"))
    gateway = (sync.get("cloud_gateway_url") or "").strip()
    catalog_key = sync.get("catalog_write_key") or ""
except Exception:
    gateway = ""
    catalog_key = ""
key = bytes.fromhex(os.environ["PROTECTED_CONFIG_HMAC_KEY"])
items["gateway_url_fingerprint"] = hashlib.sha256(gateway.encode()).hexdigest() if gateway else None
items["catalog_write_key_fingerprint"] = hmac.new(key, catalog_key.encode(), hashlib.sha256).hexdigest() if catalog_key else None
try:
    catalog = json.loads((data_dir / "catalog.json").read_text(encoding="utf-8"))
    device_ids = sorted(
        s.get("google_home_device_id") or s.get("id") or ""
        for s in catalog.get("speakers", []) if isinstance(s, dict)
    )
except Exception:
    device_ids = []
items["virtual_device_ids_fingerprint"] = hmac.new(
    key, json.dumps(device_ids, separators=(",", ":")).encode(), hashlib.sha256
).hexdigest()
print(hmac.new(key, json.dumps(items, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest())
PY
}

verify_rollback() {
  local dir="$1"
  echo "Verifying rollback viability before relying on it: $dir"
  if [[ ! -d "$dir" ]]; then
    echo "ERROR: rollback directory does not exist: $dir" >&2
    return 1
  fi
  if [[ ! -f "$dir/data.tgz" ]] || ! python3 "$SCRIPT_DIR/upgrade_backup.py" verify "$dir/data.tgz"; then
    echo "ERROR: backup archive cannot be restored and verified" >&2
    return 1
  fi
  if [[ ! -f "$dir/docker-compose.yml" ]]; then
    echo "ERROR: backup is missing compose configuration" >&2
    return 1
  fi
  if [[ ! -f "$dir/previous_image.txt" ]]; then
    echo "ERROR: backup is missing previous_image.txt" >&2
    return 1
  fi
  local prev_img rollback_img
  prev_img="$(cat "$dir/previous_image.txt")"
  rollback_img="$(cat "$dir/previous_image_digest.txt")"
  rollback_img="${prev_img:-$rollback_img}"
  if [[ -z "$rollback_img" ]]; then
    echo "ERROR: backup has no previous image digest or image ID" >&2
    return 1
  fi
  if ! docker inspect "$rollback_img" >/dev/null 2>&1; then
    echo "ERROR: previous image $rollback_img not present in local docker cache for rollback" >&2
    return 1
  fi
  echo "Rollback verification passed."
  return 0
}

write_image_override() {
  python3 - "$SERVICE_NAME" "$1" "$2" <<'PYCODE'
import json, os, pathlib, sys
service, image, name = sys.argv[1:]
path = pathlib.Path(name)
content = json.dumps({"services": {service: {"image": image}}}, sort_keys=True) + "\n"
try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    if path.read_text(encoding="utf-8") != content:
        raise RuntimeError("existing image override differs")
else:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)
PYCODE
}

rollback() {
  local target_backup="${1:-$BACKUP_DIR}"
  echo "Attempting rollback using ${target_backup}…" >&2

  local prev_img=""
  local prev_dig=""
  if [[ -f "$target_backup/previous_image.txt" ]]; then
    prev_img="$(cat "$target_backup/previous_image.txt")"
  fi
  if [[ -f "$target_backup/previous_image_digest.txt" ]]; then
    prev_dig="$(cat "$target_backup/previous_image_digest.txt")"
  fi

  local restore_image="${prev_img:-$prev_dig}"
  if [[ -z "$restore_image" ]]; then
    echo "ERROR: previous image is unavailable." >&2; return 1
  fi
  local override="$target_backup/rollback-image-override.json"
  write_image_override "$restore_image" "$override"

  # A failed candidate may have already written newer user data. Automatic
  # recovery must never overlay the archive, credentials, or configuration.
  # The verified archive remains available for an explicit offline restore.
  echo "Preserving current data and configuration; backup is available for manual offline recovery." >&2

  "${COMPOSE[@]}" -f "$override" up -d --no-deps --pull never --force-recreate "$SERVICE_NAME"
  echo "Image rollback attempted using $target_backup." >&2
}

# Run the candidate diagnostic in the existing container, including on older images.
# Source arrives on stdin; the running container supplies its actual environment and /data.
configuration_drift() {
  [[ -r "$DRIFT_MODULE" ]] || { echo "ERROR: configuration diagnostic source unavailable: $DRIFT_MODULE" >&2; return 1; }
  "${COMPOSE[@]}" exec -T "$SERVICE_NAME" python -c \
    'import sys; __package__="plex_cast_bridge"; exec(compile(sys.stdin.read(), "config_drift.py", "exec"))' < "$DRIFT_MODULE"
}

validate_drift_report() {
  python3 -c 'import json,sys
try:
 d=json.load(sys.stdin); settings=d["settings"]; files=d["files"]; warnings=d["release_preflight_warnings"]
 assert isinstance(settings,dict) and isinstance(files,dict) and isinstance(warnings,list)
 for name in ("plex_connection.json","app_settings.json","catalog.json","google_home_sync.json","google_home_setup.json","music_index.sqlite3"):
  status=files[name]; assert isinstance(status,str) and "invalid" not in status and "unreadable" not in status
 names=("cloud_gateway_url", "catalog_write_key")
 for name in names:
  entry=settings[name]; assert isinstance(entry["conflict"],bool) and entry["classification"] in ("ok","informational","release_preflight_warning")
 assert set(warnings)=={n for n in names if settings[n]["classification"]=="release_preflight_warning"}
except (ValueError,KeyError,TypeError,AssertionError): sys.exit(1)'
}

if [[ "$ACTION" == "config-preflight" ]]; then
  require docker
  report="$(configuration_drift)" || exit 1
  printf '%s' "$report" | validate_drift_report || { echo "ERROR: invalid diagnostic report" >&2; exit 1; }
  printf '%s\n' "$report"
  exit 0
fi

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

if [[ -z "${PCB_REVIEW_PLAN:-}" ]]; then
  echo "ERROR: use upgrade.py plan and apply a reviewed plan; direct upgrades are disabled." >&2
  exit 1
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

# Review before any backup, pull, or container recreation. The report contains no values.
DRIFT_BEFORE="$(configuration_drift)" || { echo "ERROR: configuration preflight unavailable; upgrade stopped." >&2; exit 1; }
printf '%s' "$DRIFT_BEFORE" | validate_drift_report || { echo "ERROR: invalid configuration preflight; upgrade stopped." >&2; exit 1; }
printf '%s\n' "$DRIFT_BEFORE"
if printf '%s' "$DRIFT_BEFORE" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin)["release_preflight_warnings"] else 1)'; then
  echo "WARNING: gateway configuration conflicts require review before upgrade." >&2
  if [[ "${ACK_CONFIG_DRIFT:-0}" != "1" ]]; then
    echo "Review config-preflight, then set ACK_CONFIG_DRIFT=1 to acknowledge the warning." >&2
    exit 1
  fi
fi

mkdir -p "$BACKUP_DIR"
printf '%s\n' "$DRIFT_BEFORE" > "$BACKUP_DIR/config_drift_before.json"

echo "Capturing current image id and digest…"
PREV_IMAGE=""
PREV_DIGEST=""
if "${COMPOSE[@]}" ps -q "$SERVICE_NAME" >/dev/null 2>&1; then
  cid="$("${COMPOSE[@]}" ps -q "$SERVICE_NAME" | head -n1 || true)"
  if [[ -n "${cid:-}" ]]; then
    PREV_IMAGE="$(docker inspect -f '{{.Image}}' "$cid" 2>/dev/null || true)"
    if [[ -n "$PREV_IMAGE" ]]; then
      PREV_DIGEST="$(docker inspect -f '{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}{{.Id}}{{end}}' "$PREV_IMAGE" 2>/dev/null || true)"
    fi
  fi
fi
if [[ -z "${cid:-}" || -z "$PREV_IMAGE" ]]; then
  echo "ERROR: running service and previous image could not be identified; upgrade stopped." >&2; exit 1
fi
printf '%s\n' "$PREV_IMAGE" >"$BACKUP_DIR/previous_image.txt"
printf '%s\n' "$PREV_DIGEST" >"$BACKUP_DIR/previous_image_digest.txt"
echo "previous_image=$PREV_IMAGE"
echo "previous_image_digest=$PREV_DIGEST"

echo "Validating compose and immutable target…"
"${COMPOSE[@]}" config >/dev/null
COMPOSE_IMAGE="$("${COMPOSE[@]}" config --format json | python3 -c \
  'import json,sys; d=json.load(sys.stdin); print(d["services"][sys.argv[1]].get("image", ""))' "$SERVICE_NAME")"
INTENDED_IMAGE="$TARGET_IMAGE"
if [[ ! "$INTENDED_IMAGE" =~ ^.+@sha256:[0-9a-f]{64}$ ]]; then
  echo "ERROR: set TARGET_IMAGE to a reviewed immutable image digest before upgrading." >&2; exit 1
fi
if [[ -z "$COMPOSE_IMAGE" ]]; then
  echo "ERROR: Compose service has no configured image." >&2; exit 1
fi
# A registry failure must not interrupt the running service.
echo "Pulling the reviewed image before stopping the service…"
docker pull "$INTENDED_IMAGE"

# Recheck the reviewed configuration after the potentially slow image pull.
if [[ -n "${PCB_REVIEW_PLAN:-}" ]]; then
  python3 "$SCRIPT_DIR/upgrade.py" check-plan --plan-file "$PCB_REVIEW_PLAN" --target "$INTENDED_IMAGE" --backup-dir "$BACKUP_DIR" --config-backup "$BACKUP_DIR/configuration.tgz" || {
    echo "ERROR: reviewed plan no longer matches; upgrade stopped." >&2; exit 1;
  }
fi

# Identify the actual data mount while the old container is still running.
# Reject nested mounts and other running containers that can write the same tree.
mapfile_output="$(python3 "$SCRIPT_DIR/upgrade_mounts.py" "$cid")" || {
  echo "ERROR: cannot establish exclusive persistent data mount; upgrade stopped." >&2; exit 1;
}
DATA_SOURCE="$(printf '%s\n' "$mapfile_output" | sed -n '1p')"
DATA_CONTAINER_PATH="$(printf '%s\n' "$mapfile_output" | sed -n '2p')"
if [[ -z "$DATA_SOURCE" || -z "$DATA_CONTAINER_PATH" ]]; then
  echo "ERROR: could not resolve persistent data mount." >&2; exit 1
fi

echo "Stopping the bridge to create a consistent backup…"
STOPPED_FOR_BACKUP=1
resume_after_backup_failure() {
  if [[ "${STOPPED_FOR_BACKUP:-0}" == "1" ]]; then
    echo "Upgrade interrupted before replacement; restarting the existing container." >&2
    "${COMPOSE[@]}" start "$SERVICE_NAME" >&2 || true
  fi
}
trap resume_after_backup_failure EXIT
"${COMPOSE[@]}" stop "$SERVICE_NAME"
if [[ "$(docker inspect -f '{{.State.Running}}' "$cid")" != "false" ]]; then
  echo "ERROR: bridge did not stop; no backup was attempted." >&2; exit 1
fi
rechecked="$(python3 "$SCRIPT_DIR/upgrade_mounts.py" "$cid" --data-dir "$DATA_CONTAINER_PATH")" || {
  echo "ERROR: a concurrent writer or changed mount was detected." >&2; exit 1;
}
if [[ "$(printf '%s\n' "$rechecked" | sed -n '1p')" != "$DATA_SOURCE" ]]; then
  echo "ERROR: persistent data mount changed during preflight." >&2; exit 1
fi
python3 "$SCRIPT_DIR/upgrade_backup.py" create "$DATA_SOURCE" "$BACKUP_DIR/data.tgz" --writers-stopped
python3 "$SCRIPT_DIR/upgrade_backup.py" verify "$BACKUP_DIR/data.tgz"
echo "Restored and verified an isolated copy of the quiesced data backup."
python3 "$SCRIPT_DIR/upgrade.py" check-backup --plan-file "$PCB_REVIEW_PLAN" --backup-dir "$BACKUP_DIR" --container "$cid" || {
  echo "ERROR: quiesced backup differs from the reviewed plan; the original container will be restarted." >&2
  exit 1
}

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

# Capture non-secret fingerprints for all configuration that an image upgrade must preserve.
CONFIG_FINGERPRINT_BEFORE="$(protected_config_fingerprint "$GCP_CREDS")"
printf '%s\n' "$CONFIG_FINGERPRINT_BEFORE" >"$BACKUP_DIR/protected_config_fingerprint.txt"
echo "Protected configuration fingerprint recorded (keyed, non-reversible)."

# A verified archive and locally available previous image are mandatory.
verify_rollback "$BACKUP_DIR"

# Add an image-only override in the private backup directory. Do not retag a
# shared image tag or edit an existing Compose file.
IMAGE_OVERRIDE="$BACKUP_DIR/upgrade-image-override.json"
write_image_override "$INTENDED_IMAGE" "$IMAGE_OVERRIDE"

echo "Recreating container with published image…"
STOPPED_FOR_BACKUP=0
"${COMPOSE[@]}" -f "$IMAGE_OVERRIDE" up -d --no-deps --pull never --force-recreate "$SERVICE_NAME" || {
  echo "ERROR: candidate container could not start. Preserve data and use the image rollback instructions." >&2
  exit 2
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

CONFIG_FINGERPRINT_AFTER="$(protected_config_fingerprint "$GCP_CREDS")"
if [[ "$CONFIG_FINGERPRINT_AFTER" != "$CONFIG_FINGERPRINT_BEFORE" ]]; then
  echo "ERROR: protected configuration fingerprint changed during upgrade; rolling back." >&2
  rollback "$BACKUP_DIR"
  CONFIG_FINGERPRINT_ROLLED_BACK="$(protected_config_fingerprint "$GCP_CREDS")"
  if [[ "$CONFIG_FINGERPRINT_ROLLED_BACK" != "$CONFIG_FINGERPRINT_BEFORE" ]]; then
    echo "FATAL: protected configuration fingerprint did not return to its backed-up value." >&2
    exit 3
  fi
  echo "Protected configuration fingerprint verified after rollback." >&2
  exit 2
fi
echo "Protected configuration fingerprint verified after upgrade."
if configuration_drift > "$BACKUP_DIR/config_drift_after.json" && \
  validate_drift_report < "$BACKUP_DIR/config_drift_after.json"; then
  echo "Post-upgrade configuration report: $BACKUP_DIR/config_drift_after.json"
  cat "$BACKUP_DIR/config_drift_after.json"
else
  rm -f "$BACKUP_DIR/config_drift_after.json"
  echo "NOT TESTED: post-upgrade configuration diagnostic unavailable or invalid; acceptance incomplete. Review manually." >&2
  exit 2
fi

echo "Layered check: diagnostics (non-secret)…"
diag_ok=1
if ! curl -sf "$DIAG_URL" >/dev/null; then
  echo "WARN: diagnostics endpoint not reachable with current auth; health passed but layered check skipped." >&2
  diag_ok=0
fi

curl -sS "$HEALTH_URL" || true
echo
echo "Upgrade complete. Backup at $BACKUP_DIR (layered_diag=$diag_ok)"
echo "Manual rollback if needed:"
echo "  bash $0 rollback $BACKUP_DIR"
