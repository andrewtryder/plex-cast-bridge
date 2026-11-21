"""Verify container-owned credentials can be backed up without chmod or chown."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from upgrade_docker_backup import docker_backup, read_host_file
from upgrade_backup import restored_tree


@pytest.mark.skipif(os.getenv("PCB_TEST_DOCKER_IMAGE") != "1", reason="requires local Docker")
def test_docker_helper_preserves_container_owned_private_file(tmp_path):
    if os.getenv("DOCKER_HOST", "").startswith(("ssh:", "tcp:")):
        pytest.skip("bind mount test must run on the Docker daemon host")
    root = Path(__file__).resolve().parents[1]
    tag = "plex-cast-bridge-config-drift-test"
    data = tmp_path / "data"
    data.mkdir(mode=0o777)
    data.chmod(0o777)
    backup = tmp_path / "backup"
    backup.mkdir(mode=0o700)
    if os.getenv("PCB_TEST_PREBUILT_IMAGE") != "1":
        subprocess.run(["docker", "build", "-f", "deploy/Dockerfile", "-t", tag, "."], cwd=root, check=True, capture_output=True)
    seed = ("from pathlib import Path; import os; p=Path('/data/plex_connection.json'); "
            "p.write_text('{\\\"jwt_token\\\":\\\"synthetic\\\"}'); os.chown(p,10001,10001); os.chmod(p,0o600)")
    subprocess.run(["docker", "run", "--rm", "--user", "0", "--mount", f"type=bind,src={data},dst=/data", tag,
                    "python", "-c", seed], check=True, capture_output=True)
    stat_before = (data / "plex_connection.json").stat()
    assert b"synthetic" in read_host_file(tag, data / "plex_connection.json")
    docker_backup("create", tag, data, backup)
    docker_backup("compare", tag, data, backup)
    stat_after = (data / "plex_connection.json").stat()
    assert (stat_before.st_uid, stat_before.st_gid, stat_before.st_mode) == (stat_after.st_uid, stat_after.st_gid, stat_after.st_mode)
    assert (backup / "data.tgz").stat().st_uid == os.getuid()
    with restored_tree(backup / "data.tgz") as restored:
        assert "synthetic" in (restored / "plex_connection.json").read_text()
