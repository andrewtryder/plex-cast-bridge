import json
import logging
import os
import socket
import sys
import threading
from pathlib import Path

import pychromecast

from .cast_discovery import _discover_via_dnssd, extract_room_name
from .local_speaker import LocalSpeaker
from .speaker_queue import DEFAULT_CAST_VOLUME, SpeakerQueue

log = logging.getLogger(__name__)

# Re-export discovery helpers and SpeakerQueue for existing import paths / monkeypatches.
__all__ = [
    "DEFAULT_CAST_VOLUME",
    "CastManager",
    "SpeakerQueue",
    "_discover_via_dnssd",
    "extract_room_name",
]


class CastManager:
    def __init__(
        self,
        default_volume: float = DEFAULT_CAST_VOLUME,
        max_volume: float = 1.0,
        default_speaker_host: str = "",
        disabled_speakers: set[str] | None = None,
        catalog_path: Path | str | None = None,
    ):
        self.speakers = {}
        self.lock = threading.RLock()
        self.default_volume = default_volume
        self.max_volume = max_volume
        self.default_speaker_host = default_speaker_host.strip()
        self.disabled_speakers: set[str] = {s.casefold() for s in (disabled_speakers or set())}
        self.last_discovered: list[dict] = []
        self.catalog_path = Path(catalog_path) if catalog_path else None
        self._discovery_lock = threading.Lock()

    def _get_catalog_speakers(self) -> list[dict]:
        speakers = []
        if self.catalog_path and self.catalog_path.exists():
            try:
                with open(self.catalog_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for s in data.get("speakers", []):
                        if s.get("removed"):
                            continue
                        name = s.get("speaker_name") or s.get("name")
                        room = s.get("room_hint") or s.get("roomHint") or s.get("room") or extract_room_name(name or "")
                        speakers.append({
                            "name": name,
                            "device_id": s.get("google_home_device_id") or s.get("id"),
                            "visible": s.get("google_home_visible", s.get("enabled", True)),
                            "room": room,
                            "model": s.get("model", "Google Cast"),
                            "uuid": s.get("cast_uuid") or s.get("uuid"),
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

    @staticmethod
    def _is_cast_connected(speaker) -> bool:
        """Return False when a Chromecast socket is known-dead (needs reconnect)."""
        if isinstance(speaker, LocalSpeaker):
            return True
        cast = getattr(speaker, "cast", None)
        if cast is None:
            return False
        socket_client = getattr(cast, "socket_client", None)
        if socket_client is None:
            return True
        return bool(getattr(socket_client, "is_connected", True))

    def _discard_speaker(self, name: str):
        """Drop a cached speaker and tear down its Cast connection."""
        speaker = self.speakers.pop(name, None)
        if speaker is None:
            return
        if isinstance(speaker, LocalSpeaker):
            try:
                speaker.stop()
            except Exception as exc:
                log.debug("LocalSpeaker stop during discard failed: %s", exc)
            return
        try:
            speaker.close()
        except Exception as exc:
            log.debug("Speaker executor shutdown during discard failed: %s", exc)
        try:
            speaker.cast.disconnect()
        except Exception as exc:
            log.debug("Cast disconnect during discard failed: %s", exc)

    def _connect_from_host(self, name: str, host_tuple: tuple, *, timeout: float = 10):
        """Open a host-based Cast connection that does not depend on discovery Zeroconf."""
        cast = pychromecast.get_chromecast_from_host(host_tuple)
        try:
            cast.wait(timeout=timeout)
        except Exception:
            # Some Cast groups are advertised with a nonstandard mDNS service
            # port even though their receiver endpoint listens on the standard
            # Cast port. Keep the discovered port as the first attempt, then
            # retry the same host and UUID on 8009 before reporting failure.
            try:
                cast.disconnect()
            except Exception:
                log.debug("Could not close failed Cast connection for '%s'", name, exc_info=True)
            if len(host_tuple) < 2 or host_tuple[1] == 8009:
                raise
            fallback = (host_tuple[0], 8009, *host_tuple[2:])
            log.warning("Cast endpoint for '%s' did not respond on port %s; retrying on 8009",
                        name, host_tuple[1])
            cast = pychromecast.get_chromecast_from_host(fallback)
            cast.wait(timeout=timeout)
        sq = SpeakerQueue(
            cast,
            default_volume=min(self.default_volume, self.max_volume),
            max_volume=self.max_volume,
        )
        self.speakers[name] = sq
        return sq

    def discover(self):
        # Never overlap mDNS scans; callers still get the last successful result.
        if not self._discovery_lock.acquire(blocking=False):
            log.debug("Speaker discovery already in progress; returning last_discovered")
            with self.lock:
                return list(self.last_discovered)
        try:
            return self._discover_unlocked()
        finally:
            self._discovery_lock.release()

    def _discover_unlocked(self):
        discovered = []
        seen_names = set()
        seen_uuids = set()

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
                dev_uuid = str(dev.get("uuid") or "").casefold()
                if (dev_uuid and dev_uuid not in seen_uuids) or (not dev_uuid and dev_fold not in seen_names):
                    seen_names.add(dev_fold)
                    if dev_uuid:
                        seen_uuids.add(dev_uuid)
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
                    dev_uuid = str(c.uuid or "").casefold()
                    if (dev_uuid and dev_uuid not in seen_uuids) or (not dev_uuid and name_fold not in seen_names):
                        seen_names.add(name_fold)
                        if dev_uuid:
                            seen_uuids.add(dev_uuid)
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
                try:
                    browser.stop_discovery()
                except Exception as exc:
                    log.debug("stop_discovery failed during speaker scan: %s", exc)
                for c in casts:
                    try:
                        c.disconnect()
                    except Exception as exc:
                        # Discovery returns some Cast objects before their socket
                        # thread starts. Their disconnect() can raise RuntimeError;
                        # cleanup must not abort the scan or mask its result.
                        log.debug("Cast cleanup after discovery failed: %s", exc)
        except Exception as exc:
            log.debug("pychromecast get_chromecasts error: %s", exc)

        # 3. Known / catalog speaker merge & reachability probe
        for spk in self._get_catalog_speakers():
            name = spk.get("name")
            if not name:
                continue
            name_fold = name.casefold()
            spk_uuid = str(spk.get("uuid") or "").casefold()
            if (spk_uuid and spk_uuid in seen_uuids) or (not spk_uuid and name_fold in seen_names):
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
                    if spk_uuid:
                        seen_uuids.add(spk_uuid)
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

    def get_device(self, device_id: str):
        """Resolve an authorized virtual device by UUID using fresh discovery.

        Never trust a persisted IP as proof of identity; DHCP can reassign it.
        """
        records = [s for s in self._get_catalog_speakers() if s.get("device_id") == device_id]
        if len(records) != 1:
            raise ValueError("Unknown or conflicting virtual device")
        record = records[0]
        if not record.get("enabled") or not record.get("visible"):
            raise ValueError("Virtual device is disabled or hidden")
        uuid = str(record.get("uuid") or "").casefold()
        if not uuid:
            name = (record.get("name") or "").casefold()
            if sum(1 for s in self._get_catalog_speakers() if (s.get("name") or "").casefold() == name) != 1:
                raise ValueError("Ambiguous legacy speaker")
            return self.get(record.get("name"))
        key = f"uuid:{uuid}"
        with self.lock:
            cached = self.speakers.get(key)
            if cached is not None:
                if self._is_cast_connected(cached):
                    return cached
                self._discard_speaker(key)

        # Share the same discovery lock used by background/manual speaker scans.
        # Concurrent Zeroconf browsers can return no result or duplicate Cast
        # records transiently, which made valid voice commands fail to route.
        with self._discovery_lock:
            casts, browser = pychromecast.get_chromecasts(timeout=3)
            try:
                matches = [c.cast_info for c in casts if str(c.uuid).casefold() == uuid]
                if len(matches) != 1:
                    raise ValueError("Cast UUID unavailable or ambiguous")
                info = matches[0]
                host_tuple = (info.host, info.port, info.uuid, info.model_name, info.friendly_name)
            finally:
                try:
                    browser.stop_discovery()
                except Exception as exc:
                    log.debug("stop_discovery failed during virtual-device resolution: %s", exc)
                for cast in casts:
                    try:
                        cast.disconnect()
                    except Exception as exc:
                        # A cleanup error must not replace a successful UUID match
                        # or obscure the actionable discovery error above.
                        log.debug("Cast cleanup after UUID lookup failed: %s", exc)

        with self.lock:
            cached = self.speakers.get(key)
            if cached is not None:
                if self._is_cast_connected(cached):
                    return cached
                self._discard_speaker(key)
            return self._connect_from_host(key, host_tuple)

    def get(self, name):
        if not name:
            raise ValueError("Specify a speaker or configure DEFAULT_SPEAKER_NAME")

        if self.is_disabled(name):
            raise ValueError(f"Speaker '{name}' is currently disabled in Plex Cast Bridge settings")

        with self.lock:
            # Check local test speaker sink
            if name.casefold() in ("test speaker", "mac speaker", "local speaker", "mac local"):
                return self.speakers.setdefault("test speaker", LocalSpeaker(enable_playback=True))

            cached = self.speakers.get(name)
            if cached is not None:
                if self._is_cast_connected(cached):
                    return cached
                log.warning("Cached Cast connection for '%s' is dead; reconnecting", name)
                self._discard_speaker(name)

            candidate_names = [name.strip()]

            # 1. Prefer known hosts (catalog / last scan). Host-based connections survive
            # discovery teardown; discovery-tied sockets do not (Zeroconf AssertionError).
            fallback_candidates = []
            seen_cand_hosts = set()
            for spk in (self.last_discovered + self._get_catalog_speakers()):
                spk_name = (spk.get("name") or "").casefold()
                spk_room = (spk.get("room") or "").casefold()
                for cand in candidate_names:
                    cand_fold = cand.casefold()
                    if cand_fold in (spk_name, spk_room):
                        host = spk.get("host")
                        port = spk.get("port", 8009)
                        if host and host != "local" and (host, port) not in seen_cand_hosts:
                            seen_cand_hosts.add((host, port))
                            fallback_candidates.append(spk)

            if len(fallback_candidates) > 1:
                raise ValueError(f"Ambiguous speaker: {name}")
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
                    return self._connect_from_host(
                        name,
                        (host, port, spk.get("uuid"), spk.get("model", "Google Cast"), spk.get("name", name)),
                        timeout=8,
                    )
                except Exception as exc:
                    log.warning("Direct connection to known speaker %s:%s failed: %s", host, port, exc)

            # 3. mDNS discovery — only to learn host/port, then reconnect via from_host
            # so stop_discovery() cannot kill the live socket's Zeroconf loop.
            try:
                casts, browser = pychromecast.get_listed_chromecasts(
                    friendly_names=candidate_names, discovery_timeout=3
                )
                host_tuple = None
                try:
                    if len(casts) > 1:
                        raise ValueError(f"Ambiguous speaker: {name}")
                    if casts:
                        info = casts[0].cast_info
                        host_tuple = (
                            info.host,
                            info.port,
                            str(info.uuid) if getattr(info, "uuid", None) else None,
                            info.model_name or "Google Cast",
                            info.friendly_name or name,
                        )
                finally:
                    try:
                        browser.stop_discovery()
                    except Exception as exc:
                        log.debug("stop_discovery failed: %s", exc)
                    for c in casts or []:
                        try:
                            c.disconnect()
                        except Exception:
                            pass
                if host_tuple:
                    log.info(
                        "Resolved '%s' via mDNS to %s:%s; connecting host-based",
                        name,
                        host_tuple[0],
                        host_tuple[1],
                    )
                    return self._connect_from_host(name, host_tuple)
            except Exception as exc:
                log.debug("pychromecast get_listed_chromecasts error: %s", exc)

            raise ValueError(f"Speaker not found: {name}")

    def close(self):
        for speaker in self.speakers.values():
            if isinstance(speaker, LocalSpeaker):
                speaker.stop()
                continue
            speaker.close()
            speaker.cast.disconnect()
