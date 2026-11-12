# Release acceptance for existing installations

The acceptance collector is read-only. It performs GET requests, reads Docker and
Cloud Run metadata, and opens the music index in SQLite read-only mode. It never
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
outcomes, speaker names/UUIDs and visibility, the four Google Home virtual
device IDs, Google Home link and catalog-sync state, Plex library selection,
index counts and scan state, saved-mix IDs and retrieval results, database
integrity, protected configuration fingerprints, Cloud Run's ready revision and
traffic, active revision images/service accounts, and Docker image/mount data.

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
proof that a backup is restorable.

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
Cast UUID, and a private known query. It also refuses to begin unless receiver
state and volume are readable and playback is idle. It lowers volume only and
tries to restore the original volume only while the same connection is
reporting state. This helper performs a short playback/pause check; it does not
test every transport or mute operation. Physical-device testing requires a
person able to observe the receiver. Do not run against an active household
session or a Cast Group. Record receiver state and volume before and after each
operation; HTTP 200 alone is not evidence that the receiver changed state. Do
not restore volume after a disconnect until the receiver has reconnected and
reported its current state.

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
