"""Disposable Docker volume backup test; opt in with PCB_TEST_DOCKER_IMAGE=1."""
import os
import subprocess
import uuid
from pathlib import Path

import pytest


@pytest.mark.skipif(os.getenv("PCB_TEST_DOCKER_IMAGE") != "1", reason="requires Docker image")
def test_quiesced_volume_backup_with_production_image():
    root = Path(__file__).resolve().parents[1]
    tag = "plex-cast-bridge-config-drift-test"
    ident = uuid.uuid4().hex
    data, backups, container = (f"pcb-test-data-{ident}", f"pcb-test-backup-{ident}", f"pcb-test-app-{ident}")
    if os.getenv("PCB_TEST_PREBUILT_IMAGE") != "1":
        subprocess.run(["docker", "build", "-f", "deploy/Dockerfile", "-t", tag, "."], cwd=root, check=True, capture_output=True)
    for volume in (data, backups):
        subprocess.run(["docker", "volume", "create", volume], check=True, capture_output=True)
    def mount(volume, dest):
        return f"type=volume,src={volume},dst={dest}"
    source = (root / "scripts/upgrade_backup.py").read_text()
    try:
        seed = ("import sqlite3; from pathlib import Path; "
                "p=Path('/data'); (p/'plex_connection.json').write_text('private'); "
                "db=sqlite3.connect(p/'music_index.sqlite3'); "
                "db.execute('create table mixes(id integer)'); db.execute('insert into mixes values(7)'); db.commit(); db.close()")
        subprocess.run(["docker", "run", "--rm", "--user", "0", "--mount", mount(data, "/data"), tag, "python", "-c", seed], check=True)
        subprocess.run(["docker", "run", "-d", "--name", container, "--mount", mount(data, "/data"),
                        tag, "python", "-c", "import time; time.sleep(300)"], check=True, capture_output=True)
        subprocess.run(["docker", "stop", "-t", "1", container], check=True, capture_output=True)
        base = ["docker", "run", "--rm", "-i", "--user", "0", "--network", "none", "--read-only",
                "--tmpfs", "/tmp", "--mount", mount(data, "/data") + ",readonly",
                "--mount", mount(backups, "/backup"), tag, "python", "-c",
                "import sys; exec(compile(sys.stdin.read(), 'upgrade_backup.py', 'exec'))"]
        run = subprocess.run(base + ["create", "/data", "/backup/data.tgz", "--writers-stopped"],
                             input=source, capture_output=True, text=True, check=True)
        assert "verification passed" in run.stdout
        verify = subprocess.run(base + ["verify", "/backup/data.tgz"], input=source, capture_output=True, text=True, check=True)
        assert "verification passed" in verify.stdout
        check = subprocess.run(["docker", "run", "--rm", "--read-only", "--mount", mount(data, "/data") + ",readonly",
                                tag, "python", "-c", "import sqlite3; from pathlib import Path; "
                                "assert (Path('/data')/'plex_connection.json').read_text() == 'private'; "
                                "db=sqlite3.connect('file:/data/music_index.sqlite3?immutable=1',uri=True); "
                                "assert db.execute('select id from mixes').fetchall() == [(7,)]"],
                               capture_output=True, text=True)
        assert check.returncode == 0, check.stderr
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        for volume in (data, backups):
            subprocess.run(["docker", "volume", "rm", volume], capture_output=True)
