#!/usr/bin/env python3
"""Create and restore-test a private archive of an already quiesced data tree.

The caller must stop all writers before calling create and leave them stopped
until it returns. This utility never stops or starts containers.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _files(root: Path) -> dict[str, str]:
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("symbolic links in persistent data require manual backup")
        if path.is_file():
            files[str(path.relative_to(root))] = _digest(path)
        elif not path.is_dir():
            raise ValueError("special files in persistent data require manual backup")
    return files


def verify(archive: Path, expected: dict[str, str] | None = None) -> None:
    with tempfile.TemporaryDirectory(prefix="pcb-restore-check-") as temp:
        root = Path(temp)
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar:
                name = PurePosixPath(member.name)
                if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] != "data":
                    raise ValueError("archive contains an unsafe path")
                target = root.joinpath(*name.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    content = tar.extractfile(member)
                    if content is None:
                        raise ValueError("archive member is unreadable")
                    with content, target.open("xb") as out:
                        shutil.copyfileobj(content, out)
                else:
                    raise ValueError("archive contains a link or special file")
        data = root / "data"
        if not data.is_dir():
            raise ValueError("archive has no data directory")
        restored = _files(data)
        if expected is not None and expected != restored:
            raise ValueError("restored files differ from the quiesced source")
        for path in data.rglob("*"):
            if not path.is_file() or path.suffix not in {".sqlite", ".sqlite3", ".db"}:
                continue
            with sqlite3.connect(path) as db:
                if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise ValueError("restored SQLite integrity check failed")


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
        os.link(temp, archive)  # no replacement of an existing backup
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
