import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from upgrade_backup import create, restored_tree
from upgrade_restore import restore_offline


def fixture(tmp_path):
    original = tmp_path / "original"
    original.mkdir()
    (original / "identity.json").write_text("old")
    archive = tmp_path / "backup.tgz"
    create(original, archive)
    current = tmp_path / "data"
    current.mkdir()
    (current / "identity.json").write_text("new")
    (current / "new-user-data").write_text("keep")
    return archive, current, tmp_path / "safety.tgz", tmp_path / "journal.json"


def test_explicit_offline_restore_preserves_newer_data_twice(tmp_path):
    archive, current, safety, journal = fixture(tmp_path)
    with pytest.raises(ValueError, match="explicitly confirm"):
        restore_offline(archive, current, safety, journal, writers_stopped=True, confirm_overwrite=False)
    assert (current / "new-user-data").read_text() == "keep"
    result = restore_offline(archive, current, safety, journal, writers_stopped=True, confirm_overwrite=True)
    assert (current / "identity.json").read_text() == "old"
    assert (Path(result["preserved"]) / "new-user-data").read_text() == "keep"
    with restored_tree(safety) as recovered:
        assert (recovered / "new-user-data").read_text() == "keep"
    assert result["phase"] == "backup_data_installed"
    with pytest.raises(FileExistsError, match="journal"):
        restore_offline(archive, current, tmp_path / "another.tgz", journal, writers_stopped=True, confirm_overwrite=True)


def test_failed_second_rename_restores_original_tree(tmp_path, monkeypatch):
    import os
    archive, current, safety, journal = fixture(tmp_path)
    original_rename = os.rename
    def fail_install(src, dst):
        if str(src).find(".pcb-restore-stage-") >= 0 and Path(dst) == current:
            raise OSError("simulated interruption")
        return original_rename(src, dst)
    monkeypatch.setattr(os, "rename", fail_install)
    with pytest.raises(OSError, match="simulated interruption"):
        restore_offline(archive, current, safety, journal, writers_stopped=True, confirm_overwrite=True)
    assert (current / "new-user-data").read_text() == "keep"
    assert '"original_data_restored"' in journal.read_text()


def test_journal_failure_after_displacement_restores_newer_tree(tmp_path, monkeypatch):
    import upgrade_restore
    archive, current, safety, journal = fixture(tmp_path)
    original = upgrade_restore._journal
    def fail_once(path, payload, **kwargs):
        if payload["phase"] == "current_data_preserved":
            raise OSError("simulated journal failure")
        return original(path, payload, **kwargs)
    monkeypatch.setattr(upgrade_restore, "_journal", fail_once)
    with pytest.raises(OSError, match="journal failure"):
        restore_offline(archive, current, safety, journal, writers_stopped=True, confirm_overwrite=True)
    assert (current / "new-user-data").read_text() == "keep"
    assert '"original_data_restored"' in journal.read_text()


def test_writer_reappearing_before_swap_leaves_current_tree_untouched(tmp_path):
    archive, current, safety, journal = fixture(tmp_path)
    def writer_started():
        raise RuntimeError("writer restarted")
    with pytest.raises(RuntimeError, match="writer restarted"):
        restore_offline(archive, current, safety, journal, writers_stopped=True, confirm_overwrite=True,
                        pre_swap_check=writer_started)
    assert (current / "new-user-data").read_text() == "keep"
    assert safety.exists()
    assert not journal.exists()
