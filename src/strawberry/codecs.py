"""
Video codec table.

One place for everything that differs per codec: Discord's RTP payload
types (from the reference client's CodecPayloadType), the ffmpeg encoder,
the davey media transform, and the NAL layout for H26x.

This is the seam for swapping the encoder backend: a future PyAV source
only has to produce frames per `VideoCodec`, packetizers stay as they are.
"""

from dataclasses import dataclass, field

import davey

OPUS_PAYLOAD_TYPE = 120


@dataclass(frozen=True)
class NalSpec:
    """How NAL types are read out of the first byte(s) of a NAL unit."""

    shift: int  # H264: 0, H265: 1
    mask: int  # H264: 0x1F, H265: 0x3F
    header_size: int  # bytes before the payload
    access_unit_delimiter: int
    # NAL types that carry a coded picture (VCL). Used to find access unit
    # boundaries in streams that carry no access unit delimiter.
    vcl_types: frozenset[int] = frozenset()
    # NAL types that come before the picture they belong to.
    parameter_set_types: frozenset[int] = frozenset()
    # NAL types that begin a random access point (H264 IDR, H265 IRAP).
    # The reference's _H26X_KEYFRAME_TYPES, and what decides whether a frame
    # is worth keeping to answer a PLI with.
    keyframe_types: frozenset[int] = frozenset()

    def type(self, nal: bytes) -> int:
        return (nal[0] >> self.shift) & self.mask


@dataclass(frozen=True)
class VideoCodec:
    name: str  # as Discord spells it (op 4 video_codec)
    payload_type: int
    rtx_payload_type: int
    encoder: str  # ffmpeg -c:v value
    encoder_args: tuple[str, ...]
    davey_codec: "davey.Codec"
    nal: "NalSpec | None" = None  # None: whole frames (VP8/VP9)
    container: str = "h264"  # ffmpeg output muxer
    # Discord's own numbering, taken from its select_protocol advertisement:
    #   AV1 101/102, H265 103/104, H264 105/106, VP8 107/108, VP9 109/110
    # and priorities AV1 1000, H265 2000, H264 3000, VP8 4000, VP9 5000.
    # The community reference client's table (H264 101, H265 103, VP8 105,
    # VP9 107, AV1 109) is *not* Discord's: AV1 on 109 lands on Discord's
    # VP9, so the server parses our HEVC as VP9 and never identifies the
    # stream. Use Discord's numbers, not the reference client's.
    priority: int = 1000  # select_protocol priority
    # which rate-control flags this encoder accepts. libsvtav1 rejects the
    # usual three: -b:v needs VBR/CBR while -maxrate needs CRF, and it
    # errors out rather than ignoring either.
    bitrate_args: tuple[str, ...] = ("-b:v", "-maxrate", "-bufsize")
    # The quality profile: encoder-private options that put the encoder in
    # constant-quality mode and hold it there.
    #
    # This replaces a target bitrate, and it has to. Rate control to an
    # average bitrate spends whatever each frame needs to hit the average, so
    # the sizes are wildly uneven - a keyframe is cheap to spend and the
    # encoder spends all of it there. Measured at 1080p30 with an 8 Mbps
    # target, on a 640x360 source:
    #
    #   VP9    median 16 KB   p90  74 KB   max 406 KB
    #   H264   median 32 KB   p90  57 KB   max 279 KB
    #
    # A frame interval is 33 ms. At 8 Mbps that is a budget of ~33 KB, so the
    # median frame already overruns it and the largest overruns it twelve
    # times over. A frame that big cannot be sent inside its own interval:
    # the sender bursts it out, the packets that do not fit are dropped on
    # the way, and the receiver loses its reference frames. That is what the
    # tearing is - it is not a packetizer bug, and the wire checks clean
    # (timestamps monotonic at 3000/frame, one marker per frame on the last
    # packet, no sequence gaps inside a frame, no datagram over the MTU).
    #
    # Constant quality targets a perceptual quality per frame instead of an
    # average size, so the sizes stay inside the frame budget and the stream
    # is sendable. Empty means "no constant-quality mode", and the caller
    # falls back to rate control to a bitrate.
    #
    # Values may name the target: `{bitrate_kbps}`, `{fps}` and `{buf_ms}`
    # are filled in from the negotiated stream. A number written out here
    # would be a guess about a bitrate that is only known at run time.
    quality: "dict[str, str]" = field(default_factory=dict)
    # The speed/quality ladder. Each entry is option overrides applied *after*
    # `quality`, so a profile has the last word on the encoder's settings.
    #
    # Three points, because the trade is not linear and one setting cannot be
    # right for both a 720p30 relay and a 1080p60 source:
    #
    #   high      best picture, slowest. May not sustain 1080p60 at all - if
    #             it cannot, the sender says it is lagging rather than lying.
    #   balanced  the default, and what this codec did before profiles existed.
    #   fast      lower quality, fastest. Still a real picture: the point is to
    #             spend fewer cycles, not to stop caring.
    #
    # Values may name the negotiated stream with `{bitrate_kbps}`, `{fps}`,
    # `{buf_ms}` and `{keyint}`, exactly as `quality` does.
    profiles: "dict[str, dict[str, str]]" = field(default_factory=dict)
    #: The profile used when the caller names none.
    default_profile: str = "balanced"
    # Frames between key frames. Long enough that key frames are not the
    # bulk of the stream, short enough that a lost one heals quickly: the
    # decoder cannot draw anything until the next one arrives, so this is
    # also how long a viewer sees damage when an IDR does not make it.
    #
    # 60 is a *fallback* for a caller that does not know the frame rate.
    # What is used is one second, i.e. the stream's own fps: the reference
    # spells it `-g {fps}` / `keyint={fps}` for every codec
    # (samples/discord-native-voice transcoding.py _video_encoder_args), and
    # a fixed frame count means the gap in *time* moves with the rate - 1s at
    # 60fps but 2s at 30 and 2.5s at 24, so a viewer waits longer for a key
    # frame on exactly the streams that need one sooner.
    keyint: int = 60
    # hardware encoder variants: name -> (ffmpeg -c:v value, args). The
    # software args carry x264 preset/tune names, which hardware encoders
    # reject ("Undefined constant"), so each variant spells its own set out.
    variants: "dict[str, tuple[str, tuple[str, ...]]]" = field(default_factory=dict)

    def encoder_for(self, profile: str) -> "tuple[str, tuple[str, ...]]":
        if profile in ("", "software"):
            return self.encoder, self.encoder_args
        try:
            return self.variants[profile]
        except KeyError:
            raise ValueError(f"{self.name} has no {profile} encoder") from None


VIDEO_CODECS: dict[str, VideoCodec] = {
    "H264": VideoCodec(
        name="H264",
        payload_type=105,
        rtx_payload_type=106,
        priority=3000,
        encoder="libx264",
        # the reference client's recipe (media/newApi.ts:313, encoders/software.ts:42):
        # IDR on every forced keyframe, no B-frames, a keyframe every second.
        encoder_args=(
            "-profile:v",
            "baseline",
            "-bsf:v",
            "h264_metadata=aud=insert",  # access unit delimiters
            "-pix_fmt",
            "yuv420p",
            "-bf",
            "0",
            "-force_key_frames",
            "expr:gte(t,n_forced*1)",
            "-forced-idr",
            "1",
            "-preset",
            "veryfast",
            # `zerolatency`, not `film`: x264's film tune trades latency for
            # quality by enabling lookahead and frame-parallel threads, which
            # buffers frames before emitting them. For a live stream the
            # buffering is latency nobody asked for, and VBV already bounds
            # the frame size. This also disables sliced-threads, so one input
            # frame is one access unit rather than several.
            "-tune",
            "zerolatency",
        ),
        davey_codec=davey.Codec.h264,
        container="h264",
        # Constant quality, not a bitrate target: `b: 0` is what tells x264
        # the quantizer is the thing being held, not an average size.
        #
        # VBV has to go through `x264-params` to reach libx264. Setting
        # `maxrate`/`bufsize` as AVOptions does nothing - they are not
        # AVOptions of the AVCodecContext, they are CLI flags that ffmpeg
        # translates. A key frame is then free to be as large as it likes,
        # which is exactly what it was: ~133 KB into a 33 ms slot.
        quality={
            "b": "0",
            "crf": "23",
            "x264-params": (
                "vbv-maxrate={bitrate_kbps}:vbv-bufsize={buf_kb}"
                ":keyint={keyint}:min-keyint={keyint}:scenecut=0"
                ":nal-hrd=cbr:sliced-threads=0"
            ),
        },
        profiles={
            # 44.0 dB at 1.02x of 1080p60 - it does not sustain 60fps, and the
            # sender will say it is lagging rather than pretend otherwise.
            # Comfortable at 1080p30 or 720p60.
            "high": {"preset": "medium", "crf": "20"},
            "fast": {"preset": "superfast", "crf": "28"},
        },
        nal=NalSpec(
            shift=0,
            mask=0x1F,
            header_size=1,
            access_unit_delimiter=9,
            # 1-5 are the slice NALs; 7/8 are SPS/PPS
            vcl_types=frozenset({1, 2, 3, 4, 5}),
            parameter_set_types=frozenset({7, 8}),
            keyframe_types=frozenset({5}),  # IDR
        ),
        variants={
            # baseline + the AUD bitstream filter work on nvenc too; only
            # the x264 preset/tune names have to go
            "nvenc": (
                "h264_nvenc",
                (
                    "-profile:v",
                    "baseline",
                    # the encoder can emit AUDs itself, no bitstream filter
                    "-aud",
                    "1",
                    "-pix_fmt",
                    "yuv420p",
                    # the settings the reference client uses for nvenc
                    "-preset",
                    "p4",
                    "-spatial-aq",
                    "0",
                    "-temporal-aq",
                    "0",
                ),
            ),
        },
    ),
    "H265": VideoCodec(
        name="H265",
        payload_type=103,
        rtx_payload_type=104,
        priority=2000,
        encoder="libx265",
        # the reference client's recipe (media/newApi.ts:313, encoders/software.ts:47)
        encoder_args=(
            "-bsf:v",
            "hevc_metadata=aud=insert",  # access unit delimiters
            "-pix_fmt",
            "yuv420p",
            "-bf",
            "0",
            "-force_key_frames",
            "expr:gte(t,n_forced*1)",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            # the reference's libx265 recipe
            # (samples/discord-native-voice transcoding.py _video_encoder_args):
            # aud for access unit delimiters, repeat-headers for in-band
            # VPS/SPS/PPS, no B-frames, and scenecut=0 so keyframes land only
            # on the GOP boundary we ask for
            # keyint/min-keyint are not repeated here: x265's `keyint` *is*
            # `gop_size`, which `_encoder_options`' caller sets from the
            # stream's fps (see pyav_source). Spelling it in this string would
            # also not work - `encoder_args` values never go through the
            # `{...}` substitution that `quality` values do, and putting it in
            # `quality` would flip this codec off ABR rate control.
            "-x265-params",
            "aud=1:repeat-headers=1:bframes=0:scenecut=0",
        ),
        profiles={
            "high": {"preset": "faster", "crf": "20"},
            "fast": {"preset": "ultrafast", "crf": "30"},
        },
        davey_codec=davey.Codec.h265,
        container="hevc",
        nal=NalSpec(
            shift=1,
            mask=0x3F,
            header_size=2,
            access_unit_delimiter=35,
            # 0-31 are the VCL NALs (TRAIL..RSV_VCL31); 32-34 are VPS/SPS/PPS
            vcl_types=frozenset(range(0, 32)),
            parameter_set_types=frozenset({32, 33, 34}),
            # 16-21 are the IRAP pictures (BLA_W_LP..CRA_NUT); 19/20 are the
            # IDR ones. The reference uses the same range.
            keyframe_types=frozenset(range(16, 22)),
        ),
        variants={
            "nvenc": (
                "hevc_nvenc",
                (
                    "-pix_fmt",
                    "yuv420p",
                    "-aud",
                    "1",
                    "-preset",
                    "p4",
                    "-spatial-aq",
                    "0",
                    "-temporal-aq",
                    "0",
                    "-tag:v",
                    "hvc1",
                ),
            ),
        },
    ),
    "VP8": VideoCodec(
        name="VP8",
        payload_type=107,
        rtx_payload_type=108,
        priority=4000,
        encoder="libvpx",
        encoder_args=(
            "-deadline",
            "realtime",
            "-cpu-used",
            "8",
            # no -crf: see the VP9 note above - it overrides bit_rate
            "-auto-alt-ref",
            "0",
            "-lag-in-frames",
            "0",
            "-error-resilient",
            "1",
            "-pix_fmt",
            "yuv420p",
        ),
        profiles={
            # `b` has to be a real target for the quality knob to do anything:
            # with b=0 libvpx pins VP8 at ~1520 kbps and ignores cq-level and
            # crf entirely (both bit-identical from crf 10 to 50). With a real
            # target crf moves the rate as expected - 8080 kbps at crf 10,
            # 3428 at crf 50 - so these set both.
            "high": {"b": "{bitrate_bps}", "crf": "20", "cpu-used": "8"},
            "balanced": {"b": "{bitrate_bps}", "crf": "30", "cpu-used": "12"},
            "fast": {"b": "{bitrate_bps}", "crf": "40", "cpu-used": "16"},
        },
        davey_codec=davey.Codec.vp8,
        container="ivf",
        # libvpx reads a quantizer as a switch to constant-quality mode, in
        # which `bit_rate` is ignored outright, so `b: 0` is what keeps it
        # there. cq-level is libvpx's 0-63 scale, lower is better.
        #
        # The buffer sizes are the part that actually bounds a key frame.
        # libvpx will spend a whole key frame interval of bits on a key
        # frame if the buffer model lets it, and one 133 KB key frame into a
        # 33 ms slot overruns the socket badly enough to lose the IDR - which
        # costs every frame after it, since they all reference it. A buffer
        # of two frame intervals is the size that makes the overshoot
        # impossible. These are AVOptions, so they are set by name here with
        # ffmpeg's own spelling (underscores), not the CLI's dashes.
        quality={
            "b": "0",
            "cq-level": "30",
            "rc_buf_sz": "{buf_ms}",
            "rc_buf_initial_sz": "{buf_ms}",
            "rc_buf_optimal_sz": "{half_buf_ms}",
            "rc_max_quantizer": "40",
            "kf_max_q": "40",
            "kf_min_dist": "{keyint}",
            "kf_max_dist": "{keyint}",
        },
    ),
    "VP9": VideoCodec(
        name="VP9",
        payload_type=109,
        rtx_payload_type=110,
        priority=5000,
        encoder="libvpx-vp9",
        encoder_args=(
            "-profile:v",
            "0",
            "-deadline",
            "realtime",
            "-cpu-used",
            "8",
            # No -crf and no -b:v: the constant-quality profile below is what
            # sets the rate, and it has to be set as one thing or libvpx
            # reads it as a switch to a different mode where `bit_rate` is
            # ignored outright. Measured at 640x360: with `-crf 30` alongside
            # a 4 Mbps target the encoder produced 827 kbps; without it,
            # 2487 kbps. The reference passes only -b:v here too
            # (samples/discord-native-voice transcoding.py, libvpx and
            # libvpx-vp9 branches).
            "-row-mt",
            "1",
            "-auto-alt-ref",
            "0",
            "-lag-in-frames",
            "0",
            "-error-resilient",
            "1",
            "-pix_fmt",
            "yuv420p",
        ),
        profiles={
            # cpu-used 2..5 buy little quality over 8 (34.99 dB against 34.96)
            # for 2-4x the time, so only `high` pays for them.
            "high": {"cpu-used": "5", "crf": "24"},
            "fast": {"cpu-used": "8", "crf": "36"},
        },
        davey_codec=davey.Codec.vp9,
        container="ivf",
        # See the VP8 note: the constant-quality profile holds the quality,
        # and the buffer model is what bounds the key frame.
        quality={
            "b": "0",
            "cq-level": "30",
            "rc_buf_sz": "{buf_ms}",
            "rc_buf_initial_sz": "{buf_ms}",
            "rc_buf_optimal_sz": "{half_buf_ms}",
            "rc_max_quantizer": "40",
            "kf_max_q": "40",
            "kf_min_dist": "{keyint}",
            "kf_max_dist": "{keyint}",
        },
    ),
    "AV1": VideoCodec(
        name="AV1",
        payload_type=101,
        rtx_payload_type=102,
        priority=1000,
        # libsvtav1, the same encoder the reference client uses
        # (encoders/software.ts:66). IVF carries whole temporal units, which
        # the packetizer splits into OBUs.
        encoder="libsvtav1",
        encoder_args=(
            "-pix_fmt",
            "yuv420p",
            # SVT-AV1's scale is inverted: higher is FASTER (0 is slowest,
            # 13 fastest), unlike x264. Preset 8 only sustains ~14.5fps at
            # 1080p on this machine, so the encoder never catches up and the
            # stream runs behind forever; 12 sustains ~30fps at 1080p and
            # ~48fps at 720p, which is what realtime streaming needs.
            "-preset",
            "12",
            # a keyframe every second, like the reference client asks of any
            # encoder (newApi.ts:325)
            "-force_key_frames",
            "expr:gte(t,n_forced*1)",
        ),
        # SVT-AV1's rate control is CRF, and its buffer sizing is not an
        # AVOption - it is only reachable through the parameter string ffmpeg
        # forwards to the library. Same job as libvpx's rc_buf_sz and
        # x264's vbv-bufsize: bound the key frame to the frame interval.
        quality={
            "crf": "35",
            # pred-struct=1 is low-delay prediction, and AV1 does not work
            # without it. SVT-AV1's default is random access, which is a
            # hierarchical structure with B frames, so it buffers and emits
            # frames in *decode* order - measured: 45 frames in, zero out
            # until the flush, then the whole second at once. Our sender
            # advances the RTP timestamp once per frame it sends, so decode
            # order has to be presentation order; in random access it is not,
            # and the frames are both late and stamped with the wrong times.
            # With pred-struct=1 the same test emits all 45 as they arrive,
            # starting at the first frame.
            #
            # The reference asks for this with `-la_depth 0`
            # (transcoding.py _video_encoder_args, libsvtav1). That flag does
            # not reach the prediction structure in this ffmpeg: setting it
            # leaves the config line reading `pred struct: random access` and
            # changes nothing. pred-struct goes through svtav1-params, which
            # is passed to the library unchanged, and it does.
            "svtav1-params": (
                "pred-struct=1:buf-sz={buf_ms}:buf-initial-sz={buf_ms}"
                ":buf-optimal-sz={half_buf_ms}:keyint={keyint}:scenecut=0"
            ),
        },
        bitrate_args=(),
        profiles={
            "high": {"preset": "8", "crf": "28"},
            "fast": {"preset": "12", "crf": "42"},
        },
        davey_codec=davey.Codec.av1,
        container="ivf",
        # AV1 is OBU framed, not NAL framed; nal=None keeps the source
        # handing us whole temporal units
        nal=None,
    ),
}

_ALIASES = {
    "h264": "H264",
    "avc": "H264",
    "h265": "H265",
    "hevc": "H265",
    "vp8": "VP8",
    "vp9": "VP9",
    "av1": "AV1",
    "av01": "AV1",
}


# Reverse index, built once from the table above rather than recomputed by a
# getattr chain at each call site. `codec_for_davey(davey.Codec.vp9)` is the
# VP9 VideoCodec; nothing needs to know the enum member happens to be spelled
# like the codec name.
#
# Keyed on str(davey.Codec.vp9) == "Codec.vp9" because davey's Codec is a
# native enum: it compares equal, but it is neither hashable nor declared
# as convertible to int, so neither can be a dict key directly.
def _davey_key(codec: "davey.Codec") -> str:
    return str(codec)


_BY_DAVEY: "dict[str, VideoCodec]" = {
    _davey_key(codec.davey_codec): codec for codec in VIDEO_CODECS.values()
}


def video_codec(name: "VideoCodec | str") -> VideoCodec:
    """Resolve a codec to its `VideoCodec`.

    Accepts a `VideoCodec` and returns it untouched, which is the common
    case: the value has usually already been resolved on its way here, and
    round-tripping it through a name (and a dict lookup) to get back to the
    same object is wasted work that also risks a lookup miss.
    """
    if isinstance(name, VideoCodec):
        return name
    key = _ALIASES.get(name.strip().lower().replace("-", "").replace(".", ""), "")
    if key not in VIDEO_CODECS:
        raise ValueError(
            f"unsupported video codec {name!r}; choose one of {', '.join(VIDEO_CODECS)}"
        )
    return VIDEO_CODECS[key]


def codec_for_davey(codec: "davey.Codec") -> VideoCodec:
    """The `VideoCodec` matching a davey media codec, or a clear error."""
    try:
        return _BY_DAVEY[_davey_key(codec)]
    except (KeyError, TypeError):
        raise ValueError(f"{codec!r} is not a video codec we send") from None


__all__ = [
    "OPUS_PAYLOAD_TYPE",
    "VIDEO_CODECS",
    "NalSpec",
    "VideoCodec",
    "codec_for_davey",
    "nal_type",
    "video_codec",
]


def nal_type(spec: NalSpec, nalu: bytes) -> int:
    return (nalu[0] >> spec.shift) & spec.mask
