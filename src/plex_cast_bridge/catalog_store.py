"""Catalog path selection and speaker persistence helpers."""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from .cast_discovery import extract_room_name

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


def load_disabled_speakers_from_catalog() -> set[str]:
    disabled: set[str] = set()
    cat_path = get_catalog_path(for_writing=False)
    if cat_path.exists():
        try:
            with open(cat_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                for spk in data.get("speakers", []):
                    if not spk.get("enabled", True):
                        if spk.get("speaker_name"):
                            disabled.add(spk["speaker_name"].casefold())
                        if spk.get("roomHint"):
                            disabled.add(spk["roomHint"].casefold())
        except Exception as exc:
            log.warning("Could not read disabled speakers from %s: %s", cat_path, exc)
    return disabled


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def persist_discovered_speakers_to_catalog(discovered: list[dict], cast_mgr: CastManager):
    """Persist discovered LAN speakers and their enabled/disabled flags into catalog.json."""
    with _CATALOG_LOCK:
        try:
            read_path = get_catalog_path(for_writing=False)
            data = {}
            if read_path.exists():
                with open(read_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

            existing_by_room = {
                (s.get("roomHint") or "").casefold(): s
                for s in data.get("speakers", [])
                if s.get("roomHint")
            }

            speakers_list = []
            seen_rooms = set()
            for dev in discovered:
                name = dev["name"]
                if name.casefold() == "test speaker":
                    continue
                room = dev.get("room") or extract_room_name(name)
                r_fold = room.casefold()
                if r_fold in seen_rooms:
                    continue
                seen_rooms.add(r_fold)
                enabled = not cast_mgr.is_disabled(name) and not cast_mgr.is_disabled(room)
                speakers_list.append({
                    "id": f"plex-jukebox:{room}",
                    "name": "Plex",
                    "nicknames": [f"{room} Plex", "Plex", "Plex Cast Bridge", "Plex Radio"],
                    "roomHint": room,
                    "speaker_name": name,
                    "model": dev.get("model", "Google Cast"),
                    "host": dev.get("host", ""),
                    "port": dev.get("port", 8009),
                    "uuid": dev.get("uuid", ""),
                    "enabled": enabled,
                })

            # Keep any previously known speakers that might be temporarily asleep on LAN
            for r_fold, old_spk in existing_by_room.items():
                if r_fold not in seen_rooms:
                    old_name = old_spk.get("speaker_name", "")
                    old_room = old_spk.get("roomHint", "")
                    old_spk["enabled"] = (
                        not cast_mgr.is_disabled(old_name) and not cast_mgr.is_disabled(old_room)
                    )
                    speakers_list.append(old_spk)

            if speakers_list:
                data["speakers"] = speakers_list
                data["inputs"] = [s["roomHint"] for s in speakers_list if s.get("enabled", True)]
                write_path = get_catalog_path(for_writing=True)
                _atomic_write_json(write_path, data)
                if CLOUD_RUN_CATALOG.parent.exists():
                    try:
                        shutil.copyfile(write_path, CLOUD_RUN_CATALOG)
                    except Exception as exc:
                        log.warning("Could not sync catalog copy to Cloud Run path: %s", exc)
        except Exception as exc:
            log.warning("Could not persist speaker states to catalog.json: %s", exc)
            raise


# Compatibility aliases matching the previous private names in main.py
_load_disabled_speakers_from_catalog = load_disabled_speakers_from_catalog
_persist_discovered_speakers_to_catalog = persist_discovered_speakers_to_catalog
