"""Bounded, non-secret activity observed by the local bridge."""

from collections import deque
from datetime import UTC, datetime
from threading import Lock

_events = deque(maxlen=100)
_lock = Lock()


def record(source: str, event: str, speaker: str | None = None):
    item = {"time": datetime.now(UTC).isoformat(timespec="seconds"), "source": source, "event": event}
    if speaker:
        item["speaker"] = str(speaker)[:120]
    with _lock:
        _events.append(item)


def recent():
    with _lock:
        return list(reversed(_events))
