from .base_packetizer import BaseMediaPacketizer


class AudioPacketizer(BaseMediaPacketizer):
    codec = "opus"

    def __init__(self, conn):
        super().__init__(conn, 0x78, False)
        self.frame_size = 48000 // 1000 * 20

    def send_frame(self, frame: bytearray):
        self.conn.send_packet(self.conn.encrypt_data(self.get_rtp_header(), frame))
        self.increment_timestamp(self.frame_size)
