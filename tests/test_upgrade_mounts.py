from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.upgrade_mounts import resolve_data_mount


def test_resolves_custom_nested_data_directory(tmp_path):
    (tmp_path / "persistent").mkdir()
    container = {"Mounts": [{"Type": "bind", "Source": str(tmp_path), "Destination": "/state", "RW": True}]}
    assert resolve_data_mount(container, [], "/state/persistent") == tmp_path / "persistent"


def test_rejects_unmounted_nested_or_shared_writable_data(tmp_path):
    mount = {"Type": "bind", "Source": str(tmp_path), "Destination": "/data", "RW": True}
    container = {"Mounts": [mount]}
    with pytest.raises(ValueError, match="not mounted"):
        resolve_data_mount(container, [], "/elsewhere")
    nested = {"Type": "volume", "Source": str(tmp_path), "Destination": "/data/nested", "RW": True}
    with pytest.raises(ValueError, match="nested"):
        resolve_data_mount({"Mounts": [mount, nested]}, [], "/data")
    other = {"State": {"Running": True}, "Mounts": [mount]}
    with pytest.raises(ValueError, match="another running container"):
        resolve_data_mount(container, [other], "/data")
    mount_ro = {**mount, "RW": False}
    assert resolve_data_mount(container, [{"State": {"Running": True}, "Mounts": [mount_ro]}], "/data") == tmp_path


def test_image_rollback_does_not_restore_or_overwrite_live_data(tmp_path):
    import os
    import subprocess
    import tarfile

    app = tmp_path / "app"
    (app / "data").mkdir(parents=True)
    (app / "data" / "state.json").write_text('{"newer": true}')
    (app / ".env").write_text("CURRENT=1\n")
    compose = app / "compose.yml"
    compose.write_text("services: {}\n")
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "docker-compose.yml").write_text("old compose")
    (backup / ".env").write_text("OLD=1\n")
    (backup / "previous_image.txt").write_text("sha256:old\n")
    (backup / "previous_image_digest.txt").write_text("sha256:old\n")
    old_data = tmp_path / "old"
    old_data.mkdir()
    (old_data / "state.json").write_text('{"old": true}')
    with tarfile.open(backup / "data.tgz", "w:gz") as tar:
        tar.add(old_data, arcname="data")
    import hashlib
    (backup / "data.tgz.sha256").write_text(hashlib.sha256((backup / "data.tgz").read_bytes()).hexdigest() + "\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, code in {"docker": "exit 0", "curl": "exit 0"}.items():
        path = bin_dir / name
        path.write_text("#!/bin/sh\n" + code + "\n")
        path.chmod(0o755)
    env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
           "APP_DIR": str(app), "COMPOSE_FILE": str(compose)}
    script = Path(__file__).resolve().parents[1] / "scripts/proxmox_upgrade.sh"
    result = subprocess.run(["bash", str(script), "rollback", str(backup)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (app / "data" / "state.json").read_text() == '{"newer": true}'
    assert (app / ".env").read_text() == "CURRENT=1\n"
    assert compose.read_text() == "services: {}\n"


def test_failed_pull_leaves_service_running_and_data_untouched(tmp_path):
    import json
    import os
    import subprocess

    app = tmp_path / "app"
    (app / "data").mkdir(parents=True)
    (app / "data" / "sentinel").write_text("unchanged")
    (app / "src/plex_cast_bridge").mkdir(parents=True)
    (app / "src/plex_cast_bridge/config_drift.py").write_text("unused")
    compose = app / "compose.yml"
    compose.write_text("services: {}")
    drift = tmp_path / "drift.json"
    names = ("plex_connection.json", "app_settings.json", "catalog.json", "google_home_sync.json", "google_home_setup.json", "music_index.sqlite3")
    drift.write_text(json.dumps({"files": {n: "absent" for n in names}, "settings": {
        n: {"conflict": False, "classification": "ok"} for n in ("cloud_gateway_url", "catalog_write_key")
    }, "release_preflight_warnings": []}))
    logs = tmp_path / "docker.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text('''#!/bin/sh
echo "$*" >> "$FAKE_LOG"
case "$*" in
  *"exec -T"*) cat "$FAKE_DRIFT" ;;
  *"ps -q"*) echo cid ;;
  *"inspect -f"*) echo sha256:old ;;
  *"config --format json"*) echo '{"services":{"plex-cast-bridge":{"image":"test:latest"}}}' ;;
  pull*) exit 1 ;;
esac
''')
    docker.chmod(0o755)
    env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"], "APP_DIR": str(app),
           "COMPOSE_FILE": str(compose), "FAKE_LOG": str(logs), "FAKE_DRIFT": str(drift),
           "TARGET_IMAGE": "example/image@sha256:" + "a" * 64}
    script = Path(__file__).resolve().parents[1] / "scripts/proxmox_upgrade.sh"
    result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "pull example/image@sha256:" in logs.read_text()
    assert " stop " not in logs.read_text()
    assert (app / "data" / "sentinel").read_text() == "unchanged"
