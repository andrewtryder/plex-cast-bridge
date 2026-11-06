import json
import logging
import os
import random
import socket
import sys
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pychromecast
from pychromecast.controllers.media import MediaStatusListener

from .cast_discovery import _discover_via_dnssd, extract_room_name
from .local_speaker import LocalSpeaker

log = logging.getLogger(__name__)

DEFAULT_CAST_VOLUME = 0.20

# Re-export discovery helpers for existing import paths / monkeypatches.
__all__ = [
    "DEFAULT_CAST_VOLUME",
    "SpeakerQueue",
    "CastManager",
    "extract_room_name",
    "_discover_via_dnssd",
]


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


class CastManager:
    def __init__(
        self,
        default_volume: float = DEFAULT_CAST_VOLUME,
        default_speaker_host: str = "",
        disabled_speakers: set[str] | None = None,
        catalog_path: Path | str | None = None,
    ):
        self.speakers = {}
        self.lock = threading.RLock()
        self.default_volume = default_volume
        self.default_speaker_host = default_speaker_host.strip()
        self.disabled_speakers: set[str] = {s.casefold() for s in (disabled_speakers or set())}
        self.last_discovered: list[dict] = []
        self.catalog_path = Path(catalog_path) if catalog_path else None

    def _get_catalog_speakers(self) -> list[dict]:
        speakers = []
        if self.catalog_path and self.catalog_path.exists():
            try:
                with open(self.catalog_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for s in data.get("speakers", []):
                        name = s.get("speaker_name") or s.get("name")
                        room = s.get("roomHint") or s.get("room") or extract_room_name(name)
                        speakers.append({
                            "name": name,
                            "room": room,
                            "model": s.get("model", "Google Cast"),
                            "uuid": s.get("uuid"),
                            "host": s.get("host"),
                            "port": s.get("port", 8009),
                            "enabled": s.get("enabled", True),
                        })
            except Exception as exc:
                log.debug("Could not read speakers from catalog: %s", exc)
        return speakers

    def is_disabled(self, name: str) -> bool:
        n = name.strip().casefold()
        r = extract_room_name(name).casefold()
        return n in self.disabled_speakers or r in self.disabled_speakers

    def set_speaker_enabled(self, name: str, enabled: bool):
        with self.lock:
            n = name.strip().casefold()
            r = extract_room_name(name).casefold()
            if enabled:
                to_remove = {
                    s for s in self.disabled_speakers
                    if s in (n, r) or extract_room_name(s).casefold() == r
                }
                self.disabled_speakers -= to_remove
            else:
                self.disabled_speakers.add(n)
                self.disabled_speakers.add(r)

    def discover(self):
        discovered = []
        seen_names = set()

        enable_test_speaker = os.getenv("ENABLE_TEST_SPEAKER", "").strip().lower() in ("true", "1", "yes")
        # On macOS, include test speaker by default unless explicitly disabled by config.
        # On Linux (headless), only include test speaker if explicitly requested via ENABLE_TEST_SPEAKER.
        if sys.platform == "darwin" or enable_test_speaker:
            model = "macOS afplay" if sys.platform == "darwin" else "Headless Simulation"
            room = "Mac Local" if sys.platform == "darwin" else "Local Host"
            discovered.append({
                "name": "test speaker",
                "room": room,
                "model": model,
                "uuid": "local-test-speaker",
                "host": "local",
                "port": 0,
                "enabled": not self.is_disabled("test speaker"),
            })
            seen_names.add("test speaker")

        # 1. Native dns-sd discovery (reliable on macOS multi-homed interfaces)
        if sys.platform == "darwin":
            for dev in _discover_via_dnssd():
                dev_fold = dev["name"].casefold()
                if dev_fold not in seen_names:
                    seen_names.add(dev_fold)
                    discovered.append({
                        "name": dev["name"],
                        "room": dev.get("room") or extract_room_name(dev["name"]),
                        "model": dev.get("model", "Google Cast"),
                        "uuid": dev.get("uuid"),
                        "host": dev["host"],
                        "port": dev.get("port", 8009),
                        "enabled": not self.is_disabled(dev["name"]),
                    })

        # 2. Standard pychromecast discovery (Linux / fallback)
        try:
            casts, browser = pychromecast.get_chromecasts(timeout=3)
            try:
                for c in casts:
                    name = c.cast_info.friendly_name
                    name_fold = name.casefold()
                    if name_fold not in seen_names:
                        seen_names.add(name_fold)
                        discovered.append({
                            "name": name,
                            "room": extract_room_name(name),
                            "model": c.cast_info.model_name or "Google Cast",
                            "uuid": str(c.uuid),
                            "host": c.cast_info.host,
                            "port": c.cast_info.port,
                            "enabled": not self.is_disabled(name),
                        })
            finally:
                browser.stop_discovery()
                for c in casts:
                    c.disconnect()
        except Exception as exc:
            log.debug("pychromecast get_chromecasts error: %s", exc)

        # 3. Known / catalog speaker merge & reachability probe
        for spk in self._get_catalog_speakers():
            name = spk.get("name")
            if not name:
                continue
            name_fold = name.casefold()
            if name_fold in seen_names:
                continue
            host = spk.get("host")
            port = spk.get("port", 8009)
            if host and host != "local":
                reachable = True
                try:
                    # Test TCP reachability to speaker host (using 8009 for Cast or configured port)
                    probe_port = port if port == 8009 else 8009
                    with socket.create_connection((host, probe_port), timeout=0.8):
                        pass
                except Exception:
                    reachable = False
                if reachable:
                    seen_names.add(name_fold)
                    discovered.append({
                        "name": name,
                        "room": spk.get("room") or extract_room_name(name),
                        "model": spk.get("model", "Google Cast"),
                        "uuid": spk.get("uuid"),
                        "host": host,
                        "port": port,
                        "enabled": not self.is_disabled(name),
                    })

        with self.lock:
            self.last_discovered = discovered
        return discovered

    def get(self, name):
        if not name:
            raise ValueError("Specify a speaker or configure DEFAULT_SPEAKER_NAME")

        if self.is_disabled(name):
            raise ValueError(f"Speaker '{name}' is currently disabled in Plex Cast Bridge settings")

        with self.lock:
            # Check local test speaker sink
            if name.casefold() in ("test speaker", "mac speaker", "local speaker", "mac local"):
                return self.speakers.setdefault("test speaker", LocalSpeaker(enable_playback=True))

            if name in self.speakers:
                return self.speakers[name]

            # Generate candidate friendly names (e.g. "Master Bedroom" -> ["Master Bedroom", "Master Bedroom display"])
            candidate_names = [name]
            cleaned = name.strip()
            if not cleaned.lower().endswith("display"):
                candidate_names.append(f"{cleaned} display")
            if not cleaned.lower().endswith("speaker"):
                candidate_names.append(f"{cleaned} speaker")

            # 1. Try standard pychromecast discovery
            try:
                casts, browser = pychromecast.get_listed_chromecasts(
                    friendly_names=candidate_names, discovery_timeout=3
                )
                try:
                    if casts:
                        cast = casts[0]
                        cast.wait(timeout=10)
                        sq = SpeakerQueue(cast, default_volume=self.default_volume)
                        self.speakers[name] = sq
                        return sq
                finally:
                    browser.stop_discovery()
            except Exception as exc:
                log.debug("pychromecast get_listed_chromecasts error: %s", exc)

            # 2. Try native macOS dns-sd discovery & direct host connection
            if sys.platform == "darwin":
                dnssd_devices = _discover_via_dnssd()
                for dev in dnssd_devices:
                    dev_name = dev["name"].casefold()
                    for cand in candidate_names:
                        cand_fold = cand.casefold()
                        if cand_fold == dev_name or cand_fold in dev_name or dev_name in cand_fold:
                            log.info(
                                "Found speaker '%s' via dns-sd matching '%s' at %s:%s",
                                dev["name"],
                                name,
                                dev["host"],
                                dev["port"],
                            )
                            cast = pychromecast.get_chromecast_from_host(
                                (dev["host"], dev["port"], dev["uuid"], dev["model"], dev["name"])
                            )
                            cast.wait(timeout=10)
                            sq = SpeakerQueue(cast, default_volume=self.default_volume)
                            self.speakers[name] = sq
                            return sq

            # 3. Known / catalog speaker direct connection fallback
            fallback_candidates = []
            seen_cand_hosts = set()
            for spk in (self.last_discovered + self._get_catalog_speakers()):
                spk_name = (spk.get("name") or "").casefold()
                spk_room = (spk.get("room") or "").casefold()
                for cand in candidate_names:
                    cand_fold = cand.casefold()
                    if cand_fold in (spk_name, spk_room) or spk_name in cand_fold or cand_fold in spk_name:
                        host = spk.get("host")
                        port = spk.get("port", 8009)
                        if host and host != "local" and (host, port) not in seen_cand_hosts:
                            seen_cand_hosts.add((host, port))
                            fallback_candidates.append(spk)

            for spk in fallback_candidates:
                host = spk["host"]
                port = spk.get("port", 8009)
                log.info(
                    "Attempting direct connection to known speaker '%s' at %s:%s",
                    spk.get("name"),
                    host,
                    port,
                )
                try:
                    cast = pychromecast.get_chromecast_from_host(
                        (host, port, spk.get("uuid"), spk.get("model", "Google Cast"), spk.get("name", name))
                    )
                    cast.wait(timeout=8)
                    sq = SpeakerQueue(cast, default_volume=self.default_volume)
                    self.speakers[name] = sq
                    return sq
                except Exception as exc:
                    log.warning("Direct connection to known speaker %s:%s failed: %s", host, port, exc)

            # 4. Configurable static host fallback via DEFAULT_SPEAKER_HOST
            if self.default_speaker_host:
                host_str = self.default_speaker_host
                port_int = 8009
                if ":" in host_str:
                    host_str, port_part = host_str.rsplit(":", 1)
                    port_int = int(port_part)
                log.info("Attempting direct connection to configured DEFAULT_SPEAKER_HOST %s:%d", host_str, port_int)
                try:
                    cast = pychromecast.get_chromecast_from_host(
                        (host_str, port_int, None, "Google Cast", name)
                    )
                    cast.wait(timeout=10)
                    sq = SpeakerQueue(cast, default_volume=self.default_volume)
                    self.speakers[name] = sq
                    return sq
                except Exception as exc:
                    log.warning("Direct connection to %s:%d failed: %s", host_str, port_int, exc)

            raise ValueError(f"Speaker not found: {name}")

    def close(self):
        for speaker in self.speakers.values():
            if isinstance(speaker, LocalSpeaker):
                speaker.stop()
                continue
            speaker.executor.shutdown(wait=True, cancel_futures=True)
            speaker.cast.disconnect()
