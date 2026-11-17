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
        upgrade.verify_plan({"schema": 2, "key": "a" * 64, "verification": {}}, {})


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
