#!/usr/bin/env python3
"""Inspect configuration provenance without starting the bridge or changing state."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from .config import Settings
try:
    from .app_settings_store import _normalize
except ModuleNotFoundError as exc:
    if exc.name != "plex_cast_bridge.app_settings_store":
        raise
    # Older releases do not support the persisted app-settings overlay. Do
    # not guess its semantics if such a file is nevertheless present.
    _normalize = None
from . import catalog_store

ROOT = catalog_store.PROJECT_ROOT

_TRACKED_JSON = (
    "plex_connection.json",
    "app_settings.json",
    "catalog.json",
    "google_home_sync.json",
    "google_home_setup.json",
)


def read_json(path: Path) -> tuple[dict, str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return (value, "readable") if isinstance(value, dict) else ({}, "invalid")
    except FileNotFoundError:
        return {}, "absent"
    except (OSError, ValueError):
        return {}, "unreadable or invalid"


def _load_persistent_files(data_dir: Path) -> tuple[dict[str, str], dict[str, dict]]:
    files: dict[str, str] = {}
    states: dict[str, dict] = {}
    for name in _TRACKED_JSON:
        states[name], files[name] = read_json(data_dir / name)
    files["music_index.sqlite3"] = (
        "present (not opened)" if (data_dir / "music_index.sqlite3").exists() else "absent"
    )
    return files, states


def _applied_app_settings(app: dict, files: dict[str, str]) -> dict:
    try:
        if app and _normalize is None:
            raise ValueError("app settings need review on this older image")
        return _normalize(app) if app and _normalize is not None else {}
    except (TypeError, ValueError):
        files["app_settings.json"] = "unreadable or invalid"
        return {}


def env_candidates(name: str, environ: dict[str, str], dotenv: dict) -> list[tuple[str, Any]]:
    key = name.upper()
    return [("process environment", environ.get(key)), (".env", dotenv.get(key))]


def env_source(name: str, environ: dict[str, str], dotenv: dict) -> str:
    return next((source for source, value in env_candidates(name, environ, dotenv) if value is not None), "default")


def make_setting_entry(
    name: str,
    candidates: list[tuple[str, Any]],
    effective: str,
    *,
    note: str | None = None,
    conflict: bool = False,
    classification: str = "ok",
) -> dict[str, Any]:
    present = [(source, value) for source, value in candidates if value is not None and value != ""]
    entry: dict[str, Any] = {
        "source": effective,
        "present_sources": [source for source, _ in present],
        "conflict": conflict,
        "classification": classification,
    }
    if note:
        entry["note"] = note
    return entry


def _normalize_numeric_candidates(candidates: list[tuple[str, Any]]) -> None:
    for i, (source, value) in enumerate(candidates):
        try:
            candidates[i] = (source, float(value)) if value is not None and value != "" else (source, value)
        except (TypeError, ValueError):
            pass


def classify_volume_setting(
    name: str,
    app: dict,
    applied_app: dict,
    environ: dict[str, str],
    dotenv: dict,
) -> dict[str, Any]:
    candidates = env_candidates(name, environ, dotenv)
    source = env_source(name, environ, dotenv)
    conflict = False
    classification = "ok"
    if applied_app.get(name) is not None:
        source = "app_settings.json" if name in app else "app_settings.json (normalized default)"
    if name in app and app[name] is not None:
        candidates.insert(0, ("app_settings.json", app[name]))
        env_value = next((v for _, v in env_candidates(name, environ, dotenv) if v is not None), None)
        try:
            conflict = env_value not in (None, "") and float(app[name]) != float(env_value)
        except (TypeError, ValueError):
            classification = "unknown"
        if conflict:
            classification = "override"
    _normalize_numeric_candidates(candidates)
    return make_setting_entry(name, candidates, source, conflict=conflict, classification=classification)


def classify_gateway_setting(
    name: str,
    sync: dict,
    setup: dict,
    environ: dict[str, str],
    dotenv: dict,
    gateway_pair: bool,
) -> dict[str, Any]:
    candidates = env_candidates(name, environ, dotenv)
    candidates.insert(0, ("google_home_sync.json", sync.get(name)))
    if name == "cloud_gateway_url":
        candidates.append(("google_home_setup.json (wizard record)", setup.get("cloud_run_url")))
    source = (
        (env_source(name, environ, dotenv) + " (copied to sync file at startup)")
        if gateway_pair
        else ("google_home_sync.json" if sync.get(name) else "not configured")
    )
    env_value = next((v for _, v in env_candidates(name, environ, dotenv) if v is not None), None)
    saved = sync.get(name)
    conflict = False
    classification = "ok"
    if env_value not in (None, "") and saved not in (None, ""):
        normalize = (lambda x: str(x).rstrip("/")) if name == "cloud_gateway_url" else str
        conflict = normalize(env_value) != normalize(saved)
    if conflict:
        classification = "release_preflight_warning" if gateway_pair else "informational"
    note = (
        "Both environment values trigger a sync-file overwrite at startup."
        if gateway_pair
        else "A partial environment pair does not configure the sync file."
    )
    return make_setting_entry(name, candidates, source, note=note, conflict=conflict, classification=classification)


def classify_plex_setting(
    name: str,
    plex: dict,
    environ: dict[str, str],
    dotenv: dict,
) -> dict[str, Any]:
    candidates = env_candidates(name, environ, dotenv)
    source = env_source(name, environ, dotenv)
    note = None
    field = {"plex_url": "server_url", "plex_music_section": "music_section", "plex_token": "jwt_token"}[name]
    if plex.get("mode") == "jwt" and plex.get("jwt_token"):
        candidates.insert(0, ("plex_connection.json (JWT)", plex.get(field)))
        source = "plex_connection.json (JWT; environment fallback if connection fails)"
        if name == "plex_token":
            note = "JWT and legacy server tokens may differ by design."
    return make_setting_entry(name, candidates, source, note=note)


def build_model_settings(
    environ: dict[str, str],
    dotenv: dict,
    *,
    app: dict,
    applied_app: dict,
    sync: dict,
    setup: dict,
    plex: dict,
) -> dict[str, dict]:
    gateway_pair = all((environ.get(k) if k in environ else dotenv.get(k)) for k in ("CLOUD_GATEWAY_URL", "CATALOG_WRITE_KEY"))
    entries: dict[str, dict] = {}
    for name in sorted(set(Settings.model_fields) | {"cloud_gateway_url", "catalog_write_key"}):
        if name in ("default_cast_volume", "max_cast_volume", "night_cast_volume"):
            entries[name] = classify_volume_setting(name, app, applied_app, environ, dotenv)
        elif name in ("cloud_gateway_url", "catalog_write_key"):
            entries[name] = classify_gateway_setting(name, sync, setup, environ, dotenv, gateway_pair)
        elif name in ("plex_url", "plex_music_section", "plex_token"):
            entries[name] = classify_plex_setting(name, plex, environ, dotenv)
        else:
            candidates = env_candidates(name, environ, dotenv)
            entries[name] = make_setting_entry(name, candidates, env_source(name, environ, dotenv))
    return entries


def build_process_path_settings(
    environ: dict[str, str],
    dotenv: dict,
    setup: dict,
) -> dict[str, dict]:
    entries: dict[str, dict] = {}
    for name in ("DATA_DIR", "CATALOG_PATH", "GOOGLE_APPLICATION_CREDENTIALS", "PORT", "PCB_DISABLE_BACKGROUND_DISCOVERY"):
        entries[name.lower()] = make_setting_entry(
            name.lower(),
            [("process environment", environ.get(name)), (".env (not read directly)", dotenv.get(name))],
            "process environment" if environ.get(name) else "default",
            note="Outside Compose, .env alone does not set this process variable.",
        )
    entries["gcp_project_id_wizard"] = make_setting_entry(
        "gcp_project_id_wizard",
        [
            ("process environment", environ.get("GCP_PROJECT_ID")),
            (".env", dotenv.get("GCP_PROJECT_ID")),
            ("google_home_setup.json (wizard record)", setup.get("project_id")),
        ],
        "process environment / .env for subscriber; wizard record for setup",
    )
    return entries


def catalog_source_label(environ: dict[str, str], data_dir: Path) -> str:
    if environ.get("CATALOG_PATH"):
        return "CATALOG_PATH process environment"
    if (data_dir / "catalog.json").exists():
        return "data catalog"
    return "bundled catalog fallback"


def diagnose(environ: dict[str, str], dotenv: dict, data_dir: Path, root: Path = ROOT) -> dict:
    del root  # retained for older call sites that passed PROJECT_ROOT explicitly
    files, states = _load_persistent_files(data_dir)
    plex = states["plex_connection.json"]
    app = states["app_settings.json"]
    applied_app = _applied_app_settings(app, files)
    sync = states["google_home_sync.json"]
    setup = states["google_home_setup.json"]
    entries = build_model_settings(
        environ, dotenv, app=app, applied_app=applied_app, sync=sync, setup=setup, plex=plex,
    )
    entries.update(build_process_path_settings(environ, dotenv, setup))
    return {
        "settings": entries,
        "files": files,
        "release_preflight_warnings": [
            name for name, entry in entries.items() if entry["classification"] == "release_preflight_warning"
        ],
        "scope": (
            "Local process and .env only; Cloud Run, Firestore, Cloud Storage and Google Home were not queried. "
            "Cloud-side conflicts are unknown."
        ),
        "catalog_source": catalog_source_label(environ, data_dir),
        "index": "SQLite owns index settings and derived snapshot; not opened by this diagnostic.",
    }


def main():
    # DATA_DIR and CATALOG_PATH are read directly from os.environ by the app,
    # not from Pydantic's dotenv loader outside Compose.
    data_dir = Path(
        os.environ.get("DATA_DIR")
        or ("/data" if Path("/data").is_dir() else ROOT / "data" if (ROOT / "data").is_dir() else ROOT)
    )
    env_path = Path.cwd() / ".env"
    result = diagnose(dict(os.environ), dict(dotenv_values(env_path)) if env_path.exists() else {}, data_dir)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
