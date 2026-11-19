from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from verify_release_commit import (
    validate_commit_ancestry,
    validate_release_commit,
    validate_release_version,
    verify_release_tag,
)


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=path, check=True, capture_output=True)


def _commit(path: Path, message: str) -> str:
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", message], cwd=path, check=True, capture_output=True)
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True)
    return res.stdout.strip()


def test_validate_release_version_succeeds_when_matching(tmp_path: Path):
    (tmp_path / ".release-please-manifest.json").write_text(json.dumps({".": "0.3.0"}))
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\nversion = "0.3.0"\n')
    validate_release_version(tmp_path, "0.3.0")


def test_validate_release_version_rejects_mismatched_manifest(tmp_path: Path):
    (tmp_path / ".release-please-manifest.json").write_text(json.dumps({".": "0.2.2"}))
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\nversion = "0.3.0"\n')
    with pytest.raises(ValueError, match="does not match .release-please-manifest.json"):
        validate_release_version(tmp_path, "0.3.0")


def test_validate_release_version_rejects_mismatched_pyproject(tmp_path: Path):
    (tmp_path / ".release-please-manifest.json").write_text(json.dumps({".": "0.3.0"}))
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\nversion = "0.2.2"\n')
    with pytest.raises(ValueError, match="does not match pyproject.toml"):
        validate_release_version(tmp_path, "0.3.0")


def test_validate_release_version_requires_manifest_or_pyproject(tmp_path: Path):
    with pytest.raises(ValueError, match="Neither .release-please-manifest.json nor pyproject.toml found"):
        validate_release_version(tmp_path, "0.3.0")


def test_validate_release_version_rejects_pyproject_without_version(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')
    with pytest.raises(ValueError, match="Could not find version field"):
        validate_release_version(tmp_path, "0.3.0")


def test_validate_commit_ancestry_accepts_ancestor_and_rejects_unmerged(tmp_path: Path):
    _init_git_repo(tmp_path)
    (tmp_path / "file.txt").write_text("initial")
    main_c1 = _commit(tmp_path, "initial commit")

    # Create a branch for feature
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "file.txt").write_text("feature branch edit")
    feature_c = _commit(tmp_path, "feature commit")

    # Switch back to main and make another commit
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "main.txt").write_text("main update")
    main_c2 = _commit(tmp_path, "main update")

    # main_c1 is an ancestor of main
    validate_commit_ancestry(tmp_path, main_c1, "main")
    # main_c2 is main itself
    validate_commit_ancestry(tmp_path, main_c2, "main")

    # feature_c is NOT merged into main
    with pytest.raises(ValueError, match="not merged into main"):
        validate_commit_ancestry(tmp_path, feature_c, "main")


def test_validate_release_commit_verifies_head_and_expected_ref(tmp_path: Path):
    _init_git_repo(tmp_path)
    (tmp_path / ".release-please-manifest.json").write_text(json.dumps({".": "0.3.0"}))
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\nversion = "0.3.0"\n')
    _commit(tmp_path, "release 0.3.0")

    # Create another branch pointing to an earlier/different commit
    subprocess.run(["git", "branch", "other"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "extra.txt").write_text("extra")
    head_sha2 = _commit(tmp_path, "second commit")

    # When ref matches HEAD
    result = validate_release_commit(tmp_path, "0.3.0", main_ref="main", expected_ref="main")
    assert result == head_sha2

    # When expected ref points to a different commit than HEAD
    with pytest.raises(ValueError, match="does not match requested ref"):
        validate_release_commit(tmp_path, "0.3.0", main_ref="main", expected_ref="other")

    # When expected ref cannot be resolved
    with pytest.raises(ValueError, match="Unable to resolve git reference"):
        validate_release_commit(tmp_path, "0.3.0", main_ref="main", expected_ref="nonexistent-ref")


def test_verify_release_tag_matching_commit(tmp_path: Path):
    _init_git_repo(tmp_path)
    (tmp_path / "file.txt").write_text("v0.3.0 content")
    commit_sha = _commit(tmp_path, "v0.3.0 commit")
    subprocess.run(["git", "tag", "-a", "v0.3.0", "-m", "Release v0.3.0"], cwd=tmp_path, check=True, capture_output=True)

    verified = verify_release_tag(tmp_path, "v0.3.0", commit_sha)
    assert verified == commit_sha


def test_verify_release_tag_mismatched_commit(tmp_path: Path):
    _init_git_repo(tmp_path)
    (tmp_path / "file.txt").write_text("first")
    _commit(tmp_path, "first commit")
    subprocess.run(["git", "tag", "-a", "v0.3.0", "-m", "Release v0.3.0"], cwd=tmp_path, check=True, capture_output=True)

    (tmp_path / "file.txt").write_text("second")
    commit2 = _commit(tmp_path, "second commit")

    with pytest.raises(ValueError, match="points to commit .*, but expected"):
        verify_release_tag(tmp_path, "v0.3.0", commit2)


def test_verify_release_tag_nonexistent(tmp_path: Path):
    _init_git_repo(tmp_path)
    (tmp_path / "file.txt").write_text("initial")
    commit_sha = _commit(tmp_path, "initial")

    with pytest.raises(ValueError, match="Unable to resolve git tag"):
        verify_release_tag(tmp_path, "v0.3.0", commit_sha)
