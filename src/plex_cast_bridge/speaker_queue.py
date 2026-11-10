"""Cast speaker playback queue and media status callbacks."""

import logging
import random
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from pychromecast.controllers.media import MediaStatusListener

from . import activity

log = logging.getLogger(__name__)

DEFAULT_CAST_VOLUME = 0.40


def track_art_url(track) -> str | None:
    """Return a Plex thumb URL for Cast metadata (may include token; not for browsers)."""
    if track is None:
        return None
    thumb = getattr(track, "thumbUrl", None)
    if thumb:
        return thumb
    if getattr(track, "thumb", None) and hasattr(track, "_server"):
        try:
            return track._server.url(track.thumb, includeToken=True)
        except Exception:
            return None
    return None


def track_art_proxy_path(track) -> str | None:
    """Browser-safe artwork path served by the bridge (no Plex token in the URL)."""
    if track is None:
        return None
    key = getattr(track, "ratingKey", None)
    if key is None:
        return None
    return f"/api/artwork/{key}"


def _browser_art(track) -> str | None:
    proxy = track_art_proxy_path(track)
    if proxy:
        return proxy
    raw = track_art_url(track)
    if raw and "X-Plex-Token=" not in str(raw):
        return raw
    return None


def _track_summary(track) -> dict:
    return {
        "rating_key": getattr(track, "ratingKey", None),
        "title": getattr(track, "title", ""),
        "artist": getattr(track, "grandparentTitle", ""),
        "album": getattr(track, "parentTitle", ""),
        "art": _browser_art(track),
        "duration_ms": getattr(track, "duration", None),
    }


class SpeakerQueue(MediaStatusListener):
    def __init__(self, cast, default_volume=DEFAULT_CAST_VOLUME, max_volume: float = 1.0):
        self.cast = cast
        self.default_volume = default_volume
        self.max_volume = max(0.0, min(1.0, float(max_volume)))
        self.queue = deque()
        self.history = deque(maxlen=20)
        self.original = []
        self.current = None
        self.repeat = False
        self.armed = False
        self.generation = 0
        self.play_generation = 0
        self.pending_load_generation: int | None = None
        self.expected_content_id: str | None = None
        self.current_load_item_id: int | None = None
        self.active_media_session_id: int | None = None
        self.rejected_load_item_ids: set[int] = set()
        self.unidentified_superseded_count = 0
        self.ignore_load_failures = False
        self.load_watchdog_seconds = 8.0
        self.lock = threading.RLock()
        self._closed = False
        self._watchdogs: set[threading.Timer] = set()
        self._saved_volume: float | None = None
        self._receiver_app_seen = False
        self._last_observed_state: str | None = None
        self._native_revision = 0
        self._native_pending = False
        self._native_item_id: int | None = None
        self._native_content_id: str | None = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cast-queue")
        cast.media_controller.register_status_listener(self)
        register = getattr(cast, "register_status_listener", None)
        if callable(register):
            register(self)

    def _begin_volume_session(self):
        if self._saved_volume is not None or self.default_volume is None:
            return
        volume = getattr(getattr(self.cast, "status", None), "volume_level", None)
        if not isinstance(volume, (int, float)) or not 0 <= volume <= 1:
            log.warning("Receiver volume unavailable on '%s'; leaving it unchanged", self.cast.name)
            return
        self._saved_volume = float(volume)
        try:
            self._send_session_volume(max(0.0, min(float(self.default_volume), self.max_volume, 1.0)))
        except Exception:
            self._saved_volume = None
            log.exception("Could not set playback volume on '%s'", self.cast.name)

    def _send_session_volume(self, volume: float):
        # The queue lock is held here. Waiting for a Cast reply can deadlock the
        # socket thread when it is delivering a media status callback to us.
        receiver = getattr(getattr(self.cast, "socket_client", None), "receiver_controller", None)
        if receiver is not None:
            receiver.send_message({"type": "SET_VOLUME", "volume": {"level": volume}})
        else:
            self.cast.set_volume(volume)

    def _restore_volume(self):
        volume = self._saved_volume
        if volume is None:
            return
        try:
            self._send_session_volume(volume)
        except Exception:
            log.exception("Could not restore receiver volume on '%s'", self.cast.name)
        else:
            self._saved_volume = None

    def _invalidate_inflight_load(self):
        """Drop unresolved load state and suppress device callbacks during replacement."""
        self._invalidate_native_next(remove=False)
        if self.pending_load_generation is not None and self.current_load_item_id is None:
            # Superseded load never reported an item id; a later LOAD_FAILED may be orphaned.
            self.unidentified_superseded_count += 1
        if self.current_load_item_id is not None:
            self.rejected_load_item_ids.add(self.current_load_item_id)
        self.pending_load_generation = None
        self.expected_content_id = None
        self.current_load_item_id = None
        self.active_media_session_id = None
        self.armed = False
        self.ignore_load_failures = True
        try:
            self._stop_media()
        except Exception as exc:
            log.debug("Media controller stop during invalidate on '%s' (ignored): %s", self.cast.name, exc)

    def _stop_media(self):
        media = self.cast.media_controller
        if getattr(getattr(self.cast, "socket_client", None), "receiver_controller", None) is not None:
            media._send_command({"type": "STOP"}, None)
        else:
            media.stop()

    def _invalidate_native_next(self, *, remove=True):
        self._native_revision += 1
        item_id = self._native_item_id
        self._native_item_id = None
        self._native_content_id = None
        if remove and item_id is not None:
            try:
                self.cast.media_controller._send_command({"type": "QUEUE_REMOVE", "itemIds": [item_id]}, None)
            except Exception:
                log.exception("Could not remove stale receiver queue item on '%s'", self.cast.name)

    def _schedule_native_next(self):
        if self._closed or self._native_pending or self._native_item_id is not None or not self.queue:
            return
        if getattr(getattr(self.cast, "socket_client", None), "receiver_controller", None) is None:
            return
        try:
            self.executor.submit(self._prime_native_next, self.generation, self._native_revision)
        except RuntimeError:
            pass

    def _prime_native_next(self, generation, revision):
        with self.lock:
            if (self._closed or generation != self.generation or revision != self._native_revision
                    or self._native_pending or self._native_item_id is not None or not self.armed or not self.queue):
                return
            track = self.queue[0]
            media = getattr(track, "media", None) or []
            parts = getattr(media[0], "parts", None) or [] if media else []
            if not parts:
                return
            url = track._server.url(parts[0].key, includeToken=True)
            content_type = {"mp3": "audio/mpeg", "flac": "audio/flac", "m4a": "audio/mp4",
                            "aac": "audio/aac", "ogg": "audio/ogg", "wav": "audio/wav"}.get(
                                getattr(media[0], "container", "").lower(), "audio/mpeg")
            metadata = {"metadataType": 3, "title": track.title,
                        "artist": getattr(track, "grandparentTitle", ""),
                        "albumName": getattr(track, "parentTitle", "")}
            self._native_pending = True
            self._native_content_id = url
            media_session = getattr(self.cast.media_controller.status, "media_session_id", None)

            def inserted(success, response):
                with self.lock:
                    self._native_pending = False
                    item_id = None
                    for status in (response or {}).get("status", []):
                        current_id = status.get("currentItemId")
                        for item in status.get("items", []):
                            if item.get("itemId") != current_id and item.get("media", {}).get("contentId") == url:
                                item_id = item.get("itemId")
                    stale = generation != self.generation or revision != self._native_revision
                    if success and item_id is not None and not stale:
                        self._native_item_id = item_id
                    elif (item_id is not None and self.expected_content_id != url
                          and getattr(self.cast.media_controller.status, "media_session_id", None) == media_session):
                        try:
                            self.cast.media_controller._send_command({"type": "QUEUE_REMOVE", "itemIds": [item_id]}, None)
                        except Exception:
                            log.exception("Could not remove stale receiver queue item on '%s'", self.cast.name)
                    if stale:
                        self._schedule_native_next()

            try:
                self.cast.media_controller.play_media(url, content_type, title=track.title, metadata=metadata,
                                                      stream_type="BUFFERED", enqueue=True, callback_function=inserted)
            except Exception:
                self._native_pending = False
                self._native_content_id = None
                log.exception("Could not prepare receiver queue on '%s'", self.cast.name)

    def replace(self, tracks, repeat):
        with self.lock:
            self.generation += 1
            self._invalidate_inflight_load()
            self.queue = deque(tracks)
            self.history.clear()
            self.original = list(tracks)
            self.repeat = repeat
            self._next()

    def _play_track(self, track):
        media_list = getattr(track, "media", None) or []
        parts_list = (getattr(media_list[0], "parts", None) or []) if media_list else []
        if not parts_list:
            log.warning("Skipping track '%s' with missing media parts", getattr(track, "title", "Unknown"))
            self._next()
            return

        self._begin_volume_session()
        self.current = track
        self.armed = False
        self.play_generation = self.generation
        self.pending_load_generation = self.generation
        self.current_load_item_id = None
        self.active_media_session_id = None

        # Build direct LAN stream URL with Plex auth token
        part = parts_list[0]
        url = track._server.url(part.key, includeToken=True)
        self.expected_content_id = url
        content_type = {
            "mp3": "audio/mpeg",
            "flac": "audio/flac",
            "m4a": "audio/mp4",
            "aac": "audio/aac",
            "ogg": "audio/ogg",
            "wav": "audio/wav",
        }.get(getattr(media_list[0], "container", "").lower(), "audio/mpeg")

        thumb = getattr(track, "thumbUrl", None)
        if not thumb and getattr(track, "thumb", None) and hasattr(track, "_server"):
            thumb = track._server.url(track.thumb, includeToken=True)

        metadata = {
            "metadataType": 3,
            "title": track.title,
            "artist": getattr(track, "grandparentTitle", ""),
            "albumName": getattr(track, "parentTitle", ""),
        }
        if thumb:
            metadata["images"] = [{"url": thumb}]

        log.info(
            "Streaming track '%s' by '%s' to '%s' (Queue remaining: %d)",
            track.title,
            metadata["artist"],
            self.cast.name,
            len(self.queue),
        )

        try:
            self.cast.media_controller.play_media(
                url,
                content_type,
                title=track.title,
                thumb=thumb,
                stream_type="BUFFERED",
                metadata=metadata,
            )
        except Exception:
            self.current = None
            self._restore_volume()
            raise
        # Accept failures only for the load we just started (not stop()-induced leftovers).
        self.ignore_load_failures = False
        self._schedule_load_watchdog(self.generation)

    def _schedule_load_watchdog(self, generation: int):
        """Advance if a pending load never arms after orphan failures were consumed.

        Sleep runs on a daemon timer so the single-worker queue executor stays free;
        the generation/pending checks below make superseded timers a no-op.
        """
        delay = self.load_watchdog_seconds
        if delay <= 0:
            return

        def _watch():
            with self.lock:
                self._watchdogs.discard(timer)
                if self._closed or self.pending_load_generation != generation or self.armed:
                    return
                if generation != self.generation:
                    return
                log.warning(
                    "Cast load watchdog fired on '%s' for generation %s; advancing queue",
                    self.cast.name,
                    generation,
                )
                self.pending_load_generation = None
                try:
                    self.executor.submit(self._advance, generation)
                except RuntimeError:
                    log.debug("Cast queue executor already shut down")

        timer = threading.Timer(delay, _watch)
        timer.daemon = True
        self._watchdogs.add(timer)
        timer.start()

    def close(self):
        with self.lock:
            self._closed = True
            self.generation += 1
            self.pending_load_generation = None
            for timer in self._watchdogs:
                timer.cancel()
            self._watchdogs.clear()
        self.executor.shutdown(wait=True, cancel_futures=True)

    def _next(self):
        if self.current is not None:
            self.history.append(self.current)

        while True:
            if not self.queue and self.repeat and self.original:
                items = self.original[:]
                random.shuffle(items)
                self.queue.extend(items)
            if not self.queue:
                self.current = None
                self.armed = False
                self._restore_volume()
                return
            track = self.queue.popleft()
            break

        self._play_track(track)

    def previous(self):
        with self.lock:
            self.generation += 1
            self._invalidate_inflight_load()
            # Check playback position if available
            status = getattr(self.cast.media_controller, "status", None)
            pos = getattr(status, "adjusted_current_time", None)
            if isinstance(pos, (int, float)) and pos > 4.0:
                if self.current:
                    self._play_track(self.current)
                return
            if not self.history:
                if self.current:
                    self._play_track(self.current)
                return
            prev_track = self.history.pop()
            if self.current:
                self.queue.appendleft(self.current)
            self._play_track(prev_track)

    def new_media_status(self, status):
        with self.lock:
            if self._closed or self.current is None:
                return
            state = getattr(status, "player_state", None)
            if state in ("PLAYING", "PAUSED", "BUFFERING", "IDLE") and state != self._last_observed_state:
                self._last_observed_state = state
                activity.record("Cast status", state.lower(), self.cast.name)
            content_id = getattr(status, "content_id", None)
            session_id = getattr(status, "media_session_id", None)
            # Bind Cast queue item id when the receiver reports it (attribute may be
            # present on some status payloads even if MediaStatus typing omits it).
            item_id = getattr(status, "current_item_id", None)
            if item_id is None:
                item_id = getattr(status, "item_id", None)

            if status.player_state in ("PLAYING", "BUFFERING"):
                if (content_id and content_id == self._native_content_id and self.queue
                        and content_id != self.expected_content_id):
                    self.history.append(self.current)
                    self.current = self.queue.popleft()
                    self.expected_content_id = content_id
                    self._native_revision += 1
                    self._native_item_id = None
                    self._native_content_id = None
                    self.play_generation = self.generation
                    activity.record("Cast status", "next track", self.cast.name)
                # Require content_id match when we know what we loaded; identity-less
                # PLAYING must not arm or clear a replacement's pending state.
                if self.expected_content_id and (
                    not content_id or content_id != self.expected_content_id
                ):
                    return
                self.armed = True
                self.pending_load_generation = None
                if session_id is not None:
                    self.active_media_session_id = session_id
                if item_id is not None:
                    self.current_load_item_id = item_id
                    self.rejected_load_item_ids.discard(item_id)
                self._schedule_native_next()
            elif status.player_state == "IDLE" and status.idle_reason == "FINISHED" and self.armed:
                if content_id:
                    if self.expected_content_id and content_id != self.expected_content_id:
                        return
                elif self.active_media_session_id is not None:
                    if session_id != self.active_media_session_id:
                        return
                else:
                    # Armed without a session id only happens after matching content_id.
                    pass
                self.armed = False
                if self._native_item_id is not None:
                    self._wait_for_native_advance(self.play_generation, self.expected_content_id)
                else:
                    log.info("Track finished on '%s'. Advancing queue automatically.", self.cast.name)
                    self.executor.submit(self._advance, self.play_generation)
            elif status.player_state == "IDLE" and status.idle_reason == "CANCELLED" and self.armed:
                if not content_id or content_id != self.expected_content_id:
                    return
                if self.active_media_session_id is not None and session_id != self.active_media_session_id:
                    return
                self.armed = False
                self.executor.submit(self._finish_cancelled, self.play_generation)

    def _wait_for_native_advance(self, generation, previous_content):
        """Give the receiver time to transition before falling back to bridge playback."""
        def check():
            with self.lock:
                self._watchdogs.discard(timer)
                if (self._closed or generation != self.generation
                        or self.expected_content_id != previous_content or self.armed):
                    return
                try:
                    self.executor.submit(self._advance, generation)
                except RuntimeError:
                    pass

        timer = threading.Timer(3.0, check)
        timer.daemon = True
        self._watchdogs.add(timer)
        timer.start()

    def new_cast_status(self, status):
        """Observe receiver ownership changes; callbacks run on the Cast socket thread."""
        with self.lock:
            if self._closed or self.current is None:
                return
            app_id = getattr(status, "app_id", None)
            if app_id == "CC1AD845":
                self._receiver_app_seen = True
                return
            if not self._receiver_app_seen or not self.armed:
                return
            self._receiver_app_seen = False
            generation = self.generation
            try:
                self.executor.submit(self._receiver_session_ended, generation, app_id)
            except RuntimeError:
                pass

    def _receiver_session_ended(self, generation, app_id):
        with self.lock:
            if self._closed or generation != self.generation or self.current is None:
                return
            current_app = getattr(getattr(self.cast, "status", None), "app_id", None)
            if current_app != app_id:
                return
            self.generation += 1
            self.queue.clear()
            self.original = []
            self.current = None
            self.armed = False
            if app_id in (None, "E8C28D3C"):
                activity.record("Cast status", "session ended", self.cast.name)
                self._restore_volume()
            else:
                activity.record("Cast status", "another app took over", self.cast.name)
                self._saved_volume = None

    def _finish_cancelled(self, generation):
        with self.lock:
            if self._closed or generation != self.generation:
                return
            # A different Cast app may already own the device. Never change its volume.
            receiver = getattr(self.cast, "status", None)
            app_id = getattr(receiver, "app_id", None)
            media = getattr(self.cast.media_controller, "status", None)
            if app_id not in (None, "CC1AD845") or getattr(media, "player_state", None) != "IDLE":
                self._saved_volume = None
                return
            self.queue.clear()
            self.original = []
            self.current = None
            self._restore_volume()

    def _advance(self, generation):
        with self.lock:
            if generation != self.generation:
                return
            try:
                self._invalidate_native_next(remove=False)
                self._next()
            except Exception:
                log.exception("Could not advance the Cast queue")

    def load_media_failed(self, queue_item_id, error_code):
        log.error(
            "Cast media load failed on '%s': item=%s code=%s. Automatically advancing to next track.",
            self.cast.name,
            queue_item_id,
            error_code,
        )
        # Known item ids are correlated directly. Unidentified superseded loads contribute
        # an orphan-failure allowance so a delayed A failure cannot claim B's pending slot;
        # a watchdog advances if the current load never arms after orphans are consumed.
        with self.lock:
            if self._closed or self.ignore_load_failures:
                log.info(
                    "Ignoring Cast media load failure on '%s' during queue transition",
                    self.cast.name,
                )
                return
            if queue_item_id in self.rejected_load_item_ids:
                log.info(
                    "Ignoring stale Cast media load failure on '%s' (rejected item %s)",
                    self.cast.name,
                    queue_item_id,
                )
                return
            if self.current_load_item_id is not None and queue_item_id != self.current_load_item_id:
                log.info(
                    "Ignoring stale Cast media load failure on '%s' (item %s != current %s)",
                    self.cast.name,
                    queue_item_id,
                    self.current_load_item_id,
                )
                return
            if self.pending_load_generation is None:
                log.info(
                    "Ignoring Cast media load failure on '%s' (no pending load)",
                    self.cast.name,
                )
                return
            if self.current_load_item_id is None and self.unidentified_superseded_count > 0:
                self.unidentified_superseded_count -= 1
                if queue_item_id is not None:
                    self.rejected_load_item_ids.add(queue_item_id)
                log.info(
                    "Ignoring uncorrelated Cast media load failure on '%s' "
                    "(orphan allowance remaining=%s)",
                    self.cast.name,
                    self.unidentified_superseded_count,
                )
                return
            if self.current_load_item_id is None and queue_item_id is not None:
                self.current_load_item_id = queue_item_id
            generation = self.pending_load_generation
            self.pending_load_generation = None
        self.executor.submit(self._advance, generation)

    def pause(self):
        try:
            self.cast.media_controller.pause()
        except Exception as exc:
            log.warning("Media controller pause error on '%s': %s", self.cast.name, exc)

    def resume(self):
        try:
            self.cast.media_controller.play()
        except Exception as exc:
            log.warning("Media controller resume/play error on '%s': %s", self.cast.name, exc)

    def set_volume(self, volume: float):
        capped = min(float(volume), self.max_volume)
        clamped = max(0.0, min(1.0, capped))
        try:
            self.cast.set_volume(clamped)
        except Exception as exc:
            log.warning("Set volume error on '%s': %s", self.cast.name, exc)

    def adjust_volume(self, delta: float):
        current = getattr(getattr(self.cast, "status", None), "volume_level", self.default_volume or 0.20)
        if current is None:
            current = self.default_volume or 0.20
        self.set_volume(float(current) + float(delta))

    def skip(self):
        with self.lock:
            self.generation += 1
            self._invalidate_inflight_load()
            self._next()

    def next(self):
        self.skip()

    def stop(self):
        stopped = threading.Event()
        accepted = []
        with self.lock:
            self.generation += 1
            generation = self.generation
            self._invalidate_inflight_load()
            self.queue.clear()
            self.original = []
            self.current = None
            try:
                receiver = getattr(getattr(self.cast, "socket_client", None), "receiver_controller", None)
                if receiver is not None:
                    def on_stopped(success, _response):
                        accepted.append(success)
                        stopped.set()

                    receiver.stop_app(callback_function=on_stopped)
                else:
                    self.cast.quit_app()
                    accepted.append(True)
                    stopped.set()
            except Exception as exc:
                log.debug("Cast quit_app error on '%s' (ignored): %s", self.cast.name, exc)
        stopped.wait(timeout=5)
        with self.lock:
            if generation == self.generation and (not accepted or accepted[0]):
                self._restore_volume()

    def queue_snapshot(self) -> list[dict]:
        with self.lock:
            return [_track_summary(t) for t in list(self.queue)]

    def remove_at(self, index: int) -> dict:
        with self.lock:
            if index < 0 or index >= len(self.queue):
                raise ValueError("queue index out of range")
            items = list(self.queue)
            removed = items.pop(index)
            self.queue = deque(items)
            self._invalidate_native_next()
            self._schedule_native_next()
            return _track_summary(removed)

    def move(self, from_index: int, to_index: int) -> None:
        with self.lock:
            items = list(self.queue)
            if from_index < 0 or from_index >= len(items):
                raise ValueError("from_index out of range")
            to_index = max(0, min(len(items) - 1, to_index))
            item = items.pop(from_index)
            items.insert(to_index, item)
            self.queue = deque(items)
            self._invalidate_native_next()
            self._schedule_native_next()

    def play_next(self, track) -> None:
        with self.lock:
            self.queue.appendleft(track)
            self._invalidate_native_next()
            self._schedule_native_next()

    def add_end(self, track) -> None:
        with self.lock:
            self.queue.append(track)
            self._schedule_native_next()

    def clear_upcoming(self) -> int:
        with self.lock:
            n = len(self.queue)
            self.queue.clear()
            self._invalidate_native_next()
            return n

    def status(self):
        with self.lock:
            track = self.current
            try:
                player_state = getattr(self.cast.media_controller.status, "player_state", "UNKNOWN")
            except Exception:
                player_state = "UNKNOWN"
            try:
                volume = getattr(getattr(self.cast, "status", None), "volume_level", None)
            except Exception:
                volume = None
            current_track = None
            position_ms = None
            duration_ms = None
            if track:
                current_track = _track_summary(track)
                try:
                    st = self.cast.media_controller.status
                    position_ms = int(float(getattr(st, "current_time", 0) or 0) * 1000)
                    duration_ms = getattr(track, "duration", None)
                    if duration_ms is None and getattr(st, "duration", None):
                        duration_ms = int(float(st.duration) * 1000)
                except Exception:
                    pass
            return {
                "speaker": getattr(self.cast, "name", "unknown"),
                "state": player_state,
                "volume": volume,
                "max_volume": self.max_volume,
                "current_track": current_track,
                "position_ms": position_ms,
                "duration_ms": duration_ms,
                "queue_length": len(self.queue),
                "up_next": [_track_summary(t) for t in list(self.queue)[:25]],
                "repeat": self.repeat,
            }
