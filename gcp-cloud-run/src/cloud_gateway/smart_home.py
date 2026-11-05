import json
import logging
import re
from pathlib import Path
from typing import Any

from .config import Settings
from .pubsub_service import PubSubService

log = logging.getLogger(__name__)

DEVICE_ID = "plex-jukebox"


def slugify(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", text.strip().lower())
    return cleaned.strip("_")


def generate_voice_synonyms(name: str) -> list[str]:
    """Generate normalized phonetic/voice-friendly synonyms for speech recognition."""
    raw = name.strip()
    if not raw:
        return []

    synonyms: list[str] = [raw]

    def _add(val: str):
        v = re.sub(r"\s+", " ", val.strip())
        if v and v.casefold() not in {s.casefold() for s in synonyms}:
            synonyms.append(v)

    # Replace & with 'and'
    if "&" in raw:
        _add(raw.replace("&", " and "))
    if "+" in raw:
        _add(raw.replace("+", " and "))

    # Replace hyphens/slashes with spaces (e.g., "Wu-Tang Clan" -> "Wu Tang Clan")
    if any(ch in raw for ch in ("-", "/", "_", ".")):
        _add(re.sub(r"[-/_.]+", " ", raw))
        _add(re.sub(r"[-/_.]+", "", raw))

    # Strip all non-alphanumeric punctuation
    alnum = re.sub(r"[^a-zA-Z0-9\s]", " ", raw)
    _add(alnum)

    # Leading article stripping ("The Chemical Brothers" -> "Chemical Brothers", "A Tribe Called Quest" -> "Tribe Called Quest")
    for prefix in ("The ", "the ", "A ", "a "):
        if raw.startswith(prefix) and len(raw) > len(prefix) + 2:
            _add(raw[len(prefix):])

    # DJ variations ("DJ Shadow" -> "D J Shadow", "Deejay Shadow")
    if raw.upper().startswith("DJ "):
        rest = raw[3:].strip()
        _add(f"D J {rest}")
        _add(f"Deejay {rest}")

    # Radio suffix
    _add(f"{raw} Radio")

    return synonyms


def load_catalog(catalog_path: Path) -> dict[str, Any]:
    if catalog_path.exists():
        try:
            with open(catalog_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as exc:
            log.warning(f"Failed to read catalog from {catalog_path}: {exc}")

    # Fallback default catalog
    return {
        "genres": ["Rock", "Jazz", "Electronic", "Classical", "Pop", "Hip Hop"],
        "artists": ["Nirvana", "Pink Floyd", "Radiohead", "The Beatles", "Miles Davis"],
        "themes": ["Upbeat 90s rock", "Relaxing acoustic", "Late night jazz"],
        "inputs": ["Master Bedroom", "Living Room", "Office", "test speaker"],
    }


def build_sync_device(catalog_data: dict[str, Any]) -> dict[str, Any]:
    genres = catalog_data.get("genres", [])
    artists = catalog_data.get("artists", [])
    themes = catalog_data.get("themes", [])
    inputs = catalog_data.get("inputs", [])

    available_channels = []
    ch_num = 1
    for artist in artists:
        key = f"artist_{slugify(artist)}"
        available_channels.append({
            "key": key,
            "names": generate_voice_synonyms(artist),
            "number": str(ch_num),
        })
        ch_num += 1

    for theme in themes:
        key = f"theme_{slugify(theme)}"
        available_channels.append({
            "key": key,
            "names": generate_voice_synonyms(theme) + [f"{theme} Mix"],
            "number": str(ch_num),
        })
        ch_num += 1

    available_applications = []
    for genre in genres:
        key = f"app_genre_{slugify(genre)}"
        syns = generate_voice_synonyms(genre) + [f"{genre} Music"]
        available_applications.append({
            "key": key,
            "names": [
                {
                    "name_synonym": syns,
                    "lang": "en",
                }
            ],
        })

    for theme in themes:
        key = f"app_theme_{slugify(theme)}"
        available_applications.append({
            "key": key,
            "names": [
                {
                    "name_synonym": generate_voice_synonyms(theme),
                    "lang": "en",
                }
            ],
        })

    available_inputs = []
    for inp in inputs:
        key = f"input_{slugify(inp)}"
        available_inputs.append({
            "key": key,
            "names": [
                {
                    "name_synonym": [inp, f"{inp} Speaker", f"{inp} Display"],
                    "lang": "en",
                }
            ],
        })

    return {
        "id": DEVICE_ID,
        "type": "action.devices.types.SETTOP",
        "traits": [
            "action.devices.traits.AppSelector",
            "action.devices.traits.Channel",
            "action.devices.traits.InputSelector",
            "action.devices.traits.TransportControl",
            "action.devices.traits.Volume",
            "action.devices.traits.OnOff",
        ],
        "name": {
            "name": "Plex",
            "nicknames": ["Plex Jukebox", "Plex Radio"],
            "defaultNames": ["Plex"],
        },
        "willReportState": False,
        "deviceInfo": {
            "manufacturer": "Plex Bridge",
            "model": "Plex Jukebox",
            "hwVersion": "1.0",
            "swVersion": "2.0",
        },
        "attributes": {
            "availableApplications": available_applications,
            "availableChannels": available_channels,
            "availableInputs": available_inputs,
            "transportControlSupportedCommands": [
                "NEXT",
                "PREVIOUS",
                "PAUSE",
                "RESUME",
                "STOP",
            ],
            "volumeMaxLevel": 100,
            "volumeCanMuteAndUnmute": True,
            "levelStepSize": 5,
            "commandOnlyOnOff": False,
            "queryOnlyOnOff": False,
        },
    }


def parse_execute_command(command: str, params: dict[str, Any], default_speaker: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {}

    if command == "action.devices.commands.selectChannel":
        channel_name = params.get("channelName") or params.get("channelCode") or params.get("channelNumber")
        query = str(channel_name) if channel_name else "radio"
        payload = {"action": "play", "query": query}

    elif command == "action.devices.commands.appSearch":
        query = params.get("query") or "music"
        payload = {"action": "play", "query": str(query)}

    elif command == "action.devices.commands.appSelect":
        app_name = params.get("newApplicationName") or params.get("newApplication") or "music"
        payload = {"action": "play", "query": str(app_name)}

    elif command == "action.devices.commands.setInput":
        speaker = params.get("newInput")
        payload = {"action": "resume", "speaker": speaker}

    elif command == "action.devices.commands.mediaNext":
        payload = {"action": "next"}

    elif command in ("action.devices.commands.mediaPrevious", "action.devices.commands.mediaPrev"):
        payload = {"action": "next"}

    elif command == "action.devices.commands.mediaPause":
        payload = {"action": "pause"}

    elif command == "action.devices.commands.mediaResume":
        payload = {"action": "resume"}

    elif command == "action.devices.commands.mediaStop":
        payload = {"action": "stop"}

    elif command == "action.devices.commands.OnOff":
        is_on = params.get("on", True)
        payload = {"action": "resume" if is_on else "pause"}

    elif command == "action.devices.commands.setVolume":
        vol = params.get("volumeLevel", 20)
        vol_float = round(max(0.0, min(100.0, float(vol))) / 100.0, 2)
        payload = {"action": "volume", "volume": vol_float}

    elif command == "action.devices.commands.volumeRelative":
        rel = params.get("volumeRelativeLevel", 5)
        rel_float = round(float(rel) / 100.0, 2)
        payload = {"action": "volume", "relative_volume": rel_float}

    else:
        log.warning(f"Unrecognized Google Smart Home command: {command}")
        payload = {"action": "resume"}

    if "speaker" not in payload and default_speaker:
        payload["speaker"] = default_speaker

    return payload


async def handle_smart_home_request(
    body: dict[str, Any],
    settings: Settings,
    pubsub_service: PubSubService,
) -> dict[str, Any]:
    request_id = body.get("requestId", "")
    inputs = body.get("inputs", [])
    if not inputs:
        return {"requestId": request_id, "payload": {"error": "Missing inputs"}}

    intent = inputs[0].get("intent")
    log.info(f"Handling Google Smart Home intent: {intent} (requestId={request_id})")

    if intent == "action.devices.SYNC":
        catalog_path = settings.get_catalog_file()
        catalog_data = load_catalog(catalog_path)
        device = build_sync_device(catalog_data)
        return {
            "requestId": request_id,
            "payload": {
                "agentUserId": "plex-personal-user",
                "devices": [device],
            },
        }

    elif intent == "action.devices.QUERY":
        return {
            "requestId": request_id,
            "payload": {
                "devices": {
                    DEVICE_ID: {
                        "online": True,
                        "status": "SUCCESS",
                        "playbackState": "PLAYING",
                        "on": True,
                        "currentVolume": 20,
                        "isMuted": False,
                    }
                }
            },
        }

    elif intent == "action.devices.EXECUTE":
        commands_input = inputs[0].get("payload", {}).get("commands", [])
        device_ids: list[str] = []

        for cmd_entry in commands_input:
            devices = cmd_entry.get("devices", [])
            for d in devices:
                dev_id = d.get("id")
                if dev_id and dev_id not in device_ids:
                    device_ids.append(dev_id)

            executions = cmd_entry.get("execution", [])
            for exec_item in executions:
                cmd_name = exec_item.get("command", "")
                params = exec_item.get("params", {})
                pubsub_payload = parse_execute_command(
                    cmd_name,
                    params,
                    settings.default_speaker_name,
                )
                log.info(f"Publishing execute payload: {pubsub_payload}")
                try:
                    await pubsub_service.publish(pubsub_payload)
                except Exception as exc:
                    log.exception(f"Failed to publish execution to Pub/Sub: {exc}")

        if not device_ids:
            device_ids = [DEVICE_ID]

        return {
            "requestId": request_id,
            "payload": {
                "commands": [
                    {
                        "ids": device_ids,
                        "status": "SUCCESS",
                        "states": {
                            "online": True,
                            "playbackState": "PLAYING",
                            "on": True,
                        },
                    }
                ]
            },
        }

    else:
        log.warning(f"Unknown Google Smart Home intent: {intent}")
        return {
            "requestId": request_id,
            "payload": {
                "errorCode": "notSupported",
            },
        }
