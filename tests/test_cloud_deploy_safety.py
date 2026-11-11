"""The provisioning script must not invent OAuth secrets during an upgrade."""

import os
import shutil
import subprocess
from pathlib import Path


def test_provisioning_requires_explicit_env_before_running_gcloud(tmp_path):
    script = Path(__file__).resolve().parents[1] / "gcp-cloud-run" / "deploy.sh"
    copied = tmp_path / "deploy.sh"
    shutil.copyfile(script, copied)
    marker = tmp_path / "gcloud-called"
    fake = tmp_path / "gcloud"
    fake.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 0\n")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "GCP_PROJECT_ID": "test-project"}
    result = subprocess.run(["bash", str(copied)], env=env, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "Restore the existing gateway configuration" in result.stderr
    assert not marker.exists()
    assert not (tmp_path / ".env").exists()


def test_first_install_deploy_refuses_existing_cloud_run_service(tmp_path):
    script = Path(__file__).resolve().parents[1] / "gcp-cloud-run" / "deploy.sh"
    copied = tmp_path / "deploy.sh"
    shutil.copyfile(script, copied)
    (tmp_path / ".env").write_text("GCP_PROJECT_ID=existing-project\n")
    marker = tmp_path / "provisioning-started"
    fake = tmp_path / "gcloud"
    fake.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *'auth list'*) echo user@example.com; exit 0;;\n"
        "  *'run services describe'*) echo https://existing.run.app; exit 0;;\n"
        f"  *) touch '{marker}'; exit 0;;\n"
        "esac\n"
    )
    fake.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "GCP_PROJECT_ID": "existing-project",
    }
    result = subprocess.run(
        ["bash", str(copied), "existing-project", "us-central1", "plex-commands",
         "plex-commands-local-worker", "plex-google-home-fulfillment", "plex-fulfillment-sa",
         "--first-install-confirmed"],
        env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "first-install provisioning only" in result.stderr
    assert not marker.exists()


def test_first_install_deploy_requires_explicit_confirmation_before_gcloud(tmp_path):
    script = Path(__file__).resolve().parents[1] / "gcp-cloud-run" / "deploy.sh"
    copied = tmp_path / "deploy.sh"
    shutil.copyfile(script, copied)
    (tmp_path / ".env").write_text("GCP_PROJECT_ID=new-project\n")
    marker = tmp_path / "gcloud-called"
    fake = tmp_path / "gcloud"
    fake.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 0\n")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "GCP_PROJECT_ID": "new-project"}
    result = subprocess.run(["bash", str(copied), "new-project"], env=env, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "--first-install-confirmed" in result.stderr
    assert not marker.exists()


def test_local_state_survives_process_restart_and_ignores_new_seed(tmp_path):
    import sys

    data = tmp_path / "data"
    data.mkdir()
    env = {**os.environ, "DATA_DIR": str(data)}
    initial = """
from plex_cast_bridge import catalog_store, catalog_sync
catalog_store.save_catalog_data({'speakers': [], 'artists': ['user library']}, sync_cloud=False)
catalog_sync.save_sync_state({'version': 7, 'catalog_write_key': 'private-key'})

"""
    subprocess.run([sys.executable, "-c", initial], env=env, capture_output=True, text=True, check=True)
    legacy_db = data / "dashboard_sessions.sqlite3"
    legacy_db.write_bytes(b"preserve legacy file")
    # A separate process simulates code replacement with a different bundled catalog.
    seed = tmp_path / "new-image"
    seed.mkdir()
    (seed / "catalog.json").write_text('{"artists": ["new seed"]}')
    restarted = """
import os
from pathlib import Path
from plex_cast_bridge import catalog_store, catalog_sync
catalog_store.PROJECT_ROOT = Path(os.environ["TEST_SEED_DIR"])
assert catalog_store.load_catalog_data()['artists'] == ['user library']
assert catalog_sync.load_sync_state()['catalog_write_key'] == 'private-key'
assert catalog_sync.load_sync_state()['version'] == 7
assert (Path(os.environ["DATA_DIR"]) / "dashboard_sessions.sqlite3").read_bytes() == b"preserve legacy file"
"""
    subprocess.run([sys.executable, "-c", restarted], env={**env, "TEST_SEED_DIR": str(seed)}, check=True)
