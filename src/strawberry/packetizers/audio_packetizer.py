from collections.abc import Sequence

from strawberry.codecs import OPUS_PAYLOAD_TYPE

from .base_packetizer import BaseMediaPacketizer


class AudioPacketizer(BaseMediaPacketizer):
    codec = "opus"

    FRAME_SIZE = 960  # 20ms @ 48kHz

    #: opus RTP timestamps count 48kHz samples, not 90kHz video ticks
    clock_rate = 48000

    def __init__(self, extensions_enabled: bool = False):
        super().__init__(OPUS_PAYLOAD_TYPE, extensions_enabled=extensions_enabled)

    def describe_payload(self, body: bytes) -> str:
        # one opus packet per RTP payload, and nothing to decode in it
        return f"opus {len(body)}B"

    def send_frame(self, conn, chunks: "Sequence[bytes]"):
        # one opus packet per call, self contained: the RTP marker bit stays
        # clear because a 20ms packet is always a whole frame
        self._send_media(conn, self.get_rtp_header(is_last=False), chunks[0])
        self.increment_timestamp(self.FRAME_SIZE)
        # audio reports on the same cadence as video: the reference calls
        # _maybe_send_audio_rtcp_sender_report once per packet, and its clock
        # is 48kHz rather than the 90kHz video one (see clock_rate)
        self._maybe_send_sender_report(conn)
