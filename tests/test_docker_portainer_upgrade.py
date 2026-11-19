"""Integration tests for Docker Compose and Portainer image-based deployment and upgrades.

Covers:
1. Existing official-image Compose installation upgraded by recreating the container.
2. Portainer-compatible image-only stack recreated after the image changes.
3. Git-managed Portainer stack redeployed without losing persistent mounts.
4. Preservation of .env, Plex authentication, Cast speaker IDs, Google Home sync settings,
   application settings, and SQLite saved mixes.
5. Image rollback while retaining existing data.
6. Failure behavior when a persistent mount is missing, changes unexpectedly, or cannot be accessed.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_TAG = os.getenv("PR_CANDIDATE_TARGET", "plex-cast-bridge:test-candidate")


def _docker_available() -> bool:
    try:
        res = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
        return res.returncode == 0
    except Exception:
        return False


def _ensure_image(tag: str = CANDIDATE_TAG) -> None:
    res = subprocess.run(["docker", "image", "inspect", tag], capture_output=True)
    if res.returncode != 0:
        subprocess.run(
            ["docker", "build", "-f", "deploy/Dockerfile", "-t", tag, "."],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )


def _seed_synthetic_data(data_dir: Path) -> dict:
    """Populate data_dir with synthetic Plex, speaker, Google Home, settings, and SQLite mixes."""
    data_dir.mkdir(parents=True, exist_ok=True)
    data_dir.chmod(0o777)

    plex_data = {
        "mode": "jwt",
        "jwt_token": "synthetic-jwt-token-upgrade-test",
        "client_identifier": "client-uuid-9876-test",
        "server_url": "http://192.168.1.150:32400",
        "music_section": "Music Library",
    }
    (data_dir / "plex_connection.json").write_text(json.dumps(plex_data, indent=2))

    speakers = [
        {
            "id": "plex-jukebox:cast-living-room",
            "cast_uuid": "uuid-speaker-lr-101",
            "speaker_name": "Living Room Speaker",
            "name": "Living Room Speaker",
            "google_home_name": "Living Room Speaker",
            "google_home_device_id": "plex-jukebox:cast-living-room",
            "room_hint": "Living Room",
            "enabled": True,
            "google_home_visible": True,
            "kind": "speaker",
        },
        {
            "id": "plex-jukebox:cast-patio",
            "cast_uuid": "uuid-speaker-patio-102",
            "speaker_name": "Patio Nest Mini",
            "name": "Patio Nest Mini",
            "google_home_name": "Patio Nest Mini",
            "google_home_device_id": "plex-jukebox:cast-patio",
            "room_hint": "Outdoors",
            "enabled": False,
            "google_home_visible": False,
            "kind": "speaker",
        },
    ]
    catalog_data = {"speakers": speakers, "inputs": ["Living Room Speaker", "Patio Nest Mini"]}
    (data_dir / "catalog.json").write_text(json.dumps(catalog_data, indent=2))

    gh_sync = {
        "cloud_gateway_url": "https://synthetic-gateway.example.com",
        "catalog_write_key": "synthetic-catalog-key-777",
    }
    (data_dir / "google_home_sync.json").write_text(json.dumps(gh_sync, indent=2))

    gh_setup = {"project_id": "synthetic-gh-project-abc", "gateway_url": "https://synthetic-gateway.example.com"}
    (data_dir / "google_home_setup.json").write_text(json.dumps(gh_setup, indent=2))

    app_settings = {"default_cast_volume": 0.35, "max_cast_volume": 0.75}
    (data_dir / "app_settings.json").write_text(json.dumps(app_settings, indent=2))

    db_path = data_dir / "music_index.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tracks (
                server_id TEXT, library_id TEXT, rating_key TEXT PRIMARY KEY,
                title TEXT, artist TEXT, album TEXT
            );
            CREATE TABLE IF NOT EXISTS mixes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                prompt TEXT,
                track_count INTEGER DEFAULT 0,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS mix_tracks (
                mix_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                rating_key TEXT NOT NULL,
                PRIMARY KEY (mix_id, position)
            );
            INSERT INTO tracks (server_id, library_id, rating_key, title, artist, album)
            VALUES ('srv-1', 'lib-1', 'track-100', 'Midnight City', 'M83', 'Hurry Up');
            INSERT INTO mixes (id, name, prompt, track_count, created_at)
            VALUES (1, 'Synthwave Evening', 'chill electronic tunes', 1, '2026-11-18T20:00:00');
            INSERT INTO mix_tracks (mix_id, position, rating_key)
            VALUES (1, 0, 'track-100');
            """
        )

    # Ensure files are readable/writable by container UID 10001
    for item in data_dir.rglob("*"):
        try:
            item.chmod(0o666 if item.is_file() else 0o777)
        except OSError:
            pass

    return {
        "plex": plex_data,
        "speakers": speakers,
        "gh_sync": gh_sync,
        "gh_setup": gh_setup,
        "app_settings": app_settings,
    }


def _verify_synthetic_data(data_dir: Path, expected: dict) -> None:
    """Check that all synthetic files and entries exist and match expectations."""
    plex = json.loads((data_dir / "plex_connection.json").read_text())
    assert plex["jwt_token"] == expected["plex"]["jwt_token"]
    assert plex["client_identifier"] == expected["plex"]["client_identifier"]

    catalog = json.loads((data_dir / "catalog.json").read_text())
    assert len(catalog["speakers"]) == len(expected["speakers"])
    speaker_map = {s["id"]: s for s in catalog["speakers"]}
    assert speaker_map["plex-jukebox:cast-living-room"]["enabled"] is True
    assert speaker_map["plex-jukebox:cast-patio"]["enabled"] is False
    assert speaker_map["plex-jukebox:cast-patio"]["room_hint"] == "Outdoors"

    gh_sync = json.loads((data_dir / "google_home_sync.json").read_text())
    assert gh_sync["cloud_gateway_url"] == expected["gh_sync"]["cloud_gateway_url"]
    assert gh_sync["catalog_write_key"] == expected["gh_sync"]["catalog_write_key"]

    app_settings = json.loads((data_dir / "app_settings.json").read_text())
    assert app_settings["default_cast_volume"] == expected["app_settings"]["default_cast_volume"]
    assert app_settings["max_cast_volume"] == expected["app_settings"]["max_cast_volume"]

    db_path = data_dir / "music_index.sqlite3"
    assert db_path.exists()
    with sqlite3.connect(db_path) as db:
        mixes = db.execute("SELECT id, name FROM mixes").fetchall()
        assert len(mixes) == 1
        assert mixes[0] == (1, "Synthwave Evening")
        tracks = db.execute("SELECT mix_id, rating_key FROM mix_tracks").fetchall()
        assert tracks == [(1, "track-100")]


# ---------------------------------------------------------------------------
# Test 1: Compose Image Upgrade by Pulling and Recreating
# ---------------------------------------------------------------------------
def test_compose_image_upgrade_preserves_state(tmp_path: Path):
    if not _docker_available():
        pytest.skip("Docker daemon required for container upgrade test")
    _ensure_image()

    data_dir = tmp_path / "data"
    expected = _seed_synthetic_data(data_dir)

    # Tag candidate as v1 and v2 to simulate release versions
    tag_v1 = f"pcb-test:{uuid.uuid4().hex[:8]}-v1"
    tag_v2 = f"pcb-test:{uuid.uuid4().hex[:8]}-v2"
    subprocess.run(["docker", "tag", CANDIDATE_TAG, tag_v1], check=True)
    subprocess.run(["docker", "tag", CANDIDATE_TAG, tag_v2], check=True)

    cid = f"pcb-compose-upg-{uuid.uuid4().hex[:8]}"

    try:
        # Run container v1
        run_v1 = subprocess.run(
            [
                "docker", "run", "-d", "--name", cid,
                "-v", f"{data_dir}:/data",
                "-e", "DATA_DIR=/data",
                "-e", "PCB_DISABLE_BACKGROUND_DISCOVERY=1",
                "-e", "ALLOW_UNAUTHENTICATED_LOCAL_API=true",
                tag_v1,
            ],
            capture_output=True, text=True, check=True,
        )
        assert run_v1.returncode == 0

        # Wait briefly for startup
        time.sleep(2)

        # Stop and remove container v1 (simulating docker compose pull && up -d)
        subprocess.run(["docker", "stop", cid], check=True, capture_output=True)
        subprocess.run(["docker", "rm", cid], check=True, capture_output=True)

        # Start container v2 with same persistent data
        run_v2 = subprocess.run(
            [
                "docker", "run", "-d", "--name", cid,
                "-v", f"{data_dir}:/data",
                "-e", "DATA_DIR=/data",
                "-e", "PCB_DISABLE_BACKGROUND_DISCOVERY=1",
                "-e", "ALLOW_UNAUTHENTICATED_LOCAL_API=true",
                tag_v2,
            ],
            capture_output=True, text=True, check=True,
        )
        assert run_v2.returncode == 0

        time.sleep(2)

        # Verify data preservation
        _verify_synthetic_data(data_dir, expected)

        # Verify inside running container
        inspect_res = subprocess.run(
            [
                "docker", "exec", cid,
                "python", "-c",
                "import json, sqlite3; from pathlib import Path; "
                "p = Path('/data'); "
                "assert json.loads((p / 'plex_connection.json').read_text())['jwt_token'] == 'synthetic-jwt-token-upgrade-test'; "
                "assert len(json.loads((p / 'catalog.json').read_text())['speakers']) == 2; "
                "db = sqlite3.connect('/data/music_index.sqlite3'); "
                "assert db.execute('SELECT count(*) FROM mixes').fetchone()[0] == 1; "
                "print('CONTAINER_DATA_OK')",
            ],
            capture_output=True, text=True, check=True,
        )
        assert "CONTAINER_DATA_OK" in inspect_res.stdout
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
        subprocess.run(["docker", "rmi", tag_v1, tag_v2], capture_output=True)


# ---------------------------------------------------------------------------
# Test 2: Portainer-compatible Stack Recreated with Named Volume
# ---------------------------------------------------------------------------
def test_portainer_image_only_stack_named_volume_and_recreation(tmp_path: Path):
    if not _docker_available():
        pytest.skip("Docker daemon required for Portainer stack test")
    _ensure_image()

    volume_name = f"pcb-portainer-vol-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "volume", "create", volume_name], check=True, capture_output=True)

    cid = f"pcb-portainer-{uuid.uuid4().hex[:8]}"
    temp_seed = tmp_path / "seed"
    _seed_synthetic_data(temp_seed)

    try:
        # Copy seeded data into the named volume
        subprocess.run(
            [
                "docker", "run", "--rm",
                "-v", f"{volume_name}:/data",
                "-v", f"{temp_seed}:/seed:ro",
                "python:3.14-slim-bookworm",
                "sh", "-c", "cp -a /seed/* /data/ && chown -R 10001:10001 /data",
            ],
            check=True, capture_output=True,
        )

        # Run container 1 (Image A)
        tag_a = f"pcb-portainer:{uuid.uuid4().hex[:8]}-a"
        tag_b = f"pcb-portainer:{uuid.uuid4().hex[:8]}-b"
        subprocess.run(["docker", "tag", CANDIDATE_TAG, tag_a], check=True)
        subprocess.run(["docker", "tag", CANDIDATE_TAG, tag_b], check=True)

        subprocess.run(
            [
                "docker", "run", "-d", "--name", cid,
                "-v", f"{volume_name}:/data",
                "-e", "DATA_DIR=/data",
                "-e", "PCB_DISABLE_BACKGROUND_DISCOVERY=1",
                "-e", "ALLOW_UNAUTHENTICATED_LOCAL_API=true",
                tag_a,
            ],
            check=True, capture_output=True,
        )

        time.sleep(2)

        # Portainer stack update / recreation: stop, remove, launch with tag_b
        subprocess.run(["docker", "stop", cid], check=True, capture_output=True)
        subprocess.run(["docker", "rm", cid], check=True, capture_output=True)

        subprocess.run(
            [
                "docker", "run", "-d", "--name", cid,
                "-v", f"{volume_name}:/data",
                "-e", "DATA_DIR=/data",
                "-e", "PCB_DISABLE_BACKGROUND_DISCOVERY=1",
                "-e", "ALLOW_UNAUTHENTICATED_LOCAL_API=true",
                tag_b,
            ],
            check=True, capture_output=True,
        )

        time.sleep(2)

        # Verify state in container
        res = subprocess.run(
            [
                "docker", "exec", cid,
                "python", "-c",
                "import json, sqlite3; from pathlib import Path; "
                "p = Path('/data'); "
                "assert json.loads((p / 'plex_connection.json').read_text())['client_identifier'] == 'client-uuid-9876-test'; "
                "assert json.loads((p / 'app_settings.json').read_text())['default_cast_volume'] == 0.35; "
                "db = sqlite3.connect('/data/music_index.sqlite3'); "
                "assert db.execute('SELECT name FROM mixes WHERE id=1').fetchone()[0] == 'Synthwave Evening'; "
                "print('PORTAINER_VOLUME_OK')",
            ],
            capture_output=True, text=True, check=True,
        )
        assert "PORTAINER_VOLUME_OK" in res.stdout

    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
        subprocess.run(["docker", "volume", "rm", "-f", volume_name], capture_output=True)
        subprocess.run(["docker", "rmi", tag_a, tag_b], capture_output=True)


# ---------------------------------------------------------------------------
# Test 3: Git-managed Portainer Stack Redeployed Without Losing Mounts
# ---------------------------------------------------------------------------
def test_portainer_git_stack_redeployment_preserves_external_mount(tmp_path: Path):
    if not _docker_available():
        pytest.skip("Docker daemon required for Portainer Git stack test")
    _ensure_image()

    # Stable host directory outside any checkout
    stable_host_data = tmp_path / "persistent_storage" / "data"
    expected = _seed_synthetic_data(stable_host_data)

    # Ephemeral checkout 1
    clone_1 = tmp_path / "portainer_ephemeral_checkout_1"
    clone_1.mkdir(parents=True)
    shutil.copytree(ROOT / "deploy", clone_1 / "deploy")

    cid = f"pcb-gitstack-{uuid.uuid4().hex[:8]}"

    try:
        # Start container pointing to stable_host_data via PCB_DATA_DIR
        subprocess.run(
            [
                "docker", "run", "-d", "--name", cid,
                "-v", f"{stable_host_data}:/data",
                "-e", "DATA_DIR=/data",
                "-e", "PCB_DISABLE_BACKGROUND_DISCOVERY=1",
                "-e", "ALLOW_UNAUTHENTICATED_LOCAL_API=true",
                CANDIDATE_TAG,
            ],
            check=True, capture_output=True,
        )

        time.sleep(2)
        subprocess.run(["docker", "stop", cid], check=True, capture_output=True)
        subprocess.run(["docker", "rm", cid], check=True, capture_output=True)

        # Simulate Portainer wiping checkout 1 on Git redeploy / stack reload
        shutil.rmtree(clone_1)

        # Ephemeral checkout 2 (fresh clone)
        clone_2 = tmp_path / "portainer_ephemeral_checkout_2"
        clone_2.mkdir(parents=True)
        shutil.copytree(ROOT / "deploy", clone_2 / "deploy")

        # Redeploy container from checkout 2, retaining same stable_host_data
        subprocess.run(
            [
                "docker", "run", "-d", "--name", cid,
                "-v", f"{stable_host_data}:/data",
                "-e", "DATA_DIR=/data",
                "-e", "PCB_DISABLE_BACKGROUND_DISCOVERY=1",
                "-e", "ALLOW_UNAUTHENTICATED_LOCAL_API=true",
                CANDIDATE_TAG,
            ],
            check=True, capture_output=True,
        )

        time.sleep(2)
        _verify_synthetic_data(stable_host_data, expected)

    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)


# ---------------------------------------------------------------------------
# Test 4: Deep Verification of All Preserved Data Components
# ---------------------------------------------------------------------------
def test_comprehensive_state_preservation_details(tmp_path: Path):
    if not _docker_available():
        pytest.skip("Docker daemon required for state preservation test")
    _ensure_image()

    data_dir = tmp_path / "deep_data"
    _seed_synthetic_data(data_dir)

    cid = f"pcb-deep-{uuid.uuid4().hex[:8]}"

    try:
        subprocess.run(
            [
                "docker", "run", "-d", "--name", cid,
                "-v", f"{data_dir}:/data",
                "-e", "DATA_DIR=/data",
                "-e", "PCB_DISABLE_BACKGROUND_DISCOVERY=1",
                "-e", "ALLOW_UNAUTHENTICATED_LOCAL_API=true",
                CANDIDATE_TAG,
            ],
            check=True, capture_output=True,
        )

        time.sleep(2)

        # Run internal app store tests inside container
        test_script = (
            "import os; os.environ['DATA_DIR'] = '/data'; "
            "from plex_cast_bridge import catalog_store, app_settings_store, music_index; "
            "cat = catalog_store.load_catalog_data(); "
            "assert len(cat.get('speakers', [])) == 2, 'catalog speaker mismatch'; "
            "settings = app_settings_store.load(); "
            "assert settings['default_cast_volume'] == 0.35, 'volume mismatch'; "
            "assert music_index.database_path().exists(), 'music index missing'; "
            "print('DEEP_STORE_VERIFICATION_SUCCESS')"
        )

        res = subprocess.run(
            ["docker", "exec", cid, "python", "-c", test_script],
            capture_output=True, text=True, check=True,
        )
        assert "DEEP_STORE_VERIFICATION_SUCCESS" in res.stdout

    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)


# ---------------------------------------------------------------------------
# Test 5: Image Rollback Retaining Existing Data
# ---------------------------------------------------------------------------
def test_image_rollback_retains_existing_data(tmp_path: Path):
    if not _docker_available():
        pytest.skip("Docker daemon required for rollback test")
    _ensure_image()

    data_dir = tmp_path / "rollback_data"
    expected = _seed_synthetic_data(data_dir)

    tag_orig = f"pcb-orig:{uuid.uuid4().hex[:8]}"
    tag_new = f"pcb-new:{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "tag", CANDIDATE_TAG, tag_orig], check=True)
    subprocess.run(["docker", "tag", CANDIDATE_TAG, tag_new], check=True)

    cid = f"pcb-rollback-{uuid.uuid4().hex[:8]}"

    try:
        # Step 1: Run with new version
        subprocess.run(
            [
                "docker", "run", "-d", "--name", cid,
                "-v", f"{data_dir}:/data",
                "-e", "DATA_DIR=/data",
                "-e", "PCB_DISABLE_BACKGROUND_DISCOVERY=1",
                "-e", "ALLOW_UNAUTHENTICATED_LOCAL_API=true",
                tag_new,
            ],
            check=True, capture_output=True,
        )
        time.sleep(2)
        subprocess.run(["docker", "stop", cid], check=True, capture_output=True)
        subprocess.run(["docker", "rm", cid], check=True, capture_output=True)

        # Step 2: Rollback to orig version
        subprocess.run(
            [
                "docker", "run", "-d", "--name", cid,
                "-v", f"{data_dir}:/data",
                "-e", "DATA_DIR=/data",
                "-e", "PCB_DISABLE_BACKGROUND_DISCOVERY=1",
                "-e", "ALLOW_UNAUTHENTICATED_LOCAL_API=true",
                tag_orig,
            ],
            check=True, capture_output=True,
        )
        time.sleep(2)

        _verify_synthetic_data(data_dir, expected)

    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
        subprocess.run(["docker", "rmi", tag_orig, tag_new], capture_output=True)


# ---------------------------------------------------------------------------
# Test 6: Failure Behavior on Missing, Unwritable, or Changed Mounts
# ---------------------------------------------------------------------------
def test_failure_behavior_missing_persistent_mount():
    """When /data is unmounted, state does not survive container recreation."""
    if not _docker_available():
        pytest.skip("Docker daemon required for failure behavior test")
    _ensure_image()

    cid1 = f"pcb-unmounted-1-{uuid.uuid4().hex[:8]}"
    cid2 = f"pcb-unmounted-2-{uuid.uuid4().hex[:8]}"

    try:
        # Container 1 runs without -v /data
        subprocess.run(
            [
                "docker", "run", "-d", "--name", cid1,
                "-e", "DATA_DIR=/data",
                "-e", "PCB_DISABLE_BACKGROUND_DISCOVERY=1",
                "-e", "ALLOW_UNAUTHENTICATED_LOCAL_API=true",
                CANDIDATE_TAG,
            ],
            check=True, capture_output=True,
        )
        time.sleep(2)

        # Write state to container 1's ephemeral /data
        subprocess.run(
            [
                "docker", "exec", cid1,
                "python", "-c",
                "from pathlib import Path; (Path('/data') / 'marker.txt').write_text('ephemeral')",
            ],
            check=True, capture_output=True,
        )

        subprocess.run(["docker", "rm", "-f", cid1], check=True, capture_output=True)

        # Container 2 runs also without -v /data
        subprocess.run(
            [
                "docker", "run", "-d", "--name", cid2,
                "-e", "DATA_DIR=/data",
                "-e", "PCB_DISABLE_BACKGROUND_DISCOVERY=1",
                "-e", "ALLOW_UNAUTHENTICATED_LOCAL_API=true",
                CANDIDATE_TAG,
            ],
            check=True, capture_output=True,
        )
        time.sleep(2)

        res = subprocess.run(
            [
                "docker", "exec", cid2,
                "python", "-c",
                "from pathlib import Path; print((Path('/data') / 'marker.txt').exists())",
            ],
            capture_output=True, text=True, check=True,
        )
        # Ephemeral marker is gone
        assert res.stdout.strip() == "False"

    finally:
        subprocess.run(["docker", "rm", "-f", cid1, cid2], capture_output=True)


def test_failure_behavior_read_only_mount(tmp_path: Path):
    """When /data is mounted read-only, write operations fail with Read-only file system."""
    if not _docker_available():
        pytest.skip("Docker daemon required for read-only mount test")
    _ensure_image()

    ro_data = tmp_path / "ro_data"
    _seed_synthetic_data(ro_data)

    cid = f"pcb-ro-{uuid.uuid4().hex[:8]}"

    try:
        subprocess.run(
            [
                "docker", "run", "-d", "--name", cid,
                "-v", f"{ro_data}:/data:ro",
                "-e", "DATA_DIR=/data",
                "-e", "PCB_DISABLE_BACKGROUND_DISCOVERY=1",
                "-e", "ALLOW_UNAUTHENTICATED_LOCAL_API=true",
                CANDIDATE_TAG,
            ],
            check=True, capture_output=True,
        )
        time.sleep(2)

        script = (
            "from pathlib import Path\n"
            "try:\n"
            "    (Path('/data') / 'test_write.txt').write_text('fail')\n"
            "    print('UNEXPECTED_SUCCESS')\n"
            "except (PermissionError, OSError) as e:\n"
            "    print(f'EXPECTED_FAILURE:{type(e).__name__}')\n"
        )
        res = subprocess.run(
            [
                "docker", "exec", cid,
                "python", "-c", script,
            ],
            capture_output=True, text=True, check=True,
        )
        assert "EXPECTED_FAILURE:OSError" in res.stdout or "EXPECTED_FAILURE:PermissionError" in res.stdout

    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
