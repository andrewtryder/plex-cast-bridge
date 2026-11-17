"""Offline data restoration primitives; callers must stop and exclude all writers."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from upgrade_backup import _files, create, restore_to_new_directory, verify


def _journal(path: Path, payload: dict, *, create_only: bool = False) -> None:
    encoded = (json.dumps(payload, indent=2) + "\n").encode()
    if create_only:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(encoded)
            out.flush()
            os.fsync(out.fileno())
    else:
        fd, name = tempfile.mkstemp(prefix=".restore-journal-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(encoded)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def restore_offline(archive: Path, source: Path, safety_archive: Path, journal: Path, *,
                    writers_stopped: bool, confirm_overwrite: bool, pre_swap_check=None) -> dict:
    """Replace an offline data directory only after preserving its current contents.

    Both confirmations must be supplied by the caller after independently stopping
    and excluding writers. A private journal and the displaced directory are kept.
    """
    if not writers_stopped or not confirm_overwrite:
        raise ValueError("Stop all writers and explicitly confirm replacement of current data")
    if source.is_symlink() or not source.is_dir() or os.path.ismount(source):
        raise ValueError("Data directory must be a non-symlink directory that is not a mountpoint")
    root = source.resolve()
    for path in (archive, safety_archive, journal):
        resolved = path.resolve()
        if resolved == root or root in resolved.parents:
            raise ValueError("Backups and journal must be outside the active data directory")
    parent = source.parent
    if parent.stat().st_mode & 0o022:
        raise ValueError("Data directory parent must not be writable by other users")
    if journal.exists() or journal.is_symlink():
        raise FileExistsError("A restore journal already exists; inspect it before retrying")
    verify(archive)
    # Snapshot newer data before any replacement. This backup is never removed.
    create(source, safety_archive)
    initial = source.stat()
    staging_parent = Path(tempfile.mkdtemp(prefix=".pcb-restore-stage-", dir=parent))
    holding_parent = Path(tempfile.mkdtemp(prefix=".pcb-restore-preserved-", dir=parent))
    stage = staging_parent / "data"
    displaced = holding_parent / "data"
    payload = {"schema": 1, "phase": "preparing", "data_source": str(source),
               "staged": str(stage), "preserved": str(displaced), "safety_archive": str(safety_archive)}
    try:
        restore_to_new_directory(archive, stage)
        if pre_swap_check is not None:
            pre_swap_check()
        verify(safety_archive, _files(source))
        current = source.stat()
        if (current.st_dev, current.st_ino) != (initial.st_dev, initial.st_ino):
            raise RuntimeError("Data directory changed during restore preparation")
        payload["phase"] = "prepared"
        _journal(journal, payload, create_only=True)
        try:
            os.rename(source, displaced)
            _fsync_directory(parent)
            payload["phase"] = "current_data_preserved"
            _journal(journal, payload)
            if source.exists() or source.is_symlink():
                raise RuntimeError("Data path was recreated during restore; manual recovery required")
            os.rename(stage, source)
            _fsync_directory(parent)
        except BaseException:
            if not source.exists() and not source.is_symlink() and displaced.exists():
                os.rename(displaced, source)
                _fsync_directory(parent)
                payload["phase"] = "original_data_restored"
                _journal(journal, payload)
            raise
        payload["phase"] = "backup_data_installed"
        _journal(journal, payload)
        staging_parent.rmdir()
        return payload
    except BaseException:
        # Never delete either tree. The journal and preserved directory provide
        # recovery evidence if the operation was interrupted mid-swap.
        raise
