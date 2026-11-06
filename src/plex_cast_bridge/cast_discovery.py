"""Cast speaker discovery helpers (dns-sd and room-name normalization)."""

from __future__ import annotations

import logging
import re
import shutil
import socket
import subprocess
import time

log = logging.getLogger(__name__)


def extract_room_name(friendly_name: str) -> str:
    """Derive a clean Google Home roomHint from a Cast friendly name (e.g. 'Kitchen display' -> 'Kitchen')."""
    raw = friendly_name.strip()
    cleaned = re.sub(
        r"\s+(?:display|speaker|speakers|tv|chromecast|mini|hub|nest|audio)$",
        "",
        raw,
        flags=re.IGNORECASE,
    ).strip()
    if not cleaned:
        cleaned = raw
    # Title-case words nicely ("Great room" -> "Great Room")
    return " ".join(w.capitalize() if w.islower() else w for w in cleaned.split())


def discover_via_dnssd(timeout_sec=2.0):
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

        instances = set()
        for line in stdout.splitlines():
            if "Add" in line and "_googlecast._tcp" in line:
                parts = line.split()
                if len(parts) >= 7:
                    inst = " ".join(parts[6:]).strip()
                    if inst:
                        instances.add(inst)

        for inst in sorted(instances):
            lproc = subprocess.Popen(
                ["dns-sd", "-L", inst, "_googlecast._tcp", "local."],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            time.sleep(0.6)
            lproc.terminate()
            lout, _ = lproc.communicate()

            host = None
            port = 8009
            model = "Google Cast"
            uuid = ""
            friendly_name = inst
            for line in lout.splitlines():
                if "can be reached at" in line:
                    match = re.search(r"reached at ([\w\.-]+):(\d+)", line)
                    if match:
                        host = match.group(1)
                        port = int(match.group(2))
                if "md=" in line:
                    m = re.search(r"md=([^\n\r]+?)(?:\s+[a-z]{2}=|$)", line)
                    if m:
                        model = m.group(1).replace("\\ ", " ").replace("_", " ").strip()
                if "fn=" in line:
                    fn_m = re.search(r"fn=([^\n\r]+?)(?:\s+[a-z]{2}=|$)", line)
                    if fn_m:
                        friendly_name = fn_m.group(1).replace("\\ ", " ").strip()
                if "id=" in line:
                    u = re.search(r"id=([a-f0-9\-]+)", line)
                    if u:
                        uuid = u.group(1)
            if host:
                if host.endswith(".local") or host.endswith(".local."):
                    try:
                        resolved_ip = socket.gethostbyname(host)
                        if resolved_ip:
                            host = resolved_ip
                    except Exception:
                        pass
                room = extract_room_name(friendly_name)
                devices.append({
                    "name": friendly_name,
                    "room": room,
                    "instance": inst,
                    "host": host,
                    "port": port,
                    "model": model,
                    "uuid": uuid,
                })
    except Exception as exc:
        log.debug("dns-sd fallback discovery error: %s", exc)
    return devices


# Compatibility alias for previous private name in cast_manager.py
_discover_via_dnssd = discover_via_dnssd
