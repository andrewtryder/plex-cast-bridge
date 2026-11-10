import base64
import urllib.parse

from cloud_gateway.smart_home import generate_voice_synonyms


def test_voice_synonyms_generation():
    syns = generate_voice_synonyms("Kruder & Dorfmeister")
    assert "Kruder and Dorfmeister" in syns
    assert "Kruder & Dorfmeister Radio" in syns

    dj_syns = generate_voice_synonyms("DJ Shadow")
    assert "D J Shadow" in dj_syns
    assert "Deejay Shadow" in dj_syns

    article_syns = generate_voice_synonyms("A Tribe Called Quest")
    assert "Tribe Called Quest" in article_syns


def test_health_endpoint(client):
    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"


def test_oauth_authorize_get_validation(client):
    res = client.get(
        "/oauth/authorize",
        params={
            "client_id": "wrong-client",
            "redirect_uri": "https://oauth-redirect.googleusercontent.com/r/test",
            "state": "xyz",
            "response_type": "code",
        },
    )
    assert res.status_code == 400

    res = client.get(
        "/oauth/authorize",
        params={
            "client_id": "test-client-id",
            "redirect_uri": "https://oauth-redirect.googleusercontent.com/r/test",
            "state": "xyz",
            "response_type": "code",
        },
    )
    assert res.status_code == 200
    assert "Plex Cast Bridge" in res.text
    assert "dashboard" in res.text.lower()


def test_oauth_authorize_post_failure(client):
    res = client.post(
        "/oauth/authorize",
        data={
            "client_id": "test-client-id",
            "redirect_uri": "https://oauth-redirect.googleusercontent.com/r/test",
            "state": "xyz",
            "response_type": "code",
            "passphrase": "wrong-password",
        },
    )
    assert res.status_code == 401
    assert "Invalid Admin Passphrase" in res.text


def test_oauth_login_throttle_returns_429(client):
    from cloud_gateway.rate_limit import MAX_FAILED_LOGINS, reset_for_tests

    reset_for_tests()
    payload = {
        "client_id": "test-client-id",
        "redirect_uri": "https://oauth-redirect.googleusercontent.com/r/test",
        "state": "xyz",
        "response_type": "code",
        "passphrase": "wrong-password",
    }
    for _ in range(MAX_FAILED_LOGINS):
        res = client.post("/oauth/authorize", data=payload)
        assert res.status_code == 401
    res = client.post("/oauth/authorize", data=payload)
    assert res.status_code == 429
    assert "Too many failed authorization attempts" in res.text
    reset_for_tests()


def test_oauth_flow_and_token_exchange(client):
    res = client.post(
        "/oauth/authorize",
        data={
            "client_id": "test-client-id",
            "redirect_uri": "https://oauth-redirect.googleusercontent.com/r/test",
            "state": "state123",
            "response_type": "code",
            "passphrase": "test-passphrase",
        },
        follow_redirects=False,
    )
    assert res.status_code == 302
    location = res.headers["location"]
    parsed = urllib.parse.urlsplit(location)
    q = dict(urllib.parse.parse_qsl(parsed.query))
    assert q["state"] == "state123"
    auth_code = q["code"]
    assert auth_code

    token_res = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "client_id": "test-client-id",
            "client_secret": "test-client-secret",
            "redirect_uri": "https://oauth-redirect.googleusercontent.com/r/test",
        },
    )
    assert token_res.status_code == 200
    token_data = token_res.json()
    assert token_data["token_type"] == "Bearer"
    assert "access_token" in token_data
    assert "refresh_token" in token_data
    assert token_data["expires_in"] > 0

    refresh_res = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": token_data["refresh_token"],
            "client_id": "test-client-id",
            "client_secret": "test-client-secret",
        },
    )
    assert refresh_res.status_code == 200
    new_tokens = refresh_res.json()
    assert "access_token" in new_tokens

    basic_val = base64.b64encode(b"test-client-id:test-client-secret").decode("utf-8")
    basic_res = client.post(
        "/oauth/token",
        headers={"Authorization": f"Basic {basic_val}"},
        data={
            "grant_type": "refresh_token",
            "refresh_token": token_data["refresh_token"],
        },
    )
    assert basic_res.status_code == 200


def test_fulfillment_auth_required(client):
    res = client.post("/fulfillment", json={"requestId": "123", "inputs": [{"intent": "action.devices.SYNC"}]})
    assert res.status_code == 401

    res = client.post(
        "/fulfillment",
        headers={"Authorization": "Bearer invalid-token"},
        json={"requestId": "123", "inputs": [{"intent": "action.devices.SYNC"}]},
    )
    assert res.status_code == 401


def test_fulfillment_sync(client, auth_headers):
    payload = {
        "requestId": "sync-req-1",
        "inputs": [{"intent": "action.devices.SYNC"}],
    }
    res = client.post("/fulfillment", headers=auth_headers, json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["requestId"] == "sync-req-1"
    devices = data["payload"]["devices"]
    # Great Room is disabled in test catalog, so only Master Bedroom and Kitchen should sync
    assert len(devices) == 2
    assert devices[0]["id"] == "plex-jukebox:Master Bedroom"
    assert devices[0]["roomHint"] == "Master Bedroom"
    assert devices[1]["id"] == "plex-jukebox:Kitchen"
    assert devices[1]["roomHint"] == "Kitchen"
    dev = devices[0]
    assert dev["type"] == "action.devices.types.SPEAKER"
    assert "action.devices.traits.MediaState" in dev["traits"]

    attrs = dev["attributes"]
    channels = attrs["availableChannels"]
    assert any("Nirvana" in c["names"] for c in channels)
    assert any("Kruder and Dorfmeister" in c["names"] for c in channels)
    apps = attrs["availableApplications"]
    assert any(any("Rock" in s for s in a["names"][0]["name_synonym"]) for a in apps)
    assert devices[0]["deviceInfo"]["model"] == "Plex Cast Bridge"
    assert devices[0]["deviceInfo"]["manufacturer"] == "Plex Cast Bridge"


def test_fulfillment_disconnect(client, auth_headers):
    payload = {
        "requestId": "disconnect-req-1",
        "inputs": [{"intent": "action.devices.DISCONNECT"}],
    }
    res = client.post("/fulfillment", headers=auth_headers, json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["requestId"] == "disconnect-req-1"
    assert "payload" not in data or data.get("payload") in (None, {})


def test_fulfillment_query(client, auth_headers):
    payload = {
        "requestId": "query-req-1",
        "inputs": [
            {
                "intent": "action.devices.QUERY",
                "payload": {"devices": [{"id": "plex-jukebox:Kitchen"}]},
            }
        ],
    }
    res = client.post("/fulfillment", headers=auth_headers, json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["requestId"] == "query-req-1"
    dev_status = data["payload"]["devices"]["plex-jukebox:Kitchen"]
    assert dev_status["online"] is True
    assert dev_status["status"] == "SUCCESS"
    assert "playbackState" not in dev_status


def test_fulfillment_execute_commands(client, auth_headers):
    published_messages = []

    def mock_publish(payload):
        published_messages.append(payload)
        return "msg-test-123"

    client.app.state.pubsub.publish_sync = mock_publish

    cmd_channel = {
        "requestId": "exec-req-1",
        "inputs": [
            {
                "intent": "action.devices.EXECUTE",
                "payload": {
                    "commands": [
                        {
                            "devices": [{"id": "plex-jukebox:Kitchen"}],
                            "execution": [
                                {
                                    "command": "action.devices.commands.selectChannel",
                                    "params": {"channelName": "Pink Floyd"},
                                }
                            ],
                        }
                    ]
                },
            }
        ],
    }
    res = client.post("/fulfillment", headers=auth_headers, json=cmd_channel)
    assert res.status_code == 200
    assert res.json()["payload"]["commands"][0]["status"] == "SUCCESS"
    assert len(published_messages) == 1
    assert published_messages[0]["action"] == "play"
    assert published_messages[0]["query"] == "Pink Floyd"
    assert published_messages[0]["speaker"] == "Kitchen"

    cmd_next = {
        "requestId": "exec-req-2",
        "inputs": [
            {
                "intent": "action.devices.EXECUTE",
                "payload": {
                    "commands": [
                        {
                            "devices": [{"id": "plex-jukebox:Kitchen"}],
                            "execution": [{"command": "action.devices.commands.mediaNext", "params": {}}],
                        }
                    ]
                },
            }
        ],
    }
    res = client.post("/fulfillment", headers=auth_headers, json=cmd_next)
    assert res.status_code == 200
    assert published_messages[1]["action"] == "next"

    # Test setVolume uses dedicated 'volume' action rather than 'resume'
    cmd_vol = {
        "requestId": "exec-req-3",
        "inputs": [
            {
                "intent": "action.devices.EXECUTE",
                "payload": {
                    "commands": [
                        {
                            "devices": [{"id": "plex-jukebox:Kitchen"}],
                            "execution": [
                                {"command": "action.devices.commands.setVolume", "params": {"volumeLevel": 65}}
                            ],
                        }
                    ]
                },
            }
        ],
    }
    res = client.post("/fulfillment", headers=auth_headers, json=cmd_vol)
    assert res.status_code == 200
    assert published_messages[2]["action"] == "volume"
    assert published_messages[2]["volume"] == 0.65


def test_custom_webhook_query(client):
    published = []
    client.app.state.pubsub.publish_sync = lambda p: published.append(p) or "msg-hook-123"

    res = client.post("/webhook/query", json={"query": "Play Beatles"})
    assert res.status_code == 401

    res = client.post(
        "/webhook/query",
        headers={"X-API-Key": "wrong-key"},
        json={"query": "Play Beatles"},
    )
    assert res.status_code == 401

    res = client.post(
        "/webhook/query",
        headers={"X-API-Key": "test-webhook-key"},
        json={"query": "Play Beatles in Office", "speaker": "Office"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "queued"
    assert data["query"] == "Play Beatles in Office"
    assert data["speaker"] == "Office"
    assert len(published) == 1
    assert published[0]["action"] == "play"


def test_oauth_security_controls(client):
    # 1. Reject untrusted redirect_uri (open redirect prevention)
    res = client.get(
        "/oauth/authorize",
        params={
            "client_id": "test-client-id",
            "redirect_uri": "https://evil-attacker.com/steal-code",
            "state": "xyz",
            "response_type": "code",
        },
    )
    assert res.status_code == 400
    assert "Unauthorized redirect_uri" in res.text

    # 2. Complete a legitimate authorization
    res = client.post(
        "/oauth/authorize",
        data={
            "client_id": "test-client-id",
            "redirect_uri": "https://oauth-redirect.googleusercontent.com/r/test",
            "state": "state-security",
            "response_type": "code",
            "passphrase": "test-passphrase",
        },
        follow_redirects=False,
    )
    assert res.status_code == 302
    parsed = urllib.parse.urlsplit(res.headers["location"])
    auth_code = dict(urllib.parse.parse_qsl(parsed.query))["code"]

    # 3. First exchange succeeds
    token_res = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "client_id": "test-client-id",
            "client_secret": "test-client-secret",
            "redirect_uri": "https://oauth-redirect.googleusercontent.com/r/test",
        },
    )
    assert token_res.status_code == 200

    # 4. Single-use enforcement: Attempting to reuse the same authorization code MUST fail
    reuse_res = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "client_id": "test-client-id",
            "client_secret": "test-client-secret",
            "redirect_uri": "https://oauth-redirect.googleusercontent.com/r/test",
        },
    )
    assert reuse_res.status_code == 400
    assert "already been redeemed" in reuse_res.text


def test_smart_home_command_key_translation_and_fail_closed(client, auth_headers):
    published = []

    def mock_pub(payload):
        published.append(payload)
        return "msg-key-test"

    client.app.state.pubsub.publish_sync = mock_pub

    # 1. Test SetInput translates input_kitchen -> Kitchen
    cmd_set_input = {
        "requestId": "exec-setinput",
        "inputs": [
            {
                "intent": "action.devices.EXECUTE",
                "payload": {
                    "commands": [
                        {
                            "devices": [{"id": "plex-jukebox:Master Bedroom"}],
                            "execution": [
                                {
                                    "command": "action.devices.commands.SetInput",
                                    "params": {"newInput": "input_kitchen"},
                                }
                            ],
                        }
                    ]
                },
            }
        ],
    }
    res = client.post("/fulfillment", headers=auth_headers, json=cmd_set_input)
    assert res.status_code == 200
    assert published[0]["action"] == "resume"
    # Virtual device ID owns routing; SetInput must not retarget another speaker.
    assert published[0]["speaker"] == "Master Bedroom"

    # 2. Test appSelect translates app_genre_rock -> Rock
    cmd_app_select = {
        "requestId": "exec-appselect",
        "inputs": [
            {
                "intent": "action.devices.EXECUTE",
                "payload": {
                    "commands": [
                        {
                            "devices": [{"id": "plex-jukebox:Master Bedroom"}],
                            "execution": [
                                {
                                    "command": "action.devices.commands.appSelect",
                                    "params": {"newApplication": "app_genre_rock"},
                                }
                            ],
                        }
                    ]
                },
            }
        ],
    }
    res = client.post("/fulfillment", headers=auth_headers, json=cmd_app_select)
    assert res.status_code == 200
    assert published[1]["action"] == "play"
    assert published[1]["query"] == "Rock"

    # 3. Test mediaPrevious emits previous
    cmd_prev = {
        "requestId": "exec-prev",
        "inputs": [
            {
                "intent": "action.devices.EXECUTE",
                "payload": {
                    "commands": [
                        {
                            "devices": [{"id": "plex-jukebox:Master Bedroom"}],
                            "execution": [
                                {"command": "action.devices.commands.mediaPrevious", "params": {}}
                            ],
                        }
                    ]
                },
            }
        ],
    }
    res = client.post("/fulfillment", headers=auth_headers, json=cmd_prev)
    assert res.status_code == 200
    assert published[2]["action"] == "previous"

    # 4. Test Pub/Sub failure fails closed (returns status: ERROR to Google Home)
    client.app.state.pubsub.publish_sync = lambda p: (_ for _ in ()).throw(RuntimeError("Pub/Sub outage"))
    cmd_fail = {
        "requestId": "exec-fail",
        "inputs": [
            {
                "intent": "action.devices.EXECUTE",
                "payload": {
                    "commands": [
                        {
                            "devices": [{"id": "plex-jukebox:Master Bedroom"}],
                            "execution": [
                                {"command": "action.devices.commands.mediaNext", "params": {}}
                            ],
                        }
                    ]
                },
            }
        ],
    }
    res = client.post("/fulfillment", headers=auth_headers, json=cmd_fail)
    assert res.status_code == 200
    data = res.json()
    cmd_result = data["payload"]["commands"][0]
    assert cmd_result["status"] == "ERROR"
    assert cmd_result["errorCode"] == "transientError"


def test_oauth_firestore_distributed_single_use(client):
    from unittest.mock import MagicMock, patch

    from google.api_core.exceptions import AlreadyExists

    mock_db = MagicMock()
    mock_doc = MagicMock()
    mock_db.collection.return_value.document.return_value = mock_doc
    mock_doc.create.side_effect = [None, AlreadyExists("Document already exists")]

    res = client.post(
        "/oauth/authorize",
        data={
            "client_id": "test-client-id",
            "redirect_uri": "https://oauth-redirect.googleusercontent.com/r/test",
            "state": "state-firestore",
            "response_type": "code",
            "passphrase": "test-passphrase",
        },
        follow_redirects=False,
    )
    auth_code = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(res.headers["location"]).query))["code"]

    with patch("cloud_gateway.auth.get_firestore_client", return_value=mock_db):
        token_res = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "client_id": "test-client-id",
                "client_secret": "test-client-secret",
                "redirect_uri": "https://oauth-redirect.googleusercontent.com/r/test",
            },
        )
        assert token_res.status_code == 200

        replay_res = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "client_id": "test-client-id",
                "client_secret": "test-client-secret",
                "redirect_uri": "https://oauth-redirect.googleusercontent.com/r/test",
            },
        )
        assert replay_res.status_code == 400
        assert "already been redeemed" in replay_res.text


def test_oauth_firestore_fails_closed_in_production(client):
    from unittest.mock import MagicMock, patch

    client.app.state.settings.environment = "production"

    res = client.post(
        "/oauth/authorize",
        data={
            "client_id": "test-client-id",
            "redirect_uri": "https://oauth-redirect.googleusercontent.com/r/test",
            "state": "state-prod-fail",
            "response_type": "code",
            "passphrase": "test-passphrase",
        },
        follow_redirects=False,
    )
    auth_code = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(res.headers["location"]).query))["code"]

    with patch("cloud_gateway.auth.get_firestore_client", return_value=None):
        token_res = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "client_id": "test-client-id",
                "client_secret": "test-client-secret",
                "redirect_uri": "https://oauth-redirect.googleusercontent.com/r/test",
            },
        )
        assert token_res.status_code == 503
        assert "unavailable" in token_res.text

    mock_db = MagicMock()
    mock_db.collection.return_value.document.return_value.create.side_effect = RuntimeError("Firestore network error")
    with patch("cloud_gateway.auth.get_firestore_client", return_value=mock_db):
        token_res = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "client_id": "test-client-id",
                "client_secret": "test-client-secret",
                "redirect_uri": "https://oauth-redirect.googleusercontent.com/r/test",
            },
        )
        assert token_res.status_code == 503
        assert "failed closed" in token_res.text

    client.app.state.settings.environment = "test"


def test_unknown_smart_home_command_is_not_published(client, auth_headers):
    import pytest
    from cloud_gateway.smart_home import UnsupportedCommandError, parse_execute_command

    published = []
    client.app.state.pubsub.publish_sync = lambda p: published.append(p) or "msg-unknown"

    with pytest.raises(UnsupportedCommandError):
        parse_execute_command(
            "action.devices.commands.TotallyFake",
            {},
            "Master Bedroom",
        )

    cmd_unknown = {
        "requestId": "exec-unknown",
        "inputs": [
            {
                "intent": "action.devices.EXECUTE",
                "payload": {
                    "commands": [
                        {
                            "devices": [{"id": "plex-jukebox:Master Bedroom"}],
                            "execution": [
                                {
                                    "command": "action.devices.commands.TotallyFake",
                                    "params": {},
                                }
                            ],
                        }
                    ]
                },
            }
        ],
    }
    res = client.post("/fulfillment", headers=auth_headers, json=cmd_unknown)
    assert res.status_code == 200
    data = res.json()
    cmd_result = data["payload"]["commands"][0]
    assert cmd_result["status"] == "ERROR"
    assert cmd_result["errorCode"] == "notSupported"
    assert published == []


def test_mixed_smart_home_commands_publish_nothing_on_unsupported(client, auth_headers):
    published = []
    client.app.state.pubsub.publish_sync = lambda p: published.append(p) or "msg-mixed"

    cmd_mixed = {
        "requestId": "exec-mixed",
        "inputs": [
            {
                "intent": "action.devices.EXECUTE",
                "payload": {
                    "commands": [
                        {
                            "devices": [{"id": "plex-jukebox:Master Bedroom"}],
                            "execution": [
                                {"command": "action.devices.commands.mediaPause", "params": {}},
                                {
                                    "command": "action.devices.commands.TotallyFake",
                                    "params": {},
                                },
                            ],
                        }
                    ]
                },
            }
        ],
    }
    res = client.post("/fulfillment", headers=auth_headers, json=cmd_mixed)
    assert res.status_code == 200
    data = res.json()
    cmd_result = data["payload"]["commands"][0]
    assert cmd_result["status"] == "ERROR"
    assert cmd_result["errorCode"] == "notSupported"
    assert published == []


def test_build_sync_devices_empty_when_all_disabled():
    import pytest
    from cloud_gateway.smart_home import build_sync_device, build_sync_devices

    empty = build_sync_devices({"speakers": []}, default_speaker="Master Bedroom")
    assert empty == []

    all_disabled = build_sync_devices(
        {
            "speakers": [
                {
                    "id": "plex-jukebox:Kitchen",
                    "name": "Plex",
                    "roomHint": "Kitchen",
                    "speaker_name": "Kitchen",
                    "enabled": False,
                }
            ]
        },
        default_speaker="Master Bedroom",
    )
    assert all_disabled == []
    with pytest.raises(ValueError, match="No enabled speakers"):
        build_sync_device({"speakers": []})

    enabled = build_sync_devices(
        {
            "speakers": [
                {
                    "id": "plex-jukebox:Kitchen",
                    "google_home_device_id": "plex-jukebox:Kitchen",
                    "google_home_name": "Kitchen Plex",
                    "nicknames": ["Kitchen Plex"],
                    "roomHint": "Kitchen",
                    "speaker_name": "Kitchen display",
                    "enabled": True,
                    "google_home_visible": True,
                },
                {
                    "id": "plex-jukebox:Living Room",
                    "google_home_device_id": "plex-jukebox:Living Room",
                    "google_home_name": "Living Room Plex",
                    "nicknames": ["Living Room Plex"],
                    "roomHint": "Living Room",
                    "speaker_name": "Living Room speaker",
                    "enabled": True,
                    "google_home_visible": True,
                },
                {
                    "id": "plex-jukebox:Bedroom",
                    "google_home_device_id": "plex-jukebox:Bedroom",
                    "google_home_name": "Bedroom Plex",
                    "roomHint": "Bedroom",
                    "speaker_name": "Bedroom Mini",
                    "enabled": True,
                    "google_home_visible": False,
                },
            ]
        },
        default_speaker="Master Bedroom",
    )
    assert len(enabled) == 2
    ids = {d["id"] for d in enabled}
    assert ids == {"plex-jukebox:Kitchen", "plex-jukebox:Living Room"}
    assert all(d["type"] == "action.devices.types.SPEAKER" for d in enabled)
    assert "action.devices.traits.MediaState" in enabled[0]["traits"]
    kitchen = next(d for d in enabled if d["id"] == "plex-jukebox:Kitchen")
    assert kitchen["name"]["name"] == "Kitchen Plex"
    assert "Plex" not in kitchen["name"]["nicknames"] or kitchen["name"]["nicknames"] == ["Kitchen Plex"]


def test_resolve_execute_speaker_rejects_unknown_and_hidden():
    from cloud_gateway.smart_home import resolve_execute_speaker

    catalog = {
        "speakers": [
            {
                "google_home_device_id": "plex-jukebox:Kitchen",
                "speaker_name": "Kitchen display",
                "enabled": True,
                "google_home_visible": True,
            },
            {
                "google_home_device_id": "plex-jukebox:Bedroom",
                "speaker_name": "Bedroom Mini",
                "enabled": True,
                "google_home_visible": False,
            },
            {
                "google_home_device_id": "plex-jukebox:Office",
                "speaker_name": "Office Mini",
                "enabled": False,
                "google_home_visible": True,
            },
        ]
    }
    speaker, err = resolve_execute_speaker(catalog, "plex-jukebox:Kitchen")
    assert speaker == "Kitchen display" and err is None
    speaker, err = resolve_execute_speaker(catalog, "plex-jukebox:Bedroom")
    assert speaker is None and err == "deviceNotFound"
    speaker, err = resolve_execute_speaker(catalog, "plex-jukebox:Office")
    assert speaker is None and err == "deviceTurnedOff"
    speaker, err = resolve_execute_speaker(catalog, "plex-jukebox:Missing")
    assert speaker is None and err == "deviceNotFound"
    speaker, err = resolve_execute_speaker(catalog, None)
    assert speaker is None and err == "deviceNotFound"


def test_fulfillment_multi_device_execute_routes_independently(client, auth_headers):
    published = []
    client.app.state.pubsub.publish_sync = lambda p: published.append(p) or "msg-multi"

    payload = {
        "requestId": "exec-multi",
        "inputs": [
            {
                "intent": "action.devices.EXECUTE",
                "payload": {
                    "commands": [
                        {
                            "devices": [
                                {"id": "plex-jukebox:Kitchen"},
                                {"id": "plex-jukebox:Master Bedroom"},
                            ],
                            "execution": [
                                {"command": "action.devices.commands.mediaNext", "params": {}}
                            ],
                        }
                    ]
                },
            }
        ],
    }
    res = client.post("/fulfillment", headers=auth_headers, json=payload)
    assert res.status_code == 200
    assert len(published) == 2
    speakers = {m["speaker"] for m in published}
    assert speakers == {"Kitchen", "Master Bedroom"}


def test_fulfillment_rejects_unknown_device_id(client, auth_headers):
    published = []
    client.app.state.pubsub.publish_sync = lambda p: published.append(p) or "msg-x"

    payload = {
        "requestId": "exec-unknown",
        "inputs": [
            {
                "intent": "action.devices.EXECUTE",
                "payload": {
                    "commands": [
                        {
                            "devices": [{"id": "plex-jukebox:DoesNotExist"}],
                            "execution": [
                                {"command": "action.devices.commands.mediaNext", "params": {}}
                            ],
                        }
                    ]
                },
            }
        ],
    }
    res = client.post("/fulfillment", headers=auth_headers, json=payload)
    assert res.status_code == 200
    body = res.json()
    assert published == []
    assert body["payload"]["commands"][0]["status"] == "ERROR"
    assert body["payload"]["commands"][0]["errorCode"] == "deviceNotFound"
