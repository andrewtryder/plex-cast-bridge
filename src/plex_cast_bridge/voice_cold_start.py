"""Conservative, opt-in Google Home resume fallback for an idle Cast receiver."""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import activity, catalog_store
from .speaker_registry import find_by_google_home_id, is_cast_group

log = logging.getLogger(__name__)
DEFAULT_MEDIA_RECEIVER_APP_ID = "CC1AD845"
MEDIA_NAMESPACE = "urn:x-cast:com.google.cast.media"
MESSAGE_DEDUPE_TTL_SECONDS = 31 * 24 * 60 * 60
MESSAGE_DEDUPE_MAX_ENTRIES = 2048


@dataclass(frozen=True)
class CastSession:
    classification: str
    connected: bool
    receiver_status_received: bool = False
    media_status_received: bool = False
    receiver_app_id: str | None = None
    receiver_is_standby: bool | None = None
    media_session_id: Any = None
    player_state: str = "UNKNOWN"
    media_content_id: str | None = None
    queue_generation: int | None = None


@dataclass(frozen=True)
class _QueueSnapshot:
    generation: int
    current: bool
    queue_length: int
    pending_load_generation: int | None
    expected_content_id: str | None
    active_media_session_id: int | None
    armed: bool

    @property
    def has_unresolved_queue_state(self) -> bool:
        return bool(
            self.current
            or self.queue_length
            or self.pending_load_generation is not None
            or self.expected_content_id
            or self.active_media_session_id is not None
            or self.armed
        )


def _queue_snapshot(speaker) -> _QueueSnapshot:
    lock = getattr(speaker, "lock", None)
    if lock is None:
        return _QueueSnapshot(0, False, 0, None, None, None, False)
    with lock:
        return _QueueSnapshot(
            generation=int(getattr(speaker, "generation", 0)),
            current=getattr(speaker, "current", None) is not None,
            queue_length=len(getattr(speaker, "queue", ()) or ()),
            pending_load_generation=getattr(speaker, "pending_load_generation", None),
            expected_content_id=getattr(speaker, "expected_content_id", None),
            active_media_session_id=getattr(speaker, "active_media_session_id", None),
            armed=bool(getattr(speaker, "armed", False)),
        )


@dataclass
class _CallbackResult:
    event: threading.Event
    success: bool | None = None
    response: dict[str, Any] | None = None

    def callback(self, success: bool, response: dict[str, Any] | None) -> None:
        self.success = success is True
        self.response = response if isinstance(response, dict) else None
        self.event.set()


def _fresh_receiver_response(result: _CallbackResult) -> dict[str, Any] | None:
    response = result.response
    if not result.success or not response or response.get("type") != "RECEIVER_STATUS":
        return None
    status = response.get("status")
    if not isinstance(status, dict) or not isinstance(status.get("applications"), list):
        return None
    standby = status.get("isStandBy")
    if not isinstance(standby, bool):
        return None
    applications = status["applications"]
    if len(applications) > 1 or any(not isinstance(app, dict) for app in applications):
        return None
    app = applications[0] if applications else {}
    app_id = app.get("appId")
    if app_id is not None and not isinstance(app_id, str):
        return None
    return {"app_id": app_id, "standby": standby}


def _fresh_media_response(result: _CallbackResult) -> dict[str, Any] | None:
    response = result.response
    if not result.success or not response or response.get("type") != "MEDIA_STATUS":
        return None
    statuses = response.get("status")
    if not isinstance(statuses, list) or len(statuses) > 1:
        return None
    if not statuses:
        return {"session_id": None, "player_state": "IDLE", "content_id": None}
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


def inspect_cast_session(speaker, timeout: float = 4.0) -> CastSession:
    """Request status from the existing connection without launching a receiver."""
    cast = getattr(speaker, "cast", None)
    socket_client = getattr(cast, "socket_client", None)
    if cast is None or socket_client is None or not getattr(socket_client, "is_connected", False):
        return CastSession("disconnected", False)

    receiver_result = _CallbackResult(threading.Event())
    media_result = _CallbackResult(threading.Event())
    try:
        # ReceiverController targets the platform namespace and cannot launch apps.
        cast.socket_client.receiver_controller.update_status(
            callback_function=receiver_result.callback
        )
        namespaces = getattr(socket_client, "app_namespaces", ()) or ()
        media_controller = cast.media_controller
        # BaseController.send_message launches its supporting app when the
        # namespace is missing. Never call it unless the current app advertises it.
        if getattr(media_controller, "namespace", MEDIA_NAMESPACE) not in namespaces:
            return CastSession("indeterminate", True)
        media_controller.update_status(callback_function=media_result.callback)
    except Exception as exc:
        log.info("Voice cold start status request failed (%s)", type(exc).__name__)
        return CastSession("indeterminate", True)

    deadline = time.monotonic() + max(0.0, timeout)
    receiver_result.event.wait(max(0.0, deadline - time.monotonic()))
    media_result.event.wait(max(0.0, deadline - time.monotonic()))
    if not getattr(socket_client, "is_connected", False):
        return CastSession("disconnected", False, receiver_result.event.is_set(), media_result.event.is_set())

    receiver = _fresh_receiver_response(receiver_result)
    media = _fresh_media_response(media_result)
    if receiver is None or media is None:
        return CastSession(
            "indeterminate",
            True,
            receiver_result.event.is_set() and receiver is not None,
            media_result.event.is_set() and media is not None,
        )

    queue = _queue_snapshot(speaker)
    session_id = media["session_id"]
    player_state = media["player_state"]
    owns_media = bool(
        session_id is not None
        and queue.current
        and queue.pending_load_generation is None
        and queue.active_media_session_id is not None
        and str(queue.active_media_session_id) == str(session_id)
        and queue.expected_content_id
        and media["content_id"] == queue.expected_content_id
    )

    group_state = getattr(speaker, "cast_group_state", None)
    group_uuid = getattr(cast, "uuid", None)
    if group_state is not None:
        group_classification = group_state.classify(group_uuid)
        if group_classification == "active":
            return CastSession(
                "other_media_active", True, True, True, receiver["app_id"],
                receiver["standby"], session_id, player_state,
                media["content_id"], queue.generation,
            )
        if group_classification != "clear":
            return CastSession(
                "indeterminate", True, True, True, receiver["app_id"],
                receiver["standby"], session_id, player_state,
                media["content_id"], queue.generation,
            )

    if session_id is not None:
        if player_state == "PAUSED":
            classification = "plex_paused" if owns_media else "other_media_paused"
        elif player_state in {"PLAYING", "BUFFERING"}:
            classification = "plex_active" if owns_media else "other_media_active"
        else:
            classification = "indeterminate"
    elif queue.has_unresolved_queue_state:
        classification = "indeterminate"
    elif receiver["app_id"] and receiver["app_id"] != DEFAULT_MEDIA_RECEIVER_APP_ID:
        classification = "other_app_active" if receiver["standby"] is False else "indeterminate"
    elif (
        receiver["app_id"] == DEFAULT_MEDIA_RECEIVER_APP_ID
        and receiver["standby"] is True
    ):
        classification = "idle"
    else:
        classification = "indeterminate"

    return CastSession(
        classification=classification,
        connected=True,
        receiver_status_received=True,
        media_status_received=True,
        receiver_app_id=receiver["app_id"],
        receiver_is_standby=receiver["standby"],
        media_session_id=session_id,
        player_state=player_state,
        media_content_id=media["content_id"],
        queue_generation=queue.generation,
    )


class PubSubMessageDeduper:
    """Persistent, bounded message-ID reservations for at-most-once Cast loads."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        ttl_seconds: int = MESSAGE_DEDUPE_TTL_SECONDS,
        max_entries: int = MESSAGE_DEDUPE_MAX_ENTRIES,
    ):
        self.path = path or (catalog_store.DATA_DIR / "voice_cold_start_dedupe.json")
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._lock = threading.Lock()

    def is_reserved(self, message_id: str) -> bool:
        if not message_id:
            return False
        with self._lock:
            if not self.path.exists():
                return False
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    return True
                timestamp = raw.get(message_id)
                return isinstance(timestamp, (int, float)) and time.time() - float(timestamp) < self.ttl_seconds
            except Exception as exc:
                log.error("Could not read Pub/Sub cold-start dedupe state (error=%s)", type(exc).__name__)
                return True

    def reserve(self, message_id: str) -> bool:
        """Persist an ID before playback. False means duplicate or persistence error."""
        if not message_id:
            return True
        now = time.time()
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
                if not isinstance(raw, dict):
                    raw = {}
                entries = {
                    key: float(timestamp)
                    for key, timestamp in raw.items()
                    if isinstance(key, str)
                    and isinstance(timestamp, (int, float))
                    and now - float(timestamp) < self.ttl_seconds
                }
                if message_id in entries:
                    return False
                entries[message_id] = now
                if len(entries) > self.max_entries:
                    entries = dict(
                        sorted(entries.items(), key=lambda item: item[1])[-self.max_entries :]
                    )
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(self.path.suffix + ".tmp")
                tmp.write_text(json.dumps(entries, separators=(",", ":")) + "\n", encoding="utf-8")
                os.replace(tmp, self.path)
                try:
                    self.path.chmod(0o600)
                except OSError:
                    pass
                return True
            except Exception as exc:
                log.error("Could not reserve Pub/Sub message ID for cold start (error=%s)", type(exc).__name__)
                return False


class VoiceColdStart:
    """Uses live Plex state, per-device serialization, and bounded redelivery dedupe."""

    def __init__(
        self,
        *,
        cast,
        plex_provider: Callable[[], Any],
        inspector=inspect_cast_session,
        message_deduper: PubSubMessageDeduper | None = None,
        duplicate_window: float = 20.0,
    ):
        self.cast = cast
        self.plex_provider = plex_provider
        self.inspector = inspector
        self.message_deduper = message_deduper or PubSubMessageDeduper()
        self.duplicate_window = duplicate_window
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._started: dict[str, float] = {}

    def resume(self, device_id: str, message_id: str | None = None):
        record = find_by_google_home_id(catalog_store.list_speaker_records(), device_id)
        if not record or not record.get("voice_cold_start_enabled", False):
            log.info("Voice cold start disabled for device_id=%s", device_id)
            activity.record("Google Home", "voice cold start disabled")
            return None
        if (
            record.get("is_group")
            or str(record.get("kind") or "").strip().casefold() == "group"
            or is_cast_group(record.get("model"), record.get("speaker_name") or record.get("name"))
        ):
            log.info("Voice cold start skipped for Cast Group target device_id=%s", device_id)
            activity.record("Google Home", "voice cold start skipped: group target", record.get("speaker_name"))
            return None
        with self._guard:
            lock = self._locks.setdefault(device_id, threading.Lock())
        with lock:
            if message_id and self.message_deduper.is_reserved(message_id):
                log.info("Voice cold start duplicate Pub/Sub message suppressed for device_id=%s", device_id)
                activity.record(
                    "Google Home", "duplicate Pub/Sub cold start suppressed", record.get("speaker_name")
                )
                return {"voice_cold_start": "duplicate_suppressed"}

            activity.record("Google Home", "voice cold start requested", record.get("speaker_name"))
            # CastManager enforces stable identity, enabled and Google Home visibility.
            speaker = self.cast.get_device(device_id)
            state = self.inspector(speaker)
            if state.classification == "plex_paused":
                speaker.resume()
                log.info("Voice resume preserved paused Plex session for device_id=%s", device_id)
                activity.record("Google Home", "existing Plex session resumed", record.get("speaker_name"))
                return {"voice_cold_start": "resumed_existing"}
            if state.classification == "plex_active":
                log.info("Voice resume preserved active Plex session for device_id=%s", device_id)
                activity.record("Google Home", "active Plex session preserved", record.get("speaker_name"))
                return {"voice_cold_start": "already_active"}
            if state.classification != "idle":
                log.info("Voice cold start skipped state=%s device_id=%s", state.classification, device_id)
                activity.record("Google Home", f"voice cold start skipped: {state.classification}", record.get("speaker_name"))
                return {"voice_cold_start": f"skipped_{state.classification}"}

            if time.monotonic() - self._started.get(device_id, 0) < self.duplicate_window:
                log.info("Voice cold start distinct request cooldown for device_id=%s", device_id)
                activity.record("Google Home", "voice cold start duplicate suppressed", record.get("speaker_name"))
                return {"voice_cold_start": "duplicate_suppressed"}

            plex = self.plex_provider()
            if plex is None:
                log.info("Voice cold start skipped because Plex is disconnected, device_id=%s", device_id)
                return {"voice_cold_start": "plex_unavailable"}
            initial_generation = state.queue_generation
            try:
                tracks = self._selection(plex, record)
            except Exception as exc:
                log.warning("Voice cold start selection failed device_id=%s error=%s", device_id, type(exc).__name__)
                return {"voice_cold_start": "selection_failed"}
            if self.plex_provider() is not plex:
                log.info("Voice cold start discarded selection from replaced Plex connection device_id=%s", device_id)
                return {"voice_cold_start": "plex_changed"}
            if not tracks:
                log.warning("Voice cold start selection empty device_id=%s", device_id)
                return {"voice_cold_start": "selection_empty"}

            # Fresh status after Plex resolution closes the long selection window.
            state = self.inspector(speaker)
            if state.classification != "idle":
                log.info("Voice cold start revalidation skipped state=%s device_id=%s", state.classification, device_id)
                return {"voice_cold_start": f"revalidation_{state.classification}"}
            if state.queue_generation != initial_generation:
                log.info("Voice cold start skipped after local queue change device_id=%s", device_id)
                return {"voice_cold_start": "queue_changed"}
            if self.plex_provider() is not plex:
                return {"voice_cold_start": "plex_changed"}

            queue_lock = getattr(speaker, "lock", None)
            if queue_lock is None:
                return {"voice_cold_start": "queue_unavailable"}
            with queue_lock:
                queue = _queue_snapshot(speaker)
                if queue.generation != initial_generation or queue.has_unresolved_queue_state:
                    return {"voice_cold_start": "queue_changed"}
                media_status = getattr(speaker.cast.media_controller, "status", None)
                if (
                    getattr(media_status, "media_session_id", None) is not None
                    or str(getattr(media_status, "player_state", "UNKNOWN")).upper()
                    in {"PLAYING", "PAUSED", "BUFFERING"}
                ):
                    return {"voice_cold_start": "cast_state_changed"}
                receiver_status = getattr(speaker.cast, "status", None)
                if (
                    getattr(receiver_status, "app_id", None) != state.receiver_app_id
                    or getattr(receiver_status, "is_stand_by", None) != state.receiver_is_standby
                ):
                    return {"voice_cold_start": "cast_state_changed"}
                group_state = getattr(speaker, "cast_group_state", None)
                if group_state is not None:
                    group_classification = group_state.classify(
                        getattr(speaker.cast, "uuid", None)
                    )
                    if group_classification != "clear":
                        return {
                            "voice_cold_start": f"group_{group_classification}"
                        }
                if self.plex_provider() is not plex:
                    return {"voice_cold_start": "plex_changed"}
                # Reserve durable message identity immediately before the side
                # effect. This keeps redelivery at-most-once after ack loss.
                if message_id and not self.message_deduper.reserve(message_id):
                    return {"voice_cold_start": "duplicate_suppressed"}
                self._started[device_id] = time.monotonic()
                try:
                    # SpeakerQueue.replace uses this same RLock and sends Cast
                    # commands asynchronously without waiting for status callbacks.
                    speaker.replace(tracks, repeat=False)
                except Exception as exc:
                    log.warning("Voice cold start failed device_id=%s error=%s", device_id, type(exc).__name__)
                    activity.record("Google Home", "voice cold start failed", record.get("speaker_name"))
                    return {"voice_cold_start": "failed"}
            log.info("Voice cold start initiated device_id=%s track_count=%d", device_id, len(tracks))
            activity.record("Google Home", "voice cold start initiated", record.get("speaker_name"))
            return {"voice_cold_start": "initiated", "track_count": len(tracks)}

    def _selection(self, plex, record):
        selection = record.get("voice_cold_start_selection") or "shuffle"
        if selection == "shuffle":
            return plex.library.search(libtype="track", sort="random", maxresults=25)
        if selection == "artist":
            artist_name = str(record.get("voice_cold_start_artist") or "").strip()
            matches = [
                artist
                for artist in getattr(plex, "artists", ())
                if str(getattr(artist, "title", "")).casefold() == artist_name.casefold()
            ]
            if not artist_name or len(matches) != 1:
                raise ValueError("Preferred artist is missing or ambiguous")
            tracks = list(matches[0].tracks(maxresults=25))
            random.shuffle(tracks)
            return tracks
        raise ValueError("Unsupported voice cold start selection")
