from strawberry.codecs import VideoCodec, video_codec

from .annexb import AnnexBPacketizer
from .audio_packetizer import AudioPacketizer
from .av1_packetizer import AV1Packetizer
from .base_packetizer import BaseMediaPacketizer
from .h264_packetizer import H264Packetizer
from .h265_packetizer import H265Packetizer
from .vpx_packetizer import VP8Packetizer, VP9Packetizer, VPXPacketizer

_PACKETIZERS = {
    "H264": H264Packetizer,
    "H265": H265Packetizer,
    "VP8": VP8Packetizer,
    "VP9": VP9Packetizer,
    "AV1": AV1Packetizer,
}


def video_packetizer(codec: "VideoCodec | str"):
    """The packetizer class for a codec (by name or `VideoCodec`).

    `video_codec` returns an already-resolved `VideoCodec` unchanged, so the
    common case is one dict lookup on `.name` - no normalisation, no
    re-lookup, and no chance of a name that resolves to something else.
    """
    name = video_codec(codec).name
    try:
        return _PACKETIZERS[name]
    except KeyError:
        raise ValueError(f"no packetizer for {name!r}") from None


__all__ = [
    "AV1Packetizer",
    "AnnexBPacketizer",
    "AudioPacketizer",
    "BaseMediaPacketizer",
    "H264Packetizer",
    "H265Packetizer",
    "VP8Packetizer",
    "VP9Packetizer",
    "VPXPacketizer",
    "video_codec",
    "video_packetizer",
]
