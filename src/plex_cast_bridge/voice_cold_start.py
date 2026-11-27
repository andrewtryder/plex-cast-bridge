"""Conservative, opt-in Google Home resume fallback for an idle Cast receiver."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from .catalog_store import list_speaker_records
from .speaker_registry import find_by_google_home_id
from . import activity

log = logging.getLogger(__name__)
DEFAULT_MEDIA_RECEIVER_APP_ID = "CC1AD845"


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


def inspect_cast_session(speaker, timeout: float = 4.0) -> CastSession:
    """Request fresh status from the existing connection; never launch an app."""
    cast = getattr(speaker, "cast", None)
    socket_client = getattr(cast, "socket_client", None)
    if cast is None or socket_client is None or not getattr(socket_client, "is_connected", False):
        return CastSession("disconnected", False)

    receiver_event, media_event = threading.Event(), threading.Event()
    try:
        cast.socket_client.receiver_controller.update_status(
            callback_function=lambda *args: receiver_event.set()
        )
        cast.media_controller.update_status(callback_function=lambda *args: media_event.set())
    except Exception as exc:
        log.info("Voice cold start status request failed (%s)", type(exc).__name__)
        return CastSession("indeterminate", True)

    receiver_received = receiver_event.wait(timeout)
    media_received = media_event.wait(timeout)
    if not getattr(socket_client, "is_connected", False):
        return CastSession("disconnected", False, receiver_received, media_received)

    receiver = getattr(cast, "status", None)
    media = getattr(cast.media_controller, "status", None)
    app_id = getattr(receiver, "app_id", None)
    standby = getattr(receiver, "is_stand_by", None)
    session_id = getattr(media, "media_session_id", None)
    player_state = str(getattr(media, "player_state", "UNKNOWN") or "UNKNOWN").upper()
    queue_status = speaker.status()
    has_plex_content = (
        bool(queue_status.get("current_track")) or int(queue_status.get("queue_length") or 0) > 0
    )

    if session_id not in (None, 0, "0", ""):
        if player_state == "PAUSED":
            classification = "plex_paused" if has_plex_content else "other_media_paused"
        elif player_state in {"PLAYING", "BUFFERING"}:
            classification = "plex_active" if has_plex_content else "other_media_active"
        else:
            classification = "indeterminate"
    elif has_plex_content or str(queue_status.get("state") or "UNKNOWN").upper() in {
        "PLAYING",
        "PAUSED",
        "BUFFERING",
    }:
        classification = "indeterminate"
    elif app_id and app_id != DEFAULT_MEDIA_RECEIVER_APP_ID and standby is not True:
        classification = "other_app_active"
    elif (
        receiver_received
        and media_received
        and app_id == DEFAULT_MEDIA_RECEIVER_APP_ID
        and standby is True
        and session_id in (None, 0, "0", "")
    ):
        classification = "idle"
    else:
        classification = "indeterminate"
    return CastSession(
        classification,
        True,
        receiver_received,
        media_received,
        app_id,
        standby,
        session_id,
        player_state,
    )


class VoiceColdStart:
    """Serializes per-device requests and suppresses Pub/Sub duplicate redelivery."""

    def __init__(
        self,
        *,
        cast,
        plex,
        inspector=inspect_cast_session,
        duplicate_window: float = 20.0,
    ):
        self.cast, self.plex = cast, plex
        self.inspector, self.duplicate_window = inspector, duplicate_window
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._started: dict[str, float] = {}

    def resume(self, device_id: str):
        record = find_by_google_home_id(list_speaker_records(), device_id)
        if not record or not record.get("voice_cold_start_enabled", False):
            log.info("Voice cold start disabled for device_id=%s", device_id)
            activity.record("Google Home", "voice cold start disabled")
            return None
        with self._guard:
            lock = self._locks.setdefault(device_id, threading.Lock())
        with lock:
            activity.record("Google Home", "voice cold start requested", record.get("speaker_name"))
            # CastManager enforces stable identity, enabled and Google Home visibility.
            speaker = self.cast.get_device(device_id)
            state = self.inspector(speaker)
            if state.classification == "plex_paused":
                speaker.resume()
                log.info("Voice resume preserved paused Plex session for device_id=%s", device_id)
                activity.record(
                    "Google Home", "existing Plex session resumed", record.get("speaker_name")
                )
                return {"voice_cold_start": "resumed_existing"}
            if state.classification == "plex_active":
                log.info("Voice resume preserved active Plex session for device_id=%s", device_id)
                activity.record(
                    "Google Home", "active Plex session preserved", record.get("speaker_name")
                )
                return {"voice_cold_start": "already_active"}
            if state.classification != "idle":
                log.info(
                    "Voice cold start skipped state=%s device_id=%s",
                    state.classification,
                    device_id,
                )
                activity.record(
                    "Google Home",
                    f"voice cold start skipped: {state.classification}",
                    record.get("speaker_name"),
                )
                return {"voice_cold_start": f"skipped_{state.classification}"}
            if time.monotonic() - self._started.get(device_id, 0) < self.duplicate_window:
                log.info("Voice cold start duplicate suppressed for device_id=%s", device_id)
                activity.record(
                    "Google Home",
                    "voice cold start duplicate suppressed",
                    record.get("speaker_name"),
                )
                return {"voice_cold_start": "duplicate_suppressed"}
            log.info("Voice cold start confirmed idle device_id=%s", device_id)
            activity.record("Google Home", "receiver confirmed idle", record.get("speaker_name"))
            try:
                tracks = self._selection(record)
            except Exception as exc:
                log.warning(
                    "Voice cold start selection failed device_id=%s error=%s",
                    device_id,
                    type(exc).__name__,
                )
                return {"voice_cold_start": "selection_failed"}
            if not tracks:
                log.warning("Voice cold start selection empty device_id=%s", device_id)
                return {"voice_cold_start": "selection_empty"}
            # Close the inspection-to-load race; don't hijack a session that appeared meanwhile.
            state = self.inspector(speaker)
            if state.classification != "idle":
                log.info(
                    "Voice cold start revalidation skipped state=%s device_id=%s",
                    state.classification,
                    device_id,
                )
                return {"voice_cold_start": f"revalidation_{state.classification}"}
            self._started[device_id] = time.monotonic()
            try:
                speaker.replace(tracks, repeat=False)
            except Exception as exc:
                log.warning(
                    "Voice cold start failed device_id=%s error=%s", device_id, type(exc).__name__
                )
                activity.record(
                    "Google Home", "voice cold start failed", record.get("speaker_name")
                )
                # replace may have reached Cast before the client saw a timeout.
                # Acknowledge this attempt instead of letting Pub/Sub replay it.
                return {"voice_cold_start": "failed"}
            log.info(
                "Voice cold start initiated device_id=%s track_count=%d", device_id, len(tracks)
            )
            activity.record("Google Home", "voice cold start initiated", record.get("speaker_name"))
            return {"voice_cold_start": "initiated", "track_count": len(tracks)}

    def _selection(self, record):
        selection = record.get("voice_cold_start_selection") or "shuffle"
        if selection == "shuffle":
            # Plex performs server-side random ordering and bounds the response size.
            return self.plex.library.search(libtype="track", sort="random", maxresults=25)
        if selection == "artist":
            artist = str(record.get("voice_cold_start_artist") or "").strip()
            if not artist:
                raise ValueError("Preferred artist is not configured")
            return self.plex.library.search(artist=artist, libtype="track", maxresults=25)
        raise ValueError("Unsupported voice cold start selection")
