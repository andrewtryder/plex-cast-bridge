#!/usr/bin/env python3
"""First-run setup validation for Plex Cast Bridge.

Checks environment, Plex reachability, local API auth, and optional Pub/Sub
credentials. Exit code 0 when all required checks pass.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def _env(name: str, file_vals: dict[str, str]) -> str:
    return (os.environ.get(name) or file_vals.get(name) or "").strip()


def _ok(msg: str) -> None:
    print(f"  OK  {msg}")


def _fail(msg: str) -> None:
    print(f" FAIL {msg}")


def _warn(msg: str) -> None:
    print(f" WARN {msg}")


def check_env(file_vals: dict[str, str]) -> bool:
    print("Environment")
    ok = True
    plex_url = _env("PLEX_URL", file_vals)
    plex_token = _env("PLEX_TOKEN", file_vals)
    if plex_url:
        _ok(f"PLEX_URL={plex_url}")
    else:
        _fail("PLEX_URL is missing")
        ok = False
    if plex_token:
        _ok("PLEX_TOKEN is set")
    else:
        _fail("PLEX_TOKEN is missing")
        ok = False

    local_key = _env("LOCAL_API_KEY", file_vals)
    allow_unauth = _env("ALLOW_UNAUTHENTICATED_LOCAL_API", file_vals).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if local_key:
        _ok("LOCAL_API_KEY is set")
    elif allow_unauth:
        _warn("LOCAL_API_KEY unset; ALLOW_UNAUTHENTICATED_LOCAL_API=true (trusted LAN only)")
    else:
        _fail(
            "LOCAL_API_KEY is required unless ALLOW_UNAUTHENTICATED_LOCAL_API=true "
            "(generate with: openssl rand -hex 32)"
        )
        ok = False

    if _env("GEMINI_API_KEY", file_vals):
        _ok("GEMINI_API_KEY is set (Smart DJ)")
    else:
        _warn("GEMINI_API_KEY unset — artist-only playback works; theme mixes need Gemini")
    return ok


def check_plex(file_vals: dict[str, str]) -> bool:
    print("Plex connectivity")
    plex_url = _env("PLEX_URL", file_vals).rstrip("/")
    plex_token = _env("PLEX_TOKEN", file_vals)
    section = _env("PLEX_MUSIC_SECTION", file_vals) or "Music"
    if not plex_url or not plex_token:
        _fail("Skipping Plex probe (missing URL or token)")
        return False

    identity = f"{plex_url}/identity"
    try:
        req = Request(identity, headers={"X-Plex-Token": plex_token, "Accept": "application/json"})
        with urlopen(req, timeout=8) as resp:
            if resp.status >= 400:
                _fail(f"Plex identity returned HTTP {resp.status}")
                return False
        _ok(f"Reached Plex at {plex_url}")
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        _fail(f"Cannot reach Plex at {plex_url}: {exc}")
        return False

    # Best-effort library section hint (non-fatal if XML-only server)
    try:
        lib_url = f"{plex_url}/library/sections?X-Plex-Token={plex_token}"
        req = Request(lib_url, headers={"Accept": "application/json", "X-Plex-Token": plex_token})
        with urlopen(req, timeout=8) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        if section.casefold() in body.casefold():
            _ok(f"Music section '{section}' appears in library sections")
        else:
            _warn(f"Could not confirm music section '{section}' in /library/sections response")
    except Exception as exc:
        _warn(f"Could not list library sections: {exc}")
    return True


def check_pubsub(file_vals: dict[str, str], creds_path: Path) -> bool:
    print("Pub/Sub (optional for dashboard-only installs)")
    enabled = _env("ENABLE_PUBSUB_LISTENER", file_vals).lower() in {"1", "true", "yes", "on"}
    if not enabled:
        _ok("ENABLE_PUBSUB_LISTENER=false — skipping credential checks")
        return True

    ok = True
    if not _env("GCP_PROJECT_ID", file_vals):
        _fail("ENABLE_PUBSUB_LISTENER=true but GCP_PROJECT_ID is missing")
        ok = False
    else:
        _ok(f"GCP_PROJECT_ID={_env('GCP_PROJECT_ID', file_vals)}")
    if not _env("GCP_PUBSUB_SUBSCRIPTION_ID", file_vals):
        _fail("ENABLE_PUBSUB_LISTENER=true but GCP_PUBSUB_SUBSCRIPTION_ID is missing")
        ok = False
    else:
        _ok(f"GCP_PUBSUB_SUBSCRIPTION_ID={_env('GCP_PUBSUB_SUBSCRIPTION_ID', file_vals)}")

    if not creds_path.exists():
        _fail(f"Missing credentials file: {creds_path}")
        ok = False
    else:
        try:
            data = json.loads(creds_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not data.get("type"):
                _fail(f"{creds_path} is not a valid service-account JSON")
                ok = False
            else:
                _ok(f"Parsed service account JSON ({data.get('client_email', 'no client_email')})")
        except json.JSONDecodeError as exc:
            _fail(f"{creds_path} is not valid JSON: {exc}")
            ok = False
    return ok


def check_docker(required: bool) -> bool:
    print("Docker")
    docker = shutil.which("docker")
    if docker:
        _ok(f"docker found at {docker}")
        return True
    if required:
        _fail("docker not found on PATH")
        return False
    _warn("docker not found on PATH (optional)")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Plex Cast Bridge setup")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=PROJECT_ROOT / ".env",
        help="Path to .env (default: project .env)",
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=PROJECT_ROOT / "gcp-credentials.json",
        help="Path to Pub/Sub subscriber service-account JSON",
    )
    parser.add_argument(
        "--require-docker",
        action="store_true",
        help="Fail if docker is not available",
    )
    parser.add_argument(
        "--skip-plex",
        action="store_true",
        help="Skip live Plex connectivity probe",
    )
    args = parser.parse_args()

    print("Plex Cast Bridge preflight")
    print(f"Project root: {PROJECT_ROOT}")
    if not args.env_file.exists():
        print(f" FAIL .env not found at {args.env_file} (copy from .env.example)")
        return 1

    file_vals = _load_dotenv(args.env_file)
    ok = True
    ok = check_env(file_vals) and ok
    if not args.skip_plex:
        ok = check_plex(file_vals) and ok
    else:
        print("Plex connectivity")
        _warn("Skipped (--skip-plex)")
    ok = check_pubsub(file_vals, args.credentials) and ok
    ok = check_docker(args.require_docker) and ok

    print()
    if ok:
        print("Preflight passed.")
        return 0
    print("Preflight failed. Fix the FAIL items above, then re-run.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
