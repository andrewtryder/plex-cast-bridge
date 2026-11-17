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
def test_reviewed_upgrade_then_image_rollback_keeps_existing_data(tmp_path):
    if os.getenv("DOCKER_HOST", "").startswith(("ssh:", "tcp:")):
        pytest.skip("bind mount test must run on the Docker daemon host")
    target = os.environ["PCB_TEST_UPGRADE_TARGET"]
    root = Path(__file__).resolve().parents[1]
    data = tmp_path / "custom-data"
    data.mkdir(mode=0o777)
    data.chmod(0o777)
    (data / "plex_connection.json").write_text(json.dumps({"mode": "jwt", "jwt_token": "synthetic", "client_identifier": "id-1"}))
    (data / "catalog.json").write_text(json.dumps({"speakers": [{"id": "virtual-1", "cast_uuid": "cast-1"}]}))
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
    compose.write_text(json.dumps({"services": {"bridge": {"image": "plex-cast-bridge-config-drift-test",
                       "command": ["python", "-c", server], "ports": [f"127.0.0.1:{port}:8000"],
                       "volumes": [f"{data}:/data"]}}}))
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
        assert json.loads((data / "catalog.json").read_text())["speakers"][0]["id"] == "virtual-1"
        assert json.loads((data / "plex_connection.json").read_text())["client_identifier"] == "id-1"
        result = cli("verify", "--plan-file", str(plan), "--health-url", health)
        assert result.returncode == 0, result.stderr
        (data / "new-user-data.json").write_text('{"keep":true}')
        result = cli("rollback", "--plan-file", str(plan), "--confirm-image-rollback", "--health-url", health)
        assert result.returncode == 0, result.stderr
        assert (data / "new-user-data.json").read_text() == '{"keep":true}'
        with sqlite3.connect(db_path) as db:
            assert db.execute("SELECT id FROM mixes").fetchall() == [(1,)]
    finally:
        run("docker", "compose", "-f", str(compose), "down", "-v", "--remove-orphans")
