"""Cast speaker playback queue and media status callbacks."""

import logging
import random
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from pychromecast.controllers.media import MediaStatusListener

log = logging.getLogger(__name__)

DEFAULT_CAST_VOLUME = 0.20


class SpeakerQueue(MediaStatusListener):
    def __init__(self, cast, default_volume=DEFAULT_CAST_VOLUME):
        self.cast = cast
        self.default_volume = default_volume
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
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cast-queue")
        cast.media_controller.register_status_listener(self)

        # Apply startup volume clamp (default 20% to prevent unexpected loud bursts)
        if self.default_volume is not None and hasattr(self.cast, "set_volume"):
            try:
                self.cast.set_volume(self.default_volume)
                log.info("Clamped startup volume to %.0f%% on '%s'", self.default_volume * 100, self.cast.name)
            except Exception as exc:
                log.warning("Could not set startup volume on '%s': %s", self.cast.name, exc)

    def _invalidate_inflight_load(self):
        """Drop unresolved load state and suppress device callbacks during replacement."""
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
            self.cast.media_controller.stop()
        except Exception as exc:
            log.debug("Media controller stop during invalidate on '%s' (ignored): %s", self.cast.name, exc)

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

        self.cast.media_controller.play_media(
            url,
            content_type,
            title=track.title,
            thumb=thumb,
            stream_type="BUFFERED",
            metadata=metadata,
        )
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
                if self.pending_load_generation != generation or self.armed:
                    return
                if generation != self.generation:
                    return
                log.warning(
                    "Cast load watchdog fired on '%s' for generation %s; advancing queue",
                    self.cast.name,
                    generation,
                )
                self.pending_load_generation = None
            self.executor.submit(self._advance, generation)

        timer = threading.Timer(delay, _watch)
        timer.daemon = True
        timer.start()

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
            if self.current is None:
                return
            content_id = getattr(status, "content_id", None)
            session_id = getattr(status, "media_session_id", None)
            # Bind Cast queue item id when the receiver reports it (attribute may be
            # present on some status payloads even if MediaStatus typing omits it).
            item_id = getattr(status, "current_item_id", None)
            if item_id is None:
                item_id = getattr(status, "item_id", None)

            if status.player_state in ("PLAYING", "BUFFERING"):
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
                log.info("Track finished on '%s'. Advancing queue automatically.", self.cast.name)
                self.executor.submit(self._advance, self.play_generation)

    def _advance(self, generation):
        with self.lock:
            if generation != self.generation:
                return
            try:
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
            if self.ignore_load_failures:
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
        clamped = max(0.0, min(1.0, float(volume)))
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
        with self.lock:
            self.generation += 1
            self._invalidate_inflight_load()
            self.queue.clear()
            self.original = []
            self.current = None
            try:
                self.cast.media_controller.stop()
            except Exception as exc:
                log.debug("Media controller stop error on '%s' (ignored): %s", self.cast.name, exc)
            try:
                self.cast.quit_app()
            except Exception as exc:
                log.debug("Cast quit_app error on '%s' (ignored): %s", self.cast.name, exc)

    def status(self):
        with self.lock:
            track = self.current
            player_state = getattr(self.cast.media_controller.status, "player_state", "UNKNOWN")
            volume = getattr(getattr(self.cast, "status", None), "volume_level", None)
            return {
                "speaker": self.cast.name,
                "state": player_state,
                "volume": volume,
                "current_track": (
                    {
                        "title": track.title,
                        "artist": getattr(track, "grandparentTitle", ""),
                        "album": getattr(track, "parentTitle", ""),
                    }
                    if track
                    else None
                ),
                "queue_length": len(self.queue),
                "repeat": self.repeat,
            }
