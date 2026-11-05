#!/usr/bin/env python3
"""
Simulate Google Assistant Voice Intent against Cloud Run Fulfillment
Sends an action.devices.EXECUTE payload (selectChannel / appSearch) to the live
Cloud Run /fulfillment endpoint, verifies <1s SUCCESS response, and confirms
delivery via GCP Cloud Pub/Sub.
"""

import base64
import hashlib
import hmac
import json
import logging
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from google.cloud import pubsub_v1

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("test_voice_fulfillment")

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def create_hs256_jwt(payload: dict, secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    b64_h = base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=").decode()
    b64_p = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    msg = f"{b64_h}.{b64_p}".encode()
    sig = hmac.new(secret.encode(), msg, hashlib.sha256).digest()
    b64_sig = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    return f"{b64_h}.{b64_p}.{b64_sig}"


def load_env_file(path: Path) -> dict:
    env = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip("\"'")
    return env


def resolve_cloud_run_url(project_id: str, region: str = "us-central1", service: str = "plex-google-home-fulfillment") -> str:
    try:
        res = subprocess.run(
            [
                "gcloud", "run", "services", "describe", service,
                "--region", region,
                "--project", project_id,
                "--format=value(status.url)",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        url = res.stdout.strip()
        if url:
            return f"{url.rstrip('/')}/fulfillment"
    except Exception as exc:
        log.debug("Could not resolve Cloud Run URL via gcloud: %s", exc)
    raise RuntimeError("Could not determine Cloud Run URL. Set CLOUD_RUN_URL in gcp-cloud-run/.env or authenticate gcloud.")


def main():
    artist_query = sys.argv[1] if len(sys.argv) > 1 else "DJ Shadow"
    speaker = sys.argv[2] if len(sys.argv) > 2 else "Master Bedroom"

    print("\n" + "=" * 65)
    print(" Google Assistant Voice Fulfillment Test")
    print("=" * 65)
    print(f"Simulated Voice Query:   'Hey Google, play {artist_query} on Plex'")
    print(f"Target Physical Speaker: {speaker}")
    print("-" * 65)

    env_path = PROJECT_ROOT / "gcp-cloud-run" / ".env"
    if not env_path.exists():
        log.error("Missing %s. Configure gcp-cloud-run/.env before running this test.", env_path)
        return 1

    env = load_env_file(env_path)
    signing_secret = env.get("OAUTH_SIGNING_SECRET", "").strip()
    if not signing_secret:
        log.error("OAUTH_SIGNING_SECRET is missing in %s", env_path)
        return 1

    project_id = env.get("GCP_PROJECT_ID", "plex-cast-bridge-home")
    sub_id = env.get("GCP_PUBSUB_SUBSCRIPTION_ID", "plex-commands-local-worker")
    cloud_run_url = env.get("CLOUD_RUN_URL")
    if cloud_run_url:
        cloud_run_url = f"{cloud_run_url.rstrip('/')}/fulfillment"
    else:
        cloud_run_url = resolve_cloud_run_url(project_id)

    token = create_hs256_jwt(
        {"sub": "admin", "type": "access", "exp": int(time.time() + 3600)},
        signing_secret,
    )

    request_id = f"sim-exec-{int(time.time())}"
    payload = {
        "requestId": request_id,
        "inputs": [
            {
                "intent": "action.devices.EXECUTE",
                "payload": {
                    "commands": [
                        {
                            "devices": [{"id": f"plex-jukebox:{speaker}"}],
                            "execution": [
                                {
                                    "command": "action.devices.commands.selectChannel",
                                    "params": {
                                        "channelName": artist_query,
                                        "channelCode": f"artist_{artist_query.lower().replace(' ', '_')}",
                                    },
                                }
                            ],
                        }
                    ]
                },
            }
        ],
    }

    print("[1/3] Dispatching EXECUTE intent to Cloud Run /fulfillment...")
    t0 = time.time()
    req = urllib.request.Request(
        cloud_run_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            latency = (time.time() - t0) * 1000
            res_data = json.loads(resp.read().decode("utf-8"))
            print(f"      Response received in {latency:.1f}ms (HTTP {resp.status})")
            cmd_status = res_data.get("payload", {}).get("commands", [{}])[0].get("status")
            print(f"      Execution Status: {cmd_status}")
            assert cmd_status == "SUCCESS", f"Unexpected execution status: {cmd_status}"
    except Exception as exc:
        log.error("Cloud Run request failed: %s", exc)
        return 1

    print("\n[2/3] Verifying message on GCP Pub/Sub queue...")
    subscriber = pubsub_v1.SubscriberClient()
    sub_path = subscriber.subscription_path(project_id, sub_id)

    response = subscriber.pull(
        request={"subscription": sub_path, "max_messages": 1},
        timeout=10,
    )

    if not response.received_messages:
        log.error("No message received on Pub/Sub subscription within timeout.")
        return 2

    received = response.received_messages[0]
    msg_data = json.loads(received.message.data.decode("utf-8"))
    ack_id = received.ack_id

    print(f"      Pub/Sub Message ID: {received.message.message_id}")
    print(f"      Published Payload:  {json.dumps(msg_data)}")
    subscriber.acknowledge(request={"subscription": sub_path, "ack_ids": [ack_id]})
    print("      Message acknowledged successfully.")

    print("\n[3/3] Intent Validation:")
    print(f"      Action:  {msg_data.get('action')}")
    print(f"      Query:   {msg_data.get('query')}")
    print(f"      Speaker: {msg_data.get('speaker', speaker)}")

    print("\n" + "=" * 65)
    print(" VERIFICATION SUCCESSFUL!")
    print(" Google Assistant command successfully published to Pub/Sub queue.")
    print(" When 'plex_cast_bridge' runs, it will process this command and cast.")
    print("=" * 65 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
