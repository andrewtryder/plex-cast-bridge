from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import release_acceptance as acceptance


def _valid_report():
    api = {path: {"http_status": 200, "elapsed_ms": 1, "error": None} for path in acceptance.API_PATHS}
    api.update({
        "google_home": {"local_plex_linked": True, "cloud_linked": True, "virtual_devices": [
            {"google_home_device_id": f"virtual-{i}", "name": f"Speaker {i}", "enabled": True,
             "google_home_visible": True} for i in range(4)
        ], "catalog_sync": {"version": 1, "status": "succeeded"}},
        "speakers": [{"uuid": f"cast-{i}", "enabled": True, "google_home_visible": True,
                      "availability": "online"} for i in range(4)],
        "saved_mix_ids": [1], "plex": {"linked": True, "section": "Music"},
        "speaker_config": {"default_cast_volume": 0.4, "max_cast_volume": 0.85},
        "music_index": {"track_count": 10, "artist_count": 3, "album_count": 4},
        "mix:1": {"http_status": 200, "retrieved": True},
    })
    return {
        "captured_at": "before",
        "local": {
            "docker": {"image_id": "sha256:old", "application_commit": None, "running": True,
                       "mounts": [{"destination": "/data", "source": "/opt/data", "source_present": True}]},
            "protected_config_fingerprints": {"OAUTH_SIGNING_SECRET": "fingerprint"},
            "compose_config_fingerprint": "compose-fp",
            "google_home_sync_state": {"present": True, "valid_json": True, "linked": True,
                "protected_config_fingerprints": {"cloud_gateway_url": "url-fp", "catalog_write_key": "key-fp"}},
            "plex_connection_state": {"present": True, "valid_json": True,
                "client_identifier_fingerprint": "plex-client-fp", "server_identity_fingerprint": "plex-server-fp"},
            "installation_files": {".env_present": True,
                "subscriber_credentials": {"present": True, "valid_json": True, "valid_service_account": True,
                    "private_key_fingerprint": "key-fp"},
                "data_dir_present": True, "data_mount_configured": True,
                "subscriber_credentials_mount_configured": True},
            "music_index_sqlite": {"present": True, "integrity": "ok", "counts": {"tracks": 10}},
            "backup": {"rollback_ready": True},
        },
        "cloud_run": {"available": True, "project_id": "project-a", "service": "svc", "region": "region",
            "latest_ready_revision": "rev1", "image": "image1", "service_account": "sa",
            "url": "https://service.run.app", "traffic": [{"revision": "rev1", "percent": 100}],
            "protected_config_fingerprints": {"GCP_PROJECT_ID": "p", "GOOGLE_CLIENT_ID": "cid",
                "GOOGLE_CLIENT_SECRET": "secret-fp", "ADMIN_PASSPHRASE": "admin-fp",
                "OAUTH_SIGNING_SECRET": "cloud-fp"}},
        "api": api,
    }


def test_fingerprint_does_not_contain_config_value():
    fingerprints = acceptance._protected_fingerprints(b"k" * 32, {
        "OAUTH_SIGNING_SECRET": "never-write-this-secret",
    })
    assert fingerprints["OAUTH_SIGNING_SECRET"] != "never-write-this-secret"
    assert "never-write-this-secret" not in json.dumps(fingerprints)


def test_compare_ignores_mutable_scan_and_catalog_timestamps(tmp_path: Path):
    base = _valid_report()
    after = json.loads(json.dumps(base))
    after["captured_at"] = "after"
    after["api"]["google_home"]["catalog_sync"] = {"version": 2, "status": "succeeded", "updated_at": "after"}
    after["local"]["docker"]["image_id"] = "sha256:new"
    after["cloud_run"]["latest_ready_revision"] = "rev2"
    after["cloud_run"]["image"] = "image2"
    after["api"]["music_index"]["track_count"] = 11
    after["api"]["speakers"][0]["availability"] = "offline"
    left, right = tmp_path / "before.json", tmp_path / "after.json"
    left.write_text(json.dumps(base))
    right.write_text(json.dumps(after))
    safe, differences = acceptance.compare(left, right)
    assert safe
    assert differences == []


def test_compare_detects_cast_uuid_change_but_ignores_availability(tmp_path: Path):
    before, after = _valid_report(), _valid_report()
    after["api"]["speakers"][0]["availability"] = "offline"
    after["api"]["speakers"][0]["uuid"] = "replacement-cast"
    left, right = tmp_path / "before.json", tmp_path / "after.json"
    left.write_text(json.dumps(before))
    right.write_text(json.dumps(after))

    safe, differences = acceptance.compare(left, right, scope="local")
    assert not safe
    assert any(item["path"] == "api.speaker_identity" for item in differences)


def test_compare_fails_closed_on_identity_change(tmp_path: Path):
    base = _valid_report()
    after = json.loads(json.dumps(base))
    after["cloud_run"]["project_id"] = "project-b"
    left, right = tmp_path / "before.json", tmp_path / "after.json"
    left.write_text(json.dumps(base))
    right.write_text(json.dumps(after))
    safe, differences = acceptance.compare(left, right, scope="cloud")
    assert not safe
    assert any(item["path"] == "cloud_run.project_id" for item in differences)


def test_compare_fails_closed_when_required_cloud_evidence_is_missing(tmp_path: Path):
    before, after = _valid_report(), _valid_report()
    before["cloud_run"] = {"available": False}
    left, right = tmp_path / "before.json", tmp_path / "after.json"
    left.write_text(json.dumps(before))
    right.write_text(json.dumps(after))
    safe, differences = acceptance.compare(left, right, scope="cloud")
    assert not safe
    assert any("Cloud Run metadata unavailable" in item.get("reason", "") for item in differences)


def test_sqlite_integrity_uses_read_only_connection(tmp_path: Path):
    db_path = tmp_path / "music_index.sqlite3"
    db = sqlite3.connect(db_path)
    db.execute("create table tracks (id integer)")
    db.execute("insert into tracks values (1)")
    db.commit()
    db.close()

    result = acceptance._sqlite_integrity(db_path)
    assert result["integrity"] == "ok"
    assert result["counts"]["tracks"] == 1

    # The collector must not change database bytes or mtime as part of its check.
    before = db_path.stat().st_mtime_ns
    acceptance._sqlite_integrity(db_path)
    assert db_path.stat().st_mtime_ns == before


def test_api_capture_records_get_status_and_mix_retrieval(monkeypatch):
    responses = {
        "/health": {"status": "ok"},
        "/api/diagnostics": {"checks": [{"id": "plex", "status": "connected", "detail": "private detail"}]},
        "/api/speakers": [{"id": "device-1", "uuid": "cast-1", "name": "Bedroom", "ip": "192.0.2.1"}],
        "/api/status": {"Master Bedroom": {"state": "PLAYING", "volume": 0.2,
            "current_track": {"title": "private track"}, "queue_length": 2}},
        "/api/google-home/status": {"plex_linked": True, "pubsub_enabled": True,
            "catalog_sync": {"linked": True, "status": "succeeded", "version": "v1", "last_error": "private"},
            "speakers": [{"google_home_device_id": "device-1", "name": "Bedroom", "enabled": True,
                          "google_home_visible": True}]},
        "/api/music-index": {"settings": {"indexing_enabled": True},
            "scan": {"status": "idle", "scanned": 12, "error": None},
            "counts": {"tracks": 12, "artists": 4, "albums": 6}, "query": "private query"},
        "/api/mixes": [{"id": 7, "name": "private mix"}],
        "/api/mixes/7": {"id": 7, "name": "private mix", "tracks": [{"title": "private track"}]},
        "/api/settings": {"library": {"linked": True, "section": "Music", "tracks": 12},
                           "volumes": {"default_cast_volume": 0.4, "max_cast_volume": 0.85}},
    }

    def fake_get(url, token, timeout):
        path = url[url.index("://") + 3 :]
        path = "/" + path.split("/", 1)[1]
        body = responses.get(path)
        return (200 if body is not None else 404), 1, body, None

    monkeypatch.setattr(acceptance, "_get_json", fake_get)
    result = acceptance._api_capture("http://localhost:8000", None, 1)
    encoded = json.dumps(result)
    assert result["/health"]["http_status"] == 200
    assert result["saved_mix_ids"] == [7]
    assert result["mix:7"]["retrieved"] is True
    assert result["music_index"]["track_count"] == 12
    assert result["plex"]["section"] == "Music"
    assert result["playback"]["Master Bedroom"]["state"] == "PLAYING"
    assert result["playback"]["Master Bedroom"]["current_track_present"] is True
    assert result["speaker_identity"][0]["uuid"] == "cast-1"
    assert "availability" not in result["speaker_identity"][0]
    assert "private track" not in encoded
    assert "private query" not in encoded
    assert "private detail" not in encoded
    assert "private mix" not in encoded


def test_container_capture_records_immutable_digest_and_only_hints_unlabelled_commit(monkeypatch):
    results = [
        [{"Image": "sha256:config-id", "Config": {"Image": "ghcr.io/example:latest", "Env": [
            "OAUTH_SIGNING_SECRET=do-not-print", "PLEX_TOKEN=private-token",
        ]}, "State": {"Running": True}, "Mounts": []}],
        [{"RepoTags": ["ghcr.io/example:latest", "ghcr.io/example:pr41-256b9d0"],
          "RepoDigests": ["ghcr.io/example@sha256:published"],
          "Config": {"Labels": {"org.opencontainers.image.version": "0.2.2"}}}],
    ]
    monkeypatch.setattr(acceptance, "_run_json", lambda *_args, **_kwargs: results.pop(0))
    result = acceptance._container_capture("plex-cast-bridge", b"k" * 32)
    encoded = json.dumps(result)
    assert result["image_id"] == "sha256:config-id"
    assert result["published_image_digest"].endswith("sha256:published")
    assert result["application_commit"] is None
    assert result["application_commit_hint_from_tag"] == "256b9d0"
    assert "do-not-print" not in encoded
    assert "private-token" not in encoded


def test_private_state_dir_and_key_permissions(tmp_path: Path):
    state_dir = tmp_path / "acceptance"
    acceptance._private_dir(state_dir)
    acceptance._key(state_dir)
    assert os.stat(state_dir).st_mode & 0o777 == 0o700
    assert os.stat(state_dir / "fingerprint.key").st_mode & 0o777 == 0o600


def test_backup_inventory_requires_all_files_archive_and_cached_image(tmp_path: Path, monkeypatch):
    backup = tmp_path / "backups" / "20261111T000000Z"
    backup.mkdir(parents=True)
    for name in (
        "data.tgz", ".env", "docker-compose.yml", "gcp-credentials.json",
        "previous_image.txt", "previous_image_digest.txt",
    ):
        (backup / name).write_text("image-sha256:abc" if "image" in name else "placeholder")

    class Completed:
        returncode = 0

    monkeypatch.setattr(acceptance.subprocess, "run", lambda *a, **kw: Completed())
    result = acceptance._backup_capture(tmp_path / "backups")
    assert result["rollback_ready"] is True
    assert result["data_archive_valid"] is True
    assert result["previous_image_cached"] is True

    (backup / "gcp-credentials.json").unlink()
    result = acceptance._backup_capture(tmp_path / "backups")
    assert result["rollback_ready"] is False


def test_physical_cast_helper_requires_disruptive_flag_before_connecting():
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(root / "src"), str(root / "gcp-cloud-run/src")))}
    proc = subprocess.run(
        [sys.executable, str(root / "scripts/test_live_cast.py"), "--device-uuid", "uuid", "--query", "private query"],
        env=env, capture_output=True, text=True, check=False, timeout=20,
    )
    assert proc.returncode == 2
    assert "physical playback requires --allow-disruptive" in proc.stderr
    assert "private query" not in proc.stdout + proc.stderr


def test_physical_volume_helper_requires_observed_success_and_live_connection():
    from scripts.test_live_cast import set_volume_and_verify

    class Status:
        volume_level = 0.4

    class Client:
        is_connected = True

    class Cast:
        status = Status()
        socket_client = Client()

        def set_volume(self, target):
            self.status.volume_level = target

    class Speaker:
        cast = Cast()
        max_volume = 0.85

    speaker = Speaker()
    assert set_volume_and_verify(speaker, 0.2, timeout=0.1) is True
    assert speaker.cast.status.volume_level == 0.2

    speaker.cast.set_volume = lambda _target: None
    assert set_volume_and_verify(speaker, 0.3, timeout=0.01) is False

    speaker.cast.socket_client.is_connected = False
    assert set_volume_and_verify(speaker, 0.2, timeout=0.01) is False


def test_cloud_execute_helper_requires_disruptive_flag_and_test_subscription():
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(root / "src"), str(root / "gcp-cloud-run/src")))}
    proc = subprocess.run(
        [sys.executable, str(root / "scripts/test_voice_fulfillment.py"), "--project", "project",
         "--subscription", "test-isolated", "--device-id", "plex-jukebox:Kitchen", "--query", "private query"],
        env=env, capture_output=True, text=True, check=False, timeout=20,
    )
    assert proc.returncode == 2
    assert "a live Cloud Run command requires --allow-disruptive" in proc.stderr
    assert "private query" not in proc.stdout + proc.stderr


def test_live_cast_helper_routes_dashboard_commands_and_restores_volume():
    from types import SimpleNamespace

    from scripts.test_live_cast import run_device_test

    class Cast:
        def __init__(self):
            self.status = SimpleNamespace(volume_level=0.4)
            self.socket_client = SimpleNamespace(is_connected=True)
            self.media_controller = SimpleNamespace(
                status=SimpleNamespace(player_state="IDLE"), update_status=lambda: None
            )

        def set_volume(self, value):
            self.status.volume_level = value

    speaker = SimpleNamespace(cast=Cast(), max_volume=0.85, is_group=False)
    requests = []

    def request(_url, path, body):
        requests.append((path, body.copy()))
        if path == "/api/command":
            speaker.cast.media_controller.status.player_state = "PLAYING"
        elif body["action"] == "pause":
            speaker.cast.media_controller.status.player_state = "PAUSED"
        elif body["action"] == "resume":
            speaker.cast.media_controller.status.player_state = "PLAYING"
        elif body["action"] == "volume":
            speaker.cast.status.volume_level = body["volume"]
        elif body["action"] == "stop":
            speaker.cast.media_controller.status.player_state = "IDLE"
        return 200

    result = run_device_test(
        speaker=speaker, device_id="plex-jukebox:bedroom", query="private query",
        base_url="http://bridge", max_volume=0.18, request=request,
        sleep=lambda _delay: None,
    )
    assert [item[0] for item in result] == [
        "baseline", "safe_volume", "play", "pause", "resume", "volume_up",
        "volume_down", "stop", "restore_volume",
    ]
    assert requests[0] == (
        "/api/command", {"query": "private query", "device_id": "plex-jukebox:bedroom"}
    )
    assert [body.get("action") for path, body in requests if path == "/api/transport"] == [
        "pause", "resume", "volume", "volume", "stop"
    ]
    assert requests[3][1]["volume"] == 0.18
    assert abs(speaker.cast.status.volume_level - 0.4) <= 0.001
    assert result[-1][1] is True


def test_live_cast_helper_retries_stop_and_skips_volume_raise_if_unconfirmed():
    from types import SimpleNamespace

    from scripts.test_live_cast import AcceptanceFailure, run_device_test

    class Cast:
        def __init__(self):
            self.status = SimpleNamespace(volume_level=0.4)
            self.socket_client = SimpleNamespace(is_connected=True)
            self.media_controller = SimpleNamespace(
                status=SimpleNamespace(player_state="IDLE"), update_status=lambda: None
            )

        def set_volume(self, value):
            self.status.volume_level = value

    speaker = SimpleNamespace(cast=Cast(), max_volume=0.85, is_group=False)
    requests = []

    def request(_url, path, body):
        requests.append((path, body.copy()))
        if path == "/api/command":
            speaker.cast.media_controller.status.player_state = "PLAYING"
        elif body["action"] == "pause":
            speaker.cast.media_controller.status.player_state = "PAUSED"
        elif body["action"] == "resume":
            speaker.cast.media_controller.status.player_state = "PLAYING"
        elif body["action"] == "volume":
            speaker.cast.status.volume_level = body["volume"]
        return 200

    def wait(_speaker, _expected, **_kwargs):
        return speaker.cast.media_controller.status.player_state

    try:
        run_device_test(
            speaker=speaker, device_id="plex-jukebox:bedroom", query="private query",
            base_url="http://bridge", max_volume=0.85, request=request, wait=wait,
            sleep=lambda _delay: None,
        )
    except AcceptanceFailure as exc:
        result = exc.results
    else:
        raise AssertionError("unconfirmed stop must fail acceptance")

    stops = [body for path, body in requests if path == "/api/transport" and body["action"] == "stop"]
    assert len(stops) == 2
    assert ("cleanup_stop", 200, "PLAYING") in result
    assert result[-1][1] == "skipped_stop_not_confirmed"
    assert abs(speaker.cast.status.volume_level - 0.1) <= 0.001


def test_live_cast_helper_refuses_groups_before_sending_commands():
    from types import SimpleNamespace

    from scripts.test_live_cast import AcceptanceFailure, run_device_test

    speaker = SimpleNamespace(
        cast=SimpleNamespace(
            status=SimpleNamespace(volume_level=0.4),
            socket_client=SimpleNamespace(is_connected=True),
            media_controller=SimpleNamespace(
                status=SimpleNamespace(player_state="IDLE"), update_status=lambda: None
            ),
        ),
        max_volume=0.85,
        is_group=True,
    )
    requests = []
    try:
        run_device_test(
            speaker=speaker, device_id="plex-jukebox:Home", query="private query",
            base_url="http://bridge", max_volume=0.85,
            request=lambda *args: requests.append(args),
        )
    except AcceptanceFailure as exc:
        assert "Cast Groups" in str(exc)
    else:
        raise AssertionError("Cast Group must be refused")
    assert requests == []


def test_live_cast_helper_never_restores_volume_after_disconnect():
    from types import SimpleNamespace

    from scripts.test_live_cast import AcceptanceFailure, run_device_test

    class Cast:
        def __init__(self):
            self.status = SimpleNamespace(volume_level=0.4)
            self.socket_client = SimpleNamespace(is_connected=True)
            self.media_controller = SimpleNamespace(
                status=SimpleNamespace(player_state="IDLE"), update_status=lambda: None
            )

        def set_volume(self, value):
            self.status.volume_level = value

    speaker = SimpleNamespace(cast=Cast(), max_volume=0.85, is_group=False)
    requests = []

    def request(_url, path, body):
        requests.append((path, body.copy()))
        if path == "/api/command":
            speaker.cast.media_controller.status.player_state = "PLAYING"
        elif body["action"] == "pause":
            speaker.cast.media_controller.status.player_state = "PAUSED"
        elif body["action"] == "resume":
            speaker.cast.media_controller.status.player_state = "PLAYING"
        elif body["action"] == "volume":
            speaker.cast.status.volume_level = body["volume"]
        elif body["action"] == "stop":
            speaker.cast.socket_client.is_connected = False
        return 200

    def wait(_speaker, expected, **_kwargs):
        if not speaker.cast.socket_client.is_connected:
            return "DISCONNECTED"
        state = speaker.cast.media_controller.status.player_state
        return state if state in expected else "UNKNOWN"

    try:
        run_device_test(
            speaker=speaker, device_id="plex-jukebox:bedroom", query="private query",
            base_url="http://bridge", max_volume=0.85, request=request, wait=wait,
            sleep=lambda _delay: None,
        )
    except AcceptanceFailure as exc:
        result = exc.results
    else:
        raise AssertionError("disconnected receiver must fail acceptance")

    assert result[-1] == ("restore_volume", "skipped_connection_or_volume_unreadable")
    assert abs(speaker.cast.status.volume_level - 0.1) <= 0.001


def test_live_cast_dashboard_request_uses_optional_api_key_header(monkeypatch):
    from scripts import test_live_cast

    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b"{}"

    def open_url(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setenv("API_KEY", "private-api-key")
    monkeypatch.setattr(test_live_cast, "urlopen", open_url)

    assert test_live_cast.dashboard_request("http://bridge", "/api/transport", {"action": "stop"}) == 200
    assert captured["request"].get_header("X-api-key") == "private-api-key"
    assert "private-api-key" not in repr(captured["request"].data)


def test_dashboard_command_failure_log_does_not_include_private_query(monkeypatch, caplog):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from plex_cast_bridge import routes

    app = FastAPI()
    app.state.settings = SimpleNamespace()
    app.state.cast = SimpleNamespace()
    app.state.dj = SimpleNamespace()
    app.include_router(routes.router)
    monkeypatch.setattr(routes, "_require_plex", lambda _request: object())

    def fail(**_kwargs):
        raise RuntimeError("playback failed")

    monkeypatch.setattr(routes, "play", fail)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/api/command", json={"query": "private listening query"})

    assert response.status_code == 500
    assert "private listening query" not in caplog.text
