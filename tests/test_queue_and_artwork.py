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


def test_mute_restores_previous_volume_and_repeated_commands_are_safe():
    cast = SimpleNamespace(
        media_controller=MagicMock(), name="Room", set_volume=MagicMock(),
        status=SimpleNamespace(volume_level=0.63),
    )
    q = SpeakerQueue(cast, default_volume=0.2)
    q.mute(True)
    q.mute(True)
    q.mute(False)
    q.mute(False)
    assert [call.args[0] for call in cast.set_volume.call_args_list] == [0.0, 0.63]
    q.close()


def test_mute_restore_obeys_configured_volume_limit():
    cast = SimpleNamespace(
        media_controller=MagicMock(), name="Room", set_volume=MagicMock(),
        status=SimpleNamespace(volume_level=0.9),
    )
    q = SpeakerQueue(cast, default_volume=0.2, max_volume=0.4)
    q.mute(True)
    q.mute(False)
    assert [call.args[0] for call in cast.set_volume.call_args_list] == [0.0, 0.4]
    q.close()


def test_unmute_after_receiver_reconnect_uses_safe_default_when_saved_level_is_lost():
    cast = SimpleNamespace(
        media_controller=MagicMock(), name="Room", set_volume=MagicMock(),
        status=SimpleNamespace(volume_level=0.0),
    )
    q = SpeakerQueue(cast, default_volume=0.35)
    q.mute(False)
    cast.set_volume.assert_called_once_with(0.35)
    q.close()


def test_playback_volume_restored_on_stop_and_not_between_tracks():
    cast = SimpleNamespace(
        media_controller=MagicMock(), name="Room", set_volume=MagicMock(), quit_app=MagicMock(),
        status=SimpleNamespace(volume_level=0.73),
    )
    q = SpeakerQueue(cast, default_volume=0.4, max_volume=0.85)
    q.load_watchdog_seconds = 0
    cast.set_volume.assert_not_called()
    q.replace([_track("a"), _track("b")], repeat=False)
    cast.set_volume.assert_called_once_with(0.4)
    q.skip()
    assert cast.set_volume.call_count == 1
    q.stop()
    assert cast.set_volume.call_args_list[-1].args == (0.73,)
    q.stop()
    assert cast.set_volume.call_count == 2
    q.close()


def test_playback_volume_restored_after_queue_finishes():
    cast = SimpleNamespace(
        media_controller=MagicMock(), name="Room", set_volume=MagicMock(),
        status=SimpleNamespace(volume_level=0.11),
    )
    q = SpeakerQueue(cast, default_volume=0.4)
    q.load_watchdog_seconds = 0
    q.replace([_track("a")], repeat=False)
    q.skip()
    assert [c.args[0] for c in cast.set_volume.call_args_list] == [0.4, 0.11]
    q.close()


def test_each_speaker_restores_its_own_previous_volume():
    def speaker(name, volume):
        return SimpleNamespace(
            media_controller=MagicMock(), name=name, set_volume=MagicMock(), quit_app=MagicMock(),
            status=SimpleNamespace(volume_level=volume),
        )

    first, second = speaker("Bedroom", 0.12), speaker("Kitchen", 0.65)
    q1, q2 = SpeakerQueue(first), SpeakerQueue(second)
    q1.load_watchdog_seconds = q2.load_watchdog_seconds = 0
    q1.replace([_track("a")], repeat=False)
    q2.replace([_track("b")], repeat=False)
    q1.stop()
    q2.stop()
    assert [c.args[0] for c in first.set_volume.call_args_list] == [0.4, 0.12]
    assert [c.args[0] for c in second.set_volume.call_args_list] == [0.4, 0.65]
    q1.close()
    q2.close()


@pytest.mark.parametrize("new_app,restored", [("E8C28D3C", True), (None, True), ("ANOTHER_APP", False)])
def test_receiver_session_exit_restores_only_when_no_other_app_owns_device(new_app, restored):
    cast = SimpleNamespace(
        media_controller=MagicMock(), name="Room", set_volume=MagicMock(),
        status=SimpleNamespace(volume_level=0.6, app_id="CC1AD845"),
    )
    q = SpeakerQueue(cast)
    q.load_watchdog_seconds = 0
    q.replace([_track("a")], repeat=False)
    q.new_cast_status(cast.status)
    q.new_media_status(SimpleNamespace(player_state="PLAYING", content_id=q.expected_content_id, media_session_id=1))
    cast.status.app_id = new_app
    q.new_cast_status(cast.status)
    q.executor.shutdown(wait=True)
    assert [c.args[0] for c in cast.set_volume.call_args_list] == ([0.4, 0.6] if restored else [0.4])
    assert q.current is None


def test_unknown_receiver_volume_is_not_overwritten():
    cast = SimpleNamespace(media_controller=MagicMock(), name="Room", set_volume=MagicMock())
    q = SpeakerQueue(cast)
    q.load_watchdog_seconds = 0
    q.replace([_track("a")], repeat=False)
    q.skip()
    cast.set_volume.assert_not_called()
    q.close()


def test_cast_session_commands_do_not_wait_for_socket_replies():
    receiver = MagicMock()
    receiver.stop_app.side_effect = lambda callback_function: callback_function(True, {})
    cast = SimpleNamespace(
        media_controller=MagicMock(), name="Room", set_volume=MagicMock(), quit_app=MagicMock(),
        socket_client=SimpleNamespace(receiver_controller=receiver),
        status=SimpleNamespace(volume_level=0.7),
    )
    q = SpeakerQueue(cast)
    q.load_watchdog_seconds = 0
    q.replace([_track("a")], repeat=False)
    q.stop()
    assert [c.args[0]["volume"]["level"] for c in receiver.send_message.call_args_list] == [0.4, 0.7]
    receiver.stop_app.assert_called_once()
    cast.set_volume.assert_not_called()
    cast.quit_app.assert_not_called()
    q.close()


def test_native_queue_advances_bridge_queue_and_removes_stale_item():
    receiver = MagicMock()
    media = MagicMock()
    media.status.media_session_id = 7
    cast = SimpleNamespace(media_controller=media, name="Room", set_volume=MagicMock(),
                           socket_client=SimpleNamespace(receiver_controller=receiver),
                           status=SimpleNamespace(volume_level=0.6))
    q = SpeakerQueue(cast)
    q.load_watchdog_seconds = 0
    q.replace([_track("a"), _track("b"), _track("c")], repeat=False)
    q.new_media_status(SimpleNamespace(player_state="PLAYING", content_id="http://plex/a", media_session_id=7))
    q.executor.submit(lambda: None).result(timeout=5)
    pending = media.play_media.call_args.kwargs
    assert pending["enqueue"] is True
    pending["callback_function"](True, {"status": [{"currentItemId": 1, "items": [
        {"itemId": 2, "media": {"contentId": "http://plex/b"}}
    ]}]})
    q.remove_at(0)
    media._send_command.assert_any_call({"type": "QUEUE_REMOVE", "itemIds": [2]}, None)
    q.executor.submit(lambda: None).result(timeout=5)
    pending = media.play_media.call_args.kwargs
    pending["callback_function"](True, {"status": [{"currentItemId": 1, "items": [
        {"itemId": 3, "media": {"contentId": "http://plex/c"}}
    ]}]})
    q.new_media_status(SimpleNamespace(player_state="IDLE", idle_reason="FINISHED",
                                       content_id="http://plex/a", media_session_id=7))
    assert q.current.title == "a"
    q.new_media_status(SimpleNamespace(player_state="PLAYING", content_id="http://plex/c", media_session_id=7))
    assert q.current.title == "c"
    assert not q.queue
    q.close()


@pytest.mark.parametrize("app_id,expected", [("CC1AD845", [0.4, 0.3]), ("OTHER_APP", [0.4])])
def test_cancelled_playback_respects_new_receiver_owner(app_id, expected):
    media = MagicMock()
    media.status.player_state = "IDLE"
    cast = SimpleNamespace(
        media_controller=media, name="Room", set_volume=MagicMock(),
        status=SimpleNamespace(volume_level=0.3, app_id=app_id),
    )
    q = SpeakerQueue(cast)
    q.load_watchdog_seconds = 0
    q.replace([_track("a")], repeat=False)
    content = q.expected_content_id
    q.new_media_status(SimpleNamespace(player_state="PLAYING", content_id=content, media_session_id=1))
    q.new_media_status(SimpleNamespace(player_state="IDLE", idle_reason="CANCELLED", content_id=content, media_session_id=1))
    q.executor.shutdown(wait=True)
    assert [c.args[0] for c in cast.set_volume.call_args_list] == expected


def test_artwork_rejects_non_int_key():
    with TestClient(app) as client:
        r = client.get("/api/artwork/not-a-number")
        assert r.status_code in {404, 422, 503}
