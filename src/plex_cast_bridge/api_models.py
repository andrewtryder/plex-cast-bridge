"""Pydantic request bodies for the local control API."""

from typing import Literal

from pydantic import BaseModel, Field


class Command(BaseModel):
    device_id: str | None = None
    query: str = Field(min_length=1)
    speaker: str | None = None


class SpeakerTest(BaseModel):
    speaker: str | None = None
    query: str | None = None


class SpeakerToggle(BaseModel):
    speaker: str = Field(min_length=1)
    enabled: bool


class SpeakerPatch(BaseModel):
    enabled: bool | None = None
    google_home_visible: bool | None = None
    google_home_name: str | None = Field(default=None, min_length=1, max_length=80)
    room_hint: str | None = Field(default=None, max_length=80)


class Transport(BaseModel):
    device_id: str | None = None
    action: Literal["pause", "resume", "next", "previous", "stop", "volume"]
    speaker: str | None = None
    volume: float | None = Field(default=None, ge=0, le=1)
    relative_volume: float | None = Field(default=None, ge=-1, le=1)


class PlexSelect(BaseModel):
    server_id: str = Field(min_length=1)
    section: str = Field(min_length=1)


class QueueMutate(BaseModel):
    speaker: str | None = None
    action: Literal["remove", "move", "clear", "play_next", "add_end"]
    index: int | None = None
    to_index: int | None = None
    rating_key: str | int | None = None


class LibraryPlay(BaseModel):
    rating_key: str | int
    speaker: str | None = None
    mode: Literal["replace", "play_next", "add_end"] = "replace"
