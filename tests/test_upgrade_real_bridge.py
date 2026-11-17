"""Disposable production bridge smoke test; no real Plex or cloud account."""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest


@pytest.mark.skipif(os.getenv("PCB_TEST_UPGRADE_E2E") != "1", reason="requires local Docker and a published immutable target")
@pytest.mark.parametrize("legacy_env", [False, True])
def test_real_bridge_upgrade_preserves_plex_configuration(tmp_path, legacy_env):
    if os.getenv("DOCKER_HOST", "").startswith(("ssh:", "tcp:")):
        pytest.skip("bind mount test must run on the Docker daemon host")
    root = Path(__file__).resolve().parents[1]
    target = os.environ["PCB_TEST_UPGRADE_TARGET"]
    data = tmp_path / "data"
    data.mkdir(mode=0o777)
    data.chmod(0o777)
    speaker = {"id": "plex-jukebox:cast-casttest", "google_home_device_id": "plex-jukebox:cast-casttest",
               "cast_uuid": "cast-test", "speaker_name": "Test", "google_home_name": "Test",
               "nicknames": ["Test", "Test Plex"], "room_hint": "Test", "roomHint": "Test", "model": "Google Cast",
               "kind": "speaker", "host": "", "port": 8009, "enabled": True, "google_home_visible": True,
               "last_seen": None, "availability": "unknown", "needs_review": False,
               "identity_conflict_original_id": None, "identity_conflict_previous_visibility": None,
               "removed": False, "name": "Test"}
    (data / "catalog.json").write_text(json.dumps({"speakers": [speaker], "inputs": ["Test"]}))
    if not legacy_env:
        (data / "plex_connection.json").write_text(json.dumps({"mode": "jwt", "jwt_token": "synthetic-token",
            "client_identifier": "synthetic-client", "server_url": "http://plex-sim:32400", "music_section": "Music"}))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = {"PCB_DISABLE_BACKGROUND_DISCOVERY": "1", "PLEX_URL": "http://plex-sim:32400"}
    if legacy_env:
        settings["PLEX_TOKEN"] = "synthetic-legacy-token"
    fake_plex = """from http.server import BaseHTTPRequestHandler,HTTPServer
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  if self.path == '/': body='<MediaContainer size="0" friendlyName="Synthetic" machineIdentifier="fake" version="1.40.0" />'
  elif self.path.startswith('/library/sections?') or self.path == '/library/sections': body='<MediaContainer size="1"><Directory type="artist" key="1" title="Music" agent="tv.plex.agents.music" scanner="Plex Music" /></MediaContainer>'
  else: body='<MediaContainer size="0" totalSize="0" />'
  self.send_response(200); self.send_header('Content-Type','application/xml'); self.end_headers(); self.wfile.write(body.encode())
 def log_message(self,*args): pass
HTTPServer(('0.0.0.0',32400),Handler).serve_forever()
"""
    compose = tmp_path / "compose.json"
    compose.write_text(json.dumps({"services": {
        "plex-sim": {"image": "plex-cast-bridge-config-drift-test", "command": ["python", "-c", fake_plex]},
        "bridge": {"image": "plex-cast-bridge-config-drift-test", "depends_on": ["plex-sim"],
                   "environment": settings, "ports": [f"127.0.0.1:{port}:8000"], "volumes": [f"{data}:/data"]}}}))
    env = {**os.environ, "COMPOSE_PROJECT_NAME": "pcbreal" + os.urandom(6).hex()}
    subprocess.run(["docker", "build", "-f", "deploy/Dockerfile", "-t", "plex-cast-bridge-config-drift-test", "."],
                   cwd=root, env=env, check=True, capture_output=True)
    def run(*args):
        return subprocess.run(list(args), cwd=root, env=env, capture_output=True, text=True, timeout=240)
    def cli(*args):
        return run(sys.executable, str(root / "scripts/upgrade.py"), *args)
    plan = tmp_path / "review.json"
    health = f"http://127.0.0.1:{port}/health"
    try:
        result = run("docker", "compose", "-f", str(compose), "up", "-d", "--no-build", "plex-sim")
        assert result.returncode == 0, result.stderr
        for _ in range(40):
            probe = run("docker", "compose", "-f", str(compose), "exec", "-T", "plex-sim", "python", "-c",
                        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:32400',timeout=1)")
            if probe.returncode == 0:
                break
            time.sleep(0.25)
        else:
            pytest.fail("synthetic Plex server did not become ready")
        result = run("docker", "compose", "-f", str(compose), "up", "-d", "--no-build", "bridge")
        assert result.returncode == 0, result.stderr
        for _ in range(40):
            try:
                with urlopen(health, timeout=1) as response:
                    if response.status == 200:
                        break
            except (OSError, URLError):
                time.sleep(0.5)
        else:
            pytest.fail("disposable bridge did not become healthy")
        with urlopen(f"http://127.0.0.1:{port}/api/settings", timeout=5) as response:
            assert json.load(response)["library"]["linked"] is True
        result = cli("plan", "--compose-file", str(compose), "--service", "bridge", "--target", target, "--plan-file", str(plan))
        assert result.returncode == 0, result.stderr
        result = cli("apply", "--plan-file", str(plan), "--confirm-exclusive-data", "--health-url", health)
        assert result.returncode == 0, result.stderr
        with urlopen(f"http://127.0.0.1:{port}/api/settings", timeout=5) as response:
            assert json.load(response)["library"]["linked"] is True
        catalog = json.loads((data / "catalog.json").read_text())
        assert catalog["speakers"][0]["google_home_device_id"] == speaker["google_home_device_id"]
        if not legacy_env:
            connection = json.loads((data / "plex_connection.json").read_text())
            assert connection["jwt_token"] == "synthetic-token"
            assert connection["client_identifier"] == "synthetic-client"
        else:
            assert "synthetic-legacy-token" not in result.stdout + result.stderr
    finally:
        run("docker", "compose", "-f", str(compose), "down", "-v", "--remove-orphans")
