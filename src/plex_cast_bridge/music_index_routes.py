"""Local-only optional music index controls."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import music_index, music_index_scan

router = APIRouter(prefix='/api/music-index')


class IndexSettings(BaseModel):
    indexing_enabled: bool | None = None
    schedule: Literal['manual', 'daily', 'weekly'] | None = None
    ai_enabled: bool | None = None


@router.get('')
async def index_status():
    def read():
        music_index_scan.recover_interrupted()
        return {'settings': music_index.get_settings(), **music_index.status()}
    return await run_in_threadpool(read)


@router.patch('/settings')
async def index_settings(body: IndexSettings, request: Request):
    patch = body.model_dump(exclude_none=True)
    if patch.get('ai_enabled'):
        key = getattr(getattr(request.app.state, 'settings', None), 'gemini_api_key', None)
        if not key or not key.get_secret_value():
            raise HTTPException(400, 'Configure a Gemini API key before enabling AI-assisted mixes')
    try:
        result = await run_in_threadpool(music_index.update_settings, patch)
        if patch.get('indexing_enabled') is False:
            await run_in_threadpool(music_index_scan.cancel)
        return result
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post('/scan')
async def start_scan(request: Request):
    try:
        await run_in_threadpool(music_index_scan.start, getattr(request.app.state, 'plex', None))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {'status': 'started'}


@router.post('/cancel')
async def cancel_scan():
    await run_in_threadpool(music_index_scan.cancel)
    return {'status': 'cancellation_requested'}
