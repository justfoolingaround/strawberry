import math
import typing

from strawberry.utils import partition_chunks

from .base_packetizer import BaseMediaPacketizer

if typing.TYPE_CHECKING:
    from strawberry.connection import UDPConnection


class H264Packetizer(BaseMediaPacketizer):
    codec = "H264"

    def __init__(self, conn: "UDPConnection"):
        super().__init__(conn, 0x65, True)

        self.fps = 30

    def send_frame(self, nalus: list[bytes]):
        for i, nalu in enumerate(nalus):
            is_last = i == len(nalus) - 1

            if len(nalu) <= self.mtu:
                self.conn.send_packet(
                    self.conn.encrypt_data(
                        self.get_rtp_header(is_last),
                        self.get_header_extension() + nalu,
                    )
                )

            else:
                nal0 = nalu[0]
                chunks_count = math.ceil((len(nalu) - 1) / self.mtu)

                nal_type = nal0 & 0x1F
                fnri = nal0 & 0xE0

                default_header = bytes((0x1C | fnri,))

                for j, nal_fragment in enumerate(partition_chunks(nalu[1:], self.mtu)):
                    chunk_header = default_header
                    is_final_chunk = j == chunks_count - 1

                    if j == 0:
                        chunk_header += bytes((0x80 | nal_type,))
                    else:
                        if is_final_chunk:
                            chunk_header += bytes((0x40 | nal_type,))
                        else:
                            chunk_header += bytes((nal_type,))

                    self.conn.send_packet(
                        self.conn.encrypt_data(
                            self.get_rtp_header(is_final_chunk and is_last),
                            self.get_header_extension() + chunk_header + nal_fragment,
                        )
                    )

        self.increment_timestamp(90000 / self.fps)
