from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from verify_release_artifact import validate_labels, validate_manifest


def _index(*platforms: tuple[str, str]) -> bytes:
    return json.dumps({"schemaVersion": 2, "manifests": [
        {"platform": {"os": os, "architecture": arch}} for os, arch in platforms
    ]}).encode()


def test_manifest_requires_amd64_and_arm64_and_returns_immutable_digest():
    raw = _index(("linux", "amd64"), ("linux", "arm64"))
    assert validate_manifest(raw) == {
        "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "platforms": ["linux/amd64", "linux/arm64"],
    }


def test_manifest_rejects_missing_required_platform_or_wrong_digest():
    raw = _index(("linux", "amd64"))
    with pytest.raises(ValueError, match="linux/arm64"):
        validate_manifest(raw)
    raw = _index(("linux", "amd64"), ("linux", "arm64"))
    with pytest.raises(ValueError, match="does not match"):
        validate_manifest(raw, "sha256:" + "0" * 64)


def test_labels_require_version_and_revision_for_each_release_platform():
    details = {"image": {
        "linux/amd64": {"config": {"Labels": {
            "org.opencontainers.image.version": "0.2.3",
            "org.opencontainers.image.revision": "abc",
        }}},
        "linux/arm64": {"config": {"Labels": {
            "org.opencontainers.image.version": "0.2.3",
            "org.opencontainers.image.revision": "abc",
        }}},
    }}
    validate_labels(details, version="0.2.3", revision="abc")
    details["image"]["linux/arm64"]["config"]["Labels"]["org.opencontainers.image.version"] = "wrong"
    with pytest.raises(ValueError, match="version label"):
        validate_labels(details, version="0.2.3", revision="abc")
