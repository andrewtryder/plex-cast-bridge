"""Exercise virtual-device routing through fulfillment and local transport."""

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import pychromecast
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from plex_cast_bridge.api_models import Command, Transport
from plex_cast_bridge.cast_manager import DIAL_IDENTITY_TIMEOUT, CastManager, SpeakerQueue
from plex_cast_bridge.playback_service import play, transport
from plex_cast_bridge.routes import router
from plex_cast_bridge.voice_cold_start import CastSession, PubSubMessageDeduper, VoiceColdStart


@pytest.mark.parametrize("payload", [{}, {"mute": None}, {"mute": 1}, {"mute": "true"}])
def test_transport_requires_strict_mute_boolean(payload):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Transport(action="mute", **payload)


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


MB_UUID = "11111111-1111-4111-8111-111111111111"
KITCHEN_UUID = "22222222-2222-4222-8222-222222222222"


def _write_catalog(tmp_path, speakers):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"speakers": speakers}), encoding="utf-8")
    return path


def _mb_record(**overrides):
    rec = {
        "id": "plex-jukebox:Master Bedroom",
        "google_home_device_id": "plex-jukebox:Master Bedroom",
        "speaker_name": "Master Bedroom display",
        "cast_uuid": MB_UUID,
        "host": "192.168.88.126",
        "port": 8009,
        "kind": "speaker",
        "model": "Google Nest Hub",
        "enabled": True,
        "google_home_visible": True,
    }
    rec.update(overrides)
    return rec


def _make_live_cast(name="Master Bedroom display", volume=0.25):
    mc = MagicMock()
    mc.status = SimpleNamespace(player_state="IDLE")
    return SimpleNamespace(
        media_controller=mc,
        name=name,
        status=SimpleNamespace(volume_level=volume),
        socket_client=SimpleNamespace(is_connected=True),
        set_volume=MagicMock(),
        wait=MagicMock(),
        disconnect=MagicMock(),
        quit_app=MagicMock(),
    )


def _dial_status(uuid_value, *, cast_type="cast", model_name="Google Nest Hub", friendly_name="Master Bedroom display"):
    return SimpleNamespace(
        friendly_name=friendly_name,
        model_name=model_name,
        manufacturer="Google Inc.",
        uuid=uuid_value,
        cast_type=cast_type,
        multizone_supported=True,
    )


def test_get_device_falls_back_to_dial_verified_catalog_host_when_mdns_empty(tmp_path, monkeypatch):
    path = _write_catalog(tmp_path, [_mb_record(cast_uuid=MB_UUID.replace("-", "").upper())])
    manager = CastManager(catalog_path=path)

    browser = MagicMock()
    monkeypatch.setattr(pychromecast, "get_chromecasts", lambda **kwargs: ([], browser))
    dial_calls = []
    monkeypatch.setattr(
        pychromecast.dial,
        "get_device_info",
        lambda host, timeout=30: dial_calls.append((host, timeout)) or _dial_status(UUID(MB_UUID)),
    )
    connected = []
    live_cast = _make_live_cast()
    monkeypatch.setattr(
        pychromecast,
        "get_chromecast_from_host",
        lambda host_tuple: connected.append(host_tuple) or live_cast,
    )

    queue = manager.get_device("plex-jukebox:Master Bedroom")
    try:
        assert dial_calls == [("192.168.88.126", DIAL_IDENTITY_TIMEOUT)]
        assert connected == [
            ("192.168.88.126", 8009, UUID(MB_UUID), "Google Nest Hub", "Master Bedroom display")
        ]
        assert queue.cast is live_cast
        assert manager.speakers[f"uuid:{MB_UUID}"] is queue
        browser.stop_discovery.assert_called_once()
    finally:
        queue.close()


def test_get_device_falls_back_to_dial_when_mdns_raises(tmp_path, monkeypatch):
    path = _write_catalog(tmp_path, [_mb_record()])
    manager = CastManager(catalog_path=path)

    def broken_mdns(**_kwargs):
        raise OSError("multicast disabled")

    monkeypatch.setattr(pychromecast, "get_chromecasts", broken_mdns)
    monkeypatch.setattr(
        pychromecast.dial,
        "get_device_info",
        lambda host, timeout=30: _dial_status(UUID(MB_UUID)),
    )
    live_cast = _make_live_cast()
    monkeypatch.setattr(pychromecast, "get_chromecast_from_host", lambda _host: live_cast)

    queue = manager.get_device("plex-jukebox:Master Bedroom")
    try:
        assert queue.cast is live_cast
    finally:
        queue.close()


def test_get_device_prefers_mdns_and_skips_dial_when_mdns_succeeds(tmp_path, monkeypatch):
    path = _write_catalog(tmp_path, [_mb_record(host="192.168.88.126")])
    manager = CastManager(catalog_path=path)

    info = SimpleNamespace(
        host="10.0.0.55",
        port=8009,
        uuid=UUID(MB_UUID),
        model_name="Google Nest Hub",
        friendly_name="Master Bedroom display",
    )
    discovered = SimpleNamespace(uuid=UUID(MB_UUID), cast_info=info, disconnect=MagicMock())
    monkeypatch.setattr(pychromecast, "get_chromecasts", lambda **kwargs: ([discovered], MagicMock()))
    dial_mock = MagicMock()
    monkeypatch.setattr(pychromecast.dial, "get_device_info", dial_mock)
    connected = []
    live_cast = _make_live_cast()
    monkeypatch.setattr(
        pychromecast,
        "get_chromecast_from_host",
        lambda host_tuple: connected.append(host_tuple) or live_cast,
    )

    queue = manager.get_device("plex-jukebox:Master Bedroom")
    try:
        dial_mock.assert_not_called()
        assert connected[0][0] == "10.0.0.55"
    finally:
        queue.close()


def test_get_device_fails_closed_when_mdns_reports_ambiguous_uuid(tmp_path, monkeypatch):
    path = _write_catalog(tmp_path, [_mb_record()])
    manager = CastManager(catalog_path=path)

    info1 = SimpleNamespace(host="10.0.0.10", port=8009, uuid=UUID(MB_UUID), model_name="Nest", friendly_name="A")
    info2 = SimpleNamespace(host="10.0.0.11", port=8009, uuid=UUID(MB_UUID), model_name="Nest", friendly_name="B")
    casts = [
        SimpleNamespace(uuid=UUID(MB_UUID), cast_info=info1, disconnect=MagicMock()),
        SimpleNamespace(uuid=UUID(MB_UUID), cast_info=info2, disconnect=MagicMock()),
    ]
    monkeypatch.setattr(pychromecast, "get_chromecasts", lambda **kwargs: (casts, MagicMock()))
    dial_mock = MagicMock()
    monkeypatch.setattr(pychromecast.dial, "get_device_info", dial_mock)
    connect_mock = MagicMock()
    monkeypatch.setattr(pychromecast, "get_chromecast_from_host", connect_mock)

    with pytest.raises(ValueError, match="unavailable or ambiguous"):
        manager.get_device("plex-jukebox:Master Bedroom")
    dial_mock.assert_not_called()
    connect_mock.assert_not_called()


@pytest.mark.parametrize("reported_uuid", [UUID(KITCHEN_UUID), None, "", "not-a-uuid"])
def test_get_device_rejects_mismatched_missing_or_malformed_dial_uuid(tmp_path, monkeypatch, reported_uuid):
    path = _write_catalog(tmp_path, [_mb_record()])
    manager = CastManager(catalog_path=path)
    monkeypatch.setattr(pychromecast, "get_chromecasts", lambda **kwargs: ([], MagicMock()))
    monkeypatch.setattr(
        pychromecast.dial,
        "get_device_info",
        lambda host, timeout=30: _dial_status(reported_uuid),
    )
    connect_mock = MagicMock()
    monkeypatch.setattr(pychromecast, "get_chromecast_from_host", connect_mock)

    with pytest.raises(ValueError, match="Cast UUID"):
        manager.get_device("plex-jukebox:Master Bedroom")
    connect_mock.assert_not_called()
    assert manager.speakers == {}


@pytest.mark.parametrize(
    "overrides",
    [
        {"cast_uuid": ""},
        {"cast_uuid": "not-a-uuid"},
        {"host": ""},
        {"host": "local"},
        {"host": "0.0.0.0"},
        {"host": "http://192.168.88.126"},
        {"port": 0},
        {"port": 70000},
        {"port": True},
    ],
)
def test_get_device_rejects_missing_or_malformed_catalog_identity_or_host(tmp_path, monkeypatch, overrides):
    path = _write_catalog(tmp_path, [_mb_record(**overrides)])
    manager = CastManager(catalog_path=path)
    monkeypatch.setattr(pychromecast, "get_chromecasts", lambda **kwargs: ([], MagicMock()))
    dial_mock = MagicMock()
    monkeypatch.setattr(pychromecast.dial, "get_device_info", dial_mock)
    connect_mock = MagicMock()
    monkeypatch.setattr(pychromecast, "get_chromecast_from_host", connect_mock)

    with pytest.raises(ValueError, match="Cast UUID unavailable"):
        manager.get_device("plex-jukebox:Master Bedroom")
    dial_mock.assert_not_called()
    connect_mock.assert_not_called()


def test_get_device_rejects_dhcp_reassigned_host_belonging_to_another_speaker(tmp_path, monkeypatch):
    path = _write_catalog(
        tmp_path,
        [
            _mb_record(host="192.168.88.126"),
            {
                "id": "plex-jukebox:Kitchen",
                "google_home_device_id": "plex-jukebox:Kitchen",
                "speaker_name": "Kitchen display",
                "cast_uuid": KITCHEN_UUID,
                "host": "192.168.88.126",
                "port": 8009,
                "kind": "speaker",
                "model": "Google Nest Hub",
                "enabled": True,
                "google_home_visible": True,
            },
        ],
    )
    manager = CastManager(catalog_path=path)
    monkeypatch.setattr(pychromecast, "get_chromecasts", lambda **kwargs: ([], MagicMock()))
    monkeypatch.setattr(
        pychromecast.dial,
        "get_device_info",
        lambda host, timeout=30: _dial_status(UUID(KITCHEN_UUID), friendly_name="Kitchen display"),
    )
    connected = []
    kitchen_cast = _make_live_cast(name="Kitchen display")
    monkeypatch.setattr(
        pychromecast,
        "get_chromecast_from_host",
        lambda host_tuple: connected.append(host_tuple) or kitchen_cast,
    )

    with pytest.raises(ValueError, match="different Cast device"):
        manager.get_device("plex-jukebox:Master Bedroom")
    assert connected == []

    queue = manager.get_device("plex-jukebox:Kitchen")
    try:
        assert len(connected) == 1
        assert connected[0][2] == UUID(KITCHEN_UUID)
    finally:
        queue.close()


@pytest.mark.parametrize("dial_behavior", [lambda *_a, **_k: None, TimeoutError("dial timeout"), OSError("no route")])
def test_get_device_fails_closed_when_dial_unreachable_or_times_out(tmp_path, monkeypatch, dial_behavior):
    path = _write_catalog(tmp_path, [_mb_record()])
    manager = CastManager(catalog_path=path)
    monkeypatch.setattr(pychromecast, "get_chromecasts", lambda **kwargs: ([], MagicMock()))
    if callable(dial_behavior):
        monkeypatch.setattr(pychromecast.dial, "get_device_info", dial_behavior)
    else:
        def raise_err(*_args, **_kwargs):
            raise dial_behavior
        monkeypatch.setattr(pychromecast.dial, "get_device_info", raise_err)
    connect_mock = MagicMock()
    monkeypatch.setattr(pychromecast, "get_chromecast_from_host", connect_mock)

    with pytest.raises(ValueError, match="could not verify device identity"):
        manager.get_device("plex-jukebox:Master Bedroom")
    connect_mock.assert_not_called()
    assert manager.speakers == {}


def test_get_device_rejects_duplicate_virtual_device_ids_and_duplicate_uuids(tmp_path, monkeypatch):
    dup_id_path = _write_catalog(
        tmp_path,
        [_mb_record(), _mb_record(host="192.168.88.127", cast_uuid=KITCHEN_UUID)],
    )
    manager = CastManager(catalog_path=dup_id_path)
    mdns_mock = MagicMock()
    dial_mock = MagicMock()
    connect_mock = MagicMock()
    monkeypatch.setattr(pychromecast, "get_chromecasts", mdns_mock)
    monkeypatch.setattr(pychromecast.dial, "get_device_info", dial_mock)
    monkeypatch.setattr(pychromecast, "get_chromecast_from_host", connect_mock)

    with pytest.raises(ValueError, match="Unknown or conflicting virtual device"):
        manager.get_device("plex-jukebox:Master Bedroom")
    mdns_mock.assert_not_called()
    dial_mock.assert_not_called()
    connect_mock.assert_not_called()

    dup_uuid_path = _write_catalog(
        tmp_path,
        [
            _mb_record(),
            _mb_record(
                id="plex-jukebox:Duplicate",
                google_home_device_id="plex-jukebox:Duplicate",
                cast_uuid=MB_UUID.replace("-", ""),
            ),
        ],
    )
    dup_uuid_mgr = CastManager(catalog_path=dup_uuid_path)
    mdns_hit = SimpleNamespace(
        uuid=UUID(MB_UUID),
        cast_info=SimpleNamespace(
            host="192.168.88.126",
            port=8009,
            uuid=UUID(MB_UUID),
            model_name="Google Nest Hub",
            friendly_name="Master Bedroom display",
        ),
        disconnect=MagicMock(),
    )
    dup_mdns_mock = MagicMock(return_value=([mdns_hit], MagicMock()))
    monkeypatch.setattr(pychromecast, "get_chromecasts", dup_mdns_mock)
    with pytest.raises(ValueError, match="unavailable or ambiguous"):
        dup_uuid_mgr.get_device("plex-jukebox:Master Bedroom")
    dup_mdns_mock.assert_not_called()
    dial_mock.assert_not_called()
    connect_mock.assert_not_called()


@pytest.mark.parametrize(
    ("overrides", "expected_msg"),
    [
        ({"enabled": False}, "disabled or hidden"),
        ({"google_home_visible": False}, "disabled or hidden"),
        ({"removed": True}, "Unknown or conflicting"),
    ],
)
def test_get_device_rejects_disabled_hidden_or_removed_speakers(tmp_path, monkeypatch, overrides, expected_msg):
    path = _write_catalog(tmp_path, [_mb_record(**overrides)])
    manager = CastManager(catalog_path=path)
    mdns_mock = MagicMock()
    dial_mock = MagicMock()
    connect_mock = MagicMock()
    monkeypatch.setattr(pychromecast, "get_chromecasts", mdns_mock)
    monkeypatch.setattr(pychromecast.dial, "get_device_info", dial_mock)
    monkeypatch.setattr(pychromecast, "get_chromecast_from_host", connect_mock)

    with pytest.raises(ValueError, match=expected_msg):
        manager.get_device("plex-jukebox:Master Bedroom")
    mdns_mock.assert_not_called()
    dial_mock.assert_not_called()
    connect_mock.assert_not_called()


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "group"},
        {"is_group": True},
        {"kind": None, "model": "Google Cast Group"},
    ],
)
def test_get_device_rejects_cast_group_catalog_fallback(tmp_path, monkeypatch, overrides):
    path = _write_catalog(tmp_path, [_mb_record(**overrides)])
    manager = CastManager(catalog_path=path)
    monkeypatch.setattr(pychromecast, "get_chromecasts", lambda **kwargs: ([], MagicMock()))
    dial_mock = MagicMock()
    connect_mock = MagicMock()
    monkeypatch.setattr(pychromecast.dial, "get_device_info", dial_mock)
    monkeypatch.setattr(pychromecast, "get_chromecast_from_host", connect_mock)

    with pytest.raises(ValueError, match="Cast Groups"):
        manager.get_device("plex-jukebox:Master Bedroom")
    dial_mock.assert_not_called()
    connect_mock.assert_not_called()


@pytest.mark.parametrize(
    ("cast_type", "model_name"),
    [("group", "Google Nest Hub"), ("cast", "Google Cast Group")],
)
def test_get_device_rejects_dial_reported_cast_group(tmp_path, monkeypatch, cast_type, model_name):
    path = _write_catalog(tmp_path, [_mb_record()])
    manager = CastManager(catalog_path=path)
    monkeypatch.setattr(pychromecast, "get_chromecasts", lambda **kwargs: ([], MagicMock()))
    monkeypatch.setattr(
        pychromecast.dial,
        "get_device_info",
        lambda host, timeout=30: _dial_status(UUID(MB_UUID), cast_type=cast_type, model_name=model_name),
    )
    connect_mock = MagicMock()
    monkeypatch.setattr(pychromecast, "get_chromecast_from_host", connect_mock)

    with pytest.raises(ValueError, match="Cast Group"):
        manager.get_device("plex-jukebox:Master Bedroom")
    connect_mock.assert_not_called()


def test_get_device_cleans_up_on_connect_failure_after_dial_verification(tmp_path, monkeypatch):
    path = _write_catalog(tmp_path, [_mb_record()])
    manager = CastManager(catalog_path=path)
    monkeypatch.setattr(pychromecast, "get_chromecasts", lambda **kwargs: ([], MagicMock()))
    monkeypatch.setattr(
        pychromecast.dial,
        "get_device_info",
        lambda host, timeout=30: _dial_status(UUID(MB_UUID)),
    )
    failing_cast = _make_live_cast()
    failing_cast.wait.side_effect = TimeoutError("cast port 8009 unreachable")
    monkeypatch.setattr(pychromecast, "get_chromecast_from_host", lambda _host: failing_cast)

    with pytest.raises(TimeoutError):
        manager.get_device("plex-jukebox:Master Bedroom")
    failing_cast.disconnect.assert_called_once()
    assert manager.speakers == {}


def test_get_device_reuses_live_cached_connection_and_replaces_dead_one(tmp_path, monkeypatch):
    path = _write_catalog(tmp_path, [_mb_record()])
    manager = CastManager(catalog_path=path)

    mdns_calls = []
    dial_calls = []
    monkeypatch.setattr(
        pychromecast,
        "get_chromecasts",
        lambda **kwargs: mdns_calls.append(kwargs) or ([], MagicMock()),
    )
    monkeypatch.setattr(
        pychromecast.dial,
        "get_device_info",
        lambda host, timeout=30: dial_calls.append(host) or _dial_status(UUID(MB_UUID)),
    )
    cast1 = _make_live_cast()
    cast2 = _make_live_cast()
    created = [cast1, cast2]
    connect_calls = []
    monkeypatch.setattr(
        pychromecast,
        "get_chromecast_from_host",
        lambda host_tuple: connect_calls.append(host_tuple) or created.pop(0),
    )

    first = manager.get_device("plex-jukebox:Master Bedroom")
    second = manager.get_device("plex-jukebox:Master Bedroom")
    assert second is first
    assert len(mdns_calls) == 1
    assert len(dial_calls) == 1
    assert len(connect_calls) == 1

    cast1.socket_client.is_connected = False
    third = manager.get_device("plex-jukebox:Master Bedroom")
    try:
        assert third is not first
        assert third.cast is cast2
        cast1.disconnect.assert_called_once()
        assert len(mdns_calls) == 2
        assert len(dial_calls) == 2
        assert len(connect_calls) == 2
    finally:
        third.close()


def test_playback_and_transport_resolve_virtual_device_via_verified_dial_fallback(tmp_path, monkeypatch):
    path = _write_catalog(tmp_path, [_mb_record()])
    manager = CastManager(catalog_path=path)
    monkeypatch.setattr(pychromecast, "get_chromecasts", lambda **kwargs: ([], MagicMock()))

    dial_uuid = {"current": UUID(MB_UUID)}
    monkeypatch.setattr(
        pychromecast.dial,
        "get_device_info",
        lambda host, timeout=30: _dial_status(dial_uuid["current"]),
    )
    live_cast = _make_live_cast()
    monkeypatch.setattr(pychromecast, "get_chromecast_from_host", lambda _host: live_cast)

    track = SimpleNamespace(
        title="Midnight in a Perfect World",
        grandparentTitle="DJ Shadow",
        parentTitle="Endtroducing.....",
        thumbUrl=None,
        media=[SimpleNamespace(parts=[SimpleNamespace(key="/library/parts/1/file.flac")], container="flac")],
        _server=SimpleNamespace(url=lambda key, includeToken: "http://plex" + key),
    )
    dj = SimpleNamespace(
        parse=lambda q, c: SimpleNamespace(
            mode="artist",
            target_speaker=None,
            model_dump=lambda: {"mode": "artist", "target_artist": "DJ Shadow"},
        )
    )
    plex = SimpleNamespace(catalog={"artists": ["DJ Shadow"]}, resolve=lambda intent: [track])
    settings = SimpleNamespace(default_speaker_name="wrong-default")

    app = FastAPI()
    app.include_router(router)
    app.state.cast = manager
    app.state.dj = dj
    app.state.plex = plex
    app.state.settings = settings
    client = TestClient(app)

    try:
        cmd_res = client.post(
            "/api/command",
            json={"query": "Play DJ Shadow", "device_id": "plex-jukebox:Master Bedroom"},
        )
        assert cmd_res.status_code == 200
        live_cast.media_controller.play_media.assert_called_once()

        for payload in (
            {"action": "pause", "device_id": "plex-jukebox:Master Bedroom"},
            {"action": "resume", "device_id": "plex-jukebox:Master Bedroom"},
            {"action": "volume", "volume": 0.18, "device_id": "plex-jukebox:Master Bedroom"},
            {"action": "stop", "device_id": "plex-jukebox:Master Bedroom"},
        ):
            tr_res = client.post("/api/transport", json=payload)
            assert tr_res.status_code == 200

        live_cast.media_controller.pause.assert_called_once()
        live_cast.media_controller.play.assert_called_once()
        live_cast.set_volume.assert_any_call(0.18)

        # Confirm playback_service helpers fail closed when DIAL UUID mismatches and no live cache exists.
        manager.speakers.clear()
        dial_uuid["current"] = UUID(KITCHEN_UUID)
        mismatch_cmd = client.post(
            "/api/command",
            json={"query": "Play DJ Shadow", "device_id": "plex-jukebox:Master Bedroom"},
        )
        mismatch_tr = client.post(
            "/api/transport",
            json={"action": "pause", "device_id": "plex-jukebox:Master Bedroom"},
        )
        assert mismatch_cmd.status_code == 400
        assert mismatch_tr.status_code == 400

        # Direct service invocation also uses verified resolution.
        dial_uuid["current"] = UUID(MB_UUID)
        play_result = asyncio.run(
            play(
                dj=dj,
                plex=plex,
                cast=manager,
                settings=settings,
                command=Command(query="Play DJ Shadow", device_id="plex-jukebox:Master Bedroom"),
            )
        )
        assert play_result["playback"]["speaker"] == "Master Bedroom display"
        tr_result = asyncio.run(
            transport(
                cast=manager,
                settings=settings,
                command=Transport(action="pause", device_id="plex-jukebox:Master Bedroom"),
            )
        )
        assert tr_result["speaker"] == "Master Bedroom display"
    finally:
        manager.close()


def test_get_device_allows_individual_speaker_sharing_ip_with_catalog_group(tmp_path, monkeypatch):
    group_uuid = "33333333-3333-4333-8333-333333333333"
    path = _write_catalog(
        tmp_path,
        [
            _mb_record(host="192.168.88.126"),
            {
                "id": "plex-jukebox:Home",
                "google_home_device_id": "plex-jukebox:Home",
                "speaker_name": "Home",
                "cast_uuid": group_uuid,
                "host": "192.168.88.126",
                "port": 42000,
                "kind": "group",
                "model": "Google Cast Group",
                "enabled": True,
                "google_home_visible": True,
            },
        ],
    )
    manager = CastManager(catalog_path=path)
    monkeypatch.setattr(pychromecast, "get_chromecasts", lambda **kwargs: ([], MagicMock()))
    monkeypatch.setattr(
        pychromecast.dial,
        "get_device_info",
        lambda host, timeout=30: _dial_status(UUID(MB_UUID)),
    )
    live_cast = _make_live_cast()
    monkeypatch.setattr(pychromecast, "get_chromecast_from_host", lambda _host: live_cast)

    queue = manager.get_device("plex-jukebox:Master Bedroom")
    try:
        assert queue.cast is live_cast
    finally:
        queue.close()


def test_get_device_rejects_cached_speaker_after_catalog_disable(tmp_path, monkeypatch):
    path = _write_catalog(tmp_path, [_mb_record()])
    manager = CastManager(catalog_path=path)
    monkeypatch.setattr(pychromecast, "get_chromecasts", lambda **kwargs: ([], MagicMock()))
    monkeypatch.setattr(
        pychromecast.dial,
        "get_device_info",
        lambda host, timeout=30: _dial_status(UUID(MB_UUID)),
    )
    live_cast = _make_live_cast()
    monkeypatch.setattr(pychromecast, "get_chromecast_from_host", lambda _host: live_cast)

    queue = manager.get_device("plex-jukebox:Master Bedroom")
    try:
        path.write_text(json.dumps({"speakers": [_mb_record(enabled=False)]}), encoding="utf-8")
        with pytest.raises(ValueError, match="disabled or hidden"):
            manager.get_device("plex-jukebox:Master Bedroom")
    finally:
        queue.close()


def test_connect_from_host_disconnects_both_casts_when_8009_retry_fails(monkeypatch):
    manager = CastManager()
    first = _make_live_cast()
    first.wait.side_effect = TimeoutError("initial port failed")
    second = _make_live_cast()
    second.wait.side_effect = TimeoutError("8009 retry failed")
    casts = [first, second]
    monkeypatch.setattr(pychromecast, "get_chromecast_from_host", lambda _host: casts.pop(0))

    with pytest.raises(TimeoutError):
        manager._connect_from_host("uuid:" + MB_UUID, ("192.168.88.126", 42000, UUID(MB_UUID), "Nest", "Bedroom"))
    first.disconnect.assert_called_once()
    second.disconnect.assert_called_once()
    assert manager.speakers == {}


@pytest.mark.parametrize("device_id_first", [False, True])
def test_name_and_google_home_lookups_share_queue_for_same_cast_uuid(
    tmp_path, monkeypatch, device_id_first
):
    path = _write_catalog(tmp_path, [_mb_record()])
    manager = CastManager(catalog_path=path)
    cast = _make_live_cast()
    cast.uuid = UUID(MB_UUID)
    monkeypatch.setattr(pychromecast, "get_chromecast_from_host", lambda _host: cast)

    discovered_info = SimpleNamespace(
        host="192.168.88.126",
        port=8009,
        uuid=UUID(MB_UUID),
        model_name="Google Nest Hub",
        friendly_name="Master Bedroom display",
    )
    discovery_cast = SimpleNamespace(
        uuid=UUID(MB_UUID), cast_info=discovered_info, disconnect=MagicMock()
    )
    browser = MagicMock()
    monkeypatch.setattr(
        pychromecast,
        "get_chromecasts",
        lambda **_kwargs: ([discovery_cast], browser),
    )

    if device_id_first:
        by_device_id = manager.get_device("plex-jukebox:Master Bedroom")
        by_name = manager.get("Master Bedroom display")
    else:
        by_name = manager.get("Master Bedroom display")
        by_device_id = manager.get_device("plex-jukebox:Master Bedroom")

    try:
        assert by_name is by_device_id
        assert by_name.cast is by_device_id.cast is cast
        assert by_name.lock is by_device_id.lock
        assert by_name.queue is by_device_id.queue
        assert by_name.generation == by_device_id.generation
        by_device_id.expected_content_id = "plex-content-id"
        by_device_id.active_media_session_id = 9
        assert by_name.expected_content_id == "plex-content-id"
        assert by_name.active_media_session_id == 9
    finally:
        manager.close()


def test_dashboard_stop_during_voice_selection_wins_shared_queue_generation(
    tmp_path, monkeypatch
):
    path = _write_catalog(tmp_path, [_mb_record()])
    manager = CastManager(catalog_path=path)
    cast = _make_live_cast()
    cast.uuid = UUID(MB_UUID)
    monkeypatch.setattr(pychromecast, "get_chromecast_from_host", lambda _host: cast)
    queue = manager.get("Master Bedroom display")

    catalog_rows = [{
        "google_home_device_id": "plex-jukebox:Master Bedroom",
        "speaker_name": "Master Bedroom display",
        "voice_cold_start_enabled": True,
    }]
    monkeypatch.setattr(
        "plex_cast_bridge.voice_cold_start.catalog_store.list_speaker_records",
        lambda: catalog_rows,
    )
    monkeypatch.setattr(
        "plex_cast_bridge.voice_cold_start.find_by_google_home_id",
        lambda _rows, device_id: catalog_rows[0]
        if device_id == "plex-jukebox:Master Bedroom"
        else None,
    )

    selecting = threading.Event()
    release_selection = threading.Event()

    class BlockingLibrary:
        def search(self, **_kwargs):
            selecting.set()
            assert release_selection.wait(timeout=3)
            return [object()]

    plex = SimpleNamespace(library=BlockingLibrary(), artists=[])
    service = VoiceColdStart(
        cast=manager,
        plex_provider=lambda: plex,
        inspector=lambda _speaker: CastSession(
            "idle",
            True,
            receiver_app_id="CC1AD845",
            receiver_is_standby=True,
            queue_generation=queue.generation,
        ),
        message_deduper=PubSubMessageDeduper(tmp_path / "dedupe.json"),
        duplicate_window=0,
    )
    results = []
    worker = threading.Thread(
        target=lambda: results.append(service.resume("plex-jukebox:Master Bedroom"))
    )
    worker.start()
    try:
        assert selecting.wait(timeout=3)
        initial_generation = queue.generation
        # This is the dashboard's normal stop operation on the name lookup.
        queue.stop()
        assert queue.generation == initial_generation + 1
    finally:
        release_selection.set()
        worker.join(timeout=3)
        manager.close()

    assert not worker.is_alive()
    assert results[0]["voice_cold_start"] == "queue_changed"
    assert queue.current is None
    assert not queue.queue
