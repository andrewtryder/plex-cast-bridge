"""Disposable SQLite WAL verification using a read-only Docker bind mount."""
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import upgrade


@pytest.mark.skipif(os.getenv("PCB_TEST_SQLITE_ONLINE") != "1", reason="requires local Docker and a cached bridge image")
def test_readonly_online_snapshot_preserves_live_wal_files(tmp_path):
    if os.getenv("DOCKER_HOST", "").startswith(("ssh:", "tcp:")):
        pytest.skip("bind mount test must run on the Docker daemon host")
    image = os.environ["PCB_TEST_SQLITE_IMAGE"]
    tmp_path.chmod(0o777)
    path = tmp_path / "music_index.sqlite3"
    db = sqlite3.connect(path)
    try:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        db.executescript("CREATE TABLE tracks(server_id TEXT,library_id TEXT,rating_key TEXT);"
                         "CREATE TABLE artists(server_id TEXT,library_id TEXT,artist_key TEXT);"
                         "CREATE TABLE albums(server_id TEXT,library_id TEXT,album_key TEXT);"
                         "CREATE TABLE mixes(id INTEGER PRIMARY KEY,name TEXT);"
                         "CREATE TABLE mix_tracks(mix_id INTEGER,position INTEGER,rating_key TEXT);")
        db.execute("INSERT INTO tracks VALUES('server','library','track')")
        db.execute("INSERT INTO mixes VALUES(1,'existing mix')")
        db.execute("INSERT INTO mix_tracks VALUES(1,0,'track')")
        db.commit()
        files = [path, Path(str(path) + "-wal"), Path(str(path) + "-shm")]
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in files}
        # Exercise the same Docker protections used by the helper, including
        # a write attempt against the existing database and a new sidecar.
        probe = '''from pathlib import Path
for p in (Path("/source/music_index.sqlite3"), Path("/source/probe")):
    try:
        p.write_bytes(b"must-not-write")
    except OSError:
        continue
    raise SystemExit("source mount allowed a write")
'''
        result = subprocess.run(["docker", "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
                                 "--cap-drop", "ALL", "--cap-add", "DAC_OVERRIDE", "--security-opt", "no-new-privileges",
                                 "--user", "0", "--entrypoint", "python", "--mount",
                                 f"type=bind,src={tmp_path},dst=/source,readonly,bind-propagation=rprivate,bind-recursive=readonly",
                                 image, "-c", probe], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        snapshot = upgrade._readonly_online_music_snapshot(tmp_path, image)
        assert snapshot["mixes"] == [(1, "existing mix")]
        assert snapshot["mix_tracks"] == [(1, 0, "track")]
        assert snapshot["index_ids"]["tracks"] == {("server", "library", "track")}
        assert before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in files}
    finally:
        db.close()


@pytest.mark.skipif(os.getenv("PCB_TEST_SQLITE_ONLINE") != "1", reason="requires local Docker and a cached bridge image")
def test_snapshot_with_writer_in_docker(tmp_path):
    if os.getenv("DOCKER_HOST", "").startswith(("ssh:", "tcp:")):
        pytest.skip("bind mount test must run on the Docker daemon host")
    image = os.environ["PCB_TEST_SQLITE_IMAGE"]
    tmp_path.chmod(0o777)
    code = '''import sqlite3, time
db=sqlite3.connect("/data/music_index.sqlite3")
db.execute("PRAGMA journal_mode=WAL")
db.executescript("CREATE TABLE tracks(server_id,library_id,rating_key);"
 "CREATE TABLE artists(server_id,library_id,artist_key);"
 "CREATE TABLE albums(server_id,library_id,album_key);"
 "CREATE TABLE mixes(id INTEGER PRIMARY KEY,name);"
 "CREATE TABLE mix_tracks(mix_id,position,rating_key);")
db.execute("INSERT INTO tracks VALUES('s','l','t')")
db.execute("INSERT INTO mixes VALUES(1,'saved')")
db.execute("INSERT INTO mix_tracks VALUES(1,0,'t')")
db.commit()
print("ready", flush=True)
time.sleep(120)
'''
    command = ["docker", "run", "--detach", "--rm", "--network", "none", "--user", "0",
               "--entrypoint", "python", "--mount", f"type=bind,src={tmp_path},dst=/data", image, "-c", code]
    container = subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()
    try:
        for _ in range(100):
            result = subprocess.run(["docker", "logs", container], capture_output=True, text=True)
            if "ready" in result.stdout:
                break
            time.sleep(0.1)
        else:
            pytest.fail("disposable SQLite writer did not start")
        files = [tmp_path / ("music_index.sqlite3" + suffix) for suffix in ("", "-wal", "-shm")]
        def file_state():
            return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in files}
        before = file_state()
        for _ in range(20):
            time.sleep(0.1)
            current = file_state()
            if current == before:
                break
            before = current
        else:
            pytest.fail("SQLite files did not settle before the read-only check")
        snapshot = upgrade._readonly_online_music_snapshot(tmp_path, image)
        assert snapshot["mixes"] == [(1, "saved")]
        assert snapshot["index_ids"]["tracks"] == {("s", "l", "t")}
        assert before == file_state()
        write_code = '''import sqlite3, time
db=sqlite3.connect("/data/music_index.sqlite3")
for n in range(100):
    with db:
        db.execute("UPDATE mixes SET name=? WHERE id=1", (str(n),))
        db.execute("UPDATE tracks SET rating_key=?", (str(n),))
    time.sleep(0.01)
'''
        writer = subprocess.Popen(["docker", "exec", container, "python", "-c", write_code],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(3):
                snapshot = upgrade._readonly_online_music_snapshot(tmp_path, image)
                mix_name = snapshot["mixes"][0][1]
                track_key = next(iter(snapshot["index_ids"]["tracks"]))[2]
                assert (mix_name, track_key) == ("saved", "t") or mix_name == track_key
        finally:
            assert writer.wait(timeout=10) == 0
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)
