import struct
import time
import typing
from collections.abc import Iterable, Sequence

from strawberry.protocols.rtcp import RTCP_HEADER_LEN, sender_report
from strawberry.utils import checked_add, partition_chunks

if typing.TYPE_CHECKING:
    from strawberry.connection.protocol import MediaConnection



def _extension_string(value: str, fallback: str) -> bytes:
    """A value for a text extension element, ASCII only, at most 16 bytes.

    Same rule as the reference's `rtp_extension_string`: keep the ASCII
    bytes, cap at 16, fall back to the default when there is nothing left.
    """
    encoded = bytes(byte for byte in value.encode("ascii", "ignore")[:16])
    return encoded or fallback.encode("ascii")[:16]


def _abs_send_time() -> bytes:
    """24-bit abs-send-time: milliseconds since the epoch, truncated."""
    millis = int(time.time() * 1000)
    return struct.pack(">I", millis & 0xFFFFFF)[1:]


class BaseMediaPacketizer:
    """Turns encoded frames into RTP packets.

    The connection is handed over per call rather than kept as a member:
    packetizers hold no reference to a connection, so they cannot keep one
    alive or form a cycle with it.
    """

    # Overridden per codec; declared here so the shared helpers below can
    # rely on both existing.
    codec: str = ""
    fps: float = 30.0
    MEDIA = "audio"
    # The reference implementation always sends the full block
    # (samples/discord-native-voice build_video_extension_payload) and its
    # AV1 works, so this defaults on. --no-rich-rtp-extension goes back to
    # the playout-delay element alone.
    rich_extension = True

    MAX_INT_16 = 1 << 16
    MAX_INT_32 = 1 << 32

    #: Discord's MTU for a voice datagram. This bounds the *whole* datagram
    #: as it leaves the socket, and it is enforced strictly: the payload
    #: budget is whatever is left after every byte of framing, so a finished
    #: packet is never larger than this.
    MTU = 1200

    #: rtpsize transport overhead: the AEAD tag and the nonce counter that
    #: follow the ciphertext on every packet.
    TRANSPORT_OVERHEAD = 16 + 4

    #: Stream identity carried in the header extension. `rid` is the one the
    #: server allocated (`'rid': '100'` in the Ready payload) and
    #: `media_stream_type` is "video" or "screen".
    rid = "100"
    media_stream_type = "video"
    #: Set on a retransmission, which carries the repaired-RID element
    #: instead of the RID one.
    repaired = False

    #: RTP clock for this stream, in Hz. A Sender Report's RTP timestamp is
    #: in these units, so the report cadence is derived from it.
    clock_rate = 90000

    def __init__(self, payload_type: int, extensions_enabled: bool = False):
        self.payload_type = payload_type
        self.sequence = 0
        self.extensions_enabled = extensions_enabled
        self.timestamp = 0

        self.ssrc = 0

        first_byte = 0x80 | (0x10 if extensions_enabled else 0x00)
        self._header_prefix = (
            bytes((first_byte, payload_type)),
            bytes((first_byte, payload_type | 0x80)),
        )

        # Fixed one-byte RTP header extension (RFC 5285): 0xBEDE profile,
        # transport-wide sequence number, for the transport-cc element
        self.transport_cc = 0
        self._payload_budget = self._compute_payload_budget()

        # Sender Report accounting: RFC 3550 wants the running packet and
        # octet counts and the wall clock, so they are kept here rather than
        # reconstructed at report time.
        self._packets_sent = 0
        self._bytes_sent = 0
        self._last_packet_ms = 0
        # Media second the last report described. -1 so the first frame of a
        # stream reports immediately, which is when a receiver most needs to
        # learn the mapping.
        self._sr_media_second = -1

    # -- the MTU ------------------------------------------------------------

    def _compute_payload_budget(self) -> int:
        """Media bytes that fit in one MTU-sized datagram.

        Everything the receiver has to strip before it sees the payload comes
        off the top: the RTP header, the extension preamble and body, and the
        rtpsize tag and counter. Budgeting the *payload* at the full MTU is
        what put every packet 56 bytes over the limit, so the budget is
        whatever is left, and `mtu` stays the hard cap on the finished
        datagram.
        """
        overhead = 12 + self.TRANSPORT_OVERHEAD  # RTP header + rtpsize
        if self.extensions_enabled:
            overhead += 4 + len(self._build_extension(0))
        return self.MTU - overhead

    @property
    def payload_budget(self) -> int:
        """Media bytes per packet; `MTU` minus all framing."""
        return self._payload_budget

    @property
    def extension_size(self) -> int:
        """On-wire length of the header extension in front of every payload.

        Side effect free, unlike `header_extension`: that property advances
        the transport-wide sequence number, so measuring the block with it
        would burn a sequence number per call. Computed fresh rather than
        cached because `rid`, `media_stream_type` and `repaired` are all set
        after construction.
        """
        if not self.extensions_enabled:
            return 0
        return len(self._build_extension(0))

    @property
    def mtu(self) -> int:
        """Payload budget, kept under the `mtu` name every packetizer uses.

        `MTU` is the datagram limit and is enforced strictly; this is what is
        left for the codec's own bytes once the framing has been subtracted,
        so `split_to_mtu` / `partition_chunks(..., self.mtu)` cannot produce
        an over-MTU packet.
        """
        return self._payload_budget

    @mtu.setter
    def mtu(self, value: int) -> None:
        """Treat an explicit `mtu` as a payload budget, as before."""
        self._payload_budget = max(1, int(value))

    def _build_extension(self, transport_sequence: int) -> bytes:
        """The extension block for one packet (see `header_extension`)."""
        if not self.rich_extension:
            return bytes((0xBE, 0xDE, 0x00, 0x01, 0x51, 0x00, 0x00, 0x00))

        block = bytearray()

        def element(ext_id: int, data: bytes) -> None:
            block.append((ext_id << 4) | (len(data) - 1))
            block.extend(data)

        # The reference sends nine elements here
        # (samples/discord-native-voice src/python/transport.rs
        # build_video_extension_payload) and its H265 is accepted, so all
        # nine go out. The four that were missing are the identifying ones:
        # content type, timing, media stream type and the RID. A stream is
        # allocated with an `rid` - the Ready payload says `'rid': '100'` -
        # and stamping it on every packet is how the far end ties a packet
        # back to the stream it belongs to.
        element(2, b"\x00\x00\x00")  # transmission offset
        element(3, _abs_send_time())  # abs-send-time, from the wall clock
        element(4, b"\x00")  # video orientation
        element(5, struct.pack(">H", transport_sequence))  # transport-cc
        element(6, b"\x00\x00\x00")  # playout delay
        element(7, b"\x01" if self.media_stream_type == "screen" else b"\x00")
        element(8, b"\x00" * 13)  # video timing
        element(10, _extension_string(self.media_stream_type, "video"))
        if self.repaired:
            element(12, _extension_string(self.rid, "100"))
        else:
            element(11, _extension_string(self.rid, "100"))
        block.extend(b"\x00" * (-len(block) % 4))  # pad to the word length
        return bytes((0xBE, 0xDE)) + struct.pack(">H", len(block) // 4) + bytes(block)

    @property
    def header_extension(self) -> bytes:
        """The RTP header extension block that precedes every video payload.

        The default is the single playout-delay element Discord's client
        negotiates as id 5 (CodecPayloadType / BaseMediaConnection SDP:
        a=extmap:5 .../playout-delay).

        --rich-rtp-extension switches to the full set the reference
        implementation sends (samples/discord-native-voice
        src/python/transport.rs build_video_extension_payload).

        Its element ids are its own (2 toffset, 3 abs-send-time,
        4 orientation, 5 transport-cc, 6 playout). Discord's own offer
        (BaseMediaConnection.ts) numbers them differently: 2 abs-send-time,
        3 transport-wide-cc, 5 playout-delay, 13 orientation, 14 toffset.
        Which set is right is untested - the one H265 run so far was already
        broken, so it said nothing about the rich block either way.
        """
        block = self._build_extension(self.transport_cc)
        if self.rich_extension:
            # One value per packet: the transport-wide sequence number has to
            # advance for every packet, not once per frame.
            self.transport_cc = (self.transport_cc + 1) & 0xFFFF
        return block

    def send_frame(self, conn: "MediaConnection", chunks: Sequence[bytes]):
        """Packetize one encoded frame and send it.

        `chunks` is the frame in whatever units the codec frames itself:
        H264 and H265 pass their NAL units, VP8/VP9/AV1 pass a single
        element holding the whole frame. One signature for every codec, so
        the connection never has to know which is which.
        """
        raise NotImplementedError

    # -- shared packet emission --------------------------------------------

    def emit(self, conn: "MediaConnection", payloads: Iterable[bytes]) -> None:
        """Send pre-built RTP payloads as one frame.

        Handles the parts every codec repeats: the header extension goes in
        front of each payload, the marker bit goes on the last packet of the
        frame, and each packet is transport-encrypted and accounted for.
        Subclasses build the payload list and let this do the rest.
        """
        payloads = list(payloads)
        last = len(payloads) - 1
        for index, payload in enumerate(payloads):
            # Rebuilt per packet, not hoisted out of the loop: the extension
            # block carries the transport-wide sequence number, which RFC 5285
            # defines as incrementing once per *packet* across every stream.
            # Building it once per frame gave every packet of a frame the same
            # value, so the receiver's bandwidth estimator saw a stream where
            # only one sequence number ever existed and a keyframe's 300+
            # packets looked like a single transmission.
            extension = self.header_extension
            self._send_media(
                conn, self.get_rtp_header(index == last), extension + payload
            )

        # At the frame boundary, never mid-frame: a report's packet and octet
        # counts describe the stream so far, and the reference reports once
        # per frame too. Reporting per packet would put a report between two
        # fragments of a key frame and report a count that is still changing.
        self._maybe_send_sender_report(conn)

    # -- sending one packet -------------------------------------------------

    def _send_media(self, conn, header: bytes, payload: bytes) -> None:
        """Dump if asked, encrypt, and send one packet.

        The dump has to happen here, on the plaintext, and only the packetizer
        knows how to read it - so this is the one place both the frame path and
        the audio path go through.
        """
        if getattr(conn, "dump_rtp", False):
            print(self.describe(header + payload), flush=True)
        self._send_packet(conn, conn.encrypt_data(header, payload))

    # -- describing a packet (--dump-rtp) -----------------------------------

    def describe(self, packet: bytes) -> str:
        """One line for `--dump-rtp`: the fields a receiver keys off.

        The packetizer owns this because it is the thing that knows how its own
        payload is framed. The connection sees ciphertext and has no idea which
        codec wrote it, which is why this used to be a table of NAL types and
        payload header sizes living next to the socket.
        """
        payload_type = packet[1] & 0x7F
        marker = "M" if packet[1] & 0x80 else "-"
        sequence, timestamp, ssrc = struct.unpack(">HII", packet[2:12])
        # everything after the RTP header: the extension block, then media
        body = packet[12 + self.extension_size :]
        return (
            f"RTP pt={payload_type} ssrc={ssrc} seq={sequence} ts={timestamp} "
            f"{marker} len={len(packet) - 12} {self.describe_payload(body)}"
        )

    def describe_payload(self, body: bytes) -> str:
        """What the media bytes are, in this codec's own terms.

        The default says nothing: a codec that frames itself (VP8/VP9) has no
        structure here that is worth decoding without the bitstream.
        """
        return f"opaque {len(body)}B"

    def split_to_mtu(self, data: bytes, overhead: int) -> list[bytes]:
        """Chop `data` into pieces that fit the MTU once `overhead` is added.

        A piece that would end up larger than the MTU but only just so is
        split in half instead, because a single fragment carrying both the
        start and the end bit is not a legal FU (RFC 6184 5.8 / RFC 7798
        4.4.3). Splitting in two keeps the start and the end apart.
        """
        budget = self.mtu - overhead
        if len(data) <= budget:
            # oversized: halve so the S and E bits land on different packets
            budget = -(-len(data) // 2)
        return list(partition_chunks(data, budget))

    def advance_timestamp(self) -> None:
        """Step the RTP clock by one frame at the negotiated frame rate."""
        self.increment_timestamp(90000 / self.fps)

    def _send_packet(self, conn: "MediaConnection", packet: bytes):
        conn.send_packet(packet, self.MEDIA)
        # self.sequence is the sequence number just used by get_rtp_header
        conn.note_rtp_packet(self.MEDIA, len(packet), self.ssrc, self.sequence)

        self._packets_sent += 1
        self._bytes_sent += len(packet)
        self._last_packet_ms = int(time.time() * 1000)

    def _maybe_send_sender_report(self, conn: "MediaConnection") -> None:
        """Report once per second of media time, as the reference does.

        Its cadence is whole seconds of *media* time, not a packet count:

            if media_time_ms // 1000 <= last_rtcp_media_time_ms // 1000:
                return

        so a stream that suddenly sends a 300 packet key frame still produces
        one report for that second rather than a burst of them. The RTP
        timestamp is the media clock, so whole seconds of it are what is
        compared - no separate timer needed, and it stays right whether the
        stream is running at real time or not.
        """
        media_second = self.timestamp // self.clock_rate
        if media_second <= self._sr_media_second:
            return
        self.send_sender_report(conn)

    def send_sender_report(self, conn: "MediaConnection") -> None:
        """Send one RTCP Sender Report.

        Sent over the media socket like any other packet, but with no media
        tag: RTCP is control traffic and must not be held behind the video
        pacer, or a report due during a long key frame arrives after the
        interval it was describing.
        """
        self._sr_media_second = self.timestamp // self.clock_rate
        conn.send_packet(self.get_rtcp_sender_report(conn))

    def get_rtcp_sender_report(self, conn: "MediaConnection") -> bytes:
        """An RFC 3550 Sender Report for this stream.

        Layout is the fixed 28-byte header and sender info with no report
        blocks:

            V=2|P=0|RC=0 | PT=200 |       length=6        |     SSRC
            NTP timestamp (msw)      | NTP timestamp (lsw)
            RTP timestamp            | sender's packet count
            sender's octet count

        The NTP field is what ties the RTP clock to a wall clock, and the
        counts are what a forwarding middlebox uses to keep its own
        accounting straight. Without this the stream has no declared timing
        reference at all.
        """
        report = sender_report(
            self.ssrc,
            self.timestamp,
            self._packets_sent,
            self._bytes_sent,
            self._last_packet_ms,
        )
        # The transport authenticates the first eight bytes and encrypts the
        # rest, so the split has to happen here rather than in the builder.
        return conn.encrypt_data(report[:RTCP_HEADER_LEN], report[RTCP_HEADER_LEN:])

    def get_new_sequence(self):
        self.sequence = checked_add(self.sequence, 1, self.MAX_INT_16)
        return self.sequence

    def increment_timestamp(self, increment):
        self.timestamp = checked_add(self.timestamp, int(increment), self.MAX_INT_32)

    def get_rtp_header(self, is_last: bool = True) -> bytes:
        return self._header_prefix[is_last] + struct.pack(
            ">HII", self.get_new_sequence(), self.timestamp, self.ssrc
        )
