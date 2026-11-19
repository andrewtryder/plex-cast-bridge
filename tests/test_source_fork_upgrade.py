"""Tests for native Python, virtual environment migration, systemd services, and fork upgrade readiness.

Covers:
1. Native Python installations using Git and uv.
2. Existing Python virtual environments transitioning to Python 3.14.
3. Existing systemd services retaining environment and external persistent data directory.
4. Developers rebuilding from source using the separate Compose build override.
5. Fork maintainers publishing and updating custom container images.
6. Consistency of persistent data paths across code-checkout changes.
7. Preservation of existing credentials, settings, catalog, speaker identities, and SQLite state.
8. Fail-closed upgrade protection (PCB_REQUIRE_EXISTING_DATA).
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _seed_synthetic_data(data_dir: Path) -> dict:
    """Populate data_dir with synthetic Plex, speaker, Google Home, settings, and SQLite mixes."""
    data_dir.mkdir(parents=True, exist_ok=True)

    plex_data = {
        "mode": "jwt",
        "jwt_token": "synthetic-jwt-token-source-upgrade-test",
        "client_identifier": "client-uuid-source-9876",
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

    app_settings = {"default_cast_volume": 0.42, "max_cast_volume": 0.88}
    (data_dir / "app_settings.json").write_text(json.dumps(app_settings, indent=2))

    db_path = data_dir / "music_index.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.executescript(
            """
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE scan_state (
                id INTEGER PRIMARY KEY CHECK (id=1), status TEXT NOT NULL DEFAULT 'idle',
                server_id TEXT, library_id TEXT, started_at TEXT, completed_at TEXT,
                scanned INTEGER NOT NULL DEFAULT 0, error TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO scan_state (id) VALUES (1);
            CREATE TABLE tracks (
                server_id TEXT NOT NULL, library_id TEXT NOT NULL, rating_key TEXT NOT NULL,
                title TEXT NOT NULL, artist TEXT, artist_key TEXT, album TEXT, album_key TEXT,
                year INTEGER, duration_ms INTEGER, genres TEXT NOT NULL DEFAULT '[]',
                moods TEXT NOT NULL DEFAULT '[]', updated_at TEXT, play_count INTEGER,
                PRIMARY KEY(server_id, library_id, rating_key)
            );
            CREATE TABLE mixes (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, server_id TEXT NOT NULL,
                library_id TEXT NOT NULL, created_at TEXT NOT NULL, provenance TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE mix_tracks (
                mix_id INTEGER NOT NULL REFERENCES mixes(id) ON DELETE CASCADE,
                position INTEGER NOT NULL, rating_key TEXT NOT NULL,
                PRIMARY KEY(mix_id, position)
            );
            PRAGMA user_version = 3;

            INSERT INTO tracks (server_id, library_id, rating_key, title, artist, album)
            VALUES
                ('srv1', 'lib1', 'trk-1001', 'Midnight City', 'M83', 'Hurry Up, We Are Dreaming'),
                ('srv1', 'lib1', 'trk-1002', 'Reckoner', 'Radiohead', 'In Rainbows');

            INSERT INTO mixes (id, name, server_id, library_id, created_at)
            VALUES (1, 'Source Upgrade Mix', 'srv1', 'lib1', '2026-11-18T12:00:00Z');

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
        "track_count": 2,
        "mix_name": "Source Upgrade Mix",
    }


def _verify_synthetic_data(data_dir: Path, expected: dict) -> None:
    """Verify all synthetic data in data_dir matches expected values."""
    plex_file = data_dir / "plex_connection.json"
    assert plex_file.exists(), "plex_connection.json missing"
    plex = json.loads(plex_file.read_text())
    assert plex["jwt_token"] == expected["plex"]["jwt_token"]
    assert plex["server_url"] == expected["plex"]["server_url"]

    cat_file = data_dir / "catalog.json"
    assert cat_file.exists(), "catalog.json missing"
    catalog = json.loads(cat_file.read_text())
    speaker_ids = [s["id"] for s in catalog.get("speakers", [])]
    assert "plex-jukebox:cast-living-room" in speaker_ids
    assert "plex-jukebox:cast-patio" in speaker_ids

    gh_file = data_dir / "google_home_sync.json"
    assert gh_file.exists(), "google_home_sync.json missing"
    gh_sync = json.loads(gh_file.read_text())
    assert gh_sync["catalog_write_key"] == expected["gh_sync"]["catalog_write_key"]

    app_file = data_dir / "app_settings.json"
    assert app_file.exists(), "app_settings.json missing"
    app_settings = json.loads(app_file.read_text())
    assert app_settings["default_cast_volume"] == expected["app_settings"]["default_cast_volume"]
    assert app_settings["max_cast_volume"] == expected["app_settings"]["max_cast_volume"]

    db_path = data_dir / "music_index.sqlite3"
    assert db_path.exists(), "music_index.sqlite3 missing"
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
# Test 1: Native Python installation using Git and uv
# ---------------------------------------------------------------------------
def test_native_python_uv_sync_workflow(tmp_path: Path):
    """Verify clean native installation using uv in an isolated directory."""
    if not shutil.which("uv"):
        pytest.skip("uv binary not available")

    install_dir = tmp_path / "native-install"
    shutil.copytree(
        ROOT,
        install_dir,
        ignore=shutil.ignore_patterns(".venv", ".git", "__pycache__", "*.pyc", "data"),
    )

    # Run uv sync to build and install dependencies in isolated directory
    res = subprocess.run(
        ["uv", "sync"],
        cwd=install_dir,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"uv sync failed:\n{res.stderr}"

    venv_python = install_dir / ".venv" / "bin" / "python"
    assert venv_python.exists(), ".venv/bin/python was not created by uv sync"

    # Verify python version in venv is Python 3.14
    ver_res = subprocess.run(
        [str(venv_python), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert ver_res.stdout.strip() == "3.14", f"Expected Python 3.14 in venv, got {ver_res.stdout.strip()}"

    # Verify package imports cleanly
    import_res = subprocess.run(
        [str(venv_python), "-c", "import plex_cast_bridge; print(plex_cast_bridge.__file__)"],
        cwd=install_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "plex_cast_bridge" in import_res.stdout


# ---------------------------------------------------------------------------
# Test 2: Existing virtual environment transitioning to Python 3.14
# ---------------------------------------------------------------------------
def test_existing_virtualenv_transition_to_python_314(tmp_path: Path):
    """Verify an existing Python 3.12/3.13 venv is safely migrated to 3.14 on uv sync."""
    if not shutil.which("uv"):
        pytest.skip("uv binary not available")

    # Find an older python (e.g. 3.12 or 3.13)
    older_python = shutil.which("python3.12") or shutil.which("python3.13")
    if not older_python:
        pytest.skip("Neither python3.12 nor python3.13 available to test venv upgrade transition")

    project_dir = tmp_path / "venv-migration"
    shutil.copytree(
        ROOT,
        project_dir,
        ignore=shutil.ignore_patterns(".venv", ".git", "__pycache__", "*.pyc", "data"),
    )

    # Pre-create .venv using older Python
    venv_dir = project_dir / ".venv"
    subprocess.run([older_python, "-m", "venv", str(venv_dir)], check=True, capture_output=True)

    old_ver = subprocess.run(
        [str(venv_dir / "bin" / "python"), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert old_ver in {"3.12", "3.13"}, f"Initial venv should be 3.12 or 3.13, got {old_ver}"

    # Now execute uv sync in the project
    res = subprocess.run(["uv", "sync"], cwd=project_dir, capture_output=True, text=True)
    assert res.returncode == 0, f"uv sync failed during venv migration:\n{res.stderr}"

    new_ver = subprocess.run(
        [str(venv_dir / "bin" / "python"), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert new_ver == "3.14", f"Expected migrated venv to be Python 3.14, got {new_ver}"


# ---------------------------------------------------------------------------
# Test 3: Existing systemd service retaining environment and external data directory
# ---------------------------------------------------------------------------
def test_systemd_service_environment_and_external_data_dir(tmp_path: Path):
    """Verify systemd service configuration respects external persistent DATA_DIR."""
    service_file = ROOT / "deploy" / "plex-cast-bridge.service"
    assert service_file.exists(), "deploy/plex-cast-bridge.service missing"
    content = service_file.read_text()

    # Verify unit structure
    assert "Documentation=https://github.com/andrewtryder/plex-cast-bridge" in content
    assert "WorkingDirectory=/opt/plex-cast-bridge" in content
    assert 'Environment="DATA_DIR=/opt/plex-cast-bridge/data"' in content
    assert "EnvironmentFile=/opt/plex-cast-bridge/.env" in content
    assert "ExecStart=/opt/plex-cast-bridge/.venv/bin/uvicorn plex_cast_bridge.main:app" in content

    # Simulate external host data directory (e.g. /var/lib/plex-cast-bridge/data)
    external_data = tmp_path / "var-lib-pcb-data"
    expected = _seed_synthetic_data(external_data)

    # Test preflight script pointing to external data dir
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"DATA_DIR={external_data}\n"
        "PLEX_URL=http://192.168.1.150:32400\n"
        "PLEX_TOKEN=test-token\n"
        "ENABLE_PUBSUB_LISTENER=false\n"
    )

    res = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "preflight.py"),
            "--env-file", str(env_file),
            "--data-dir", str(external_data),
            "--skip-plex",
            "--require-existing-data",
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"Preflight failed on external data directory:\n{res.stderr}\n{res.stdout}"
    assert f"Found existing data files in {external_data}" in res.stdout
    _verify_synthetic_data(external_data, expected)


# ---------------------------------------------------------------------------
# Test 4: Developers rebuilding from source using Compose build override
# ---------------------------------------------------------------------------
def test_compose_build_override_developer_workflow(tmp_path: Path):
    """Verify separate Compose build override enables local building without altering base compose."""
    if not shutil.which("docker"):
        pytest.skip("docker binary required for compose config test")

    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    shutil.copy(ROOT / "deploy" / "docker-compose.yml", deploy_dir / "docker-compose.yml")
    shutil.copy(ROOT / "deploy" / "docker-compose.build.yml", deploy_dir / "docker-compose.build.yml")

    # Provide synthetic .env in parent directory as expected by default ${PCB_ENV_FILE:-../.env}
    (tmp_path / ".env").write_text("PLEX_URL=http://192.168.1.100:32400\n")

    # Base compose config should not have build section
    res_base = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "config"],
        cwd=deploy_dir,
        capture_output=True,
        text=True,
    )
    assert res_base.returncode == 0, f"Base compose config failed:\n{res_base.stderr}"
    assert "build:" not in res_base.stdout
    assert "ghcr.io/andrewtryder/plex-cast-bridge:latest" in res_base.stdout

    # With build override, build section is present and image defaults to plex-cast-bridge:local
    res_override = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.build.yml", "config"],
        cwd=deploy_dir,
        capture_output=True,
        text=True,
    )
    assert res_override.returncode == 0, f"Override compose config failed:\n{res_override.stderr}"
    assert "build:" in res_override.stdout
    assert "plex-cast-bridge:local" in res_override.stdout


# ---------------------------------------------------------------------------
# Test 5: Fork maintainers publishing and updating custom container images
# ---------------------------------------------------------------------------
def test_fork_maintainer_custom_image_workflow(tmp_path: Path):
    """Verify PCB_CUSTOM_IMAGE is respected by build override and image-only deployment."""
    if not shutil.which("docker"):
        pytest.skip("docker binary required for compose config test")

    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    shutil.copy(ROOT / "deploy" / "docker-compose.yml", deploy_dir / "docker-compose.yml")
    shutil.copy(ROOT / "deploy" / "docker-compose.build.yml", deploy_dir / "docker-compose.build.yml")
    shutil.copy(ROOT / "deploy" / "docker-compose.portainer.yml", deploy_dir / "docker-compose.portainer.yml")

    (tmp_path / ".env").write_text("PLEX_URL=http://192.168.1.100:32400\n")

    custom_image = "ghcr.io/forkmaintainer/plex-cast-bridge:v2.5.0"
    env = {**os.environ, "PCB_CUSTOM_IMAGE": custom_image}

    # Test 1: Build override tags with custom image
    res_build = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.build.yml", "config"],
        cwd=deploy_dir,
        env=env,
        capture_output=True,
        text=True,
    )
    assert res_build.returncode == 0
    assert custom_image in res_build.stdout

    # Test 2: Standard Compose pulls/uses custom image
    res_base = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "config"],
        cwd=deploy_dir,
        env=env,
        capture_output=True,
        text=True,
    )
    assert res_base.returncode == 0
    assert custom_image in res_base.stdout, "deploy/docker-compose.yml should expand PCB_CUSTOM_IMAGE"

    # Test 3: Portainer git-stack template pulls/uses custom image
    res_port = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.portainer.yml", "config"],
        cwd=deploy_dir,
        env={**env, "PCB_DATA_DIR": "/opt/plex-cast-bridge/data"},
        capture_output=True,
        text=True,
    )
    assert res_port.returncode == 0
    assert custom_image in res_port.stdout, "deploy/docker-compose.portainer.yml should expand PCB_CUSTOM_IMAGE"


# ---------------------------------------------------------------------------
# Test 6: Consistency of persistent data paths across code-checkout changes
# ---------------------------------------------------------------------------
def test_persistent_data_across_code_checkout_moves(tmp_path: Path):
    """Verify persistent data directory retains state when code is checked out in a new location."""
    stable_data = tmp_path / "permanent_host_data"
    expected = _seed_synthetic_data(stable_data)

    env_file = tmp_path / "common.env"
    env_file.write_text(
        f"DATA_DIR={stable_data}\n"
        "PLEX_URL=http://192.168.1.150:32400\n"
        "PLEX_TOKEN=test-token\n"
        "ENABLE_PUBSUB_LISTENER=false\n"
    )

    # Checkout 1
    checkout_1 = tmp_path / "checkout_v1"
    shutil.copytree(ROOT, checkout_1, ignore=shutil.ignore_patterns(".venv", ".git", "data"))

    # Checkout 2 (simulating a git clone into a new directory or Portainer stack recreation)
    checkout_2 = tmp_path / "checkout_v2"
    shutil.copytree(ROOT, checkout_2, ignore=shutil.ignore_patterns(".venv", ".git", "data"))

    # Run validation pointing to stable_data from checkout 1
    res1 = subprocess.run(
        [
            sys.executable,
            str(checkout_1 / "scripts" / "preflight.py"),
            "--env-file", str(env_file),
            "--data-dir", str(stable_data),
            "--skip-plex",
            "--require-existing-data",
        ],
        cwd=checkout_1,
        capture_output=True,
        text=True,
    )
    assert res1.returncode == 0, f"Checkout 1 preflight failed:\n{res1.stderr}\n{res1.stdout}"

    # Run validation pointing to stable_data from checkout 2
    res2 = subprocess.run(
        [
            sys.executable,
            str(checkout_2 / "scripts" / "preflight.py"),
            "--env-file", str(env_file),
            "--data-dir", str(stable_data),
            "--skip-plex",
            "--require-existing-data",
        ],
        cwd=checkout_2,
        capture_output=True,
        text=True,
    )
    assert res2.returncode == 0, f"Checkout 2 preflight failed:\n{res2.stderr}\n{res2.stdout}"

    # Verify no state was clobbered
    _verify_synthetic_data(stable_data, expected)


# ---------------------------------------------------------------------------
# Test 7: Fail-closed upgrade protection (PCB_REQUIRE_EXISTING_DATA)
# ---------------------------------------------------------------------------
def test_fail_closed_empty_data_dir_protection(tmp_path: Path):
    """Verify that an empty or missing data directory is rejected when PCB_REQUIRE_EXISTING_DATA is set."""
    empty_data = tmp_path / "empty_dir"
    empty_data.mkdir()

    env_file = tmp_path / "dummy.env"
    env_file.write_text("PLEX_URL=http://localhost:32400\nENABLE_PUBSUB_LISTENER=false\n")

    res = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "preflight.py"),
            "--env-file", str(env_file),
            "--data-dir", str(empty_data),
            "--skip-plex",
            "--require-existing-data",
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0, "Preflight must fail when require_existing_data is set on empty dir"
    assert "Refusing upgrade preflight" in res.stdout
