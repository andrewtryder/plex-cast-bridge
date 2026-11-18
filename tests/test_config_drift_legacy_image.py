"""Exercise the current read-only diagnostic inside an actual older release."""
import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.getenv("PCB_TEST_DOCKER_IMAGE") != "1", reason="requires Docker and a published image")
def test_diagnostic_runs_in_official_older_image_without_modifying_data(tmp_path):
    if os.getenv("DOCKER_HOST", "").startswith(("ssh:", "tcp:")):
        pytest.skip("bind mount test must run on the Docker daemon host")
    image = "ghcr.io/andrewtryder/plex-cast-bridge@sha256:a66f551562750ba50a11f2e4aeb20753e45ad44667375e2869bcbc15a8e5afbb"
    subprocess.run(["docker", "pull", image], check=True, capture_output=True)
    tmp_path.chmod(0o777)
    state = tmp_path / "google_home_sync.json"
    state.write_text('{"cloud_gateway_url":"https://saved.invalid","catalog_write_key":"synthetic-saved"}')
    before = state.read_bytes()
    source = (Path(__file__).resolve().parents[1] / "src/plex_cast_bridge/config_drift.py").read_text()
    command = ["docker", "run", "--rm", "-i", "--network", "none", "--read-only", "--entrypoint", "python",
               "--mount", f"type=bind,src={tmp_path},dst=/data,readonly", "-e", "DATA_DIR=/data",
               "-e", "CLOUD_GATEWAY_URL=https://new.invalid", "-e", "CATALOG_WRITE_KEY=synthetic-new",
               image, "-c", 'import sys; __package__="plex_cast_bridge"; exec(compile(sys.stdin.read(), "config_drift.py", "exec"))']
    result = subprocess.run(command, input=source, capture_output=True, text=True, timeout=60, check=True)
    report = json.loads(result.stdout)
    assert set(report["release_preflight_warnings"]) == {"cloud_gateway_url", "catalog_write_key"}
    assert "synthetic-saved" not in result.stdout and "synthetic-new" not in result.stdout
    assert state.read_bytes() == before
    app_settings = tmp_path / "app_settings.json"
    app_settings.write_text('{"default_cast_volume":0.4}')
    result = subprocess.run(command, input=source, capture_output=True, text=True, timeout=60, check=True)
    assert "unreadable or invalid" in json.loads(result.stdout)["files"]["app_settings.json"]
    assert app_settings.read_text() == '{"default_cast_volume":0.4}'
