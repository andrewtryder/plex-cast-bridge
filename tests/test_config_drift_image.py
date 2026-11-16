"""Production image integration: opt in with PCB_TEST_DOCKER_IMAGE=1."""
import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest


@pytest.mark.skipif(os.getenv("PCB_TEST_DOCKER_IMAGE") != "1", reason="requires Docker image build")
def test_production_image_diagnostic_is_read_only():
    root = Path(__file__).resolve().parents[1]
    tag = "plex-cast-bridge-config-drift-test"
    volume = "pcb-drift-test-" + uuid.uuid4().hex
    subprocess.run(["docker", "build", "-f", "deploy/Dockerfile", "-t", tag, "."], cwd=root, check=True)
    subprocess.run(["docker", "volume", "create", volume], check=True, capture_output=True)
    mount = f"type=volume,src={volume},dst=/data"
    try:
        subprocess.run(["docker", "run", "--rm", "--user", "0", "--mount", mount, tag,
                        "python", "-c", "from pathlib import Path; p=Path('/data'); "
                        "(p/'google_home_sync.json').write_text('{\\\"catalog_write_key\\\":\\\"saved-secret\\\"}'.replace('\\\\\\\"','\\\"')); "
                        "(p/'music_index.sqlite3').write_bytes(b'untouched')"], check=True)
        snapshot_code = ("from pathlib import Path; import hashlib,json; p=Path('/data'); "
                         "print(json.dumps({x.name:hashlib.sha256(x.read_bytes()).hexdigest() "
                         "for x in p.iterdir()}))")
        def snapshot():
            result = subprocess.run(["docker", "run", "--rm", "--network", "none", "--read-only",
                                     "--mount", mount + ",readonly", tag, "python", "-c", snapshot_code],
                                    capture_output=True, text=True, check=True)
            return json.loads(result.stdout)

        before = snapshot()
        run = subprocess.run(["docker", "run", "--rm", "--network", "none", "--read-only",
                              "--mount", mount + ",readonly", "-e", "CATALOG_WRITE_KEY=other-secret", tag,
                              "python", "-m", "plex_cast_bridge.config_drift"],
                             capture_output=True, text=True, check=True)
        assert json.loads(run.stdout)["release_preflight_warnings"] == ["catalog_write_key"]
        assert "saved-secret" not in run.stdout and "other-secret" not in run.stdout
        assert snapshot() == before
    finally:
        subprocess.run(["docker", "volume", "rm", volume], check=True, capture_output=True)
