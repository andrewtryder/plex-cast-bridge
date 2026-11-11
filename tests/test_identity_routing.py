"""Exercise virtual-device routing through fulfillment and local transport."""

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pychromecast
import pytest

from plex_cast_bridge.api_models import Transport
from plex_cast_bridge.cast_manager import CastManager, SpeakerQueue
from plex_cast_bridge.playback_service import transport


def test_fulfillment_message_routes_by_uuid(client, auth_headers, test_settings, tmp_path, monkeypatch):
    speakers = [
        {"id": "plex-jukebox:a", "speaker_name": "Twin", "cast_uuid": "uuid-a", "enabled": True, "google_home_visible": True},
        {"id": "plex-jukebox:b", "speaker_name": "Twin", "cast_uuid": "uuid-b", "enabled": True, "google_home_visible": True},
    ]
    catalog = {"speakers": speakers}
    assert client.put("/internal/catalog", headers={"X-API-Key": "test-webhook-key"},
                      json={"version": 1, "catalog": catalog, "request_sync": False}).status_code == 200
    published = []
    client.app.state.pubsub.publish_sync = lambda payload: published.append(payload) or "message"
    response = client.post("/fulfillment", headers=auth_headers, json={
        "requestId": "1", "inputs": [{"intent": "action.devices.EXECUTE", "payload": {"commands": [{
            "devices": [{"id": "plex-jukebox:b"}],
            "execution": [{"command": "action.devices.commands.mediaNext", "params": {}}],
        }]}}],
    })
    assert response.json()["payload"]["commands"][0]["status"] == "SUCCESS"
    assert published[0]["device_id"] == "plex-jukebox:b"
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(catalog))
    manager = CastManager(catalog_path=path)
    info = SimpleNamespace(host="10.0.0.22", port=8009, uuid="uuid-b", model_name="Group", friendly_name="Renamed")
    discovered = SimpleNamespace(uuid="uuid-b", cast_info=info, disconnect=lambda: None)
    browser = MagicMock()
    monkeypatch.setattr(pychromecast, "get_chromecasts", lambda **kwargs: ([discovered], browser))
    queue = MagicMock()
    connect = MagicMock(return_value=queue)
    monkeypatch.setattr(manager, "_connect_from_host", connect)
    asyncio.run(transport(cast=manager, settings=SimpleNamespace(default_speaker_name="wrong"), command=Transport(**published[0])))
    assert connect.call_args.args[1][0] == "10.0.0.22"
    assert connect.call_args.args[1][2] == "uuid-b"
    queue.skip.assert_called_once()


def test_uuid_route_fails_closed_when_not_discovered(tmp_path, monkeypatch):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"speakers": [{"id": "plex-jukebox:a", "speaker_name": "Twin", "cast_uuid": "uuid-a", "enabled": True, "google_home_visible": True}]}))
    manager = CastManager(catalog_path=path)
    monkeypatch.setattr(pychromecast, "get_chromecasts", lambda **kwargs: ([], MagicMock()))
    with pytest.raises(ValueError, match="UUID unavailable"):
        manager.get_device("plex-jukebox:a")


def test_mute_routes_to_cast_group_by_device_uuid(client, auth_headers, tmp_path, monkeypatch):
    catalog = {"speakers": [{
        "id": "plex-jukebox:whole-house",
        "speaker_name": "Whole House",
        "cast_uuid": "group-uuid",
        "enabled": True,
        "google_home_visible": True,
    }]}
    assert client.put("/internal/catalog", headers={"X-API-Key": "test-webhook-key"},
                      json={"version": 1, "catalog": catalog, "request_sync": False}).status_code == 200
    published = []
    client.app.state.pubsub.publish_sync = lambda payload: published.append(payload) or "message"
    response = client.post("/fulfillment", headers=auth_headers, json={
        "requestId": "group-mute",
        "inputs": [{"intent": "action.devices.EXECUTE", "payload": {"commands": [{
            "devices": [{"id": "plex-jukebox:whole-house"}],
            "execution": [{"command": "action.devices.commands.mute", "params": {"mute": True}}],
        }]}}],
    })
    assert response.json()["payload"]["commands"][0]["status"] == "SUCCESS"
    assert published[0]["device_id"] == "plex-jukebox:whole-house"
    assert published[0]["speaker"] == "Whole House"

    catalog_file = tmp_path / "catalog.json"
    catalog_file.write_text(json.dumps(catalog))
    manager = CastManager(catalog_path=catalog_file)
    info = SimpleNamespace(
        host="10.0.0.80", port=8009, uuid="group-uuid",
        model_name="Google Cast Group", friendly_name="Whole House",
    )
    discovered = SimpleNamespace(uuid="group-uuid", cast_info=info, disconnect=lambda: None)
    monkeypatch.setattr(pychromecast, "get_chromecasts", lambda **kwargs: ([discovered], MagicMock()))
    queue = MagicMock()
    connect = MagicMock(return_value=queue)
    monkeypatch.setattr(manager, "_connect_from_host", connect)
    asyncio.run(transport(
        cast=manager,
        settings=SimpleNamespace(default_speaker_name="wrong"),
        command=Transport(**published[0]),
    ))
    assert connect.call_args.args[1][2] == "group-uuid"
    queue.mute.assert_called_once_with(True)


def test_muted_device_unmutes_after_uuid_connection_reconnects(tmp_path, monkeypatch):
    catalog_file = tmp_path / "catalog.json"
    catalog_file.write_text(json.dumps({"speakers": [{
        "id": "plex-jukebox:bedroom",
        "speaker_name": "Bedroom",
        "cast_uuid": "bedroom-uuid",
        "enabled": True,
        "google_home_visible": True,
    }]}))
    manager = CastManager(catalog_path=catalog_file, default_volume=0.35)

    disconnected_cast = SimpleNamespace(
        media_controller=MagicMock(), name="Bedroom",
        status=SimpleNamespace(volume_level=0.0),
        socket_client=SimpleNamespace(is_connected=False),
        disconnect=MagicMock(),
    )
    stale_queue = SpeakerQueue(disconnected_cast, default_volume=0.35)
    stale_queue._mute_volume = 0.62
    stale_queue._is_muted = True
    manager.speakers["uuid:bedroom-uuid"] = stale_queue

    info = SimpleNamespace(
        host="10.0.0.42", port=8009, uuid="bedroom-uuid",
        model_name="Nest Mini", friendly_name="Bedroom",
    )
    discovered = SimpleNamespace(uuid="bedroom-uuid", cast_info=info, disconnect=MagicMock())
    monkeypatch.setattr(pychromecast, "get_chromecasts", lambda **kwargs: ([discovered], MagicMock()))
    reconnected_cast = SimpleNamespace(
        media_controller=MagicMock(), name="Bedroom",
        status=SimpleNamespace(volume_level=0.0),
        socket_client=SimpleNamespace(is_connected=True),
        set_volume=MagicMock(), wait=MagicMock(), disconnect=MagicMock(),
    )
    monkeypatch.setattr(pychromecast, "get_chromecast_from_host", lambda _host: reconnected_cast)

    queue = manager.get_device("plex-jukebox:bedroom")
    assert queue is not stale_queue
    queue.mute(False)
    reconnected_cast.set_volume.assert_called_once_with(0.35)
    queue.close()


def test_uuid_route_serializes_discovery_and_ignores_cleanup_errors(tmp_path, monkeypatch):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"speakers": [{
        "id": "plex-jukebox:a",
        "speaker_name": "Bedroom",
        "cast_uuid": "uuid-a",
        "enabled": True,
        "google_home_visible": True,
    }]}))
    manager = CastManager(catalog_path=path)
    info = SimpleNamespace(host="10.0.0.22", port=8009, uuid="uuid-a", model_name="Nest", friendly_name="Bedroom")
    found = SimpleNamespace(uuid="uuid-a", cast_info=info, disconnect=MagicMock(side_effect=RuntimeError("thread not started")))
    browser = MagicMock()
    browser.stop_discovery.side_effect = RuntimeError("discovery already stopped")
    scan_started = threading.Event()
    monkeypatch.setattr(pychromecast, "get_chromecasts", lambda **kwargs: (scan_started.set() or [found], browser))
    queue = MagicMock()
    monkeypatch.setattr(manager, "_connect_from_host", lambda *_args, **_kwargs: queue)

    manager._discovery_lock.acquire()
    result = []
    worker = threading.Thread(target=lambda: result.append(manager.get_device("plex-jukebox:a")))
    worker.start()
    scan_started_early = scan_started.wait(0.05)
    manager._discovery_lock.release()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert not scan_started_early
    assert scan_started.is_set()
    assert result == [queue]
