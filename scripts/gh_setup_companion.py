#!/usr/bin/env python3
"""Admin-machine companion for Google Home provisioning (local gcloud ADC).

Security model:
  - Loopback bind only
  - Cryptographically random pairing token required for all sensitive routes
  - Origin allowlist (dashboard origins) + Host header checks
  - No wildcard CORS; secrets never returned without pairing auth

  uv run python scripts/gh_setup_companion.py serve
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from plex_cast_bridge.gcp_provision import (  # noqa: E402
    build_provision_plan,
    describe_cost_notes,
    run_provision_step,
)
from plex_cast_bridge.gcp_provision.ops import (  # noqa: E402
    CLOUD_RUN_ENV,
    _read_env_value,
    auth_status,
    verify_project_resources,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18765
MAX_BODY_BYTES = 256_000
PAIRING_TTL_SEC = 2 * 60 * 60

ALLOWED_ORIGINS = {
    "http://127.0.0.1:8000",
    "http://localhost:8000",
}


class CompanionState:
    def __init__(self) -> None:
        self.pairing_token = secrets.token_urlsafe(32)
        self.created_at = time.time()
        self.expired = False

    def valid(self, provided: str | None) -> bool:
        if self.expired:
            return False
        if time.time() - self.created_at > PAIRING_TTL_SEC:
            self.expired = True
            return False
        if not provided:
            return False
        return secrets.compare_digest(provided, self.pairing_token)

    def expire(self) -> None:
        self.expired = True
        self.pairing_token = secrets.token_urlsafe(32)


STATE = CompanionState()


def _plan_from_body(body: dict[str, Any]):
    return build_provision_plan(
        project_id=str(body.get("project_id") or body.get("project") or "plex-cast-bridge"),
        create_project=bool(body.get("create_project")),
        region=str(body.get("region") or "us-central1"),
        recreate_canonical=bool(body.get("recreate_canonical")),
        billing_account=str(body.get("billing_account") or ""),
    )


def _is_loopback_host(host: str) -> bool:
    hostname = (host or "").split(":")[0].strip().lower()
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _cors_headers(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    origin = handler.headers.get("Origin") or ""
    headers = {
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Companion-Token",
        "Vary": "Origin",
    }
    if origin in ALLOWED_ORIGINS:
        headers["Access-Control-Allow-Origin"] = origin
    return headers


def _json_response(handler: BaseHTTPRequestHandler, code: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    for k, v in _cors_headers(handler).items():
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    if length > MAX_BODY_BYTES:
        raise ValueError("request body too large")
    raw = handler.rfile.read(length)
    return json.loads(raw.decode("utf-8") or "{}")


def _token_from(handler: BaseHTTPRequestHandler, body: dict[str, Any] | None = None) -> str | None:
    header = handler.headers.get("X-Companion-Token")
    if header:
        return header.strip()
    if body and isinstance(body.get("pairing_token"), str):
        return body["pairing_token"].strip()
    return None


def _require_pairing(handler: BaseHTTPRequestHandler, body: dict[str, Any] | None = None) -> bool:
    if not STATE.valid(_token_from(handler, body)):
        _json_response(handler, 401, {"error": "pairing_required", "detail": "Invalid or expired pairing token"})
        return False
    return True


def _origin_ok(handler: BaseHTTPRequestHandler) -> bool:
    origin = handler.headers.get("Origin")
    # Non-browser clients (curl) may omit Origin; still require Host loopback + pairing.
    if origin is None:
        return True
    return origin in ALLOWED_ORIGINS


def _host_ok(handler: BaseHTTPRequestHandler) -> bool:
    host = handler.headers.get("Host") or ""
    return _is_loopback_host(host)


class CompanionHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        msg = fmt % args
        # Never log Authorization / tokens / secrets.
        if "pairing" in msg.lower() or "secret" in msg.lower() or "passphrase" in msg.lower():
            msg = "[redacted]"
        sys.stderr.write("[gh-companion] " + msg + "\n")

    def _guard(self) -> bool:
        if not _host_ok(self):
            _json_response(self, 403, {"error": "forbidden_host"})
            return False
        if not _origin_ok(self):
            _json_response(self, 403, {"error": "forbidden_origin"})
            return False
        return True

    def do_OPTIONS(self) -> None:
        if not self._guard():
            return
        self.send_response(204)
        for k, v in _cors_headers(self).items():
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self) -> None:
        if not self._guard():
            return
        path = urlparse(self.path).path
        if path in {"/", "/health"}:
            _json_response(
                self,
                200,
                {
                    "status": "ok",
                    "service": "gh-setup-companion",
                    "pairing_required": True,
                    "expired": STATE.expired,
                },
            )
            return
        if path == "/pairing":
            # Intentionally no secrets — only confirms whether a token is still live.
            token = _token_from(self)
            _json_response(self, 200, {"paired": STATE.valid(token), "expired": STATE.expired})
            return
        if not _require_pairing(self):
            return
        if path == "/auth/status":
            _json_response(self, 200, auth_status())
            return
        if path == "/console-paste":
            client_id = _read_env_value(CLOUD_RUN_ENV, "GOOGLE_CLIENT_ID") or ""
            client_secret = _read_env_value(CLOUD_RUN_ENV, "GOOGLE_CLIENT_SECRET") or ""
            admin_passphrase = _read_env_value(CLOUD_RUN_ENV, "ADMIN_PASSPHRASE") or ""
            project_id = _read_env_value(CLOUD_RUN_ENV, "GCP_PROJECT_ID") or "plex-cast-bridge"
            base = _read_env_value(CLOUD_RUN_ENV, "CLOUD_RUN_URL") or ""
            _json_response(
                self,
                200,
                {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "admin_passphrase": admin_passphrase,
                    "authorization_url": f"{base.rstrip('/')}/oauth/authorize" if base else "",
                    "token_url": f"{base.rstrip('/')}/oauth/token" if base else "",
                    "fulfillment_url": f"{base.rstrip('/')}/fulfillment" if base else "",
                    "project_id": project_id,
                },
            )
            return
        if path.startswith("/projects/") and path.endswith("/verify"):
            project_id = path.split("/")[2]
            _json_response(self, 200, verify_project_resources(project_id))
            return
        _json_response(self, 404, {"error": "not_found"})

    def do_POST(self) -> None:
        if not self._guard():
            return
        try:
            body = _read_json(self)
        except ValueError as exc:
            _json_response(self, 400, {"error": "bad_request", "detail": str(exc)})
            return
        except Exception:
            _json_response(self, 400, {"error": "invalid_json"})
            return
        if not _require_pairing(self, body):
            return
        path = urlparse(self.path).path
        if path == "/session/finish":
            STATE.expire()
            _json_response(self, 200, {"status": "expired"})
            return
        if path == "/provision/preview":
            plan = _plan_from_body(body if isinstance(body, dict) else {})
            _json_response(
                self,
                200,
                {
                    "plan": plan.to_dict(),
                    "cost_notes": describe_cost_notes(),
                },
            )
            return
        if path == "/provision/step":
            if not body.get("confirm"):
                _json_response(
                    self,
                    400,
                    {"error": "confirmation_required", "detail": "Set confirm=true for infrastructure changes"},
                )
                return
            step = body.get("step") or ""
            plan = _plan_from_body(body if isinstance(body, dict) else {})
            result = run_provision_step(plan, step, dry_run=bool(body.get("dry_run")))
            _json_response(self, 200, result)
            return
        if path in {"/projects/create", "/attach"}:
            if not body.get("confirm"):
                _json_response(self, 400, {"error": "confirmation_required"})
                return
            plan = _plan_from_body(body if isinstance(body, dict) else {})
            step = "create_project" if path.endswith("create") else "attach_project"
            _json_response(self, 200, run_provision_step(plan, step, dry_run=False))
            return
        _json_response(self, 404, {"error": "not_found"})


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    if not _is_loopback_host(host):
        raise SystemExit(f"Refusing non-loopback bind host {host!r}; use 127.0.0.1")
    httpd = ThreadingHTTPServer((host, port), CompanionHandler)
    print(f"gh-setup-companion listening on http://{host}:{port}")
    print("Pairing token (enter in the dashboard wizard):")
    print(STATE.pairing_token)
    print(f"Token expires in {PAIRING_TTL_SEC // 60} minutes or when /session/finish is called.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down companion")
    finally:
        STATE.expire()
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Google Home setup companion")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve_p = sub.add_parser("serve", help="Run localhost companion HTTP server")
    serve_p.add_argument("--host", default=DEFAULT_HOST)
    serve_p.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    if args.cmd == "serve":
        serve(args.host, args.port)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
