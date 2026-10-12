import asyncio
import base64
import contextlib
import enum
import logging
import typing

import aiohttp

from strawberry.voice import VoiceConnection, VoiceServer, VoiceState

from .connection import (
    StreamConnection,
    create_stream_connection,
    create_voice_connection,
)

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


class DiscordVoicePool:
    def __init__(self):
        self._states: dict[str, VoiceState] = {}
        self._connection: dict[str, VoiceConnection] = {}

        self._futures: dict[str, asyncio.Future[VoiceConnection]] = {}

    def _get_identifier(self, channel_id: str, guild_id: str | None):

        if guild_id is None:
            return f"private::{channel_id}"

        return f"guild::{guild_id}:{channel_id}"

    def create_future_for_connection(self, channel_id: str, guild_id: str | None):
        future = asyncio.Future()
        self._futures[self._get_identifier(channel_id, guild_id)] = future
        return future

    def update_connection(self, server: VoiceServer):
        for ident, state in self._states.items():
            if state.guild_id == server.guild_id:
                conn = VoiceConnection(state=state, server=server)
                future = self._futures.get(ident)
                # a second VOICE_SERVER_UPDATE for the same channel would
                # otherwise raise InvalidStateError
                if future is not None and not future.done():
                    future.set_result(conn)

                self._connection[ident] = conn

    def update_state(self, state: VoiceState):
        if state.channel_id is None:
            # disconnect: drop every state for this user
            self._states = {
                ident: s
                for ident, s in self._states.items()
                if s.user_id != state.user_id
            }
            return
        identifier = self._get_identifier(state.channel_id, state.guild_id)
        self._states[identifier] = state


# A gateway dispatch: the op, the dispatch type and its data.
GatewayPayload = dict[str, typing.Any]


class _Waiter:
    """One pending subscription to a gateway dispatch type.

    Deliberately not a callable: the old interceptor was an async closure
    registered in a list and removed from that list while the dispatcher
    was iterating it. A future is resolved by assignment, needs no
    deregistration, and carries a result type.
    """

    __slots__ = ("event_type", "future")

    def __init__(self, event_type: str, loop: asyncio.AbstractEventLoop):
        self.event_type = event_type
        self.future: asyncio.Future[dict[str, typing.Any]] = loop.create_future()


class DiscordGateway:
    VOICE_CAPABILITIES = 1 << 7
    DISCORD_API_ENDPOINT = "https://discord.com/api/v9"

    GATEWAY_VERSION = 9

    def __init__(self, ws: aiohttp.ClientWebSocketResponse, user_id: str):
        self.logger = logging.getLogger("gateway")
        self.loop = asyncio.get_running_loop()

        self.sequence = None

        self.user_id = user_id
        self._conn_pool = DiscordVoicePool()
        self.pending_joins = {}
        self.ws = ws

        # One-shot subscriptions to gateway dispatch types. See `wait_for`.
        self._waiters: list[_Waiter] = []

        self.latency = 0
        self.last_heartbeat_sent = 0

        self._ready_data: asyncio.Future[GatewayPayload] = (
            self.loop.create_future()
        )
        self.ws_handler_task = self.loop.create_task(self.handle_incoming())

    # -- waiting for dispatches ---------------------------------------------

    def wait_for(self, event_type: str) -> "asyncio.Future[GatewayPayload]":
        """Subscribe to the next dispatch of `event_type`.

        Returns a typed future rather than registering a callback, so callers
        can await one event or several at once:

            create, update = await asyncio.gather(
                gw.wait_for("STREAM_CREATE"),
                gw.wait_for("STREAM_SERVER_UPDATE"),
            )

        A subscription is consumed by the first matching dispatch and never
        outlives it - there is no list to clean up by hand, and nothing can
        mutate a collection while it is being iterated.
        """
        waiter = _Waiter(event_type, self.loop)
        self._waiters.append(waiter)
        return waiter.future

    def _resolve_waiters(self, event_type: str, payload: GatewayPayload) -> None:
        """Deliver `payload` to whoever is waiting for `event_type`.

        Replaces the waiter list rather than mutating it: a subscriber that
        arrives mid-iteration cannot corrupt the sweep.
        """
        remaining = []
        for waiter in self._waiters:
            if waiter.event_type == event_type and not waiter.future.done():
                waiter.future.set_result(payload)
            elif not waiter.future.done():
                remaining.append(waiter)
        self._waiters = remaining

    def _cancel_waiters(self, reason: Exception) -> None:
        """Fail every pending subscription rather than leaving it hanging.

        Without this, a socket that closes mid-handshake leaves the caller
        awaiting a future nothing will ever resolve, and the interceptor
        that was meant to catch it leaks for the life of the gateway.
        """
        for waiter in self._waiters:
            if not waiter.future.done():
                waiter.future.set_exception(reason)
        self._waiters.clear()

    async def fetch_ready_state(self):
        return await self._ready_data

    async def join_voice_channel(
        self,
        session: aiohttp.ClientSession,
        channel_id: str,
        guild_id=None,
        region=None,
        *,
        self_deaf: bool = True,
    ):
        """Join a voice channel.

        Deafened by default. This client is a broadcaster: it plays its own
        source and never the channel's audio, so being un-deafened only asks
        the server to forward every other member's media to us - which is what
        the unclassifiable `opaque pt=...` datagrams in the receive count are.
        The camera path already did this (`set_video_enabled`), so the two
        halves of the same client disagreed.
        """
        future = self._conn_pool.create_future_for_connection(channel_id, guild_id)
        await self.ws.send_json(
            {
                "op": DiscordGatewayOPCodes.VOICE_STATE_UPDATE,
                "d": {
                    "guild_id": guild_id,
                    "channel_id": channel_id,
                    "self_mute": False,
                    "self_deaf": self_deaf,
                    "self_video": False,
                    # null/empty means "let Discord pick the closest";
                    # an empty string is a real (unmatched) region value
                    "preferred_region": region or None,
                },
            }
        )
        return await future

    async def heartbeat(self, interval):
        while self.ws is not None and not self.ws.closed:
            await asyncio.sleep(interval / 1000)
            await self.ws.send_json({"op": DiscordGatewayOPCodes.HEARTBEAT, "d": 1337})
            self.last_heartbeat_sent = self.loop.time()

    async def handle_incoming(self):

        try:
            async for message in self.ws:
                try:
                    await self._handle_message(message)
                except Exception:
                    self.logger.exception("Error handling gateway message")
        except Exception as exc:
            # the socket is gone; nothing further will resolve, so nothing
            # should be left waiting
            self._cancel_waiters(exc)
            raise

    async def _handle_message(self, message):
        data = message.json()

        event_type = data.get("t")
        if event_type and data["op"] == DiscordGatewayOPCodes.DISPATCH:
            self._resolve_waiters(event_type, data)

        match data["op"]:
            case DiscordGatewayOPCodes.HELLO:
                self.loop.create_task(self.heartbeat(data["d"]["heartbeat_interval"]))

            case DiscordGatewayOPCodes.DISPATCH:
                match data["t"]:
                    case "READY":
                        self._ready_data.set_result(data["d"])
                    case "VOICE_STATE_UPDATE":
                        self._conn_pool.update_state(
                            VoiceState.model_validate(data["d"])
                        )
                    case "VOICE_SERVER_UPDATE":
                        self._conn_pool.update_connection(
                            VoiceServer.model_validate(data["d"])
                        )

            case DiscordGatewayOPCodes.HEARTBEAT_ACK:
                self.latency = (self.loop.time() - self.last_heartbeat_sent) * 1000

    async def wait(self):
        if self.ws_handler_task is not None:
            await self.ws_handler_task

    async def set_video_enabled(self, voice_conn: VoiceConnection, enabled: bool):
        """Turn the camera on/off for our own voice state.

        This is how camera video is published: the frames go out on the
        voice channel's own media session, with no Go Live stream involved
        (the reference client does the same for its camera mode).
        """
        await self.ws.send_json(
            {
                "op": DiscordGatewayOPCodes.VOICE_STATE_UPDATE,
                "d": {
                    "guild_id": voice_conn.state.guild_id,
                    "channel_id": voice_conn.state.channel_id,
                    "self_mute": False,
                    "self_deaf": True,
                    "self_video": enabled,
                },
            }
        )

    async def create_voice_media(
        self,
        session: aiohttp.ClientSession,
        voice_conn: VoiceConnection,
        video_codec: str = "H264",
        video_extension: bool = True,
        camera: bool = False,
    ) -> VoiceConnection:
        """Connect a voice channel media session, ready to send.

        Same contract as `create_stream`: the connection comes back
        transport-ready and the caller closes it.
        """
        return await create_voice_connection(
            session,
            user_id=voice_conn.state.user_id,
            channel_id=voice_conn.state.channel_id,
            session_id=voice_conn.state.session_id,
            guild_id=voice_conn.state.guild_id,
            endpoint=voice_conn.server.endpoint,
            token=voice_conn.server.token,
            video_codec=video_codec,
            video_extension=video_extension,
            camera=camera,
        )

    async def create_stream(
        self,
        session: aiohttp.ClientSession,
        voice_conn: VoiceConnection,
        preferred_region=None,
        video_codec: str = "H264",
        video_extension: bool = True,
    ) -> StreamConnection:
        """Create a Go Live stream and return it connected and ready.

        Sends STREAM_CREATE, waits for the two dispatches it depends on, and
        completes the media handshake, so the connection handed back is one a
        caller can send frames on immediately - the UDP transport is up and
        `transport_ready` has fired.

        The caller owns it:

            conn = await gateway.create_stream(session, voice_conn)
            try:
                ...
            finally:
                await conn.close()

        or, equivalently, `async with await gateway.create_stream(...)`.
        """
        is_call = voice_conn.state.guild_id is None

        payload = {
            "op": DiscordGatewayOPCodes.STREAM_CREATE,
            "d": {
                "type": "call" if is_call else "guild",
                "guild_id": voice_conn.state.guild_id,
                "channel_id": voice_conn.state.channel_id,
                "preferred_region": preferred_region or None,
            },
        }

        # The stream key is derivable client side; the reference client
        # unpauses with it right after STREAM_CREATE instead of waiting
        # for STREAM_CREATE to come back around.
        local_stream_key = ":".join(
            filter(
                None,
                [
                    "call" if is_call else "guild",
                    voice_conn.state.guild_id,
                    voice_conn.state.channel_id,
                    voice_conn.state.user_id,
                ],
            )
        )

        await self.ws.send_json(payload)
        await self.ws.send_json(
            {
                "op": DiscordGatewayOPCodes.STREAM_SET_PAUSED,
                "d": {"stream_key": local_stream_key, "paused": False},
            }
        )

        # Both dispatches are independent, so subscribe together and let the
        # event loop deliver whichever lands first.
        stream_create_data, stream_server_update_data = await asyncio.gather(
            self.wait_for("STREAM_CREATE"),
            self.wait_for("STREAM_SERVER_UPDATE"),
        )

        stream_key = stream_create_data["d"]["stream_key"]
        rtc_server_id = stream_create_data["d"]["rtc_server_id"]

        try:
            conn = await create_stream_connection(
                session,
                user_id=voice_conn.state.user_id,
                channel_id=voice_conn.state.channel_id,
                session_id=voice_conn.state.session_id,
                guild_id=voice_conn.state.guild_id,
                stream_key=stream_key,
                rtc_server_id=rtc_server_id,
                endpoint=stream_server_update_data["d"]["endpoint"],
                token=stream_server_update_data["d"]["token"],
                video_codec=video_codec,
                video_extension=video_extension,
                # the MLS group id for Go Live streams is rtc_server_id - 1,
                # not the voice channel id (see the JS reference client)
                dave_channel_id=str(int(rtc_server_id) - 1),
            )
        except BaseException:
            # the stream exists on Discord's side whatever happens next
            await self.delete_stream(stream_key)
            raise

        # The stream outlives the handshake, so it cannot be deleted in a
        # finally here. Hand the deletion to the connection instead: closing
        # the connection is what should end the stream, and a caller cannot
        # forget to do that by accident.
        conn.on_close = lambda: self.delete_stream(stream_key)
        return conn

    async def delete_stream(self, stream_key: str) -> None:
        """Remove a Go Live stream. Safe to call more than once."""
        await self.ws.send_json(
            {
                "op": DiscordGatewayOPCodes.STREAM_DELETE,
                "d": {"stream_key": stream_key},
            }
        )

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


async def fetch_optimal_voice_region(
    session: aiohttp.ClientSession, api_endpoint: str, token: str
) -> "str | None":
    """The voice region Discord marks optimal for this client.

    `GET /voice/regions` returns every region with an `optimal` flag
    (see discord.py's VoiceRegion: id/name/vip/optimal/deprecated).
    Falls back to None (Discord picks) when the lookup fails.
    """
    try:
        async with session.get(
            f"{api_endpoint}/voice/regions", headers={"Authorization": token}
        ) as response:
            if response.status != 200:
                return None
            regions = await response.json()
    except Exception as e:
        logging.getLogger("gateway").warning("voice region lookup failed: %s", e)
        return None

    for region in regions:
        if region.get("optimal") and not region.get("deprecated"):
            return region["id"]

    # no explicit optimum: take any live non-vip region
    for region in regions:
        if not region.get("deprecated") and not region.get("vip"):
            return region["id"]

    return None


@contextlib.asynccontextmanager
async def create_gateway_connection(
    session: aiohttp.ClientSession, token: str, *, gateway: str | None = None
):
    if gateway is None:
        async with session.get(
            f"{DiscordGateway.DISCORD_API_ENDPOINT}/gateway"
        ) as response:
            url = (await response.json())["url"]
    else:
        url = gateway

    if token[:4] == "Bot ":
        raise ValueError("Invalid token: Bot tokens are not supported.")

    ws = await session.ws_connect(
        url,
        params={
            "v": str(DiscordGateway.GATEWAY_VERSION),
            "encoding": "json",
        },
    )

    uid_payload, _ = token.split(".", 1)
    user_id = base64.b64decode(uid_payload + "===").decode("utf-8")

    await ws.send_json(
        {
            "op": DiscordGatewayOPCodes.IDENTIFY,
            "d": {
                "token": token,
                "capabilities": voice_capabilities,
                "properties": {},
                "compress": False,
            },
        }
    )
    try:
        yield DiscordGateway(ws, user_id)
    finally:
        await ws.close()
