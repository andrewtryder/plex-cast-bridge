#!/usr/bin/env python3
"""Verify that a release candidate commit is merged into main and matches the release version."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


def resolve_commit(repo_dir: Path, ref: str) -> str:
    for candidate in [f"{ref}^{{commit}}", f"origin/{ref}^{{commit}}"]:
        res = subprocess.run(
            ["git", "rev-parse", "--verify", candidate],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0:
            return res.stdout.strip()
    raise ValueError(f"Unable to resolve git reference {ref!r}")


def get_head_commit(repo_dir: Path) -> str:
    res = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"Unable to determine HEAD commit: {res.stderr.strip()}")
    return res.stdout.strip()


def validate_commit_ancestry(repo_dir: Path, commit_sha: str, main_ref: str = "origin/main") -> None:
    main_sha = resolve_commit(repo_dir, main_ref)
    res = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit_sha, main_sha],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise ValueError(
            f"Release revision {commit_sha} is not merged into {main_ref} ({main_sha}); "
            "official releases must be built from a reviewed commit on main."
        )


def validate_release_version(repo_dir: Path, expected_version: str) -> None:
    found_any = False

    manifest_path = repo_dir / ".release-please-manifest.json"
    if manifest_path.is_file():
        found_any = True
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {manifest_path}: {exc}") from exc
        manifest_version = data.get(".", "")
        if manifest_version != expected_version:
            raise ValueError(
                f"Release version {expected_version!r} does not match "
                f".release-please-manifest.json ({manifest_version!r})"
            )

    pyproject_path = repo_dir / "pyproject.toml"
    if pyproject_path.is_file():
        found_any = True
        content = pyproject_path.read_text(encoding="utf-8")
        match = re.search(r'(?m)^version\s*=\s*["\']([^"\']+)["\']', content)
        if match:
            pyproject_version = match.group(1)
            if pyproject_version != expected_version:
                raise ValueError(
                    f"Release version {expected_version!r} does not match "
                    f"pyproject.toml ({pyproject_version!r})"
                )
        else:
            raise ValueError(f"Could not find version field in {pyproject_path}")

    if not found_any:
        raise ValueError("Neither .release-please-manifest.json nor pyproject.toml found to verify version")


def validate_release_commit(
    repo_dir: Path,
    expected_version: str,
    main_ref: str = "origin/main",
    expected_ref: str | None = None,
) -> str:
    head_sha = get_head_commit(repo_dir)

    if expected_ref is not None:
        resolved_expected = resolve_commit(repo_dir, expected_ref)
        if resolved_expected != head_sha:
            raise ValueError(
                f"Checked out HEAD ({head_sha}) does not match requested ref "
                f"{expected_ref!r} ({resolved_expected})"
            )

    validate_commit_ancestry(repo_dir, head_sha, main_ref)
    validate_release_version(repo_dir, expected_version)
    return head_sha


def verify_release_tag(repo_dir: Path, tag_name: str, expected_sha: str) -> str:
    for candidate in [f"refs/tags/{tag_name}^{{commit}}", f"{tag_name}^{{commit}}"]:
        res = subprocess.run(
            ["git", "rev-parse", "--verify", candidate],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0:
            tag_sha = res.stdout.strip()
            if tag_sha != expected_sha:
                raise ValueError(
                    f"Release tag {tag_name!r} points to commit {tag_sha}, but expected {expected_sha}"
                )
            return tag_sha
    raise ValueError(f"Unable to resolve git tag {tag_name!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="Release version (e.g. 0.3.0)")
    parser.add_argument("--ref", default=None, help="Optional requested git ref to verify against HEAD")
    parser.add_argument("--main-ref", default="origin/main", help="Git reference for main (default: origin/main)")
    parser.add_argument("--verify-tag", default=None, help="Verify that existing git tag resolves to release commit")
    parser.add_argument("--repo-dir", default=".", help="Path to repository root")
    args = parser.parse_args()

    try:
        sha = validate_release_commit(
            repo_dir=Path(args.repo_dir).resolve(),
            expected_version=args.version,
            main_ref=args.main_ref,
            expected_ref=args.ref,
        )
        print(f"Validated release commit {sha} for version {args.version} on {args.main_ref}")
        if args.verify_tag:
            verify_release_tag(
                repo_dir=Path(args.repo_dir).resolve(),
                tag_name=args.verify_tag,
                expected_sha=sha,
            )
            print(f"Verified release tag {args.verify_tag} points to {sha}")
    except (ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
