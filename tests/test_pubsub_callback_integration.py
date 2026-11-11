"""Real subscriber callback with an isolated event loop and fake Pub/Sub."""
import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from plex_cast_bridge.pubsub_worker import PubSubWorker


@pytest.fixture
def subscriber(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr("plex_cast_bridge.pubsub_worker.pubsub_v1.SubscriberClient", lambda: client)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever)
    thread.start()
    workers = []

    def make(dispatch):
        worker = PubSubWorker(SimpleNamespace(gcp_project_id="test", gcp_pubsub_subscription_id="sub"), loop, dispatch)
        workers.append(worker)
        return worker

    yield make
    for worker in workers:
        worker.close()
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2)
    loop.close()
    assert not thread.is_alive()


def message(payload):
    result = MagicMock()
    result.data = json.dumps(payload).encode()
    result.message_id = "m1"
    result.delivery_attempt = 1
    done = threading.Event()
    result.ack.side_effect = done.set
    result.nack.side_effect = done.set
    return result, done


def test_callback_dispatches_device_id_and_acknowledges(subscriber, caplog):
    observed = []

    async def dispatch(payload):
        observed.append(payload)

    worker = subscriber(dispatch)
    msg, done = message({
        "action": "play",
        "query": "Private Artist Query",
        "speaker": "Twin",
        "device_id": "plex-jukebox:b",
    })
    worker._callback(msg)
    assert done.wait(2)
    assert observed[0]["device_id"] == "plex-jukebox:b"
    assert "Google Home command received action=play" in caplog.text
    assert "Google Home command completed action=play" in caplog.text
    assert "speaker='Twin'" in caplog.text
    assert "device_id='plex-jukebox:b'" in caplog.text
    assert "Private Artist Query" not in caplog.text
    msg.ack.assert_called_once()
    msg.nack.assert_not_called()


def test_callback_nacks_dispatch_failures_and_acks_invalid_messages(subscriber, caplog):
    async def dispatch(payload):
        raise ValueError(f"target unavailable for {payload.get('query')}")

    worker = subscriber(dispatch)
    private_query = "Private Song Title"
    msg, done = message({"action": "play", "query": private_query, "device_id": "missing"})
    worker._callback(msg)
    assert done.wait(2)
    msg.nack.assert_called_once()
    msg.ack.assert_not_called()
    assert "error_type=ValueError" in caplog.text
    assert private_query not in caplog.text
    invalid, done = message({"action": "bogus"})
    worker._callback(invalid)
    assert done.wait(2)
    invalid.ack.assert_called_once()


def test_callback_routes_duplicate_names_by_uuid(subscriber, tmp_path, monkeypatch):
    import pychromecast
    from plex_cast_bridge.api_models import Transport
    from plex_cast_bridge.cast_manager import CastManager
    from plex_cast_bridge.playback_service import transport

    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"speakers": [
        {"id": "plex-jukebox:a", "speaker_name": "Twin", "cast_uuid": "uuid-a", "enabled": True, "google_home_visible": True},
        {"id": "plex-jukebox:b", "speaker_name": "Twin", "cast_uuid": "uuid-b", "enabled": True, "google_home_visible": True},
    ]}))
    manager = CastManager(catalog_path=catalog)
    info = SimpleNamespace(host="10.0.0.2", port=8009, uuid="uuid-b", model_name="Group", friendly_name="Renamed")
    found = SimpleNamespace(uuid="uuid-b", cast_info=info, disconnect=lambda: None)
    monkeypatch.setattr(pychromecast, "get_chromecasts", lambda **kwargs: ([found], MagicMock()))
    queue = MagicMock()
    connect = MagicMock(return_value=queue)
    monkeypatch.setattr(manager, "_connect_from_host", connect)

    async def dispatch(payload):
        return await transport(cast=manager, settings=SimpleNamespace(default_speaker_name="Twin"), command=Transport(**payload))

    worker = subscriber(dispatch)
    msg, done = message({"action": "next", "speaker": "Twin", "device_id": "plex-jukebox:b"})
    worker._callback(msg)
    assert done.wait(2)
    msg.ack.assert_called_once()
    assert connect.call_args.args[1][2] == "uuid-b"
    queue.skip.assert_called_once()

    missing, done = message({"action": "next", "speaker": "Twin", "device_id": "plex-jukebox:a"})
    worker._callback(missing)
    assert done.wait(2)
    missing.nack.assert_called_once()
    assert connect.call_count == 1


@pytest.mark.parametrize(
    ("device_id", "cast_uuid", "model_name"),
    [
        ("plex-jukebox:bedroom", "speaker-uuid", "Nest Mini"),
        ("plex-jukebox:whole-house", "group-uuid", "Google Cast Group"),
    ],
)
def test_worker_dispatches_mute_and_unmute_to_selected_cast_uuid(
    subscriber, tmp_path, monkeypatch, device_id, cast_uuid, model_name,
):
    import pychromecast
    from plex_cast_bridge.api_models import Transport
    from plex_cast_bridge.cast_manager import CastManager
    from plex_cast_bridge.playback_service import transport

    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"speakers": [{
        "id": device_id,
        "speaker_name": "Whole House" if "whole-house" in device_id else "Bedroom",
        "cast_uuid": cast_uuid,
        "enabled": True,
        "google_home_visible": True,
    }]}))
    manager = CastManager(catalog_path=catalog, default_volume=0.35)
    cast_info = SimpleNamespace(
        host="10.0.0.44", port=8009, uuid=cast_uuid,
        model_name=model_name, friendly_name="Cast target",
    )
    discovered = SimpleNamespace(uuid=cast_uuid, cast_info=cast_info, disconnect=MagicMock())
    monkeypatch.setattr(pychromecast, "get_chromecasts", lambda **kwargs: ([discovered], MagicMock()))

    status = SimpleNamespace(volume_level=0.61)
    cast = SimpleNamespace(
        media_controller=MagicMock(), name="Cast target", status=status,
        socket_client=SimpleNamespace(is_connected=True),
        set_volume=MagicMock(side_effect=lambda level: setattr(status, "volume_level", level)),
        wait=MagicMock(), disconnect=MagicMock(),
    )
    connected = []
    monkeypatch.setattr(pychromecast, "get_chromecast_from_host", lambda host: connected.append(host) or cast)

    async def dispatch(payload):
        return await transport(
            cast=manager,
            settings=SimpleNamespace(default_speaker_name="must-not-route-here"),
            command=Transport(**payload),
        )

    worker = subscriber(dispatch)
    for muted in (True, False):
        msg, done = message({"action": "mute", "mute": muted, "speaker": "duplicate room", "device_id": device_id})
        worker._callback(msg)
        assert done.wait(2)
        msg.ack.assert_called_once()
        msg.nack.assert_not_called()

    assert connected[0][2] == cast_uuid
    assert [call.args[0] for call in cast.set_volume.call_args_list] == [0.0, 0.61]


def test_worker_nacks_volume_failures_without_marking_them_completed(
    subscriber, tmp_path, monkeypatch, caplog,
):
    import pychromecast
    from plex_cast_bridge.api_models import Transport
    from plex_cast_bridge.cast_manager import CastManager
    from plex_cast_bridge.playback_service import transport

    device_id = "plex-jukebox:bedroom"
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"speakers": [{
        "id": device_id, "speaker_name": "Bedroom", "cast_uuid": "bedroom-uuid",
        "enabled": True, "google_home_visible": True,
    }]}))
    manager = CastManager(catalog_path=catalog, default_volume=0.35)
    cast_info = SimpleNamespace(
        host="10.0.0.45", port=8009, uuid="bedroom-uuid",
        model_name="Nest Mini", friendly_name="Bedroom",
    )
    discovered = SimpleNamespace(uuid="bedroom-uuid", cast_info=cast_info, disconnect=MagicMock())
    monkeypatch.setattr(pychromecast, "get_chromecasts", lambda **kwargs: ([discovered], MagicMock()))

    status = SimpleNamespace(volume_level=0.61)
    fail_volume = {"enabled": True}

    def set_volume(level):
        if fail_volume["enabled"]:
            raise RuntimeError("Cast volume command failed")
        status.volume_level = level

    cast = SimpleNamespace(
        media_controller=MagicMock(), name="Bedroom", status=status,
        socket_client=SimpleNamespace(is_connected=True),
        set_volume=MagicMock(side_effect=set_volume),
        wait=MagicMock(), disconnect=MagicMock(),
    )
    monkeypatch.setattr(pychromecast, "get_chromecast_from_host", lambda _host: cast)

    async def dispatch(payload):
        return await transport(
            cast=manager,
            settings=SimpleNamespace(default_speaker_name="Bedroom"),
            command=Transport(**payload),
        )

    worker = subscriber(dispatch)
    failed_mute, done = message({"action": "mute", "mute": True, "device_id": device_id})
    worker._callback(failed_mute)
    assert done.wait(2)
    failed_mute.nack.assert_called_once()
    failed_mute.ack.assert_not_called()
    queue = manager.speakers["uuid:bedroom-uuid"]
    assert queue._is_muted is False
    assert queue._mute_volume is None
    assert "Google Home command failed action=mute" in caplog.text
    assert "Google Home command completed action=mute" not in caplog.text

    fail_volume["enabled"] = False
    succeeded_mute, done = message({"action": "mute", "mute": True, "device_id": device_id})
    worker._callback(succeeded_mute)
    assert done.wait(2)
    succeeded_mute.ack.assert_called_once()
    assert queue._is_muted is True
    assert queue._mute_volume == 0.61

    caplog.clear()
    fail_volume["enabled"] = True
    failed_unmute, done = message({"action": "mute", "mute": False, "device_id": device_id})
    worker._callback(failed_unmute)
    assert done.wait(2)
    failed_unmute.nack.assert_called_once()
    failed_unmute.ack.assert_not_called()
    assert queue._is_muted is True
    assert queue._mute_volume == 0.61
    assert "Google Home command failed action=mute" in caplog.text
    assert "Google Home command completed action=mute" not in caplog.text


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "mute"},
        {"action": "mute", "mute": None},
        {"action": "mute", "mute": "true"},
        {"action": "volume", "volume": True},
        {"action": "volume", "volume": 0.2, "relative_volume": 0.1},
    ],
)
def test_worker_rejects_invalid_volume_payloads_before_dispatch(subscriber, payload):
    dispatched = []

    async def dispatch(data):
        dispatched.append(data)

    worker = subscriber(dispatch)
    msg, done = message(payload)
    worker._callback(msg)
    assert done.wait(2)
    msg.ack.assert_called_once()
    msg.nack.assert_not_called()
    assert dispatched == []
