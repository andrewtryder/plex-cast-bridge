#!/usr/bin/env python3
"""Verify that a release tag resolves to an immutable multi-architecture OCI image."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from typing import Any


REQUIRED_PLATFORMS = {("linux", "amd64"), ("linux", "arm64")}


def inspect_raw(reference: str) -> bytes:
    try:
        result = subprocess.run(
            ["docker", "buildx", "imagetools", "inspect", "--raw", reference],
            check=False,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Unable to inspect the published OCI image") from exc
    if result.returncode:
        raise RuntimeError("Published image tag could not be resolved")
    return result.stdout


def validate_manifest(raw: bytes, expected_digest: str | None = None) -> dict[str, Any]:
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Published image did not return an OCI manifest") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("manifests"), list):
        raise ValueError("Published image is not a multi-platform OCI index")
    platforms = {
        (str(item.get("platform", {}).get("os")), str(item.get("platform", {}).get("architecture")))
        for item in manifest["manifests"]
        if isinstance(item, dict) and isinstance(item.get("platform"), dict)
    }
    missing = REQUIRED_PLATFORMS - platforms
    if missing:
        rendered = ", ".join(f"{os}/{arch}" for os, arch in sorted(missing))
        raise ValueError(f"Published image is missing required platform(s): {rendered}")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if expected_digest and digest != expected_digest:
        raise ValueError("Published image digest does not match the build result")
    return {"digest": digest, "platforms": sorted(f"{os}/{arch}" for os, arch in platforms)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Versioned GHCR image reference")
    parser.add_argument("--expected-digest", help="Digest emitted by the publish build")
    args = parser.parse_args()
    print(json.dumps(validate_manifest(inspect_raw(args.image), args.expected_digest), sort_keys=True))


if __name__ == "__main__":
    main()
