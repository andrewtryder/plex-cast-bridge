# Release acceptance for existing installations

The acceptance collector avoids GET endpoints that trigger discovery, catalog migration, index initialization, or scan recovery. It reads the selected catalog directly inside the running container and opens the music index in immutable read-only mode. An active SQLite WAL or journal makes index verification NOT TESTED; retry after a consistent offline snapshot is available. It never
plays, pauses, changes volume, or stops a Cast device. Reports and the HMAC key
are stored outside the repository under
`~/.local/share/plex-cast-bridge/release-acceptance` (directory mode `0700`, files
mode `0600`). Do not share these private reports publicly; they contain device
UUIDs and installation identity metadata, but no credential values or music
titles.

## Before an upgrade

Run the collector from the Proxmox LXC or the Docker host where `docker inspect`
can see `plex-cast-bridge`. For the current installation:

```sh
cd /opt/plex-cast-bridge
python3 /path/to/plex-cast-bridge/scripts/release_acceptance.py capture \
  --api-url http://127.0.0.1:8000 \
  --env-file /opt/plex-cast-bridge/.env \
  --credentials-file /opt/plex-cast-bridge/gcp-credentials.json \
  --data-dir /opt/plex-cast-bridge/data \
  --backup-dir /opt/plex-cast-bridge/backups \
  --project plex-cast-bridge-1ffe19
```

If the local API key is enabled, set `API_KEY` in the invoking shell; it is sent
only in the `X-API-Key` header. Do not put API keys in a URL or command line. The
collector records each GET's HTTP status and elapsed time, diagnostics
outcomes, speaker names/UUIDs and visibility, the existing Google Home virtual
device IDs, Google Home link and catalog-sync state, Plex library selection,
index counts and scan state, saved-mix IDs from the index when safely readable, database
integrity, protected configuration fingerprints, Cloud Run's ready revision and
traffic, active revision images/service accounts, and Docker image/mount data.
Speaker online/offline availability is recorded for reference but excluded from
the identity comparison; Cast UUIDs, virtual IDs, names, enabled state, and
Google Home visibility remain fail-closed identity checks.

If `gcloud` is available only on the workstation, capture Cloud Run separately
there; this writes a sanitized private snapshot with a stable local HMAC key:

```sh
python3 scripts/release_acceptance.py capture-cloud \
  --project plex-cast-bridge-1ffe19 --region us-central1
```

`OAUTH_SIGNING_SECRET`, OAuth client credentials, catalog write keys, project
identity, Plex connection values, and subscriber private-key material are never
written to the report. The HMAC key is generated once and kept in the private
state directory so fingerprints remain comparable across captures on this host.
Back up that private directory if reports must be compared from another machine;
never copy the key into the repository.

Before replacing an image, independently verify that the existing backup contains
`data/`, `.env`, Compose configuration, and the separate Google subscriber
credential file. Record the previous image digest and perform the documented
rollback procedure in a disposable validation window. A file count alone is not
proof that a backup is restorable. The collector requires the recorded archive
checksum, restores the archive into an isolated temporary directory, checks
SQLite integrity, checks the configured environment and credential files when
present, and confirms the previous image is cached. An older archive without an
integrity record is NOT TESTED by this check. This does not prove that a full
service rollback will succeed, that restored ownership can be recreated, or that
every custom mount was backed up.

## After an image upgrade

Run the same `capture` command and compare the two generated JSON paths:

```sh
python3 /path/to/plex-cast-bridge/scripts/release_acceptance.py compare \
  --scope local \
  ~/.local/share/plex-cast-bridge/release-acceptance/baseline-BEFORE.json \
  ~/.local/share/plex-cast-bridge/release-acceptance/baseline-AFTER.json
```

The comparison fails closed on changes to protected configuration, persistent
mounts, account-link state, stable virtual-device IDs, Cast UUID mappings and
speaker visibility, mix IDs, Plex link/library, database integrity, or backup
availability. Image digests, application commits, Cloud Run revisions, sync
versions, playback state, scan timestamps, and library counts are reported but
are expected to change or be reviewed separately. Confirm that the *new* image
matches the intended published artifact and that the Cloud Run revision change
was intentional. A passing comparison is necessary but does not prove playback,
voice recognition, or rollback success.

Compare local LXC reports on the LXC, and compare workstation Cloud Run reports
with each other using `compare --scope cloud`. The default `compare --scope all`
requires complete local and Cloud Run evidence in both files and fails closed if
either scope is missing. The HMAC keys are machine-local by design; do not move
them into the repository.

### Cloud Run caveat

Cloud Run metadata is collected only when `gcloud` is installed and authenticated
to the explicit `--project`; use `capture-cloud` when the Docker host has no
Cloud SDK. A missing `cloud_run.available` is **NOT TESTED**, not a pass. Never
use the provisioning script for an image-only upgrade. Do not
run `gcp-cloud-run/deploy.sh` as a routine release operation: it provisions
resources and applies environment configuration. Use the existing image-only
upgrade process and verify the service configuration before and after.

## Browser dashboard acceptance

Run the existing browser regressions from the repository with its supported
browser-test setup. For live acceptance, open the local dashboard at desktop and
390px widths while Home is playing. Select Master Bedroom and wait at least 15
seconds through normal status polling. Confirm the selector remains on Master
Bedroom, the UI distinguishes selected output from the actively playing speaker,
and pause/resume/next/stop controls target the selected output. Do not change
Google Home visibility or room assignments. If the dashboard cannot be reached,
record the existing browser regression results and mark live browser acceptance
**NOT TESTED**.

## Real-device playback and voice acceptance

The release collector is read-only. `scripts/test_live_cast.py` is the separate
physical-device helper and refuses to run without `--allow-disruptive`, an exact
Cast UUID, a private known query, and the base URL for the actual local
dashboard API. It refuses to begin unless the exact receiver connection,
initial volume, and idle state are readable; Cast Groups are excluded. It
reads an optional `API_KEY` from the environment and sends it only in the
`X-API-Key` header.
starts playback through `/api/command`, then uses `/api/transport` for pause,
resume, bounded volume up/down, and stop. It checks receiver state after each
request, retries stop during cleanup when the receiver does not confirm idle,
and restores the original volume only after stop is receiver-confirmed and the
same Cast connection still reports a readable volume. If stop remains
unconfirmed, it leaves the safe test volume in place and reports that fact.
Query text is not printed by the helper, and
dashboard command failures no longer log the query. Mute/unmute is deliberately
not exercised unless the deployed application and worker both support it.
Physical-device testing requires a person able to observe the receiver. Do not
run against an active household session or a Cast Group. HTTP 200 alone is not
evidence that the receiver changed state. Do not restore volume after a
disconnect until the receiver has reconnected and reported its current state.

Example (only after confirming the receiver is idle and authorizing the
disruptive test):

```sh
python3 scripts/test_live_cast.py --allow-disruptive \
  --base-url http://127.0.0.1:8000 \
  --device-uuid CAST_UUID_FROM_PRIVATE_BASELINE
```

The helper prompts for the known library query with terminal echo disabled.
For an automated local run, pass `--query-stdin` and pipe one query line without
printing it in the invoking shell.

`scripts/test_voice_fulfillment.py` is a simulated Cloud Run EXECUTE request,
not a Google Assistant voice-recognition test. It requires `--allow-disruptive`,
an explicit project, a stable virtual device ID, and a Pub/Sub subscription whose
name starts with `test-`; it pulls and acknowledges one message from that
subscription. Never point it at the production worker subscription.

Run Android Assistant tests separately from dashboard and worker tests:

1. Start a known library track from the dashboard on **Master Bedroom Plex**.
2. Say “Hey Google, pause Master Bedroom Plex”; record Assistant's response,
   Cloud Run EXECUTE receipt, Pub/Sub publication/delivery, Docker worker
   received/completed entries, and Cast state transition.
3. Repeat for resume and stop. Correlate timestamps and device IDs across layers.
4. Report each layer independently. Local HTTP playback does not prove Assistant
   interpreted a voice command.

Do not relink Google Home or force OAuth refresh as a diagnostic shortcut.

## Release decision record

For each candidate release, attach private before/after baseline references and
record `PASS`, `FAIL`, or `NOT TESTED` for:

| Acceptance item | Evidence |
| --- | --- |
| Docker image and application commit | immutable image ID and OCI revision label |
| Cloud Run revision and identity | ready/traffic revisions, image, service account, explicit project |
| Preserved configuration and mounts | equal HMAC fingerprints and mount sources |
| Plex and Google Home continuity | GET smoke checks and unchanged stable IDs |
| Data integrity and persistence | read-only SQLite integrity, counts, mix IDs/retrieval |
| Browser output selection | live desktop/mobile observations and browser regressions |
| Cast controls | receiver state transitions and restored safe volume |
| Assistant voice routing | distinct Assistant, Cloud Run, Pub/Sub, worker, receiver evidence |
| Rollback readiness | verified backup contents and tested restore procedure |
| Provisioning isolation | regression tests prove normal upgrade does not provision or mint credentials |

The release is **NO-GO** if an identity check fails, a protected fingerprint or
persistent mount changes unexpectedly, a link/device ID changes, the SQLite
integrity check fails, or backup/rollback has not been verified. Unit tests alone
are not sufficient for GO.

Local capture includes a redacted `local.configuration_drift` report and summarizes gateway conflicts as `release_preflight_warnings` for review. Capture runs the checkout's diagnostic inside the running container, so it uses the container environment and works with older images. If it cannot run, `available` is false; comparison reports the required diagnostic as NOT TESTED and cannot pass. A new or changed gateway drift classification also requires review. Review pre- and post-upgrade captures before proceeding. Differences between JWT and legacy Plex tokens and unsaved volume defaults are not treated as conflicts. Cloud-only captures cannot inspect local configuration.

## v0.5.0 Preflight Acceptance Matrix

Before publishing v0.5.0, evaluate readiness across both automated/simulated preflight suites and operator-approved live environments:

### Automated & Synthetic Preflight Verification

| Target Surface | Upgrade Mechanism | State Safety Requirements | Automated Preflight Status |
| --- | --- | --- | --- |
| **Docker Compose (Generic)** | Image-only pull (`docker compose pull && docker compose up -d`) | Host volume `/data` preserves Plex tokens, Cast UUIDs, virtual device IDs, `app_settings.json`, and `music_index.sqlite3` | PASS (verified via disposable candidate container upgrade suite) |
| **Portainer (Web Editor Simulation)** | Re-pull image & recreate container via Web UI | Stack environment variables (`PCB_DATA_DIR`, `PCB_ENV_FILE`, `PCB_CUSTOM_IMAGE`) preserved; persistent host volume untouched | PASS (verified via synthetic simulation in `test_docker_portainer_upgrade.py`) |
| **Portainer (Git Stack Simulation)** | Git pull & stack redeploy using `deploy/docker-compose.portainer.yml` | Image pulled from GHCR; build directive absent; persistent mount retained | PASS (verified via synthetic simulation in `test_docker_portainer_upgrade.py`) |
| **Automated Updates (Watchtower)** | Container recreation on image change | Fails closed (`PCB_REQUIRE_EXISTING_DATA`) if mount is missing or unmounted; no state loss | PASS (verified with fail-closed container startup checks) |
| **Native Python (`uv`)** | `git pull && uv sync` | Automatic virtualenv migration from Python 3.12/3.13 to 3.14; existing config in legacy locations preserved | PASS (verified in test suite `test_source_fork_upgrade.py`) |
| **Systemd Service** | `git pull && uv sync && systemctl restart plex-cast-bridge` | Service unit does not hardcode `DATA_DIR`; preserves legacy location precedence (`/data` -> `data/` -> project root) and `.env` overrides | PASS (verified in test suite `test_source_fork_upgrade.py`) |
| **Source Rebuilds** | `docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.build.yml up -d --build` | Base Compose remains image-only; build override targets `plex-cast-bridge:local` or custom tag | PASS (verified in test suite `test_source_fork_upgrade.py`) |
| **Fork Deployments** | Compose / Portainer with `PCB_CUSTOM_IMAGE` | Custom image tag forwarded to service definition without editing repository Compose files | PASS (verified in test suite `test_source_fork_upgrade.py`) |
| **SQLite Music Index & Mixes** | Online read-only snapshot or offline backup | Database schema user_version 3 intact; tracks, mixes, and provenance preserved across container restart | PASS (verified in test suite `test_upgrade_sqlite_online.py`) |
| **Cloud Run Gateway (Mocked/E2E)** | Independent container image deployment | OAuth tokens, refresh token deduplication, retry-safe authorization codes, and linked Google Home devices preserved | PASS (verified in Cloud Run test suite and retry tests) |

### Live Production Acceptance Gates

Automated simulations are not a substitute for live environment verification. The following live acceptance gates remain **PENDING** and must be performed in their respective environments prior to or as part of operator-approved production rollout:

| Environment | Scope & Method | Required Baseline Evidence | Status |
| --- | --- | --- | --- |
| **Proxmox LXC 112 (Live Docker)** | Local bridge image replacement using `release_acceptance.py capture` before and after upgrade | Equal HMAC fingerprints for protected config, verified mount sources, unchanged Plex/Google Home device identities, SQLite integrity check PASS | **PENDING LIVE ACCEPTANCE** (Disposable Linux Docker upgrade tests PASS) |
| **Live Portainer Host** | Web Editor image re-pull and Git-stack redeployment on live Portainer host | Unchanged container volume mappings, preserved stack environment variables, verified container startup without data reset | **PENDING LIVE ACCEPTANCE** (Synthetic Compose simulation PASS) |
| **Production Cloud Run** | Staged revision deployment and traffic migration using `release_acceptance.py capture-cloud` | Cloud Run revision traffic staged without dropping active tokens, Firestore OAuth state preserved, voice fulfillment verified | **PENDING LIVE ACCEPTANCE** (Cloud Run integration test suite PASS) |
| **Live Google Assistant Voice E2E** | Live smart speaker command execution (play, pause, resume, volume) | Assistant voice intent correctly routed through Cloud Run, Pub/Sub, Docker worker, and Google Cast speaker | **PENDING LIVE ACCEPTANCE** (Pub/Sub worker mock & receiver tests PASS) |
