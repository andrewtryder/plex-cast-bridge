"""Stable Cast speaker identity, migration, and discovery reconciliation."""

from __future__ import annotations

import copy
import logging
import secrets
from datetime import UTC, datetime
from typing import Any

from .cast_discovery import extract_room_name

log = logging.getLogger(__name__)

DEVICE_ID_PREFIX = "plex-jukebox"
LEGACY_GENERIC_NICKNAMES = {"plex", "plex cast bridge", "plex radio"}


def _utcnow_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _norm(value: str | None) -> str:
    return (value or "").strip().casefold()


def is_cast_group(model: str | None, name: str | None = None) -> bool:
    blob = f"{model or ''} {name or ''}".casefold()
    return "group" in blob


def default_google_home_name(room_hint: str | None, speaker_name: str | None) -> str:
    room = (room_hint or extract_room_name(speaker_name or "Speaker") or "Speaker").strip()
    return f"{room} Plex"


def new_google_home_device_id(cast_uuid: str | None = None) -> str:
    """Opaque stable Google Home device id for newly discovered destinations."""
    token = (cast_uuid or "").replace("-", "").strip().lower()
    if len(token) >= 8:
        return f"{DEVICE_ID_PREFIX}:cast-{token}"
    return f"{DEVICE_ID_PREFIX}:id-{secrets.token_hex(8)}"


def _unique_nicknames(google_home_name: str) -> list[str]:
    name = google_home_name.strip() or "Plex"
    nicknames = [name]
    if not name.casefold().endswith(" plex"):
        nicknames.append(f"{name} Plex")
    return nicknames


def speaker_playback_target(record: dict[str, Any]) -> str | None:
    return (record.get("speaker_name") or record.get("room_hint") or record.get("roomHint") or "").strip() or None


def is_google_home_exposed(record: dict[str, Any]) -> bool:
    """True when the virtual device should appear in Google Home SYNC."""
    if record.get("removed"):
        return False
    return bool(record.get("google_home_visible", False))


def migrate_speaker_record(raw: dict[str, Any], *, previously_exposed: bool = True) -> dict[str, Any]:
    """Normalize a catalog speaker dict to the stable-identity schema (idempotent)."""
    speaker_name = (raw.get("speaker_name") or raw.get("name") or "").strip()
    if speaker_name.casefold() in {"plex", "plex cast bridge"}:
        # Older catalogs used name=Plex; prefer speaker_name / room.
        speaker_name = (raw.get("speaker_name") or raw.get("roomHint") or raw.get("room_hint") or "Speaker").strip()

    room_hint = (raw.get("room_hint") or raw.get("roomHint") or extract_room_name(speaker_name)).strip()
    cast_uuid = (raw.get("cast_uuid") or raw.get("uuid") or "").strip()
    legacy_id = (raw.get("google_home_device_id") or raw.get("id") or "").strip()

    # Preserve legacy plex-jukebox:<room> IDs when present.
    if legacy_id.startswith(f"{DEVICE_ID_PREFIX}:"):
        google_home_device_id = legacy_id
    elif cast_uuid:
        google_home_device_id = new_google_home_device_id(cast_uuid)
    else:
        google_home_device_id = new_google_home_device_id()

    google_home_name = (raw.get("google_home_name") or "").strip()
    if not google_home_name:
        # Prefer unique room-based name; ignore legacy generic "Plex" display name.
        legacy_name = (raw.get("name") or "").strip()
        if legacy_name and legacy_name.casefold() not in LEGACY_GENERIC_NICKNAMES:
            google_home_name = legacy_name
        else:
            google_home_name = default_google_home_name(room_hint, speaker_name)

    if "google_home_visible" in raw:
        google_home_visible = bool(raw.get("google_home_visible"))
    else:
        # Migration: previously every enabled speaker was SYNC'd.
        google_home_visible = bool(previously_exposed and raw.get("enabled", True))

    nicknames = raw.get("nicknames")
    if not isinstance(nicknames, list) or not nicknames:
        nicknames = _unique_nicknames(google_home_name)
    else:
        # Drop generic shared nicknames that make multi-device targeting ambiguous.
        cleaned = []
        for n in nicknames:
            if not isinstance(n, str):
                continue
            if n.strip().casefold() in LEGACY_GENERIC_NICKNAMES and n.strip().casefold() != google_home_name.casefold():
                continue
            cleaned.append(n.strip())
        if google_home_name not in cleaned:
            cleaned.insert(0, google_home_name)
        nicknames = cleaned or _unique_nicknames(google_home_name)

    model = raw.get("model") or "Google Cast"
    kind = raw.get("kind") or ("group" if is_cast_group(model, speaker_name) else "speaker")

    return {
        "id": google_home_device_id,
        "google_home_device_id": google_home_device_id,
        "cast_uuid": cast_uuid,
        "speaker_name": speaker_name,
        "google_home_name": google_home_name,
        "nicknames": nicknames,
        "room_hint": room_hint,
        "roomHint": room_hint,  # cloud SYNC compatibility
        "model": model,
        "kind": kind,
        "host": raw.get("host") or "",
        "port": int(raw.get("port") or 8009),
        "enabled": bool(raw.get("enabled", True)),
        "google_home_visible": google_home_visible,
        "last_seen": raw.get("last_seen"),
        "availability": raw.get("availability") or "unknown",
        "needs_review": bool(raw.get("needs_review", False)),
        "removed": bool(raw.get("removed", False)),
        "name": google_home_name,  # legacy field used by older readers
    }


def migrate_catalog_speakers(catalog: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Idempotently migrate catalog['speakers']; returns (catalog, changed)."""
    data = copy.deepcopy(catalog) if catalog else {}
    raw_list = list(data.get("speakers") or [])
    migrated = [migrate_speaker_record(s, previously_exposed=True) for s in raw_list]
    # Deduplicate by google_home_device_id while migrating.
    by_id: dict[str, dict[str, Any]] = {}
    for rec in migrated:
        gid = rec["google_home_device_id"]
        if gid in by_id:
            # Prefer the record with a cast_uuid / more recent last_seen.
            existing = by_id[gid]
            if not existing.get("cast_uuid") and rec.get("cast_uuid"):
                by_id[gid] = rec
            continue
        by_id[gid] = rec
    new_list = list(by_id.values())
    changed = new_list != raw_list
    data["speakers"] = new_list
    data["inputs"] = [
        s.get("room_hint") or s.get("speaker_name")
        for s in new_list
        if s.get("enabled", True) and not s.get("removed")
    ]
    return data, changed


def _match_existing(
    discovered: dict[str, Any],
    existing: list[dict[str, Any]],
    used_ids: set[str],
) -> dict[str, Any] | None:
    uuid = _norm(discovered.get("uuid") or discovered.get("cast_uuid"))
    name = _norm(discovered.get("name"))

    if uuid:
        for rec in existing:
            if rec["google_home_device_id"] in used_ids:
                continue
            if _norm(rec.get("cast_uuid")) == uuid:
                return rec

    # Only attach a UUID to an unambiguous legacy record without an identity.
    # A different known UUID must never be merged, even if its name or IP matches.
    if name:
        candidates = [rec for rec in existing if _norm(rec.get("speaker_name")) == name]
        if len(candidates) == 1:
            rec = candidates[0]
            if not rec.get("cast_uuid") and rec["google_home_device_id"] not in used_ids:
                return rec
    return None


def reconcile_speakers(
    existing_speakers: list[dict[str, Any]],
    discovered: list[dict[str, Any]],
    *,
    now_iso: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Merge discovery results into persisted speaker records.

    Returns (speakers, summary) where summary counts new/renamed/unchanged/offline/review.
    """
    now = now_iso or _utcnow_iso()
    existing = [migrate_speaker_record(s) for s in existing_speakers]
    summary = {
        "new": 0,
        "renamed": 0,
        "unchanged": 0,
        "offline": 0,
        "returned": 0,
        "needs_review": 0,
        "total": 0,
        "message": "",
    }

    used_ids: set[str] = set()
    result: list[dict[str, Any]] = []

    seen_uuids: set[str] = set()
    name_counts: dict[str, int] = {}
    for dev in discovered:
        key = _norm(dev.get("name"))
        name_counts[key] = name_counts.get(key, 0) + 1

    for dev in discovered:
        name = (dev.get("name") or "").strip()
        if not name or name.casefold() == "test speaker":
            continue
        uuid_key = _norm(dev.get("uuid") or dev.get("cast_uuid"))
        if uuid_key and uuid_key in seen_uuids:
            continue
        if uuid_key:
            seen_uuids.add(uuid_key)
        # Ambiguous discovery names cannot safely identify legacy records.
        match_dev = dev if name_counts[_norm(name)] == 1 else {**dev, "name": ""}
        matched = _match_existing(match_dev, existing, used_ids)
        if matched and matched.get("removed"):
            used_ids.add(matched["google_home_device_id"])
            result.append(matched)
            continue
        room = (dev.get("room") or extract_room_name(name)).strip()
        model = dev.get("model") or "Google Cast"
        cast_uuid = (dev.get("uuid") or "").strip()
        host = dev.get("host") or ""
        port = int(dev.get("port") or 8009)
        kind = "group" if is_cast_group(model, name) else "speaker"

        if matched:
            used_ids.add(matched["google_home_device_id"])
            rec = copy.deepcopy(matched)
            renamed = _norm(rec.get("speaker_name")) != _norm(name)
            was_offline = (rec.get("availability") or "") == "offline"
            rec["speaker_name"] = name
            if cast_uuid:
                rec["cast_uuid"] = cast_uuid
            rec["host"] = host
            rec["port"] = port
            rec["model"] = model
            rec["kind"] = kind
            # Preserve user room_hint edits unless empty.
            if not rec.get("room_hint"):
                rec["room_hint"] = room
            rec["roomHint"] = rec.get("room_hint") or room
            rec["last_seen"] = now
            rec["availability"] = "online"
            rec["removed"] = False
            if renamed:
                summary["renamed"] += 1
            elif was_offline:
                summary["returned"] += 1
            else:
                summary["unchanged"] += 1
            result.append(migrate_speaker_record(rec, previously_exposed=rec.get("google_home_visible", False)))
        else:
            gh_name = default_google_home_name(room, name)
            rec = migrate_speaker_record(
                {
                    "speaker_name": name,
                    "room_hint": room,
                    "cast_uuid": cast_uuid,
                    "model": model,
                    "kind": kind,
                    "host": host,
                    "port": port,
                    "enabled": True,
                    "google_home_visible": False,  # new devices hidden until opted in
                    "google_home_name": gh_name,
                    "last_seen": now,
                    "availability": "online",
                },
                previously_exposed=False,
            )
            summary["new"] += 1
            used_ids.add(rec["google_home_device_id"])
            result.append(rec)

    # Preserve known speakers not seen this scan (offline), unless explicitly removed.
    for rec in existing:
        gid = rec["google_home_device_id"]
        if gid in used_ids:
            continue
        if rec.get("removed"):
            result.append(rec)
            continue
        offline = copy.deepcopy(rec)
        offline["availability"] = "offline"
        # Do not clear last_seen; do not auto-delete.
        summary["offline"] += 1
        result.append(migrate_speaker_record(offline, previously_exposed=offline.get("google_home_visible", False)))

    for rec in result:
        if rec.get("needs_review"):
            summary["needs_review"] += 1

    summary["total"] = len([r for r in result if not r.get("removed")])
    parts = []
    if summary["new"]:
        parts.append(f"{summary['new']} new")
    if summary["renamed"]:
        parts.append(f"{summary['renamed']} renamed")
    if summary["returned"]:
        parts.append(f"{summary['returned']} returned")
    if summary["offline"]:
        parts.append(f"{summary['offline']} offline")
    unchanged = summary["unchanged"]
    if unchanged:
        parts.append(f"{unchanged} unchanged")
    summary["message"] = (", ".join(parts) + " speaker" + ("s" if summary["total"] != 1 else "")) if parts else "No speaker changes."
    return result, summary


def google_home_sync_fingerprint(speakers: list[dict[str, Any]]) -> str:
    """Stable fingerprint of SYNC-relevant speaker fields (ignore host/last_seen)."""
    rows = []
    for s in speakers:
        if not is_google_home_exposed(s):
            continue
        rows.append(
            "|".join(
                [
                    s.get("google_home_device_id") or "",
                    s.get("google_home_name") or "",
                    s.get("room_hint") or "",
                    ",".join(s.get("nicknames") or []),
                    speaker_playback_target(s) or "",
                ]
            )
        )
    return "\n".join(sorted(rows))


def find_by_google_home_id(speakers: list[dict[str, Any]], device_id: str | None) -> dict[str, Any] | None:
    if not device_id:
        return None
    for s in speakers:
        if (s.get("google_home_device_id") or s.get("id")) == device_id:
            return s
    return None
