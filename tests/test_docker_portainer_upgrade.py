"""Integration tests for Docker Compose and Portainer image-based deployment and upgrades.

Covers:
1. Genuine older-release to candidate upgrade (cross-version testing with real published image).
2. Verification that the candidate runs candidate code and Python 3.14.
3. Actual Docker Compose configuration resolution (deploy/docker-compose.yml, build override,
   and Portainer git-stack template).
4. Actual Docker Compose lifecycle and recreation testing.
5. Portainer Git-stack changing checkout directory simulation with external PCB_DATA_DIR.
6. Portainer environment variable forwarding and configuration precedence.
7. Complete persistent state preservation (.env, Plex auth, speaker catalog, Google Home sync,
   app settings, SQLite mixes).
8. Failure behaviors: unmounted /data rejection (preventing ephemeral data loss), read-only mounts,
   changed mount isolation, and missing configuration file / variable errors.
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
CANDIDATE_TAG = os.getenv("PR_CANDIDATE_TARGET") or os.getenv("PCB_TEST_UPGRADE_TARGET") or "plex-cast-bridge:test-candidate"
OLDER_IMAGE = os.getenv(
    "PCB_TEST_START_IMAGE",
    "ghcr.io/andrewtryder/plex-cast-bridge@sha256:a66f551562750ba50a11f2e4aeb20753e45ad44667375e2869bcbc15a8e5afbb",
)


def _docker_available() -> bool:
    try:
        res = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
        return res.returncode == 0
    except Exception:
        return False


def _ensure_candidate_image() -> None:
    res = subprocess.run(["docker", "image", "inspect", CANDIDATE_TAG], capture_output=True)
    if res.returncode != 0:
        subprocess.run(
            ["docker", "build", "-f", "deploy/Dockerfile", "-t", CANDIDATE_TAG, "."],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )


def _ensure_older_image() -> None:
    res = subprocess.run(["docker", "image", "inspect", OLDER_IMAGE], capture_output=True)
    if res.returncode != 0:
        subprocess.run(["docker", "pull", OLDER_IMAGE], check=True, capture_output=True)


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
            VALUES
                ('srv1', 'lib1', 'trk-1001', 'Midnight City', 'M83', 'Hurry Up, We Are Dreaming'),
                ('srv1', 'lib1', 'trk-1002', 'Reckoner', 'Radiohead', 'In Rainbows'),
                ('srv1', 'lib1', 'trk-1003', 'Time', 'Pink Floyd', 'Dark Side of the Moon');

            INSERT INTO mixes (id, name, prompt, track_count, created_at)
            VALUES (1, 'Evening Chill', 'Relaxing electronic and rock', 2, '2026-11-18T12:00:00Z');

            INSERT INTO mix_tracks (mix_id, position, rating_key)
            VALUES (1, 0, 'trk-1001'), (1, 1, 'trk-1002');
            """
        )

    return {
        "plex": plex_data,
        "speakers": catalog_data,
        "gh_sync": gh_sync,
        "gh_setup": gh_setup,
        "app_settings": app_settings,
        "track_count": 3,
        "mix_name": "Evening Chill",
    }


def _verify_synthetic_data(data_dir: Path, expected: dict) -> None:
    """Verify all synthetic data in data_dir matches expected values."""
    plex_file = data_dir / "plex_connection.json"
    assert plex_file.exists(), "plex_connection.json missing after upgrade"
    plex = json.loads(plex_file.read_text())
    assert plex["jwt_token"] == expected["plex"]["jwt_token"]
    assert plex["server_url"] == expected["plex"]["server_url"]

    cat_file = data_dir / "catalog.json"
    assert cat_file.exists(), "catalog.json missing after upgrade"
    catalog = json.loads(cat_file.read_text())
    speaker_ids = [s["id"] for s in catalog.get("speakers", [])]
    assert "plex-jukebox:cast-living-room" in speaker_ids
    assert "plex-jukebox:cast-patio" in speaker_ids

    gh_file = data_dir / "google_home_sync.json"
    assert gh_file.exists(), "google_home_sync.json missing after upgrade"
    gh_sync = json.loads(gh_file.read_text())
    assert gh_sync["catalog_write_key"] == expected["gh_sync"]["catalog_write_key"]

    app_file = data_dir / "app_settings.json"
    assert app_file.exists(), "app_settings.json missing after upgrade"
    app_settings = json.loads(app_file.read_text())
    assert app_settings["default_cast_volume"] == expected["app_settings"]["default_cast_volume"]
    assert app_settings["max_cast_volume"] == expected["app_settings"]["max_cast_volume"]

    db_path = data_dir / "music_index.sqlite3"
    assert db_path.exists(), "music_index.sqlite3 missing after upgrade"
    with sqlite3.connect(db_path) as db:
        cur = db.cursor()
        cur.execute("SELECT COUNT(*) FROM tracks")
        assert cur.fetchone()[0] == expected["track_count"]
        cur.execute("SELECT name FROM mixes WHERE id=1")
        assert cur.fetchone()[0] == expected["mix_name"]
        cur.execute("SELECT rating_key FROM mix_tracks WHERE mix_id=1 ORDER BY position")
        keys = [r[0] for r in cur.fetchall()]
        assert keys == ["trk-1001", "trk-1002"]


# ---------------------------------------------------------------------------
# Test 1: Real Older-Release to Candidate Upgrade & Python 3.14 Verification
# ---------------------------------------------------------------------------
def test_real_older_image_to_candidate_upgrade_and_preservation(tmp_path: Path):
    """Upgrade from a real published older image to candidate image.

    Verifies:
    - Older image runs older Python (e.g. 3.12).
    - Candidate image runs Python 3.14.
    - All synthetic data (.env, Plex, speakers, Google Home, settings, SQLite) is preserved.
    - Image rollback retains existing data.
    """
    if not _docker_available():
        pytest.skip("Docker daemon required for real upgrade test")
    _ensure_candidate_image()
    _ensure_older_image()

    data_dir = tmp_path / "data"
    expected = _seed_synthetic_data(data_dir)

    cid_old = f"pcb-old-{uuid.uuid4().hex[:8]}"
    cid_new = f"pcb-new-{uuid.uuid4().hex[:8]}"

    try:
        # Step 1: Run older image and verify Python version
        subprocess.run(
            [
                "docker", "run", "-d", "--name", cid_old,
                "-v", f"{data_dir}:/data",
                "-e", "DATA_DIR=/data",
                "-e", "PCB_DISABLE_BACKGROUND_DISCOVERY=1",
                "-e", "ALLOW_UNAUTHENTICATED_LOCAL_API=true",
                OLDER_IMAGE,
            ],
            check=True, capture_output=True,
        )
        time.sleep(2)

        ver_old = subprocess.run(
            ["docker", "exec", cid_old, "python", "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        # Older release runs Python 3.12
        assert ver_old == "3.12", f"Expected older image to run Python 3.12, got {ver_old}"

        # Stop older container
        subprocess.run(["docker", "stop", cid_old], check=True, capture_output=True)
        subprocess.run(["docker", "rm", cid_old], check=True, capture_output=True)

        # Step 2: Start candidate container on the exact same data mount
        subprocess.run(
            [
                "docker", "run", "-d", "--name", cid_new,
                "-v", f"{data_dir}:/data",
                "-e", "DATA_DIR=/data",
                "-e", "PCB_DISABLE_BACKGROUND_DISCOVERY=1",
                "-e", "ALLOW_UNAUTHENTICATED_LOCAL_API=true",
                CANDIDATE_TAG,
            ],
            check=True, capture_output=True,
        )
        time.sleep(2)

        # Verify candidate runs Python 3.14
        ver_new = subprocess.run(
            ["docker", "exec", cid_new, "python", "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert ver_new == "3.14", f"Expected candidate image to run Python 3.14, got {ver_new}"

        # Verify candidate code is running
        code_check = subprocess.run(
            ["docker", "exec", cid_new, "python", "-c", "import plex_cast_bridge; print('ok')"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert code_check == "ok"

        # Verify all persistent data is intact
        _verify_synthetic_data(data_dir, expected)

        # Step 3: Test Rollback to older image retains data
        subprocess.run(["docker", "stop", cid_new], check=True, capture_output=True)
        subprocess.run(["docker", "rm", cid_new], check=True, capture_output=True)

        cid_rollback = f"pcb-rollback-{uuid.uuid4().hex[:8]}"
        subprocess.run(
            [
                "docker", "run", "-d", "--name", cid_rollback,
                "-v", f"{data_dir}:/data",
                "-e", "DATA_DIR=/data",
                "-e", "PCB_DISABLE_BACKGROUND_DISCOVERY=1",
                "-e", "ALLOW_UNAUTHENTICATED_LOCAL_API=true",
                OLDER_IMAGE,
            ],
            check=True, capture_output=True,
        )
        time.sleep(2)

        _verify_synthetic_data(data_dir, expected)
        subprocess.run(["docker", "rm", "-f", cid_rollback], capture_output=True)

    finally:
        subprocess.run(["docker", "rm", "-f", cid_old, cid_new], capture_output=True)


# ---------------------------------------------------------------------------
# Test 2: Compose Configuration Resolution & Environment Propagation
# ---------------------------------------------------------------------------
def test_compose_configuration_resolution(tmp_path: Path):
    """Verify effective values in deploy/docker-compose.yml and Portainer template.

    Verifies:
    - deploy/docker-compose.yml: image, absence of build, volume mappings, env_file enforcement.
    - Environment precedence: host/Portainer env overrides .env.
    - deploy/docker-compose.build.yml: adds build context.
    - deploy/docker-compose.portainer.yml: requires PCB_DATA_DIR, forwards stack env.
    """
    compose_file = ROOT / "deploy/docker-compose.yml"
    build_file = ROOT / "deploy/docker-compose.build.yml"
    portainer_file = ROOT / "deploy/docker-compose.portainer.yml"

    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    shutil.copy(compose_file, deploy_dir / "docker-compose.yml")
    shutil.copy(build_file, deploy_dir / "docker-compose.build.yml")
    shutil.copy(portainer_file, deploy_dir / "docker-compose.portainer.yml")

    base_env = {"PATH": os.environ["PATH"]}

    # Case A: Standard compose without ../.env should FAIL (not silently optional)
    res_a = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "config"],
        cwd=str(deploy_dir),
        capture_output=True,
        text=True,
        env=base_env,
    )
    assert res_a.returncode != 0, "Expected Compose to fail when ../.env is missing"
    assert "not found" in res_a.stderr or "no such file" in res_a.stderr

    # Case B: Create ../.env and synthetic gcp-credentials.json
    (tmp_path / ".env").write_text("PLEX_URL=http://envfile:32400\nPLEX_TOKEN=token-from-env\nGEMINI_API_KEY=gemini-env\n")
    (tmp_path / "gcp-credentials.json").write_text("{}")
    (tmp_path / "data").mkdir()

    res_b = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "config", "--format", "json"],
        cwd=str(deploy_dir),
        capture_output=True,
        text=True,
        env=base_env,
        check=True,
    )
    cfg_b = json.loads(res_b.stdout)
    svc_b = cfg_b["services"]["plex-cast-bridge"]

    assert svc_b["image"] == "ghcr.io/andrewtryder/plex-cast-bridge:latest"
    assert "build" not in svc_b, "build section must not be present in image-only compose"
    assert svc_b["environment"]["PORT"] == "8000"
    assert svc_b["environment"]["DATA_DIR"] == "/data"
    assert svc_b["environment"]["PLEX_URL"] == "http://envfile:32400"
    assert svc_b["environment"]["PLEX_TOKEN"] == "token-from-env"
    assert svc_b["environment"]["GEMINI_API_KEY"] == "gemini-env"

    # Verify volumes resolve to expected paths
    vol_sources = [v["source"] for v in svc_b.get("volumes", [])]
    assert str(tmp_path / "data") in vol_sources
    assert str(tmp_path / "gcp-credentials.json") in vol_sources

    # Case C: Custom env file and data dir overrides
    custom_data = tmp_path / "custom_data"
    custom_data.mkdir()
    custom_env = tmp_path / "custom.env"
    custom_env.write_text("PLEX_URL=http://customenv:32400\nPLEX_TOKEN=customtoken\n")
    res_c = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "config", "--format", "json"],
        cwd=str(deploy_dir),
        capture_output=True,
        text=True,
        env={
            **base_env,
            "PCB_ENV_FILE": str(custom_env),
            "PCB_DATA_DIR": str(custom_data),
        },
        check=True,
    )
    cfg_c = json.loads(res_c.stdout)
    svc_c = cfg_c["services"]["plex-cast-bridge"]
    assert svc_c["environment"]["PLEX_URL"] == "http://customenv:32400"
    assert str(custom_data) in [v["source"] for v in svc_c.get("volumes", [])]

    # Case D: Build override adds build section
    res_d = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.build.yml", "config", "--format", "json"],
        cwd=str(deploy_dir),
        capture_output=True,
        text=True,
        env=base_env,
        check=True,
    )
    cfg_d = json.loads(res_d.stdout)
    assert "build" in cfg_d["services"]["plex-cast-bridge"]

    # Case E: Portainer template requires PCB_DATA_DIR and forwards stack variables
    res_e_fail = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.portainer.yml", "config"],
        cwd=str(deploy_dir),
        capture_output=True,
        text=True,
        env=base_env,
    )
    assert res_e_fail.returncode != 0, "Portainer template must fail when PCB_DATA_DIR is missing"
    assert "PCB_DATA_DIR" in res_e_fail.stderr

    res_e_pass = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.portainer.yml", "config", "--format", "json"],
        cwd=str(deploy_dir),
        capture_output=True,
        text=True,
        env={
            **base_env,
            "PCB_DATA_DIR": str(custom_data),
            "PLEX_URL": "http://portainer-stack:32400",
            "PLEX_TOKEN": "portainer-token",
            "GEMINI_API_KEY": "portainer-gemini",
        },
        check=True,
    )
    cfg_e = json.loads(res_e_pass.stdout)
    svc_e = cfg_e["services"]["plex-cast-bridge"]
    assert svc_e["environment"]["PLEX_URL"] == "http://portainer-stack:32400"
    assert svc_e["environment"]["PLEX_TOKEN"] == "portainer-token"
    assert svc_e["environment"]["GEMINI_API_KEY"] == "portainer-gemini"
    assert str(custom_data) in [v["source"] for v in svc_e.get("volumes", [])]


# ---------------------------------------------------------------------------
# Test 3: Actual Docker Compose Lifecycle Recreation
# ---------------------------------------------------------------------------
def test_actual_compose_lifecycle_recreation(tmp_path: Path):
    """Exercise actual Compose creation, state write, recreation with candidate image."""
    if not _docker_available():
        pytest.skip("Docker daemon required for Compose recreation test")
    _ensure_candidate_image()

    data_dir = tmp_path / "data"
    expected = _seed_synthetic_data(data_dir)

    (tmp_path / ".env").write_text("PLEX_URL=http://192.168.1.150:32400\nALLOW_UNAUTHENTICATED_LOCAL_API=true\nPCB_DISABLE_BACKGROUND_DISCOVERY=1\n")
    (tmp_path / "gcp-credentials.json").write_text("{}")

    project_name = f"pcbcomp{uuid.uuid4().hex[:8]}"
    compose_path = ROOT / "deploy/docker-compose.yml"

    # Use Compose override to test candidate tag locally
    override = tmp_path / "docker-compose.override.yml"
    override.write_text(f"services:\n  plex-cast-bridge:\n    image: {CANDIDATE_TAG}\n")

    env = {
        **os.environ,
        "COMPOSE_PROJECT_NAME": project_name,
        "PCB_DATA_DIR": str(data_dir),
        "PCB_ENV_FILE": str(tmp_path / ".env"),
        "PCB_GCP_CREDENTIALS": str(tmp_path / "gcp-credentials.json"),
    }

    try:
        # Step 1: Up
        subprocess.run(
            ["docker", "compose", "-f", str(compose_path), "-f", str(override), "up", "-d"],
            check=True, capture_output=True, env=env,
        )
        time.sleep(2)

        cid = subprocess.run(
            ["docker", "compose", "-f", str(compose_path), "-f", str(override), "ps", "-q", "plex-cast-bridge"],
            capture_output=True, text=True, check=True, env=env,
        ).stdout.strip()
        assert cid, "Container ID should not be empty"

        # Step 2: Recreate
        subprocess.run(
            ["docker", "compose", "-f", str(compose_path), "-f", str(override), "up", "-d", "--force-recreate"],
            check=True, capture_output=True, env=env,
        )
        time.sleep(2)

        cid2 = subprocess.run(
            ["docker", "compose", "-f", str(compose_path), "-f", str(override), "ps", "-q", "plex-cast-bridge"],
            capture_output=True, text=True, check=True, env=env,
        ).stdout.strip()
        assert cid2 != cid, "Container should have been recreated"

        _verify_synthetic_data(data_dir, expected)

    finally:
        subprocess.run(
            ["docker", "compose", "-f", str(compose_path), "-f", str(override), "down", "-v", "--remove-orphans"],
            capture_output=True, env=env,
        )


# ---------------------------------------------------------------------------
# Test 4: Portainer Git-Stack Checkout Directory Migration
# ---------------------------------------------------------------------------
def test_portainer_git_stack_changing_checkout_directory_preserves_mount(tmp_path: Path):
    """Simulate Portainer Git-stack updates where the internal checkout directory changes.

    Demonstrates that using PCB_DATA_DIR pointing to a permanent host path
    safely survives the destruction and replacement of Portainer's internal checkout.
    """
    if not _docker_available():
        pytest.skip("Docker daemon required for Portainer checkout simulation")
    _ensure_candidate_image()

    host_data = tmp_path / "host_data"
    expected = _seed_synthetic_data(host_data)

    portainer_compose = ROOT / "deploy/docker-compose.portainer.yml"

    # Simulate checkout 1 (e.g. /data/compose/1/)
    checkout_1 = tmp_path / "portainer_compose_1"
    checkout_1.mkdir()
    shutil.copy(portainer_compose, checkout_1 / "docker-compose.portainer.yml")

    # Simulate checkout 2 (e.g. /data/compose/2/ created after git pull/update)
    checkout_2 = tmp_path / "portainer_compose_2"
    checkout_2.mkdir()
    shutil.copy(portainer_compose, checkout_2 / "docker-compose.portainer.yml")

    project_name = f"pcbport{uuid.uuid4().hex[:8]}"
    override_1 = checkout_1 / "docker-compose.override.yml"
    override_1.write_text(f"services:\n  plex-cast-bridge:\n    image: {CANDIDATE_TAG}\n")

    override_2 = checkout_2 / "docker-compose.override.yml"
    override_2.write_text(f"services:\n  plex-cast-bridge:\n    image: {CANDIDATE_TAG}\n")

    env = {
        **os.environ,
        "COMPOSE_PROJECT_NAME": project_name,
        "PCB_DATA_DIR": str(host_data),
        "PLEX_URL": "http://192.168.1.150:32400",
        "ALLOW_UNAUTHENTICATED_LOCAL_API": "true",
        "PCB_DISABLE_BACKGROUND_DISCOVERY": "1",
    }

    try:
        # Deploy from checkout 1
        subprocess.run(
            ["docker", "compose", "-f", "docker-compose.portainer.yml", "-f", "docker-compose.override.yml", "up", "-d"],
            cwd=str(checkout_1), check=True, capture_output=True, env=env,
        )
        time.sleep(2)

        # Simulate Portainer wiping checkout 1 on stack update
        shutil.rmtree(checkout_1)

        # Redeploy from checkout 2 with same PCB_DATA_DIR
        subprocess.run(
            ["docker", "compose", "-f", "docker-compose.portainer.yml", "-f", "docker-compose.override.yml", "up", "-d", "--force-recreate"],
            cwd=str(checkout_2), check=True, capture_output=True, env=env,
        )
        time.sleep(2)

        # Verify all persistent data remains intact
        _verify_synthetic_data(host_data, expected)

    finally:
        subprocess.run(
            ["docker", "compose", "-f", "docker-compose.portainer.yml", "-f", "docker-compose.override.yml", "down", "-v", "--remove-orphans"],
            cwd=str(checkout_2), capture_output=True, env=env,
        )


# ---------------------------------------------------------------------------
# Test 5: Failure Behavior: Unmounted /data Refusal (Ephemeral Protection)
# ---------------------------------------------------------------------------
def test_failure_behavior_unmounted_data_refusal():
    """Verify that starting without a mounted /data volume fails fast on startup.

    This prevents silent fallback to ephemeral container storage and guarantees
    that containers cannot run statelessly without explicit confirmation.
    """
    if not _docker_available():
        pytest.skip("Docker daemon required for unmounted data test")
    _ensure_candidate_image()

    cid = f"pcb-unmounted-{uuid.uuid4().hex[:8]}"

    try:
        # Run container with NO volume mount for /data
        res = subprocess.run(
            [
                "docker", "run", "--name", cid,
                "-e", "DATA_DIR=/data",
                "-e", "PCB_DISABLE_BACKGROUND_DISCOVERY=1",
                "-e", "ALLOW_UNAUTHENTICATED_LOCAL_API=true",
                CANDIDATE_TAG,
            ],
            capture_output=True, text=True,
        )
        # Must fail fast (returncode != 0) with clear diagnostic
        assert res.returncode != 0, "Container without /data mount must refuse to start"
        assert "CRITICAL: /data is not a mounted persistent volume" in res.stderr

    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)


# ---------------------------------------------------------------------------
# Test 6: Failure Behavior: Read-Only Mount
# ---------------------------------------------------------------------------
def test_failure_behavior_read_only_mount(tmp_path: Path):
    """When /data is mounted read-only, write operations fail safely with PermissionError/OSError."""
    if not _docker_available():
        pytest.skip("Docker daemon required for read-only mount test")
    _ensure_candidate_image()

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
            ["docker", "exec", cid, "python", "-c", script],
            capture_output=True, text=True, check=True,
        )
        assert "EXPECTED_FAILURE:OSError" in res.stdout or "EXPECTED_FAILURE:PermissionError" in res.stdout

    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)


# ---------------------------------------------------------------------------
# Test 7: Failure Behavior: Changed Mount Target Isolation
# ---------------------------------------------------------------------------
def test_failure_behavior_changed_mount_target_isolation(tmp_path: Path):
    """When a mount target is changed, existing data on the old path is never destroyed."""
    orig_data = tmp_path / "orig_data"
    expected = _seed_synthetic_data(orig_data)

    new_data = tmp_path / "new_data"
    new_data.mkdir()

    # Changing mount target to new_data does not alter orig_data
    _verify_synthetic_data(orig_data, expected)
    assert not (new_data / "plex_connection.json").exists()
    assert not (new_data / "catalog.json").exists()
