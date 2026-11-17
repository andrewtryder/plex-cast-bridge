#!/usr/bin/env python3
"""Inspect configuration provenance without starting the bridge or changing state."""
from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import dotenv_values

from .config import Settings
from .app_settings_store import _normalize
from . import catalog_store

ROOT = catalog_store.PROJECT_ROOT


def read_json(path: Path) -> tuple[dict, str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return (value, "readable") if isinstance(value, dict) else ({}, "invalid")
    except FileNotFoundError:
        return {}, "absent"
    except (OSError, ValueError):
        return {}, "unreadable or invalid"


def diagnose(environ: dict[str, str], dotenv: dict, data_dir: Path, root: Path = ROOT) -> dict:
    files = {}
    states = {}
    for name in ("plex_connection.json", "app_settings.json", "catalog.json", "google_home_sync.json", "google_home_setup.json"):
        states[name], files[name] = read_json(data_dir / name)
    files["music_index.sqlite3"] = "present (not opened)" if (data_dir / "music_index.sqlite3").exists() else "absent"
    plex = states["plex_connection.json"]
    app = states["app_settings.json"]
    try:
        applied_app = _normalize(app) if app else {}
    except (TypeError, ValueError):
        applied_app = {}
        files["app_settings.json"] = "unreadable or invalid"
    sync = states["google_home_sync.json"]
    setup = states["google_home_setup.json"]
    entries = {}

    def add(name, candidates, effective, note=None, conflict=False, classification="ok"):
        present = [(source, value) for source, value in candidates if value is not None and value != ""]
        # Values are compared only in memory and are never included in output.
        entries[name] = {"source": effective, "present_sources": [source for source, _ in present],
                         "conflict": conflict, "classification": classification}
        if note:
            entries[name]["note"] = note

    def env_candidates(name):
        key = name.upper()
        return [("process environment", environ.get(key)), (".env", dotenv.get(key))]

    def env_source(name):
        return next((source for source, value in env_candidates(name) if value is not None), "default")

    gateway_pair = all((environ.get(k) if k in environ else dotenv.get(k)) for k in ("CLOUD_GATEWAY_URL", "CATALOG_WRITE_KEY"))
    for name in Settings.model_fields:
        candidates = env_candidates(name)
        source = env_source(name)
        note = None
        conflict = False
        classification = "ok"
        if name in ("default_cast_volume", "max_cast_volume", "night_cast_volume"):
            if applied_app.get(name) is not None:
                source = "app_settings.json" if name in app else "app_settings.json (normalized default)"
            if name in app and app[name] is not None:
                candidates.insert(0, ("app_settings.json", app[name]))
                env_value = next((v for _, v in env_candidates(name) if v is not None), None)
                try:
                    conflict = env_value not in (None, "") and float(app[name]) != float(env_value)
                except (TypeError, ValueError):
                    classification = "unknown"
                if conflict:
                    classification = "override"
            # Numeric spellings such as 0.4 and 0.40 are equivalent.
            for i, (s, v) in enumerate(candidates):
                try:
                    candidates[i] = (s, float(v)) if v is not None and v != "" else (s, v)
                except (TypeError, ValueError):
                    pass
        elif name in ("cloud_gateway_url", "catalog_write_key"):
            candidates.insert(0, ("google_home_sync.json", sync.get(name)))
            if name == "cloud_gateway_url":
                candidates.append(("google_home_setup.json (wizard record)", setup.get("cloud_run_url")))
            source = (env_source(name) + " (copied to sync file at startup)") if gateway_pair else ("google_home_sync.json" if sync.get(name) else "not configured")
            env_value = next((v for _, v in env_candidates(name) if v is not None), None)
            saved = sync.get(name)
            if env_value not in (None, "") and saved not in (None, ""):
                normalize = (lambda x: str(x).rstrip("/")) if name == "cloud_gateway_url" else str
                conflict = normalize(env_value) != normalize(saved)
            if conflict:
                classification = "release_preflight_warning" if gateway_pair else "informational"
            note = "Both environment values trigger a sync-file overwrite at startup." if gateway_pair else "A partial environment pair does not configure the sync file."
        elif name in ("plex_url", "plex_music_section", "plex_token"):
            field = {"plex_url": "server_url", "plex_music_section": "music_section", "plex_token": "jwt_token"}[name]
            if plex.get("mode") == "jwt" and plex.get("jwt_token"):
                candidates.insert(0, ("plex_connection.json (JWT)", plex.get(field)))
                source = "plex_connection.json (JWT; environment fallback if connection fails)"
                if name == "plex_token":
                    note = "JWT and legacy server tokens may differ by design."
        add(name, candidates, source, note, conflict, classification)
    for name in ("DATA_DIR", "CATALOG_PATH", "GOOGLE_APPLICATION_CREDENTIALS", "PORT", "PCB_DISABLE_BACKGROUND_DISCOVERY"):
        add(name.lower(), [("process environment", environ.get(name)), (".env (not read directly)", dotenv.get(name))], "process environment" if environ.get(name) else "default", "Outside Compose, .env alone does not set this process variable.")
    add("gcp_project_id_wizard", [("process environment", environ.get("GCP_PROJECT_ID")), (".env", dotenv.get("GCP_PROJECT_ID")), ("google_home_setup.json (wizard record)", setup.get("project_id"))], "process environment / .env for subscriber; wizard record for setup")
    return {"settings": entries, "files": files,
            "release_preflight_warnings": [name for name, entry in entries.items() if entry["classification"] == "release_preflight_warning"],
            "scope": "Local process and .env only; Cloud Run, Firestore, Cloud Storage and Google Home were not queried. Cloud-side conflicts are unknown.",
            "catalog_source": "CATALOG_PATH process environment" if environ.get("CATALOG_PATH") else ("data catalog" if (data_dir / "catalog.json").exists() else "bundled catalog fallback"),
            "index": "SQLite owns index settings and derived snapshot; not opened by this diagnostic."}


def main():
    # DATA_DIR and CATALOG_PATH are read directly from os.environ by the app,
    # not from Pydantic's dotenv loader outside Compose.
    data_dir = Path(os.environ.get("DATA_DIR") or ("/data" if Path("/data").is_dir() else ROOT / "data" if (ROOT / "data").is_dir() else ROOT))
    env_path = Path.cwd() / ".env"
    result = diagnose(dict(os.environ), dict(dotenv_values(env_path)) if env_path.exists() else {}, data_dir)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
