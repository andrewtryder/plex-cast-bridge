import json
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from upgrade_inputs import create_inputs_backup
from upgrade_backup import verify


def test_inputs_backup_preserves_reviewed_contents_and_absence(tmp_path):
    config = tmp_path / "compose.yml"
    secret = tmp_path / "credentials.json"
    missing = tmp_path / "optional.env"
    config.write_text("services: {}")
    secret.write_text('{"private_key":"secret"}')
    inputs = {str(config): config.read_bytes().hex(), str(secret): secret.read_bytes().hex(), str(missing): None}
    archive = tmp_path / "backup" / "configuration.tgz"
    create_inputs_backup(inputs, archive)
    verify(archive)
    with tarfile.open(archive, "r:gz") as tar:
        manifest = json.load(tar.extractfile("data/manifest.json"))
        entry = next(item for item in manifest if item["source"] == str(secret))
        assert tar.extractfile("data/" + entry["file"]).read() == secret.read_bytes()
        assert next(item for item in manifest if item["source"] == str(missing))["file"] is None
    assert archive.stat().st_mode & 0o077 == 0
    assert secret.read_text() == '{"private_key":"secret"}'


def test_inputs_backup_rejects_changed_reviewed_file(tmp_path):
    path = tmp_path / "environment"
    path.write_text("before")
    expected = path.read_bytes().hex()
    path.write_text("after")
    archive = tmp_path / "backup.tgz"
    with pytest.raises(ValueError, match="changed"):
        create_inputs_backup({str(path): expected}, archive)
    assert not archive.exists()
    assert path.read_text() == "after"
