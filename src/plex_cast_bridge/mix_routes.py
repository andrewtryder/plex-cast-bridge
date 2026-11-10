"""Preview and manage bridge-local mixes without Plex write permissions."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from plexapi.exceptions import NotFound
from starlette.concurrency import run_in_threadpool

from . import mix_ai, mixes, music_index

router = APIRouter(prefix='/api/mixes')


class Preview(BaseModel):
    prompt: str = Field(min_length=1, max_length=300)
    mode: Literal['artist', 'genre', 'mood', 'decade', 'deep_cuts']
    use_ai: bool = False


class Save(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    track_ids: list[str] = Field(min_length=1, max_length=100)
    mode: Literal['artist', 'genre', 'mood', 'decade', 'deep_cuts']
    use_ai: bool = False


class Edit(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    track_ids: list[str] | None = Field(default=None, min_length=1, max_length=100)


class Play(BaseModel):
    speaker: str = Field(min_length=1, max_length=200)


class PlayPreview(Play):
    track_ids: list[str] = Field(min_length=1, max_length=100)


def _error(exc: ValueError):
    raise HTTPException(400, str(exc)) from exc


@router.post('/preview')
async def preview(body: Preview, request: Request):
    try:
        result = await run_in_threadpool(mixes.preview, body.prompt, body.mode, 50 if body.use_ai else 25)
        if body.use_ai:
            settings = await run_in_threadpool(music_index.get_settings)
            if not settings['ai_enabled']:
                raise ValueError('Enable AI-assisted mix generation first')
            key = getattr(request.app.state.settings, 'gemini_api_key', None)
            result['tracks'] = await run_in_threadpool(mix_ai.choose, result['tracks'], body.mode,
                                                       key.get_secret_value() if key else '')
            result['duration_ms'] = sum(t['duration_ms'] or 0 for t in result['tracks'])
        return result
    except ValueError as exc:
        _error(exc)


@router.get('')
async def list_saved():
    try:
        return {'mixes': await run_in_threadpool(mixes.list_mixes)}
    except ValueError as exc:
        _error(exc)


@router.post('')
async def save(body: Save):
    try:
        mix_id = await run_in_threadpool(mixes.save, body.name, body.track_ids,
                                         {'mode': body.mode, 'generator': 'ai-assisted' if body.use_ai else 'metadata'})
        return await run_in_threadpool(mixes.get, mix_id)
    except ValueError as exc:
        _error(exc)


@router.get('/{mix_id}')
async def get(mix_id: int):
    try:
        return await run_in_threadpool(mixes.get, mix_id)
    except ValueError as exc:
        _error(exc)


@router.patch('/{mix_id}')
async def update(mix_id: int, body: Edit):
    try:
        await run_in_threadpool(mixes.update, mix_id, body.name, body.track_ids)
        return await run_in_threadpool(mixes.get, mix_id)
    except ValueError as exc:
        _error(exc)


@router.delete('/{mix_id}')
async def delete(mix_id: int):
    try:
        await run_in_threadpool(mixes.delete, mix_id)
        return {'status': 'deleted'}
    except ValueError as exc:
        _error(exc)


def _play_mix(mix: dict, speaker_name: str, request: Request) -> dict:
    plex = getattr(request.app.state, 'plex', None)
    if plex is None:
        raise ValueError('Connect Plex before playing')
    if str(plex.server.machineIdentifier) != mix['server_id'] or str(plex.library.key) != mix['library_id']:
        raise ValueError('This mix belongs to a different Plex server or library')
    tracks, missing = [], []
    for entry in mix['tracks']:
        if entry['missing']:
            missing.append(entry['rating_key'])
            continue
        try:
            tracks.append(plex.server.fetchItem(int(entry['rating_key'])))
        except NotFound:
            missing.append(entry['rating_key'])
    if not tracks:
        raise ValueError('No tracks from this mix are available in Plex')
    speaker = request.app.state.cast.get(speaker_name)
    speaker.replace(tracks, repeat=False)
    return {'playback': speaker.status(), 'missing_track_ids': missing}


@router.post('/play-preview')
async def play_preview(body: PlayPreview, request: Request):
    try:
        return await run_in_threadpool(lambda: _play_mix(mixes.validate_preview(body.track_ids), body.speaker, request))
    except ValueError as exc:
        _error(exc)


@router.post('/{mix_id}/play')
async def play(mix_id: int, body: Play, request: Request):
    try:
        return await run_in_threadpool(lambda: _play_mix(mixes.get(mix_id), body.speaker, request))
    except ValueError as exc:
        _error(exc)
