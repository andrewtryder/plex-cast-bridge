"""Production image integration: opt in with PCB_TEST_DOCKER_IMAGE=1."""
import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.getenv("PCB_TEST_DOCKER_IMAGE") != "1", reason="requires Docker image build")
def test_production_image_diagnostic_is_read_only(tmp_path):
    root = Path(__file__).resolve().parents[1]
    tag = "plex-cast-bridge-config-drift-test"
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    (data / "google_home_sync.json").write_text('{"catalog_write_key":"saved-secret"}')
    (data / "music_index.sqlite3").write_bytes(b"untouched")
    # Docker's non-root user must be able to traverse the test mount.
    tmp_path.chmod(0o755)
    before = {p.name: p.read_bytes() for p in data.iterdir()}
    subprocess.run(["docker", "build", "-f", "deploy/Dockerfile", "-t", tag, "."], cwd=root, check=True)
    run = subprocess.run(["docker", "run", "--rm", "--network", "none", "--read-only",
                          "--mount", f"type=bind,src={data},dst=/data,readonly",
                          "-e", "CATALOG_WRITE_KEY=other-secret", tag,
                          "python", "-m", "plex_cast_bridge.config_drift"],
                         capture_output=True, text=True, check=True)
    report = json.loads(run.stdout)
    assert report["release_preflight_warnings"] == ["catalog_write_key"]
    assert "saved-secret" not in run.stdout and "other-secret" not in run.stdout
    assert {p.name: p.read_bytes() for p in data.iterdir()} == before
