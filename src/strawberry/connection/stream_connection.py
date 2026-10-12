"""
Strawberry Stream Connection (DAVE-capable)
===========================================

A Go Live stream connection: a voice websocket (v8, JSON + binary frames)
to the media server negotiated by the gateway, plus the UDP connection
that carries the RTP packets.

Media protection is two layers:

- transport encryption, `aead_xchacha20_poly1305_rtpsize` (mandatory since
  the xsalsa20 modes were deprecated): every RTP packet is
  `[RTP header (+ extension preamble)][ciphertext+tag][4-byte nonce counter]`
  with the header (incl. preamble) as additional data.
- DAVE end-to-end encryption on top: encoded frames (Annex B for H264)
  are wrapped by the davey session before packetization, keyed by the MLS
  group negotiated via voice gateway opcodes 21-31.

Send-only: received packets are not decrypted here.
"""

import asyncio
import base64
import enum
import logging
import socket
import struct
import threading
import time
import typing
from collections.abc import Awaitable, Callable

import aiohttp
import davey

from strawberry.dave import DaveSessionManager
from strawberry.protocols.rtcp import (
    parse_rtcp_nack,
    rtcp_kind,
)

from .base_connection import (
    ENCRYPTION_MODES,
    SUPPORTED_MODES,
    TRANSPORT_MODE,
    BaseConnection,
    decrypt_rtcp,
)

logger = logging.getLogger("stream_connection")

# IP discovery request: type=1, length=0x46, ssrc (filled in per connection)
UDP_DISCOVERY_REQUEST = bytearray(74)
struct.pack_into(">HHI", UDP_DISCOVERY_REQUEST, 0, 0x1, 0x46, 0)


class VoiceOpCodes(enum.IntEnum):
    IDENTIFY = 0
    SELECT_PROTOCOL = 1
    READY = 2
    HEARTBEAT = 3
    SESSION_DESCRIPTION = 4  # a.k.a. select_protocol_ack
    SPEAKING = 5
    HEARTBEAT_ACK = 6
    RESUME = 7
    HELLO = 8
    RESUMED = 9
    VIDEO = 12
    CLIENTS_CONNECT = 11
    CLIENT_DISCONNECT = 13
    SESSION_UPDATE = 14
    MEDIA_SINK_WANTS = 15
    VOICE_BACKEND_VERSION = 16
    CHANNEL_OPTIONS_UPDATE = 17
    FLAGS = 18
    SPEED_TEST = 19
    PLATFORM = 20
    DAVE_PREPARE_TRANSITION = 21
    DAVE_EXECUTE_TRANSITION = 22
    DAVE_TRANSITION_READY = 23
    DAVE_PREPARE_EPOCH = 24
    DAVE_MLS_EXTERNAL_SENDER = 25
    DAVE_MLS_KEY_PACKAGE = 26
    DAVE_MLS_PROPOSALS = 27
    DAVE_MLS_COMMIT_WELCOME = 28
    DAVE_MLS_ANNOUNCE_COMMIT_TRANSITION = 29
    DAVE_MLS_WELCOME = 30
    DAVE_MLS_INVALID_COMMIT_WELCOME = 31


# What op 15 announces as max_bitrate. The reference client hardcodes 10 Mbps
# regardless of what it encodes (BaseMediaConnection.ts:614); announcing less
# than we actually send gets the stream throttled by the server.
ANNOUNCED_MAX_BITRATE = 10_000_000

# How long to wait for the DAVE group before streaming transport-only.
DAVE_WAIT_SECONDS = 60

RTP_CODECS = {
    111: "opus",
    120: "opus",
    # Discord's own numbering, see VIDEO_CODECS in codecs.py
    101: "AV1",
    102: "rtx",
    103: "H265",
    104: "rtx",
    105: "H264",
    106: "rtx",
    107: "VP8",
    108: "rtx",
    109: "VP9",
    110: "rtx",
}


class UDPProtocol(asyncio.DatagramProtocol):
    """Handles the IP discovery response and classifies whatever else the
    media server sends us.

    We are send-only, so nothing is decoded past the header - other
    members' media is not ours to read - but the traffic itself is worth
    knowing about: RTCP feedback (PLI/FIR keyframe requests, NACK) is the
    only way to learn that a viewer is struggling.
    """

    def __init__(self, conn: "StreamConnection"):
        self.conn = conn
        self.discovery: asyncio.Future[tuple[str, int]] | None = (
            conn.loop.create_future()
        )
        self.received: dict[str, int] = {}
        self.bytes_in = 0
        self._feedback = conn
        self.window = conn.loop.time()
        self._report_at = self.window

    def datagram_received(self, data: bytes, addr):
        if self.discovery is not None and not self.discovery.done():
            if len(data) >= 74 and data[:2] == b"\x00\x02":
                ip = data[8 : data.index(0, 8)].decode("utf-8")
                port = int.from_bytes(data[-2:], "big")
                self.conn.logger.info("ip discovery: %s:%s", ip, port)
                self.discovery.set_result((ip, port))
                self.discovery = None
            return

        self.bytes_in += len(data)
        self._note_feedback(data)
        kind = self._classify(data)
        self.received[kind] = self.received.get(kind, 0) + 1
        self._report()

    def _classify(self, data: bytes) -> str:
        """Name a packet from its first bytes.

        Media forwarded to us is transport encrypted with the session key,
        so headers are usually unreadable; the few unencrypted control
        packets are.
        """
        if len(data) < 2 or data[0] & 0xC0 != 0x80:
            return f"opaque/{len(data)}B"

        # RTCP types sit in the raw byte; RTP payload types have the marker
        # bit in the high bit, so they need masking (and RTCP has none)
        raw = data[1]
        name = rtcp_kind(data)
        if name is not None:
            return f"rtcp {name}"

        payload_type = raw & 0x7F
        codec = RTP_CODECS.get(payload_type)
        if codec is not None:
            marker = "M" if raw & 0x80 else ""
            ssrc = int.from_bytes(data[8:12], "big") if len(data) >= 12 else 0
            return f"rtp {codec}{marker} ssrc={ssrc}"
        return f"opaque pt={payload_type}/{len(data)}B"

    def _note_feedback(self, data: bytes):
        """Handle RTCP feedback from the media server.

        We are send-only, so everything that arrives here is control
        traffic.

        The datagram is decrypted *before* it is parsed, and a decryption
        failure means "this was not feedback". That is the guard the earlier
        attempt lacked: media forwarded to us is transport-encrypted too, so
        it arrives as ciphertext, and since a NACK's FCI is a list of 16 bit
        sequence numbers, parsing unauthenticated bytes matched the sent
        window constantly - 10k junk packets turned into 6.1M injected
        datagrams. Nothing is acted on unless it authenticates.
        """
        if len(data) < 12:
            return
        kind = rtcp_kind(data)
        if kind not in ("NACK", "PLI", "FIR", "SR", "RR"):
            return

        conn = self.conn
        try:
            key = conn.secret_key
            if key is None:
                return
            body = decrypt_rtcp(data, key, conn.transport_mode)
        except Exception:
            # Not authenticated, so not RTCP: forwarded media, or noise.
            # Count it for the summary, but never act on it.
            conn.note_rtcp_feedback("rtcp-ish", 0)
            return

        if kind == "NACK":
            self._resend_nacked(body)
        elif kind in ("PLI", "FIR"):
            # A key frame request names the media SSRC in the same place a
            # NACK does: after the packet header and the *sender* SSRC. This
            # read body[4:8] - the sender's - so every request aimed at our
            # video was labelled "other" and thrown away, and a viewer could
            # ask for a key frame forever without ever being sent one. The
            # log said `other pli ssrc=2 x7`: 2 is the client's own SSRC.
            media_ssrc = int.from_bytes(body[8:12], "big")
            conn.note_rtcp_feedback(kind, media_ssrc)
            if media_ssrc == conn.video_ssrc and conn.resend_keyframe():
                conn.logger.info("replayed the last key frame for a %s", kind)
        else:
            # SR and RR have no media SSRC; the reporter's own is the useful
            # one, and it is at the same offset.
            conn.note_rtcp_feedback(kind, int.from_bytes(body[4:8], "big"))

    def _resend_nacked(self, body: bytes) -> None:
        """Rebuild and resend the video packets a NACK asks for."""
        conn = self.conn
        named = list(parse_rtcp_nack(body))
        if not conn.rtx_enabled or not conn.rtx_ssrc or conn.secret_key is None:
            return

        requested = [
            sequence
            for media_ssrc, sequence in named
            # Only our own video. A NACK naming another member's SSRC, or our
            # audio SSRC, must not produce a retransmission.
            if media_ssrc == conn.video_ssrc
        ]
        if not requested:
            # Say which stream was asked for. Returning quietly here made a
            # stream's whole retransmission traffic invisible: 54 NACKs in
            # one run produced no log line at all, so there was no way to
            # tell whether the far end was losing our video or our audio.
            conn.logger.info(
                "nack named %s, not our video ssrc %s - not resending",
                sorted({media_ssrc for media_ssrc, _ in named}) or "nothing",
                conn.video_ssrc,
            )
            return

        budget = conn.nack_gate.allow(time.monotonic(), len(requested))
        resent = 0
        for sequence in requested:
            if resent >= budget:
                break
            if conn.send_rtx(sequence):
                resent += 1

        sender_ssrc = int.from_bytes(body[4:8], "big")
        conn.note_rtcp_feedback(
            "NACK", sender_ssrc, requested=len(requested), answered=resent
        )
        if resent:
            conn.logger.debug("rtx: resent %d/%d video packets", resent, len(requested))

    def _report(self):
        now = self.conn.loop.time()
        elapsed = now - self._report_at
        if elapsed < 10:
            return
        summary = ", ".join(f"{k} x{v}" for k, v in sorted(self.received.items()))
        self.conn.logger.info(
            "received %s bytes (%.1f kbps): %s",
            f"{self.bytes_in:,}",
            self.bytes_in * 8 / elapsed / 1000,
            summary or "nothing",
        )
        self.received.clear()
        self.bytes_in = 0
        self._report_at = now

    def error_received(self, exc: Exception):
        self.conn.logger.warning("UDP error received: %s", exc)

    def connection_lost(self, exc: Exception | None):
        if exc is not None:
            self.conn.logger.warning("UDP connection lost: %s", exc)


class StreamConnection(BaseConnection):
    # 2 marks an active Go Live stream; voice channels use 1
    SPEAKING_ACTIVE = 2

    def __init__(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        user_id: str,
        channel_id: str,
        *,
        stream_key: "str | None" = None,
        # ponytail: the MLS group id for Go Live streams is assumed to be
        # the voice channel id; if commits get rejected, pass the stream's
        # own id here instead.
        dave_channel_id: "str | None" = None,
        video_codec: str = "H264",
        video_extension: bool = True,
        camera: bool = False,
    ):
        super().__init__(video_codec=video_codec, video_extension=video_extension)
        # camera: video on the voice channel's own session. The stream is
        # announced (op 12, active) as soon as the session is described,
        # like the official client does when you turn the camera on.
        self.camera = camera
        self.logger = logging.getLogger("stream_connection")
        self.loop = asyncio.get_event_loop()
        self.ws: aiohttp.ClientWebSocketResponse = ws
        # Optional gateway-side teardown (deleting the Go Live stream),
        # set by DiscordGateway.create_stream so that closing a connection
        # cannot leave a stream behind on Discord's side. `close()` runs it
        # before releasing the transport.
        self.on_close: Callable[[], Awaitable[None]] | None = None

        self.dave = DaveSessionManager(
            int(user_id),
            int(dave_channel_id or channel_id),
            self._schedule_send_json,
            self._schedule_send_binary,
        )

        self.stream_key = stream_key

        self.ws_handler_task = self.loop.create_task(self.handle_incoming())
        self.heartbeat_task = None
        self.ws_sequence = -1

        self.udp_transport: asyncio.DatagramTransport | None = None
        # set once a send has failed: a dead socket fails every packet, and
        # that is one warning rather than one per packet
        self._send_failed = False
        self.udp_protocol: UDPProtocol | None = None

        self.ssrc = 0
        self.video_ssrc = 0
        self.rtx_ssrc = 0
        self.secret_key: bytes | None = None
        self.encryption_mode: str | None = None
        self.rtc_connection_id: str | None = None
        self.experiments: list[typing.Any] = []
        # Whether a viewer currently wants our video ssrc, from op 15. Only
        # used to greet a *new* viewer with a key frame, so it is a flag
        # rather than a count.
        self._video_wanted = False
        self.nonce = 0
        self._nonce_lock = threading.Lock()  # streamer threads share it

        self.transport_ready = asyncio.Event()
        self._closed = False
        # packet pacing (see send_packet); primed from the announced bitrate
        self._pace_lock = threading.Lock()
        self._next_packet_at = 0.0
        self.packet_size = 1200
        self._packet_interval = 0.005
        # how far the pacer may run ahead of real time before it gives up
        # on catching up; set from the framerate with set_video_bitrate
        self.video_frame_interval = 0.033
        # How far behind schedule the pacer may get before it abandons the
        # backlog and resynchronises to now. One frame interval: a live
        # stream that is a frame late should drop the stale schedule, not
        # replay a frame's worth of packets back to back.
        self._pace_resync = 0.033
        self._pace_dropped = 0

        # [rtp packets, bytes] per media type, logged periodically so a
        # silent or mis-paced media path can never hide again
        self._sent = {"audio": [0, 0], "video": [0, 0]}
        self._frames = {"audio": 0, "video": 0}
        self._last_report = {
            "audio": [0, 0, 0],
            "video": [0, 0, 0],
            "at": self.loop.time(),
            "at_start": self.loop.time(),
        }

        # the codec we can encode and packetize; the server may pick
        # the effective codec is picked at op 4, see set_video_codec
        # one-byte RTP header extension (playout delay); receivers that did
        # not negotiate one misparse every video packet
        self.video_extension = video_extension

        # Binary messages: server -> client is [u16 seq][u8 op][payload],
        # client -> server is [u8 op][payload].

    # -- websocket --------------------------------------------------------

    async def _send_json(self, op: int, data):
        if op not in (VoiceOpCodes.HEARTBEAT, VoiceOpCodes.HEARTBEAT_ACK):
            self.logger.debug("send op %s: %s", VoiceOpCodes(op).name, data)
        await self.ws.send_json({"op": op, "d": data})

    async def _send_binary(self, op: int, payload: bytes):
        self.logger.debug("send binary op %s (%s bytes)", op, len(payload))
        await self.ws.send_bytes(bytes((op,)) + payload)

    def _schedule_send_json(self, op: int, data):
        self.loop.create_task(self._guarded(self._send_json(op, data)))

    def _schedule_send_binary(self, op: int, payload: bytes):
        self.loop.create_task(self._guarded(self._send_binary(op, payload)))

    async def _guarded(self, coro):
        try:
            await coro
        except Exception as e:
            self.logger.warning("Websocket send failed: %s", e)

    async def close(self):
        """Tear the connection down.

        The gateway-side cleanup is best effort but the transport is not:
        even if deleting the stream raises, the websocket and the UDP socket
        still have to go, or the process keeps sending into a dead session.
        """
        self._closed = True
        for task in (self.ws_handler_task, self.heartbeat_task):
            if task is not None:
                task.cancel()
        try:
            if self.on_close is not None:
                callback, self.on_close = self.on_close, None
                await callback()
        finally:
            if self.ws is not None and not self.ws.closed:
                await self.ws.close()
            self._close_udp()

    async def __aenter__(self) -> "StreamConnection":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    async def handle_incoming(self):

        async for msg in self.ws:
            try:
                if msg.type is aiohttp.WSMsgType.BINARY:
                    self.ws_sequence = int.from_bytes(msg.data[:2], "big")
                    self.logger.debug(
                        "recv binary op %s (%s bytes, seq %s)",
                        msg.data[2],
                        len(msg.data),
                        self.ws_sequence,
                    )
                    await self.handle_binary(msg.data[2], msg.data[3:])
                elif msg.type is aiohttp.WSMsgType.TEXT:
                    packet = msg.json()
                    if packet.get("seq"):
                        self.ws_sequence = packet["seq"]
                    await self.handle_json(packet)
            except Exception:
                # never let one bad message kill the handler: that would
                # leave create_stream_connection waiting on transport_ready
                # forever
                self.logger.exception("Error handling voice websocket message")

        self.logger.info("voice websocket closed: code=%s", self.ws.close_code)

    async def handle_json(self, packet):
        op, data = packet["op"], packet.get("d") or {}
        self.logger.debug("recv op %s", VoiceOpCodes(op).name)

        match op:
            case VoiceOpCodes.HELLO:
                self.heartbeat_task = self.loop.create_task(
                    self.setup_heartbeat(data["heartbeat_interval"])
                )

            case VoiceOpCodes.READY:
                stream = (data.get("streams") or [None])[0]
                self.logger.info(
                    "ready: ssrc %s, video ssrc %s (server streams: %s)",
                    data["ssrc"],
                    stream["ssrc"] if stream else "none",
                    data.get("streams") or "none",
                )
                if not stream:
                    # No video stream was allocated: we fall back to
                    # ssrc + 1, which the media server does not know, so
                    # anything we send is silently dropped. Say so loudly
                    # rather than streaming into the void.
                    requested = self.requested_codec or self.video_codec
                    self.logger.warning(
                        "the media server allocated no video stream "
                        "(%s); video cannot be delivered on ssrc %s + 1",
                        requested.name,
                        data["ssrc"],
                    )
                self.set_ssrc(
                    data["ssrc"],
                    video_ssrc=stream["ssrc"] if stream else None,
                    rtx_ssrc=stream["rtx_ssrc"] if stream else None,
                )
                # The RID the server allocated travels in a header extension
                # on every video packet, which is how the far end ties a
                # packet back to the stream it belongs to. The reference does
                # this (transport.rs build_video_extension_payload) and its
                # H265 is accepted; we were sending no RID at all.
                if stream and stream.get("rid"):
                    self.video_packetizer.rid = stream["rid"]

                # The server offers a list and we pick from it by our own
                # preference order, then tell it which one in select_protocol.
                # We used to hardcode XChaCha20 here and advertise that, so
                # the server never chose the AES mode it prefers.
                offered = data.get("modes", [])
                modes = [m for m in SUPPORTED_MODES if m in offered]
                if not modes:
                    raise ValueError(
                        f"Voice server offers no mode we support: {offered}"
                    )
                self.transport_mode = modes[0]
                self.rtc_connection_id = data.get("rtc_connection_id")
                self.experiments = list(data.get("experiments") or [])
                await self._setup_udp(data["ip"], data["port"], modes[0])

            case VoiceOpCodes.SESSION_DESCRIPTION:
                self.set_video_codec(data.get("video_codec"))
                self.encryption_mode = data["mode"]
                # the server picks the aead cipher and tells us which, in
                # op 4. We used to always send xchacha whatever it said.
                if data["mode"] in ENCRYPTION_MODES:
                    self.transport_mode = data["mode"]
                else:
                    self.logger.warning(
                        "unknown transport mode %s, falling back to %s",
                        data["mode"], TRANSPORT_MODE)
                self.secret_key = bytes(data["secret_key"])
                self.logger.info(
                    "session description: mode=%s dave_protocol_version=%s "
                    "audio_codec=%s video_codec=%s",
                    data.get("mode"),
                    data.get("dave_protocol_version"),
                    data.get("audio_codec"),
                    data.get("video_codec"),
                )
                self.transport_ready.set()
                self.dave.on_session_description(data.get("dave_protocol_version", 0))
                await self.set_video_state(False)
                if self.camera:
                    self.logger.info("announcing camera video to the server")
                    await self.set_video_state(True, *self.camera_params)

            case VoiceOpCodes.MEDIA_SINK_WANTS:
                self.on_media_sink_wants(data)

            case VoiceOpCodes.CLIENTS_CONNECT:
                self.dave.on_clients_connect(data["user_ids"])

            case VoiceOpCodes.CLIENT_DISCONNECT:
                self.dave.on_client_disconnect(data["user_id"])

            case VoiceOpCodes.DAVE_PREPARE_TRANSITION:
                self.dave.on_prepare_transition(
                    data["transition_id"], data["protocol_version"]
                )

            case VoiceOpCodes.DAVE_EXECUTE_TRANSITION:
                self.dave.on_execute_transition(data["transition_id"])

            case VoiceOpCodes.DAVE_PREPARE_EPOCH:
                self.dave.on_prepare_epoch(data["epoch"], data["protocol_version"])

            case VoiceOpCodes.HEARTBEAT_ACK:
                self.logger.debug("Heartbeat ACK.")

            case _:
                self.logger.info("unhandled voice op %s: %s", op, data)

    async def handle_binary(self, op: int, payload: bytes):
        match op:
            case VoiceOpCodes.DAVE_MLS_EXTERNAL_SENDER:
                self.dave.on_external_sender(payload)

            case VoiceOpCodes.DAVE_MLS_PROPOSALS:
                # payload: [u8 operation_type][MLSMessage proposal vector]
                self.dave.on_proposals(payload[0], payload[1:])

            case VoiceOpCodes.DAVE_MLS_ANNOUNCE_COMMIT_TRANSITION:
                # payload: [u16 transition_id][MLSMessage commit]
                transition_id = int.from_bytes(payload[:2], "big")
                self.dave.on_commit(payload[2:], transition_id)

            case VoiceOpCodes.DAVE_MLS_WELCOME:
                # payload: [u16 transition_id][MLSMessage welcome]
                transition_id = int.from_bytes(payload[:2], "big")
                self.dave.on_welcome(payload[2:], transition_id)

    async def setup_heartbeat(self, interval: int):
        while self.ws is not None and not self.ws.closed:
            await self._send_json(
                VoiceOpCodes.HEARTBEAT,
                {"t": int(time.time() * 1000), "seq_ack": self.ws_sequence},
            )
            self.send_udp_keepalive()
            await asyncio.sleep(interval / 1000)

    # -- UDP ---------------------------------------------------------------

    async def _setup_udp(self, ip: str, port: int, transport_mode: str):
        """Async UDP connection + IP discovery, straight on the event loop."""
        self.logger.info(
            "connecting udp to %s:%s (ssrc %s, mode %s)",
            ip,
            port,
            self.ssrc,
            transport_mode,
        )
        transport, protocol = await self.loop.create_datagram_endpoint(
            lambda: UDPProtocol(self), remote_addr=(ip, port)
        )
        # every packet of an access unit leaves back to back, so a burst can
        # outrun the socket buffer and the media server's receive queue (the
        # loss shows up as RTCP NACK/PLI from the viewer)
        sock = transport.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
            except OSError as e:  # not fatal, just less headroom
                self.logger.debug("could not raise the udp send buffer: %s", e)
        self.udp_transport, self.udp_protocol = transport, protocol
        self.logger.info("udp socket bound to %s", transport.get_extra_info("sockname"))

        request = bytearray(UDP_DISCOVERY_REQUEST)
        struct.pack_into(">I", request, 4, self.ssrc)
        transport.sendto(request)
        self.logger.info("sent ip discovery request")

        assert protocol.discovery is not None
        try:
            async with asyncio.timeout(10):
                our_ip, our_port = await protocol.discovery
        except TimeoutError as e:
            raise RuntimeError(
                f"No IP discovery response from {ip}:{port} within 10s"
            ) from e

        await self._send_json(
            VoiceOpCodes.SELECT_PROTOCOL,
            {
                "protocol": "udp",
                "codecs": self.codecs(),
                "data": {
                    "address": our_ip,
                    "port": our_port,
                    "mode": transport_mode,
                },
                # discord.py echoes both back on select_protocol
                **(
                    {"rtc_connection_id": self.rtc_connection_id}
                    if self.rtc_connection_id
                    else {}
                ),
                **({"experiments": self.experiments} if self.experiments else {}),
            },
        )

    def on_media_sink_wants(self, data: "dict[str, typing.Any]") -> None:
        """op 15: which of our streams the far end wants, and at what quality.

        Discord's MediaSinkWants. `wants` is per SSRC, and 0 means "do not
        forward this one"; `any` is the fallback for SSRCs it did not name;
        `pixelCounts` is the resolution each viewer would like.

        The reference stores all three and uses them to decide whether a *low
        quality* simulcast layer is worth sending at all
        (`_video_stream_has_remote_receivers`, client.py). We publish one
        stream at quality 100, which it always sends, so that gating does not
        apply to us.

        What the message does give us is the first notice that somebody is
        watching - and it arrives *before* they have anything to decode. So a
        video ssrc that has just become wanted is treated as a key frame
        request: the PLI that would ask for the same thing cannot be sent
        until a packet has failed to arrive or decode, so acting here saves a
        round trip of black picture.
        """
        if not isinstance(data, dict):
            return
        wants = {
            int(ssrc): int(value)
            for ssrc, value in data.items()
            if ssrc not in ("any", "pixelCounts")
        }
        any_want = data.get("any")
        self.logger.info(
            "media sink wants: %s (any %s, pixels %s)",
            ", ".join(f"ssrc {s} -> {q}" for s, q in sorted(wants.items())) or "none",
            any_want,
            data.get("pixelCounts") or "none",
        )

        wanted = wants.get(self.video_ssrc, any_want)
        wants_video = isinstance(wanted, int) and wanted > 0
        if wants_video and not self._video_wanted:
            self._video_wanted = True
            if self.resend_keyframe():
                self.logger.info(
                    "video ssrc %s is newly wanted: replayed a key frame",
                    self.video_ssrc,
                )
        elif not wants_video:
            # Nobody wants it: forget, so a later viewer is greeted as new.
            self._video_wanted = False

    def send_udp_keepalive(self):
        if self.udp_transport is not None:
            self.send_packet(b"\x80\xc8\x00\x00")

    def send_packet(self, packet: bytes, media: "str | None" = None):
        """Thread-safe: media packets arrive from the streamer threads.

        Video is paced: a single key frame can be dozens of packets and
        firing them back to back overruns the socket and the media
        server's receive queue, which shows up as a stream that never
        appears. The reference client caps bursts the same way
        (PacingHandler(bitrate, 2)).

        Audio is not paced here: it has its own cadence from the sender
        loop, and gating it behind the video interval only adds jitter.

        `_pace_resync` bounds how far behind the schedule may get. Past that
        the schedule is dropped and restarted from now, because a live
        viewer wants the current frame at the right rate, not a backlog
        replayed at them - and because trying to catch up is what turned a
        frame that does not fit the frame interval into one large burst.
        """
        if self.udp_transport is None:
            if not self._closed:
                raise ValueError("UDP connection not established")
            return  # shutting down: drop instead of failing every sender

        if media != "video":
            self.loop.call_soon_threadsafe(self._send_datagram, packet)
            return

        now = time.monotonic()
        with self._pace_lock:
            if self._next_packet_at < now - self._pace_resync:
                # The schedule is far enough behind that catching up packet by
                # packet is the wrong thing to do. This happens whenever the
                # frame does not fit the frame interval: a 1080p key frame is
                # ~100 packets, which at `_packet_interval` is ~157 ms of
                # packets against a 33 ms budget. Clamping to
                # `now + video_frame_interval` used to cap how far ahead the
                # schedule could run, but every packet past that cap then got
                # `due <= now` and was sent immediately - so 79 of every 100
                # packets went out in one burst and the frame arrived as a
                # spike. For a live stream the answer is to drop the stale
                # schedule and restart from now: the next packet goes out
                # promptly, at the right *rate*, and the missed time is never
                # made up (a live viewer does not want it replayed at them).
                self._pace_dropped += 1
                self._next_packet_at = now
            due = max(now, self._next_packet_at)
            self._next_packet_at = due + self._packet_interval
        if due <= now:
            self.loop.call_soon_threadsafe(self._send_datagram, packet)
        else:
            # Through `call_soon_threadsafe`, because `call_later` is not one:
            # it pushes onto the loop's timer heap while the loop thread is
            # popping from it. Both sender threads run this, so under load the
            # two overlap. uvloop's heap is C, which turns that race into a
            # segfault in the sending thread rather than a late packet.
            self.loop.call_soon_threadsafe(
                self.loop.call_later, due - now, self._send_datagram, packet
            )

    def set_video_bitrate(self, bitrate: int):
        """Derive the packet interval from the video bitrate we encode at."""
        self._packet_interval = max(
            0.0005, min(0.05, self.packet_size * 8 / max(bitrate, 1))
        )
        self.logger.info(
            "video pacing: %.2f ms per packet (%.0f packets/s at %d bytes)",
            self._packet_interval * 1000,
            1 / self._packet_interval,
            self.packet_size,
        )

    def _send_datagram(self, packet: bytes):
        """Send one datagram, if there is still a socket to send it on.

        This runs as a loop callback, so an exception here is reported per
        packet by asyncio and does not stop the sender - a socket that failed
        once would produce a traceback for every packet left in the stream.
        Warn once and keep dropping, rather than a hundred times a second.
        """
        transport = self.udp_transport
        if transport is None or transport.is_closing():
            return
        try:
            transport.sendto(packet)
        except OSError as exc:
            if not self._send_failed:
                self._send_failed = True
                self.logger.warning(
                    "UDP send failed (%s); dropping packets for the rest of "
                    "the stream",
                    exc,
                )

    def _close_udp(self):
        if self.udp_transport is not None:
            self.udp_transport.close()
            self.udp_transport = None

    # -- media -------------------------------------------------------------

    async def set_speaking(self, speaking: bool):
        return await self._send_json(
            VoiceOpCodes.SPEAKING,
            {
                # 2 marks an active Go Live stream, 0 an idle one
                "speaking": 2 if speaking else 0,
                "delay": 0,
                "ssrc": self.ssrc,
            },
        )

    async def wait_dave_ready(self) -> None:
        """Hold media until the DAVE group is up, or give up on it.

        Media sent before the MLS group exists goes out transport-only and is
        dropped by E2EE receivers, so it is worth waiting a moment - but only
        when somebody is there to receive it.
        """
        if not self.dave.protocol_version:
            return
        # Nobody else is here, so no group can form - a group needs another
        # member - and there is no decryptor that could drop transport-only
        # media. Waiting could only run to its timeout, which is a minute.
        if not self.dave.connected_user_ids:
            return

        loop = asyncio.get_running_loop()
        deadline = loop.time() + DAVE_WAIT_SECONDS
        logged = False
        while not self.dave.ready_event.is_set():
            if self.dave.protocol_version == 0:
                return  # downgraded: transport-only is what the server wants
            if loop.time() > deadline:
                self.logger.warning(
                    "no DAVE group after %ss, streaming transport-only "
                    "(nobody else joined the session?)",
                    DAVE_WAIT_SECONDS,
                )
                return
            if not logged:
                self.logger.info("waiting for the DAVE group before sending media")
                logged = True
            await asyncio.sleep(0.25)

    async def set_video_state(
        self,
        state: bool,
        width: int = 1280,
        height: int = 720,
        framerate: int = 30,
        bitrate: "int | None" = None,
    ):
        if not state:
            return await self._send_json(
                VoiceOpCodes.VIDEO,
                {
                    "audio_ssrc": self.ssrc,
                    "video_ssrc": 0,
                    "rtx_ssrc": 0,
                    "streams": [],
                },
            )
        if state:
            self.video_resolution = (width, height)

        if bitrate is None:
            # The reference client hardcodes this ("we don't really know
            # anyway", BaseMediaConnection.ts:614) and encodes at 5000k with
            # 7000k as the ceiling. Announcing less than we encode gets the
            # stream throttled by the server.
            bitrate = ANNOUNCED_MAX_BITRATE

        self.logger.info(
            f"video stream: %sx%s @ %sfps, max_bitrate %s bps (ssrc: {self.video_ssrc},"
            f" rtx: {self.rtx_ssrc})",
            width,
            height,
            framerate,
            bitrate,
        )

        return await self._send_json(
            VoiceOpCodes.VIDEO,
            {
                "audio_ssrc": self.ssrc,
                "video_ssrc": self.video_ssrc,
                "rtx_ssrc": self.rtx_ssrc,
                "streams": [
                    {
                        "type": "video",
                        "rid": "100",
                        "ssrc": self.video_ssrc,
                        "active": True,
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
        )

    async def set_preview(
        self,
        session,
        api_endpoint: str,
        authorization_token: str,
        preview: bytes,
        preview_type: str = "image/jpeg",
    ):
        if self.stream_key is None:
            raise ValueError("Stream key for the stream connection is not set")

        async with session.post(
            f"{api_endpoint}/streams/{self.stream_key}/preview",
            headers={"Authorization": authorization_token},
            json={
                "thumbnail": (
                    f"data:{preview_type};base64,"
                    f"{base64.b64encode(preview).decode('utf-8')}"
                )
            },
        ) as response:
            return response.status == 204


class VoiceChannelConnection(StreamConnection):
    """Voice channel audio over the same UDP + DAVE transport. The MLS
    group id is the channel id (not rtc_server_id - 1) and the speaking
    indicator is 1."""

    SPEAKING_ACTIVE = 1


async def open_media_connection(session, connection_class, **kwargs):
    """Connect a media connection and return it transport-ready.

    The caller owns what it gets back: `await conn.close()`, or
    `async with await open_media_connection(...) as conn`.

    The websocket carries the voice session for as long as media flows, so
    it has to outlive this function - which is why the success path hands it
    to the connection rather than closing it. Only a failure has to clean
    up, and that is the whole of the teardown logic.
    """
    ws = await session.ws_connect(
        f"wss://{kwargs['endpoint']}/", params={"v": "8"}
    )
    try:
        await ws.send_json(
            {
                "op": VoiceOpCodes.IDENTIFY,
                "d": {
                    "server_id": kwargs.get("rtc_server_id")
                    or kwargs.get("guild_id")
                    or kwargs["channel_id"],
                    "user_id": kwargs["user_id"],
                    "session_id": kwargs["session_id"],
                    "token": kwargs["token"],
                    "max_dave_protocol_version": davey.DAVE_PROTOCOL_VERSION,
                    "video": True,
                    # "screen" asks for a Go Live stream, "video" for the
                    # camera on the voice channel's own session - the
                    # official client sends the latter when turning the
                    # camera on, and the server grants no video ssrc without it
                    "streams": [
                        {
                            "type": kwargs.get("stream_type", "screen"),
                            "rid": "100",
                            "quality": 100,
                        }
                    ],
                },
            }
        )

        conn = connection_class(
            ws,
            user_id=kwargs["user_id"],
            channel_id=kwargs["channel_id"],
            stream_key=kwargs.get("stream_key"),
            dave_channel_id=kwargs.get("dave_channel_id"),
            video_codec=kwargs.get("video_codec", "H264"),
            video_extension=kwargs.get("video_extension", True),
            camera=kwargs.get("camera", False),
        )

        async with asyncio.timeout(30):
            await conn.transport_ready.wait()
    except TimeoutError as e:
        await ws.close()
        raise RuntimeError(
            "Voice handshake timed out: no session description received."
        ) from e
    except BaseException:
        # anything else: release the socket we opened, then let it propagate
        await ws.close()
        raise

    return conn


async def create_stream_connection(session, **kwargs):
    """Connect a Go Live stream connection.

    `dave_channel_id` defaults to the voice channel id; the gateway passes
    rtc_server_id - 1 instead, which is the MLS group id for streams.
    """
    return await open_media_connection(session, StreamConnection, **kwargs)


async def create_voice_connection(session, **kwargs):
    """Voice channel media connection: audio, and camera video when asked.

    The stream type is "video" (camera) rather than "screen" (Go Live), so
    the server allocates a video ssrc on this session.
    """
    kwargs.setdefault("stream_type", "video")
    return await open_media_connection(session, VoiceChannelConnection, **kwargs)
