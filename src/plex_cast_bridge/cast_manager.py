import ipaddress
import json
import logging
import os
import re
import socket
import sys
import threading
from pathlib import Path
from uuid import UUID

import pychromecast
from pychromecast.const import CAST_TYPE_GROUP

from .cast_discovery import _discover_via_dnssd, extract_room_name
from .local_speaker import LocalSpeaker
from .speaker_queue import DEFAULT_CAST_VOLUME, SpeakerQueue
from .speaker_registry import is_cast_group

log = logging.getLogger(__name__)

DIAL_IDENTITY_TIMEOUT = 4.0
_HOSTNAME_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?$"
)

# Re-export discovery helpers and SpeakerQueue for existing import paths / monkeypatches.
__all__ = [
    "DEFAULT_CAST_VOLUME",
    "DIAL_IDENTITY_TIMEOUT",
    "CastManager",
    "SpeakerQueue",
    "_discover_via_dnssd",
    "extract_room_name",
    "resolve_dial_display_metadata",
    "verify_host_cast_uuid",
]


def _parse_uuid(value: object) -> UUID | None:
    """Parse a valid UUID value without raising on missing or malformed inputs."""
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        return UUID(raw)
    except (ValueError, AttributeError, TypeError):
        return None


def _uuids_match(left: object, right: object) -> bool:
    """Compare two Cast UUIDs structurally when valid, else case-insensitively."""
    left_uuid = _parse_uuid(left)
    right_uuid = _parse_uuid(right)
    if left_uuid is not None and right_uuid is not None:
        return left_uuid == right_uuid
    left_text = str(left or "").strip().casefold()
    right_text = str(right or "").strip().casefold()
    return bool(left_text and left_text == right_text)


def _validate_host(host: object) -> str | None:
    """Return a clean IP address or hostname suitable for Cast/DIAL access."""
    if not isinstance(host, str):
        return None
    candidate = host.strip()
    if not candidate or candidate.casefold() == "local":
        return None
    if any(ch.isspace() or ch in "/?#@\\" for ch in candidate):
        return None
    ip_part = candidate.partition("%")[0]
    try:
        ip = ipaddress.ip_address(ip_part)
    except ValueError:
        if ":" in candidate or len(candidate) > 253 or not _HOSTNAME_RE.match(candidate):
            return None
        return candidate
    if ip.is_unspecified:
        return None
    return candidate


def _validate_port(port: object) -> int | None:
    """Return a valid TCP port number, defaulting None to standard Cast port 8009."""
    if port is None:
        return 8009
    if isinstance(port, bool) or not isinstance(port, int):
        return None
    if not 1 <= port <= 65535:
        return None
    return port


def _is_individual_speaker_record(record: dict) -> bool:
    """Return True only when a catalog record represents an individual speaker."""
    if record.get("is_group"):
        return False
    kind = record.get("kind")
    if kind is not None and str(kind).strip().casefold() != "speaker":
        return False
    if is_cast_group(record.get("model"), record.get("name")):
        return False
    return True


def verify_host_cast_uuid(
    host: str,
    expected_uuid: str | UUID,
    *,
    timeout: float = DIAL_IDENTITY_TIMEOUT,
) -> tuple[UUID, object]:
    """Verify a Cast host's identity independently via PyChromecast DIAL."""
    expected = _parse_uuid(expected_uuid)
    if expected is None:
        raise ValueError("Cast UUID unavailable: expected UUID is missing or malformed")
    validated_host = _validate_host(host)
    if validated_host is None:
        raise ValueError("Cast UUID unavailable: configured speaker host is missing or invalid")
    try:
        device_info = pychromecast.dial.get_device_info(validated_host, timeout=timeout)
    except Exception as exc:
        log.warning(
            "DIAL identity check failed for host %s (%s)",
            validated_host,
            type(exc).__name__,
        )
        raise ValueError(
            "Cast UUID unavailable: could not verify device identity from configured host"
        ) from exc
    if device_info is None:
        log.warning("DIAL identity check returned no device info for host %s", validated_host)
        raise ValueError(
            "Cast UUID unavailable: could not verify device identity from configured host"
        )

    cast_type = str(getattr(device_info, "cast_type", "") or "").strip().casefold()
    reported_model = getattr(device_info, "model_name", None)
    if cast_type == CAST_TYPE_GROUP or is_cast_group(reported_model):
        raise ValueError("Cast UUID unavailable: configured host reported a Cast Group")

    reported = _parse_uuid(getattr(device_info, "uuid", None))
    if reported is None:
        raise ValueError(
            "Cast UUID unavailable: configured host did not report a valid device UUID"
        )
    if reported != expected:
        raise ValueError("Cast UUID mismatch: configured host reported a different Cast device")
    return reported, device_info


def resolve_dial_display_metadata(
    device_info: object,
    *,
    fallback_model: str = "Google Cast",
    fallback_name: str = "Cast speaker",
) -> tuple[str, str]:
    """Extract non-placeholder model and friendly name from a DIAL DeviceStatus."""
    reported_model = getattr(device_info, "model_name", None)
    model_name = (
        reported_model.strip()
        if isinstance(reported_model, str)
        and reported_model.strip()
        and reported_model.strip() != "Unknown model name"
        else fallback_model
    )
    reported_name = getattr(device_info, "friendly_name", None)
    friendly_name = (
        reported_name.strip()
        if isinstance(reported_name, str)
        and reported_name.strip()
        and reported_name.strip() != "Unknown Chromecast"
        else fallback_name
    )
    return model_name, friendly_name


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
                            "kind": s.get("kind"),
                            "is_group": bool(s.get("is_group", False)),
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
            try:
                cast.wait(timeout=timeout)
            except Exception:
                try:
                    cast.disconnect()
                except Exception:
                    log.debug("Could not close failed Cast connection for '%s'", name, exc_info=True)
                raise
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

    def _resolve_verified_catalog_host(self, device_id: str, expected_uuid_key: str) -> tuple:
        """Resolve an individual speaker from catalog after independent DIAL UUID verification."""
        catalog_speakers = self._get_catalog_speakers()
        records = [s for s in catalog_speakers if s.get("device_id") == device_id]
        if len(records) != 1:
            raise ValueError("Unknown or conflicting virtual device")
        record = records[0]
        if not record.get("enabled") or not record.get("visible"):
            raise ValueError("Virtual device is disabled or hidden")
        if not _is_individual_speaker_record(record):
            raise ValueError(
                "Cast UUID unavailable: direct host fallback is not supported for Cast Groups"
            )
        raw_uuid = str(record.get("uuid") or "").strip()
        expected_uuid = _parse_uuid(raw_uuid)
        if expected_uuid is None or str(expected_uuid) != expected_uuid_key:
            raise ValueError("Cast UUID unavailable: catalog UUID is missing or malformed")
        matching_uuid_records = [
            s for s in catalog_speakers if _parse_uuid(s.get("uuid")) == expected_uuid
        ]
        if len(matching_uuid_records) != 1:
            raise ValueError("Cast UUID unavailable or ambiguous")

        host = _validate_host(record.get("host"))
        if host is None:
            raise ValueError("Cast UUID unavailable: no valid configured speaker host")
        port = _validate_port(record.get("port", 8009))
        if port is None:
            raise ValueError("Cast UUID unavailable: invalid configured speaker port")

        reported_uuid, device_info = verify_host_cast_uuid(host, expected_uuid)
        model_name, friendly_name = resolve_dial_display_metadata(
            device_info,
            fallback_model=record.get("model") or "Google Cast",
            fallback_name=record.get("name") or device_id,
        )
        log.info(
            "Verified Cast UUID for %r via DIAL at %s:%s; connecting host-based",
            device_id,
            host,
            port,
        )
        return (host, port, reported_uuid, model_name, friendly_name)

    def get_device(self, device_id: str):
        """Resolve an authorized virtual device by UUID via mDNS or DIAL-verified host fallback.

        Never trust a persisted IP as proof of identity; DHCP can reassign it.
        """
        catalog_speakers = self._get_catalog_speakers()
        records = [s for s in catalog_speakers if s.get("device_id") == device_id]
        if len(records) != 1:
            raise ValueError("Unknown or conflicting virtual device")
        record = records[0]
        if not record.get("enabled") or not record.get("visible"):
            raise ValueError("Virtual device is disabled or hidden")
        raw_uuid = str(record.get("uuid") or "").strip()
        if not raw_uuid:
            raise ValueError("Cast UUID unavailable: catalog UUID is missing or malformed")
        matching_uuid_records = [
            s for s in catalog_speakers if _uuids_match(s.get("uuid"), raw_uuid)
        ]
        if len(matching_uuid_records) != 1:
            raise ValueError("Cast UUID unavailable or ambiguous")
        parsed_uuid = _parse_uuid(raw_uuid)
        uuid = str(parsed_uuid) if parsed_uuid is not None else raw_uuid.casefold()
        key = f"uuid:{uuid}"
        with self.lock:
            cached = self.speakers.get(key)
            if cached is not None:
                if self._is_cast_connected(cached):
                    return cached
                self._discard_speaker(key)

        host_tuple = None
        # Share the same discovery lock used by background/manual speaker scans.
        # Concurrent Zeroconf browsers can return no result or duplicate Cast
        # records transiently, which made valid voice commands fail to route.
        with self._discovery_lock:
            casts = []
            browser = None
            try:
                casts, browser = pychromecast.get_chromecasts(timeout=3)
                matches = [c.cast_info for c in casts if _uuids_match(c.uuid, uuid)]
                if len(matches) > 1:
                    raise ValueError("Cast UUID unavailable or ambiguous")
                if len(matches) == 1:
                    info = matches[0]
                    host_tuple = (
                        info.host,
                        info.port,
                        info.uuid,
                        info.model_name,
                        info.friendly_name,
                    )
            except ValueError:
                raise
            except Exception as exc:
                log.debug("pychromecast get_chromecasts error during virtual-device lookup: %s", exc)
            finally:
                if browser is not None:
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

        if host_tuple is None:
            log.info(
                "mDNS did not find virtual device %r; attempting UUID-verified host fallback",
                device_id,
            )
            host_tuple = self._resolve_verified_catalog_host(device_id, uuid)

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
