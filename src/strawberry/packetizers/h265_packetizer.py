"""HEVC RTP (RFC 7798).

`H265Packetizer` is an `H264Packetizer` and inherits the frame loop, the
marker bit and the RTP plumbing unchanged. `iter_fragments_from_nalu` is
reimplemented in full rather than shared: AVC and HEVC agree on the idea
and on almost nothing else, and the version that tried to share it
through a header width and a type width is exactly the one that made the
old external-iterator design hard to debug.

What differs, and why it is not a parameter:

===============  ==============================  ==============================
                 H.264 (RFC 6184 FU-A)          H.265 (RFC 7798 FU)
===============  ==============================  ==============================
NAL header       1 byte                         2 bytes
nal_type         ``h[0] & 0x1F``  (5 bits)     ``(h[0] >> 1) & 0x3F`` (6)
FU indicator     ``0x1C | (h[0] & 0xE0)``      ``((h[0] & 0x81) | 49<<1, h[1])``
FU header        ``S|E|R | type(5)``            ``S|E | FuType(6)``
FU prefix        2 bytes                        3 bytes
nalu_data        ``nalu[1:]``                   ``nalu[2:]``
===============  ==============================  ==============================
"""

import typing
from collections.abc import Iterator

from strawberry.codecs import VIDEO_CODECS

from .h264_packetizer import H264Packetizer

if typing.TYPE_CHECKING:
    pass


class H265Packetizer(H264Packetizer):
    """HEVC RTP (RFC 7798)."""

    MEDIA = "video"
    codec = VIDEO_CODECS["H265"].name

    #: FU, the HEVC fragmentation unit indicator (RFC 7798 4.4.3). The NAL
    #: type becomes 49; every other field comes from the NAL unit sent.
    FU_TYPE = 49

    def __init__(self, extensions_enabled: bool = True) -> None:
        super().__init__(
            extensions_enabled=extensions_enabled, codec=VIDEO_CODECS["H265"]
        )

    def iter_fragments_from_nalu(self, nalu: bytes) -> "Iterator[tuple[bool, bytes]]":
        """FU fragments, per RFC 7798 4.4.3.

            +---------------+---------------+
            |F|   Type    |  LayerId  | TID |     PayloadHdr
            +-------------+-----------------+

        becomes type 49, and the FU header is one more byte:

            +---------------+
            |S|E|    Type   |
            +---------------+

        Three bytes of prefix and the two byte NAL header is dropped, which
        is the only thing this shares with AVC: the shape, not the code.
        """
        # the two byte HEVC NAL header
        nalu_header = nalu[:2]
        nalu_data = nalu[2:]
        nal_type = (nalu_header[0] >> 1) & 0x3F
        # RFC 7798 4.4.3: "All other fields except Type come from the NAL
        # being transmitted" - keep F and TID, take LayerId from the NAL.
        default_header = bytes(
            ((nalu_header[0] & 0x81) | (self.FU_TYPE << 1), nalu_header[1])
        )

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
