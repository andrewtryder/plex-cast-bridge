import json
import logging
import re
from pathlib import Path
from typing import Any

from .config import Settings
from .pubsub_service import PubSubService

log = logging.getLogger(__name__)

DEVICE_ID = "plex-jukebox"


class UnsupportedCommandError(ValueError):
    """Raised when Google sends a Smart Home command this bridge does not handle."""

    def __init__(self, command: str):
        self.command = command
        super().__init__(f"Unsupported Smart Home command: {command}")


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
        "speakers": [
            {
                "id": f"{DEVICE_ID}:Master Bedroom",
                "name": "Plex",
                "nicknames": ["Master Bedroom Plex", "Plex", "Plex Cast Bridge"],
                "roomHint": "Master Bedroom",
                "speaker_name": "Master Bedroom",
                "enabled": True,
            }
        ],
    }


def _build_shared_attributes(catalog_data: dict[str, Any]) -> dict[str, Any]:
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
        "commandOnlyOnOff": True,
        "queryOnlyOnOff": False,
        "commandOnlyVolume": True,
        "commandOnlyChannel": True,
        "commandOnlyInputSelector": True,
        "commandOnlyAppSelector": True,
    }


def build_sync_devices(catalog_data: dict[str, Any], default_speaker: str = "Master Bedroom") -> list[dict[str, Any]]:
    shared_attributes = _build_shared_attributes(catalog_data)
    raw_speakers = catalog_data.get("speakers") or []

    # Filter to enabled speakers. An empty / all-disabled catalog yields no devices
    # so dashboard disables are honored (no synthetic fallback speaker).
    enabled_speakers = [s for s in raw_speakers if s.get("enabled", True)]
    if not enabled_speakers:
        return []

    devices: list[dict[str, Any]] = []
    for spk in enabled_speakers:
        speaker_name = spk.get("speaker_name") or spk.get("roomHint") or default_speaker or "Master Bedroom"
        room_hint = spk.get("roomHint") or speaker_name
        dev_id = spk.get("id") or f"{DEVICE_ID}:{speaker_name}"
        dev_name = spk.get("name") or "Plex"
        nicknames = spk.get("nicknames") or [
            f"{room_hint} Plex",
            "Plex",
            "Plex Cast Bridge",
            "Plex Radio",
        ]

        devices.append({
            "id": dev_id,
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
                "name": dev_name,
                "nicknames": nicknames,
                "defaultNames": ["Plex"],
            },
            "roomHint": room_hint,
            "willReportState": False,
            "deviceInfo": {
                "manufacturer": "Plex Cast Bridge",
                "model": "Plex Cast Bridge",
                "hwVersion": "1.0",
                "swVersion": "2.0",
            },
            "attributes": shared_attributes,
        })

    return devices


def build_sync_device(catalog_data: dict[str, Any]) -> dict[str, Any]:
    """Backwards-compatible helper returning the primary virtual device."""
    devices = build_sync_devices(catalog_data)
    if not devices:
        raise ValueError("No enabled speakers in catalog")
    return devices[0]


def extract_speaker_from_device_id(device_id: str | None, default_speaker: str | None) -> str | None:
    if device_id and ":" in device_id:
        _, speaker_name = device_id.split(":", 1)
        if speaker_name.strip():
            return speaker_name.strip()
    return default_speaker


def resolve_input_key(raw_input: str | None, catalog_inputs: list[str]) -> str | None:
    if not raw_input:
        return None
    val = raw_input.strip()
    if val.startswith("input_"):
        slug = val[len("input_"):]
        for inp in catalog_inputs:
            if slugify(inp) == slug:
                return inp
        return slug.replace("_", " ").title()
    return val


def resolve_app_key(raw_app: str | None) -> str:
    if not raw_app:
        return "music"
    val = raw_app.strip()
    if val.startswith("app_genre_"):
        return val[len("app_genre_"):].replace("_", " ").title()
    if val.startswith("app_theme_"):
        return val[len("app_theme_"):].replace("_", " ").title()
    return val


def parse_execute_command(
    command: str,
    params: dict[str, Any],
    target_speaker: str | None,
    catalog_inputs: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    cmd_lower = command.lower().strip()
    inputs_list = catalog_inputs or []

    if cmd_lower == "action.devices.commands.selectchannel":
        channel_name = params.get("channelName") or params.get("channelCode") or params.get("channelNumber")
        query = str(channel_name) if channel_name else "radio"
        if query.startswith("artist_"):
            query = query[len("artist_"):].replace("_", " ").title()
        elif query.startswith("theme_"):
            query = query[len("theme_"):].replace("_", " ").title()
        payload = {"action": "play", "query": query}

    elif cmd_lower == "action.devices.commands.appsearch":
        query = params.get("query") or params.get("category") or params.get("application") or "music"
        payload = {"action": "play", "query": str(query)}

    elif cmd_lower == "action.devices.commands.appselect":
        raw_app = params.get("newApplicationName") or params.get("newApplication") or "music"
        query = resolve_app_key(str(raw_app))
        payload = {"action": "play", "query": query}

    elif cmd_lower in ("action.devices.commands.setinput",):
        raw_input = params.get("newInput")
        speaker = resolve_input_key(raw_input, inputs_list) or target_speaker
        payload = {"action": "resume", "speaker": speaker}

    elif cmd_lower == "action.devices.commands.medianext":
        payload = {"action": "next"}

    elif cmd_lower in ("action.devices.commands.mediaprevious", "action.devices.commands.mediaprev"):
        payload = {"action": "previous"}

    elif cmd_lower == "action.devices.commands.mediapause":
        payload = {"action": "pause"}

    elif cmd_lower == "action.devices.commands.mediaresume":
        payload = {"action": "resume"}

    elif cmd_lower == "action.devices.commands.mediastop":
        payload = {"action": "stop"}

    elif cmd_lower == "action.devices.commands.onoff":
        is_on = params.get("on", True)
        payload = {"action": "resume" if is_on else "pause"}

    elif cmd_lower == "action.devices.commands.setvolume":
        vol = params.get("volumeLevel", 20)
        vol_float = round(max(0.0, min(100.0, float(vol))) / 100.0, 2)
        payload = {"action": "volume", "volume": vol_float}

    elif cmd_lower == "action.devices.commands.volumerelative":
        rel = params.get("volumeRelativeLevel", 5)
        rel_float = round(float(rel) / 100.0, 2)
        payload = {"action": "volume", "relative_volume": rel_float}

    else:
        log.warning(f"Unrecognized Google Smart Home command: {command}")
        raise UnsupportedCommandError(command)

    if "speaker" not in payload and target_speaker:
        payload["speaker"] = target_speaker

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
        devices = build_sync_devices(catalog_data, default_speaker=settings.default_speaker_name)
        return {
            "requestId": request_id,
            "payload": {
                "agentUserId": "plex-personal-user",
                "devices": devices,
            },
        }

    elif intent == "action.devices.QUERY":
        query_devices = inputs[0].get("payload", {}).get("devices", [])
        requested_ids = [d.get("id") for d in query_devices if d.get("id")]
        if not requested_ids:
            requested_ids = [DEVICE_ID]

        device_states = {
            dev_id: {
                "online": True,
                "status": "SUCCESS",
            }
            for dev_id in requested_ids
        }
        return {
            "requestId": request_id,
            "payload": {
                "devices": device_states,
            },
        }

    elif intent == "action.devices.EXECUTE":
        catalog_path = settings.get_catalog_file()
        catalog_data = load_catalog(catalog_path)
        catalog_inputs = catalog_data.get("inputs", [])

        commands_input = inputs[0].get("payload", {}).get("commands", [])
        success_ids: list[str] = []
        error_commands: list[dict[str, Any]] = []

        for cmd_entry in commands_input:
            devices = cmd_entry.get("devices", [])
            executions = cmd_entry.get("execution", [])

            target_dev_ids = [d.get("id") for d in devices if d.get("id")] or [DEVICE_ID]
            for dev_id in target_dev_ids:
                target_speaker = extract_speaker_from_device_id(dev_id, settings.default_speaker_name)
                # Validate the full execution group before publishing anything so a
                # trailing unsupported command cannot partially apply earlier actions.
                parsed_payloads: list[dict[str, Any]] = []
                unsupported_command: str | None = None
                for exec_item in executions:
                    cmd_name = exec_item.get("command", "")
                    params = exec_item.get("params", {})
                    try:
                        parsed_payloads.append(
                            parse_execute_command(
                                cmd_name,
                                params,
                                target_speaker,
                                catalog_inputs,
                            )
                        )
                    except UnsupportedCommandError:
                        unsupported_command = cmd_name
                        break

                if unsupported_command is not None:
                    log.warning(
                        "Rejecting unsupported Smart Home command '%s' for device '%s' "
                        "(no commands published for this device)",
                        unsupported_command,
                        dev_id,
                    )
                    error_commands.append({
                        "ids": [dev_id],
                        "status": "ERROR",
                        "errorCode": "notSupported",
                    })
                    continue

                dev_success = True
                error_code = "transientError"
                for pubsub_payload in parsed_payloads:
                    log.info(f"Publishing execute payload for device '{dev_id}': {pubsub_payload}")
                    try:
                        await pubsub_service.publish(pubsub_payload)
                    except Exception as exc:
                        log.exception(f"Failed to publish execution to Pub/Sub: {exc}")
                        dev_success = False
                        error_code = "transientError"
                        break

                if dev_success:
                    if dev_id not in success_ids:
                        success_ids.append(dev_id)
                else:
                    error_commands.append({
                        "ids": [dev_id],
                        "status": "ERROR",
                        "errorCode": error_code,
                    })

        response_commands = []
        if success_ids:
            response_commands.append({
                "ids": success_ids,
                "status": "SUCCESS",
            })
        response_commands.extend(error_commands)
        if not response_commands:
            response_commands = [{"ids": [DEVICE_ID], "status": "SUCCESS"}]

        return {
            "requestId": request_id,
            "payload": {
                "commands": response_commands,
            },
        }

    elif intent == "action.devices.DISCONNECT":
        # Google sends DISCONNECT when the user unlinks the integration.
        # Access/refresh tokens are self-contained JWTs; acknowledge unlink so
        # Home Graph clears the agent association. No durable cloud session store.
        log.info("Google Home DISCONNECT for requestId=%s; acknowledging unlink", request_id)
        return {"requestId": request_id}

    else:
        log.warning(f"Unknown Google Smart Home intent: {intent}")
        return {
            "requestId": request_id,
            "payload": {
                "errorCode": "notSupported",
            },
        }
