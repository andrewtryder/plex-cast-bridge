"""Explicit, non-destructive preparation of a managed Compose override."""
import json
import logging
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from docker_integration_util import ensure_immutable_image_present
import upgrade_compose_selection as selection
import upgrade

log = logging.getLogger(__name__)


@contextmanager
def _phase_timer(phase: str):
    if os.getenv("PCB_COMPOSE_TIMING") != "1":
        yield
        return
    start = time.monotonic()
    try:
        yield
    finally:
        elapsed = time.monotonic() - start
        log.warning("pcb_compose_phase phase=%s elapsed_sec=%.3f", phase, elapsed)


def _stub_compose(monkeypatch, image="example:old", environment=""):
    config = {"name": "test", "services": {"bridge": {"image": image, "environment": {"KEEP": "yes"}}}}
    monkeypatch.setattr(selection, "_compose", lambda _dir, *args: environment if "--environment" in args else json.dumps(config))
    monkeypatch.setattr(selection, "_running_image", lambda *_args: ("container", "sha256:" + "d" * 64))


def test_migration_creates_only_the_reviewed_override(tmp_path, monkeypatch):
    monkeypatch.delenv("COMPOSE_FILE", raising=False)
    base = tmp_path / "compose.yaml"
    base.write_text("# user customization\nservices: {}\n")
    env = tmp_path / ".env"
    env.write_text("KEEP=private\n")
    _stub_compose(monkeypatch)
    before = base.read_bytes(), env.read_bytes()
    preview = selection.migration_preview(base, "bridge")
    assert not Path(preview["override"]).exists()
    path = selection.create_migration(preview)
    assert path.read_bytes() == selection.managed_content("bridge", "sha256:" + "d" * 64)
    assert (base.read_bytes(), env.read_bytes()) == before
    with pytest.raises(ValueError, match="existing Compose override"):
        selection.create_migration(preview)


def test_migration_refuses_custom_override_or_ambiguous_selection(tmp_path, monkeypatch):
    monkeypatch.delenv("COMPOSE_FILE", raising=False)
    base = tmp_path / "compose.yaml"
    base.write_text("services: {}\n")
    _stub_compose(monkeypatch)
    override = tmp_path / "compose.override.yml"
    override.write_text("# user-owned customization\n")
    with pytest.raises(ValueError, match="must not be overwritten"):
        selection.migration_preview(base, "bridge")
    assert override.read_text() == "# user-owned customization\n"
    override.unlink()
    _stub_compose(monkeypatch, environment="COMPOSE_FILE=compose.yaml\n")
    with pytest.raises(ValueError, match="COMPOSE_FILE"):
        selection.migration_preview(base, "bridge")
    assert not (tmp_path / "compose.override.yaml").exists()


def test_migration_rechecks_image_before_creating_file(tmp_path, monkeypatch):
    monkeypatch.delenv("COMPOSE_FILE", raising=False)
    base = tmp_path / "docker-compose.yml"
    base.write_text("services: {}\n")
    _stub_compose(monkeypatch)
    preview = selection.migration_preview(base, "bridge")
    _stub_compose(monkeypatch, image="example:changed")
    with pytest.raises(ValueError, match="changed after review"):
        selection.create_migration(preview)
    assert not Path(preview["override"]).exists()
    _stub_compose(monkeypatch)
    monkeypatch.setattr(selection, "_running_image", lambda *_args: ("new-container", "sha256:" + "e" * 64))
    with pytest.raises(ValueError, match="changed after review"):
        selection.create_migration(preview)
    assert not Path(preview["override"]).exists()
    with pytest.raises(ValueError, match="not suitable"):
        selection.managed_content("bridge", "example/$VARIABLE")


def test_cli_requires_a_private_review_and_confirmation(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("COMPOSE_FILE", raising=False)
    base = tmp_path / "compose.yaml"
    base.write_text("services: {}\n")
    _stub_compose(monkeypatch)
    review = tmp_path / "review.json"
    assert upgrade.main(["prepare-compose-migration", "--compose-file", str(base), "--service", "bridge", "--review-file", str(review)]) == 0
    assert review.stat().st_mode & 0o077 == 0
    assert not (tmp_path / "compose.override.yaml").exists()
    assert upgrade.main(["migrate-compose", "--review-file", str(review)]) == 2
    assert not (tmp_path / "compose.override.yaml").exists()
    assert upgrade.main(["migrate-compose", "--review-file", str(review), "--confirm"]) == 0
    assert "using both the original Compose file and this override" in capsys.readouterr().out


def test_managed_detection_requires_default_compose_to_select_exact_file(tmp_path, monkeypatch):
    monkeypatch.delenv("COMPOSE_FILE", raising=False)
    base = tmp_path / "compose.yaml"
    base.write_text("services: {}\n")
    _stub_compose(monkeypatch)
    preview = selection.migration_preview(base, "bridge")
    override = selection.create_migration(preview)
    running_image = "sha256:" + "d" * 64
    _stub_compose(monkeypatch, image=running_image)
    rendered = json.loads(selection._compose(tmp_path, "config", "--format", "json"))
    assert selection.validate_managed([base, override], "bridge", running_image, rendered) == override
    override.chmod(0o666)
    with pytest.raises(ValueError, match="permissions"):
        selection.validate_managed([base, override], "bridge", running_image, rendered)
    override.chmod(0o644)
    override.write_text("# customized\nservices: {}\n")
    assert selection.validate_managed([base, override], "bridge", "example:old", rendered) is None
    assert selection.validate_managed([base], "bridge", "example:old", rendered) is None


def test_image_switch_is_idempotent_and_refuses_customizations(tmp_path):
    override = tmp_path / "compose.override.yaml"
    old = "example:old"
    target = "example.invalid/bridge@sha256:" + "a" * 64
    rollback = "sha256:" + "b" * 64
    override.write_bytes(selection.managed_content("bridge", old))
    selection.switch_managed_image(override, "bridge", old, target)
    assert override.read_bytes() == selection.managed_content("bridge", target)
    selection.switch_managed_image(override, "bridge", old, target)
    selection.switch_managed_image(override, "bridge", target, rollback)
    assert override.read_bytes() == selection.managed_content("bridge", rollback)
    override.chmod(0o666)
    with pytest.raises(ValueError, match="writable by others"):
        selection.switch_managed_image(override, "bridge", rollback, target)
    assert override.read_bytes() == selection.managed_content("bridge", rollback)
    override.chmod(0o644)
    override.write_text("# user customization\nservices: {}\n")
    with pytest.raises(ValueError, match="changed"):
        selection.switch_managed_image(override, "bridge", rollback, target)
    assert override.read_text() == "# user customization\nservices: {}\n"
    with pytest.raises(ValueError, match="reviewed digest"):
        selection.switch_managed_image(override, "bridge", rollback, "example:latest")
    other = tmp_path / "user-file"
    other.write_bytes(selection.managed_content("bridge", rollback))
    override.unlink()
    override.symlink_to(other)
    with pytest.raises(RuntimeError, match="could not be updated"):
        selection.switch_managed_image(override, "bridge", rollback, target)
    assert other.read_bytes() == selection.managed_content("bridge", rollback)


@pytest.mark.docker_integration
@pytest.mark.skipif(os.getenv("PCB_TEST_DOCKER_IMAGE") != "1", reason="requires local Docker Compose")
def test_default_compose_loads_new_override_without_changing_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv("COMPOSE_FILE", raising=False)
    base = tmp_path / "compose.yaml"
    base.write_text("# retain this comment\nservices:\n  bridge:\n    image: example.invalid/bridge:old\n    environment:\n      KEEP: custom\n")
    env = tmp_path / ".env"
    env.write_text("UNRELATED=synthetic\n")
    before_config = json.loads(selection._compose(tmp_path, "config", "--format", "json"))
    before = base.read_bytes(), env.read_bytes()
    running_image = "sha256:" + "d" * 64
    monkeypatch.setattr(selection, "_running_image", lambda *_args: ("container", running_image))
    review = tmp_path / "migration-review.json"
    assert upgrade.main(["prepare-compose-migration", "--compose-file", str(base), "--service", "bridge", "--review-file", str(review)]) == 0
    assert upgrade.main(["migrate-compose", "--review-file", str(review), "--confirm"]) == 0
    override = tmp_path / "compose.override.yaml"
    migrated_config = json.loads(selection._compose(tmp_path, "config", "--format", "json"))
    before_config["services"]["bridge"]["image"] = running_image
    assert migrated_config == before_config
    target = "example.invalid/bridge@sha256:" + "a" * 64
    selection.switch_managed_image(override, "bridge", running_image, target)
    selected = json.loads(selection._compose(tmp_path, "config", "--format", "json"))
    assert selected["services"]["bridge"]["image"] == target
    next_target = "example.invalid/bridge@sha256:" + "b" * 64
    selection.switch_managed_image(override, "bridge", target, next_target)
    assert json.loads(selection._compose(tmp_path, "config", "--format", "json"))["services"]["bridge"]["image"] == next_target
    previous_id = "sha256:" + "c" * 64
    selection.switch_managed_image(override, "bridge", next_target, previous_id)
    assert json.loads(selection._compose(tmp_path, "config", "--format", "json"))["services"]["bridge"]["image"] == previous_id
    assert (base.read_bytes(), env.read_bytes()) == before


@pytest.mark.docker_integration
@pytest.mark.skipif(os.getenv("PCB_TEST_COMPOSE_RECREATE") != "1", reason="requires cached disposable Docker images")
def test_selection_survives_restart_recreation_and_successive_switches(tmp_path, monkeypatch):
    if os.getenv("DOCKER_HOST", "").startswith(("ssh:", "tcp:")):
        pytest.skip("bind mount test must run on the Docker daemon host")
    monkeypatch.delenv("COMPOSE_FILE", raising=False)
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "pcbselection" + os.urandom(6).hex())
    old = "ghcr.io/andrewtryder/plex-cast-bridge@sha256:a66f551562750ba50a11f2e4aeb20753e45ad44667375e2869bcbc15a8e5afbb"
    target = os.environ["PCB_TEST_UPGRADE_TARGET"]
    with _phase_timer("ensure_target_image"):
        ensure_immutable_image_present(target)
    data = tmp_path / "data"
    data.mkdir()
    (data / "sentinel").write_text("synthetic unchanged")
    base = tmp_path / "compose.yaml"
    base.write_text(json.dumps({"services": {"bridge": {"image": old, "network_mode": "none",
        "command": ["python", "-c", "import time; time.sleep(3600)"], "volumes": [f"{data}:/data"],
        "restart": "unless-stopped"}}}))
    original = base.read_bytes()
    def compose(*args):
        result = subprocess.run(["docker", "compose", *args], cwd=tmp_path, capture_output=True, text=True, check=False, timeout=120)
        assert result.returncode == 0, result.stderr
        return result
    def image_id(ref):
        return subprocess.run(["docker", "image", "inspect", ref, "--format", "{{.Id}}"],
                              capture_output=True, text=True, check=True).stdout.strip()
    def assert_image(ref):
        cid = compose("ps", "-q", "bridge").stdout.strip()
        result = subprocess.run(["docker", "inspect", cid, "--format", "{{.Image}}"], capture_output=True, text=True, check=True)
        assert result.stdout.strip() == image_id(ref)
        return cid
    try:
        # Simulate an installation previously upgraded with a private override:
        # its original Compose file still names old, but target is running.
        prior = tmp_path / "prior-selection.json"
        prior.write_text(json.dumps({"services": {"bridge": {"image": target}}}))
        with _phase_timer("compose_initial_up"):
            compose("-f", str(base), "-f", str(prior), "up", "-d", "--pull", "never")
        assert_image(target)
        with _phase_timer("migration_preview"):
            preview = selection.migration_preview(base, "bridge")
        assert preview["configured_image"] == old and preview["current_image"] == image_id(target)
        with _phase_timer("create_migration"):
            override = selection.create_migration(preview)
        with _phase_timer("compose_force_recreate"):
            compose("up", "-d", "--force-recreate")
        assert_image(target)
        with _phase_timer("switch_to_old"):
            selection.switch_managed_image(override, "bridge", image_id(target), old)
        with _phase_timer("compose_after_switch_old"):
            compose("up", "-d")
        assert_image(old)
        with _phase_timer("switch_to_target"):
            selection.switch_managed_image(override, "bridge", old, target)
        with _phase_timer("compose_after_switch_target"):
            compose("up", "-d")
        cid = assert_image(target)
        with _phase_timer("docker_restart"):
            subprocess.run(["docker", "restart", cid], capture_output=True, check=True, timeout=120)
        assert_image(target)
        with _phase_timer("compose_second_force_recreate"):
            compose("up", "-d", "--force-recreate")
        assert_image(target)
        previous_id = image_id(old)
        with _phase_timer("rollback_switch"):
            selection.switch_managed_image(override, "bridge", target, previous_id)
        with _phase_timer("compose_after_rollback"):
            compose("up", "-d")
        assert_image(old)
        with _phase_timer("forward_switch"):
            selection.switch_managed_image(override, "bridge", previous_id, target)
        with _phase_timer("compose_final_up"):
            compose("up", "-d")
        assert_image(target)
        assert base.read_bytes() == original
        assert (data / "sentinel").read_text() == "synthetic unchanged"
    finally:
        with _phase_timer("compose_down"):
            subprocess.run(["docker", "compose", "down", "--remove-orphans"], cwd=tmp_path, capture_output=True, timeout=120)
