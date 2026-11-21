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
@pytest.mark.parametrize("external_catalog", [False, True])
def test_reviewed_upgrade_then_image_rollback_keeps_existing_data(tmp_path, external_catalog):
    if os.getenv("DOCKER_HOST", "").startswith(("ssh:", "tcp:")):
        pytest.skip("bind mount test must run on the Docker daemon host")
    target = os.environ["PCB_TEST_UPGRADE_TARGET"]
    first_target = os.getenv("PCB_TEST_FIRST_TARGET", target)
    root = Path(__file__).resolve().parents[1]
    data = tmp_path / "custom-data"
    data.mkdir(mode=0o777)
    data.chmod(0o777)
    (data / "plex_connection.json").write_text(json.dumps({"mode": "jwt", "jwt_token": "synthetic", "client_identifier": "id-1"}))
    catalog = tmp_path / "external-catalog.json" if external_catalog else data / "catalog.json"
    speakers = [{"id": "virtual-1", "cast_uuid": "cast-1", "kind": "speaker"},
                {"id": "virtual-2", "cast_uuid": "cast-2", "kind": "speaker"},
                {"id": "virtual-group", "cast_uuid": "group-1", "kind": "group"}]
    catalog.write_text(json.dumps({"speakers": speakers}))
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
    if external_catalog:
        app_dir = tmp_path / "proxmox-app"
        (app_dir / "deploy").mkdir(parents=True)
        env_file = app_dir / ".env"
        env_file.write_text("PLEX_TOKEN=synthetic-proxmox-token\n")
        credentials = app_dir / "gcp-credentials.json"
        credentials.write_text('{"synthetic":"credential"}')
        compose = app_dir / "deploy" / "docker-compose.yml"
    else:
        compose = tmp_path / "compose.yaml"
    service = {"image": "plex-cast-bridge-config-drift-test", "command": ["python", "-c", server],
               "ports": [f"127.0.0.1:{port}:8000"], "volumes": [f"{data}:/data"]}
    if external_catalog:
        service["environment"] = {"CATALOG_PATH": "/external-catalog.json", "GOOGLE_APPLICATION_CREDENTIALS": "/app/gcp-credentials.json"}
        service["env_file"] = ["../.env"]
        service["volumes"].append(f"{catalog}:/external-catalog.json")
        service["volumes"].append(f"{credentials}:/app/gcp-credentials.json:ro")
    compose.write_text(json.dumps({"services": {"bridge": service}}))
    original_compose = compose.read_bytes()
    env = {**os.environ, "COMPOSE_PROJECT_NAME": "pcbe2e" + os.urandom(6).hex()}
    if os.getenv("PCB_TEST_PREBUILT_IMAGE") != "1":
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
        migration = tmp_path / "migration.json"
        result = cli("prepare-compose-migration", "--compose-file", str(compose), "--service", "bridge", "--review-file", str(migration))
        assert result.returncode == 0, result.stderr
        result = cli("migrate-compose", "--review-file", str(migration), "--confirm")
        assert result.returncode == 0, result.stderr
        managed = compose.parent / ("docker-compose.override.yml" if external_catalog else "compose.override.yaml")
        original_override = managed.read_bytes()
        result = cli("plan", "--compose-file", str(compose), "--compose-file", str(managed), "--target", first_target, "--plan-file", str(plan))
        assert result.returncode == 0, result.stderr
        result = cli("apply", "--plan-file", str(plan), "--confirm-exclusive-data", "--health-url", health)
        if sys.platform != "linux":
            assert result.returncode == 2
            assert "music index verification NOT TESTED on non-Linux" in result.stderr
            assert compose.read_bytes() == original_compose
            return
        assert result.returncode == 0, result.stderr
        assert {item["id"] for item in json.loads(catalog.read_text())["speakers"]} == {item["id"] for item in speakers}
        assert json.loads((data / "plex_connection.json").read_text())["client_identifier"] == "id-1"
        if not external_catalog:
            # Reproduce the on-disk state after a crash between journaling and
            # the atomic selection switch while the candidate remains running.
            managed.write_bytes(original_override)
            interrupted = cli("verify", "--plan-file", str(plan), "--health-url", health)
            assert interrupted.returncode != 0
            result = cli("finalize-selection", "--plan-file", str(plan), "--confirm", "--health-url", health)
            assert result.returncode == 0, result.stderr
        result = cli("verify", "--plan-file", str(plan), "--health-url", health)
        assert result.returncode == 0, result.stderr
        before_restart = run("docker", "compose", "-f", str(compose), "ps", "-q", "bridge").stdout.strip()
        restarted = run("docker", "restart", before_restart)
        assert restarted.returncode == 0, restarted.stderr
        assert json.loads(run("docker", "inspect", before_restart).stdout)[0]["Image"] == json.loads(run("docker", "image", "inspect", first_target).stdout)[0]["Id"]
        recreated = subprocess.run(["docker", "compose", "up", "-d", "--force-recreate"],
                                   cwd=compose.parent, env=env, capture_output=True, text=True, timeout=240)
        assert recreated.returncode == 0, recreated.stderr
        current = run("docker", "compose", "-f", str(compose), "ps", "-q", "bridge").stdout.strip()
        actual_id = json.loads(run("docker", "inspect", current).stdout)[0]["Image"]
        target_id = json.loads(run("docker", "image", "inspect", first_target).stdout)[0]["Id"]
        assert actual_id == target_id
        if first_target != target:
            plan = tmp_path / "second-review.json"
            result = cli("plan", "--compose-file", str(compose), "--compose-file", str(managed),
                         "--target", target, "--plan-file", str(plan))
            assert result.returncode == 0, result.stderr
            result = cli("apply", "--plan-file", str(plan), "--confirm-exclusive-data", "--health-url", health)
            assert result.returncode == 0, result.stderr
            recreated = subprocess.run(["docker", "compose", "up", "-d", "--force-recreate"],
                                       cwd=compose.parent, env=env, capture_output=True, text=True, timeout=240)
            assert recreated.returncode == 0, recreated.stderr
            current = run("docker", "compose", "-f", str(compose), "ps", "-q", "bridge").stdout.strip()
            assert json.loads(run("docker", "inspect", current).stdout)[0]["Image"] == json.loads(run("docker", "image", "inspect", target).stdout)[0]["Id"]
        (data / "new-user-data.json").write_text('{"keep":true}')
        result = cli("rollback", "--plan-file", str(plan), "--confirm-image-rollback", "--health-url", health)
        assert result.returncode == 0, result.stderr
        assert (data / "new-user-data.json").read_text() == '{"keep":true}'
        assert compose.read_bytes() == original_compose
        if external_catalog:
            assert env_file.read_text() == "PLEX_TOKEN=synthetic-proxmox-token\n"
            assert credentials.read_text() == '{"synthetic":"credential"}'
        with sqlite3.connect(db_path) as db:
            assert db.execute("SELECT id FROM mixes").fetchall() == [(1,)]
        if os.getenv("PCB_TEST_IMAGE_ONLY") == "1":
            return
        result = cli("restore-data", "--plan-file", str(plan), "--confirm-overwrite-data",
                     "--confirm-exclusive-data", "--health-url", health)
        if sys.platform != "linux":
            assert result.returncode == 2
            assert "cannot preserve backup ownership" in result.stderr
            return
        assert result.returncode == 0, result.stderr
        assert not (data / "new-user-data.json").exists()
        journal = json.loads((plan.with_suffix(".backup") / "data-restore-journal.json").read_text())
        assert (Path(journal["preserved"]) / "new-user-data.json").read_text() == '{"keep":true}'
        assert Path(journal["safety_archive"]).exists()
        with sqlite3.connect(db_path) as db:
            assert db.execute("SELECT id FROM mixes").fetchall() == [(1,)]
    finally:
        run("docker", "compose", "-f", str(compose), "down", "-v", "--remove-orphans")


@pytest.mark.skipif(os.getenv("PCB_TEST_UPGRADE_E2E") != "1", reason="requires local Docker and a published immutable target")
@pytest.mark.parametrize("failure_mode", ["startup", "health", "creation", "pull", "interruption"])
def test_failed_candidate_recovers_old_image_without_overwriting_newer_data(tmp_path, failure_mode):
    import signal
    import shlex
    import shutil
    import time
    if os.getenv("DOCKER_HOST", "").startswith(("ssh:", "tcp:")):
        pytest.skip("bind mount test must run on the Docker daemon host")
    root = Path(__file__).resolve().parents[1]
    target = os.environ["PCB_TEST_UPGRADE_TARGET"]
    data = tmp_path / "data"
    data.mkdir(mode=0o777)
    data.chmod(0o777)
    (data / "catalog.json").write_text('{"speakers":[]}')
    (data / "plex_connection.json").write_text('{"mode":"jwt","jwt_token":"synthetic-test-only"}')
    (data / "google_home_sync.json").write_text('{"catalog_write_key":"synthetic-test-only"}')
    protected = {name: (data / name).read_bytes() for name in
                 ("catalog.json", "plex_connection.json", "google_home_sync.json")}
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # The locally built candidate contains the new diagnostic; the pinned older
    # published image does not. Its startup writes a synthetic file, then fails.
    server = ("import os,sys; from pathlib import Path\n"
              "if not os.path.exists('/app/src/plex_cast_bridge/config_drift.py'):\n"
              " Path('/data/newer.json').write_text('{\\\"keep\\\":true}')\n"
              " sys.exit(1)\n"
              "from http.server import HTTPServer,BaseHTTPRequestHandler\n"
              "H=type('H',(BaseHTTPRequestHandler,),{'do_GET':lambda s:(s.send_response(200),s.end_headers()),"
              "'log_message':lambda *a:None}); HTTPServer(('0.0.0.0',8000),H).serve_forever()")
    if failure_mode == "interruption":
        server = ("from http.server import HTTPServer,BaseHTTPRequestHandler; "
                  "H=type('H',(BaseHTTPRequestHandler,),{'do_GET':lambda s:(s.send_response(200),s.end_headers()),"
                  "'log_message':lambda *a:None}); HTTPServer(('0.0.0.0',8000),H).serve_forever()")
    elif failure_mode == "health":
        server = ("import os; from pathlib import Path\n"
                  "bad=not os.path.exists('/app/src/plex_cast_bridge/config_drift.py')\n"
                  "if bad: Path('/data/newer.json').write_text('{\"keep\":true}')\n"
                  "from http.server import HTTPServer,BaseHTTPRequestHandler\n"
                  "H=type('H',(BaseHTTPRequestHandler,),{'do_GET':lambda s:(s.send_response(503 if bad else 200),s.end_headers()),"
                  "'log_message':lambda *a:None}); HTTPServer(('0.0.0.0',8000),H).serve_forever()")
    compose = tmp_path / "compose.yaml"
    test_image = os.getenv("PCB_TEST_LOCAL_IMAGE", "pcb-failure-test:" + os.urandom(6).hex())
    env_file = tmp_path / ".env"
    env_file.write_text("PLEX_TOKEN=synthetic-env-token\n")
    original_env = env_file.read_bytes()
    compose.write_text(json.dumps({"services": {"bridge": {"image": test_image,
        "env_file": [str(env_file)], "command": ["python", "-c", server],
        "ports": [f"127.0.0.1:{port}:8000"], "volumes": [f"{data}:/data"]}}}))
    env = {**os.environ, "COMPOSE_PROJECT_NAME": "pcbfail" + os.urandom(6).hex()}
    if os.getenv("PCB_TEST_PREBUILT_IMAGE") != "1":
        subprocess.run(["docker", "build", "-f", "deploy/Dockerfile", "-t", test_image, "."],
                       cwd=root, env=env, check=True, capture_output=True)
    def run(*args):
        return subprocess.run(list(args), cwd=root, env=env, capture_output=True, text=True, timeout=240)
    def cli(*args):
        return run(sys.executable, str(root / "scripts/upgrade.py"), *args)
    plan = tmp_path / "review.json"
    try:
        result = run("docker", "compose", "-f", str(compose), "up", "-d", "--no-build")
        assert result.returncode == 0, result.stderr
        old = run("docker", "compose", "-f", str(compose), "ps", "-q", "bridge").stdout.strip()
        old_image = json.loads(run("docker", "inspect", old).stdout)[0]["Image"]
        migration = tmp_path / "migration.json"
        result = cli("prepare-compose-migration", "--compose-file", str(compose), "--service", "bridge", "--review-file", str(migration))
        assert result.returncode == 0, result.stderr
        result = cli("migrate-compose", "--review-file", str(migration), "--confirm")
        assert result.returncode == 0, result.stderr
        result = cli("plan", "--compose-file", str(compose), "--compose-file", str(tmp_path / "compose.override.yaml"),
                     "--target", target, "--plan-file", str(plan))
        assert result.returncode == 0, result.stderr
        if failure_mode in ("creation", "pull", "interruption"):
            # Simulate a daemon creation error after the old container has been
            # removed. All other operations still use the real Docker daemon.
            docker_path = shutil.which("docker")
            assert docker_path
            wrapper_dir = tmp_path / "docker-wrapper"
            wrapper_dir.mkdir()
            wrapper = wrapper_dir / "docker"
            injected = (f'''*upgrade-image-override.json*" up "*)
    {shlex.quote(docker_path)} rm -f "$PCB_TEST_OLD_CONTAINER" >/dev/null
    echo 'injected candidate creation failure' >&2
    exit 1 ;;''' if failure_mode == "creation" else '''" pull "*)
    echo 'injected image pull failure' >&2
    exit 1 ;;''' if failure_mode == "pull" else f'''*upgrade-image-override.json*" up "*)
    {shlex.quote(docker_path)} "$@" || exit $?
    touch "$PCB_TEST_MARKER"
    sleep 120
    exit 0 ;;''')
            wrapper.write_text(f'''#!/bin/sh
case " $* " in
  {injected}
esac
exec {shlex.quote(docker_path)} "$@"
''')
            wrapper.chmod(0o755)
            env["PATH"] = str(wrapper_dir) + os.pathsep + env["PATH"]
            env["PCB_TEST_OLD_CONTAINER"] = old
            env["PCB_TEST_MARKER"] = str(tmp_path / "replacement-started")
        if failure_mode == "interruption":
            args = [sys.executable, str(root / "scripts/upgrade.py"), "apply", "--plan-file", str(plan),
                    "--confirm-exclusive-data", "--health-url", f"http://127.0.0.1:{port}/health"]
            process = subprocess.Popen(args, cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, start_new_session=True)
            try:
                deadline = time.monotonic() + 180
                marker = Path(env["PCB_TEST_MARKER"])
                while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.2)
                assert marker.exists(), "replacement did not reach the interruption point"
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=30)
            result = cli("rollback", "--plan-file", str(plan), "--confirm-image-rollback",
                         "--health-url", f"http://127.0.0.1:{port}/health")
            assert result.returncode == 0, result.stderr
        else:
            result = cli("apply", "--plan-file", str(plan), "--confirm-exclusive-data",
                         "--health-url", f"http://127.0.0.1:{port}/health")
            assert result.returncode != 0
        if failure_mode == "pull":
            current = run("docker", "compose", "-f", str(compose), "ps", "-q", "bridge").stdout.strip()
            assert current == old
            assert all((data / name).read_bytes() == contents for name, contents in protected.items())
            assert env_file.read_bytes() == original_env
            assert not (plan.with_suffix(".backup") / "data.tgz").exists()
            return
        if failure_mode in ("startup", "health"):
            assert (data / "newer.json").exists()
        current = run("docker", "compose", "-f", str(compose), "ps", "-q", "bridge").stdout.strip()
        assert current, result.stdout + result.stderr
        assert json.loads(run("docker", "inspect", current).stdout)[0]["Image"] == old_image, result.stdout + result.stderr
        recreated = subprocess.run(["docker", "compose", "up", "-d", "--force-recreate"],
                                   cwd=tmp_path, env=env, capture_output=True, text=True, timeout=240)
        assert recreated.returncode == 0, recreated.stderr
        current = run("docker", "compose", "-f", str(compose), "ps", "-q", "bridge").stdout.strip()
        assert json.loads(run("docker", "inspect", current).stdout)[0]["Image"] == old_image
        assert not (plan.with_suffix(".backup") / "apply-completed.json").exists()
        result = cli("rollback", "--plan-file", str(plan), "--confirm-image-rollback",
                     "--health-url", f"http://127.0.0.1:{port}/health")
        assert result.returncode == 0, result.stderr
        if failure_mode in ("startup", "health"):
            assert (data / "newer.json").read_text() == '{"keep":true}'
        assert all((data / name).read_bytes() == contents for name, contents in protected.items())
        assert env_file.read_bytes() == original_env
    finally:
        run("docker", "compose", "-f", str(compose), "down", "-v", "--remove-orphans")
        run("docker", "image", "rm", test_image)
