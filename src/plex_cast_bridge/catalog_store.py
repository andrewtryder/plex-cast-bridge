"""Catalog path selection and speaker persistence helpers."""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .speaker_registry import (
    google_home_sync_fingerprint,
    migrate_catalog_speakers,
    migrate_speaker_record,
    reconcile_speakers,
    speaker_playback_target,
)

if TYPE_CHECKING:
    from .cast_manager import CastManager

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = Path(
    os.getenv("DATA_DIR")
    or (
        "/data"
        if Path("/data").is_dir()
        else (PROJECT_ROOT / "data" if (PROJECT_ROOT / "data").is_dir() else PROJECT_ROOT)
    )
)
CANONICAL_CATALOG = PROJECT_ROOT / "catalog.json"
CLOUD_RUN_CATALOG = PROJECT_ROOT / "gcp-cloud-run" / "catalog.json"

_CATALOG_LOCK = threading.RLock()


def get_catalog_path(for_writing: bool = False) -> Path:
    env_cat = os.getenv("CATALOG_PATH")
    if env_cat:
        p = Path(env_cat)
        if for_writing:
            p.parent.mkdir(parents=True, exist_ok=True)
        return p

    data_cat = DATA_DIR / "catalog.json"
    bundled_cat = PROJECT_ROOT / "catalog.json"

    if for_writing:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not data_cat.exists() and bundled_cat.exists() and DATA_DIR != PROJECT_ROOT:
            try:
                shutil.copyfile(bundled_cat, data_cat)
            except Exception:
                pass
        return data_cat if (DATA_DIR != PROJECT_ROOT or data_cat.exists()) else bundled_cat

    if data_cat.exists():
        return data_cat
    if bundled_cat.exists():
        return bundled_cat
    return data_cat


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def _mirror_cloud_run_catalog(write_path: Path) -> None:
    if CLOUD_RUN_CATALOG.parent.exists():
        try:
            shutil.copyfile(write_path, CLOUD_RUN_CATALOG)
        except Exception as exc:
            log.warning("Could not sync catalog copy to Cloud Run path: %s", exc)


def load_catalog_data() -> dict[str, Any]:
    path = get_catalog_path(for_writing=False)
    if not path.exists():
        return {"speakers": [], "inputs": [], "genres": [], "artists": [], "themes": []}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return {"speakers": []}
    migrated, changed = migrate_catalog_speakers(data)
    if changed:
        save_catalog_data(migrated, sync_cloud=False)
    return migrated


def save_catalog_data(data: dict[str, Any], *, sync_cloud: bool = True) -> dict[str, Any]:
    with _CATALOG_LOCK:
        migrated, _ = migrate_catalog_speakers(data)
        write_path = get_catalog_path(for_writing=True)
        _atomic_write_json(write_path, migrated)
        _mirror_cloud_run_catalog(write_path)
        if sync_cloud:
            _maybe_schedule_google_home_push(migrated)
        return migrated


def _maybe_schedule_google_home_push(catalog: dict[str, Any]) -> None:
    """Debounced catalog push only when Google Home SYNC metadata changed."""
    try:
        from . import catalog_sync as gh_sync

        state = gh_sync.load_sync_state()
        fingerprint = google_home_sync_fingerprint(catalog.get("speakers") or [])
        if state.get("last_gh_fingerprint") == fingerprint and state.get("last_push_status") in {
            "succeeded",
            "pending",
            "configured",
        }:
            return
        gh_sync.save_sync_state({"last_gh_fingerprint": fingerprint})
        gh_sync.bump_and_schedule_push(delay_sec=2.0)
    except Exception as exc:
        log.debug("Google Home push schedule skipped: %s", exc)


def load_disabled_speakers_from_catalog() -> set[str]:
    disabled: set[str] = set()
    try:
        data = load_catalog_data()
        for spk in data.get("speakers", []):
            if spk.get("removed"):
                continue
            if not spk.get("enabled", True):
                target = speaker_playback_target(spk)
                if target:
                    disabled.add(target.casefold())
                if spk.get("room_hint"):
                    disabled.add(spk["room_hint"].casefold())
    except Exception as exc:
        log.warning("Could not read disabled speakers: %s", exc)
    return disabled


def list_speaker_records(*, include_removed: bool = False) -> list[dict[str, Any]]:
    data = load_catalog_data()
    out = []
    for s in data.get("speakers") or []:
        if s.get("removed") and not include_removed:
            continue
        out.append(s)
    return out


def persist_discovered_speakers_to_catalog(
    discovered: list[dict],
    cast_mgr: CastManager,
) -> dict[str, Any]:
    """Reconcile LAN discovery into catalog.json; returns {speakers, summary, changed}."""
    with _CATALOG_LOCK:
        data = load_catalog_data()
        before_fp = google_home_sync_fingerprint(data.get("speakers") or [])
        before_json = json.dumps(data.get("speakers") or [], sort_keys=True)

        # Align CastManager disabled set into discovered enabled flags.
        for dev in discovered:
            name = dev.get("name") or ""
            room = dev.get("room") or ""
            if cast_mgr.is_disabled(name) or cast_mgr.is_disabled(room):
                dev["enabled"] = False
            elif "enabled" not in dev:
                dev["enabled"] = True

        speakers, summary = reconcile_speakers(data.get("speakers") or [], discovered)

        # Honor cast_mgr disabled flags on matched records.
        for rec in speakers:
            target = speaker_playback_target(rec) or ""
            room = rec.get("room_hint") or ""
            if cast_mgr.is_disabled(target) or cast_mgr.is_disabled(room):
                rec["enabled"] = False

        data["speakers"] = speakers
        data["inputs"] = [
            s.get("room_hint") or s.get("speaker_name")
            for s in speakers
            if s.get("enabled", True) and not s.get("removed")
        ]
        after_json = json.dumps(data.get("speakers") or [], sort_keys=True)
        changed = before_json != after_json
        if changed:
            write_path = get_catalog_path(for_writing=True)
            _atomic_write_json(write_path, data)
            _mirror_cloud_run_catalog(write_path)
            after_fp = google_home_sync_fingerprint(data.get("speakers") or [])
            if after_fp != before_fp:
                _maybe_schedule_google_home_push(data)
        return {"speakers": speakers, "summary": summary, "changed": changed}


def update_speaker_record(google_home_device_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    with _CATALOG_LOCK:
        data = load_catalog_data()
        speakers = list(data.get("speakers") or [])
        found = None
        for idx, s in enumerate(speakers):
            if (s.get("google_home_device_id") or s.get("id")) == google_home_device_id:
                found = idx
                break
        if found is None:
            raise KeyError(google_home_device_id)
        rec = dict(speakers[found])
        allowed = {
            "enabled",
            "google_home_visible",
            "google_home_name",
            "room_hint",
            "nicknames",
            "needs_review",
            "removed",
        }
        for key, value in patch.items():
            if key in allowed:
                rec[key] = value
        if "google_home_name" in patch and patch["google_home_name"]:
            name = str(patch["google_home_name"]).strip()
            rec["google_home_name"] = name
            rec["name"] = name
            if "nicknames" not in patch:
                rec["nicknames"] = [name]
        if "room_hint" in patch:
            rec["roomHint"] = rec.get("room_hint")
        speakers[found] = migrate_speaker_record(rec, previously_exposed=rec.get("google_home_visible", False))
        data["speakers"] = speakers
        save_catalog_data(data, sync_cloud=True)
        return speakers[found]


def forget_speaker(google_home_device_id: str) -> dict[str, Any]:
    """Soft-remove a speaker so it disappears from SYNC but can be rediscovered."""
    return update_speaker_record(
        google_home_device_id,
        {"removed": True, "google_home_visible": False, "enabled": False},
    )


# Compatibility aliases matching the previous private names in main.py
_load_disabled_speakers_from_catalog = load_disabled_speakers_from_catalog
_persist_discovered_speakers_to_catalog = persist_discovered_speakers_to_catalog
