#!/usr/bin/env python3
"""Create and restore-test a private archive of an already quiesced data tree.

The caller must stop all writers before calling create and leave them stopped
until it returns. This utility never stops or starts containers.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import hmac
import os
import sqlite3
import shutil
import tarfile
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_metadata(path: Path) -> None:
    if path.is_file() and path.stat().st_nlink > 1:
        raise ValueError("hard-linked files require a backup that preserves link identity")
    if hasattr(os, "listxattr"):
        try:
            attributes = os.listxattr(path, follow_symlinks=False)
        except OSError as exc:
            if exc.errno not in (errno.ENOTSUP, errno.EOPNOTSUPP):
                raise
        else:
            if attributes:
                raise ValueError("extended attributes require a backup that preserves them")


def _files(root: Path) -> dict[str, tuple[str, int]]:
    files = {}
    _check_metadata(root)
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("symbolic links in persistent data require manual backup")
        _check_metadata(path)
        if path.is_file():
            files[str(path.relative_to(root))] = (_digest(path), path.stat().st_mode & 0o777)
        elif path.is_dir():
            files[str(path.relative_to(root))] = ("directory", path.stat().st_mode & 0o777)
        else:
            raise ValueError("special files in persistent data require manual backup")
    return files


@contextmanager
def restored_tree(archive: Path, expected: dict[str, tuple[str, int]] | None = None):
    if expected is None:
        sidecar = archive.with_name(archive.name + ".sha256")
        try:
            recorded = sidecar.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise ValueError("backup integrity record is missing or invalid") from exc
        if len(recorded) != 64 or not hmac.compare_digest(recorded, _digest(archive)):
            raise ValueError("backup archive does not match its integrity record")
    with tempfile.TemporaryDirectory(prefix="pcb-restore-check-") as temp:
        root = Path(temp)
        directories = []
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar:
                name = PurePosixPath(member.name)
                if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] != "data":
                    raise ValueError("archive contains an unsafe path")
                target = root.joinpath(*name.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    directories.append((target, member.mode & 0o777))
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    content = tar.extractfile(member)
                    if content is None:
                        raise ValueError("archive member is unreadable")
                    with content, target.open("xb") as out:
                        shutil.copyfileobj(content, out)
                    os.chmod(target, member.mode & 0o777)
                else:
                    raise ValueError("archive contains a link or special file")
        for directory, mode in reversed(directories):
            os.chmod(directory, mode)
        data = root / "data"
        if not data.is_dir():
            raise ValueError("archive has no data directory")
        restored = _files(data)
        if expected is not None and expected != restored:
            raise ValueError("restored files differ from the quiesced source")
        for path in data.rglob("*"):
            if not path.is_file() or path.suffix not in {".sqlite", ".sqlite3", ".db"}:
                continue
            with closing(sqlite3.connect(path)) as db:
                if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise ValueError("restored SQLite integrity check failed")

        yield data


def verify(archive: Path, expected: dict[str, tuple[str, int]] | None = None) -> None:
    with restored_tree(archive, expected):
        pass


def create(source: Path, archive: Path) -> None:
    if not source.is_dir():
        raise ValueError("persistent data directory does not exist")
    if archive.exists():
        raise FileExistsError("backup already exists")
    if source.resolve() in archive.resolve().parents:
        raise ValueError("backup must be outside the persistent data directory")
    original = _files(source)
    archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=".pcb-backup-", dir=archive.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        with tarfile.open(temp, "w:gz") as tar:
            tar.add(source, arcname="data", recursive=True)
        if original != _files(source):
            raise RuntimeError("persistent data changed during backup")
        verify(temp, original)
        sidecar = archive.with_name(archive.name + ".sha256")
        if sidecar.exists():
            raise FileExistsError("backup integrity record already exists")
        checksum = _digest(temp)
        with temp.open("rb") as stream:
            os.fsync(stream.fileno())
        os.link(temp, archive)  # no replacement of an existing backup
        try:
            fd = os.open(sidecar, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as stream:
                stream.write(checksum + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            directory_fd = os.open(archive.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            archive.unlink(missing_ok=True)
            raise
    finally:
        temp.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    make = sub.add_parser("create")
    make.add_argument("source", type=Path)
    make.add_argument("archive", type=Path)
    make.add_argument("--writers-stopped", action="store_true", required=True)
    check = sub.add_parser("verify")
    check.add_argument("archive", type=Path)
    args = parser.parse_args()
    if args.action == "create":
        create(args.source, args.archive)
    else:
        verify(args.archive)
    print("Backup restore verification passed.")


if __name__ == "__main__":
    main()


def restore_to_new_directory(archive: Path, destination: Path) -> None:
    """Restore to a new offline directory, preserving ownership and permissions.

    This never replaces an existing path. The caller must decide how and when a
    staged tree can replace live data after stopping every writer.
    """
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("restore destination already exists")
    if destination.parent.stat().st_mode & 0o077:
        raise ValueError("restore destination parent must be private")
    with restored_tree(archive) as restored:
        expected = _files(restored)
        destination.mkdir(mode=0o700, parents=False, exist_ok=False)
        try:
            shutil.copytree(restored, destination, dirs_exist_ok=True)
            if _files(destination) != expected:
                raise ValueError("staged restore does not match the verified backup")
            with tarfile.open(archive, "r:gz") as tar:
                entries = tar.getmembers()
            # Change directory ownership last, so non-root callers never lose
            # traversal access before all child ownership has been restored.
            entries.sort(key=lambda member: (member.isdir(), -len(PurePosixPath(member.name).parts)))
            for entry in entries:
                parts = PurePosixPath(entry.name).parts
                if not parts or parts[0] != "data" or ".." in parts or not (entry.isfile() or entry.isdir()):
                    raise ValueError("archive contains an unsafe restore member")
                path = destination.joinpath(*parts[1:])
                if not path.exists():
                    if entry.isfile() and entry.name.endswith(("-wal", "-shm", "-journal")):
                        continue  # SQLite checkpointed this sidecar in the isolated copy.
                    raise ValueError("staged restore is missing an archive member")
                info = path.stat()
                if (info.st_uid, info.st_gid) != (entry.uid, entry.gid):
                    try:
                        os.chown(path, entry.uid, entry.gid)
                    except OSError as exc:
                        raise PermissionError("cannot preserve backup ownership; run recovery with suitable permissions") from exc
                os.chmod(path, entry.mode & 0o777)
        except BaseException:
            shutil.rmtree(destination)
            raise
