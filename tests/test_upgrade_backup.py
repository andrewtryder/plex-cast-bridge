import sqlite3
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.upgrade_backup import create, verify


def test_backup_restores_wal_database_and_other_files(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = sqlite3.connect(data / "music_index.sqlite3")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA wal_autocheckpoint=0")
    db.execute("CREATE TABLE mixes (id INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO mixes VALUES (7)")
    db.commit()
    (data / "plex_connection.json").write_text('{"jwt_token":"private"}')
    original = {p.name: p.read_bytes() for p in data.iterdir()}
    archive = tmp_path / "backup" / "data.tgz"
    create(data, archive)
    verify(archive)
    restore = tmp_path / "restore"
    restore.mkdir()
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(restore, filter="data")
    with sqlite3.connect(restore / "data" / "music_index.sqlite3") as restored:
        assert restored.execute("SELECT id FROM mixes").fetchall() == [(7,)]
    assert archive.stat().st_mode & 0o077 == 0
    assert {p.name: p.read_bytes() for p in data.iterdir()} == original
    db.close()


def test_backup_refuses_invalid_sqlite_and_does_not_replace_archive(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "music_index.sqlite3").write_bytes(b"invalid")
    archive = tmp_path / "archive.tgz"
    with pytest.raises(sqlite3.DatabaseError):
        create(data, archive)
    assert not archive.exists()
    archive.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        create(data, archive)
    assert archive.read_bytes() == b"existing"


def test_backup_rejects_symlinks(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "link").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symbolic"):
        create(data, tmp_path / "archive.tgz")


def test_verify_detects_changed_archive_and_missing_record(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text('{"value":1}')
    archive = tmp_path / "backup.tgz"
    create(data, archive)
    archive.write_bytes(archive.read_bytes() + b"altered")
    with pytest.raises(ValueError, match="integrity record"):
        verify(archive)
    archive.with_name(archive.name + ".sha256").unlink()
    with pytest.raises(ValueError, match="missing"):
        verify(archive)


def test_restore_verification_preserves_private_modes_and_empty_directories(tmp_path):
    from scripts.upgrade_backup import restored_tree
    data = tmp_path / "data"
    data.mkdir()
    private = data / "credential.json"
    private.write_text("secret")
    private.chmod(0o600)
    empty = data / "empty"
    empty.mkdir(mode=0o700)
    archive = tmp_path / "archive.tgz"
    create(data, archive)
    with restored_tree(archive) as restored:
        assert (restored / "credential.json").stat().st_mode & 0o777 == 0o600
        assert (restored / "empty").stat().st_mode & 0o777 == 0o700


def test_restore_to_new_directory_never_overwrites_existing_data(tmp_path):
    from scripts.upgrade_backup import restore_to_new_directory
    source = tmp_path / "source"
    source.mkdir()
    (source / "secret.json").write_text("protected")
    (source / "secret.json").chmod(0o600)
    archive = tmp_path / "backup.tgz"
    create(source, archive)
    destination = tmp_path / "staged"
    restore_to_new_directory(archive, destination)
    assert (destination / "secret.json").read_text() == "protected"
    assert (destination / "secret.json").stat().st_mode & 0o777 == 0o600
    (destination / "newer.json").write_text("do not overwrite")
    with pytest.raises(FileExistsError):
        restore_to_new_directory(archive, destination)
    assert (destination / "newer.json").read_text() == "do not overwrite"


def test_backup_refuses_to_lose_hardlink_identity(tmp_path):
    import os
    data = tmp_path / "data"
    data.mkdir()
    (data / "first").write_text("content")
    os.link(data / "first", data / "second")
    with pytest.raises(ValueError, match="hard-linked"):
        create(data, tmp_path / "backup.tgz")
    assert (data / "first").read_text() == "content"


def test_staged_restore_refuses_unpreservable_ownership(tmp_path, monkeypatch):
    import hashlib
    import io
    import os
    import tarfile
    from scripts.upgrade_backup import restore_to_new_directory

    archive = tmp_path / "backup.tgz"
    with tarfile.open(archive, "w:gz") as tar:
        folder = tarfile.TarInfo("data")
        folder.type = tarfile.DIRTYPE
        folder.mode = 0o700
        folder.uid, folder.gid = os.getuid(), os.getgid()
        tar.addfile(folder)
        member = tarfile.TarInfo("data/secret")
        member.mode = 0o600
        member.uid, member.gid = os.getuid() + 1, os.getgid()
        member.size = 6
        tar.addfile(member, io.BytesIO(b"secret"))
    archive.with_name("backup.tgz.sha256").write_text(hashlib.sha256(archive.read_bytes()).hexdigest())
    def fail_chown(*args):
        raise PermissionError("denied")
    monkeypatch.setattr(os, "chown", fail_chown)
    destination = tmp_path / "stage"
    with pytest.raises(PermissionError, match="preserve backup ownership"):
        restore_to_new_directory(archive, destination)
    assert not destination.exists()
