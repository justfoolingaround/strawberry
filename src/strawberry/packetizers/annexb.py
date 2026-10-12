"""Frame sources and the Annex B packetizer hierarchy.

`AnnexBPacketizer` owns everything between "bytes on a stream" and "RTP
payloads": the Annex B split, the access unit boundary, and the fragment
header. `H264Packetizer` fills in the AVC details and `H265Packetizer`
overrides only the three things HEVC does differently - how a NAL unit is
split, how the fragment indicator is built, and how the type is read out.

The split used to live in the source layer, which meant the IVFPacketizer
iterated bytes while the NAL packetizers iterated a list, and neither could
be exercised on its own. Here both halves are the same object: give it a
stream and it produces access units; give it an access unit and it
produces packets.
"""

from collections.abc import Iterator, Sequence
from typing import IO, TYPE_CHECKING, Any

from strawberry.codecs import VideoCodec

if TYPE_CHECKING:
    from strawberry.connection.protocol import MediaConnection

from strawberry.containers.annexb import (
    ANNEX_B_START_CODE,
    NAL_START_CODE,
    NalPacketIterator,
)
from strawberry.containers.ivf import IvfFrameIterator

from .base_packetizer import BaseMediaPacketizer


def iter_access_units(
    stream: "IO[bytes]", codec: VideoCodec
) -> "Iterator[list[bytes] | bytes]":
    """Access units from a byte stream, in the shape the codec needs.

    H26x yields a list of NAL units per access unit; VPx/AV1 yield whole
    frames, because their framing has no notion of a NAL unit on the wire.
    """
    read = getattr(stream, "read1", None) or stream.read
    if codec.nal is not None:
        parser: Any = NalPacketIterator(codec.nal)
        step = parser.iter_access_units
    else:
        parser = IvfFrameIterator()
        step = parser.iter_frames
    while chunk := read(65536):
        yield from step(chunk)


# -- the packetizer hierarchy ---------------------------------------------


class AnnexBPacketizer(BaseMediaPacketizer):
    """Owns demux to Annex B, then to RTP payloads.

    Subclasses say how their codec differs; everything else - the access
    unit split, the single NAL versus fragment decision, the S/E bits - is
    shared. `iter_access_units` is the debug entry point: it runs the whole
    demux half without a connection, so a frame can be inspected as NAL
    units before anything reaches a socket.
    """

    MEDIA = "video"

    #: header of the fragment indicator that replaces a NAL type
    FU_TYPE = 28  # H.264 FU-A

    def __init__(
        self,
        codec: VideoCodec,
        extensions_enabled: bool = True,
    ) -> None:
        super().__init__(codec.payload_type, extensions_enabled=extensions_enabled)
        self.spec = codec
        self.fps = 30

    def describe_payload(self, body: bytes) -> str:
        if not body:
            return "empty"
        spec = self.spec.nal
        if spec is None:
            return "opaque"
        nal_type = (body[0] >> spec.shift) & spec.mask
        if nal_type != self.FU_TYPE:
            return f"single type={nal_type}"
        if spec.header_size == 2:
            # HEVC: 2 byte NAL header, then S|E|FuType
            if len(body) < 3:
                return "FU truncated"
            return (
                f"FU start={body[2] >> 7} end={(body[2] >> 6) & 1} "
                f"type={body[2] & 0x3F} layer={(body[1] >> 1) & 0x3F} "
                f"tid={(body[1] & 7) + 1}"
            )
        # AVC: 1 byte indicator, then S|E|R|Type
        if len(body) < 2:
            return "FU truncated"
        return (
            f"FU-A start={body[1] >> 7} end={(body[1] >> 6) & 1} type={body[1] & 0x1F}"
        )

    def iter_fragments_from_nalu(self, nalu: bytes) -> "Iterator[tuple[bool, bytes]]":
        """Fragment one NAL unit. Each codec implements this in full.

        Yielding ``(is_final_chunk, fragment)`` is the whole contract; the
        two Annex B codecs disagree on enough of the wire format - header
        width, type width, indicator bytes, data offset - that sharing it
        through a bit width or a lookup turned a readable packetizer into
        something nobody could reason about.
        """
        raise NotImplementedError

    def iter_access_units(self, stream: "IO[bytes]") -> "Iterator[list[bytes]]":
        """Debug entry point: the NAL units this packetizer would send.

        Deliberately connection-free, so a frame can be dumped and compared
        without a socket, an SRTP key, or a running stream.
        """
        for unit in iter_access_units(stream, self.spec):
            if isinstance(unit, bytes):
                # whole frames (VPx/AV1): a unit is one access unit
                yield [unit]
            else:
                yield unit

    def send_frame(self, conn: "MediaConnection", chunks: "Sequence[bytes]") -> None:
        """One access unit in, RTP packets out.

        A NAL that fits goes out whole; anything larger is fragmented. That
        is the whole rule, and it is the rule both reference
        implementations use:

            // samples/discord-native-voice src/video/h265.rs
            if nalu.len() <= self.state.mtu() { single NAL packet } else { FU }

            # this project's previous packetizer
            if len(nalu) <= self.mtu: single NAL packet

        This used to add "and is not picture data", fragmenting every VCL
        NAL however small. Nothing else does that: it turned a slice that
        fits into an FU carrying type 49, where the reference sends the same
        slice as a single NAL packet carrying its real type (1, 19, 20...).
        The rule's stated reason was that a fragmented NAL puts the type in a
        header built before encryption - but a single NAL packet carries the
        NAL header in the clear through DAVE as well, so there is nothing to
        protect, and the far end has to reassemble the frame to decrypt it
        either way.

        The marker bit, the accounting and the timestamp step are shared;
        only the fragmenting itself is per codec.
        """
        payloads: list[bytes] = []

        for nalu in chunks:
            if len(nalu) <= self.mtu:
                payloads.append(nalu)
                continue
            payloads.extend(
                fragment for _, fragment in self.iter_fragments_from_nalu(nalu)
            )

        self.emit(conn, payloads)
        self.advance_timestamp()


