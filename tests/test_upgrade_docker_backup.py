import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import upgrade_docker_backup as helper


def test_helper_uses_read_only_source_and_private_staging(tmp_path, monkeypatch):
    source = tmp_path / "data"
    source.mkdir()
    (source / "secret").write_text("unchanged")
    backup = tmp_path / "backup"
    backup.mkdir()
    calls = []
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        mount = next(value for value in cmd if value.startswith("type=bind,") and "dst=/backup" in value)
        if ",readonly" not in mount:
            stage = Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
            (stage / "data.tgz").write_bytes(b"archive")
            (stage / "data.tgz.sha256").write_text("digest")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(helper.subprocess, "run", fake_run)
    helper.docker_backup("create", "sha256:old", source, backup)
    helper.docker_backup("compare", "sha256:old", source, backup)
    assert (backup / "data.tgz").read_bytes() == b"archive"
    assert (source / "secret").read_text() == "unchanged"
    assert all("--network" in cmd and "none" in cmd and "--read-only" in cmd for cmd in calls)
    assert all(any("dst=/source,readonly" in item for item in cmd) for cmd in calls)
    assert any("dst=/backup,readonly" in item for item in calls[1])
    with pytest.raises(FileExistsError):
        helper.docker_backup("create", "sha256:old", source, backup)
