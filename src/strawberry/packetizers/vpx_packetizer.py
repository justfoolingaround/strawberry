import struct
import typing
from collections.abc import Sequence

from strawberry.codecs import VIDEO_CODECS
from strawberry.utils import partition_chunks

from .base_packetizer import BaseMediaPacketizer

if typing.TYPE_CHECKING:
    from strawberry.connection.protocol import MediaConnection


class VPXPacketizer(BaseMediaPacketizer):
    """RFC 7741 (VP8) / RFC 9628 (VP9) RTP.

    VPx frames have no internal framing, so a frame is one blob that is
    sliced at the MTU. The shared `send_frame` signature takes a sequence
    of chunks like every other packetizer does; VPx just requires that it
    hold exactly one.
    """

    MEDIA = "video"

    #: bytes of payload descriptor prepended to every chunk by `payloads`.
    #: Overridden by subclasses; 0 means the raw frame goes out as-is.
    descriptor_size = 0

    def __init__(self, payload_type: int, extensions_enabled: bool = False):
        super().__init__(payload_type, extensions_enabled=extensions_enabled)
        self.fps = 30

    def payloads(
        self, conn: "MediaConnection", frame: bytes, chunks: "list[bytes]"
    ) -> "list[bytes]":
        """Build each packet's payload; subclasses prepend a descriptor.

        `conn` is passed because a descriptor may need connection state -
        VP9 carries the frame resolution, which VP9 headers do not.
        """
        return chunks

    def send_frame(self, conn: "MediaConnection", chunks: Sequence[bytes]):
        # VPx frames have no internal framing, so there is exactly one per
        # frame. Accept the frame itself or a one-element list holding it
        # (callers batch per access unit for H26x, so both shapes arrive) -
        # but reject anything longer rather than silently slicing it into a
        # single "frame", which would put two pictures in one timestamp.
        if len(chunks) != 1:
            raise ValueError(
                f"{type(self).__name__} takes exactly one frame per call, "
                f"got {len(chunks)}"
            )
        frame = chunks[0]
        # The descriptor is prepended to every chunk *after* the split, so the
        # split has to leave room for it or the finished packet is
        # `descriptor_size` bytes over the MTU. Subclasses declare that size;
        # without it a VP9 packet went out at 1242 bytes against a 1200 limit,
        # which the path then fragmented or dropped.
        room = self.chunk_room(conn)
        self.emit(
            conn,
            self.payloads(conn, frame, list(partition_chunks(frame, room))),
        )
        self.advance_timestamp()

    def chunk_room(self, conn: "MediaConnection") -> int:
        """How many bytes of frame data fit in one packet."""
        return self.mtu - self.descriptor_size


class VP8Packetizer(VPXPacketizer):
    """VP8 RTP, ported from the working Discord sender in
    samples/Discord-experiments/codecs/vp8.js.

    Discord expects, on every packet:
    - the extension bit set with the id 5 extension ("REQUIRED every
      packet" per that file, and it is what Discord's own client sends),
    - a 4 byte payload descriptor: X=1 (extended control bits follow),
      the S bit on the frame's first packet, the control byte, and a
      15 bit picture ID that increments per *frame*.
    """

    codec = VIDEO_CODECS["VP8"].name

    #: |X| extended control bit + control byte + 15 bit picture id
    descriptor_size = 4

    def __init__(self, extensions_enabled: bool = True):
        super().__init__(
            VIDEO_CODECS["VP8"].payload_type, extensions_enabled=extensions_enabled
        )
        self.picture_id = 0

    def payloads(
        self, conn: "MediaConnection", frame: bytes, chunks: "list[bytes]"
    ) -> "list[bytes]":
        control = 0x80  # I=1: a picture ID follows
        # 15 bit picture ID, top bit marks the extension as present
        picture = ((self.picture_id & 0x7FFF) | 0x8000).to_bytes(2, "big")
        try:
            # S is set on the frame's first packet and PID stays 0, exactly as
            # the working sender does. libvpx splits keyframes into a header
            # and a data partition, so the "true" S=0/PID=1 would disagree
            # with every VP8 receiver we can observe; Discord reassembles by
            # fragment boundaries instead.
            return [
                bytes((0x80 | (0x10 if index == 0 else 0x00), control))
                + picture
                + chunk
                for index, chunk in enumerate(chunks)
            ]
        finally:
            # the picture ID advances once per frame, not per packet
            self.picture_id = (self.picture_id + 1) & 0x7FFF


class VP9Packetizer(VPXPacketizer):
    """VP9 needs a payload descriptor ahead of every packet: the
    depacketizer reads it to find frame boundaries. DAVE leaves VP9 frames
    fully encrypted, so the descriptor is the only thing a receiver can
    parse. The id 5 extension goes on every packet too, like H264 and the
    VP8 sender in Discord-experiments."""

    codec = VIDEO_CODECS["VP9"].name

    # descriptor bits, RFC 9628 figure 3: |I|P|L|F|B|E|V|Z|
    # Note B precedes E here; the older drafts had them the other way
    # round, which makes every frame look like it ends on its first packet
    # and starts on its last - nothing ever assembles.
    I_PID = 0x80
    P_INTER = 0x40
    L_LAYERS = 0x20
    F_FLEXIBLE = 0x10
    B_START = 0x08
    E_END = 0x04
    V_SS = 0x02
    Z = 0x01

    # RFC 9628 figure 4: | N_S (3) | Y | G | reserved (3) |
    # so Y (resolution present) is bit 4 and G (picture group) bit 3.
    # One spatial layer (N_S = 0), Y = 1, G = 0 -> 0b0001_0000.
    SS_ONE_LAYER_RESOLUTION = 0x10

    def __init__(self, extensions_enabled: bool = True):
        super().__init__(
            VIDEO_CODECS["VP9"].payload_type, extensions_enabled=extensions_enabled
        )

    @staticmethod
    def is_inter_frame(frame: bytes) -> bool:
        """frame_type out of the VP9 uncompressed header.

        Verified against ffprobe's pict_type: for profiles 0..2 the bits run
        frame_marker(2) profile_low(1) profile_high(1) show_existing(1)
        frame_type(1), so frame_type is bit 2. Profile 3 inserts reserved_zero
        plus three profile bits, pushing it into byte 1, bit 6.
        """
        first = frame[0]
        if ((first >> 4) & 0b11) < 3:
            return bool((first >> 2) & 1)
        return bool((frame[1] >> 6) & 1)

    def descriptor(self, frame: bytes, is_first: bool, is_last: bool) -> int:
        value = 0
        if self.is_inter_frame(frame):
            value |= self.P_INTER
        if is_first:
            value |= self.B_START
        if is_last:
            value |= self.E_END
        return value

    def scalability_structure(self, resolution) -> bytes:
        """RFC 9628 SS data block with an explicit frame resolution.

        VP9 carries no dimensions in its headers (profile 0) and RFC 9628's
        descriptor has nowhere else to put them, so a receiver has no way to
        size the picture unless we spell them out here. One spatial layer,
        Y set (resolution present), G clear (no picture group description).
        """
        width, height = resolution
        return bytes((self.SS_ONE_LAYER_RESOLUTION,)) + struct.pack(
            ">HH", width, height
        )

    def chunk_room(self, conn: "MediaConnection") -> int:
        # The SS block rides on the frame's first packet, so the split has to
        # leave room for the widest packet the frame will produce.
        overhead = 1 + (self.ss_size(conn) if self._has_ss(conn) else 0)
        return self.mtu - overhead

    def _has_ss(self, conn: "MediaConnection") -> bool:
        return getattr(conn, "video_resolution", None) is not None

    @staticmethod
    def ss_size(conn: "MediaConnection") -> int:
        # one SS descriptor byte + 16 bit width + 16 bit height, one spatial layer
        return 1 + 4

    def payloads(
        self, conn: "MediaConnection", frame: bytes, chunks: "list[bytes]"
    ) -> "list[bytes]":
        resolution = getattr(conn, "video_resolution", None)
        last = len(chunks) - 1
        payloads = []
        for index, chunk in enumerate(chunks):
            value = self.descriptor(frame, index == 0, index == last)
            if resolution and index == 0:
                # On the first packet only. RFC 9628 puts the SS block there,
                # and that is where the reference sends it (vp9.rs
                # packetize_with_info: `first && info.resolution.is_some()`).
                # Repeating it on every packet put 5 bytes of resolution in
                # front of each one, which is both wasted MTU and a second
                # thing for a receiver to mis-parse.
                value |= self.V_SS
                payloads.append(
                    bytes((value,)) + self.scalability_structure(resolution) + chunk
                )
                continue
            payloads.append(bytes((value,)) + chunk)
        return payloads
