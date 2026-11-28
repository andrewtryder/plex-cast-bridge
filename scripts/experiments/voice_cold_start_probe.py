#!/usr/bin/env python3
"""Observe and selectively probe Google Home to Plex cold-start behavior.

Read-only observation is the default. Playback requires --play-query or the
explicit one-shot --experimental-fallback option. This tool never reads Pub/Sub.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


@dataclass(frozen=True)
class Device:
    name: str
    device_id: str
    physical_name: str
    virtual_name: str
    availability: str = "unknown"
    cast_host: str = ""
    cast_port: int = 8009
    cast_uuid: str = ""
    model: str = ""


def normalize_speakers(payload: Any) -> list[Device]:
    rows = payload.get("speakers", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("speaker response must be a list")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("speaker_name") or row.get("name")
        device_id = row.get("google_home_device_id") or row.get("id")
        if not isinstance(name, str) or not isinstance(device_id, str):
            continue
        result.append(Device(
            name=name,
            device_id=device_id,
            physical_name=name,
            virtual_name=str(row.get("google_home_name") or row.get("name") or ""),
            availability=str(row.get("availability") or "unknown"),
            cast_host=str(row.get("host") or ""),
            cast_port=int(row.get("port") or 8009),
            cast_uuid=str(row.get("uuid") or ""),
            model=str(row.get("model") or ""),
        ))
    return result


def select_device(devices: Iterable[Device], selector: str) -> Device:
    matches = [d for d in devices if selector == d.device_id]
    if not matches:
        matches = [d for d in devices if selector.casefold() in {
            d.physical_name.casefold(), d.virtual_name.casefold()
        }]
    if len(matches) != 1:
        if not matches:
            raise ValueError(f"no speaker matches {selector!r}; select by exact device ID")
        raise ValueError(f"{selector!r} matches multiple speakers; select by exact device ID")
    return matches[0]


def parse_bridge_logs(lines: Iterable[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in lines:
        if "Google Home command received" in line or "Google Home command completed" in line:
            event = {key: _field(line, key) for key in ("action", "speaker", "device_id")}
            event["kind"] = "received" if " command received" in line else "completed"
            timestamp = re.match(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3})", line)
            if timestamp:
                parsed = datetime.strptime(timestamp.group(1), "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone.utc)
                event["timestamp"] = parsed.isoformat()
            events.append(event)
    return events


def _field(line: str, key: str) -> str | None:
    import re
    match = re.search(rf"\b{key}=(?:'([^']*)'|\"([^\"]*)\"|([^\s]+))", line)
    if not match:
        return None
    return next(value for value in match.groups() if value is not None)


def correlate(events: Iterable[dict[str, Any]], device_id: str) -> list[dict[str, Any]]:
    relevant = [e for e in events if e.get("device_id", "").strip("'\"") == device_id]
    return relevant


def playback_state(payload: Any, physical_name: str) -> str:
    if not isinstance(payload, dict):
        return "unknown"
    rows = [
        row for key, row in payload.items()
        if isinstance(row, dict)
        and (key == physical_name or row.get("speaker") == physical_name)
    ]
    if len(rows) != 1:
        return "unknown"  # An absent or ambiguous cache entry is not evidence of idle.
    row = rows[0]
    state = str(row.get("state") or "unknown").upper()
    return state if state in {"PLAYING", "PAUSED", "IDLE", "BUFFERING", "UNKNOWN"} else "unknown"


def bridge_row(payload: Any, physical_name: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    rows = [
        row for key, row in payload.items()
        if isinstance(row, dict)
        and (key == physical_name or row.get("speaker") == physical_name)
    ]
    return rows[0] if len(rows) == 1 else {}


DEFAULT_MEDIA_RECEIVER_APP_ID = "CC1AD845"  # Observed from PyChromecast 14.1.1 on this receiver.


def classify_cast_state(snapshot: dict[str, Any] | None, bridge: dict[str, Any]) -> str:
    """Classify conservatively; only positive Cast evidence can establish idle."""
    if not isinstance(snapshot, dict) or snapshot.get("connected") is not True:
        return "disconnected"

    app_id = snapshot.get("receiver_app_id")
    standby = snapshot.get("receiver_is_standby")
    media_id = snapshot.get("media_session_id")
    player_state = str(snapshot.get("player_state") or "UNKNOWN").upper()
    has_session = media_id not in (None, 0, "0", "")
    has_plex_content = bool(bridge.get("current_track")) or int(bridge.get("queue_length") or 0) > 0
    bridge_state = str(bridge.get("state") or "UNKNOWN").upper()

    if has_session:
        if player_state == "PAUSED":
            return "plex_paused" if has_plex_content else "other_media_paused"
        if player_state in {"PLAYING", "BUFFERING"}:
            return "plex_active" if has_plex_content else "other_media_active"
        return "indeterminate"

    if has_plex_content or bridge_state in {"PLAYING", "PAUSED", "BUFFERING"}:
        return "indeterminate"
    if app_id and app_id != DEFAULT_MEDIA_RECEIVER_APP_ID and standby is not True:
        return "other_app_active"
    if (
        app_id == DEFAULT_MEDIA_RECEIVER_APP_ID
        and standby is True
        and snapshot.get("receiver_status_received") is True
        and snapshot.get("media_status_received") is True
        and media_id in (None, 0, "0", "")
        and not bridge.get("current_track")
        and int(bridge.get("queue_length") or 0) == 0
    ):
        return "idle_no_media_session"
    return "indeterminate"


_CAST_DIAGNOSTIC = r'''import json,sys,time,threading
from uuid import UUID
from pychromecast import get_chromecast_from_host
host,port,cast_uuid,model,name=json.loads(sys.argv[1])
cast=None
try:
    cast=get_chromecast_from_host((host,int(port),UUID(cast_uuid),model or None,name),tries=1,retry_wait=1,timeout=6)
    cast.wait(timeout=8)
    connected=bool(cast.socket_client.is_connected)
    if connected:
        receiver_event=threading.Event()
        cast.socket_client.receiver_controller.update_status(callback_function=lambda *args: receiver_event.set())
        receiver_received=receiver_event.wait(4)
        media_event=threading.Event()
        cast.media_controller.update_status(callback_function=lambda *args: media_event.set())
        media_received=media_event.wait(4)
        receiver=cast.status
        media=cast.media_controller.status
        print(json.dumps({"connected":True,"receiver_status_received":receiver_received,
            "media_status_received":media_received,"receiver_app_id":receiver.app_id,
            "receiver_app_name":receiver.display_name,"receiver_is_standby":receiver.is_stand_by,
            "receiver_session_id":receiver.session_id,"media_session_id":media.media_session_id,
            "player_state":media.player_state,"content_type":media.content_type,
            "current_time":media.current_time,"sampled_at":time.time()}))
    else:
        print(json.dumps({"connected":False,"sampled_at":time.time()}))
except Exception as exc:
    print(json.dumps({"connected":False,"error_type":type(exc).__name__,"sampled_at":time.time()}))
finally:
    if cast is not None:
        cast.disconnect()
'''


def direct_cast_snapshot(device: Device, container: str, ssh_host: str,
                         ssh_user: str = "root") -> dict[str, Any] | None:
    """Use the bridge image's installed PyChromecast to query only receiver state."""
    if not (device.cast_host and device.cast_uuid and ssh_host):
        return None
    details = json.dumps([device.cast_host, device.cast_port, device.cast_uuid,
                          device.model, device.physical_name], separators=(",", ":"))
    remote_parts = ["docker", "exec", container, "python", "-c", _CAST_DIAGNOSTIC, details]
    remote_command = " ".join(shlex.quote(part) for part in remote_parts)
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-l", ssh_user,
             ssh_host, remote_command],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"connected": False, "error_type": "ssh_unavailable"}
    if result.returncode:
        return {"connected": False, "error_type": "cast_probe_failed"}
    for line in reversed(result.stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {"connected": False, "error_type": "cast_probe_no_result"}


class FallbackGate:
    """Allow one fallback after a fresh targeted resume and positive idle evidence."""

    def __init__(self, device_id: str, started_at: datetime):
        self.device_id = device_id
        self.started_at = started_at
        self.used = False
        self.processed: set[tuple[str, str, str]] = set()

    def should_trigger(self, events: Iterable[dict[str, Any]], cast_state: str) -> bool:
        if self.used:
            return False
        targeted = [
            event for event in events
            if event.get("device_id") == self.device_id
            and event.get("action") == "resume"
            and event.get("speaker")
            and event.get("timestamp")
        ]
        received = [event for event in targeted if event.get("kind") == "received"]
        completed = [event for event in targeted if event.get("kind") == "completed"]
        for done in completed:
            try:
                done_at = datetime.fromisoformat(done["timestamp"])
            except (TypeError, ValueError):
                continue
            for start in received:
                try:
                    received_at = datetime.fromisoformat(start["timestamp"])
                except (TypeError, ValueError):
                    continue
                if (
                    start.get("speaker") != done.get("speaker")
                    or received_at < self.started_at
                    or done_at < received_at
                    or done_at - received_at > timedelta(seconds=120)
                ):
                    continue
                key = (done["device_id"], done["speaker"], done["timestamp"])
                if key in self.processed:
                    continue
                self.processed.add(key)
                if cast_state == "idle_no_media_session":
                    self.used = True
                    return True
        return False


def request_playback_once(post_request, inspect_after_timeout):
    """Never retry an ambiguous timed-out play; inspect state once instead."""
    try:
        return {"result": "submitted", "response": post_request()}
    except (TimeoutError, socket.timeout) as exc:
        return {
            "result": "timed_out",
            "error_type": type(exc).__name__,
            "inspection": inspect_after_timeout(),
        }


def _request(base: str, path: str, body: dict | None = None, timeout: float = 30) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base.rstrip("/") + path, data=data,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def docker_logs(container: str, since: str, ssh_host: str | None = None,
                ssh_user: str = "root") -> list[str]:
    command = ["docker", "logs", "--since", since, container]
    if ssh_host:
        remote_command = " ".join(shlex.quote(part) for part in command)
        command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                   "-l", ssh_user, ssh_host, remote_command]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode:
        return []
    return (result.stdout + result.stderr).splitlines()


def cloud_logs(service: str, since: str) -> list[str]:
    try:
        result = subprocess.run([
            "gcloud", "logging", "read",
            f'resource.type="cloud_run_revision" AND resource.labels.service_name="{service}" AND timestamp>="{since}"',
            "--limit=100", "--format=value(textPayload)",
        ], capture_output=True, text=True, timeout=12)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return result.stdout.splitlines() if result.returncode == 0 else []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-url", default="http://127.0.0.1:8000")
    parser.add_argument("--docker-container", default="plex-cast-bridge")
    parser.add_argument("--ssh-host", help="Read Docker logs on a remote host over SSH")
    parser.add_argument("--ssh-user", default="root")
    parser.add_argument("--cloud-run-service", default="plex-google-home-fulfillment")
    parser.add_argument("--target-speaker", help="Exact speaker name or virtual device ID")
    parser.add_argument("--music-query", default="Play songs by DJ Shadow")
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--http-timeout", type=float, default=30,
                        help="Seconds to wait for each local bridge API request")
    parser.add_argument("--cast-refresh", type=float, default=10,
                        help="Seconds between read-only receiver status queries")
    parser.add_argument("--play-query", action="store_true", help="Start the configured Plex query")
    parser.add_argument("--experimental-fallback", action="store_true",
                        help="One-shot play query after targeted resume completes and state is idle")
    parser.add_argument("--cloud-logs", action="store_true", help="Read existing Cloud Run logs with gcloud")
    args = parser.parse_args(argv)
    try:
        devices = normalize_speakers(_request(args.bridge_url, "/api/speakers", timeout=args.http_timeout))
    except (OSError, urllib.error.URLError, ValueError, TimeoutError) as exc:
        print(f"Bridge unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if not devices:
        print("No speaker records returned.")
        return 1
    print("Available physical / virtual speakers:")
    for device in devices:
        print(f"  {device.physical_name} / {device.virtual_name}  id={device.device_id}  availability={device.availability}")
    if not args.target_speaker:
        return 0
    try:
        device = select_device(devices, args.target_speaker)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    def inspect_current_state() -> dict[str, Any]:
        try:
            status_data = _request(args.bridge_url, "/api/status", timeout=args.http_timeout)
        except (OSError, urllib.error.URLError, ValueError, TimeoutError) as exc:
            return {"bridge_error_type": type(exc).__name__, "cast_state": "indeterminate"}
        row = bridge_row(status_data, device.physical_name)
        snapshot = direct_cast_snapshot(
            device, args.docker_container, args.ssh_host, args.ssh_user
        ) if args.ssh_host else None
        return {
            "bridge_state": row.get("state", "UNKNOWN"),
            "current_track_present": bool(row.get("current_track")),
            "queue_length": row.get("queue_length", 0),
            "cast_state": classify_cast_state(snapshot, row),
            "cast": snapshot,
        }

    def post_playback_once() -> dict[str, Any]:
        return request_playback_once(
            lambda: _request(args.bridge_url, "/api/command", {
                "query": args.music_query,
                "speaker": device.physical_name,
                "device_id": device.device_id,
            }, timeout=args.http_timeout),
            inspect_current_state,
        )

    if args.play_query:
        outcome = post_playback_once()
        print("Playback request outcome:", json.dumps(outcome, sort_keys=True))
        return 0

    started_at = datetime.now(timezone.utc)
    started = started_at.isoformat(timespec="seconds")
    fallback_armed = False
    if args.experimental_fallback:
        if not args.ssh_host:
            print("--experimental-fallback requires --ssh-host for direct Cast verification.", file=sys.stderr)
            return 2
        baseline = inspect_current_state()
        print("Initial Cast evidence:", json.dumps(baseline, sort_keys=True))
        fallback_armed = baseline.get("cast_state") == "idle_no_media_session"
        if fallback_armed:
            print("Monitoring active; fallback is armed for a fresh targeted resume.")
        else:
            print("Monitoring active; fallback is disarmed because the baseline is not confirmed idle.")

    deadline = time.monotonic() + max(0, args.timeout)
    seen: set[tuple[str | None, str | None, str | None, str | None, str | None]] = set()
    cloud_seen: set[str] = set()
    fallback_gate = FallbackGate(device.device_id, started_at)
    fallback_submitted = False
    last_cast_check = 0.0
    last_cast_snapshot: dict[str, Any] | None = None
    prior_cast_position: float | None = None
    while time.monotonic() < deadline:
        try:
            state_data = _request(args.bridge_url, "/api/status", timeout=args.http_timeout)
            row = bridge_row(state_data, device.physical_name)
            state = playback_state(state_data, device.physical_name)
        except (OSError, urllib.error.URLError, ValueError, TimeoutError):
            row = {}
            state = "unknown"
        logs = docker_logs(args.docker_container, started, args.ssh_host, args.ssh_user)
        events = correlate(parse_bridge_logs(logs), device.device_id)
        new_events = []
        for event in events:
            signature = (event.get("kind"), event.get("action"), event.get("speaker"),
                         event.get("device_id"), event.get("timestamp"))
            if signature not in seen:
                seen.add(signature)
                new_events.append(event)
                print("Bridge command:", json.dumps(event, sort_keys=True))
        if args.cloud_logs:
            for line in cloud_logs(args.cloud_run_service, started):
                if any(marker in line for marker in ("EXECUTE", "Publishing execute", "requestId")):
                    if line not in cloud_seen:
                        cloud_seen.add(line)
                        print("Cloud Run:", line[:1200])

        now_mono = time.monotonic()
        new_resume_completion = any(
            event.get("kind") == "completed"
            and event.get("action") == "resume"
            and event.get("device_id") == device.device_id
            for event in new_events
        )
        if args.ssh_host and (last_cast_snapshot is None or now_mono - last_cast_check >= args.cast_refresh):
            last_cast_snapshot = direct_cast_snapshot(
                device, args.docker_container, args.ssh_host, args.ssh_user
            )
            last_cast_check = time.monotonic()
        cast_state = classify_cast_state(last_cast_snapshot, row)
        print(f"Playback state: {state}; Cast classification: {cast_state}")
        if isinstance(last_cast_snapshot, dict):
            position = last_cast_snapshot.get("current_time")
            if isinstance(position, (int, float)):
                if prior_cast_position is not None and position > prior_cast_position + 0.25:
                    print(f"Cast playback position advanced: {prior_cast_position:.2f}s -> {position:.2f}s")
                prior_cast_position = position

        if fallback_armed and not fallback_submitted and new_resume_completion:
            # Query again after completion: bridge UNKNOWN is acceptable only when
            # receiver status positively proves standby with no media session.
            last_cast_snapshot = direct_cast_snapshot(
                device, args.docker_container, args.ssh_host, args.ssh_user
            )
            last_cast_check = time.monotonic()
            cast_state = classify_cast_state(last_cast_snapshot, row)
            print("Post-resume Cast evidence:", json.dumps({
                "classification": cast_state,
                "receiver_app_id": (last_cast_snapshot or {}).get("receiver_app_id"),
                "receiver_app_name": (last_cast_snapshot or {}).get("receiver_app_name"),
                "receiver_is_standby": (last_cast_snapshot or {}).get("receiver_is_standby"),
                "media_session_id": (last_cast_snapshot or {}).get("media_session_id"),
                "player_state": (last_cast_snapshot or {}).get("player_state"),
                "bridge_current_track": bool(row.get("current_track")),
                "bridge_queue_length": row.get("queue_length", 0),
            }, sort_keys=True))
            if fallback_gate.should_trigger(events, cast_state):
                fallback_submitted = True
                outcome = post_playback_once()
                print("One-shot fallback outcome:", json.dumps(outcome, sort_keys=True))
        time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
