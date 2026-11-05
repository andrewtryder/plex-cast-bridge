import logging
import random
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import pychromecast
from pychromecast.controllers.media import MediaStatusListener

from .local_speaker import LocalSpeaker

log = logging.getLogger(__name__)

DEFAULT_CAST_VOLUME = 0.20


def _discover_via_dnssd(timeout_sec=2.0):
    """
    Fallback speaker discovery using macOS native dns-sd command.
    Bypasses zeroconf multicast drop issues on multi-homed macOS interfaces.
    """
    if not shutil.which("dns-sd"):
        return []
    devices = []
    try:
        proc = subprocess.Popen(
            ["dns-sd", "-B", "_googlecast._tcp", "local."],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(timeout_sec)
        proc.terminate()
        stdout, _ = proc.communicate()

        names = set()
        for line in stdout.splitlines():
            if "Add" in line and "_googlecast._tcp" in line:
                parts = line.split()
                if len(parts) >= 7:
                    name = " ".join(parts[6:]).strip()
                    if name:
                        names.add(name)

        for name in names:
            lookup = subprocess.run(
                ["dns-sd", "-L", name, "_googlecast._tcp", "local."],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=2,
            )
            host = None
            port = 8009
            model = "Google Cast"
            uuid = ""
            for line in lookup.stdout.splitlines():
                if "can be reached at" in line:
                    match = re.search(r"reached at ([\w\.-]+):(\d+)", line)
                    if match:
                        host = match.group(1)
                        port = int(match.group(2))
                if "md=" in line:
                    m = re.search(r"md=([^ \t\n]+)", line)
                    if m:
                        model = m.group(1).replace("_", " ")
                if "id=" in line:
                    u = re.search(r"id=([a-f0-9]+)", line)
                    if u:
                        uuid = u.group(1)
            if host:
                devices.append({
                    "name": name,
                    "host": host,
                    "port": port,
                    "model": model,
                    "uuid": uuid,
                })
    except Exception as exc:
        log.debug("dns-sd fallback discovery error: %s", exc)
    return devices


class SpeakerQueue(MediaStatusListener):
    def __init__(self, cast, default_volume=DEFAULT_CAST_VOLUME):
        self.cast = cast
        self.default_volume = default_volume
        self.queue = deque()
        self.original = []
        self.current = None
        self.repeat = False
        self.armed = False
        self.generation = 0
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

    def replace(self, tracks, repeat):
        with self.lock:
            self.generation += 1
            self.queue = deque(tracks)
            self.original = list(tracks)
            self.repeat = repeat
            self.armed = False
            self._next()

    def _next(self):
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
            media_list = getattr(track, "media", None) or []
            parts_list = (getattr(media_list[0], "parts", None) or []) if media_list else []
            if not parts_list:
                log.warning("Skipping track '%s' with missing media parts", getattr(track, "title", "Unknown"))
                continue
            break

        self.current = track
        self.armed = False

        # Build direct LAN stream URL with Plex auth token
        part = parts_list[0]
        url = track._server.url(part.key, includeToken=True)
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

    def new_media_status(self, status):
        with self.lock:
            if self.current is None:
                return
            if status.player_state in ("PLAYING", "BUFFERING"):
                self.armed = True
            elif status.player_state == "IDLE" and status.idle_reason == "FINISHED" and self.armed:
                self.armed = False
                log.info("Track finished on '%s'. Advancing queue automatically.", self.cast.name)
                self.executor.submit(self._advance, self.generation)

    def _advance(self, generation):
        with self.lock:
            if generation != self.generation:
                return
            try:
                self._next()
            except Exception:
                log.exception("Could not advance the Cast queue")

    def load_media_failed(self, queue_item_id, error_code):
        log.error("Cast media load failed on '%s': item=%s code=%s", self.cast.name, queue_item_id, error_code)

    def pause(self):
        self.cast.media_controller.pause()

    def resume(self):
        self.cast.media_controller.resume()

    def set_volume(self, volume: float):
        clamped = max(0.0, min(1.0, float(volume)))
        self.cast.set_volume(clamped)

    def adjust_volume(self, delta: float):
        current = getattr(getattr(self.cast, "status", None), "volume_level", self.default_volume or 0.20)
        if current is None:
            current = self.default_volume or 0.20
        self.set_volume(float(current) + float(delta))

    def skip(self):
        with self.lock:
            self.generation += 1
            self.armed = False
            self._next()

    def stop(self):
        with self.lock:
            self.generation += 1
            self.queue.clear()
            self.original = []
            self.current = None
            self.armed = False
            self.cast.media_controller.stop()

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
    def __init__(self, default_volume: float = DEFAULT_CAST_VOLUME, default_speaker_host: str = ""):
        self.speakers = {}
        self.lock = threading.RLock()
        self.default_volume = default_volume
        self.default_speaker_host = default_speaker_host.strip()

    def discover(self):
        discovered = [{"name": "test speaker", "uuid": None, "host": "local"}]
        seen_names = {"test speaker"}

        # 1. Standard pychromecast discovery
        try:
            casts, browser = pychromecast.get_chromecasts(timeout=3)
            try:
                for c in casts:
                    name = c.cast_info.friendly_name
                    if name not in seen_names:
                        seen_names.add(name)
                        discovered.append({
                            "name": name,
                            "uuid": str(c.uuid),
                            "host": c.cast_info.host,
                        })
            finally:
                browser.stop_discovery()
                for c in casts:
                    c.disconnect()
        except Exception as exc:
            log.debug("pychromecast get_chromecasts error: %s", exc)

        # 2. Native dns-sd fallback (essential on macOS multi-homed interfaces)
        for dev in _discover_via_dnssd():
            if dev["name"] not in seen_names:
                seen_names.add(dev["name"])
                discovered.append({
                    "name": dev["name"],
                    "uuid": dev.get("uuid"),
                    "host": dev["host"],
                })

        return discovered

    def get(self, name):
        if not name:
            raise ValueError("Specify a speaker or configure DEFAULT_SPEAKER_NAME")

        with self.lock:
            # Check local test speaker sink
            if name.casefold() in ("test speaker", "mac speaker", "local speaker"):
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

            # 3. Configurable static host fallback via DEFAULT_SPEAKER_HOST
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
