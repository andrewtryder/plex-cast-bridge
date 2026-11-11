"""Pydantic request bodies for the local control API."""

from typing import Literal

from pydantic import BaseModel, Field, StrictBool, model_validator


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
    action: Literal["pause", "resume", "next", "previous", "stop", "volume", "mute"]
    speaker: str | None = None
    volume: float | None = Field(default=None, ge=0, le=1)
    relative_volume: float | None = Field(default=None, ge=-1, le=1)
    mute: StrictBool | None = None

    @model_validator(mode="after")
    def validate_action_parameters(self):
        has_volume = self.volume is not None
        has_relative_volume = self.relative_volume is not None
        if self.action == "mute":
            if self.mute is None or has_volume or has_relative_volume:
                raise ValueError("mute action requires a boolean mute value only")
        elif self.action == "volume":
            if has_volume == has_relative_volume or self.mute is not None:
                raise ValueError("volume action requires exactly one volume value")
        elif self.mute is not None or has_volume or has_relative_volume:
            raise ValueError("volume parameters are only valid for volume or mute actions")
        return self


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
