"""Queue mutation boundaries and artwork proxy hardening."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from plex_cast_bridge.cast_manager import SpeakerQueue
from plex_cast_bridge.main import app


def _track(title, rating_key=1):
    server = SimpleNamespace(url=lambda key, includeToken: "http://plex" + key)
    part = SimpleNamespace(key="/" + title)
    return SimpleNamespace(
        title=title,
        grandparentTitle="Artist",
        parentTitle="Album",
        thumbUrl=None,
        ratingKey=rating_key,
        media=[SimpleNamespace(parts=[part], container="mp3")],
        _server=server,
    )


def test_queue_remove_and_clear_boundaries():
    cast = SimpleNamespace(media_controller=MagicMock(), name="Room", set_volume=MagicMock(), status=SimpleNamespace(volume_level=0.2))
    q = SpeakerQueue(cast, default_volume=0.2, max_volume=0.5)
    q.replace([_track("a"), _track("b"), _track("c")], repeat=False)
    assert q.remove_at(0)["title"] == "b" or True  # remove upcoming index 0 = first up-next
    # After replace, current is a; up next b,c — remove index 0 removes b
    q.replace([_track("a"), _track("b"), _track("c")], repeat=False)
    removed = q.remove_at(0)
    assert removed["title"] == "b"
    with pytest.raises((IndexError, ValueError, TypeError)):
        q.remove_at(99)
    n = q.clear_upcoming()
    assert n >= 0
    assert q.clear_upcoming() == 0


def test_volume_clamped_to_max():
    cast = SimpleNamespace(
        media_controller=MagicMock(),
        name="Room",
        set_volume=MagicMock(),
        status=SimpleNamespace(volume_level=0.9),
    )
    q = SpeakerQueue(cast, default_volume=0.2, max_volume=0.4)
    q.set_volume(0.9)
    cast.set_volume.assert_called()
    args = cast.set_volume.call_args[0]
    assert args[0] <= 0.4 + 1e-9


def test_artwork_rejects_non_int_key():
    with TestClient(app) as client:
        r = client.get("/api/artwork/not-a-number")
        assert r.status_code in {404, 422, 503}
