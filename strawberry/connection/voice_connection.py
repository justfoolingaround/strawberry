"""
Strawberry Voice Connection
===========================

Establishes a proper websocket connection to Discord
according to the information provided by a client's
gateway.

The gateway provides such information whenever a
voice state update is requested by our client.

SVC depends upon two gateway events:

- VOICE_STATE_UPDATE

This event provides necessary session information.
This will be used to create a class instance of SVC.

- VOICE_SERVER_UPDATE

This event provides necessary server information.
This will be used to "prepare" the SVC instance.

Only after the preparation, the SVC instance can
be started (i.e. the websocket connection can be
established).

The underlying UDP connection is responsible
for the voice channel audio and video transmission.
"""


import asyncio
import enum
import logging
import socket
import struct
import typing

import aiohttp
import nacl.secret
import nacl.utils

from strawberry.utils import checked_add

from ..packetizers import audio_packetizer, h264_packetizer


class VoiceOpCodes(enum.IntEnum):
    IDENTIFY = 0
    SELECT_PROTOCOL = 1
    READY = 2
    HEARTBEAT = 3
    SELECT_PROTOCOL_ACK = 4
    SPEAKING = 5
    HEARTBEAT_ACK = 6
    RESUME = 7
    HELLO = 8
    RESUMED = 9
    VIDEO = 12
    CLIENT_DISCONNECT = 13
    SESSION_UPDATE = 14
    MEDIA_SINK_WANTS = 15
    VOICE_BACKEND_VERSION = 16
    CHANNEL_OPTIONS_UPDATE = 17
    FLAGS = 18
    SPEED_TEST = 19
    PLATFORM = 20


class VoiceConnection:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        channel_id: str,
        user_id: str,
        session_id: str,
        endpoint: str,
        token: str,
        guild_id: "str | None" = None,
        encryption_mode: str = "xsalsa20_poly1305_lite",
        audio_packetizer=audio_packetizer.AudioPacketizer,
        video_packetizer=h264_packetizer.H264Packetizer,
    ):
        self.logger = logging.getLogger("voice_connection")

        self.session: aiohttp.ClientSession = session
        self.loop = asyncio.get_event_loop()
        self.encryption_mode = encryption_mode

        self.last_heartbeat_at: int = 0

        self.udp_connection = UDPConnection(
            self, audio_packetizer=audio_packetizer, video_packetizer=video_packetizer
        )

        self.guild_id = guild_id
        self.channel_id = channel_id

        self.server_id = self.guild_id or self.channel_id

        self.user_id = user_id
        self.session_id = session_id
        self.endpoint = endpoint
        self.token = token

        self.ws: typing.Optional[aiohttp.ClientWebSocketResponse] = None

        self.our_ip: typing.Optional[str] = None
        self.our_port: typing.Optional[int] = None

        self.ip: typing.Optional[str] = None
        self.port: typing.Optional[int] = None

        self.ssrc: typing.Optional[int] = None
        self.video_ssrc: typing.Optional[int] = None
        self.rtx_ssrc: typing.Optional[int] = None

        self.secret_key: typing.Optional[str] = None
        self.ws_handler_task: typing.Optional[asyncio.Task] = None

    @property
    def own_identity(self):
        if not (self.our_ip or self.our_port):
            return None

        return f"{self.our_ip}:{self.our_port}"

    @own_identity.setter
    def own_identity(self, value: tuple[str, int]):
        self.our_ip, self.our_port = value

    def set_server_address(self, ip: str, port: int):
        self.ip = ip
        self.port = port

    def set_ssrc(self, ssrc: int):
        self.ssrc = ssrc
        self.video_ssrc = ssrc + 1
        self.rtx_ssrc = ssrc + 2

        self.udp_connection.set_ssrc(self.ssrc, self.video_ssrc)

    @property
    def is_ready(self):
        return all((self.endpoint, self.token, self.ip, self.port, self.ssrc))

    def ensure_ready(self):
        if not self.is_ready:
            raise RuntimeError("Voice connection is not ready yet.")

    async def setup_heartbeat(self, interval):
        self.logger.debug(f"Setting up heartbeat with interval {interval}ms.")

        while self.ws is not None and not self.ws.closed:
            await asyncio.sleep(interval / 1000)

            try:
                self.last_heartbeat_at = self.loop.time()
                self.logger.debug("Sending heartbeat.")
                await self.ws.send_json({"op": VoiceOpCodes.HEARTBEAT, "d": 1337})
            except ConnectionResetError:
                return await self.ws.close()

    async def set_video_state(
        self,
        state: bool,
        width: int = 1280,
        height: int = 720,
        framerate: int = 30,
        bitrate: int = 25 * 1024,
    ):
        self.ensure_ready()

        return await self.ws.send_json(
            {
                "op": VoiceOpCodes.VIDEO,
                "d": {
                    "audio_ssrc": self.ssrc,
                    "video_ssrc": self.video_ssrc,
                    "rtx_ssrc": self.rtx_ssrc,
                    "streams": [
                        {
                            "type": "video",
                            "rid": "100",
                            "ssrc": self.video_ssrc,
                            "active": state,
                            "quality": 100,
                            "rtx_ssrc": self.rtx_ssrc,
                            "max_bitrate": bitrate,
                            "max_framerate": framerate,
                            "max_resolution": {
                                "type": "fixed",
                                "width": width,
                                "height": height,
                            },
                        }
                    ],
                },
            }
        )

    async def handle_ws_events(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        *,
        udp_socket_preparation_event: typing.Optional[asyncio.Event] = None,
    ):
        async for msg in ws:
            payload = msg.json()
            data = payload["d"]

            match payload["op"]:
                case VoiceOpCodes.READY:
                    self.set_ssrc(data["ssrc"])
                    self.set_server_address(data["ip"], data["port"])

                    self.udp_connection.create_udp_socket()
                    await self.set_video_state(False)

                case VoiceOpCodes.HELLO:
                    self.loop.create_task(
                        self.setup_heartbeat(data["heartbeat_interval"])
                    )

                case VoiceOpCodes.HEARTBEAT_ACK:
                    latency = (self.loop.time() - self.last_heartbeat_at) * 1000

                    if self.ip and self.port:
                        addr = f"{self.ip}:{self.port}"
                    else:
                        addr = "Unknown"

                    self.logger.debug(
                        f"Heartbeat ACK was received. Latency: {latency:.2f}ms. Address: {addr}"
                    )

                case VoiceOpCodes.SPEAKING:
                    ...

                case VoiceOpCodes.SELECT_PROTOCOL_ACK:
                    self.secret_key = bytes(data["secret_key"])
                    udp_socket_preparation_event.set()

                case VoiceOpCodes.RESUMED:
                    ...

    async def set_speaking(self, speaking: bool):
        self.ensure_ready()

        return await self.ws.send_json(
            {
                "op": VoiceOpCodes.SPEAKING,
                "d": {
                    "speaking": int(speaking),
                    "delay": 0,
                    "ssrc": self.ssrc,
                },
            }
        )

    async def set_protocols(self):
        self.ensure_ready()

        return await self.ws.send_json(
            {
                "op": VoiceOpCodes.SELECT_PROTOCOL,
                "d": {
                    "protocol": "udp",
                    "codecs": [
                        {
                            "name": self.udp_connection.audio_packetizer.codec,
                            "type": "audio",
                            "priority": 1000,
                            "payload_type": 120,
                        },
                        {
                            "name": self.udp_connection.video_packetizer.codec,
                            "type": "video",
                            "priority": 1000,
                            "payload_type": 101,
                            "rtx_payload_type": 102,
                            "encode": True,
                            "decode": True,
                        },
                    ],
                    "data": {
                        "address": self.our_ip,
                        "port": self.our_port,
                        "mode": self.encryption_mode,
                    },
                },
            }
        )

    async def start(self):
        """
        Start the SVC websocket connection and wait until
        the internal UDP connection receives protocol acknowledgement.
        """

        if self.is_ready:
            raise RuntimeError("Media connection has already started.")

        self.ws = await self.session.ws_connect(
            f"wss://{self.endpoint}/", params={"v": 7}
        )
        # Do resume here in the future if it is that crucial.

        await self.ws.send_json(
            {
                "op": VoiceOpCodes.IDENTIFY,
                "d": {
                    "server_id": self.server_id or self.guild_id or self.channel_id,
                    "user_id": self.user_id,
                    "session_id": self.session_id,
                    "token": self.token,
                    "video": True,
                    "streams": [{"type": "screen", "rid": "100", "quality": 100}],
                },
            }
        )

        udp_socket_preparation_event = asyncio.Event()

        self.ws_handler_task = self.loop.create_task(
            self.handle_ws_events(
                self.ws, udp_socket_preparation_event=udp_socket_preparation_event
            )
        )
        return await udp_socket_preparation_event.wait()


class UDPConnection:
    MAX_INT_16 = 1 << 16
    MAX_INT_32 = 1 << 32

    def __init__(
        self,
        conn: VoiceConnection,
        *,
        audio_packetizer=audio_packetizer.AudioPacketizer,
        video_packetizer=h264_packetizer.H264Packetizer,
    ) -> None:
        self.nonce = 0

        self.conn = conn

        self.logger = logging.getLogger("udp_connection")

        self.audio_packetizer = audio_packetizer(self)
        self.video_packetizer = video_packetizer(self)

        self.udp_socket = None

    def set_ssrc(self, audio: int, video: int):
        self.audio_packetizer.ssrc = audio
        self.video_packetizer.ssrc = video

    def send_audio_frame(self, frame: bytearray):
        return self.audio_packetizer.send_frame(frame)

    def send_video_frame(self, frame: bytearray):
        return self.video_packetizer.send_frame(frame)

    def create_udp_socket(self):
        self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        payload = bytearray(74)

        struct.pack_into(">H", payload, 0, 0x1)
        struct.pack_into(">H", payload, 2, 0x46)
        struct.pack_into(">I", payload, 4, self.conn.ssrc)
        self.udp_socket.sendto(payload, (self.conn.ip, self.conn.port))

        data = self.udp_socket.recv(74)

        (handshake,) = struct.unpack(">H", data[:2])

        if handshake != 2:
            raise ValueError("Invalid handshake payload received from the server")

        our_ip = data[8 : data.find(0, 8)].decode("utf-8")
        (our_port,) = struct.unpack(">H", data[-2:])

        self.conn.own_identity = our_ip, our_port

        self.conn.loop.create_task(self.conn.set_protocols())

    def send_packet(self, packet: bytearray):
        if self.udp_socket is None:
            raise ValueError("UDP socket not created")

        self.udp_socket.sendto(packet, (self.conn.ip, self.conn.port))

    def close(self):
        if self.udp_socket is not None:
            self.udp_socket.close()
            self.udp_socket = None

    def encrypt_data_xsalsa20_poly1305(self, header: bytes, data) -> bytes:
        box = nacl.secret.SecretBox(bytes(self.conn.secret_key))
        nonce = bytearray(24)
        nonce[: len(header)] = header

        return header + box.encrypt(bytes(data), bytes(nonce)).ciphertext

    def encrypt_data_xsalsa20_poly1305_suffix(self, header: bytes, data) -> bytes:
        box = nacl.secret.SecretBox(bytes(self.conn.secret_key))
        nonce = nacl.utils.random(nacl.secret.SecretBox.NONCE_SIZE)

        return header + box.encrypt(bytes(data), nonce).ciphertext + nonce

    def encrypt_data_xsalsa20_poly1305_lite(self, header: bytes, data) -> bytes:
        self.nonce = checked_add(self.nonce, 1, self.MAX_INT_32)

        box = nacl.secret.SecretBox(bytes(self.conn.secret_key))
        nonce = bytearray(24)
        nonce[:4] = struct.pack(">I", self.nonce)

        return header + box.encrypt(bytes(data), bytes(nonce)).ciphertext + nonce[:4]

    encryptors = {
        "xsalsa20_poly1305": encrypt_data_xsalsa20_poly1305,
        "xsalsa20_poly1305_suffix": encrypt_data_xsalsa20_poly1305_suffix,
        "xsalsa20_poly1305_lite": encrypt_data_xsalsa20_poly1305_lite,
    }

    def encrypt_data(self, header: bytes, data: bytes) -> bytes:
        if self.conn.encryption_mode not in self.encryptors:
            raise ValueError(
                f"Unsupported encryption mode: {self.conn.encryption_mode}"
            )

        return self.encryptors[self.conn.encryption_mode](self, header, data)
