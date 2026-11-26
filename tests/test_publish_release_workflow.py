from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "publish-release.yml"
RELEASE_PLEASE_CONFIG_PATH = REPO_ROOT / "release-please-config.json"
REAL_GIT = shutil.which("git") or "git"

CANDIDATE_TAG_VERSION = "0.5.0"
CANDIDATE_MAJOR_MINOR = "0.5"
GH_REPO = "andrewtryder/plex-cast-bridge"


def _load_workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _get_steps() -> list[dict[str, Any]]:
    workflow = _load_workflow()
    return workflow["jobs"]["publish"]["steps"]


def _get_step(name: str) -> dict[str, Any]:
    for step in _get_steps():
        if step.get("name") == name:
            return step
    raise AssertionError(f"Step not found in workflow: {name}")


def _manifest_bytes(label_marker: str = "approved") -> bytes:
    payload = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "platform": {"os": "linux", "architecture": "amd64"},
                "digest": "sha256:" + hashlib.sha256(f"{label_marker}-amd64".encode()).hexdigest(),
            },
            {
                "platform": {"os": "linux", "architecture": "arm64"},
                "digest": "sha256:" + hashlib.sha256(f"{label_marker}-arm64".encode()).hexdigest(),
            },
        ],
        "annotations": {"org.example.marker": label_marker},
    }
    return json.dumps(payload, sort_keys=True).encode("utf-8")


def _digest_of(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _details_payload(version: str, revision: str) -> dict[str, Any]:
    labels = {
        "org.opencontainers.image.version": version,
        "org.opencontainers.image.revision": revision,
    }
    return {
        "image": {
            "linux/amd64": {"config": {"Labels": dict(labels)}},
            "linux/arm64": {"config": {"Labels": dict(labels)}},
        }
    }


def _eval_if(expr: str | None, inputs: dict[str, str], step_outputs: dict[str, dict[str, str]], job_failed: bool) -> bool:
    if expr is None:
        return not job_failed
    inner = expr.strip()
    if inner.startswith("${{") and inner.endswith("}}"):
        inner = inner[3:-2].strip()
    clauses = [part.strip() for part in inner.split("&&")]
    has_status_fn = any(c in ("always()", "failure()", "cancelled()", "success()") for c in clauses)
    if not has_status_fn and job_failed:
        return False
    for clause in clauses:
        if clause == "success()":
            if job_failed:
                return False
        elif clause in ("always()", "failure()", "cancelled()"):
            return False
        elif m := re.fullmatch(r"inputs\.([a-zA-Z0-9_]+)\s*(==|!=)\s*'([^']*)'", clause):
            key, op, expected = m.groups()
            actual = inputs.get(key, "")
            if (op == "==" and actual != expected) or (op == "!=" and actual == expected):
                return False
        elif m := re.fullmatch(r"steps\.([a-zA-Z0-9_]+)\.outputs\.([a-zA-Z0-9_]+)\s*(==|!=)\s*'([^']*)'", clause):
            step_id, out_key, op, expected = m.groups()
            actual = step_outputs.get(step_id, {}).get(out_key, "")
            if (op == "==" and actual != expected) or (op == "!=" and actual == expected):
                return False
        else:
            raise AssertionError(f"Unsupported workflow if-clause in test evaluator: {clause!r}")
    return True


def _resolve_expr(value: Any, inputs: dict[str, str], step_outputs: dict[str, dict[str, str]]) -> str:
    text = str(value)

    def _eval_token(token: str) -> str:
        token = token.strip()
        if m := re.fullmatch(r"inputs\.([a-zA-Z0-9_]+)", token):
            return inputs.get(m.group(1), "")
        if m := re.fullmatch(r"steps\.([a-zA-Z0-9_]+)\.outputs\.([a-zA-Z0-9_]+)", token):
            return step_outputs.get(m.group(1), {}).get(m.group(2), "")
        if token == "github.token":
            return "fake-github-token"
        if token == "github.repository":
            return GH_REPO
        raise AssertionError(f"Unsupported GitHub expression token: {token!r}")

    def _replace(match: re.Match[str]) -> str:
        expr = match.group(1).strip()
        for part in expr.split("||"):
            val = _eval_token(part)
            if val:
                return val
        return ""

    return re.sub(r"\$\{\{\s*([^}]+?)\s*\}\}", _replace, text)


def _write_shims(bin_dir: Path, state_path: Path, trace_path: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)

    docker_shim = bin_dir / "docker"
    docker_shim.write_text(
        f"""#!{sys.executable}
import hashlib
import json
import sys
from pathlib import Path

STATE_PATH = Path({str(state_path)!r})
TRACE_PATH = Path({str(trace_path)!r})

args = sys.argv[1:]
with TRACE_PATH.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"tool": "docker", "args": args}}) + "\\n")

state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
if args[:3] == ["buildx", "imagetools", "inspect"]:
    rest = args[3:]
    raw_mode = False
    fmt = None
    ref = None
    i = 0
    while i < len(rest):
        if rest[i] == "--raw":
            raw_mode = True
            i += 1
        elif rest[i] == "--format" and i + 1 < len(rest):
            fmt = rest[i + 1]
            i += 2
        else:
            ref = rest[i]
            i += 1
    entry = state["images"].get(ref)
    if not entry:
        sys.stderr.write(f"ERROR: manifest not found for {{ref}}\\n")
        sys.exit(1)
    if raw_mode:
        sys.stdout.write(entry["raw"])
    elif fmt == "{{{{json .}}}}":
        sys.stdout.write(json.dumps(entry["details"]))
    else:
        sys.stdout.write(json.dumps({{"ref": ref, "digest": "sha256:" + hashlib.sha256(entry["raw"].encode()).hexdigest()}}))
    sys.exit(0)

if args[:3] == ["buildx", "imagetools", "create"]:
    rest = args[3:]
    tags = []
    sources = []
    i = 0
    while i < len(rest):
        if rest[i] in ("--tag", "-t") and i + 1 < len(rest):
            tags.append(rest[i + 1])
            i += 2
        else:
            sources.append(rest[i])
            i += 1
    mode = "latest" if any(t.endswith(":latest") for t in tags) else "versioned"
    fail_key = f"create_failures_{{mode}}"
    remaining = state.get(fail_key, 0)
    if remaining > 0:
        state[fail_key] = remaining - 1
        STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
        sys.stderr.write(f"simulated registry push failure ({{mode}}, remaining={{remaining - 1}})\\n")
        sys.exit(1)
    src = sources[0]
    src_entry = state["images"].get(src)
    if not src_entry:
        sys.stderr.write(f"ERROR: source digest not found: {{src}}\\n")
        sys.exit(1)
    overrides = state.get("post_create_overrides", {{}})
    for tag in tags:
        state["images"][tag] = overrides.get(tag, src_entry)
    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    sys.exit(0)

sys.stderr.write(f"Unexpected docker invocation in test shim: {{args}}\\n")
sys.exit(2)
""",
        encoding="utf-8",
    )
    docker_shim.chmod(0o755)

    git_shim = bin_dir / "git"
    git_shim.write_text(
        f"""#!{sys.executable}
import json
import subprocess
import sys
from pathlib import Path

STATE_PATH = Path({str(state_path)!r})
TRACE_PATH = Path({str(trace_path)!r})
REAL_GIT = {REAL_GIT!r}

args = sys.argv[1:]
if args and args[0] == "push":
    with TRACE_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({{"tool": "git", "args": args}}) + "\\n")
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    remaining = state.get("git_push_failures", 0)
    if remaining > 0:
        state["git_push_failures"] = remaining - 1
        STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
        sys.stderr.write(f"simulated git push failure (remaining={{remaining - 1}})\\n")
        sys.exit(1)

res = subprocess.run([REAL_GIT, *args], check=False)
sys.exit(res.returncode)
""",
        encoding="utf-8",
    )
    git_shim.chmod(0o755)

    gh_shim = bin_dir / "gh"
    gh_shim.write_text(
        f"""#!{sys.executable}
import json
import sys
from pathlib import Path

STATE_PATH = Path({str(state_path)!r})
TRACE_PATH = Path({str(trace_path)!r})

args = sys.argv[1:]
with TRACE_PATH.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"tool": "gh", "args": args}}) + "\\n")

state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
if args[:2] == ["release", "view"]:
    tag = args[2]
    if tag in state.get("releases", []):
        sys.stdout.write(json.dumps({{"tagName": tag}}) + "\\n")
        sys.exit(0)
    sys.stderr.write(f"release not found: {{tag}}\\n")
    sys.exit(1)

if args[:2] == ["release", "create"]:
    if state.get("fail_gh_release_create"):
        sys.stderr.write("simulated gh release create failure\\n")
        sys.exit(1)
    tag = args[2]
    state.setdefault("releases", []).append(tag)
    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    sys.exit(0)

sys.stderr.write(f"Unexpected gh invocation in test shim: {{args}}\\n")
sys.exit(2)
""",
        encoding="utf-8",
    )
    gh_shim.chmod(0o755)


def _setup_temp_repo(tmp_path: Path, version: str = CANDIDATE_TAG_VERSION) -> tuple[Path, Path, str]:
    remote_dir = tmp_path / "remote.git"
    work_dir = tmp_path / "repo"
    subprocess.run([REAL_GIT, "init", "--bare", str(remote_dir)], check=True, capture_output=True)
    subprocess.run([REAL_GIT, "init", "-b", "main", str(work_dir)], check=True, capture_output=True)
    subprocess.run([REAL_GIT, "-C", str(work_dir), "config", "user.name", "Test User"], check=True, capture_output=True)
    subprocess.run([REAL_GIT, "-C", str(work_dir), "config", "user.email", "test@example.com"], check=True, capture_output=True)
    subprocess.run([REAL_GIT, "-C", str(work_dir), "remote", "add", "origin", str(remote_dir)], check=True, capture_output=True)

    (work_dir / ".release-please-manifest.json").write_text(json.dumps({".": version}) + "\n", encoding="utf-8")
    (work_dir / "pyproject.toml").write_text(f'[project]\nname = "plex-cast-bridge"\nversion = "{version}"\n', encoding="utf-8")
    scripts_dir = work_dir / "scripts"
    scripts_dir.mkdir()
    for script_name in ("verify_release_commit.py", "verify_release_artifact.py"):
        shutil.copy2(REPO_ROOT / "scripts" / script_name, scripts_dir / script_name)

    subprocess.run([REAL_GIT, "-C", str(work_dir), "add", "."], check=True, capture_output=True)
    subprocess.run([REAL_GIT, "-C", str(work_dir), "commit", "-m", f"chore(main): release {version}"], check=True, capture_output=True)
    subprocess.run([REAL_GIT, "-C", str(work_dir), "push", "-u", "origin", "main"], check=True, capture_output=True)
    commit_sha = subprocess.check_output([REAL_GIT, "-C", str(work_dir), "rev-parse", "HEAD"], text=True).strip()
    return work_dir, remote_dir, commit_sha


def _run_workflow_pipeline(
    tmp_path: Path,
    *,
    mode: str = "promote",
    version: str = CANDIDATE_TAG_VERSION,
    ref: str = "main",
    allow_recovery: bool = False,
    state_overrides: dict[str, Any] | None = None,
    candidate_raw: bytes | None = None,
    expected_digest_override: str | None = None,
) -> dict[str, Any]:
    work_dir, remote_dir, commit_sha = _setup_temp_repo(tmp_path, version=version)
    raw = candidate_raw if candidate_raw is not None else _manifest_bytes("approved")
    candidate_digest = _digest_of(raw)
    details = _details_payload(version, commit_sha)
    candidate_entry = {"raw": raw.decode("utf-8"), "details": details}

    images: dict[str, Any] = {
        f"ghcr.io/{GH_REPO}:candidate-{version}": candidate_entry,
        f"ghcr.io/{GH_REPO}@{candidate_digest}": candidate_entry,
    }
    state: dict[str, Any] = {
        "images": images,
        "releases": [],
        "create_failures_versioned": 0,
        "create_failures_latest": 0,
        "git_push_failures": 0,
        "fail_gh_release_create": False,
        "post_create_overrides": {},
    }
    if state_overrides:
        if "images" in state_overrides:
            state["images"].update(state_overrides["images"])
        for k, v in state_overrides.items():
            if k != "images":
                state[k] = v
    for mapping in (state["images"], state["post_create_overrides"]):
        for entry in mapping.values():
            for platform_data in (entry.get("details") or {}).get("image", {}).values():
                labels = platform_data.get("config", {}).get("Labels", {})
                if labels.get("org.opencontainers.image.revision") == "__COMMIT_SHA__":
                    labels["org.opencontainers.image.revision"] = commit_sha

    state_path = tmp_path / "shim_state.json"
    trace_path = tmp_path / "shim_trace.jsonl"
    summary_path = tmp_path / "step_summary.md"
    bin_dir = tmp_path / "bin"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    trace_path.write_text("", encoding="utf-8")
    summary_path.write_text("", encoding="utf-8")
    _write_shims(bin_dir, state_path, trace_path)

    inputs = {
        "mode": mode,
        "version": version,
        "ref": ref,
        "candidate_digest": expected_digest_override if expected_digest_override is not None else (candidate_digest if mode == "promote" else ""),
        "allow_recovery": "true" if allow_recovery else "false",
    }
    step_outputs: dict[str, dict[str, str]] = {}
    executed_steps: list[str] = []
    step_results: list[dict[str, Any]] = []
    job_failed = False

    for step in _get_steps():
        name = step["name"]
        if not _eval_if(step.get("if"), inputs, step_outputs, job_failed):
            continue

        if mode == "candidate" and name in {
            "Run Linux Docker acceptance matrix against exact candidate digest",
            "Verify official legacy image upgrade to candidate digest",
        }:
            executed_steps.append(name)
            step_results.append({"name": name, "returncode": 0, "stdout": "", "stderr": ""})
            continue

        if "uses" in step:
            executed_steps.append(name)
            if step.get("id") == "build":
                step_outputs["build"] = {"digest": candidate_digest}
            continue

        executed_steps.append(name)
        step_id = step.get("id")
        output_file = tmp_path / f"gh_out_{len(executed_steps)}.txt"
        output_file.write_text("", encoding="utf-8")

        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            "GITHUB_REPOSITORY": GH_REPO,
            "GITHUB_OUTPUT": str(output_file),
            "GITHUB_STEP_SUMMARY": str(summary_path),
            "RETRY_DELAY_SECONDS": "0",
        }
        for key, raw_val in (step.get("env") or {}).items():
            env[key] = _resolve_expr(raw_val, inputs, step_outputs)

        res = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", step["run"]],
            cwd=work_dir,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if step_id:
            parsed: dict[str, str] = {}
            for line in output_file.read_text(encoding="utf-8").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    parsed[k.strip()] = v.strip()
            step_outputs[step_id] = parsed
        step_results.append({"name": name, "returncode": res.returncode, "stdout": res.stdout, "stderr": res.stderr})
        if res.returncode != 0:
            job_failed = True

    trace = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {
        "failed": job_failed,
        "executed_steps": executed_steps,
        "step_results": step_results,
        "step_outputs": step_outputs,
        "trace": trace,
        "state": json.loads(state_path.read_text(encoding="utf-8")),
        "summary": summary_path.read_text(encoding="utf-8"),
        "work_dir": work_dir,
        "remote_dir": remote_dir,
        "commit_sha": commit_sha,
        "candidate_digest": candidate_digest,
    }


def _step_result(run_result: dict[str, Any], name: str) -> dict[str, Any]:
    for entry in run_result["step_results"]:
        if entry["name"] == name:
            return entry
    raise AssertionError(f"Step did not run: {name}; executed={run_result['executed_steps']}")


def test_ghcr_version_tag_publish_fails_nonzero_after_three_failed_attempts(tmp_path: Path):
    fail_run = _run_workflow_pipeline(
        tmp_path / "fail",
        state_overrides={"create_failures_versioned": 3},
    )
    assert fail_run["failed"] is True
    pub_step = _step_result(fail_run, "Publish verified versioned tags to GHCR")
    assert pub_step["returncode"] != 0
    assert "after 3 attempts" in pub_step["stderr"]

    versioned_creates = [
        item for item in fail_run["trace"]
        if item["tool"] == "docker"
        and item["args"][:3] == ["buildx", "imagetools", "create"]
        and any(t.endswith(f":{CANDIDATE_TAG_VERSION}") for t in item["args"])
    ]
    assert len(versioned_creates) == 3

    for forbidden_step in (
        "Verify published versioned GHCR tags",
        "Create release tag",
        "Create GitHub release",
        "Promote the verified digest to latest",
    ):
        assert forbidden_step not in fail_run["executed_steps"]

    retry_pass = _run_workflow_pipeline(
        tmp_path / "retry_pass",
        state_overrides={"create_failures_versioned": 2},
    )
    assert retry_pass["failed"] is False
    assert _step_result(retry_pass, "Publish verified versioned tags to GHCR")["returncode"] == 0
    retry_creates = [
        item for item in retry_pass["trace"]
        if item["tool"] == "docker"
        and item["args"][:3] == ["buildx", "imagetools", "create"]
        and any(t.endswith(f":{CANDIDATE_TAG_VERSION}") for t in item["args"])
    ]
    assert len(retry_creates) == 3


def test_git_tag_push_fails_nonzero_after_three_failed_attempts(tmp_path: Path):
    fail_run = _run_workflow_pipeline(
        tmp_path / "fail",
        state_overrides={"git_push_failures": 3},
    )
    assert fail_run["failed"] is True
    tag_step = _step_result(fail_run, "Create release tag")
    assert tag_step["returncode"] != 0
    assert "after 3 attempts" in tag_step["stderr"]

    push_calls = [item for item in fail_run["trace"] if item["tool"] == "git" and item["args"][0] == "push"]
    assert len(push_calls) == 3
    assert "Create GitHub release" not in fail_run["executed_steps"]
    assert "Promote the verified digest to latest" not in fail_run["executed_steps"]

    retry_pass = _run_workflow_pipeline(
        tmp_path / "retry_pass",
        state_overrides={"git_push_failures": 2},
    )
    assert retry_pass["failed"] is False
    assert _step_result(retry_pass, "Create release tag")["returncode"] == 0
    retry_pushes = [item for item in retry_pass["trace"] if item["tool"] == "git" and item["args"][0] == "push"]
    assert len(retry_pushes) == 3
    ls_remote = subprocess.run(
        [REAL_GIT, "--git-dir", str(retry_pass["remote_dir"]), "show-ref", "--tags", f"refs/tags/v{CANDIDATE_TAG_VERSION}"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert ls_remote.returncode == 0


@pytest.mark.parametrize("tampered_tag", [CANDIDATE_TAG_VERSION, CANDIDATE_MAJOR_MINOR])
def test_versioned_ghcr_tags_verified_against_candidate_digest_before_github_release(
    tmp_path: Path, tampered_tag: str
):
    steps = [s["name"] for s in _get_steps()]
    assert steps.index("Publish verified versioned tags to GHCR") < steps.index("Verify published versioned GHCR tags")
    assert steps.index("Verify published versioned GHCR tags") < steps.index("Create release tag")
    assert steps.index("Create release tag") < steps.index("Create GitHub release")
    assert steps.index("Create GitHub release") < steps.index("Promote the verified digest to latest")

    wrong_raw = _manifest_bytes("tampered-version-tag")
    tampered_entry = {
        "raw": wrong_raw.decode("utf-8"),
        "details": _details_payload(CANDIDATE_TAG_VERSION, "__COMMIT_SHA__"),
    }

    fail_run = _run_workflow_pipeline(
        tmp_path / f"tamper_{tampered_tag}",
        state_overrides={
            "post_create_overrides": {
                f"ghcr.io/{GH_REPO}:{tampered_tag}": tampered_entry,
            }
        },
    )
    assert fail_run["failed"] is True
    verify_step = _step_result(fail_run, "Verify published versioned GHCR tags")
    assert verify_step["returncode"] != 0
    assert "does not match" in verify_step["stderr"]
    assert "Create release tag" not in fail_run["executed_steps"]
    assert "Create GitHub release" not in fail_run["executed_steps"]
    assert "Promote the verified digest to latest" not in fail_run["executed_steps"]
    assert not [i for i in fail_run["trace"] if i["tool"] == "gh"]


def test_recovery_rejects_existing_release_image_with_mismatched_digest(tmp_path: Path):
    approved_raw = _manifest_bytes("approved-candidate")
    mismatched_raw = _manifest_bytes("stale-existing-release")
    assert _digest_of(approved_raw) != _digest_of(mismatched_raw)

    mismatched_entry = {
        "raw": mismatched_raw.decode("utf-8"),
        "details": _details_payload(CANDIDATE_TAG_VERSION, "__COMMIT_SHA__"),
    }

    # Even when platforms & OCI version/revision labels match, recovery MUST reject a digest mismatch.
    bad_recovery = _run_workflow_pipeline(
        tmp_path / "bad_recovery",
        allow_recovery=True,
        candidate_raw=approved_raw,
        state_overrides={
            "images": {
                f"ghcr.io/{GH_REPO}:{CANDIDATE_TAG_VERSION}": mismatched_entry,
                f"ghcr.io/{GH_REPO}:{CANDIDATE_MAJOR_MINOR}": mismatched_entry,
            }
        },
    )
    assert bad_recovery["failed"] is True
    status_step = _step_result(bad_recovery, "Check versioned image status")
    assert status_step["returncode"] != 0
    assert "does not match" in status_step["stderr"]
    assert bad_recovery["step_outputs"].get("image_status", {}).get("image_exists") != "true"
    assert "Create release tag" not in bad_recovery["executed_steps"]
    assert "Create GitHub release" not in bad_recovery["executed_steps"]
    assert "Promote the verified digest to latest" not in bad_recovery["executed_steps"]

    # Non-recovery must refuse when the versioned image already exists, even if its digest matches.
    non_recovery = _run_workflow_pipeline(
        tmp_path / "non_recovery",
        allow_recovery=False,
        candidate_raw=approved_raw,
        state_overrides={
            "images": {
                f"ghcr.io/{GH_REPO}:{CANDIDATE_TAG_VERSION}": {
                    "raw": approved_raw.decode("utf-8"),
                    "details": _details_payload(CANDIDATE_TAG_VERSION, "__COMMIT_SHA__"),
                }
            }
        },
    )
    assert non_recovery["failed"] is True
    assert _step_result(non_recovery, "Check versioned image status")["returncode"] != 0

    clean_run = _run_workflow_pipeline(tmp_path / "clean_run", allow_recovery=False, candidate_raw=approved_raw)
    assert clean_run["failed"] is False
    assert f"ghcr.io/{GH_REPO}:{CANDIDATE_TAG_VERSION}" in clean_run["state"]["images"]
    assert f"ghcr.io/{GH_REPO}:latest" in clean_run["state"]["images"]
    assert f"v{CANDIDATE_TAG_VERSION}" in clean_run["state"]["releases"]


def test_recovery_succeeds_when_existing_image_matches_candidate_digest(tmp_path: Path):
    approved_raw = _manifest_bytes("approved-recovery")
    approved_digest = _digest_of(approved_raw)
    work_dir, _, commit_sha = _setup_temp_repo(tmp_path)
    matching_entry = {
        "raw": approved_raw.decode("utf-8"),
        "details": _details_payload(CANDIDATE_TAG_VERSION, commit_sha),
    }

    state_path = tmp_path / "state.json"
    trace_path = tmp_path / "trace.jsonl"
    bin_dir = tmp_path / "bin"
    out_file = tmp_path / "gh_out.txt"
    out_file.write_text("", encoding="utf-8")
    trace_path.write_text("", encoding="utf-8")
    state_path.write_text(
        json.dumps(
            {
                "images": {
                    f"ghcr.io/{GH_REPO}:{CANDIDATE_TAG_VERSION}": matching_entry,
                    f"ghcr.io/{GH_REPO}:{CANDIDATE_MAJOR_MINOR}": matching_entry,
                }
            }
        ),
        encoding="utf-8",
    )
    _write_shims(bin_dir, state_path, trace_path)

    step = _get_step("Check versioned image status")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "GITHUB_REPOSITORY": GH_REPO,
        "GITHUB_OUTPUT": str(out_file),
        "RELEASE_VERSION": CANDIDATE_TAG_VERSION,
        "CANDIDATE_DIGEST": approved_digest,
        "EXPECTED_SHA": commit_sha,
        "ALLOW_RECOVERY": "true",
    }
    res = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", step["run"]],
        cwd=work_dir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    outputs = dict(line.split("=", 1) for line in out_file.read_text(encoding="utf-8").splitlines() if "=" in line)
    assert outputs["image_exists"] == "true"
    assert outputs["digest"] == approved_digest


def test_candidate_mode_never_creates_stable_tags_or_updates_latest(tmp_path: Path):
    meta_step = _get_step("Extract Docker metadata for candidate")
    build_step = _get_step("Build and push candidate multi-architecture image")
    assert "candidate-" in meta_step["with"]["tags"]
    assert "latest" not in meta_step["with"]["tags"]
    assert build_step["with"]["tags"] == "ghcr.io/${{ github.repository }}:candidate-${{ inputs.version }}"
    acceptance = _get_step("Run Linux Docker acceptance matrix against exact candidate digest")
    assert acceptance["env"]["CANDIDATE_IMAGE"] == "ghcr.io/${{ github.repository }}@${{ steps.build.outputs.digest }}"
    assert acceptance["env"]["PCB_TEST_UPGRADE_TARGET"] == acceptance["env"]["CANDIDATE_IMAGE"]
    assert "docker pull \"$CANDIDATE_IMAGE\"" in acceptance["run"]
    assert "docker tag \"$CANDIDATE_IMAGE\" \"$PCB_TEST_LOCAL_IMAGE\"" in acceptance["run"]
    assert "pytest -q -m docker_integration" in acceptance["run"]
    assert acceptance["env"]["PCB_TEST_SQLITE_ONLINE"] == "1"
    assert acceptance["env"]["PCB_TEST_SQLITE_IMAGE"] == acceptance["env"]["PCB_TEST_LOCAL_IMAGE"]
    assert acceptance["env"]["PCB_TEST_COMPOSE_RECREATE"] == "1"
    legacy = _get_step("Verify official legacy image upgrade to candidate digest")
    assert legacy["env"]["PCB_TEST_UPGRADE_TARGET"] == legacy["env"]["CANDIDATE_IMAGE"]
    assert "PCB_TEST_START_IMAGE" in legacy["env"]

    for promote_only_step in (
        "Check release tag status",
        "Check versioned image status",
        "Verify candidate artifact and OCI metadata (promote mode)",
        "Publish verified versioned tags to GHCR",
        "Verify published versioned GHCR tags",
        "Create release tag",
        "Create GitHub release",
        "Promote the verified digest to latest",
        "Record promotion summary",
    ):
        cond = _get_step(promote_only_step).get("if", "")
        assert "inputs.mode == 'promote'" in cond, f"{promote_only_step} missing promote guard: {cond!r}"

    cand_run = _run_workflow_pipeline(tmp_path, mode="candidate")
    assert cand_run["failed"] is False
    assert "Verify candidate artifact and OCI metadata (candidate mode)" in cand_run["executed_steps"]
    assert "Run Linux Docker acceptance matrix against exact candidate digest" in cand_run["executed_steps"]
    assert "Verify official legacy image upgrade to candidate digest" in cand_run["executed_steps"]
    assert "Record candidate summary and halt" in cand_run["executed_steps"]
    assert f"ghcr.io/{GH_REPO}@{cand_run['candidate_digest']}" in cand_run["summary"]

    for forbidden in (
        "Publish verified versioned tags to GHCR",
        "Verify published versioned GHCR tags",
        "Create release tag",
        "Create GitHub release",
        "Promote the verified digest to latest",
    ):
        assert forbidden not in cand_run["executed_steps"]

    assert not [i for i in cand_run["trace"] if i["tool"] == "docker" and "create" in i["args"]]
    assert not [i for i in cand_run["trace"] if i["tool"] == "git" and "push" in i["args"]]
    assert not [i for i in cand_run["trace"] if i["tool"] == "gh"]
    assert f"ghcr.io/{GH_REPO}:{CANDIDATE_TAG_VERSION}" not in cand_run["state"]["images"]
    assert f"ghcr.io/{GH_REPO}:latest" not in cand_run["state"]["images"]


@pytest.mark.parametrize(
    ("failure_mode", "overrides"),
    [
        ("bad_candidate_digest", {"expected_digest_override": "sha256:" + "0" * 64}),
        ("versioned_tag_publish_failed", {"state_overrides": {"create_failures_versioned": 3}}),
        (
            "verify_versioned_tags_failed",
            {
                "state_overrides": {
                    "post_create_overrides": {
                        f"ghcr.io/{GH_REPO}:{CANDIDATE_TAG_VERSION}": {
                            "raw": _manifest_bytes("wrong-tag-digest").decode("utf-8"),
                            "details": _details_payload(CANDIDATE_TAG_VERSION, "__COMMIT_SHA__"),
                        }
                    }
                }
            },
        ),
        ("git_push_failed", {"state_overrides": {"git_push_failures": 3}}),
        ("gh_release_failed", {"state_overrides": {"fail_gh_release_create": True}}),
    ],
)
def test_latest_promotion_impossible_if_earlier_publication_step_fails(
    tmp_path: Path, failure_mode: str, overrides: dict[str, Any]
):
    for step in _get_steps():
        assert not step.get("continue-on-error"), f"Step {step['name']} must not set continue-on-error"
        cond = str(step.get("if") or "")
        for forbidden_fn in ("always()", "failure()", "cancelled()"):
            assert forbidden_fn not in cond, f"Step {step['name']} must not use {forbidden_fn}"

    latest_step = _get_step("Promote the verified digest to latest")
    assert "success()" in latest_step["if"]
    assert "inputs.mode == 'promote'" in latest_step["if"]
    assert latest_step["env"]["IMAGE_DIGEST"] == "${{ inputs.candidate_digest }}"

    run_res = _run_workflow_pipeline(tmp_path / failure_mode, **overrides)
    assert run_res["failed"] is True
    assert "Promote the verified digest to latest" not in run_res["executed_steps"]
    assert f"ghcr.io/{GH_REPO}:latest" not in run_res["state"]["images"]
    latest_creates = [
        i for i in run_res["trace"]
        if i["tool"] == "docker" and i["args"][:3] == ["buildx", "imagetools", "create"] and any(":latest" in a for a in i["args"])
    ]
    assert latest_creates == []


def test_latest_promotion_step_fails_nonzero_after_three_failed_attempts(tmp_path: Path):
    run_res = _run_workflow_pipeline(
        tmp_path,
        state_overrides={"create_failures_latest": 3},
    )
    assert run_res["failed"] is True
    latest_step = _step_result(run_res, "Promote the verified digest to latest")
    assert latest_step["returncode"] != 0
    assert "after 3 attempts" in latest_step["stderr"]
    assert "Record promotion summary" not in run_res["executed_steps"]
    assert f"ghcr.io/{GH_REPO}:latest" not in run_res["state"]["images"]


def test_release_please_config_tracks_uv_lock_root_package():
    config = json.loads(RELEASE_PLEASE_CONFIG_PATH.read_text(encoding="utf-8"))
    extra_files = config["packages"]["."]["extra-files"]
    assert {
        "type": "toml",
        "path": "uv.lock",
        "jsonpath": "$.package[?(@.name.value=='plex-cast-bridge')].version",
    } in extra_files
