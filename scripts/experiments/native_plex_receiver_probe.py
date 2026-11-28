#!/usr/bin/env python3
"""Safely compare PyChromecast's native Plex receiver with the bridge receiver.

Run inside the existing Plex Cast Bridge runtime so its already-authorized
Plex connection and Cast catalog are reused. Inspection is read-only. Any
playback requires --allow-disruptive, an exact device UUID, and a positive
idle check (or an explicit in-person UNKNOWN-state confirmation).

This script never logs Plex tokens, media URLs, HTTP payloads, or controller
command payloads. It does not update stored connection settings or the Cast
catalog. It does not require Home Assistant.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import traceback
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pychromecast
from plexapi.playqueue import PlayQueue
from plexapi.server import PlexServer
from pychromecast.const import CAST_TYPE_GROUP
from pychromecast.controllers.multizone import (
    MultiZoneControllerListener,
    MultizoneManager,
    PlaybackSessionUpdate,
)
from pychromecast.controllers.plex import PlexController

from plex_cast_bridge.cast_manager import (
    SpeakerQueue,
    resolve_dial_display_metadata,
    verify_host_cast_uuid,
)
from plex_cast_bridge.catalog_store import list_speaker_records
from plex_cast_bridge.config import Settings
from plex_cast_bridge.plex_connection_store import load_connection
from plex_cast_bridge.speaker_registry import is_cast_group

LOG = logging.getLogger("native_plex_receiver_probe")
DEFAULT_MEDIA_RECEIVER_APP_ID = "CC1AD845"
NATIVE_PLEX_APP_ID = "9AC194DC"
IDLE_APP_ID = "E8C28D3C"
MEDIA_NAMESPACE = "urn:x-cast:com.google.cast.media"


class ProbeFailure(RuntimeError):
    """A safety precondition or observable playback condition failed."""


class ObservedPlexController(PlexController):
    """Plex controller that records only a message type, never message data."""

    def __init__(self) -> None:
        super().__init__()
        self.last_message_type: str | None = None
        self.last_message_at: float | None = None

    def receive_message(self, message: object, data: dict[str, Any]) -> bool:
        self.last_message_type = str(data.get("type", ""))[:40]
        self.last_message_at = time.monotonic()
        return super().receive_message(message, data)


class GroupStatusObserver(MultiZoneControllerListener):
    """Record that a group returned a fresh multizone membership snapshot."""

    def __init__(self) -> None:
        self.event = threading.Event()

    def multizone_member_added(self, _group_uuid: str) -> None:
        pass

    def multizone_member_removed(self, _group_uuid: str) -> None:
        pass

    def multizone_status_received(self) -> None:
        self.event.set()

    def multizone_playback_session_updated(self, _update: PlaybackSessionUpdate) -> None:
        pass


@dataclass
class _StatusCallback:
    event: threading.Event
    success: bool = False
    response: dict[str, Any] | None = None

    def callback(self, success: bool, response: dict[str, Any] | None) -> None:
        self.success = success is True
        self.response = response if isinstance(response, dict) else None
        self.event.set()


def _fresh_receiver_response(result: _StatusCallback) -> dict[str, Any] | None:
    response = result.response
    if not result.success or not response or response.get("type") != "RECEIVER_STATUS":
        return None
    status = response.get("status")
    if not isinstance(status, dict) or not isinstance(status.get("applications"), list):
        return None
    standby = status.get("isStandBy")
    applications = status["applications"]
    if not isinstance(standby, bool) or len(applications) > 1:
        return None
    app = applications[0] if applications else {}
    if not isinstance(app, dict):
        return None
    app_id = app.get("appId")
    if app_id is not None and not isinstance(app_id, str):
        return None
    return {
        "app_id": app_id,
        "app_name": app.get("displayName"),
        "standby": standby,
    }


def _fresh_media_response(result: _StatusCallback) -> dict[str, Any] | None:
    response = result.response
    if not result.success or not response or response.get("type") != "MEDIA_STATUS":
        return None
    statuses = response.get("status")
    if not isinstance(statuses, list) or len(statuses) > 1:
        return None
    if not statuses:
        return {"session_id": None, "player_state": "IDLE"}
    media = statuses[0]
    if not isinstance(media, dict):
        return None
    session_id = media.get("mediaSessionId")
    player_state = media.get("playerState")
    content_id = media.get("media", {}).get("contentId") if isinstance(media.get("media"), dict) else media.get("contentId")
    if not isinstance(session_id, (int, str)) or not str(session_id).isdigit():
        return None
    if player_state not in {"PLAYING", "PAUSED", "BUFFERING", "IDLE"}:
        return None
    if content_id is not None and not isinstance(content_id, str):
        return None
    return {"session_id": session_id, "player_state": player_state, "content_id": content_id}


def _fresh_receiver_volume(result: _StatusCallback) -> float | None:
    response = result.response
    if not result.success or not response or response.get("type") != "RECEIVER_STATUS":
        return None
    status = response.get("status")
    volume = status.get("volume") if isinstance(status, dict) else None
    level = volume.get("level") if isinstance(volume, dict) else None
    if isinstance(level, bool) or not isinstance(level, (int, float)):
        return None
    level = float(level)
    return level if math.isfinite(level) and 0 <= level <= 1 else None


def _query_receiver_volume(cast: pychromecast.Chromecast, timeout: float = 2.0) -> float | None:
    result = _StatusCallback(threading.Event())
    try:
        cast.socket_client.receiver_controller.update_status(callback_function=result.callback)
        result.event.wait(timeout)
    except Exception:
        return None
    return _fresh_receiver_volume(result)


def _wait_receiver_volume(cast: pychromecast.Chromecast, target: float, timeout: float = 4.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observed = _query_receiver_volume(
            cast, timeout=min(1.0, max(0.05, deadline - time.monotonic()))
        )
        if observed is not None and abs(observed - target) <= 0.02:
            return True
        time.sleep(0.1)
    return False


def _restore_original_volume(cast: pychromecast.Chromecast, original: float) -> bool:
    cast.set_volume(original)
    return _wait_receiver_volume(cast, original)


def _norm_uuid(value: object) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return str(value or "").strip().casefold()


def _plex_connection() -> tuple[PlexServer, str]:
    """Reuse stored authorization without refreshing or persisting credentials."""
    settings = Settings()
    stored = load_connection() or {}
    url = str(stored.get("server_url") or settings.plex_url).rstrip("/")
    section = str(stored.get("music_section") or settings.plex_music_section)
    token = str(stored.get("access_token") or stored.get("jwt_token") or "")
    if not token and settings.plex_token:
        token = settings.plex_token.get_secret_value()
    if not url or not token:
        raise ProbeFailure("No existing Plex connection is available")
    try:
        server = PlexServer(url, token)
        server.library.section(section)
    except Exception as exc:
        raise ProbeFailure(f"Plex connection failed ({type(exc).__name__})") from None
    return server, section


def _select_record(device_uuid: str) -> dict[str, Any]:
    matches = [
        row
        for row in list_speaker_records()
        if _norm_uuid(row.get("cast_uuid") or row.get("uuid")) == _norm_uuid(device_uuid)
    ]
    if len(matches) != 1:
        raise ProbeFailure("UUID must identify exactly one configured Cast record")
    row = matches[0]
    if not row.get("enabled", True) or not row.get("google_home_visible", True):
        raise ProbeFailure("Configured Cast record is disabled or hidden")
    return row


def _record_is_group(row: dict[str, Any]) -> bool:
    return bool(
        row.get("kind") == "group"
        or row.get("is_group")
        or is_cast_group(row.get("model"), row.get("speaker_name") or row.get("name"))
    )


def _device_info(row: dict[str, Any]) -> tuple[tuple, str, bool]:
    """Build an endpoint only after identity evidence independent of from_host."""
    host = row.get("host")
    port = row.get("port") or 8009
    expected = row.get("cast_uuid") or row.get("uuid")
    if not host or not expected:
        raise ProbeFailure("Configured Cast host or UUID is unavailable")
    if _record_is_group(row):
        # DIAL deliberately rejects Cast Groups. Require a fresh exact mDNS match.
        casts, browser = pychromecast.get_chromecasts(timeout=4)
        try:
            hits = [
                cast.cast_info
                for cast in casts
                if _norm_uuid(getattr(cast, "uuid", None)) == _norm_uuid(expected)
                and getattr(cast.cast_info, "cast_type", None) == CAST_TYPE_GROUP
            ]
            if len(hits) != 1:
                raise ProbeFailure("Cast Group identity was not uniquely confirmed by mDNS")
            info = hits[0]
            if info.host != host:
                raise ProbeFailure("Configured Cast Group host differs from current mDNS")
            model = info.model_name or row.get("model") or "Google Cast Group"
            name = info.friendly_name or row.get("speaker_name") or "Cast Group"
            return (info.host, info.port or port, info.uuid, model, name), str(info.uuid), True
        finally:
            try:
                browser.stop_discovery()
            except Exception:
                pass
            for cast in casts or []:
                try:
                    cast.disconnect()
                except Exception:
                    pass

    try:
        verified, device_info = verify_host_cast_uuid(str(host), str(expected))
    except ValueError as exc:
        raise ProbeFailure(f"Individual Cast identity verification failed ({type(exc).__name__})") from None
    model, name = resolve_dial_display_metadata(
        device_info,
        fallback_model=row.get("model") or "Google Cast",
        fallback_name=row.get("speaker_name") or row.get("name") or "Cast speaker",
    )
    return (str(host), int(port), verified, model, name), str(verified), False


def _fresh_cast_status(cast: pychromecast.Chromecast, timeout: float = 4.0) -> dict[str, Any]:
    """Request only receiver status and active media status; never launch an app."""
    receiver_result = _StatusCallback(threading.Event())
    media_result = _StatusCallback(threading.Event())
    media = cast.media_controller
    cast.socket_client.receiver_controller.update_status(callback_function=receiver_result.callback)
    namespaces = getattr(cast.socket_client, "app_namespaces", ()) or ()
    media_active = media.namespace in namespaces
    if media_active:
        media.update_status(callback_function=media_result.callback)
    receiver_result.event.wait(timeout)
    if media_active:
        media_result.event.wait(timeout)
    status = media.status
    receiver_data = _fresh_receiver_response(receiver_result)
    media_data = _fresh_media_response(media_result) if media_active else None
    return {
        "connected": bool(cast.socket_client.is_connected),
        "receiver_callback_received": receiver_result.event.is_set(),
        "receiver_response_type": str(
            receiver_result.response.get("type", "")
            if receiver_result.response else ""
        )[:40],
        "receiver_status_received": receiver_data is not None,
        "receiver_volume_level": _fresh_receiver_volume(receiver_result),
        "media_namespace_active": media_active,
        "media_callback_received": media_result.event.is_set() if media_active else False,
        "media_response_type": str(
            media_result.response.get("type", "")
            if media_result.response else ""
        )[:40],
        "media_status_received": media_data is not None,
        "receiver_app_id": receiver_data.get("app_id") if receiver_data else None,
        "receiver_app_name": receiver_data.get("app_name") if receiver_data else None,
        "receiver_is_standby": receiver_data.get("standby") if receiver_data else None,
        "player_state": str(
            media_data.get("player_state") if media_data else getattr(status, "player_state", "UNKNOWN")
        ).upper(),
        "media_session_id_present": (
            media_data.get("session_id") is not None
            if media_data is not None
            else getattr(status, "media_session_id", None) is not None
        ),
        "current_time": getattr(status, "current_time", None),
        "duration": getattr(status, "duration", None),
        "title": getattr(status, "title", None),
        "artist": getattr(status, "artist", None),
        "album": getattr(status, "album_name", None),
        "artwork_available": any(
            bool(getattr(image, "url", None))
            for image in (getattr(status, "images", None) or [])
        ),
        "media_content_id_present": bool(getattr(status, "content_id", None)),
    }


def _group_inventory(target_uuid: str, target_name: str) -> dict[str, Any]:
    """Ask discovered Cast Groups for membership/status without changing playback."""
    try:
        casts, browser = pychromecast.get_chromecasts(timeout=4)
    except Exception as exc:
        return {"discovery": "failed", "error_type": type(exc).__name__, "memberships": []}
    manager = MultizoneManager()
    groups = []
    status_observers: dict[str, GroupStatusObserver] = {}
    groups_seen = 0
    group_connection_failures = 0
    try:
        for cast in casts or []:
            info = getattr(cast, "cast_info", None)
            if getattr(info, "cast_type", None) != CAST_TYPE_GROUP:
                continue
            groups_seen += 1
            try:
                cast.wait(timeout=5)
                manager.add_multizone(cast)
                observer = GroupStatusObserver()
                group_listener = manager._groups[str(cast.uuid)]["listener"]
                group_listener._mz.register_listener(observer)
                status_observers[_norm_uuid(cast.uuid)] = observer
                # add_multizone may be registered after the Cast socket was
                # already connected; request the membership snapshot directly.
                group_listener._mz.update_members()
                groups.append(cast)
            except Exception:
                group_connection_failures += 1
        missing_group_status = [
            group_uuid for group_uuid, observer in status_observers.items()
            if not observer.event.wait(4.0)
        ]
        try:
            memberships = manager.get_multizone_memberships(UUID(target_uuid))
        except (KeyError, ValueError):
            memberships = []
        group_rows = []
        by_uuid = {_norm_uuid(cast.uuid): cast for cast in groups}
        for group_uuid in memberships:
            group = by_uuid.get(_norm_uuid(group_uuid))
            if group is None:
                group_rows.append({"uuid": str(group_uuid), "status": "unobserved"})
                continue
            group_status = _fresh_cast_status(group, timeout=2)
            group_rows.append({
                "uuid": str(group_uuid),
                "name": getattr(group.cast_info, "friendly_name", None),
                "player_state": group_status["player_state"],
                "receiver_status_received": group_status["receiver_status_received"],
                "media_status_received": group_status["media_status_received"],
                "receiver_app_id": group_status["receiver_app_id"],
                "receiver_is_standby": group_status["receiver_is_standby"],
                "media_session_id_present": group_status["media_session_id_present"],
            })
        return {
            "discovery": (
                "complete"
                if group_connection_failures == 0 and not missing_group_status
                else "incomplete"
            ),
            "groups_seen": groups_seen,
            "group_connection_failures": group_connection_failures,
            "groups_without_status": missing_group_status,
            "memberships": group_rows,
            "target_name": target_name,
        }
    finally:
        try:
            browser.stop_discovery()
        except Exception:
            pass
        for cast in casts or []:
            try:
                cast.disconnect()
            except Exception:
                pass


def _classify(snapshot: dict[str, Any], groups: dict[str, Any]) -> str:
    if not snapshot.get("connected"):
        return "disconnected"
    if snapshot.get("media_session_id_present"):
        state = snapshot.get("player_state")
        return state.lower() if state in {"PLAYING", "PAUSED", "BUFFERING"} else "indeterminate"
    if snapshot.get("player_state") in {"PLAYING", "PAUSED", "BUFFERING"}:
        return "other_media_" + snapshot["player_state"].lower()
    if snapshot.get("receiver_app_id") not in (None, DEFAULT_MEDIA_RECEIVER_APP_ID):
        if (
            snapshot.get("receiver_app_id") == IDLE_APP_ID
            and snapshot.get("receiver_is_standby") is True
        ):
            return "indeterminate"
        return "other_app_active" if snapshot.get("receiver_is_standby") is False else "indeterminate"
    if (
        snapshot.get("receiver_status_received")
        and snapshot.get("media_status_received")
        and snapshot.get("receiver_app_id") == DEFAULT_MEDIA_RECEIVER_APP_ID
        and snapshot.get("receiver_is_standby") is True
        and not snapshot.get("media_session_id_present")
    ):
        if groups.get("discovery") != "complete":
            return "indeterminate_group_state"
        for group in groups.get("memberships", []):
            if (
                group.get("player_state") not in {"IDLE", "STOPPED"}
                or group.get("receiver_status_received") is not True
                or group.get("media_status_received") is not True
                or group.get("media_session_id_present") is True
            ):
                return "group_active_or_indeterminate"
            if group.get("receiver_is_standby") is not True:
                return "group_active_or_indeterminate"
        return "idle"
    return "indeterminate"


def _physical_idle_confirmation_can_override(
    snapshot: dict[str, Any], groups: dict[str, Any], *, is_group: bool
) -> bool:
    """Permit an in-person override only when no positive active evidence exists."""
    if not snapshot.get("connected"):
        return False
    if snapshot.get("media_session_id_present") is True:
        return False
    if snapshot.get("player_state") not in {"UNKNOWN", "IDLE", "STOPPED"}:
        return False
    if groups.get("discovery") != "complete":
        return False
    if groups.get("memberships") and not is_group:
        # Starting a member can affect every speaker in its active group.
        return False
    receiver_app = snapshot.get("receiver_app_id")
    if receiver_app not in (None, DEFAULT_MEDIA_RECEIVER_APP_ID, IDLE_APP_ID):
        return False
    if snapshot.get("receiver_is_standby") is False:
        return False
    return True


def _pick_unique(items: list[Any], query: str, media_type: str) -> Any:
    if not query.strip():
        raise ProbeFailure(f"A known in-library {media_type} selection is required")
    exact = [item for item in items if str(getattr(item, "title", "")).casefold() == query.casefold()]
    if len(exact) == 1:
        return exact[0]
    if len(items) == 1:
        return items[0]
    if not items:
        raise ProbeFailure(f"No matching {media_type} was found in the configured library")
    raise ProbeFailure(f"Selection matches {len(items)} {media_type} results; use an exact title")


def _build_media(server: PlexServer, section_name: str, kind: str, selection: str) -> tuple[Any, int | None]:
    section = server.library.section(section_name)
    if kind == "track":
        item = _pick_unique(section.search(title=selection, libtype="track"), selection, "track")
        return item, 1
    if kind == "album":
        item = _pick_unique(section.search(title=selection, libtype="album"), selection, "album")
        tracks = item.tracks()
        return item, len(tracks)
    if kind == "artist-shuffle":
        item = _pick_unique(section.search(title=selection, libtype="artist"), selection, "artist")
        queue = PlayQueue.create(server, item, shuffle=True, includeRelated=False)
        return queue, len(queue.items)
    if kind == "playlist":
        playlists = server.playlists()
        item = _pick_unique(playlists, selection, "playlist")
        if str(getattr(item, "playlistType", "audio")).casefold() not in {"audio", "music"}:
            raise ProbeFailure("Selected Plex playlist is not an audio playlist")
        queue = PlayQueue.create(server, item, includeRelated=False)
        return queue, len(queue.items)
    if kind == "station":
        artist = _pick_unique(section.search(title=selection, libtype="artist"), selection, "artist")
        stations = artist.station()
        station_key = getattr(stations, "key", None)
        if not station_key:
            raise ProbeFailure("Plex did not provide a station key for the selected artist")
        queue = PlayQueue.fromStationKey(server, station_key)
        return queue, len(queue.items)
    raise ProbeFailure("Unsupported media case")


def _status_snapshot(cast: pychromecast.Chromecast) -> dict[str, Any]:
    status = cast.media_controller.status
    return {
        "player_state": str(getattr(status, "player_state", "UNKNOWN")).upper(),
        "session_id": getattr(status, "media_session_id", None),
        "content_id": getattr(status, "content_id", None),
        "title": getattr(status, "title", None),
        "artist": getattr(status, "artist", None),
        "album": getattr(status, "album_name", None),
        "position": getattr(status, "current_time", None),
        "duration": getattr(status, "duration", None),
        "artwork": any(bool(getattr(image, "url", None)) for image in (getattr(status, "images", None) or [])),
        "fresh_status": False,
    }


def _refresh_media(cast: pychromecast.Chromecast, timeout: float = 4.0) -> dict[str, Any]:
    namespaces = getattr(cast.socket_client, "app_namespaces", ()) or ()
    media = cast.media_controller
    if getattr(media, "namespace", MEDIA_NAMESPACE) not in namespaces:
        return _status_snapshot(cast)
    result = _StatusCallback(threading.Event())
    try:
        media.update_status(callback_function=result.callback)
        result.event.wait(timeout)
    except Exception:
        pass
    snapshot = _status_snapshot(cast)
    media_data = _fresh_media_response(result)
    if media_data is not None:
        snapshot["fresh_status"] = True
        snapshot["player_state"] = media_data["player_state"]
        snapshot["session_id"] = media_data["session_id"]
        snapshot["content_id"] = media_data.get("content_id")
    return snapshot


def _wait_state(cast: pychromecast.Chromecast, expected: set[str], timeout: float = 25.0) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = _refresh_media(cast, timeout=2)
        if snapshot["fresh_status"] and snapshot["player_state"] in expected:
            return snapshot
        time.sleep(0.5)
    return None


def _session_snapshot(server: PlexServer, device_name: str) -> list[dict[str, Any]]:
    """Return safe session fields only; omit connection URLs and token data."""
    try:
        sessions = server.sessions()
    except Exception as exc:
        return [{"query": "failed", "error_type": type(exc).__name__}]
    result = []
    for session in sessions:
        player = getattr(session, "player", None)
        player_title = str(getattr(player, "title", "") or "")
        if device_name.casefold() not in player_title.casefold():
            continue
        decisions = []
        for media in getattr(session, "media", []) or []:
            for part in getattr(media, "parts", []) or []:
                for stream in getattr(part, "streams", []) or []:
                    decision = getattr(stream, "decision", None)
                    if decision:
                        decisions.append(str(decision))
        result.append({
            "player_found": True,
            "player_product": getattr(player, "product", None),
            "state": getattr(session, "state", None),
            "title": getattr(session, "title", None),
            "view_offset_ms": getattr(session, "viewOffset", None),
            "stream_decisions": sorted(set(decisions)),
        })
    return result


def _safe_volume(
    cast: pychromecast.Chromecast,
    configured_max: float,
    original: float | None,
) -> tuple[float, float]:
    if original is None or not math.isfinite(original) or not 0 <= original <= 1:
        raise ProbeFailure("Fresh receiver volume is unavailable; refusing playback")
    target = min(original, 0.12, float(configured_max), 0.85)
    if target != original:
        cast.set_volume(target)
        if not _wait_receiver_volume(cast, target):
            raise ProbeFailure("Could not verify reduced test volume")
    return original, target


def _wait_for_media_change(cast: pychromecast.Chromecast, previous: dict[str, Any], timeout: float = 20) -> tuple[dict[str, Any] | None, float | None]:
    start = time.monotonic()
    deadline = start + timeout
    previous_key = previous.get("content_id") or previous.get("title")
    while time.monotonic() < deadline:
        snapshot = _refresh_media(cast, timeout=2)
        current_key = snapshot.get("content_id") or snapshot.get("title")
        if (
            snapshot["fresh_status"]
            and current_key
            and current_key != previous_key
            and snapshot["player_state"] == "PLAYING"
        ):
            return snapshot, time.monotonic() - start
        time.sleep(0.25)
    return None, None


def _position_advance(playing: dict[str, Any], later: dict[str, Any]) -> float | None:
    """Return advancing seconds only when both samples are fresh PLAYING states."""
    if (
        not playing.get("fresh_status")
        or playing.get("player_state") != "PLAYING"
        or not later.get("fresh_status")
        or later.get("player_state") != "PLAYING"
    ):
        return None
    first = playing.get("position")
    second = later.get("position")
    if (
        isinstance(first, bool)
        or isinstance(second, bool)
        or not isinstance(first, (int, float))
        or not isinstance(second, (int, float))
        or not math.isfinite(first)
        or not math.isfinite(second)
    ):
        return None
    return round(float(second) - float(first), 3)


def _run_case(
    *,
    server: PlexServer,
    section_name: str,
    cast: pychromecast.Chromecast,
    controller: ObservedPlexController | None,
    device_name: str,
    engine: str,
    kind: str,
    selection: str,
    configured_max: float,
    initial_volume: float | None,
    dwell_seconds: float,
) -> dict[str, Any]:
    media, expected_queue_size = _build_media(server, section_name, kind, selection)
    original_volume, safe_volume = _safe_volume(cast, configured_max, initial_volume)
    output: dict[str, Any] = {
        "case": kind,
        "selection": selection,
        "expected_queue_size": expected_queue_size,
        "volume_original": round(original_volume, 3),
        "volume_test": round(safe_volume, 3),
        "events": [],
        "cleanup": "not_started",
    }
    did_start = False
    stop_confirmed = False
    bridge_queue: SpeakerQueue | None = None

    def event(name: str, **values: Any) -> None:
        output["events"].append({"event": name, "at_utc": datetime.now(UTC).isoformat(), **values})

    try:
        t0 = time.monotonic()
        # The receiver may launch even if sending/awaiting the first controller
        # response raises. From this point, always run the verified stop cleanup.
        did_start = True
        if engine == "native":
            if controller is None:
                raise ProbeFailure("Native Plex controller was not registered")
            controller.play_media(media)
            controller.play_media_event.wait(20)
            event("native_play_command_sent", controller_send_completed=controller.play_media_event.is_set())
        else:
            if getattr(media, "TYPE", None) == "playqueue":
                bridge_tracks = list(media.items)
            elif kind == "album":
                bridge_tracks = list(media.tracks())
            else:
                bridge_tracks = [media]
            bridge_queue = SpeakerQueue(
                cast,
                default_volume=min(safe_volume, 0.12),
                max_volume=min(configured_max, 0.12),
            )
            bridge_queue.replace(bridge_tracks, repeat=kind == "artist-shuffle")
            event("bridge_queue_loaded", queue_size=len(bridge_tracks))
        playing = _wait_state(cast, {"PLAYING"}, timeout=30)
        if playing is None:
            event("playing_not_observed", receiver_app_id=getattr(cast.status, "app_id", None))
            return output
        first_playing = time.monotonic()
        event("playing_observed", startup_latency_seconds=round(first_playing - t0, 3), **{
            key: playing[key] for key in ("player_state", "title", "artist", "album", "position", "duration", "artwork")
        })
        output["plex_sessions"] = _session_snapshot(server, device_name)

        if dwell_seconds:
            time.sleep(dwell_seconds)
        later = _refresh_media(cast)
        position_delta = _position_advance(playing, later)
        if position_delta is not None:
            output["position_advance_seconds"] = position_delta
            output["position_advance_confirmed"] = position_delta > 0
        else:
            output["position_advance_confirmed"] = False
        event(
            "position_sample",
            fresh_status=later["fresh_status"],
            state=later["player_state"],
            position=later.get("position") if later["fresh_status"] else None,
            advance_seconds=position_delta,
        )
        output["audible_confirmation_required"] = True

        if expected_queue_size and expected_queue_size > 1:
            before_next = _status_snapshot(cast)
            next_start = time.monotonic()
            if engine == "native":
                controller.next()
            else:
                bridge_queue.skip()
            next_status, next_latency = _wait_for_media_change(cast, before_next)
            output["next"] = {
                "track_changed": next_status is not None,
                "transition_latency_seconds": round(next_latency, 3) if next_latency is not None else None,
                "title": next_status.get("title") if next_status else None,
            }
            event("next_checked", elapsed_seconds=round(time.monotonic() - next_start, 3), **output["next"])
            if next_status:
                prev_start = time.monotonic()
                if engine == "native":
                    controller.previous()
                else:
                    bridge_queue.previous()
                previous_status, previous_latency = _wait_for_media_change(cast, next_status)
                output["previous"] = {
                    "track_changed": previous_status is not None,
                    "transition_latency_seconds": round(previous_latency, 3) if previous_latency is not None else None,
                    "title": previous_status.get("title") if previous_status else None,
                }
                event("previous_checked", elapsed_seconds=round(time.monotonic() - prev_start, 3), **output["previous"])

        if engine == "native":
            controller.pause()
        else:
            bridge_queue.pause()
        paused = _wait_state(cast, {"PAUSED"}, timeout=12)
        output["pause_confirmed"] = paused is not None
        event("pause_checked", confirmed=paused is not None)
        if paused:
            if engine == "native":
                controller.play()
            else:
                bridge_queue.resume()
            resumed = _wait_state(cast, {"PLAYING"}, timeout=12)
            output["resume_confirmed"] = resumed is not None
            event("resume_checked", confirmed=resumed is not None)

        output["plex_sessions_after_transport"] = _session_snapshot(server, device_name)
    except Exception as exc:
        # Do not include exception text: PlexAPI and controller exceptions can
        # carry URLs or serialized authenticated request bodies.
        output["error_type"] = type(exc).__name__
    finally:
        if did_start:
            try:
                if engine == "native":
                    controller.stop()
                elif bridge_queue is not None:
                    # SpeakerQueue.stop() normally restores its volume session
                    # in its own finally block, even after an unconfirmed
                    # stop. Suppress that restoration so this probe alone
                    # restores the original level after fresh stop evidence.
                    with bridge_queue.lock:
                        bridge_queue._saved_volume = None
                    bridge_queue.stop()
                else:
                    raise ProbeFailure("Bridge queue did not initialize")
                stopped = _wait_state(cast, {"IDLE", "STOPPED"}, timeout=15)
                if stopped is None:
                    deadline = time.monotonic() + 12
                    while time.monotonic() < deadline:
                        receiver_result = _StatusCallback(threading.Event())
                        cast.socket_client.receiver_controller.update_status(
                            callback_function=receiver_result.callback
                        )
                        receiver_result.event.wait(2)
                        receiver_data = _fresh_receiver_response(receiver_result)
                        media = _status_snapshot(cast)
                        if media["session_id"] is None and receiver_data and (
                            receiver_data["app_id"] in (
                                None, DEFAULT_MEDIA_RECEIVER_APP_ID, IDLE_APP_ID
                            )
                            and receiver_data["standby"] is True
                        ):
                            stopped = media
                            break
                        time.sleep(0.5)
                stop_confirmed = stopped is not None
                output["cleanup"] = "stopped" if stop_confirmed else "stop_unconfirmed"
                event("stop_checked", confirmed=stop_confirmed)
            except Exception as exc:
                output["cleanup"] = "stop_error"
                output["cleanup_error_type"] = type(exc).__name__
            if stop_confirmed and getattr(cast.socket_client, "is_connected", False):
                try:
                    output["volume_restored"] = _restore_original_volume(cast, original_volume)
                except Exception as exc:
                    output["volume_restored"] = False
                    output["volume_restore_error_type"] = type(exc).__name__
                if not output["volume_restored"]:
                    output["volume_restore_note"] = "restore was not confirmed by fresh receiver status"
            elif not stop_confirmed:
                output["volume_restored"] = False
                output["volume_restore_note"] = "left at safe test level because stop was not verified"
            if bridge_queue is not None:
                try:
                    bridge_queue.close()
                except Exception as exc:
                    output["bridge_queue_close_error_type"] = type(exc).__name__
        else:
            output["cleanup"] = "playback_not_started"
            if getattr(cast.socket_client, "is_connected", False):
                try:
                    output["volume_restored"] = _restore_original_volume(cast, original_volume)
                except Exception:
                    output["volume_restored"] = False
    return output


def _inventory(server: PlexServer, section_name: str) -> dict[str, Any]:
    section = server.library.section(section_name)
    return {
        "music_section": section_name,
        "tracks": len(section.all(libtype="track")),
        "albums": len(section.all(libtype="album")),
        "artists": len(section.all(libtype="artist")),
        "playlists": len(server.playlists()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-uuid", required=True, help="exact configured Cast UUID")
    parser.add_argument("--inventory", action="store_true", help="read-only Plex library counts")
    parser.add_argument("--engine", choices=("native", "bridge"), default="native",
                        help="native PlexController or existing bridge SpeakerQueue")
    parser.add_argument("--case", choices=("track", "album", "artist-shuffle", "playlist", "station"))
    parser.add_argument("--selection", help="exact library title; omitted from errors and never logged")
    parser.add_argument("--allow-disruptive", action="store_true", help="explicitly authorize playback on the selected receiver")
    parser.add_argument("--confirm-physical-idle", action="store_true", help="confirm in person that UNKNOWN state is silent and idle")
    parser.add_argument("--dwell-seconds", type=float, default=8.0)
    args = parser.parse_args(argv)

    if args.case and (not args.allow_disruptive or not args.selection):
        parser.error("--case requires --allow-disruptive and --selection")
    if args.dwell_seconds < 0 or args.dwell_seconds > 30:
        parser.error("--dwell-seconds must be between 0 and 30")
    pychromecast_logger = logging.getLogger("pychromecast")
    pychromecast_logger.setLevel(logging.WARNING)
    logging.getLogger("plexapi").setLevel(logging.WARNING)

    cast = None
    try:
        row = _select_record(args.device_uuid)
        host_info, verified_uuid, is_group = _device_info(row)
        server, section_name = _plex_connection()
        cast = pychromecast.get_chromecast_from_host(host_info, tries=1, retry_wait=1, timeout=6)
        cast.wait(timeout=8)
        if not cast.socket_client.is_connected:
            raise ProbeFailure("Cast socket did not connect")
        snapshot = _fresh_cast_status(cast)
        groups = _group_inventory(verified_uuid, str(row.get("speaker_name") or row.get("name") or "Cast"))
        classification = _classify(snapshot, groups)
        result: dict[str, Any] = {
            "probe": "native-plex-receiver",
            "pychromecast_version": __import__("importlib.metadata", fromlist=["version"]).version("PyChromecast"),
            "device": {
                "name": row.get("speaker_name") or row.get("name"),
                "model": row.get("model"),
                "uuid_verified": True,
                "is_group": is_group,
            },
            "receiver": snapshot,
            "group_context": groups,
            "cold_start_classification": classification,
            "native_plex_app_id": NATIVE_PLEX_APP_ID,
        }
        if args.inventory:
            result["plex_inventory"] = _inventory(server, section_name)

        if args.case:
            allowed_states = {"idle"}
            if (
                args.confirm_physical_idle
                and classification == "indeterminate"
                and _physical_idle_confirmation_can_override(
                    snapshot, groups, is_group=is_group
                )
            ):
                allowed_states.add("indeterminate")
            if classification not in allowed_states:
                raise ProbeFailure(f"Playback refused by idle/group safety gate ({classification})")
            if is_group and not args.confirm_physical_idle:
                raise ProbeFailure("Cast Group playback also requires --confirm-physical-idle")
            controller = None
            if args.engine == "native":
                controller = ObservedPlexController()
                cast.register_handler(controller)
            result["experiment"] = _run_case(
                server=server,
                section_name=section_name,
                cast=cast,
                controller=controller,
                device_name=str(row.get("speaker_name") or row.get("name") or ""),
                engine=args.engine,
                kind=args.case,
                selection=args.selection,
                configured_max=Settings().max_cast_volume,
                initial_volume=snapshot.get("receiver_volume_level"),
                dwell_seconds=args.dwell_seconds,
            )
            result["experiment"]["engine"] = args.engine
        print(json.dumps(result, sort_keys=True))
        return 0
    except ProbeFailure as exc:
        print(json.dumps({"probe": "native-plex-receiver", "result": "blocked", "reason": str(exc)}))
        return 2
    except Exception as exc:
        frames = traceback.extract_tb(exc.__traceback__)
        print(json.dumps({
            "probe": "native-plex-receiver",
            "result": "failed",
            "error_type": type(exc).__name__,
            "trace": [f"{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno}:{frame.name}" for frame in frames],
        }))
        return 1
    finally:
        if cast is not None:
            try:
                cast.disconnect()
            except Exception:
                pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
    raise SystemExit(main())
