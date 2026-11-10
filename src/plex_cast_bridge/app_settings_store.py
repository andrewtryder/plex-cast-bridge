"""JSON-backed non-secret app settings under DATA_DIR (UI-editable)."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from .catalog_store import DATA_DIR

log = logging.getLogger(__name__)

_LOCK = threading.RLock()
SETTINGS_FILENAME = "app_settings.json"
SCHEMA_VERSION = 1

EDITABLE_KEYS = (
    "default_cast_volume",
    "max_cast_volume",
    "night_cast_volume",
)


def settings_path() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / SETTINGS_FILENAME


def _clamp01(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, min(1.0, float(value)))


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    default_v = _clamp01(data.get("default_cast_volume"))
    max_v = _clamp01(data.get("max_cast_volume"))
    night_v = _clamp01(data.get("night_cast_volume"))
    if default_v is None and max_v is None and night_v is None and not data:
        return {"schema_version": SCHEMA_VERSION}
    if max_v is None:
        max_v = 0.85
    if default_v is None:
        default_v = 0.40
    if default_v > max_v:
        default_v = max_v
    if night_v is not None and night_v > max_v:
        night_v = max_v
    out: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "default_cast_volume": round(default_v, 4),
        "max_cast_volume": round(max_v, 4),
    }
    if night_v is not None:
        out["night_cast_volume"] = round(night_v, 4)
    return out


def load() -> dict[str, Any]:
    path = settings_path()
    with _LOCK:
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Could not read %s: %s", path, exc)
            return {}
        if not isinstance(raw, dict):
            return {}
        return _normalize(raw)


def save(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge patch into stored settings and write. Returns normalized public dict."""
    with _LOCK:
        current = {}
        path = settings_path()
        if path.exists():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(current, dict):
                    current = {}
            except Exception:
                current = {}
        merged = {**current}
        for key in EDITABLE_KEYS:
            if key in patch:
                merged[key] = patch[key]
        normalized = _normalize(merged)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(normalized, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return normalized


def public_dict(stored: dict[str, Any] | None = None) -> dict[str, Any]:
    data = stored if stored is not None else load()
    if not data:
        return {
            "schema_version": SCHEMA_VERSION,
            "default_cast_volume": None,
            "max_cast_volume": None,
            "night_cast_volume": None,
            "configured": False,
        }
    return {
        "schema_version": int(data.get("schema_version") or SCHEMA_VERSION),
        "default_cast_volume": data.get("default_cast_volume"),
        "max_cast_volume": data.get("max_cast_volume"),
        "night_cast_volume": data.get("night_cast_volume"),
        "configured": True,
    }


def apply_to_settings(settings: Any, stored: dict[str, Any] | None = None) -> Any:
    """Overlay file values onto a pydantic Settings instance (mutates in place)."""
    data = stored if stored is not None else load()
    if not data:
        return settings
    if data.get("default_cast_volume") is not None:
        settings.default_cast_volume = float(data["default_cast_volume"])
    if data.get("max_cast_volume") is not None:
        settings.max_cast_volume = float(data["max_cast_volume"])
    if "night_cast_volume" in data:
        night = data.get("night_cast_volume")
        settings.night_cast_volume = float(night) if night is not None else None
    return settings
