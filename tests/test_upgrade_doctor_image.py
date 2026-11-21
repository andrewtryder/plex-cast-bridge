"""Exercise doctor with an isolated Compose project, without running the bridge."""
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(os.getenv("PCB_TEST_DOCKER_IMAGE") != "1", reason="requires local Docker")
def test_doctor_on_compose_only_installation(tmp_path):
    root = Path(__file__).resolve().parents[1]
    tag = "plex-cast-bridge-config-drift-test"
    data = tmp_path / "custom-data"
    data.mkdir(mode=0o777)
    data.chmod(0o777)
    compose = tmp_path / "compose.yml"
    compose.write_text(f'''services:
  bridge:
    image: {tag}
    network_mode: none
    command: ["python", "-c", "import time; time.sleep(300)"]
    volumes:
      - "{data}:/data"
''')
    before = list(data.iterdir())
    project = "pcbdoctor" + os.urandom(6).hex()
    env = {**os.environ, "COMPOSE_PROJECT_NAME": project}
    if os.getenv("PCB_TEST_PREBUILT_IMAGE") != "1":
        subprocess.run(["docker", "build", "-f", "deploy/Dockerfile", "-t", tag, "."], cwd=root, check=True, capture_output=True)
    try:
        subprocess.run(["docker", "compose", "-f", str(compose), "up", "-d", "--no-build"], env=env, check=True, capture_output=True)
        result = subprocess.run([sys.executable, str(root / "scripts/upgrade.py"), "doctor", "--compose-file", str(compose)],
                                env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert "Service: bridge" in result.stdout
        assert "Configuration diagnostic: PASS" in result.stdout
        assert list(data.iterdir()) == before
    finally:
        subprocess.run(["docker", "compose", "-f", str(compose), "down", "-v", "--remove-orphans"], env=env, capture_output=True)
