import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts/experiments/voice_cold_start_probe.py"
SPEC = importlib.util.spec_from_file_location("voice_cold_start_probe", MODULE_PATH)
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


def event(
    kind,
    action="resume",
    device_id="gh:1",
    speaker="Master Bedroom",
    timestamp="2026-11-27T20:01:00+00:00",
):
    return {
        "kind": kind,
        "action": action,
        "device_id": device_id,
        "speaker": speaker,
        "timestamp": timestamp,
    }


def idle_unknown():
    return {
        "connected": True,
        "receiver_app_id": "CC1AD845",
        "receiver_app_name": "Default Media Receiver",
        "receiver_is_standby": True,
        "receiver_status_received": True,
        "media_status_received": True,
        "media_session_id": None,
        "player_state": "UNKNOWN",
    }


class VoiceColdStartProbeTests(unittest.TestCase):
    def test_identity_matching_requires_unambiguous_exact_id(self):
        devices = probe.normalize_speakers(
            [
                {
                    "speaker_name": "Master Bedroom",
                    "google_home_name": "Master Bedroom Plex",
                    "google_home_device_id": "gh:1",
                    "uuid": "abc",
                    "host": "192.0.2.1",
                },
                {
                    "speaker_name": "Office",
                    "google_home_name": "Master Bedroom Plex",
                    "google_home_device_id": "gh:2",
                },
            ]
        )
        self.assertEqual(probe.select_device(devices, "gh:1").cast_uuid, "abc")
        with self.assertRaisesRegex(ValueError, "multiple"):
            probe.select_device(devices, "Master Bedroom Plex")
        with self.assertRaisesRegex(ValueError, "exact device ID"):
            probe.select_device(devices, "Bedroom")

    def test_log_parse_and_target_correlation(self):
        events = probe.parse_bridge_logs(
            [
                "2026-11-27 20:01:00,001 [INFO] Google Home command received action=resume speaker='Master Bedroom' device_id='gh:1'",
                "2026-11-27 20:01:00,021 [INFO] Google Home command completed action=resume speaker='Master Bedroom' device_id='gh:1'",
                "2026-11-27 20:01:00,030 [INFO] Google Home command received action=resume speaker='Office' device_id='gh:2'",
            ]
        )
        targeted = probe.correlate(events, "gh:1")
        self.assertEqual([item["kind"] for item in targeted], ["received", "completed"])
        self.assertEqual(targeted[0]["action"], "resume")
        self.assertIsNotNone(targeted[0]["timestamp"])

    def test_missing_bridge_status_is_not_idle(self):
        self.assertEqual(probe.playback_state({}, "Master Bedroom"), "unknown")
        self.assertEqual(
            probe.playback_state({"Other": {"state": "IDLE"}}, "Master Bedroom"), "unknown"
        )

    def test_unknown_bridge_state_with_responsive_standby_receiver_is_idle(self):
        row = {
            "speaker": "Master Bedroom",
            "state": "UNKNOWN",
            "current_track": None,
            "queue_length": 0,
        }
        self.assertEqual(probe.classify_cast_state(idle_unknown(), row), "idle_no_media_session")

    def test_unknown_from_disconnected_cast_is_not_idle(self):
        snapshot = {**idle_unknown(), "connected": False}
        row = {
            "speaker": "Master Bedroom",
            "state": "UNKNOWN",
            "current_track": None,
            "queue_length": 0,
        }
        self.assertEqual(probe.classify_cast_state(snapshot, row), "disconnected")

    def test_existing_paused_plex_session_is_classified_and_never_falls_back(self):
        snapshot = {
            **idle_unknown(),
            "receiver_is_standby": False,
            "media_session_id": 17,
            "player_state": "PAUSED",
        }
        row = {
            "speaker": "Master Bedroom",
            "state": "PAUSED",
            "current_track": {"artist": "DJ Shadow"},
            "queue_length": 3,
        }
        state = probe.classify_cast_state(snapshot, row)
        self.assertEqual(state, "plex_paused")
        gate = probe.FallbackGate("gh:1", datetime(2026, 11, 27, 20, 0, tzinfo=timezone.utc))
        self.assertFalse(gate.should_trigger([event("received"), event("completed")], state))

    def test_existing_active_plex_session_is_not_idle(self):
        snapshot = {
            **idle_unknown(),
            "receiver_is_standby": False,
            "media_session_id": 18,
            "player_state": "PLAYING",
        }
        row = {"current_track": {"artist": "DJ Shadow"}, "queue_length": 2}
        self.assertEqual(probe.classify_cast_state(snapshot, row), "plex_active")

    def test_active_other_receiver_application_is_not_idle(self):
        snapshot = {
            **idle_unknown(),
            "receiver_app_id": "CC1AD845X",
            "receiver_app_name": "Other Cast App",
            "receiver_is_standby": False,
            "media_session_id": 19,
            "player_state": "PLAYING",
        }
        row = {"current_track": None, "queue_length": 0}
        self.assertEqual(probe.classify_cast_state(snapshot, row), "other_media_active")

    def test_active_receiver_application_without_media_session_is_not_idle(self):
        snapshot = {
            **idle_unknown(),
            "receiver_app_id": "OTHERAPP",
            "receiver_app_name": "Other Cast App",
            "receiver_is_standby": False,
        }
        self.assertEqual(probe.classify_cast_state(snapshot, {}), "other_app_active")

    def test_default_receiver_with_unmanaged_media_session_is_not_idle(self):
        snapshot = {
            **idle_unknown(),
            "receiver_is_standby": False,
            "media_session_id": 20,
            "player_state": "PLAYING",
        }
        self.assertEqual(probe.classify_cast_state(snapshot, {}), "other_media_active")

    def test_unknown_without_standby_evidence_remains_indeterminate(self):
        snapshot = {**idle_unknown(), "receiver_is_standby": False}
        self.assertEqual(probe.classify_cast_state(snapshot, {}), "indeterminate")

    def test_fallback_requires_fresh_targeted_resume_pair_and_is_exactly_once(self):
        gate = probe.FallbackGate("gh:1", datetime(2026, 11, 27, 20, 0, tzinfo=timezone.utc))
        fresh = [event("received"), event("completed", timestamp="2026-11-27T20:01:01+00:00")]
        stale = [
            event("received", timestamp="2026-11-27T19:59:00+00:00"),
            event("completed", timestamp="2026-11-27T19:59:01+00:00"),
        ]
        self.assertFalse(gate.should_trigger(stale, "idle_no_media_session"))
        self.assertFalse(gate.should_trigger([event("completed")], "idle_no_media_session"))
        self.assertFalse(
            gate.should_trigger(
                [event("received", device_id="gh:2"), event("completed", device_id="gh:2")],
                "idle_no_media_session",
            )
        )
        self.assertFalse(
            gate.should_trigger(
                [event("received", action="mute"), event("completed", action="mute")],
                "idle_no_media_session",
            )
        )
        self.assertTrue(gate.should_trigger(fresh, "idle_no_media_session"))
        self.assertFalse(gate.should_trigger(fresh, "idle_no_media_session"))
        uncertain_gate = probe.FallbackGate(
            "gh:1", datetime(2026, 11, 27, 20, 0, tzinfo=timezone.utc)
        )
        self.assertFalse(uncertain_gate.should_trigger(fresh, "indeterminate"))
        self.assertFalse(uncertain_gate.should_trigger(fresh, "idle_no_media_session"))

    def test_timed_out_playback_is_inspected_without_retry(self):
        calls = []
        inspections = []

        def post():
            calls.append("POST")
            raise TimeoutError("simulated timeout")

        outcome = probe.request_playback_once(
            post,
            lambda: (
                inspections.append("GET status")
                or {
                    "bridge_state": "PLAYING",
                    "current_track_present": True,
                }
            ),
        )
        self.assertEqual(calls, ["POST"])
        self.assertEqual(inspections, ["GET status"])
        self.assertEqual(outcome["result"], "timed_out")
        self.assertTrue(outcome["inspection"]["current_track_present"])

    def test_cli_defaults_to_observation_only(self):
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--play-query", action="store_true")
        parser.add_argument("--experimental-fallback", action="store_true")
        args = parser.parse_args([])
        self.assertFalse(args.play_query)
        self.assertFalse(args.experimental_fallback)

    def test_observation_timeout_zero_does_not_play(self):
        original = probe._request
        probe._request = lambda *a, **k: [
            {"speaker_name": "Master Bedroom", "google_home_device_id": "gh:1"}
        ]
        try:
            self.assertEqual(probe.main(["--target-speaker", "gh:1", "--timeout", "0"]), 0)
        finally:
            probe._request = original


if __name__ == "__main__":
    unittest.main()
