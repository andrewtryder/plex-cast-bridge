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
        raise RuntimeError(f"Unable to inspect the published OCI image: {exc}") from exc
    if result.returncode:
        err = (
            result.stderr.decode(errors="replace").strip()
            if isinstance(result.stderr, bytes)
            else str(result.stderr).strip()
        )
        raise RuntimeError(f"Published image tag could not be resolved: {err}")
    return result.stdout


def inspect_details(reference: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["docker", "buildx", "imagetools", "inspect", "--format", "{{json .}}", reference],
            check=False,
            capture_output=True,
            timeout=60,
        )
        if result.returncode:
            err = (
                result.stderr.decode(errors="replace").strip()
                if isinstance(result.stderr, bytes)
                else str(result.stderr).strip()
            )
            raise RuntimeError(f"Published image metadata could not be resolved: {err}")
        return json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to inspect published OCI metadata: {exc}") from exc


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
        raise ValueError(
            f"Published image digest does not match the build result: expected {expected_digest}, got {digest}"
        )
    return {"digest": digest, "platforms": sorted(f"{os}/{arch}" for os, arch in platforms)}


def validate_labels(details: dict[str, Any], *, version: str, revision: str) -> None:
    images = details.get("image")
    if not isinstance(images, dict):
        raise ValueError("Published image metadata is incomplete")
    for os_name, architecture in REQUIRED_PLATFORMS:
        platform = f"{os_name}/{architecture}"
        labels = images.get(platform, {}).get("config", {}).get("Labels")
        if not isinstance(labels, dict):
            raise ValueError(f"Published image is missing OCI labels for {platform}")
        actual_version = labels.get("org.opencontainers.image.version")
        if actual_version != version:
            raise ValueError(
                f"Published image has an incorrect OCI version label for {platform}: "
                f"expected {version!r}, got {actual_version!r}"
            )
        actual_revision = labels.get("org.opencontainers.image.revision")
        if actual_revision != revision:
            raise ValueError(
                f"Published image has an incorrect OCI revision label for {platform}: "
                f"expected {revision!r}, got {actual_revision!r}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Versioned GHCR image reference")
    parser.add_argument("--expected-digest", help="Digest emitted by the publish build")
    parser.add_argument("--expected-version", required=True, help="Release version expected in OCI labels")
    parser.add_argument("--expected-revision", required=True, help="Git revision expected in OCI labels")
    args = parser.parse_args()
    result = validate_manifest(inspect_raw(args.image), args.expected_digest)
    validate_labels(inspect_details(args.image), version=args.expected_version, revision=args.expected_revision)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
