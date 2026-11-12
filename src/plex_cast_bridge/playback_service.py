"""Playback and transport orchestration for the local bridge."""

from starlette.concurrency import run_in_threadpool

from .api_models import Command, Transport
from .cast_manager import CastManager
from .config import Settings
from .plex_service import PlexService


async def play(
    *,
    dj,
    plex: PlexService,
    cast: CastManager,
    settings: Settings,
    command: Command,
):
    def execute():
        intent = dj.parse(command.query, plex.catalog)
        tracks = plex.resolve(intent)
        if not tracks:
            raise ValueError("No matching tracks found in the Plex library")
        name = command.speaker or intent.target_speaker or settings.default_speaker_name
        speaker = cast.get_device(command.device_id) if command.device_id else cast.get(name)
        speaker.replace(tracks, intent.mode in ("artist", "artist_radio", "theme_mix"))
        return {"intent": intent.model_dump(), "playback": speaker.status()}

    return await run_in_threadpool(execute)


async def transport(
    *,
    cast: CastManager,
    settings: Settings,
    command: Transport,
):
    def execute():
        speaker = (cast.get_device(command.device_id) if command.device_id
                   else cast.get(command.speaker or settings.default_speaker_name))
        if command.action == "next":
            speaker.skip()
        elif command.action == "previous":
            if hasattr(speaker, "previous"):
                speaker.previous()
            else:
                speaker.skip()
        elif command.action == "stop":
            speaker.stop()
        elif command.action in ("pause", "resume"):
            getattr(speaker, command.action)()
        elif command.action == "mute":
            if command.mute is None:
                raise ValueError("Mute action requires a boolean mute value")
            speaker.mute(command.mute)
        if command.volume is not None:
            speaker.set_volume(command.volume)
        elif command.relative_volume is not None and hasattr(speaker, "adjust_volume"):
            speaker.adjust_volume(command.relative_volume)
        return speaker.status()

    return await run_in_threadpool(execute)
