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

from .voice_connection import VoiceConnection, VoiceOpCodes


class StreamConnection(VoiceConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.logger = self.logger.getChild("stream")
        self.stream_key = None

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
