"""AVC RTP (RFC 6184).

The frame loop, the marker bit and the RTP plumbing are inherited from
`AnnexBPacketizer`. Only the fragmenter is AVC's, and it is written out in
full: the NAL header is one byte, the type is five bits, the indicator is
`0x1C | fnri`, and the data starts one byte in.
"""

import typing
from collections.abc import Iterator

from strawberry.codecs import VIDEO_CODECS, VideoCodec

from .annexb import AnnexBPacketizer

if typing.TYPE_CHECKING:
    pass


class H264Packetizer(AnnexBPacketizer):
    """AVC RTP (RFC 6184).

    VCL NAL units are always fragmented, even when they would fit: after
    DAVE the slice body is ciphertext, so a single NAL packet would leave
    a receiver nothing to parse. FU-A puts the type in a header we build
    before encryption.
    """

    MEDIA = "video"
    codec = VIDEO_CODECS["H264"].name

    #: FU-A, the AVC fragmentation unit indicator (RFC 6184 5.8)
    FU_A_NAL_TYPE = 0x1C

    def __init__(
        self,
        extensions_enabled: bool = True,
        codec: "VideoCodec | None" = None,
    ) -> None:
        super().__init__(codec or VIDEO_CODECS["H264"], extensions_enabled=extensions_enabled)

    def iter_fragments_from_nalu(self, nalu: bytes) -> "Iterator[tuple[bool, bytes]]":
        """FU-A fragments, per RFC 6184 5.8.

            +---------------+       +---------------+
            |0|1|2|3|4|5|6|7|       |0|1|2|3|4|5|6|7|
            +---------------+       +===============+
            |F|NRI|  Type  |       |S|E|R|  Type  |
            +---------------+       +---------------+

        The indicator keeps F and NRI from the NAL header; the FU header
        carries the original type with the start and end bits on top. Two
        bytes of prefix, and the NAL header itself is dropped.
        """
        nal_header = nalu[0]
        nalu_data = nalu[1:]
        nal_type = nal_header & 0x1F
        fnri = nal_header & 0xE0
        default_header = bytes((self.FU_A_NAL_TYPE | fnri,))

        fragments = self.split_to_mtu(nalu_data, len(default_header) + 1)
        last = len(fragments) - 1
        for index, nalu_fragment in enumerate(fragments):
            chunk_header = default_header
            is_final_chunk = index == last
            if index == 0:
                chunk_header += bytes((0x80 | nal_type,))
            elif is_final_chunk:
                chunk_header += bytes((0x40 | nal_type,))
            else:
                chunk_header += bytes((nal_type,))
            yield is_final_chunk, chunk_header + nalu_fragment
