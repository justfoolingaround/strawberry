import asyncio
import base64
import enum

import aiohttp

from .connection import StreamConnection, VoiceConnection

voice_capabilities = 1 << 7


class DiscordGatewayOPCodes(enum.IntEnum):
    DISPATCH = 0
    HEARTBEAT = 1
    IDENTIFY = 2
    PRESENCE_UPDATE = 3
    VOICE_STATE_UPDATE = 4
    VOICE_SERVER_PING = 5
    RESUME = 6
    RECONNECT = 7
    REQUEST_GUILD_MEMBERS = 8
    INVALID_SESSION = 9
    HELLO = 10
    HEARTBEAT_ACK = 11
    CALL_CONNECT = 13
    GUILD_SUBSCRIPTIONS = 14
    LOBBY_CONNECT = 15
    LOBBY_DISCONNECT = 16
    LOBBY_VOICE_STATES_UPDATE = 17
    STREAM_CREATE = 18
    STREAM_DELETE = 19
    STREAM_WATCH = 20
    STREAM_PING = 21
    STREAM_SET_PAUSED = 22
    REQUEST_GUILD_APPLICATION_COMMANDS = 24
    EMBEDDED_ACTIVITY_LAUNCH = 25
    EMBEDDED_ACTIVITY_CLOSE = 26
    EMBEDDED_ACTIVITY_UPDATE = 27
    REQUEST_FORUM_UNREADS = 28
    REMOTE_COMMAND = 29
    GET_DELETED_ENTITY_IDS_NOT_MATCHING_HASH = 30
    REQUEST_SOUNDBOARD_SOUNDS = 31
    SPEED_TEST_CREATE = 32
    SPEED_TEST_DELETE = 33
    REQUEST_LAST_MESSAGES = 34
    SEARCH_RECENT_MEMBERS = 35


class DiscordGateway:
    VOICE_CAPABILITIES = 1 << 7
    DISCORD_API_ENDPOINT = "https://discord.com/api/v9"

    GATEWAY_VERSION = 9

    def __init__(self, token: str, *, session=None):
        self.loop = asyncio.get_event_loop()

        if token[:4] == "Bot ":
            raise ValueError("Invalid token: Bot tokens are not supported.")

        uid_payload, _ = token.split(".", 1)

        self.token = token

        self.session = session or aiohttp.ClientSession()
        self.user_id = base64.b64decode(uid_payload + "===").decode("utf-8")

        self.sequence = None
        self.ws_handler_task = None

        self.interceptors = []

        self.voice_connection: asyncio.Future[VoiceConnection] = asyncio.Future()
        self.stream_connection: asyncio.Future[StreamConnection] = asyncio.Future()

        self.pending_joins = {}
        self.ws = None

        self.latency = 0
        self.last_heartbeat_sent = 0

    async def join_voice_channel(self, channel_id: str, guild_id=None, region=None):
        await self.ws.send_json(
            {
                "op": DiscordGatewayOPCodes.VOICE_STATE_UPDATE,
                "d": {
                    "guild_id": guild_id,
                    "channel_id": channel_id,
                    "self_mute": False,
                    "self_deaf": False,
                    "self_video": False,
                    "preferred_region": region,
                },
            }
        )

        state_update, server_update = await self.create_ws_interceptor(
            (
                lambda data: data["t"] == "VOICE_STATE_UPDATE"
                and data["d"]["channel_id"] == channel_id
                and data["d"]["user_id"] == self.user_id
            ),
            (lambda data: data["t"] == "VOICE_SERVER_UPDATE"),
        )

        voice_conn = VoiceConnection(
            self.session,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=self.user_id,
            session_id=state_update["d"]["session_id"],
            endpoint=server_update["d"]["endpoint"],
            token=server_update["d"]["token"],
        )

        await voice_conn.start()
        return voice_conn

    async def heartbeat(self, interval):
        while self.ws is not None and not self.ws.closed:
            await asyncio.sleep(interval / 1000)
            await self.ws.send_json({"op": DiscordGatewayOPCodes.HEARTBEAT, "d": 1337})
            self.last_heartbeat_sent = self.loop.time()

    async def create_ws_interceptor(self, *predicates):
        """
        Intercepts the next message that satisfies the predicate
        if the predicate fails, the regular handling is done.
        """
        interception = asyncio.Future()
        unmatched_predicates = list(predicates)
        predicate_mapping = dict.fromkeys(predicates)

        async def interceptor(data):
            for predicate in unmatched_predicates:
                if predicate(data):
                    unmatched_predicates.remove(predicate)
                    predicate_mapping[predicate] = data

            if not unmatched_predicates:
                interception.set_result(list(predicate_mapping.values()))
                self.interceptors.remove(interceptor)

        self.interceptors.append(interceptor)
        return await interception

    async def handle_incoming(self):
        if self.ws is None:
            return

        async for message in self.ws:
            data = message.json()

            for interceptor in self.interceptors:
                await interceptor(data)

            match data["op"]:
                case DiscordGatewayOPCodes.HELLO:
                    self.loop.create_task(
                        self.heartbeat(data["d"]["heartbeat_interval"])
                    )

                case DiscordGatewayOPCodes.DISPATCH:
                    match data["t"]:
                        case "READY":
                            ...

                case DiscordGatewayOPCodes.HEARTBEAT_ACK:
                    self.latency = (self.loop.time() - self.last_heartbeat_sent) * 1000

    async def ws_connect(self):
        async with self.session.get(f"{self.DISCORD_API_ENDPOINT}/gateway") as response:
            gateway_endpoint = (await response.json())["url"]

        self.ws = await self.session.ws_connect(
            gateway_endpoint,
            params={
                "v": self.GATEWAY_VERSION,
                "encoding": "json",
            },
        )
        self.ws_handler_task = self.loop.create_task(self.handle_incoming())

        await self.ws.send_json(
            {
                "op": DiscordGatewayOPCodes.IDENTIFY,
                "d": {
                    "token": self.token,
                    "capabilities": voice_capabilities,
                    "properties": {},
                    "compress": False,
                },
            }
        )

    async def wait(self):
        await self.ws_handler_task

    async def update_voice_state(self, muted=False, deafened=False, video=False):
        voice_conn = await self.voice_connection

        await self.ws.send_json(
            {
                "op": DiscordGatewayOPCodes.VOICE_STATE_UPDATE,
                "d": {
                    "guild_id": voice_conn.guild_id,
                    "channel_id": voice_conn.channel_id,
                    "self_mute": muted,
                    "self_deaf": deafened,
                    "self_video": video,
                },
            }
        )

    async def create_stream(self, voice_conn: VoiceConnection, preferred_region=None):
        payload = {
            "op": DiscordGatewayOPCodes.STREAM_CREATE,
            "d": {
                "type": "guild",
                "guild_id": voice_conn.guild_id,
                "channel_id": voice_conn.channel_id,
                "preferred_region": preferred_region,
            },
        }

        if voice_conn.guild_id is None:
            payload["d"]["type"] = "call"

        await self.ws.send_json(payload)

        (
            stream_create_data,
            stream_server_update_data,
        ) = await self.create_ws_interceptor(
            (lambda data: data["t"] == "STREAM_CREATE"),
            (lambda data: data["t"] == "STREAM_SERVER_UPDATE"),
        )

        stream_conn = StreamConnection.from_voice_connection(
            voice_conn,
            stream_key=stream_create_data["d"]["stream_key"],
            rtc_server_id=stream_create_data["d"]["rtc_server_id"],
            rtc_server_endpoint=stream_server_update_data["d"]["endpoint"],
            rtc_server_token=stream_server_update_data["d"]["token"],
        )

        await self.set_stream_pause(stream_conn, False)
        await stream_conn.start()
        return stream_conn

    async def set_stream_pause(self, stream_conn: StreamConnection, paused: bool):
        await self.ws.send_json(
            {
                "op": DiscordGatewayOPCodes.STREAM_SET_PAUSED,
                "d": {
                    "stream_key": stream_conn.stream_key,
                    "paused": paused,
                },
            }
        )

    async def delete_stream(self, stream_conn: StreamConnection):
        await self.ws.send_json(
            {
                "op": DiscordGatewayOPCodes.STREAM_DELETE,
                "d": {"stream_key": stream_conn.stream_key},
            }
        )
