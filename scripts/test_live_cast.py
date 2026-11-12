#!/usr/bin/env python3
"""Exercise one explicitly authorized, individual Cast receiver via dashboard API."""

import argparse
import json
import logging
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from plex_cast_bridge.cast_manager import CastManager
from plex_cast_bridge.catalog_store import list_speaker_records
from plex_cast_bridge.config import Settings

log = logging.getLogger("test_live_cast")


class AcceptanceFailure(RuntimeError):
    """A requested device transition could not be verified."""


def dashboard_request(base_url: str, path: str, body: dict, timeout: float = 90) -> int:
    """Submit a dashboard API request without logging its private body."""
    payload = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("API_KEY")
    if api_key:
        headers["X-API-Key"] = api_key
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=payload,
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            response.read()
            return response.status
    except HTTPError as exc:
        # Do not print the response body: failures can echo private query text.
        raise AcceptanceFailure(f"Dashboard API returned HTTP {exc.code} for {path}") from None
    except (URLError, TimeoutError) as exc:
        raise AcceptanceFailure(
            f"Dashboard API request failed for {path} ({type(exc).__name__})"
        ) from None


def receiver_state(speaker) -> str:
    """Refresh and return the state from this exact Cast connection."""
    speaker.cast.media_controller.update_status()
    return str(getattr(speaker.cast.media_controller.status, "player_state", "UNKNOWN")).upper()


def wait_for_state(speaker, expected: set[str], timeout: float = 15, sleep=time.sleep) -> str:
    """Wait for a receiver state; never infer success from the HTTP response."""
    deadline = time.monotonic() + timeout
    observed = "UNKNOWN"
    while time.monotonic() < deadline:
        client = getattr(speaker.cast, "socket_client", None)
        if not getattr(client, "is_connected", False):
            return "DISCONNECTED"
        observed = receiver_state(speaker)
        if observed in expected:
            return observed
        sleep(0.5)
    return observed


def set_volume_and_verify(speaker, volume: float, timeout: float = 4, sleep=time.sleep) -> bool:
    """Set volume on the same live Cast connection and read it back."""
    try:
        client = getattr(speaker.cast, "socket_client", None)
        current = getattr(getattr(speaker.cast, "status", None), "volume_level", None)
        if not getattr(client, "is_connected", False) or current is None:
            return False
        target = float(volume)
        maximum = float(speaker.max_volume)
        if target < 0 or target > maximum:
            return False
        speaker.cast.set_volume(target)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not getattr(client, "is_connected", False):
                return False
            current = getattr(getattr(speaker.cast, "status", None), "volume_level", None)
            if current is not None and abs(float(current) - target) <= 0.02:
                return True
            sleep(0.1)
        return False
    except Exception:  # noqa: BLE001 - Cast library errors vary with receiver state.
        return False


def run_device_test(
    *,
    speaker,
    device_id: str,
    query: str,
    base_url: str,
    max_volume: float,
    request=dashboard_request,
    sleep=time.sleep,
    wait=wait_for_state,
) -> list[tuple]:
    """Run play/pause/resume/volume/stop and always attempt verified cleanup."""
    client = getattr(speaker.cast, "socket_client", None)
    original_volume = getattr(getattr(speaker.cast, "status", None), "volume_level", None)
    initial_state = receiver_state(speaker)
    if not getattr(client, "is_connected", False) or original_volume is None:
        raise AcceptanceFailure("Exact receiver connection or initial volume is unreadable")
    if initial_state not in {"IDLE", "STOPPED"}:
        raise AcceptanceFailure(f"Receiver is not confirmed idle (state={initial_state})")
    if getattr(speaker, "is_group", False):
        raise AcceptanceFailure("Cast Groups are excluded from this individual-speaker test")

    cap = min(float(max_volume), float(speaker.max_volume), 0.85)
    safe = min(float(original_volume), 0.15, cap)
    raised = min(safe + 0.05, cap)
    lowered = max(0.0, safe - 0.05)
    results = [("baseline", initial_state, round(float(original_volume), 3))]
    play_requested = False
    stopped = False
    failure = None

    def transport(action: str, **extra) -> int:
        return request(
            base_url, "/api/transport", {"action": action, "device_id": device_id, **extra}
        )

    try:
        if not set_volume_and_verify(speaker, safe):
            raise AcceptanceFailure("Could not verify safe starting volume")
        results.append(("safe_volume", round(safe, 3)))

        # Set before the request so a timeout after server-side execution still
        # enters the finally cleanup path.
        play_requested = True
        code = request(base_url, "/api/command", {"query": query, "device_id": device_id})
        state = wait(speaker, {"PLAYING"}, sleep=sleep)
        results.append(("play", code, state))
        if code != 200 or state != "PLAYING":
            raise AcceptanceFailure("Receiver did not confirm PLAYING")

        code = transport("pause")
        state = wait(speaker, {"PAUSED"}, sleep=sleep)
        results.append(("pause", code, state))
        if code != 200 or state != "PAUSED":
            raise AcceptanceFailure("Receiver did not confirm PAUSED")

        code = transport("resume")
        state = wait(speaker, {"PLAYING"}, sleep=sleep)
        results.append(("resume", code, state))
        if code != 200 or state != "PLAYING":
            raise AcceptanceFailure("Receiver did not confirm resumed PLAYING")

        for label, target in (("volume_up", raised), ("volume_down", lowered)):
            code = transport("volume", volume=target)
            if code != 200 or not set_volume_and_verify(speaker, target):
                raise AcceptanceFailure(f"Receiver did not confirm {label}")
            results.append((label, code, round(target, 3)))

        code = transport("stop")
        state = wait(speaker, {"IDLE", "STOPPED"}, sleep=sleep)
        results.append(("stop", code, state))
        stopped = code == 200 and state in {"IDLE", "STOPPED"}
        if not stopped:
            raise AcceptanceFailure("Stop was accepted but receiver did not confirm IDLE/STOPPED")
    except Exception as exc:  # noqa: BLE001 - cleanup must run for API and receiver failures.
        failure = (
            exc if isinstance(exc, AcceptanceFailure) else AcceptanceFailure(type(exc).__name__)
        )
    finally:
        if play_requested and not stopped:
            try:
                cleanup_code = transport("stop")
                cleanup_state = wait(speaker, {"IDLE", "STOPPED"}, timeout=8, sleep=sleep)
                results.append(("cleanup_stop", cleanup_code, cleanup_state))
                stopped = cleanup_code == 200 and cleanup_state in {"IDLE", "STOPPED"}
            except Exception as exc:  # noqa: BLE001 - preserve failure while making cleanup observable.
                results.append(("cleanup_stop", type(exc).__name__))

        connected = getattr(client, "is_connected", False)
        current_volume = getattr(getattr(speaker.cast, "status", None), "volume_level", None)
        if connected and current_volume is not None and (not play_requested or stopped):
            restored = set_volume_and_verify(speaker, float(original_volume))
            results.append(
                (
                    "restore_volume",
                    restored,
                    round(float(getattr(speaker.cast.status, "volume_level", 0)), 3),
                )
            )
        elif connected and current_volume is not None:
            results.append(
                ("restore_volume", "skipped_stop_not_confirmed", round(float(current_volume), 3))
            )
        else:
            results.append(("restore_volume", "skipped_connection_or_volume_unreadable"))
    if failure is not None:
        failure.results = results
        raise failure
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Explicitly authorized individual Cast acceptance test"
    )
    parser.add_argument(
        "--allow-disruptive",
        action="store_true",
        help="authorize one dashboard playback and transport test",
    )
    parser.add_argument(
        "--device-uuid", required=True, help="exact Cast UUID from the private baseline"
    )
    parser.add_argument(
        "--query", required=True, help="known library query; never printed or logged"
    )
    parser.add_argument(
        "--base-url", default="http://127.0.0.1:8000", help="local dashboard API URL"
    )
    args = parser.parse_args(argv)
    if not args.allow_disruptive:
        parser.error("physical playback requires --allow-disruptive")

    settings = Settings()
    records = [
        record
        for record in list_speaker_records()
        if str(record.get("cast_uuid") or record.get("uuid") or "").casefold()
        == args.device_uuid.casefold()
    ]
    if len(records) != 1:
        log.error("Cast UUID does not resolve to exactly one configured speaker")
        return 1
    record = records[0]
    device_id = record.get("google_home_device_id") or record.get("id")
    if record.get("kind") == "group" or record.get("is_group"):
        log.error("Refusing Cast Group; this helper only targets individual speakers")
        return 1

    cm = CastManager(default_volume=0.15, default_speaker_host=settings.default_speaker_host)
    try:
        speaker = cm.get_device(device_id)
        outcome = run_device_test(
            speaker=speaker,
            device_id=device_id,
            query=args.query,
            base_url=args.base_url,
            max_volume=settings.max_cast_volume,
        )
        print(json.dumps(outcome))
        return 0
    except AcceptanceFailure as exc:
        if hasattr(exc, "results"):
            print(json.dumps(exc.results))
        log.error("Live Cast acceptance incomplete: %s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001 - no payloads or query values in diagnostics.
        log.error("Live Cast acceptance failed (%s)", type(exc).__name__)
        return 1
    finally:
        cm.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    raise SystemExit(main())
