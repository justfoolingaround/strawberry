import struct
import typing

from strawberry.utils import checked_add

if typing.TYPE_CHECKING:
    from strawberry.connection import UDPConnection


class BaseMediaPacketizer:
    codec: str

    MAX_INT_16 = 1 << 16
    MAX_INT_32 = 1 << 32

    def __init__(
        self,
        conn: "UDPConnection",
        payload_type: int,
        extensions_enabled: bool = False,
    ):
        self.conn = conn
        self.payload_type = payload_type
        self.sequence = 0
        self.mtu = 1200
        self.extensions_enabled = extensions_enabled
        self.timestamp = 0

        self.ssrc = 0

    def send_frame(self, _: bytearray):
        raise NotImplementedError

    def get_new_sequence(self):
        self.sequence = checked_add(self.sequence, 1, self.MAX_INT_16)
        return self.sequence

    def increment_timestamp(self, increment):
        self.timestamp = checked_add(self.timestamp, int(increment), self.MAX_INT_32)

    def get_rtp_header(self, is_last: bool = True):
        header = bytearray(12)

        header[0] = 2 << 6 | (int(self.extensions_enabled) << 4)
        header[1] = self.payload_type

        if is_last:
            header[1] |= 0b10000000

        struct.pack_into(
            ">HII", header, 2, self.get_new_sequence(), self.timestamp, self.ssrc
        )
        return header

    def get_header_extension(self):
        profile = bytearray(4)

        extensions_enabled = [
            {
                "id": 5,
                "len": 2,
                "val": 0,
            }
        ]

        profile[0] = 0xBE
        profile[1] = 0xDE

        struct.pack_into(">H", profile, 2, len(extensions_enabled))

        for extension in extensions_enabled:
            data = bytearray(4)

            data[0] = (extension["id"] & 0b00001111) << 4
            data[0] |= (extension["len"] - 1) & 0b00001111

            struct.pack_into(">H", data, 1, extension["val"])
            profile += data

        return profile
