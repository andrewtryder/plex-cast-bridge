import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from docker_integration_util import ensure_immutable_image_present


def test_ensure_immutable_image_skips_pull_when_present(monkeypatch):
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        class Result:
            returncode = 0
            stdout = "sha256:" + "a" * 64
        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    digest = "ghcr.io/example/bridge@sha256:" + "b" * 64
    assert ensure_immutable_image_present(digest) == "sha256:" + "a" * 64
    assert calls == [["docker", "image", "inspect", digest, "--format", "{{.Id}}"]]


def test_ensure_immutable_image_refuses_mutable_tags(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: type("R", (), {"returncode": 1, "stdout": ""})())
    with pytest.raises(ValueError, match="immutable digest"):
        ensure_immutable_image_present("ghcr.io/example:latest")
