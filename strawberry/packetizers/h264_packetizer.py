import struct
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

    def send_frame(self, frame: bytearray):
        access_unit = frame
        nalus: list[bytearray] = []

        offset = 0

        while offset < len(access_unit):
            (nalu_size,) = struct.unpack_from(">I", access_unit, offset)
            offset += 4

            nalu = access_unit[offset : offset + nalu_size]
            nalus.append(nalu)
            offset += nalu_size

        for i, nalu in enumerate(nalus):
            is_last = i == len(nalus) - 1
            nal0 = nalu[0]

            if len(nalu) <= self.mtu:
                packet = self.conn.encrypt_data(
                    self.get_rtp_header(is_last),
                    bytearray(self.get_header_extension()) + nalu,
                )

                self.conn.send_packet(packet)
            else:
                chunks = list(partition_chunks(nalu[1:], self.mtu - 12))

                nal_type = nal0 & 0x1F
                fnri = nal0 & 0xE0

                default_header = bytes((0x1C | fnri,))

                for j, chunk in enumerate(chunks):
                    chunk_header = default_header
                    is_final_chunk = j == len(chunks) - 1

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
                            self.get_header_extension() + chunk_header + chunk,
                        )
                    )

        self.increment_timestamp(90000 / self.fps)
