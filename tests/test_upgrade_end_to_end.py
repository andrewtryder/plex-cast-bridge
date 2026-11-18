"""Disposable Compose upgrade/rollback with a synthetic health service."""
import json
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(os.getenv("PCB_TEST_UPGRADE_E2E") != "1", reason="requires local Docker and a published immutable target")
@pytest.mark.parametrize("external_catalog", [False, True])
def test_reviewed_upgrade_then_image_rollback_keeps_existing_data(tmp_path, external_catalog):
    if os.getenv("DOCKER_HOST", "").startswith(("ssh:", "tcp:")):
        pytest.skip("bind mount test must run on the Docker daemon host")
    target = os.environ["PCB_TEST_UPGRADE_TARGET"]
    root = Path(__file__).resolve().parents[1]
    data = tmp_path / "custom-data"
    data.mkdir(mode=0o777)
    data.chmod(0o777)
    (data / "plex_connection.json").write_text(json.dumps({"mode": "jwt", "jwt_token": "synthetic", "client_identifier": "id-1"}))
    catalog = tmp_path / "external-catalog.json" if external_catalog else data / "catalog.json"
    speakers = [{"id": "virtual-1", "cast_uuid": "cast-1", "kind": "speaker"},
                {"id": "virtual-2", "cast_uuid": "cast-2", "kind": "speaker"},
                {"id": "virtual-group", "cast_uuid": "group-1", "kind": "group"}]
    catalog.write_text(json.dumps({"speakers": speakers}))
    db_path = data / "music_index.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.executescript("CREATE TABLE tracks(server_id TEXT,library_id TEXT,rating_key TEXT);"
                         "CREATE TABLE artists(server_id TEXT,library_id TEXT,artist_key TEXT);"
                         "CREATE TABLE albums(server_id TEXT,library_id TEXT,album_key TEXT);"
                         "CREATE TABLE mixes(id INTEGER PRIMARY KEY,name TEXT);"
                         "CREATE TABLE mix_tracks(mix_id INTEGER,position INTEGER,rating_key TEXT);"
                         "INSERT INTO tracks VALUES('s','l','t'); INSERT INTO mixes VALUES(1,'synthetic');"
                         "INSERT INTO mix_tracks VALUES(1,0,'t');")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = ("from http.server import HTTPServer,BaseHTTPRequestHandler; "
              "H=type('H',(BaseHTTPRequestHandler,),{'do_GET':lambda s:(s.send_response(200),s.end_headers()),"
              "'log_message':lambda *a:None}); HTTPServer(('0.0.0.0',8000),H).serve_forever()")
    compose = tmp_path / "compose.json"
    service = {"image": "plex-cast-bridge-config-drift-test", "command": ["python", "-c", server],
               "ports": [f"127.0.0.1:{port}:8000"], "volumes": [f"{data}:/data"]}
    if external_catalog:
        service["environment"] = {"CATALOG_PATH": "/external-catalog.json"}
        service["volumes"].append(f"{catalog}:/external-catalog.json")
    compose.write_text(json.dumps({"services": {"bridge": service}}))
    env = {**os.environ, "COMPOSE_PROJECT_NAME": "pcbe2e" + os.urandom(6).hex()}
    subprocess.run(["docker", "build", "-f", "deploy/Dockerfile", "-t", "plex-cast-bridge-config-drift-test", "."],
                   cwd=root, env=env, check=True, capture_output=True)
    def run(*args):
        return subprocess.run(list(args), cwd=root, env=env, capture_output=True, text=True, timeout=240)
    def cli(*args):
        return run(sys.executable, str(root / "scripts/upgrade.py"), *args)
    plan = tmp_path / "review.json"
    health = f"http://127.0.0.1:{port}/health"
    try:
        start = run("docker", "compose", "-f", str(compose), "up", "-d", "--no-build")
        assert start.returncode == 0, start.stderr
        result = cli("plan", "--compose-file", str(compose), "--target", target, "--plan-file", str(plan))
        assert result.returncode == 0, result.stderr
        result = cli("apply", "--plan-file", str(plan), "--confirm-exclusive-data", "--health-url", health)
        assert result.returncode == 0, result.stderr
        assert {item["id"] for item in json.loads(catalog.read_text())["speakers"]} == {item["id"] for item in speakers}
        assert json.loads((data / "plex_connection.json").read_text())["client_identifier"] == "id-1"
        result = cli("verify", "--plan-file", str(plan), "--health-url", health)
        assert result.returncode == 0, result.stderr
        (data / "new-user-data.json").write_text('{"keep":true}')
        result = cli("rollback", "--plan-file", str(plan), "--confirm-image-rollback", "--health-url", health)
        assert result.returncode == 0, result.stderr
        assert (data / "new-user-data.json").read_text() == '{"keep":true}'
        with sqlite3.connect(db_path) as db:
            assert db.execute("SELECT id FROM mixes").fetchall() == [(1,)]
        result = cli("restore-data", "--plan-file", str(plan), "--confirm-overwrite-data",
                     "--confirm-exclusive-data", "--health-url", health)
        assert result.returncode == 0, result.stderr
        assert not (data / "new-user-data.json").exists()
        journal = json.loads((plan.with_suffix(".backup") / "data-restore-journal.json").read_text())
        assert (Path(journal["preserved"]) / "new-user-data.json").read_text() == '{"keep":true}'
        assert Path(journal["safety_archive"]).exists()
        with sqlite3.connect(db_path) as db:
            assert db.execute("SELECT id FROM mixes").fetchall() == [(1,)]
    finally:
        run("docker", "compose", "-f", str(compose), "down", "-v", "--remove-orphans")


@pytest.mark.skipif(os.getenv("PCB_TEST_UPGRADE_E2E") != "1", reason="requires local Docker and a published immutable target")
@pytest.mark.parametrize("failure_mode", ["startup", "creation"])
def test_failed_candidate_recovers_old_image_without_overwriting_newer_data(tmp_path, failure_mode):
    import shlex
    import shutil
    if os.getenv("DOCKER_HOST", "").startswith(("ssh:", "tcp:")):
        pytest.skip("bind mount test must run on the Docker daemon host")
    root = Path(__file__).resolve().parents[1]
    target = os.environ["PCB_TEST_UPGRADE_TARGET"]
    data = tmp_path / "data"
    data.mkdir(mode=0o777)
    data.chmod(0o777)
    (data / "catalog.json").write_text('{"speakers":[]}')
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # The locally built candidate contains the new diagnostic; the pinned older
    # published image does not. Its startup writes a synthetic file, then fails.
    server = ("import os,sys; from pathlib import Path\n"
              "if not os.path.exists('/app/src/plex_cast_bridge/config_drift.py'):\n"
              " Path('/data/newer.json').write_text('{\\\"keep\\\":true}')\n"
              " sys.exit(1)\n"
              "from http.server import HTTPServer,BaseHTTPRequestHandler\n"
              "H=type('H',(BaseHTTPRequestHandler,),{'do_GET':lambda s:(s.send_response(200),s.end_headers()),"
              "'log_message':lambda *a:None}); HTTPServer(('0.0.0.0',8000),H).serve_forever()")
    compose = tmp_path / "compose.json"
    compose.write_text(json.dumps({"services": {"bridge": {"image": "plex-cast-bridge-config-drift-test",
        "command": ["python", "-c", server], "ports": [f"127.0.0.1:{port}:8000"], "volumes": [f"{data}:/data"]}}}))
    env = {**os.environ, "COMPOSE_PROJECT_NAME": "pcbfail" + os.urandom(6).hex()}
    subprocess.run(["docker", "build", "-f", "deploy/Dockerfile", "-t", "plex-cast-bridge-config-drift-test", "."],
                   cwd=root, env=env, check=True, capture_output=True)
    def run(*args):
        return subprocess.run(list(args), cwd=root, env=env, capture_output=True, text=True, timeout=240)
    def cli(*args):
        return run(sys.executable, str(root / "scripts/upgrade.py"), *args)
    plan = tmp_path / "review.json"
    try:
        result = run("docker", "compose", "-f", str(compose), "up", "-d", "--no-build")
        assert result.returncode == 0, result.stderr
        old = run("docker", "compose", "-f", str(compose), "ps", "-q", "bridge").stdout.strip()
        old_image = json.loads(run("docker", "inspect", old).stdout)[0]["Image"]
        result = cli("plan", "--compose-file", str(compose), "--target", target, "--plan-file", str(plan))
        assert result.returncode == 0, result.stderr
        if failure_mode == "creation":
            # Simulate a daemon creation error after the old container has been
            # removed. All other operations still use the real Docker daemon.
            docker_path = shutil.which("docker")
            assert docker_path
            wrapper_dir = tmp_path / "docker-wrapper"
            wrapper_dir.mkdir()
            wrapper = wrapper_dir / "docker"
            wrapper.write_text(f'''#!/bin/sh
case " $* " in
  *upgrade-image-override.json*" up "*)
    {shlex.quote(docker_path)} rm -f "$PCB_TEST_OLD_CONTAINER" >/dev/null
    echo 'injected candidate creation failure' >&2
    exit 1 ;;
esac
exec {shlex.quote(docker_path)} "$@"
''')
            wrapper.chmod(0o755)
            env["PATH"] = str(wrapper_dir) + os.pathsep + env["PATH"]
            env["PCB_TEST_OLD_CONTAINER"] = old
        result = cli("apply", "--plan-file", str(plan), "--confirm-exclusive-data",
                     "--health-url", f"http://127.0.0.1:{port}/health")
        assert result.returncode != 0
        if failure_mode == "startup":
            assert (data / "newer.json").exists()
        current = run("docker", "compose", "-f", str(compose), "ps", "-q", "bridge").stdout.strip()
        assert json.loads(run("docker", "inspect", current).stdout)[0]["Image"] == old_image
        assert not (plan.with_suffix(".backup") / "apply-completed.json").exists()
        result = cli("rollback", "--plan-file", str(plan), "--confirm-image-rollback",
                     "--health-url", f"http://127.0.0.1:{port}/health")
        assert result.returncode == 0, result.stderr
        if failure_mode == "startup":
            assert (data / "newer.json").read_text() == '{"keep":true}'
    finally:
        run("docker", "compose", "-f", str(compose), "down", "-v", "--remove-orphans")
