"""Optional Gemini selection from a bounded, locally chosen candidate set."""
from __future__ import annotations

import json
import re
from typing import Literal

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from .ai_router import CANDIDATE_MODELS

MAX_CANDIDATES = 50


class Selection(BaseModel):
    track_ids: list[str] = Field(max_length=MAX_CANDIDATES)


def _safe_text(value: object) -> str:
    text = str(value or '')[:160]
    text = re.sub(r'(?i)\b(?:https?://|www\.)\S+', '[redacted]', text)
    text = re.sub(r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b', '[redacted]', text)
    text = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', '[redacted]', text)
    return re.sub(r'(?i)\b[\w.-]+\.(?:local|lan|internal)\b', '[redacted]', text)


def choose(candidates: list[dict], mode: Literal['artist', 'genre', 'mood', 'decade', 'deep_cuts'], api_key: str) -> list[dict]:
    if not api_key:
        raise ValueError('Configure a Gemini API key before using AI-assisted mixes')
    candidates = candidates[:MAX_CANDIDATES]
    if not candidates:
        return []
    allowed = {str(track['rating_key']): track for track in candidates}
    metadata = [{'id': str(t['rating_key']), 'title': _safe_text(t.get('title')),
                 'artist': _safe_text(t.get('artist')), 'album': _safe_text(t.get('album')),
                 'year': t.get('year'), 'play_count': t.get('play_count')} for t in candidates]
    # The user's free-form request, Plex address, credentials and audio are never sent.
    contents = ('Choose and order music tracks for the supplied mix type. '
                'Return only IDs from the candidate list, without duplicates. '
                f'Mix type: {mode}. Candidates: {json.dumps(metadata, ensure_ascii=False)}')
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=CANDIDATE_MODELS[0], contents=contents,
        config=types.GenerateContentConfig(response_mime_type='application/json', response_schema=Selection,
                                           automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)),
    )
    selection = Selection.model_validate(response.parsed) if response.parsed is not None else Selection.model_validate_json(response.text)
    ids = selection.track_ids
    if not ids or len(set(ids)) != len(ids) or any(track_id not in allowed for track_id in ids):
        raise ValueError('AI returned an invalid track selection; try a metadata-only mix')
    return [allowed[track_id] for track_id in ids]
