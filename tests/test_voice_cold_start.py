from __future__ import annotations

import threading
from collections import deque
from types import SimpleNamespace

import pytest

from plex_cast_bridge import catalog_store
from plex_cast_bridge.voice_cold_start import (
    CastSession,
    MEDIA_NAMESPACE,
    PubSubMessageDeduper,
    VoiceColdStart,
    inspect_cast_session,
)


def receiver_response(*, app_id="CC1AD845", standby=True):
    applications = [] if app_id is None else [{"appId": app_id, "sessionId": 1}]
    return {"type": "RECEIVER_STATUS", "status": {"isStandBy": standby, "applications": applications}}


def media_response(*, session_id=None, player_state="IDLE", content_id=None):
    if session_id is None:
        return {"type": "MEDIA_STATUS", "status": []}
    return {
        "type": "MEDIA_STATUS",
        "status": [{
            "mediaSessionId": session_id,
            "playerState": player_state,
            "media": {"contentId": content_id},
        }],
    }


class FakeSpeaker:
    def __init__(self):
        self.lock = threading.RLock()
        self.generation = 0
        self.current = None
        self.queue = deque()
        self.pending_load_generation = None
        self.expected_content_id = None
        self.active_media_session_id = None
        self.armed = False
        self.replacements = []
        self.resumes = 0
        self.receiver_payload = receiver_response()
        self.media_payload = media_response()
        self.receiver_success = True
        self.media_success = True
        self.receiver_callback_response = True
        self.media_callback_response = True
        self.media_update_calls = 0
        self.cast = SimpleNamespace(
            socket_client=SimpleNamespace(
                is_connected=True,
                app_namespaces=[MEDIA_NAMESPACE],
                receiver_controller=None,
            ),
            status=SimpleNamespace(app_id="CC1AD845", is_stand_by=True),
            media_controller=None,
        )

        def receiver_update(callback_function):
            callback_function(
                self.receiver_success,
                self.receiver_payload if self.receiver_callback_response else None,
            )

        self.cast.socket_client.receiver_controller = SimpleNamespace(update_status=receiver_update)

        def media_update(callback_function):
            self.media_update_calls += 1
            callback_function(
                self.media_success,
                self.media_payload if self.media_callback_response else None,
            )

        self.cast.media_controller = SimpleNamespace(
            namespace=MEDIA_NAMESPACE,
            status=SimpleNamespace(media_session_id=None, player_state="UNKNOWN"),
            update_status=media_update,
        )

    def replace(self, tracks, repeat=False):
        self.replacements.append((tracks, repeat))
        self.generation += 1

    def resume(self):
        self.resumes += 1


class FakeLibrary:
    def __init__(self, tracks=None):
        self.tracks = tracks if tracks is not None else ["track"]
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return self.tracks


class FakePlex:
    def __init__(self, tracks=None):
        self.library = FakeLibrary(tracks)
        self.artists = []


def coordinator(monkeypatch, state="idle", selection="shuffle", enabled=True, tracks=None,
                get_device_error=None, duplicate_window=20.0, deduper=None):
    speaker = FakeSpeaker()
    plex = FakePlex(tracks)
    selected = {"plex": plex}
    rows = [{
        "google_home_device_id": "plex-jukebox:room",
        "speaker_name": "Room",
        "enabled": True,
        "google_home_visible": True,
        "voice_cold_start_enabled": enabled,
        "voice_cold_start_selection": selection,
        "voice_cold_start_artist": "DJ Shadow",
    }]
    monkeypatch.setattr(catalog_store, "list_speaker_records", lambda: rows)
    monkeypatch.setattr(
        "plex_cast_bridge.voice_cold_start.find_by_google_home_id",
        lambda _rows, device_id: next((r for r in rows if r["google_home_device_id"] == device_id), None),
    )

    def get_device(_device_id):
        if get_device_error:
            raise get_device_error
        return speaker

    cast = SimpleNamespace(get_device=get_device)
    service = VoiceColdStart(
        cast=cast,
        plex_provider=lambda: selected["plex"],
        inspector=lambda _speaker: CastSession(
            state,
            True,
            receiver_app_id="CC1AD845",
            receiver_is_standby=True,
            queue_generation=speaker.generation,
        ),
        message_deduper=deduper,
        duplicate_window=duplicate_window,
    )
    return service, speaker, plex, selected


def test_idle_shuffle_uses_bounded_server_side_random_query(monkeypatch):
    service, speaker, plex, _ = coordinator(monkeypatch)
    assert service.resume("plex-jukebox:room")["voice_cold_start"] == "initiated"
    assert plex.library.calls == [{"libtype": "track", "sort": "random", "maxresults": 25}]
    assert speaker.replacements[0][1] is False


def test_disabled_policy_preserves_existing_transport_path(monkeypatch):
    service, speaker, _plex, _ = coordinator(monkeypatch, enabled=False)
    assert service.resume("plex-jukebox:room") is None
    assert not speaker.replacements


def test_group_target_is_left_to_existing_transport_path(monkeypatch):
    service, speaker, _plex, _ = coordinator(monkeypatch)
    row = catalog_store.list_speaker_records()[0]
    row["is_group"] = True
    assert service.resume("plex-jukebox:room") is None
    assert not speaker.replacements


@pytest.mark.parametrize("state", ["other_media_active", "other_media_paused", "other_app_active", "indeterminate", "disconnected"])
def test_non_plex_states_are_never_replaced(monkeypatch, state):
    service, speaker, _plex, _ = coordinator(monkeypatch, state=state)
    result = service.resume("plex-jukebox:room")
    assert result["voice_cold_start"].startswith("skipped_")
    assert not speaker.replacements


@pytest.mark.parametrize("state", ["plex_paused", "plex_active"])
def test_owned_plex_sessions_are_preserved(monkeypatch, state):
    service, speaker, _plex, _ = coordinator(monkeypatch, state=state)
    result = service.resume("plex-jukebox:room")
    assert result["voice_cold_start"] in {"resumed_existing", "already_active"}
    assert speaker.resumes == (1 if state == "plex_paused" else 0)
    assert not speaker.replacements


def test_empty_library_skips_without_substitution(monkeypatch):
    service, speaker, _plex, _ = coordinator(monkeypatch, tracks=[])
    assert service.resume("plex-jukebox:room")["voice_cold_start"] == "selection_empty"
    assert not speaker.replacements


def test_unknown_or_failed_callback_cannot_authorize_idle():
    speaker = FakeSpeaker()
    speaker.receiver_success = False
    state = inspect_cast_session(speaker, timeout=0.01)
    assert state.classification == "indeterminate"
    assert state.receiver_status_received is False


def test_media_callback_wrong_response_type_cannot_authorize_idle():
    speaker = FakeSpeaker()
    speaker.media_payload = {"type": "LOAD_FAILED", "status": []}
    state = inspect_cast_session(speaker, timeout=0.01)
    assert state.classification == "indeterminate"
    assert state.media_status_received is False


def test_callback_timeout_cannot_use_stale_cached_idle_status():
    speaker = FakeSpeaker()
    speaker.receiver_callback_response = False
    speaker.media_callback_response = False
    # Cached status looks idle, but neither fresh callback succeeded.
    state = inspect_cast_session(speaker, timeout=0.001)
    assert state.classification == "indeterminate"


def test_disconnection_during_status_query_fails_closed():
    speaker = FakeSpeaker()

    def disconnect(callback_function):
        speaker.cast.socket_client.is_connected = False
        callback_function(False, None)

    speaker.cast.media_controller.update_status = disconnect
    state = inspect_cast_session(speaker, timeout=0.01)
    assert state.classification == "disconnected"


def test_status_inspection_does_not_launch_if_media_namespace_is_missing():
    speaker = FakeSpeaker()
    speaker.cast.socket_client.app_namespaces = []
    launches = []
    speaker.cast.socket_client.receiver_controller.launch_app = lambda *args, **kwargs: launches.append(args)
    state = inspect_cast_session(speaker, timeout=0.01)
    assert state.classification == "indeterminate"
    assert speaker.media_update_calls == 0
    assert launches == []


def test_confirmed_default_receiver_standby_and_empty_media_is_idle():
    speaker = FakeSpeaker()
    state = inspect_cast_session(speaker, timeout=0.01)
    assert state.classification == "idle"
    assert state.receiver_status_received and state.media_status_received


@pytest.mark.parametrize("group_state", ["active"])
@pytest.mark.parametrize("individual_state", ["IDLE", "UNKNOWN"])
def test_group_activity_blocks_idle_and_unknown_member(group_state, individual_state):
    speaker = FakeSpeaker()
    classified = []
    speaker.cast_group_state = SimpleNamespace(classify=lambda _uuid: classified.append(_uuid) or group_state)
    speaker.cast.uuid = "10000000-0000-0000-0000-000000000001"
    if individual_state == "UNKNOWN":
        speaker.media_payload = {
            "type": "MEDIA_STATUS",
            "status": [{"mediaSessionId": 9, "playerState": "UNKNOWN"}],
        }
    state = inspect_cast_session(speaker, timeout=0.01)
    assert state.classification == "other_media_active"
    assert classified == [speaker.cast.uuid]


def test_stale_or_unavailable_group_state_is_indeterminate():
    speaker = FakeSpeaker()
    speaker.cast_group_state = SimpleNamespace(classify=lambda _uuid: "unknown")
    speaker.cast.uuid = "10000000-0000-0000-0000-000000000001"
    assert inspect_cast_session(speaker, timeout=0.01).classification == "indeterminate"


def test_confirmed_idle_without_group_conflict_remains_idle():
    speaker = FakeSpeaker()
    speaker.cast_group_state = SimpleNamespace(classify=lambda _uuid: "clear")
    speaker.cast.uuid = "10000000-0000-0000-0000-000000000001"
    assert inspect_cast_session(speaker, timeout=0.01).classification == "idle"


def test_group_starts_during_selection_blocks_load(monkeypatch):
    service, speaker, _plex, _ = coordinator(monkeypatch)
    current = {"group": "clear"}
    speaker.cast.uuid = "10000000-0000-0000-0000-000000000001"
    speaker.cast_group_state = SimpleNamespace(classify=lambda _uuid: current["group"])
    service.inspector = inspect_cast_session
    select = service._selection

    def select_with_group_start(plex, record):
        tracks = select(plex, record)
        current["group"] = "active"
        return tracks

    service._selection = select_with_group_start
    result = service.resume("plex-jukebox:room")
    assert result["voice_cold_start"] == "revalidation_other_media_active"
    assert not speaker.replacements


def test_final_group_check_blocks_race_after_status_revalidation(monkeypatch):
    service, speaker, _plex, _ = coordinator(monkeypatch)
    checks = {"count": 0}
    speaker.cast.uuid = "10000000-0000-0000-0000-000000000001"

    def classify(_uuid):
        checks["count"] += 1
        return "active" if checks["count"] >= 3 else "clear"

    speaker.cast_group_state = SimpleNamespace(classify=classify)
    service.inspector = inspect_cast_session
    result = service.resume("plex-jukebox:room")
    assert result["voice_cold_start"] == "group_active"
    assert not speaker.replacements


@pytest.mark.parametrize("player_state", ["PLAYING", "PAUSED"])
def test_external_default_receiver_media_not_owned_by_stale_plex_queue(player_state):
    speaker = FakeSpeaker()
    speaker.current = object()
    speaker.queue.append(object())
    speaker.expected_content_id = "http://plex.invalid/old-track?token=secret"
    speaker.active_media_session_id = 1
    speaker.cast.status.is_stand_by = False
    speaker.receiver_payload = receiver_response(standby=False)
    speaker.media_payload = media_response(
        session_id=91,
        player_state=player_state,
        content_id="https://other-app.invalid/media",
    )
    state = inspect_cast_session(speaker, timeout=0.01)
    assert state.classification == ("other_media_paused" if player_state == "PAUSED" else "other_media_active")


def test_matching_expected_content_and_active_session_proves_plex_ownership():
    speaker = FakeSpeaker()
    speaker.current = object()
    speaker.expected_content_id = "http://plex.invalid/current?token=secret"
    speaker.active_media_session_id = 12
    speaker.media_payload = media_response(
        session_id=12,
        player_state="PAUSED",
        content_id=speaker.expected_content_id,
    )
    state = inspect_cast_session(speaker, timeout=0.01)
    assert state.classification == "plex_paused"


def test_no_session_with_stale_bridge_queue_is_indeterminate():
    speaker = FakeSpeaker()
    speaker.current = object()
    state = inspect_cast_session(speaker, timeout=0.01)
    assert state.classification == "indeterminate"


def test_same_message_id_is_suppressed_across_coordinator_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(catalog_store, "DATA_DIR", tmp_path)
    deduper1 = PubSubMessageDeduper()
    service, speaker, _plex, _ = coordinator(
        monkeypatch, duplicate_window=0, deduper=deduper1
    )
    assert service.resume("plex-jukebox:room", "pubsub-message-1")["voice_cold_start"] == "initiated"
    deduper2 = PubSubMessageDeduper()
    service2, _speaker2, _plex2, _ = coordinator(
        monkeypatch, duplicate_window=0, deduper=deduper2
    )
    assert service2.resume("plex-jukebox:room", "pubsub-message-1")["voice_cold_start"] == "duplicate_suppressed"
    assert len(speaker.replacements) == 1


def test_distinct_concurrent_messages_start_at_most_once(monkeypatch, tmp_path):
    service, speaker, _plex, _ = coordinator(
        monkeypatch, deduper=PubSubMessageDeduper(tmp_path / "dedupe.json")
    )
    barrier = threading.Barrier(3)

    def run(message_id):
        barrier.wait()
        service.resume("plex-jukebox:room", message_id)

    threads = [threading.Thread(target=run, args=(f"msg-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert len(speaker.replacements) == 1


def test_queue_generation_change_during_selection_prevents_replace(monkeypatch):
    service, speaker, plex, _ = coordinator(monkeypatch)

    def start_dashboard_playback(**_kwargs):
        speaker.generation += 1
        return ["track"]

    plex.library.search = start_dashboard_playback
    assert service.resume("plex-jukebox:room")["voice_cold_start"] == "queue_changed"
    assert not speaker.replacements


def test_revalidation_prevents_external_sender_race(monkeypatch):
    service, speaker, _plex, _ = coordinator(monkeypatch)
    calls = iter([
        CastSession("idle", True, queue_generation=0),
        CastSession("other_media_active", True, queue_generation=0),
    ])
    service.inspector = lambda _speaker: next(calls)
    assert service.resume("plex-jukebox:room")["voice_cold_start"].startswith("revalidation_")
    assert not speaker.replacements


def test_preferred_artist_requires_unique_exact_match(monkeypatch):
    service, speaker, plex, _ = coordinator(monkeypatch, selection="artist")
    selected = SimpleNamespace(title="DJ Shadow", tracks=lambda **_kwargs: ["one", "two"])
    plex.artists = [selected]
    service._selection(plex, {"voice_cold_start_selection": "artist", "voice_cold_start_artist": "dj shadow"})
    plex.artists.append(SimpleNamespace(title="DJ Shadow", tracks=lambda **_kwargs: ["wrong"]))
    assert service.resume("plex-jukebox:room")["voice_cold_start"] == "selection_failed"
    assert not speaker.replacements


def test_unavailable_plex_connection_skips(monkeypatch):
    service, speaker, _plex, selected = coordinator(monkeypatch)
    selected["plex"] = None
    assert service.resume("plex-jukebox:room")["voice_cold_start"] == "plex_unavailable"
    assert not speaker.replacements


def test_coordinator_uses_plex_connection_connected_after_initialization(monkeypatch):
    service, speaker, plex, selected = coordinator(monkeypatch, duplicate_window=0)
    selected["plex"] = None
    assert service.resume("plex-jukebox:room")["voice_cold_start"] == "plex_unavailable"
    selected["plex"] = plex
    assert service.resume("plex-jukebox:room")["voice_cold_start"] == "initiated"
    assert len(plex.library.calls) == 1
    assert len(speaker.replacements) == 1


def test_coordinator_switches_servers_and_libraries_without_reconstruction(monkeypatch):
    service, speaker, first_plex, selected = coordinator(monkeypatch, duplicate_window=0)
    assert service.resume("plex-jukebox:room")["voice_cold_start"] == "initiated"
    second_plex = FakePlex(["from second server/library"])
    selected["plex"] = second_plex
    speaker.generation = 0
    assert service.resume("plex-jukebox:room")["voice_cold_start"] == "initiated"
    assert len(first_plex.library.calls) == 1
    assert len(second_plex.library.calls) == 1
    assert speaker.replacements[-1][0] == ["from second server/library"]


def test_disconnect_after_coordinator_initialization_skips(monkeypatch):
    service, speaker, plex, selected = coordinator(monkeypatch)
    selected["plex"] = None
    assert service.resume("plex-jukebox:room")["voice_cold_start"] == "plex_unavailable"
    assert not plex.library.calls
    assert not speaker.replacements


def test_plex_connection_change_during_selection_discards_old_results(monkeypatch):
    service, speaker, old_plex, selected = coordinator(monkeypatch)
    replacement_plex = FakePlex(["new library track"])

    def switch_connection(**_kwargs):
        selected["plex"] = replacement_plex
        return ["obsolete server track"]

    old_plex.library.search = switch_connection
    assert service.resume("plex-jukebox:room")["voice_cold_start"] == "plex_changed"
    assert not speaker.replacements
