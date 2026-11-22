"""Shared helpers for Docker integration tests (read-only image cache checks)."""
from __future__ import annotations

import re
import subprocess

_IMMUTABLE_REF = re.compile(
    r"(?:@sha256:[0-9a-f]{64}$)|(?:^sha256:[0-9a-f]{64}$)"
)


def _require_immutable_ref(ref: str) -> None:
    if not _IMMUTABLE_REF.search(ref):
        raise ValueError(f"Integration tests require an immutable digest ref, not a mutable tag: {ref!r}")


def ensure_immutable_image_present(ref: str, *, timeout: int = 120) -> str:
    """Return the local image ID for an immutable digest reference.

    Ref format is validated before any Docker CLI call. Mutable tags are
    rejected even when already present locally.
    """
    _require_immutable_ref(ref)

    inspect = subprocess.run(
        ["docker", "image", "inspect", ref, "--format", "{{.Id}}"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if inspect.returncode == 0 and inspect.stdout.strip():
        return inspect.stdout.strip()

    try:
        subprocess.run(
            ["docker", "pull", ref],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Docker pull failed for immutable ref {ref!r}") from exc

    inspect = subprocess.run(
        ["docker", "image", "inspect", ref, "--format", "{{.Id}}"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    image_id = inspect.stdout.strip()
    if inspect.returncode != 0 or not image_id:
        raise RuntimeError(f"Docker image missing after pull for {ref!r}")
    return image_id
