#!/usr/bin/env python3
"""Admin-machine companion for Google Home provisioning (uses local gcloud ADC).

Serve mode lets the Settings wizard call gcloud from the browser via localhost:
  uv run python scripts/gh_setup_companion.py serve
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
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
from plex_cast_bridge import google_home_setup as _gh_setup  # noqa: E402

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18765


def _json_response(handler: BaseHTTPRequestHandler, code: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw.decode("utf-8") or "{}")


class CompanionHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[gh-companion] " + (fmt % args) + "\n")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/", "/health"}:
            _json_response(self, 200, {"status": "ok", "service": "gh-setup-companion"})
            return
        if path == "/auth/status":
            _json_response(self, 200, auth_status())
            return
        if path == "/console-paste":
            # Local-only paste pack for Google Home Developer Console (includes secret from .env).
            client_id = _read_env_value(CLOUD_RUN_ENV, "GOOGLE_CLIENT_ID") or ""
            client_secret = _read_env_value(CLOUD_RUN_ENV, "GOOGLE_CLIENT_SECRET") or ""
            admin_passphrase = _read_env_value(CLOUD_RUN_ENV, "ADMIN_PASSPHRASE") or ""
            # Prefer live Cloud Run URL from gcloud when available.
            project_id = _read_env_value(CLOUD_RUN_ENV, "GCP_PROJECT_ID") or "plex-cast-bridge"
            url_proc = subprocess.run(
                [
                    "gcloud",
                    "run",
                    "services",
                    "describe",
                    "plex-google-home-fulfillment",
                    f"--project={project_id}",
                    "--region=us-central1",
                    "--format=value(status.url)",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            base = (url_proc.stdout or "").strip().rstrip("/")
            _json_response(
                self,
                200,
                {
                    "integration_name": _gh_setup.CANONICAL["integration_name"],
                    "device_type": _gh_setup.CANONICAL["device_type"],
                    "icon_path": str(ROOT / "assets" / "icon-144.png"),
                    "icon_url": "/assets/icon-144.png",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "admin_passphrase": admin_passphrase,
                    "authorization_url": f"{base}/oauth/authorize" if base else None,
                    "token_url": f"{base}/oauth/token" if base else None,
                    "fulfillment_url": f"{base}/fulfillment" if base else None,
                    "cloud_run_url": base or None,
                    "developer_console_url": "https://console.home.google.com/projects",
                    "steps": [
                        "Open your Home Console project → Add Cloud-to-cloud → Setup & configuration",
                        "Integration name: Plex Cast Bridge · Device type: TV or Audio Video Receiver",
                        "Download the 144×144 icon from this step and upload it in App branding",
                        "Leave Local fulfillment and App Flip off",
                        "Paste Client ID, Client secret, Authorization URL, Token URL, Fulfillment URL",
                        "Save, then return here and click Save & verify console",
                    ],
                    "link_steps": [
                        "On your phone, open the Google Home app (same Google account as the Developer Console)",
                        "Devices → Add / + → Works with Google",
                        "Search for [test] Plex Cast Bridge and tap it",
                        "Allow the login sheet — linking completes automatically (no passphrase by default)",
                        "Assign each speaker/device to a room in Google Home",
                        "Return here and click Confirm linked",
                    ],
                },
            )
            return
        if path.startswith("/projects/") and path.endswith("/verify"):
            project_id = path.split("/")[2]
            _json_response(self, 200, verify_project_resources(project_id))
            return
        _json_response(self, 404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = _read_json(self)
        except Exception as exc:
            _json_response(self, 400, {"error": f"invalid json: {exc}"})
            return

        if path == "/projects/create":
            project_id = (body.get("project_id") or "").strip()
            if not project_id:
                _json_response(self, 400, {"error": "project_id required"})
                return
            if not body.get("confirm_billable"):
                _json_response(self, 400, {"error": "confirm_billable=true required"})
                return
            plan = build_provision_plan(
                project_id=project_id,
                create_project=True,
                region=body.get("region") or "us-central1",
            )
            result = run_provision_step(plan, "create_project", dry_run=False)
            _json_response(self, 200 if result.get("status") != "failed" else 500, result)
            return

        if path == "/projects/attach":
            project_id = (body.get("project_id") or "").strip()
            if not project_id:
                _json_response(self, 400, {"error": "project_id required"})
                return
            # Idempotent: describe must succeed and be ACTIVE.
            desc = subprocess.run(
                ["gcloud", "projects", "describe", project_id, "--format=value(lifecycleState)"],
                capture_output=True,
                text=True,
                check=False,
            )
            state = (desc.stdout or "").strip()
            if desc.returncode != 0 or state != "ACTIVE":
                _json_response(
                    self,
                    404,
                    {
                        "step": "attach_project",
                        "status": "failed",
                        "project_id": project_id,
                        "detail": (desc.stderr or desc.stdout or "Project not found or not ACTIVE").strip()[:500],
                    },
                )
                return
            subprocess.run(
                ["gcloud", "config", "set", "project", project_id],
                capture_output=True,
                text=True,
                check=False,
            )
            _json_response(
                self,
                200,
                {
                    "step": "attach_project",
                    "status": "exists",
                    "project_id": project_id,
                    "lifecycleState": state,
                    "detail": f"Using existing ACTIVE project {project_id}",
                },
            )
            return

        if path == "/provision/step":
            project_id = (body.get("project_id") or "").strip()
            step = (body.get("step") or "").strip()
            if not project_id or not step:
                _json_response(self, 400, {"error": "project_id and step required"})
                return
            if not body.get("confirm_billable") and step not in {
                "confirm_billing",
                "mint_local_secrets",
            }:
                _json_response(self, 400, {"error": "confirm_billable=true required for this step"})
                return
            if step == "link_billing" and not body.get("confirm_billable"):
                _json_response(self, 400, {"error": "confirm_billable=true required to link billing"})
                return
            try:
                plan = build_provision_plan(
                    project_id=project_id,
                    create_project=False,
                    region=body.get("region") or "us-central1",
                )
                result = run_provision_step(plan, step, dry_run=bool(body.get("dry_run")))
                code = 200 if result.get("status") != "failed" else 500
                _json_response(self, code, {"result": result, "cost_notes": describe_cost_notes(plan)})
            except Exception as exc:
                _json_response(self, 500, {"error": str(exc), "result": {"step": step, "status": "failed", "detail": str(exc)}})
            return

        if path == "/provision/preview":
            project_id = (body.get("project_id") or "").strip()
            if not project_id:
                _json_response(self, 400, {"error": "project_id required"})
                return
            plan = build_provision_plan(
                project_id=project_id,
                create_project=bool(body.get("create_project", True)),
                region=body.get("region") or "us-central1",
            )
            _json_response(
                self,
                200,
                {
                    "plan": plan.to_dict(),
                    "cost_notes": describe_cost_notes(plan),
                    "steps": [run_provision_step(plan, s, dry_run=True) for s in plan.steps],
                },
            )
            return

        _json_response(self, 404, {"error": "not found"})


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> int:
    server = ThreadingHTTPServer((host, port), CompanionHandler)
    print(json.dumps({"status": "listening", "url": f"http://{host}:{port}", "auth": auth_status()}))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Google Home GCP provision companion")
    sub = parser.add_subparsers(dest="cmd")

    serve_p = sub.add_parser("serve", help="HTTP helper for the Settings wizard (localhost)")
    serve_p.add_argument("--host", default=DEFAULT_HOST)
    serve_p.add_argument("--port", type=int, default=DEFAULT_PORT)

    parser.add_argument("--project-id", default=None)
    parser.add_argument("--recreate-canonical", action="store_true")
    parser.add_argument("--create-project", action="store_true")
    parser.add_argument("--step", default=None)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-billable", action="store_true")
    args = parser.parse_args()

    if args.cmd == "serve":
        return serve(args.host, args.port)

    project_id = args.project_id or "plex-cast-home"
    plan = build_provision_plan(
        project_id=project_id,
        create_project=args.create_project,
        recreate_canonical=args.recreate_canonical,
    )
    print(json.dumps({"plan": plan.to_dict(), "cost_notes": describe_cost_notes(plan), "auth": auth_status()}, indent=2))

    if not args.execute:
        for step in plan.steps:
            print(json.dumps(run_provision_step(plan, step, dry_run=True), indent=2))
        print("Dry run only. Use `serve` for the browser wizard, or --execute --confirm-billable.")
        return 0

    if not args.confirm_billable:
        print("Refusing to execute without --confirm-billable", file=sys.stderr)
        return 2
    step = args.step or "enable_apis"
    result = run_provision_step(plan, step, dry_run=False)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
