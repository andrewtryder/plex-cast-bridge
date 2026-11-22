import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from docker_integration_util import ensure_immutable_image_present

DIGEST = "ghcr.io/example/bridge@sha256:" + "b" * 64
IMAGE_ID = "sha256:" + "a" * 64


def test_immutable_digest_already_present_skips_pull(monkeypatch):
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, IMAGE_ID, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ensure_immutable_image_present(DIGEST) == IMAGE_ID
    assert calls == [["docker", "image", "inspect", DIGEST, "--format", "{{.Id}}"]]


def test_immutable_digest_missing_pulls_then_inspects(monkeypatch):
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        if argv[0:2] == ["docker", "image"] and len(calls) == 1:
            return subprocess.CompletedProcess(argv, 1, "", "missing")
        if argv[0:2] == ["docker", "pull"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, IMAGE_ID, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ensure_immutable_image_present(DIGEST) == IMAGE_ID
    assert [c[0:2] for c in calls] == [["docker", "image"], ["docker", "pull"], ["docker", "image"]]


@pytest.mark.parametrize("mutable_ref", ["ghcr.io/example:latest", "plex-cast-bridge:candidate"])
def test_mutable_tag_rejected_when_cached(monkeypatch, mutable_ref):
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, IMAGE_ID, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="immutable digest"):
        ensure_immutable_image_present(mutable_ref)
    assert calls == []


def test_mutable_tag_rejected_when_not_cached(monkeypatch):
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="immutable digest"):
        ensure_immutable_image_present("ghcr.io/example:old")
    assert calls == []


def test_invalid_digest_rejected_before_docker(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **_k: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    with pytest.raises(ValueError, match="immutable digest"):
        ensure_immutable_image_present("ghcr.io/example@sha256:tooshort")
    assert calls == []


def test_inspect_failure_triggers_pull(monkeypatch):
    pull_seen = []

    def fake_run(argv, **_kwargs):
        if argv[0:2] == ["docker", "pull"]:
            pull_seen.append(True)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 1, "", "no such image")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="missing after pull"):
        ensure_immutable_image_present(DIGEST)
    assert pull_seen


def test_pull_failure_propagates(monkeypatch):
    def fake_run(argv, **kwargs):
        if argv[0:2] == ["docker", "image"]:
            return subprocess.CompletedProcess(argv, 1, "", "")
        if argv[0:2] == ["docker", "pull"]:
            raise subprocess.CalledProcessError(1, argv, "", "network error")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="pull failed"):
        ensure_immutable_image_present(DIGEST)


def test_missing_image_after_successful_pull(monkeypatch):
    def fake_run(argv, **_kwargs):
        if argv[0:2] == ["docker", "pull"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="missing after pull"):
        ensure_immutable_image_present(DIGEST)
