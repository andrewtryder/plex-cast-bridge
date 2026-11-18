# Upgrading Plex Cast Bridge

> **Draft:** this workflow is under review and is not yet recommended for general use. Keep your existing installation and backups until the upgrade has been verified.

An ordinary local upgrade replaces the bridge image and keeps its persistent data. It does not run first-time setup or change Cloud Run. You do not need to reconnect Plex or relink Google Home as part of the local upgrade.

## Before you begin

Run these commands on the Docker host: for Proxmox, that means inside the LXC that runs Docker, not on the Proxmox hypervisor. For Docker Compose, use the host where the bridge container runs. Use the upgrade scripts from a checkout or an extracted source archive for the release you are reviewing; a Compose-only installation does not need Git. Replace the example paths with your own.

For a Compose-only installation, download and extract the source archive for the exact reviewed release from the project's [releases page](https://github.com/andrewtryder/plex-cast-bridge/releases), then change into the extracted directory before running the commands below. The archive must include both `scripts/` and `src/`; downloading only `upgrade.py` is insufficient. Keep your existing Compose files and `.env` in their original location. This draft workflow has not yet been released, so do not substitute an older release's scripts.

Stop all other programs and containers that write the bridge's data directory or external catalog file, and prevent them from restarting until the upgrade finishes. The tool checks other running Docker containers at preflight, but cannot prevent a new writer from starting or identify every host process. Make sure you have enough free space for a private backup and know where your existing Compose files are. If you use multiple Compose files, provide `--compose-file` once for each, in order.

## Check the installation and available release

From the extracted source or checkout on the Docker host:

```sh
python3 scripts/upgrade.py doctor \
  --compose-file /path/to/compose.yml \
  --check-updates
```

The command shows the current image and version when the image supplies a version label, the persistent data path, and the latest official release and its immutable image digest. A custom image is not checked against the official release automatically. If the check is unavailable, inspect the project's [releases](https://github.com/andrewtryder/plex-cast-bridge/releases) and obtain the digest from your registry.

If the tool cannot choose the service, add `--service SERVICE_NAME`. For an override file, add another `--compose-file /path/to/override.yml`. Do not proceed if the diagnostic is **NOT TESTED**. A gateway warning needs review: existing environment values may overwrite the saved gateway configuration on startup. Do not delete or regenerate credentials to clear a warning.

## Review and apply

Copy the immutable image reference displayed by the update check and use it as the target. Choose a new private plan path outside the bridge's data directory:

```sh
python3 scripts/upgrade.py plan \
  --compose-file /path/to/compose.yml \
  --target 'ghcr.io/andrewtryder/plex-cast-bridge@sha256:REPLACE_WITH_DIGEST' \
  --plan-file "$HOME/.local/share/plex-cast-bridge/upgrade-review.json"
```

Review the current and target images, persistent data path, backup location, warnings, and proposed actions. The tool records a private plan and checks the installation again before stopping the bridge. A changed installation requires a new plan. The backup is created alongside the plan, is private, and includes the quiesced data directory and reviewed Compose, environment, and mounted subscriber credential inputs.

After you have stopped other writers and prevented them from restarting, and reviewed the plan, explicitly start the upgrade:

```sh
python3 scripts/upgrade.py apply \
  --plan-file "$HOME/.local/share/plex-cast-bridge/upgrade-review.json" \
  --confirm-exclusive-data
```

If the plan shows a gateway warning and you have reviewed its consequences, add `--acknowledge-gateway-drift`. If the health endpoint is not on the default local port, add `--health-url http://127.0.0.1:PORT/health`. The bridge is stopped briefly while its data backup is created and checked by restoring an isolated copy. An unavailable image registry stops the process before the bridge is stopped.

## Verify or recover

The apply command runs verification after replacement. The tool keeps an image override inside the backup directory; retain that directory. If you later run Compose commands manually, include its `upgrade-image-override.json` as the last `-f` file so Compose does not revert to the original image. You can repeat verification later:

```sh
python3 scripts/upgrade.py verify \
  --plan-file "$HOME/.local/share/plex-cast-bridge/upgrade-review.json"
```

Use the same `--health-url` if you supplied one during apply. The music check attempts a SQLite online snapshot in a network-isolated helper with the source mounted recursively read-only. It returns **NOT TESTED** if the Docker version, kernel, filesystem, database state, or permissions do not support that operation. Offline backup verification is separate. A **NOT TESTED** result is not a pass. Keep the backup and investigate before treating the upgrade as complete. In the dashboard, open existing saved mixes and check that their tracks are present, then browse or search the existing music index. Compare these with what you recorded before the upgrade. This manual check does not establish database integrity; retain the verified offline backup. Check Google Home in normal use if applicable. Keep the previous image and backup until you are satisfied.

For image recovery, select the same plan and explicitly confirm the image rollback:

```sh
python3 scripts/upgrade.py rollback \
  --plan-file "$HOME/.local/share/plex-cast-bridge/upgrade-review.json" \
  --confirm-image-rollback
```

This changes the image only. It does not overwrite newer data, credentials, or Compose files with the backup. An older image may not understand data changed by a newer version; if it cannot start, keep the backup and current data and investigate before restoring anything. Never extract a database backup over a running writer. A full data restore replaces the active data with the older backup, so newer changes will no longer appear in the service. It requires separate explicit authorization and all writers must remain stopped.

You can inspect a backup without changing active data. Choose a new destination under a private directory; preserving the original file ownership may require additional host permissions:

```sh
mkdir -p -m 700 "$HOME/.local/share/plex-cast-bridge/restore-review"
python3 scripts/upgrade.py stage-restore \
  --plan-file "$HOME/.local/share/plex-cast-bridge/upgrade-review.json" \
  --destination "$HOME/.local/share/plex-cast-bridge/restore-review/staged-data"
```

The staged copy contains credentials and needs the same protection as the original data. The command refuses to overwrite an existing destination or write inside the active data directory.

If image-only rollback cannot recover the service and you have reviewed the backup, this separate command stops the bridge, creates and verifies a safety backup of current data, preserves the current directory, restores the selected data backup, and starts the previous image:

```sh
python3 scripts/upgrade.py restore-data \
  --plan-file "$HOME/.local/share/plex-cast-bridge/upgrade-review.json" \
  --confirm-overwrite-data --confirm-exclusive-data
```

This is an explicit data replacement. Keep the printed safety backup, preserved directory, and restore journal: they contain newer data and credentials. The command refuses to proceed if it cannot preserve ownership, if a data restore journal already exists, or if the current configuration differs from the reviewed one. It does not replace Compose files, external catalogs, or credentials outside the data directory. If interrupted, leave writers stopped and inspect the journal and preserved directory before taking further action.

## Google Home and Cloud Run

Cloud Run is optional and is not changed by a local upgrade. Existing Google Home users should keep their current cloud service, credentials, Firestore data, and linked devices. Cloud Run image updates are a separate maintainer operation described in [persistent state and upgrades](persistent-state.md); do not rerun first-install provisioning or relink merely to upgrade.

The offline data restore has not yet been validated across all filesystem and container configurations. It stops for manual review when it cannot safely account for additional writable mounts. A catalog outside the data mount is supported only when it is an individually bind-mounted file. Named Docker volumes whose source is inaccessible to the host user also need manual handling. These limitations are being addressed before the workflow is recommended for general users.
