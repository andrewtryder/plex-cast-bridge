import argparse
import json
import os
import sys
from pathlib import Path

from .capture import capture, capture_cloud
from .compare import compare
from .constants import DEFAULT_STATE_DIR

DOC = '''Read-only release baseline and smoke checks for an existing installation.

Run on the Docker host (or inside the LXC) so the default API URL is local.
Reports and the HMAC key are kept outside the repository with private modes.
No playback or provisioning command is issued by this tool.
'''

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=DOC)
    sub = parser.add_subparsers(dest="command", required=True)
    cap = sub.add_parser("capture", help="Run GET-only API smoke checks and save a private baseline")
    cap.add_argument("--api-url", default="http://127.0.0.1:8000")
    cap.add_argument("--api-key", default=os.environ.get("API_KEY"), help="API key sent only as X-API-Key")
    cap.add_argument("--timeout", type=int, default=8)
    cap.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    cap.add_argument("--env-file", type=Path, default=Path("/opt/plex-cast-bridge/.env"))
    cap.add_argument("--credentials-file", type=Path, default=Path("/opt/plex-cast-bridge/gcp-credentials.json"))
    cap.add_argument("--compose-file", type=Path, default=Path("/opt/plex-cast-bridge/deploy/docker-compose.yml"))
    cap.add_argument("--container", default="plex-cast-bridge")
    cap.add_argument("--data-dir", type=Path, default=Path("/opt/plex-cast-bridge/data"))
    cap.add_argument("--backup-dir", type=Path, default=Path("/opt/plex-cast-bridge/backups"))
    cap.add_argument("--project", default="")
    cap.add_argument("--cloud-run-service", default="plex-google-home-fulfillment")
    cap.add_argument("--region", default="us-central1")
    cloud = sub.add_parser("capture-cloud", help="Capture Cloud Run metadata from a workstation with gcloud access")
    cloud.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    cloud.add_argument("--project", required=True)
    cloud.add_argument("--cloud-run-service", default="plex-google-home-fulfillment")
    cloud.add_argument("--region", default="us-central1")
    cmp = sub.add_parser("compare", help="Fail closed when protected identities differ")
    cmp.add_argument("before", type=Path)
    cmp.add_argument("after", type=Path)
    cmp.add_argument("--scope", choices=("all", "local", "cloud"), default="all",
                     help="all requires complete local and Cloud Run evidence")
    return parser



def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "capture":
        result = capture(args)
        print(json.dumps({"report_path": result["report_path"], "summary": {
            "api_http_statuses": {
                path: value.get("http_status")
                for path, value in result["report"]["api"].items()
                if isinstance(value, dict) and "http_status" in value
            },
            "configuration_drift": {
                "available": result["report"]["local"]["configuration_drift"]["available"],
                "release_preflight_warnings": result["report"]["local"]["configuration_drift"].get("report", {}).get("release_preflight_warnings", []),
            },
            "speaker_count": len(result["report"]["api"].get("speakers", [])),
            "google_home_linked": result["report"]["api"].get("google_home", {}).get("cloud_linked"),
            "local_plex_linked": result["report"]["api"].get("google_home", {}).get("local_plex_linked"),
            "saved_mix_count": len(result["report"]["api"].get("saved_mix_ids", [])),
            "docker": {k: (result["report"]["local"]["docker"] or {}).get(k) for k in (
                "image_id", "published_image_digest", "application_commit",
                "application_commit_hint_from_tag", "running", "health",
            )},
            "cloud_run": {k: result["report"]["cloud_run"].get(k) for k in ("project_id", "latest_ready_revision", "image", "service_account")},
        }}, indent=2, sort_keys=True))
        return 0
    if args.command == "capture-cloud":
        result = capture_cloud(args)
        cloud_run = result["report"]["cloud_run"]
        print(json.dumps({"report_path": result["report_path"], "summary": {
            k: cloud_run.get(k) for k in (
                "available", "project_id", "service", "region", "latest_ready_revision",
                "latest_created_revision", "image", "service_account", "url", "traffic",
            )
        }}, indent=2, sort_keys=True))
        return 0 if cloud_run.get("available") else 2
    ok, differences = compare(args.before, args.after, scope=args.scope)
    print(json.dumps({"safe": ok, "differences": differences}, indent=2, sort_keys=True))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
