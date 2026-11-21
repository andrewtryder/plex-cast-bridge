import json
import subprocess
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .constants import API_PATHS

def _run_json(argv: list[str], timeout: int = 25) -> Any | None:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        if proc.returncode != 0:
            return None
        value = json.loads(proc.stdout)
        return value if isinstance(value, (dict, list)) else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def _get_json(url: str, token: str | None, timeout: int) -> tuple[int | None, int, Any, str | None]:
    headers = {"Accept": "application/json"}
    if token:
        headers["X-API-Key"] = token
    started = time.monotonic()
    try:
        with urlopen(Request(url, headers=headers), timeout=timeout) as response:
            raw = response.read()
            status = response.status
        elapsed = round((time.monotonic() - started) * 1000)
        try:
            body = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = None
        return status, elapsed, body, None
    except HTTPError as exc:
        return exc.code, round((time.monotonic() - started) * 1000), None, "http_error"
    except (URLError, TimeoutError, OSError):
        return None, round((time.monotonic() - started) * 1000), None, "unreachable"


def _api_capture(base_url: str, token: str | None, timeout: int) -> dict[str, Any]:
    from ._facade import package

    results: dict[str, Any] = {}
    bodies: dict[str, Any] = {}
    for path in API_PATHS:
        status, elapsed, body, error = package()._get_json(base_url.rstrip("/") + path, token, timeout)
        results[path] = {"http_status": status, "elapsed_ms": elapsed, "error": error}
        if body is not None:
            bodies[path] = body

    diag = bodies.get("/api/diagnostics", {})
    if isinstance(diag, dict):
        # Keep diagnostic outcomes only; error text can contain deployment details.
        checks = diag.get("checks", [])
        if isinstance(checks, list):
            results["/api/diagnostics"]["checks"] = [
                {"id": item.get("id"), "status": item.get("status")}
                for item in checks if isinstance(item, dict)
            ]

    settings_body = bodies.get("/api/settings", {})
    if isinstance(settings_body, dict):
        library = settings_body.get("library") if isinstance(settings_body.get("library"), dict) else {}
        volumes = settings_body.get("volumes") if isinstance(settings_body.get("volumes"), dict) else {}
        results["plex"] = {k: library.get(k) for k in ("linked", "section", "tracks", "artists", "albums") if k in library}
        results["speaker_config"] = {k: volumes.get(k) for k in (
            "default_cast_volume", "max_cast_volume", "night_cast_volume",
        ) if k in volumes}

    # Playback is mutable; retain only current state/count metadata, no media names.
    status_body = bodies.get("/api/status", {})
    if isinstance(status_body, dict):
        results["playback"] = {
            str(name): {
                k: value.get(k) for k in ("state", "volume", "max_volume", "queue_length", "repeat")
                if k in value
            } | {"current_track_present": bool(value.get("current_track"))}
            for name, value in status_body.items() if isinstance(value, dict)
        }
    return results
