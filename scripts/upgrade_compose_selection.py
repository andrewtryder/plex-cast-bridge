"""Prepare an explicit, image-only Compose override without editing user files.

This module only prepares migration; it does not switch an installation's
image. The upgrade transaction must separately update and recover the managed
selection before this can be offered as a complete upgrade workflow.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any

HEADER = "# Managed by Plex Cast Bridge upgrade tooling; do not edit.\n"
DEFAULTS = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
OVERRIDES = ("compose.override.yaml", "compose.override.yml", "docker-compose.override.yaml", "docker-compose.override.yml")


def managed_content(service: str, image: str) -> bytes:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", service) or not image or "\n" in image:
        raise ValueError("Service or image is not suitable for a managed Compose override")
    return (HEADER + json.dumps({"services": {service: {"image": image}}}, sort_keys=True, indent=2) + "\n").encode()


def _compose(directory: Path, *args: str) -> str:
    try:
        result = subprocess.run(["docker", "compose", *args], cwd=directory, text=True,
                                capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Compose configuration could not be inspected") from exc
    if result.returncode:
        # Compose output can include secrets; do not print it.
        raise RuntimeError("Compose configuration could not be inspected")
    return result.stdout


def migration_preview(base: Path, service: str) -> dict[str, Any]:
    """Review a default single-file Compose setup without modifying it."""
    if base.is_symlink() or not base.is_file():
        raise ValueError("Compose file must be an existing regular file")
    base = base.resolve()
    if base.name not in DEFAULTS:
        raise ValueError("This migration requires a default Compose filename; review custom file selection manually")
    directory = base.parent
    if any((directory / name).exists() or (directory / name).is_symlink() for name in DEFAULTS if name != base.name):
        raise ValueError("Multiple default Compose files require manual review")
    if any((directory / name).exists() or (directory / name).is_symlink() for name in OVERRIDES):
        raise ValueError("An existing Compose override must not be overwritten; migrate it manually")
    if "COMPOSE_FILE" in os.environ:
        raise ValueError("COMPOSE_FILE requires manual review before migration")
    interpolation = _compose(directory, "config", "--environment")
    if any(line.startswith("COMPOSE_FILE=") for line in interpolation.splitlines()):
        raise ValueError("COMPOSE_FILE requires manual review before migration")
    try:
        automatic = json.loads(_compose(directory, "config", "--format", "json"))
        explicit = json.loads(_compose(directory, "-f", str(base), "config", "--format", "json"))
        if automatic != explicit:
            raise ValueError("Default Compose configuration differs from the selected file")
        image = automatic["services"][service]["image"]
        if not isinstance(image, str):
            raise ValueError("Selected service does not have an image")
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Selected service or image is unavailable in Compose configuration") from exc
    prefix = "compose" if base.name.startswith("compose.") else "docker-compose"
    suffix = "yaml" if base.name.endswith(".yaml") else "yml"
    override = directory / f"{prefix}.override.{suffix}"
    content = managed_content(service, image)
    return {"base": str(base), "override": str(override), "service": service,
            "current_image": image, "content": content.decode()}


def create_migration(preview: dict[str, Any]) -> Path:
    """Create the exact reviewed override once; never replace an existing path."""
    fresh = migration_preview(Path(preview["base"]), preview["service"])
    if fresh != preview:
        raise ValueError("Compose configuration changed after review; prepare a new migration")
    path = Path(preview["override"])
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o644)
    with os.fdopen(fd, "wb") as stream:
        stream.write(preview["content"].encode())
        stream.flush()
        os.fsync(stream.fileno())
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return path


def switch_managed_image(path: Path, service: str, previous: str, selected: str) -> None:
    """Atomically switch an unmodified managed override to a verified image.

    The caller must keep other configuration writers stopped and journal the
    transaction before calling this function. An existing customized file is
    never intentionally rewritten, and repeated calls are idempotent.
    """
    if not re.fullmatch(r"(?:.+@sha256:|sha256:)[0-9a-f]{64}", selected):
        raise ValueError("The selected image must be a reviewed digest or local image ID")
    expected = managed_content(service, previous)
    replacement = managed_content(service, selected)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            old_stat = os.fstat(stream.fileno())
            if not stat.S_ISREG(old_stat.st_mode):
                raise ValueError("Managed override is not a regular file")
            if old_stat.st_uid != os.geteuid():
                raise ValueError("Managed override ownership requires manual review")
            current = stream.read()
        if current == replacement:
            return
        if current != expected:
            raise ValueError("Managed override was changed; review it before switching images")
        temp_fd, temp_name = tempfile.mkstemp(prefix=".pcb-image-", dir=path.parent)
        try:
            with os.fdopen(temp_fd, "wb") as stream:
                if os.fstat(stream.fileno()).st_gid != old_stat.st_gid:
                    os.fchown(stream.fileno(), old_stat.st_uid, old_stat.st_gid)
                os.fchmod(stream.fileno(), stat.S_IMODE(old_stat.st_mode))
                stream.write(replacement)
                stream.flush()
                os.fsync(stream.fileno())
            check = os.lstat(path)
            if (check.st_dev, check.st_ino, check.st_size, check.st_mtime_ns) != (
                old_stat.st_dev, old_stat.st_ino, old_stat.st_size, old_stat.st_mtime_ns
            ) or path.is_symlink() or path.read_bytes() != expected:
                raise ValueError("Managed override changed during image selection")
            os.replace(temp_name, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
    except OSError as exc:
        raise RuntimeError("Managed image selection could not be updated; inspect the override") from exc
