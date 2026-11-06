from collections import deque
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
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

    # First 2 tracks must be pinned to DJ Shadow
    assert queue[0].title.startswith("shadow_")
    assert queue[1].title.startswith("shadow_")
    # Queue must contain tracks from both target and peer at ~50/50 ratio
    shadow_count = sum(1 for t in queue if t.title.startswith("shadow_"))
    krush_count = sum(1 for t in queue if t.title.startswith("krush_"))
    assert shadow_count == 5
    assert krush_count >= 3


def test_fast_path_artist_vs_radio_routing():
    from plex_cast_bridge.ai_router import SmartDJ

    dj = SmartDJ(api_key="dummy-key")
    catalog = {"artists": ["DJ Shadow", "Fugazi", "The Herbaliser", "Kruder & Dorfmeister"]}

    # Direct artist queries hit 0ms deterministic fast-path with mode='artist'
    intent1 = dj.parse("DJ Shadow", catalog)
    assert intent1.mode == "artist"
    assert intent1.target_artist == "DJ Shadow"

    intent2 = dj.parse("Play Fugazi on Plex", catalog)
    assert intent2.mode == "artist"
    assert intent2.target_artist == "Fugazi"

    intent3 = dj.parse("switch to Herbaliser", catalog)
    assert intent3.mode == "artist"
    assert intent3.target_artist == "The Herbaliser"

    # Explicit 'radio' query bypasses fast-path so Gemini selects sonic peers
    assert dj._try_fast_artist_match("play DJ Shadow radio", catalog["artists"]) is None


def test_speaker_disable_toggle():
    from plex_cast_bridge.cast_manager import CastManager

    mgr = CastManager()
    assert not mgr.is_disabled("Kitchen display")
    mgr.set_speaker_enabled("Kitchen display", False)
    assert mgr.is_disabled("Kitchen display")
    assert mgr.is_disabled("Kitchen")
    try:
        mgr.get("Kitchen")
        assert False, "Expected ValueError for disabled speaker"
    except ValueError as exc:
        assert "disabled" in str(exc).lower()
    mgr.set_speaker_enabled("Kitchen", True)
    assert not mgr.is_disabled("Kitchen display")


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

    # Test SpeakerQueue transport delegates to MediaController
    mock_mc = MagicMock()
    mock_real_cast = SimpleNamespace(media_controller=mock_mc, name="TestCast", status=SimpleNamespace(volume_level=0.5))
    sq = SpeakerQueue(mock_real_cast)

    sq.pause()
    mock_mc.pause.assert_called_once()

    sq.resume()
    mock_mc.play.assert_called_once()

    sq.next()  # should alias to skip
    assert sq.armed is False


def test_local_api_key_authentication():
    from pydantic import SecretStr, ValidationError

    from plex_cast_bridge.config import Settings

    with pytest.raises(ValidationError):
        Settings(
            plex_token=SecretStr("tok"),
            local_api_key=None,
            allow_unauthenticated_local_api=False,
        )

    ok = Settings(
        plex_token=SecretStr("tok"),
        local_api_key=None,
        allow_unauthenticated_local_api=True,
    )
    assert ok.allow_unauthenticated_local_api is True

    mock_speaker = MagicMock()
    mock_speaker.status.return_value = {"speaker": "Master Bedroom", "state": "IDLE"}
    mock_cast = MagicMock()
    mock_cast.get.return_value = mock_speaker

    app.state.cast = mock_cast
    app.state.settings = SimpleNamespace(
        default_speaker_name="Master Bedroom",
        local_api_key=SecretStr("super-secret-local-key"),
    )

    client = TestClient(app)

    # 1. Unauthenticated request rejected
    resp = client.post("/api/transport", json={"action": "pause", "speaker": "Master Bedroom"})
    assert resp.status_code == 401

    # 2. Authenticated with X-API-Key succeeds
    resp = client.post(
        "/api/transport",
        headers={"X-API-Key": "super-secret-local-key"},
        json={"action": "pause", "speaker": "Master Bedroom"},
    )
    assert resp.status_code == 200

    # 3. Authenticated with query param ?api_key= succeeds (deprecated bootstrap path)
    resp = client.post(
        "/api/transport?api_key=super-secret-local-key",
        json={"action": "resume", "speaker": "Master Bedroom"},
    )
    assert resp.status_code == 200

    # Restore settings without local_api_key
    app.state.settings = SimpleNamespace(default_speaker_name="Master Bedroom")


def test_plex_startup_retry_eventually_succeeds(monkeypatch):
    import asyncio

    from plex_cast_bridge import main as local_main

    calls = {"n": 0}

    class FakePlex:
        def __init__(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("plex down")

        def refresh(self):
            return {"artists": []}

    async def immediate_sleep(_delay):
        return None

    monkeypatch.setattr(local_main, "PlexService", FakePlex)
    monkeypatch.setattr(local_main.asyncio, "sleep", immediate_sleep)

    settings = SimpleNamespace(
        plex_url="http://plex",
        plex_token=SimpleNamespace(get_secret_value=lambda: "tok"),
        plex_music_section="Music",
    )
    plex = asyncio.run(local_main._init_plex_with_retry(settings, attempts=5, base_delay_sec=0.01))
    assert isinstance(plex, FakePlex)
    assert calls["n"] == 3


def test_plex_startup_retry_exhausted(monkeypatch):
    import asyncio

    from plex_cast_bridge import main as local_main

    class AlwaysDown:
        def __init__(self, *args, **kwargs):
            raise ConnectionError("plex down")

    async def immediate_sleep(_delay):
        return None

    monkeypatch.setattr(local_main, "PlexService", AlwaysDown)
    monkeypatch.setattr(local_main.asyncio, "sleep", immediate_sleep)

    settings = SimpleNamespace(
        plex_url="http://plex",
        plex_token=SimpleNamespace(get_secret_value=lambda: "tok"),
        plex_music_section="Music",
    )
    with pytest.raises(RuntimeError, match="Plex unavailable after 2 startup attempts"):
        asyncio.run(local_main._init_plex_with_retry(settings, attempts=2, base_delay_sec=0.01))


def test_speaker_queue_load_media_failed_and_previous():
    cast = SimpleNamespace(media_controller=MagicMock(), name="TestRoom")
    queue = SpeakerQueue(cast)
    track1 = track("song1")
    track2 = track("song2")
    track3 = track("song3")
    queue.replace([track1, track2, track3], repeat=False)

    assert queue.current.title == "song1"

    # Simulate load_media_failed -> should automatically skip to track2
    queue.load_media_failed(queue_item_id=1, error_code="LOAD_FAILED")
    queue.executor.shutdown(wait=True)
    assert queue.current.title == "song2"

    # Advance to track3
    queue.executor = ThreadPoolExecutor(max_workers=1)
    queue.skip()
    assert queue.current.title == "song3"

    # Call previous -> should go back to track2
    queue.previous()
    assert queue.current.title == "song2"
    queue.executor.shutdown(wait=True)


def test_stale_load_media_failed_does_not_advance_replacement_queue():
    """A delayed failure from a prior queue must not skip the replacement's first track."""
    cast = SimpleNamespace(media_controller=MagicMock(), name="TestRoom")
    queue = SpeakerQueue(cast)
    old_a = track("old-a")
    old_b = track("old-b")
    new_a = track("new-a")
    new_b = track("new-b")

    queue.replace([old_a, old_b], repeat=False)
    assert queue.current.title == "old-a"
    stale_generation = queue.play_generation
    assert list(queue.pending_load_generations) == [stale_generation]

    queue.replace([new_a, new_b], repeat=False)
    assert queue.current.title == "new-a"
    # replace() drops superseded pending loads so a late failure has nothing to claim.
    assert list(queue.pending_load_generations) == [queue.play_generation]
    assert stale_generation != queue.generation

    # Stale callback after replace: pending tokens from the old load were cleared,
    # so this must be ignored when we only have a drained/cleared stale token.
    with queue.lock:
        # Recreate the post-replace stale condition: old token gone, then a late
        # failure arrives without a pending load for that old generation.
        saved_pending = deque(queue.pending_load_generations)
        queue.pending_load_generations.clear()
    queue.load_media_failed(queue_item_id=99, error_code="LOAD_FAILED")
    with queue.lock:
        queue.pending_load_generations = saved_pending
    # Old generation advance must also be a no-op against the new queue.
    queue.executor.submit(queue._advance, stale_generation)
    queue.executor.shutdown(wait=True)

    assert queue.current.title == "new-a"
    assert [t.title for t in queue.queue] == ["new-b"]

def test_pubsub_worker_native_dlq_delegation():
    from unittest.mock import MagicMock, patch
    import json
    from plex_cast_bridge.pubsub_worker import PubSubWorker

    settings = SimpleNamespace(
        gcp_project_id="test-proj",
        gcp_pubsub_subscription_id="test-sub"
    )
    dispatch = MagicMock()
    loop = MagicMock()

    with patch("plex_cast_bridge.pubsub_worker.pubsub_v1.SubscriberClient"):
        worker = PubSubWorker(settings, loop, dispatch)

    mock_msg = MagicMock()
    mock_msg.message_id = "msg-native-dlq-1"
    mock_msg.data = json.dumps({"action": "play", "query": "Unknown"}).encode("utf-8")
    mock_msg.delivery_attempt = 3

    completed_future = MagicMock()
    completed_future.result.side_effect = RuntimeError("Playback crash")

    def mock_run_coroutine(coro, _loop):
        fut = MagicMock()
        def mock_add_done(cb):
            cb(completed_future)
        fut.add_done_callback = mock_add_done
        return fut

    with patch("asyncio.run_coroutine_threadsafe", side_effect=mock_run_coroutine):
        worker._callback(mock_msg)

    mock_msg.nack.assert_called_once()
    mock_msg.ack.assert_not_called()


def test_pubsub_worker_local_dlq_retention(tmp_path):
    from unittest.mock import MagicMock, patch
    import json
    from plex_cast_bridge.pubsub_worker import PubSubWorker, MAX_MESSAGE_RETRIES
    import plex_cast_bridge.pubsub_worker as pw

    settings = SimpleNamespace(
        gcp_project_id="test-proj",
        gcp_pubsub_subscription_id="test-sub"
    )
    dispatch = MagicMock()
    loop = MagicMock()

    with patch("plex_cast_bridge.pubsub_worker.pubsub_v1.SubscriberClient"):
        worker = PubSubWorker(settings, loop, dispatch)

    test_dlq_file = tmp_path / "dead_letter_test.jsonl"
    with patch.object(pw, "DEAD_LETTER_LOG", test_dlq_file):
        mock_msg = MagicMock(spec=["message_id", "data", "ack", "nack"])
        mock_msg.message_id = "msg-poison-pill"
        mock_msg.data = json.dumps({"action": "play", "query": "CrashArtist"}).encode("utf-8")

        completed_future = MagicMock()
        completed_future.result.side_effect = RuntimeError("Persistent crash")

        def mock_run_coroutine(coro, _loop):
            fut = MagicMock()
            def mock_add_done(cb):
                cb(completed_future)
            fut.add_done_callback = mock_add_done
            return fut

        with patch("asyncio.run_coroutine_threadsafe", side_effect=mock_run_coroutine):
            for i in range(MAX_MESSAGE_RETRIES - 1):
                worker._callback(mock_msg)
                mock_msg.nack.assert_called()
                mock_msg.ack.assert_not_called()
                mock_msg.nack.reset_mock()

            worker._callback(mock_msg)
            mock_msg.ack.assert_called_once()
            assert test_dlq_file.exists()
            lines = test_dlq_file.read_text(encoding="utf-8").strip().splitlines()
            assert len(lines) == 1
            record = json.loads(lines[0])
            assert record["message_id"] == "msg-poison-pill"
            assert "Persistent crash" in record["error"]


def test_data_dir_isolation_and_catalog_persistence(tmp_path, monkeypatch):
    from plex_cast_bridge.main import get_catalog_path
    from plex_cast_bridge.pubsub_worker import get_dead_letter_log_path
    from plex_cast_bridge import catalog_store

    test_data = tmp_path / "custom_data"
    monkeypatch.setattr(catalog_store, "DATA_DIR", test_data)
    monkeypatch.delenv("CATALOG_PATH", raising=False)
    monkeypatch.setenv("DATA_DIR", str(test_data))

    # Writing path seeds catalog into custom DATA_DIR
    write_p = get_catalog_path(for_writing=True)
    assert write_p == test_data / "catalog.json"
    assert test_data.exists()

    # DLQ path defaults to DATA_DIR
    dlq_p = get_dead_letter_log_path()
    assert dlq_p == test_data / "dead_letter_queue.jsonl"


def test_local_bridge_health_endpoint():
    client = TestClient(app)
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_cross_platform_speaker_discovery(monkeypatch):
    import pychromecast
    from plex_cast_bridge.cast_manager import CastManager

    # Mock pychromecast to return no network chromecasts
    monkeypatch.setattr(pychromecast, "get_chromecasts", lambda timeout: ([], MagicMock()))

    # Case 1: macOS platform -> includes test speaker by default
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.delenv("ENABLE_TEST_SPEAKER", raising=False)
    cm = CastManager()
    devices = cm.discover()
    test_speakers = [d for d in devices if d["name"] == "test speaker"]
    assert len(test_speakers) == 1
    assert test_speakers[0]["model"] == "macOS afplay"
    assert test_speakers[0]["room"] == "Mac Local"

    # Case 2: Linux platform (default) -> omits test speaker
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("ENABLE_TEST_SPEAKER", raising=False)
    cm_linux = CastManager()
    devices_linux = cm_linux.discover()
    assert not any(d["name"] == "test speaker" for d in devices_linux)

    # Case 3: Linux with explicit ENABLE_TEST_SPEAKER=true -> includes headless simulation
    monkeypatch.setenv("ENABLE_TEST_SPEAKER", "true")
    cm_linux_sim = CastManager()
    devices_sim = cm_linux_sim.discover()
    test_sim = [d for d in devices_sim if d["name"] == "test speaker"]
    assert len(test_sim) == 1
    assert test_sim[0]["model"] == "Headless Simulation"
    assert test_sim[0]["room"] == "Local Host"


def test_local_speaker_platform_status(monkeypatch, tmp_path):
    from plex_cast_bridge.local_speaker import LocalSpeaker

    # On macOS with afplay
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/afplay" if cmd == "afplay" else None)
    sp_mac = LocalSpeaker(directory=tmp_path / "mac", enable_playback=True)
    assert sp_mac.enable_playback is True
    assert sp_mac.status()["speaker"] == "Mac Speaker (Emulated)"

    # On Linux, afplay is never invoked and status reports headless simulation
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/afplay" if cmd == "afplay" else None)
    sp_linux = LocalSpeaker(directory=tmp_path / "linux", enable_playback=True)
    assert sp_linux.enable_playback is False
    assert sp_linux.status()["speaker"] == "Headless Simulation Speaker"


def test_catalog_fallback_speaker_resolution(tmp_path, monkeypatch):
    import json
    import pychromecast
    from plex_cast_bridge.cast_manager import CastManager

    # Simulate dynamic discovery returning nothing
    monkeypatch.setattr(pychromecast, "get_listed_chromecasts", lambda friendly_names, discovery_timeout: ([], MagicMock()))

    catalog_file = tmp_path / "catalog.json"
    catalog_file.write_text(json.dumps({
        "speakers": [
            {
                "speaker_name": "Kitchen display",
                "roomHint": "Kitchen",
                "host": "192.168.88.83",
                "port": 8009,
                "model": "Google Nest Hub",
                "uuid": "test-kitchen-uuid",
                "enabled": True,
            }
        ]
    }), encoding="utf-8")

    mock_cast = MagicMock()
    mock_cast.name = "Kitchen display"
    mock_cast.wait = MagicMock()
    monkeypatch.setattr(pychromecast, "get_chromecast_from_host", lambda args: mock_cast)

    cm = CastManager(catalog_path=catalog_file)
    queue = cm.get("Kitchen")
    assert queue.cast == mock_cast
    assert mock_cast.wait.called
