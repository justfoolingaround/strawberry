"""
Gateway-side voice models.

Only the pydantic models of the gateway's VOICE_STATE_UPDATE /
VOICE_SERVER_UPDATE payloads. The actual media connection lives in
strawberry.connection (UDP + DAVE, see stream_connection.py).
"""

import typing

from pydantic import BaseModel, ConfigDict


class VoiceState(BaseModel):
    model_config: typing.ClassVar[ConfigDict] = ConfigDict(slots=True)  # pyright: ignore[reportCallIssue]

    member: dict[str, typing.Any]
    user_id: str
    # Discord sends a null channel_id when the user leaves the channel
    channel_id: str | None
    guild_id: str | None

    suppress: bool

    session_id: str

    self_video: bool
    self_mute: bool
    self_deaf: bool

    request_to_speak_timestamp: int | None
    mute: bool
    deaf: bool
    connected_at: int


class VoiceServer(BaseModel):
    model_config: typing.ClassVar[ConfigDict] = ConfigDict(slots=True)  # pyright: ignore[reportCallIssue]

    token: str
    guild_id: str
    endpoint: str


class VoiceConnection(BaseModel):
    model_config: typing.ClassVar[ConfigDict] = ConfigDict(slots=True)  # pyright: ignore[reportCallIssue]

    state: VoiceState
    server: VoiceServer
