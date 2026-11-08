"""Pydantic request bodies for the local control API."""

from typing import Literal

from pydantic import BaseModel, Field


class Command(BaseModel):
    query: str = Field(min_length=1)
    speaker: str | None = None


class SpeakerTest(BaseModel):
    speaker: str | None = None
    query: str | None = None


class SpeakerToggle(BaseModel):
    speaker: str = Field(min_length=1)
    enabled: bool


class Transport(BaseModel):
    action: Literal["pause", "resume", "next", "previous", "stop", "volume"]
    speaker: str | None = None
    volume: float | None = Field(default=None, ge=0, le=1)
    relative_volume: float | None = Field(default=None, ge=-1, le=1)


class PlexSelect(BaseModel):
    server_id: str = Field(min_length=1)
    section: str = Field(min_length=1)
