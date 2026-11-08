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
            speaker.executor.shutdown(wait=False, cancel_futures=True)
        except Exception as exc:
            log.debug("Speaker executor shutdown during discard failed: %s", exc)
        try:
            speaker.cast.disconnect()
        except Exception as exc:
            log.debug("Cast disconnect during discard failed: %s", exc)

    def _connect_from_host(self, name: str, host_tuple: tuple, *, timeout: float = 10):
        """Open a host-based Cast connection that does not depend on discovery Zeroconf."""
        cast = pychromecast.get_chromecast_from_host(host_tuple)
        cast.wait(timeout=timeout)
        sq = SpeakerQueue(
            cast,
            default_volume=min(self.default_volume, self.max_volume),
            max_volume=self.max_volume,
        )
        self.speakers[name] = sq
        return sq

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

            cached = self.speakers.get(name)
            if cached is not None:
                if self._is_cast_connected(cached):
                    return cached
                log.warning("Cached Cast connection for '%s' is dead; reconnecting", name)
                self._discard_speaker(name)

            # Generate candidate friendly names (e.g. "Master Bedroom" -> ["Master Bedroom", "Master Bedroom display"])
            candidate_names = [name]
            cleaned = name.strip()
            if not cleaned.lower().endswith("display"):
                candidate_names.append(f"{cleaned} display")
            if not cleaned.lower().endswith("speaker"):
                candidate_names.append(f"{cleaned} speaker")

            # 1. Prefer known hosts (catalog / last scan). Host-based connections survive
            # discovery teardown; discovery-tied sockets do not (Zeroconf AssertionError).
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
                    return self._connect_from_host(
                        name,
                        (host, port, spk.get("uuid"), spk.get("model", "Google Cast"), spk.get("name", name)),
                        timeout=8,
                    )
                except Exception as exc:
                    log.warning("Direct connection to known speaker %s:%s failed: %s", host, port, exc)

            # 2. Native macOS dns-sd discovery & direct host connection
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
                            try:
                                return self._connect_from_host(
                                    name,
                                    (dev["host"], dev["port"], dev["uuid"], dev["model"], dev["name"]),
                                )
                            except Exception as exc:
                                log.warning(
                                    "dns-sd host connect to %s:%s failed: %s",
                                    dev["host"],
                                    dev["port"],
                                    exc,
                                )

            # 3. mDNS discovery — only to learn host/port, then reconnect via from_host
            # so stop_discovery() cannot kill the live socket's Zeroconf loop.
            try:
                casts, browser = pychromecast.get_listed_chromecasts(
                    friendly_names=candidate_names, discovery_timeout=3
                )
                host_tuple = None
                try:
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

            # 4. Configurable static host fallback via DEFAULT_SPEAKER_HOST
            if self.default_speaker_host:
                host_str = self.default_speaker_host
                port_int = 8009
                if ":" in host_str:
                    host_str, port_part = host_str.rsplit(":", 1)
                    port_int = int(port_part)
                log.info("Attempting direct connection to configured DEFAULT_SPEAKER_HOST %s:%d", host_str, port_int)
                try:
                    return self._connect_from_host(
                        name,
                        (host_str, port_int, None, "Google Cast", name),
                    )
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
