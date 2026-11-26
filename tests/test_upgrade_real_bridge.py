"""Disposable production bridge smoke test; no real Plex or cloud account."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    legacy_start = bool(os.getenv("PCB_TEST_START_IMAGE"))
    if legacy_start:
        # 0.2.1 predates the music index; seed representative newer-format
        # state explicitly so preservation can still be tested across it.
        subprocess.run(["docker", "pull", target], cwd=root, check=True, capture_output=True)
        (data / "google_home_sync.json").write_text(json.dumps({
            "cloud_gateway_url": "http://gateway.invalid", "catalog_write_key": "synthetic-gateway-key"}))
        (data / "google_home_setup.json").write_text(json.dumps({"project_id": "synthetic-project"}))
        seed = ("import sqlite3; from plex_cast_bridge import music_index; music_index.initialize(); "
                "db=sqlite3.connect('/data/music_index.sqlite3'); "
                "db.execute(\"INSERT INTO tracks(server_id,library_id,rating_key,title) VALUES('s','l','t','Track')\"); "
                "db.execute(\"INSERT INTO mixes(id,name,server_id,library_id,created_at) VALUES(1,'Saved','s','l','2020')\"); "
                "db.execute(\"INSERT INTO mix_tracks(mix_id,position,rating_key) VALUES(1,0,'t')\"); db.commit()")
        subprocess.run(["docker", "run", "--rm", "--pull", "never", "--network", "none", "--entrypoint", "python",
                        "--mount", f"type=bind,src={data},dst=/data", "-e", "DATA_DIR=/data", target, "-c", seed],
                       cwd=root, check=True, capture_output=True)
    if not legacy_env:
        (data / "plex_connection.json").write_text(json.dumps({"mode": "jwt", "jwt_token": "synthetic-token",
            "client_identifier": "synthetic-client", "server_url": "http://plex-sim:32400", "music_section": "Music"}))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = {"PCB_DISABLE_BACKGROUND_DISCOVERY": "1", "ALLOW_UNAUTHENTICATED_LOCAL_API": "true"}
    env_file = tmp_path / ".env"
    env_file.write_text("PLEX_URL=http://plex-sim:32400\n" + ("PLEX_TOKEN=synthetic-legacy-token\n" if legacy_env else ""))
    if legacy_env:
        settings["PLEX_MUSIC_SECTION"] = "Music"
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
    test_image = os.getenv("PCB_TEST_LOCAL_IMAGE", "pcb-real-test:" + os.urandom(6).hex())
    compose = tmp_path / "compose.yaml"
    compose.write_text(json.dumps({"services": {
        "plex-sim": {"image": test_image, "command": ["python", "-c", fake_plex], "networks": ["isolated"]},
        "bridge": {"image": os.getenv("PCB_TEST_START_IMAGE", test_image), "depends_on": ["plex-sim"],
                   "env_file": [str(env_file)], "environment": settings, "networks": ["isolated"],
                   "volumes": [f"{data}:/data"]}},
        "networks": {"isolated": {"internal": True}}}))
    reviewed_compose = compose.read_bytes()
    reviewed_env = env_file.read_bytes()
    env = {**os.environ, "COMPOSE_PROJECT_NAME": "pcbreal" + os.urandom(6).hex()}
    built_test_image = os.getenv("PCB_TEST_PREBUILT_IMAGE") != "1"
    if built_test_image:
        subprocess.run(["docker", "build", "-f", "deploy/Dockerfile", "-t", test_image, "."],
                       cwd=root, env=env, check=True, capture_output=True)
    def run(*args):
        return subprocess.run(list(args), cwd=root, env=env, capture_output=True, text=True, timeout=240)
    def cli(*args):
        return run(sys.executable, str(root / "scripts/upgrade.py"), *args)
    # The internal Docker network cannot reach a real Google Home account.
    # Forward only local test HTTP requests through docker exec, so the health
    # and settings checks work even when Docker Desktop hides internal ports.
    class Proxy(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                endpoint = "/health"
            elif self.path == "/api/settings":
                endpoint = "/api/settings"
            else:
                self.send_error(404)
                return
            probe = run("docker", "compose", "-f", str(compose), "exec", "-T", "bridge", "python", "-c",
                        "import sys,urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000'+sys.argv[1],timeout=3).read().decode())",
                        endpoint)
            self.send_response(200 if probe.returncode == 0 else 503)
            self.end_headers()
            self.wfile.write(probe.stdout.encode())
        def log_message(self, *args):
            pass
    proxy = ThreadingHTTPServer(("127.0.0.1", port), Proxy)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
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
        original = run("docker", "compose", "-f", str(compose), "ps", "-q", "bridge").stdout.strip()
        original_image = json.loads(run("docker", "inspect", original).stdout)[0]["Image"]
        for _ in range(40):
            try:
                with urlopen(health, timeout=1) as response:
                    if response.status == 200:
                        break
            except (OSError, URLError):
                time.sleep(0.5)
        else:
            logs = run("docker", "compose", "-f", str(compose), "logs", "bridge")
            pytest.fail("disposable bridge did not become healthy: " + logs.stdout[-4000:])
        if os.getenv("PCB_TEST_START_IMAGE") and legacy_env:
            probe = run("docker", "compose", "-f", str(compose), "exec", "-T", "bridge", "python", "-c",
                        "from plex_cast_bridge.config import Settings; s=Settings(); "
                        "assert s.plex_token.get_secret_value() == 'synthetic-legacy-token', 'legacy token not loaded'; "
                        "assert str(s.plex_url).rstrip('/') == 'http://plex-sim:32400', 'Plex URL not loaded'")
            assert probe.returncode == 0, probe.stderr
        else:
            with urlopen(f"http://127.0.0.1:{port}/api/settings", timeout=5) as response:
                assert json.load(response)["library"]["linked"] is True
        migration = tmp_path / "migration.json"
        result = cli("prepare-compose-migration", "--compose-file", str(compose), "--service", "bridge", "--review-file", str(migration))
        assert result.returncode == 0, result.stderr
        result = cli("migrate-compose", "--review-file", str(migration), "--confirm")
        assert result.returncode == 0, result.stderr
        result = cli("plan", "--compose-file", str(compose), "--compose-file", str(tmp_path / "compose.override.yaml"),
                     "--service", "bridge", "--target", target, "--plan-file", str(plan))
        assert result.returncode == 0, result.stderr
        result = cli("apply", "--plan-file", str(plan), "--confirm-exclusive-data", "--health-url", health)
        if sys.platform != "linux":
            assert result.returncode == 2
            assert "music index verification NOT TESTED on non-Linux" in result.stderr
            return
        assert result.returncode == 0, result.stderr
        with urlopen(f"http://127.0.0.1:{port}/api/settings", timeout=5) as response:
            assert json.load(response)["library"]["linked"] is True
        if os.getenv("PCB_TEST_EXPECTED_PYTHON"):
            expected_py = os.environ["PCB_TEST_EXPECTED_PYTHON"]
            probe = run("docker", "compose", "-f", str(compose), "exec", "-T", "bridge", "python", "-c",
                        "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
            assert probe.returncode == 0, probe.stderr
            assert probe.stdout.strip() == expected_py, (
                f"expected Python {expected_py} in upgraded bridge, got {probe.stdout.strip()}"
            )
        catalog = json.loads((data / "catalog.json").read_text())
        assert catalog["speakers"][0]["google_home_device_id"] == speaker["google_home_device_id"]
        if not legacy_env:
            connection = json.loads((data / "plex_connection.json").read_text())
            assert connection["jwt_token"] == "synthetic-token"
            assert connection["client_identifier"] == "synthetic-client"
        else:
            assert "synthetic-legacy-token" not in result.stdout + result.stderr
        assert compose.read_bytes() == reviewed_compose and env_file.read_bytes() == reviewed_env
        if legacy_start:
            sync = json.loads((data / "google_home_sync.json").read_text())
            assert sync["cloud_gateway_url"] == "http://gateway.invalid" and sync["catalog_write_key"] == "synthetic-gateway-key"
            result = cli("verify", "--plan-file", str(plan), "--health-url", health)
            assert result.returncode == 0, result.stderr
        result = cli("rollback", "--plan-file", str(plan), "--confirm-image-rollback", "--health-url", health)
        assert result.returncode == 0, result.stderr
        restored = run("docker", "compose", "-f", str(compose), "ps", "-q", "bridge").stdout.strip()
        assert json.loads(run("docker", "inspect", restored).stdout)[0]["Image"] == original_image
        assert compose.read_bytes() == reviewed_compose and env_file.read_bytes() == reviewed_env
        assert json.loads((data / "catalog.json").read_text())["speakers"][0]["cast_uuid"] == speaker["cast_uuid"]
        if legacy_start:
            sync = json.loads((data / "google_home_sync.json").read_text())
            assert sync["cloud_gateway_url"] == "http://gateway.invalid" and sync["catalog_write_key"] == "synthetic-gateway-key"
            assert json.loads((data / "google_home_setup.json").read_text())["project_id"] == "synthetic-project"
            # The database may be owned by the container user and inaccessible
            # to the host user. Reuse the read-only, network-isolated helper.
            sys.path.insert(0, str(root / "scripts"))
            import upgrade
            snapshot = upgrade._readonly_online_music_snapshot(data, target)
            assert snapshot["mixes"][0][1] == "Saved"
            assert ("s", "l", "t") in snapshot["index_ids"]["tracks"]
    finally:
        run("docker", "compose", "-f", str(compose), "down", "-v", "--remove-orphans")
        if built_test_image:
            run("docker", "image", "rm", test_image)
        proxy.shutdown()
        proxy.server_close()
