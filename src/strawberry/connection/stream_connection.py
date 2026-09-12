"""
Strawberry Stream Connection
============================

A stream connection is practically a voice connection
that depends upon two other gateway events.

- STREAM_CREATE

Provides all the necessary information to start a stream connection.
The session_id must be derived from the voice connection where
the stream connection takes place.

- STREAM_SERVER_UPDATE

Provides the endpoint and the token to connect to the voice server.

Similar to the voice connection, the stream connection also
has an underlying UDP connection to send and receive stream packets.

The audio sent in the stream is not the same as the audio sent
in the voice connection.
"""

import base64
import typing

from .voice_connection import VoiceConnection, VoiceOpCodes

if typing.TYPE_CHECKING:
    from strawberry.gateway import DiscordGateway


class StreamConnection(VoiceConnection):
    def __init__(self, *args, stream_key: str, rtc_server_id: str, **kwargs):
        super().__init__(*args, **kwargs)

        self.logger = self.logger.getChild("stream")
        self.stream_key = stream_key
        self.server_id = rtc_server_id

    async def set_speaking(self, speaking: bool):
        self.ensure_ready()
        return await self.ws.send_json(
            {
                "op": VoiceOpCodes.SPEAKING,
                "d": {
                    "speaking": 2 if speaking else 0,
                    "delay": 0,
                    "ssrc": self.ssrc,
                },
            }
        )

    async def set_preview(
        self,
        gateway: "DiscordGateway",
        preview: bytes,
        preview_type: str = "image/jpeg",
    ):
        if self.stream_key is None:
            raise ValueError("Stream key for the stream connection is not set")

        async with self.session.post(
            f"{gateway.DISCORD_API_ENDPOINT}/streams/{self.stream_key}/preview",
            headers={
                "Authorization": gateway.token,
            },
            json={
                "thumbnail": f"data:{preview_type};base64,{base64.b64encode(preview).decode('utf-8')}"
            },
        ) as response:
            return response.status == 204

    @classmethod
    def from_voice_connection(
        cls,
        voice_conn: VoiceConnection,
        *,
        stream_key: str,
        rtc_server_id: str,
        rtc_server_endpoint: str,
        rtc_server_token: str,
    ):
        return cls(
            voice_conn.session,
            channel_id=voice_conn.channel_id,
            user_id=voice_conn.user_id,
            session_id=voice_conn.session_id,
            guild_id=voice_conn.guild_id,
            stream_key=stream_key,
            endpoint=rtc_server_endpoint,
            token=rtc_server_token,
            rtc_server_id=rtc_server_id,
        )
