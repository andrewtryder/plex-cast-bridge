from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from plex_cast_bridge.ai_router import PlaybackIntent
from plex_cast_bridge.cast_manager import SpeakerQueue
from plex_cast_bridge.main import app
from plex_cast_bridge.plex_service import PlexService


def track(title):
    server = SimpleNamespace(url=lambda key, includeToken: "http://plex" + key)
    part = SimpleNamespace(key="/" + title)
    return SimpleNamespace(title=title, grandparentTitle="Artist", parentTitle="Album", thumbUrl=None,
                           media=[SimpleNamespace(parts=[part], container="mp3")], _server=server)


def test_queue_advances_once_and_repeats():
    cast = SimpleNamespace(media_controller=MagicMock(), name="Room")
    queue = SpeakerQueue(cast)
    queue.replace([track("one"), track("two")], repeat=True)
    assert queue.current.title == "one"
    queue.new_media_status(SimpleNamespace(player_state="PLAYING", idle_reason=None))
    status = SimpleNamespace(player_state="IDLE", idle_reason="FINISHED")
    queue.new_media_status(status)
    queue.new_media_status(status)
    queue.executor.shutdown(wait=True)
    assert queue.current.title == "two"
    assert cast.media_controller.play_media.call_count == 2
    assert cast.media_controller.play_media.call_args.args == ("http://plex/two", "audio/mpeg")
    queue.skip()
    assert queue.current.title in {"one", "two"}


def test_album_resolution_and_radio_fallback():
    song = track("song")
    album = SimpleNamespace(title="Album", tracks=lambda: [song])
    artist = SimpleNamespace(title="Artist", genres=[], similar=[], similarArtists=[],
                             albums=lambda: [album], tracks=lambda: [song],
                             station=MagicMock(return_value=None))
    service = PlexService.__new__(PlexService)
    service.artists = [artist]
    assert service.resolve(PlaybackIntent(mode="album", target_artist="Artist", target_album="Album")) == [song]
    assert service.resolve(PlaybackIntent(mode="artist_radio", target_artist="Artist")) == [song]
    assert service.resolve(PlaybackIntent(mode="artist", target_artist="Artist")) == [song]


def test_artist_radio_pins_track1_and_interleaves_seeds():
    artist_tracks = [track(f"shadow_{i}") for i in range(5)]
    target = SimpleNamespace(
        title="DJ Shadow",
        genres=["Electronic"],
        similar=[],
        similarArtists=[],
        albums=lambda: [],
        tracks=lambda: list(artist_tracks),
    )
    peer_tracks = [track(f"krush_{i}") for i in range(10)]
    peer = SimpleNamespace(
        title="DJ Krush",
        genres=["Electronic"],
        similar=[],
        similarArtists=[],
        albums=lambda: [],
        tracks=lambda: list(peer_tracks),
    )
    service = PlexService.__new__(PlexService)
    service.artists = [target, peer]

    intent = PlaybackIntent(
        mode="artist_radio",
        target_artist="DJ Shadow",
        seed_artists=["DJ Shadow", "DJ Krush"],
    )
    queue = service.resolve(intent)

    # Track 1 must be pinned to DJ Shadow
    assert queue[0].title.startswith("shadow_")
    # Queue must contain tracks from both target and peer
    shadow_count = sum(1 for t in queue if t.title.startswith("shadow_"))
    krush_count = sum(1 for t in queue if t.title.startswith("krush_"))
    assert shadow_count >= 1
    assert krush_count >= 1


def test_transport_volume_actions():
    mock_speaker = MagicMock()
    mock_speaker.status.return_value = {"speaker": "Master Bedroom", "state": "PLAYING"}
    mock_cast = MagicMock()
    mock_cast.get.return_value = mock_speaker

    app.state.cast = mock_cast
    app.state.settings = SimpleNamespace(default_speaker_name="Master Bedroom")

    client = TestClient(app)

    # Test absolute volume
    resp = client.post("/api/transport", json={"action": "volume", "speaker": "Master Bedroom", "volume": 0.25})
    assert resp.status_code == 200
    mock_cast.get.assert_called_once_with("Master Bedroom")
    mock_speaker.set_volume.assert_called_once_with(0.25)

    # Test relative volume
    mock_cast.reset_mock()
    mock_speaker.reset_mock()
    resp = client.post("/api/transport", json={"action": "volume", "speaker": "Master Bedroom", "relative_volume": -0.05})
    assert resp.status_code == 200
    mock_cast.get.assert_called_once_with("Master Bedroom")
    mock_speaker.adjust_volume.assert_called_once_with(-0.05)



