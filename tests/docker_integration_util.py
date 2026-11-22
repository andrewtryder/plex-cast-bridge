"""Shared helpers for Docker integration tests (read-only image cache checks)."""
from __future__ import annotations

import re
import subprocess


def _immutable_ref(ref: str) -> bool:
    return bool(re.search(r"@sha256:[0-9a-f]{64}$", ref) or re.fullmatch(r"sha256:[0-9a-f]{64}", ref))


def ensure_immutable_image_present(ref: str, *, timeout: int = 120) -> str:
    """Return the local image ID for an immutable digest reference.

  Pull only when the daemon does not already have the exact ref. Mutable tags
    are always pulled so CI never substitutes an unexpected digest.
    """
    inspect = subprocess.run(
        ["docker", "image", "inspect", ref, "--format", "{{.Id}}"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if inspect.returncode == 0 and inspect.stdout.strip():
        return inspect.stdout.strip()
    if not _immutable_ref(ref):
        raise ValueError(f"Integration tests require an immutable digest ref, not a mutable tag: {ref!r}")
    subprocess.run(["docker", "pull", ref], capture_output=True, check=True, timeout=timeout)
    inspect = subprocess.run(
        ["docker", "image", "inspect", ref, "--format", "{{.Id}}"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    return inspect.stdout.strip()
