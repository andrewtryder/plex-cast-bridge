import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import upgrade


def test_inspect_and_plan_redact_credentials_and_record_reviewed_identity(tmp_path, monkeypatch):
    data = tmp_path / "custom-data"
    data.mkdir()
    secret = "DO-NOT-EXPOSE-THIS"
    (data / "plex_connection.json").write_text(json.dumps({"jwt_token": secret}))
    compose = tmp_path / "compose.yml"
    compose.write_text("services: {}")
    override = tmp_path / "compose.override.yml"
    override.write_text("services: {}")
    container = {"Id": "abcde", "Image": "sha256:old", "State": {"Running": True},
                 "Config": {"Env": ["PLEX_TOKEN=" + secret]}, "HostConfig": {},
                 "Mounts": [{"Type": "bind", "Source": str(data), "Destination": "/custom", "RW": True}]}

    def fake_docker(*args):
        if "config" in args:
            assert args.count("-f") == 2
            assert str(override.resolve()) in args
            return json.dumps({"services": {"bridge": {"image": "example:old", "environment": {"SECRET": secret}}}})
        if args[-2:] == ("-q", "bridge"):
            return "abcde\n"
        if args[:2] == ("inspect", "abcde"):
            return json.dumps([container])
        if args[0] == "exec":
            return "/custom\n"
        if args == ("ps", "-q"):
            return "abcde\n"
        if args[:2] == ("image", "inspect"):
            return '[{"RepoDigests":["example@sha256:old"]}]'
        if args[:2] == ("buildx", "imagetools"):
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(upgrade, "docker", fake_docker)
    monkeypatch.setattr(upgrade, "_configuration_drift_capture", lambda _: {"available": True, "report": {"release_preflight_warnings": []}})
    monkeypatch.setattr(upgrade, "_catalog_snapshot_capture", lambda _: {"available": True, "speakers": []})
    state = upgrade.inspect([compose, override], None)
    plan_file = tmp_path / "private" / "plan.json"
    report = upgrade.plan(state, "example@sha256:" + "a" * 64, plan_file)
    assert report["installation"]["service"] == "bridge"
    assert report["installation"]["compose_files"] == [str(compose.resolve()), str(override.resolve())]
    assert report["installation"]["data_source"] == str(data)
    assert secret not in json.dumps(report)
    assert secret not in plan_file.read_text()
    assert plan_file.stat().st_mode & 0o077 == 0
    before = plan_file.read_bytes()
    upgrade.plan(state, "example@sha256:" + "a" * 64, plan_file)
    assert plan_file.read_bytes() == before
    tampered = json.loads(before)
    tampered["target"] = "example@sha256:" + "b" * 64
    plan_file.write_text(json.dumps(tampered))
    with pytest.raises(ValueError, match="changed"):
        upgrade.load_plan(plan_file)
    plan_file.write_bytes(before)
    state["_protected"]["files"]["plex_connection.json"] = "changed"
    with pytest.raises(ValueError, match="no longer matches"):
        upgrade.plan(state, "example@sha256:" + "a" * 64, plan_file)


def test_plan_refuses_mutable_target_or_unavailable_registry(tmp_path, monkeypatch):
    state = {"_protected": {}, "configuration_drift": {"status": "PASS", "warnings": []}}
    with pytest.raises(ValueError, match="immutable"):
        upgrade.plan(state, "example:latest", tmp_path / "plan.json")
    monkeypatch.setattr(upgrade, "docker", lambda *args: (_ for _ in ()).throw(RuntimeError("unavailable")))
    with pytest.raises(RuntimeError, match="unavailable"):
        upgrade.plan(state, "example@sha256:" + "b" * 64, tmp_path / "plan.json")
    assert not (tmp_path / "plan.json").exists()


def test_plan_refuses_unavailable_drift_before_writing(tmp_path, monkeypatch):
    state = {"_protected": {}, "configuration_drift": {"status": "NOT TESTED", "warnings": []}}
    monkeypatch.setattr(upgrade, "docker", lambda *args: "")
    with pytest.raises(ValueError, match="NOT TESTED"):
        upgrade.plan(state, "example@sha256:" + "a" * 64, tmp_path / "plan.json")
    assert not (tmp_path / "plan.json").exists()


def test_release_lookup_resolves_official_immutable_digest(monkeypatch):
    from io import BytesIO
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: BytesIO(b'{"tag_name":"v1.2.3","draft":false,"prerelease":false}'))
    monkeypatch.setattr(upgrade, "docker", lambda *args: "Name: image\nDigest: sha256:" + "d" * 64 + "\n")
    found = upgrade.latest_release()
    assert found == {"version": "1.2.3", "target": "ghcr.io/andrewtryder/plex-cast-bridge@sha256:" + "d" * 64}


def test_verification_ignores_operational_state_but_detects_identity_change(tmp_path, monkeypatch):
    def raw(value):
        return json.dumps(value).encode().hex()

    monkeypatch.setattr(upgrade, "docker", lambda *args: "")
    target = "example@sha256:" + "a" * 64
    files = {"plex_connection.json": raw({"mode": "jwt", "jwt_token": "token", "client_identifier": "id"}),
             "app_settings.json": raw({"default_cast_volume": 0.4}),
             "catalog.json": raw({"speakers": [{"id": "virtual-1", "cast_uuid": "cast-1", "last_seen": "old"}]}),
             "google_home_sync.json": raw({"cloud_gateway_url": "url", "catalog_write_key": "secret", "version": 1}),
             "google_home_setup.json": None, "plex_device_private.pem": "0102", "plex_device_public.pem": "0304"}
    protected = {"files": files, "compose": {"services": {}},
                 "container": {"Mounts": [{"Type": "bind", "Source": "/x", "Destination": "/data", "RW": True}]}}
    state = {"_protected": protected, "configuration_drift": {"status": "PASS", "warnings": []}, "current_image_digests": []}
    path = tmp_path / "plan.json"
    upgrade.plan(state, target, path)
    content = json.loads(path.read_text())
    state["current_image_digests"] = [target]
    files["google_home_sync.json"] = raw({"cloud_gateway_url": "url", "catalog_write_key": "secret", "version": 2})
    files["catalog.json"] = raw({"speakers": [{"id": "virtual-1", "cast_uuid": "cast-1", "last_seen": "new"}]})
    assert upgrade.verify_plan(content, state) == []
    files["catalog.json"] = raw({"speakers": [{"id": "virtual-2", "cast_uuid": "cast-1"}]})
    assert "persistent identities or credentials changed" in upgrade.verify_plan(content, state)
    state["configuration_drift"] = {"status": "NOT TESTED", "warnings": []}
    assert "configuration drift diagnostic NOT TESTED" in upgrade.verify_plan(content, state)
    assert "secret" not in path.read_text()


def test_custom_catalog_must_be_backed_up_with_data_mount():
    assert upgrade._catalog_relative("/data", "/data/custom/library.json", "/app") == Path("custom/library.json")
    assert upgrade._catalog_relative("/data", "catalog.json", "/data") == Path("catalog.json")
    with pytest.raises(ValueError, match="outside the data mount"):
        upgrade._catalog_relative("/data", "/other/catalog.json", "/app")
    with pytest.raises(ValueError, match="parent traversal"):
        upgrade._catalog_relative("/data", "/data/../other/catalog.json", "/app")


def test_verification_rejects_incomplete_plan():
    with pytest.raises(ValueError, match="lacks valid verification evidence"):
        upgrade.verify_plan({"schema": 5, "key": "a" * 64, "verification": {}}, {})


def test_compose_inputs_include_override_env_and_secret_files_without_printing(tmp_path):
    compose = tmp_path / "compose.yml"
    override = tmp_path / "override.yml"
    env_file = tmp_path / "custom.env"
    secret = tmp_path / "secret.txt"
    for path in (compose, override, env_file, secret):
        path.write_text("private content")
    unresolved = {"services": {"bridge": {"env_file": [{"path": str(env_file)}]}},
                  "secrets": {"key": {"file": str(secret)}}}
    captured = upgrade._configuration_inputs([compose, override], unresolved, {})
    assert all(str(path.resolve()) in captured for path in (compose, override, env_file, secret))
    assert captured[str(tmp_path / ".env")] is None


def test_check_plan_refuses_changed_identity_or_missing_privacy(tmp_path, monkeypatch):
    state = {"_protected": {"files": {}, "compose": {}, "container": {}},
             "configuration_drift": {"status": "PASS", "warnings": []},
             "compose_files": ["/synthetic/compose.yml"], "service": "bridge"}
    monkeypatch.setattr(upgrade, "docker", lambda *args: "")
    monkeypatch.setattr(upgrade, "inspect", lambda *args: state)
    path = tmp_path / "plan.json"
    upgrade.plan(state, "example@sha256:" + "a" * 64, path)
    upgrade.check_plan_file(path)
    state["_protected"]["files"]["some-secret"] = "changed"
    with pytest.raises(ValueError, match="no longer matches"):
        upgrade.check_plan_file(path)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        upgrade.check_plan_file(path)


def test_apply_requires_explicit_acknowledgements_before_writing(tmp_path, monkeypatch):
    state = {"configuration_drift": {"status": "PASS", "warnings": ["catalog_write_key"]},
             "_protected": {"inputs": {}}, "compose_files": [str(tmp_path / "compose.yml")], "service": "bridge"}
    content = {"backup_dir": str(tmp_path / "backup"), "target": "image@sha256:" + "a" * 64}
    monkeypatch.setattr(upgrade, "check_plan_file", lambda _: (content, state))
    with pytest.raises(ValueError, match="non-container writers"):
        upgrade.apply_plan(tmp_path / "plan.json", confirm_exclusive=False, acknowledge_drift=False, health_url="http://localhost/health")
    with pytest.raises(ValueError, match="explicit acknowledgement"):
        upgrade.apply_plan(tmp_path / "plan.json", confirm_exclusive=True, acknowledge_drift=False, health_url="http://localhost/health")
    assert not (tmp_path / "backup").exists()


def test_apply_never_claims_full_verification_when_index_is_untested(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    import urllib.request
    state = {"configuration_drift": {"status": "PASS", "warnings": []},
             "_protected": {"inputs": {}}, "compose_files": [str(tmp_path / "compose.yml")],
             "service": "bridge", "music_index_present": True, "data_source": str(tmp_path)}
    content = {"backup_dir": str(tmp_path / "backup"), "target": "image@sha256:" + "a" * 64,
               "plan_auth": "signed", "installation": {"compose_files": [str(tmp_path / "compose.yml")], "service": "bridge"}}
    monkeypatch.setattr(upgrade, "check_plan_file", lambda _: (content, state))
    monkeypatch.setattr(upgrade, "load_plan", lambda _: content)
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: Response())
    monkeypatch.setattr(upgrade, "create_inputs_backup", lambda *args: None)
    monkeypatch.setattr(upgrade, "verify_inputs_backup", lambda *args: None)
    monkeypatch.setattr(upgrade, "verify_backup", lambda *args: None)
    monkeypatch.setattr(upgrade, "_receipt", lambda *args: {"schema": 2, "plan_auth": "signed", "target": content["target"]})
    monkeypatch.setattr(upgrade, "inspect", lambda *args: state)
    monkeypatch.setattr(upgrade, "verify_plan", lambda *args: [])
    monkeypatch.setattr(upgrade, "verify_music_backup", lambda *args: ["music index verification NOT TESTED"])
    calls = []
    monkeypatch.setattr(upgrade.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(returncode=0))
    result = upgrade.apply_plan(tmp_path / "plan.json", confirm_exclusive=True, acknowledge_drift=False, health_url="http://localhost/health")
    assert result == 2
    assert "NOT TESTED" in capsys.readouterr().err
    assert calls[0][1]["env"]["TARGET_IMAGE"] == content["target"]


def test_saved_mix_verification_compares_restored_backup_without_writes(tmp_path):
    import sqlite3
    from upgrade_backup import create

    data = tmp_path / "data"
    data.mkdir()
    path = data / "music_index.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE tracks (server_id TEXT, library_id TEXT, rating_key TEXT)")
        db.execute("CREATE TABLE artists (server_id TEXT, library_id TEXT, artist_key TEXT)")
        db.execute("CREATE TABLE albums (server_id TEXT, library_id TEXT, album_key TEXT)")
        db.execute("INSERT INTO tracks VALUES ('server', 'library', 'track')")
        db.execute("CREATE TABLE mixes (id INTEGER PRIMARY KEY, name TEXT)")
        db.execute("CREATE TABLE mix_tracks (mix_id INTEGER, position INTEGER, rating_key TEXT)")
        db.execute("INSERT INTO mixes VALUES (1, 'private name')")
        db.execute("INSERT INTO mix_tracks VALUES (1, 0, 'track')")
    archive = tmp_path / "backup.tgz"
    create(data, archive)
    before = path.read_bytes(), path.stat().st_mtime_ns
    assert upgrade.verify_music_backup(archive, data) == []
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM mix_tracks")
    assert upgrade.verify_music_backup(archive, data) == ["music index or saved mixes changed from the verified backup"]
    (data / "music_index.sqlite3-wal").write_bytes(b"active")
    assert "NOT TESTED" in upgrade.verify_music_backup(archive, data)[0]


def test_unbacked_writable_mounts_cannot_be_silently_ignored():
    upgrade._require_backed_mounts({"Mounts": [{"Destination": "/custom", "RW": True}, {"Destination": "/creds", "RW": False}]}, "/custom")
    with pytest.raises(ValueError, match="writable mounts"):
        upgrade._require_backed_mounts({"Mounts": [{"Destination": "/custom", "RW": True}, {"Destination": "/other", "RW": True}]}, "/custom")
    with pytest.raises(ValueError, match="writable mounts"):
        upgrade._require_backed_mounts({"Mounts": [{"Destination": "/custom", "RW": True}]}, "/custom/data")


def test_rollback_from_stopped_candidate_preserves_current_data(tmp_path, monkeypatch):
    from types import SimpleNamespace
    data = tmp_path / "data"
    data.mkdir()
    (data / "sentinel").write_text("newer user data")
    backup = tmp_path / "backup"
    backup.mkdir()
    old_id = "sha256:" + "1" * 64
    (backup / "previous_image.txt").write_text(old_id)
    (backup / "previous_image_digest.txt").write_text(old_id)
    container = {"Id": "candidate", "State": {"Running": False},
                 "Mounts": [{"Type": "bind", "Source": str(data), "Destination": "/data", "RW": True}]}
    key = b"k" * 32
    saved = {"compose_files": [str(tmp_path / "compose.yml")], "service": "bridge", "data_dir": "/data",
             "data_source": str(data), "current_image_id": old_id, "current_image_digests": []}
    content = {"backup_dir": str(backup), "installation": saved, "key": key.hex(),
               "verification": {"mounts": upgrade._mount_signature(container, key)}}
    monkeypatch.setattr(upgrade, "load_plan", lambda _: content)
    monkeypatch.setattr(upgrade, "verify_backup", lambda _: None)
    monkeypatch.setattr(upgrade, "resolve_data_mount", lambda *args: data)
    monkeypatch.setattr(upgrade, "inspect", lambda *args: {"current_image_id": old_id})
    def fake_docker(*args):
        if "ps" in args and "-a" in args:
            return "candidate\n"
        if args == ("inspect", "candidate"):
            return json.dumps([container])
        if args == ("ps", "-q"):
            return ""
        raise AssertionError(args)
    monkeypatch.setattr(upgrade, "docker", fake_docker)
    monkeypatch.setattr(upgrade.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    assert upgrade.rollback_plan(tmp_path / "plan", confirm=True, health_url="http://localhost/health") == 0
    assert (data / "sentinel").read_text() == "newer user data"
    with pytest.raises(ValueError, match="Confirm image rollback"):
        upgrade.rollback_plan(tmp_path / "plan", confirm=False, health_url="http://localhost/health")


def test_rollback_recovers_when_failed_recreation_left_no_container(tmp_path, monkeypatch):
    from types import SimpleNamespace
    data = tmp_path / "data"
    data.mkdir()
    (data / "sentinel").write_text("keep")
    compose = tmp_path / "compose.yml"
    compose.write_text("services: {}")
    backup = tmp_path / "backup"
    backup.mkdir()
    old_id = "sha256:" + "1" * 64
    (backup / "previous_image.txt").write_text(old_id)
    (backup / "previous_image_digest.txt").write_text(old_id)
    mounts = [{"Type": "bind", "Source": str(data), "Destination": "/data", "RW": True}]
    key = b"k" * 32
    inputs = upgrade._read_recorded_inputs([str(compose), str(tmp_path / ".env")])
    config = {"services": {}}
    saved = {"compose_files": [str(compose)], "service": "bridge", "data_dir": "/data",
             "data_source": str(data), "current_image_id": old_id, "current_image_digests": [],
             "mounts": mounts, "configuration_input_paths": list(inputs)}
    content = {"backup_dir": str(backup), "installation": saved, "key": key.hex(), "verification": {
        "mounts": upgrade._mount_signature({"Mounts": mounts}, key),
        "compose_configuration": upgrade._signature({"rendered": config, "inputs": inputs}, key)}}
    monkeypatch.setattr(upgrade, "load_plan", lambda _: content)
    monkeypatch.setattr(upgrade, "verify_backup", lambda _: None)
    monkeypatch.setattr(upgrade, "resolve_data_mount", lambda *args: data)
    monkeypatch.setattr(upgrade, "inspect", lambda *args: {"current_image_id": old_id})
    def fake_docker(*args):
        if "ps" in args:
            return ""
        if "config" in args:
            return json.dumps(config)
        raise AssertionError(args)
    monkeypatch.setattr(upgrade, "docker", fake_docker)
    monkeypatch.setattr(upgrade.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    assert upgrade.rollback_plan(tmp_path / "plan", confirm=True, health_url="http://localhost/health") == 0
    assert (data / "sentinel").read_text() == "keep"
