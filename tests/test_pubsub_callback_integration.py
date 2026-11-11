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


def test_callback_nacks_dispatch_failures_and_acks_invalid_messages(subscriber):
    async def dispatch(payload):
        raise ValueError("target unavailable")

    worker = subscriber(dispatch)
    msg, done = message({"action": "play", "query": "music", "device_id": "missing"})
    worker._callback(msg)
    assert done.wait(2)
    msg.nack.assert_called_once()
    msg.ack.assert_not_called()
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
