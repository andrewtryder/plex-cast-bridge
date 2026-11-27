from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from plex_cast_bridge.voice_cold_start import CastSession, VoiceColdStart, inspect_cast_session


class FakeSpeaker:
    def __init__(self, classification="idle"):
        self.cast = SimpleNamespace(
            socket_client=SimpleNamespace(is_connected=True),
            status=SimpleNamespace(app_id="CC1AD845", is_stand_by=True),
            media_controller=SimpleNamespace(
                status=SimpleNamespace(media_session_id=None, player_state="UNKNOWN")
            ),
        )
        self.cast.socket_client.receiver_controller = SimpleNamespace(
            update_status=lambda callback_function: callback_function()
        )
        self.cast.media_controller.update_status = lambda callback_function: callback_function()
        self.state = classification
        self.current = None
        self.queue = []
        self.replacements = []
        self.resumes = 0

    def status(self):
        return {"state": "UNKNOWN", "current_track": self.current, "queue_length": len(self.queue)}

    def replace(self, tracks, repeat=False):
        self.replacements.append((tracks, repeat))

    def resume(self):
        self.resumes += 1


class FakePlex:
    def __init__(self, tracks=None):
        self.tracks = tracks if tracks is not None else ["track"]
        self.calls = []
        self.library = self

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return self.tracks


def coordinator(
    monkeypatch, state="idle", selection="shuffle", enabled=True, tracks=None, get_device_error=None
):
    speaker = FakeSpeaker(state)
    plex = FakePlex(tracks)
    monkeypatch.setattr(
        "plex_cast_bridge.voice_cold_start.list_speaker_records",
        lambda: [
            {
                "google_home_device_id": "plex-jukebox:room",
                "enabled": True,
                "google_home_visible": True,
                "voice_cold_start_enabled": enabled,
                "voice_cold_start_selection": selection,
                "voice_cold_start_artist": "DJ Shadow",
            }
        ],
    )

    def get_device(device_id):
        if get_device_error:
            raise get_device_error
        return speaker

    cast = SimpleNamespace(get_device=get_device)
    return VoiceColdStart(cast=cast, plex=plex), speaker, plex


def test_shuffle_uses_bounded_server_side_query_without_gemini(monkeypatch):
    service, speaker, plex = coordinator(monkeypatch)
    assert service.resume("plex-jukebox:room") == {
        "voice_cold_start": "initiated",
        "track_count": 1,
    }
    assert plex.calls == [{"libtype": "track", "sort": "random", "maxresults": 25}]
    assert speaker.replacements[0][1] is False


def test_preferred_artist_selection(monkeypatch):
    service, _speaker, plex = coordinator(monkeypatch, selection="artist")
    service.resume("plex-jukebox:room")
    assert plex.calls == [{"artist": "DJ Shadow", "libtype": "track", "maxresults": 25}]


def test_disabled_by_default_behavior_returns_to_existing_transport(monkeypatch):
    service, speaker, _plex = coordinator(monkeypatch, enabled=False)
    assert service.resume("plex-jukebox:room") is None
    assert speaker.replacements == []


@pytest.mark.parametrize(
    "state", ["other_media_active", "other_app_active", "indeterminate", "disconnected"]
)
def test_non_idle_sessions_are_not_replaced(monkeypatch, state):
    service, speaker, _plex = coordinator(monkeypatch)
    service.inspector = lambda _speaker: CastSession(state, state != "disconnected")
    assert service.resume("plex-jukebox:room")["voice_cold_start"].startswith("skipped_")
    assert not speaker.replacements


@pytest.mark.parametrize("state", ["plex_paused", "plex_active"])
def test_existing_plex_session_is_preserved(monkeypatch, state):
    service, speaker, _plex = coordinator(monkeypatch)
    service.inspector = lambda _speaker: CastSession(state, True)
    result = service.resume("plex-jukebox:room")
    assert result["voice_cold_start"] in {"resumed_existing", "already_active"}
    assert speaker.resumes == (1 if state == "plex_paused" else 0)
    assert speaker.replacements == []


def test_empty_library_is_not_started(monkeypatch):
    service, speaker, _plex = coordinator(monkeypatch, tracks=[])
    assert service.resume("plex-jukebox:room")["voice_cold_start"] == "selection_empty"
    assert not speaker.replacements


def test_revalidation_prevents_hijacking_new_session(monkeypatch):
    service, speaker, _plex = coordinator(monkeypatch)
    calls = iter([CastSession("idle", True), CastSession("other_media_active", True)])
    service.inspector = lambda _speaker: next(calls)
    assert service.resume("plex-jukebox:room")["voice_cold_start"].startswith("revalidation_")
    assert not speaker.replacements


def test_duplicate_delivery_is_suppressed(monkeypatch):
    service, speaker, _plex = coordinator(monkeypatch)
    service.resume("plex-jukebox:room")
    assert service.resume("plex-jukebox:room")["voice_cold_start"] == "duplicate_suppressed"
    assert len(speaker.replacements) == 1


def test_uncertain_cast_timeout_is_not_raised_or_replayed(monkeypatch):
    service, speaker, _plex = coordinator(monkeypatch)
    attempts = []

    def timeout_after_send(_tracks, repeat=False):
        attempts.append(("sent", repeat))
        raise TimeoutError("acknowledgement lost")

    speaker.replace = timeout_after_send
    assert service.resume("plex-jukebox:room")["voice_cold_start"] == "failed"
    assert service.resume("plex-jukebox:room")["voice_cold_start"] == "duplicate_suppressed"
    assert len(attempts) == 1


def test_concurrent_requests_replace_once(monkeypatch):
    service, speaker, _plex = coordinator(monkeypatch)
    barrier = threading.Barrier(3)

    def call():
        barrier.wait()
        service.resume("plex-jukebox:room")

    threads = [threading.Thread(target=call) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert len(speaker.replacements) == 1


def test_inspection_requires_fresh_positive_status_for_idle():
    speaker = FakeSpeaker()
    state = inspect_cast_session(speaker, timeout=0.01)
    assert state.classification == "idle"


def test_inspection_does_not_treat_disconnected_unknown_as_idle():
    speaker = FakeSpeaker()
    speaker.cast.socket_client.is_connected = False
    assert inspect_cast_session(speaker).classification == "disconnected"


def test_status_timeout_is_indeterminate():
    speaker = FakeSpeaker()
    speaker.cast.socket_client.receiver_controller.update_status = lambda callback_function: None
    speaker.cast.media_controller.update_status = lambda callback_function: None
    state = inspect_cast_session(speaker, timeout=0.001)
    assert state.classification == "indeterminate"


def test_paused_plex_is_classified_from_session_and_bridge_queue():
    speaker = FakeSpeaker()
    speaker.current = {"rating_key": "3325", "title": "Stem"}
    speaker.queue = ["next track"]
    speaker.cast.media_controller.status.media_session_id = 1
    speaker.cast.media_controller.status.player_state = "PAUSED"
    state = inspect_cast_session(speaker, timeout=0.01)
    assert state.classification == "plex_paused"
    assert speaker.current["rating_key"] == "3325"
    assert speaker.queue == ["next track"]


def test_active_external_media_is_classified_as_non_plex():
    speaker = FakeSpeaker()
    speaker.cast.status.app_id = "OTHER_APP"
    speaker.cast.status.is_stand_by = False
    speaker.cast.media_controller.status.media_session_id = 7
    speaker.cast.media_controller.status.player_state = "PLAYING"
    assert inspect_cast_session(speaker, timeout=0.01).classification == "other_media_active"


@pytest.mark.parametrize("error", [ValueError("disabled"), ValueError("hidden")])
def test_cast_manager_authorization_errors_prevent_cold_start(monkeypatch, error):
    service, _speaker, _plex = coordinator(monkeypatch, get_device_error=error)
    with pytest.raises(ValueError):
        service.resume("plex-jukebox:room")


def test_deleted_artist_fails_without_unrelated_substitution(monkeypatch):
    service, speaker, plex = coordinator(monkeypatch, selection="artist", tracks=[])
    assert service.resume("plex-jukebox:room")["voice_cold_start"] == "selection_empty"
    assert plex.calls[0]["artist"] == "DJ Shadow"
    assert not speaker.replacements
