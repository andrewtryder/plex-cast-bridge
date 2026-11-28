"""Offline safety checks for the native Plex receiver hardware probe."""

from types import SimpleNamespace
import threading
from unittest.mock import MagicMock

from scripts.experiments.native_plex_receiver_probe import (
    DEFAULT_MEDIA_RECEIVER_APP_ID,
    IDLE_APP_ID,
    MEDIA_NAMESPACE,
    _StatusCallback,
    _classify,
    _fresh_media_response,
    _fresh_receiver_response,
    _fresh_receiver_volume,
    _physical_idle_confirmation_can_override,
    _position_advance,
    _refresh_media,
    _restore_original_volume,
    _safe_volume,
)


def _idle_snapshot(**overrides):
    snapshot = {
        "connected": True,
        "receiver_status_received": True,
        "media_status_received": True,
        "receiver_app_id": DEFAULT_MEDIA_RECEIVER_APP_ID,
        "receiver_is_standby": True,
        "media_session_id_present": False,
        "player_state": "IDLE",
    }
    snapshot.update(overrides)
    return snapshot


def _no_groups():
    return {
        "discovery": "complete",
        "groups_seen": 0,
        "group_connection_failures": 0,
        "groups_without_status": [],
        "memberships": [],
    }


def test_confirmed_default_receiver_idle_requires_fresh_cast_and_group_status():
    assert _classify(_idle_snapshot(), _no_groups()) == "idle"


def test_standby_idle_app_is_not_mistaken_for_confirmed_idle():
    snapshot = _idle_snapshot(receiver_app_id=IDLE_APP_ID)
    assert _classify(snapshot, _no_groups()) == "indeterminate"


def test_incomplete_group_scan_cannot_authorize_idle():
    groups = {**_no_groups(), "discovery": "incomplete", "groups_without_status": ["group-a"]}
    assert _classify(_idle_snapshot(), groups) == "indeterminate_group_state"


def test_physical_idle_confirmation_can_resolve_missing_status_only():
    snapshot = _idle_snapshot(
        receiver_status_received=False,
        media_status_received=False,
        receiver_app_id=None,
        receiver_is_standby=None,
        player_state="UNKNOWN",
    )
    assert _physical_idle_confirmation_can_override(snapshot, _no_groups(), is_group=False)


def test_physical_idle_confirmation_cannot_override_media_session_or_other_app():
    assert not _physical_idle_confirmation_can_override(
        _idle_snapshot(media_session_id_present=True, player_state="UNKNOWN"),
        _no_groups(),
        is_group=False,
    )
    assert not _physical_idle_confirmation_can_override(
        _idle_snapshot(receiver_app_id="Netflix", receiver_is_standby=None),
        _no_groups(),
        is_group=False,
    )


def test_physical_idle_confirmation_cannot_override_group_membership_for_member():
    groups = {**_no_groups(), "memberships": [{"uuid": "group-a"}]}
    assert not _physical_idle_confirmation_can_override(
        _idle_snapshot(player_state="UNKNOWN"), groups, is_group=False
    )


def test_active_group_membership_blocks_idle_receiver_classification():
    groups = {
        **_no_groups(),
        "groups_seen": 1,
        "memberships": [{
            "uuid": "group-a",
            "player_state": "PLAYING",
            "receiver_status_received": True,
            "media_status_received": True,
            "receiver_is_standby": False,
            "media_session_id_present": True,
        }],
    }
    assert _classify(_idle_snapshot(), groups) == "group_active_or_indeterminate"


def test_receiver_callback_requires_successful_status_payload():
    result = _StatusCallback(threading.Event(), False, {
        "type": "RECEIVER_STATUS",
        "status": {"isStandBy": True, "applications": []},
    })
    assert _fresh_receiver_response(result) is None


def test_media_callback_requires_success_and_valid_media_status():
    result = _StatusCallback(threading.Event(), False, {
        "type": "MEDIA_STATUS",
        "status": [{"mediaSessionId": 3, "playerState": "PLAYING"}],
    })
    assert _fresh_media_response(result) is None


def test_volume_evidence_requires_successful_receiver_response():
    failed = _StatusCallback(threading.Event(), False, {
        "type": "RECEIVER_STATUS",
        "status": {"volume": {"level": 0.1}},
    })
    fresh = _StatusCallback(threading.Event(), True, {
        "type": "RECEIVER_STATUS",
        "status": {"volume": {"level": 0.1}},
    })
    assert _fresh_receiver_volume(failed) is None
    assert _fresh_receiver_volume(fresh) == 0.1


def test_volume_is_capped_then_restored_only_with_fresh_readback():
    state = {"level": 0.67}

    def receiver_status(callback_function):
        callback_function(True, {
            "type": "RECEIVER_STATUS",
            "status": {"volume": {"level": state["level"]}},
        })

    cast = SimpleNamespace(
        set_volume=MagicMock(side_effect=lambda value: state.update(level=value)),
        socket_client=SimpleNamespace(
            receiver_controller=SimpleNamespace(update_status=receiver_status)
        ),
    )
    assert _safe_volume(cast, configured_max=0.8, original=0.67) == (0.67, 0.12)
    cast.set_volume.assert_called_once_with(0.12)
    assert _restore_original_volume(cast, 0.67)
    cast.set_volume.assert_called_with(0.67)


def test_failed_media_refresh_does_not_trust_stale_playing_status():
    status = SimpleNamespace(
        player_state="PLAYING",
        media_session_id=5,
        content_id="old-content",
        title="Old title",
        artist="Old artist",
        album_name="Old album",
        current_time=23.0,
        duration=120.0,
        images=[],
    )
    media = SimpleNamespace(
        namespace=MEDIA_NAMESPACE,
        status=status,
        update_status=lambda callback_function: callback_function(False, None),
    )
    cast = SimpleNamespace(
        media_controller=media,
        socket_client=SimpleNamespace(app_namespaces={MEDIA_NAMESPACE}),
    )

    snapshot = _refresh_media(cast, timeout=0.01)
    assert snapshot["player_state"] == "PLAYING"
    assert snapshot["fresh_status"] is False


def test_position_advance_requires_fresh_playing_samples():
    playing = {"fresh_status": True, "player_state": "PLAYING", "position": 4.0}
    assert _position_advance(playing, {
        "fresh_status": False, "player_state": "PLAYING", "position": 12.0,
    }) is None
    assert _position_advance(playing, {
        "fresh_status": True, "player_state": "PAUSED", "position": 12.0,
    }) is None
    assert _position_advance(playing, {
        "fresh_status": True, "player_state": "PLAYING", "position": 8.25,
    }) == 4.25
