"""The media source: one PyAV container, decoded and encoded in process.

Why this exists
---------------
The pipeline used to shell out to ffmpeg, read its stdout from a pipe, and
probe it with ffprobe. That cost a process per source, a pipe in the middle,
and a second parse of the same file just to learn its dimensions.

Doing it in-process is both faster and simpler to reason about:

* a source already in the codec we send, at the size we send, is remuxed
  rather than re-encoded, so the encode cost disappears;
* frames are dropped in Python to hold the target rate, rather than asking
  ffmpeg to resample and hoping it kept up;
* one `av.open` hands us everything - the streams, their metadata, their
  codecs - so probing and reading are the same code path.

Framing
-------
Encoders are driven with no output container, which decides the framing:

* libx264 / libx265 emit Annex B themselves - start codes and all - so
  their packets go straight through. (An earlier version prepended a start
  code; that double-prefixed every NAL and corrupted the stream.)
* libsvtav1 / libvpx emit one self-contained temporal unit per packet,
  which is the IVF frame body `IVFFrameIterator` strips out.
* AV1 needs the decoder, not the encoder, to name its codec: SVT-AV1 is
  encode-only, so an AV1 source is libdav1d on the way in.

Audio
-----
Audio is optional in every sense. A source with no audio stream still
works; a separate `audio_source` supplies the audio instead, and the
difference to the caller is one object, not a different code path.

Decoupling
----------
Video and audio are decoded by one thread and handed to two queues, and
neither queue is allowed to block: a live frame that has been superseded
is worthless, and audio that arrives late is worse than useless. A single
blocking put let whichever consumer was slower stall the other - which
showed up as video never being sent at all, because the decoder never
reached the code that releases the readers.
"""

import fractions
import logging
import os
import queue
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from av.bitstream import BitStreamFilterContext

from strawberry.containers.annexb import split_annex_b
from strawberry.codecs import VIDEO_CODECS, VideoCodec

logger = logging.getLogger("pyav_source")

try:
    import av

    # Imported for the side effect: `av.video` is not an attribute of the
    # package until the submodule has been imported somewhere.
    import av.video.frame
except ImportError:  # pragma: no cover - PyAV is a hard dependency now
    av = None

if TYPE_CHECKING:
    from av.audio.codeccontext import AudioCodecContext
    from av.audio.resampler import AudioResampler
    from av.video.codeccontext import VideoCodecContext

ANNEX_B_START = b"\x00\x00\x00\x01"

# mp4 stores H26x as AVCC (length prefixed); the packetizers work in Annex B.
_ANNEXB_FILTER = {
    "h264": "h264_mp4toannexb",
    "hevc": "hevc_mp4toannexb",
}

SVT_PRESET = 12

# How many threads the encoder may use. Measured at 1080p60 on a 12 core box:
# VP8 at 1 thread 69 fps, 8 threads 102, 12 threads 74 - the curve turns over,
# so more is not better. This is what libvpx needed to stop running on one
# core; x264 threads itself well already and is unaffected by a sane value.
ENCODER_THREADS = 8

# Opus RTP is 20ms at 48kHz, and the audio sender paces at the same
# interval, so the two numbers have to agree.
OPUS_RATE = 48000
OPUS_FRAME_SIZE = 960
OPUS_LAYOUT = "stereo"
OPUS_BITRATE = 128_000
OPUS_PACKETS_PER_SECOND = OPUS_RATE // OPUS_FRAME_SIZE

# The decode loop is paced by `_RealtimePacer`, off the wall clock, so these
# queues are a handover buffer and not a rate control. What they *do* have to
# do is hold a whole demux run.
#
# A container hands streams over in runs rather than in presentation order.
# Measured on Demo.mp4: fifteen video packets covering media 0 to 0.467s, then
# twenty-one audio packets covering 0 to 0.464s, then video resumes at 0.5s.
# A queue shallower than one run truncates it: `_offer` discards the oldest
# when full, so eight audio slots against a twenty-one packet run throws away
# thirteen packets, and that is what showed up as a source producing 49.4
# packets/s that the sender only ever received 39.8 of - a deficit that grows
# into lag and never recovers.
#
# So the depth is one second of each stream, derived from its own rate rather
# than a magic count. That is far more than one run needs, and it cannot grow
# into latency because the pacer is what stops the decoder racing ahead - a
# file source draining audio far faster than the sender reads it was a real
# backlog, but the pacer is the thing that fixes it, not a shallow queue.
QUEUE_SECONDS = 1.0


_SENTINEL = object()


class _RealtimePacer:
    """Hold the decode loop to real time, by the clock rather than by the queue.

    Decoding has no natural rate: libvpx encodes a frame far faster than the
    stream interval, so an unpaced loop consumes the whole source as fast as
    the CPU allows. Measured on Demo.mp4 at 640x360: 26365 frames in 19
    seconds - 1374 fps, 46x real time.

    The obvious brake is to block when the queue fills, but one thread feeds
    both queues, so waiting on one stops the other being produced at all: a
    source whose demuxer emits a long run of one stream fills that queue and
    wedges the source, with the senders still parked at the `MediaGate`. No
    queue depth fixes that, because the run length is a property of the file.

    So the rate comes from the clock instead. It cannot deadlock and it needs
    no queue to be the right size.

    The schedule follows the *source's* presentation timestamps rather than a
    frame counter. A counter assumes every frame lasts exactly `1/fps`, which
    holds only for constant-rate video and silently resamples everything else.
    Feeding it each frame's own pts makes the encoder follow the source's real
    timing: variable frame rate plays at its true speed, and the decoder
    neither runs ahead to be discarded nor stalls waiting for frames that
    will never come.
    """

    def __init__(self, rate: "float | fractions.Fraction"):
        self._interval = 1.0 / float(rate) if rate else 0.0
        self._origin: float | None = None
        self._media_origin: float | None = None
        self._count = 0
        self._last_elapsed = 0.0

    def tick(self, media_seconds: "float | None" = None) -> None:
        """Wait until this frame is due; never blocks for long."""
        if self._interval <= 0:
            return
        now = time.perf_counter()
        if self._origin is None:
            self._origin = now
            self._media_origin = media_seconds
        if media_seconds is None:
            # No usable pts: fall back to counting at the stream rate.
            self._count += 1
            elapsed = self._interval * self._count
        else:
            if self._media_origin is None:
                self._media_origin = media_seconds
            elapsed = media_seconds - self._media_origin
            # A demuxer hands streams over in runs, not in presentation
            # order. Measured on Demo.mp4: fifteen video packets covering
            # media 0 to 0.467s, then twenty-one audio packets that also
            # cover 0 to 0.464s, then video from 0.5s onwards.
            #
            # So the timeline goes backwards every time the runs swap, and
            # the wall clock is not going backwards with it - the audio
            # block is catching up to a moment that has already gone past.
            # Holding the high-water mark says exactly that: those packets
            # are due now, and they go out at the rate the queue takes them
            # rather than each waiting out the block before it.
            #
            # Letting it re-anchor instead cost every block the time of the
            # one before it, and both streams ran at half rate - measured
            # 15.9 video frames/s and 25.9 audio packets/s against 30
            # and 50.
            if elapsed < self._last_elapsed:
                elapsed = self._last_elapsed
            self._last_elapsed = elapsed

        delay = self._origin + elapsed - now
        if delay > 0:
            # Bound the sleep: so a close is noticed promptly, and so a source
            # with a long gap between frames does not stall the queue holding
            # a frame nobody is waiting for.
            time.sleep(min(delay, self._interval))
            return
        # Behind schedule.
        #
        # A late packet is not on its own a reason to re-point the timeline.
        # Containers hand over runs: a dozen audio packets after a dozen video
        # ones, every one of them due while the loop was asleep waiting for
        # video. Re-syncing on each of those threw the schedule away as fast
        # as it was built, and the loop ran at half rate on both streams -
        # measured 15.7 video frames/s and 25.8 audio packets/s against 30
        # and 50. A burst is content that was already due, so it just goes
        # out as fast as the queue takes it, which is how the debt is paid
        # off.
        #
        # The resync is still right for a genuine stall, where the source
        # itself cannot keep up and carrying the debt would mean never
        # waiting again. That is a debt bigger than a frame interval.
        if -delay > self._interval:
            self._origin = now
            self._media_origin = media_seconds


# Every av.open goes through this. The user agent is what HTTP sources need
# to be served media rather than an error page, and TLS verification is off
# because Discord's media hosts are behind chains this does not trust.
CONTAINER_OPTIONS = {
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36",
    "tls_verify": "0",  # Disables peer certificate verification
}


def open_container(source: str) -> Any:
    """The one place `av.open` is called, so the options cannot be missed."""
    if av is None:  # pragma: no cover - PyAV is a declared dependency
        raise RuntimeError("PyAV is not installed")
    return av.open(source, container_options=dict(CONTAINER_OPTIONS))


# -- probes ---------------------------------------------------------------


def _rate(value: Any) -> "fractions.Fraction | None":
    if value is None:
        return None
    if isinstance(value, fractions.Fraction):
        return value
    if isinstance(value, (int, float)):
        return fractions.Fraction(value).limit_denominator(1001)
    try:
        return fractions.Fraction(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ProbedVideo:
    """One video stream, as the container describes it."""

    codec: str
    width: int
    height: int
    fps: float
    #: The same rate as `numerator/denominator`, the spelling ffprobe uses and
    #: the one the source's own metadata carries.
    frame_rate: str
    duration: "float | None"
    bit_rate: "int | None"


@dataclass(frozen=True)
class ProbedAudio:
    """One audio stream."""

    codec: str
    sample_rate: int
    channels: int
    duration: "float | None"
    bit_rate: "int | None"


@dataclass(frozen=True)
class Probe:
    """What a source holds.

    Typed rather than an ffprobe-shaped dict: the dict was three levels of
    `str -> list[dict[str, Any]]`, so every reader indexed into it and every
    typo was an AttributeError at runtime instead of a type error.
    """

    video: "tuple[ProbedVideo, ...]"
    audio: "tuple[ProbedAudio, ...]"


def _rational(rate: "fractions.Fraction") -> str:
    """A frame rate as `numerator/denominator`.

    `str(Fraction(30, 1))` is "30", but the field is named avg_frame_rate
    and its readers split on "/", so always emit the ratio.
    """
    return f"{rate.numerator}/{rate.denominator}"


def _probe_video(stream: Any) -> ProbedVideo:
    rate = _rate(stream.average_rate) or fractions.Fraction(30)
    return ProbedVideo(
        codec=stream.codec_context.name,
        width=stream.codec_context.width,
        height=stream.codec_context.height,
        fps=float(rate),
        frame_rate=_rational(rate),
        duration=_duration(stream),
        bit_rate=stream.bit_rate,
    )


def _probe_audio(stream: Any) -> ProbedAudio:
    return ProbedAudio(
        codec=stream.codec_context.name,
        sample_rate=stream.codec_context.sample_rate,
        channels=stream.codec_context.channels,
        duration=_duration(stream),
        bit_rate=stream.bit_rate,
    )


def _duration(stream: Any) -> "float | None":
    if stream.duration is None or stream.time_base is None:
        return None
    return float(stream.duration * stream.time_base)


def available() -> bool:
    return av is not None


# -- framing helpers ------------------------------------------------------


def codec_family(name: str) -> str:
    """Group encoder names, so h264/hevc compare equal to their libx forms."""
    name = name.lower()
    if name.startswith(("h264", "avc", "libx264")):
        return "h264"
    if name.startswith(("h265", "hevc", "libx265")):
        return "hevc"
    if name.startswith(("av1", "libsvtav1", "libaom", "libdav1d")):
        return "av1"
    # `libvpx-vp9` before the bare `libvpx`, or VP9's decoder reads as VP8 -
    # which is not cosmetic: the family is what decides whether a source is
    # already in the codec we send, and so whether it is remuxed or re-encoded.
    if name.startswith(("vp9", "libvpx-vp9")):
        return "vp9"
    if name.startswith(("vp8", "libvpx")):
        return "vp8"
    return name


def _pairs(args: "tuple[str, ...]") -> "list[tuple[str, str]]":
    out: list[tuple[str, str]] = []
    index = 0
    while index + 1 < len(args):
        flag = args[index]
        if flag.startswith("-"):
            name = flag.lstrip("-")
            if "=" in name:
                name, _, value = name.partition("=")
                out.append((name, value))
                index += 1
                continue
            out.append((name, args[index + 1]))
            index += 2
            continue
        index += 1
    return out


def _encoder_options(
    codec: VideoCodec,
    preset: "int | None" = None,
    crf: "int | None" = None,
    bitrate: int = 0,
    fps: int = 30,
    profile: "str | None" = None,
) -> "dict[str, str]":
    """Encoder options, taking the ffmpeg CLI flags we already agreed on.

    Only the flags PyAV has no property for survive here; everything that is
    a CodecContext attribute is set directly by the caller.
    """
    # Only encoder-private options belong here. Anything ffmpeg resolves
    # itself (a bitstream filter, a container flag, a pixel format) is
    # either rejected or silently dropped by libav, so it is filtered out
    # rather than passed along. Rate control, pixel format, keyframe
    # interval and bitrate are set on the CodecContext directly.
    #
    # These are the bare flag names, with no leading dash: `_pairs` strips it
    # while parsing, so comparing against dashed names here matched nothing
    # and discarded every option - which left libvpx on its defaults
    # (cpu-used 0, lag-in-frames 25) instead of the realtime settings the
    # codec table asks for, and cost VP9 an order of magnitude in speed.
    known = {
        "preset", "crf", "tune", "profile", "profile:v", "x264-params",
        "x265-params", "bf", "rc", "deadline", "cpu-used",
        "lag-in-frames", "auto-alt-ref", "row-mt", "error-resilient",
        "qmin", "qmax", "forced-idr", "spatial-aq", "lag-frames",
        "usage", "speed", "la-depth", "sc-detection", "cq-level",
    }
    options = {
        name.replace("_", "-"): value
        for name, value in _pairs(codec.encoder_args)
        if name in known
    }
    # `-b:v` is an ffmpeg CLI bitrate flag, not an encoder-private option.
    # Passing that spelling through would be rejected; the encoder-private
    # equivalent is `-b`, and it is the quality profile's to set.
    for cli_only in ("b:v", "b", "crf"):
        options.pop(cli_only, None)
    # The quality profile goes on last, so it is the last word on rate
    # control. Everything above it is about speed and framing.
    #
    # The placeholders are the target we actually negotiated, not a guess
    # written into the codec table: the buffer model is sized from the
    # frame interval it has to fit into, and the ceiling from the bitrate
    # the transport can carry.
    kbps = max(1, (bitrate or 8_000_000) // 1000)
    buf_ms = int(fps and 2000 / max(fps, 1))
    # One second of frames. The reference asks for `keyint={fps}` / `-g {fps}`
    # at every rate, so the gap a viewer waits for a key frame is a second
    # whatever the source runs at; a constant 60 frames stretches it to 2s at
    # 30fps and 2.5s at 24. `codec.keyint` is only a fallback for a caller
    # that did not supply a rate.
    keyint = max(1, int(fps) or codec.keyint)
    fields = {
        "bitrate_kbps": kbps,
        # `b` is avcodec's bit_rate, in bits per second, not the kbps the
        # other placeholders use. Feeding `b` a kbps value asks for 8 kbps.
        "bitrate_bps": kbps * 1000,
        "buf_kb": max(1, kbps * buf_ms // 1000),
        "buf_ms": buf_ms,
        "half_buf_ms": max(1, buf_ms // 2),
        "fps": fps,
        "keyint": keyint,
    }
    options.update(
        {k: v.format(**fields) if isinstance(v, str) else v
         for k, v in codec.quality.items()}
    )
    if preset is not None:
        options["preset"] = str(preset)
    elif codec.encoder.startswith("libsvtav1"):
        options["preset"] = str(SVT_PRESET)
    if crf is not None:
        options["crf"] = str(crf)
    # The profile has the last word. It is the caller's explicit choice of
    # where to sit on the speed/quality ladder, and it is written in the same
    # option names as everything above it.
    profile = profile or codec.default_profile
    overrides = codec.profiles.get(profile)
    if overrides:
        options.update(
            {k: v.format(**fields) if isinstance(v, str) else v
             for k, v in overrides.items()}
        )
    return options


def lead_with_aud(codec: VideoCodec, nalus: "list[bytes]") -> None:
    """Put an access unit delimiter in front when the encoder did not.

    `h264_metadata=aud=insert` and `-x265-params aud=1` do this in the
    ffmpeg recipe; on the remux path nothing adds one, so normalise it here
    rather than have every consumer guess.
    """
    if codec.nal is None:
        return
    delimiter = bytes((codec.nal.access_unit_delimiter << codec.nal.shift, 1))
    # Compare the NAL type, not the bytes: ours is `09 01`, but a stream that
    # already carries a delimiter from its own encoder writes `09 f0` (a
    # different primary_pic_type), which the byte comparison missed - so those
    # frames went out with two access unit delimiters.
    if nalus and nalus[0] and codec.nal.type(nalus[0]) == codec.nal.access_unit_delimiter:
        return
    nalus.insert(0, delimiter)


def _frame(codec: VideoCodec, payload: bytes, pts: int) -> "list[bytes] | bytes":
    """Shape an encoded packet the way the packetizers expect it."""
    if codec.nal is None:
        return payload
    nalus = split_annex_b(payload)
    lead_with_aud(codec, nalus)
    return nalus


# -- the source -----------------------------------------------------------


class PyAVSource:
    """One container, decoded and encoded in process, video and optional audio.

    `.video` and `.audio` are independent readers. Constructing the source
    does no decoding: the decode thread starts on the first read, because
    the caller builds every source up front and only starts its sender
    threads later.
    """

    @staticmethod
    def probe(source: str) -> Probe:
        """Open a source, describe it, and close it again.

        Opening it is the whole test - a 403 fails here.
        """
        probe, container = PyAVSource.probe_open(source)
        container.close()
        return probe

    @staticmethod
    def probe_open(source: str) -> "tuple[Probe, Any]":
        """Describe a source, handing back the container it was described from.

        The container is left open: opening a remote source costs a round trip
        and can be refused, so a caller that opened it to check it works should
        keep it rather than open the same URL twice.
        """
        container = open_container(source)
        try:
            streams = container.streams
            probe = Probe(
                video=tuple(_probe_video(s) for s in streams.video),
                audio=tuple(_probe_audio(s) for s in streams.audio),
            )
        except Exception:
            container.close()
            raise
        return probe, container

    def __init__(
        self,
        source: str,
        *,
        codec: "VideoCodec | str" = "H264",
        width: int = 1280,
        height: int = 720,
        framerate: int = 30,
        bitrate: "int | None" = None,
        bitrate_max: "int | None" = None,
        preset: "int | None" = None,
        crf: "int | None" = None,
        profile: "str | None" = None,
        container: "Any | None" = None,
        audio_container: "Any | None" = None,
        audio_source: "str | None" = None,
        video_extra_args: "tuple[str, ...]" = (),
    ) -> None:
        if av is None:  # pragma: no cover
            raise RuntimeError("PyAV is not installed")

        self.codec = VIDEO_CODECS[codec] if isinstance(codec, str) else codec
        self.width = width
        self.height = height
        self.framerate = framerate
        self.profile = profile
        self._closed = False
        self._started = False
        self._stop = threading.Event()

        self.container = container or open_container(source)
        streams = self.container.streams
        if not streams.video:
            self.container.close()
            raise ValueError(f"{source} has no video stream")
        self.video_stream = streams.video[0]

        # Audio may live in this container, in another one, or nowhere.
        #
        # When it is in the same source it shares this container handle, and
        # that is not an optimisation - it is required. A source can be a
        # live single-client stream: `streamlink --player-external-http`, for
        # one, serves exactly one connection at a time and never ends.
        # Opening it a second time for the audio thread leaves the first
        # connection holding the only server while the second open waits on
        # data that will not come, so the source probes fine and then reads
        # nothing at all. One connection, one demux, both streams.
        self.audio_container = None
        if audio_source:
            # A container the caller already opened for the same URL, handed
            # over so the source is opened once rather than once per check.
            self.audio_container = audio_container or open_container(audio_source)
            audio_streams = self.audio_container.streams
            self.audio_stream = audio_streams.audio[0] if audio_streams.audio else None
        else:
            self.audio_stream = streams.audio[0] if streams.audio else None

        in_codec = self.video_stream.codec_context.name
        in_w = self.video_stream.codec_context.width
        in_h = self.video_stream.codec_context.height
        self.in_rate = _rate(self.video_stream.average_rate) or fractions.Fraction(30)
        self.out_rate = fractions.Fraction(framerate)
        # Real-time pacing for the decode loop. Reset at the start of each
        # decode pass so a restarted source does not inherit a stale schedule.
        self._pacer = _RealtimePacer(self.out_rate)
        self._encoder_options_set: dict[str, str] = {}
        self._options_checked = False
        self._video_dropped = 0
        self._audio_dropped = 0
        # keep 1 frame in N to hold the target rate
        self.step = max(1, round(float(self.in_rate / self.out_rate)))
        self._video_seen = 0
        self._decoded = 0

        # --- video: remux when we can, otherwise encode -------------------
        self.reencoded = True
        self._bsf: BitStreamFilterContext | None = None
        self.video_encoder: VideoCodecContext | None = None
        self.video_decoder: Any = self.video_stream.codec_context

        in_bitrate = self.video_stream.bit_rate or 0
        # Remuxing costs no CPU, but it also applies no rate control: whatever
        # the source was encoded at is what goes on the wire. A 28 Mbps source
        # remuxed at 1080p shipped 28 Mbps to a connection whose pacer - and
        # whose announced max_bitrate - assume 8, so the frame never fit the
        # frame interval and went out as one large burst. Only take the cheap
        # path when the source is already close enough to the target, and
        # never when no target was given at all.
        matches = codec_family(in_codec) == codec_family(self.codec.name)
        same_size = in_w == width and in_h == height
        remux_ok = bool(bitrate) and in_bitrate <= bitrate * 1.5
        # A remuxed stream keeps the source's B frames. Demux hands those over
        # in decode order, while the RTP timestamp advances once per frame
        # *sent*, so the timestamps would describe a different order than the
        # pictures - a 25 fps documentary has 40 pts back-steps in 600 packets.
        # Re-encoding sets bf=0, which keeps presentation order in sent order.
        if self.video_stream.codec_context.has_b_frames:
            remux_ok = False

        if matches and same_size and remux_ok:
            self.reencoded = False
            if self.codec.nal is not None:
                name = _ANNEXB_FILTER.get(codec_family(in_codec))
                if name is not None:
                    self._bsf = BitStreamFilterContext(name, self.video_stream)
            logger.info(
                "source is already %s at %dx%d: remuxing, no re-encode",
                self.codec.name,
                width,
                height,
            )
        elif matches and same_size:
            logger.info(
                "source is already %s at %dx%d but encoded at %s bps; "
                "re-encoding to %s bps so a frame fits the send budget",
                self.codec.name,
                width,
                height,
                f"{in_bitrate:,}" if in_bitrate else "unknown",
                f"{bitrate:,}" if bitrate else "unknown",
            )

        if self.reencoded:
            # CodecContext.create is typed as the base CodecContext, but a
            # video encoder is a VideoCodecContext and that is where
            # width/height/pix_fmt live.
            encoder = cast(
                "VideoCodecContext", av.CodecContext.create(self.codec.encoder, "w")
            )
            encoder.width = width
            encoder.height = height
            encoder.pix_fmt = "yuv420p"
            encoder.time_base = self.video_stream.time_base
            encoder.framerate = self.out_rate
            # One second between key frames, at whatever rate the source
            # runs at - the same value `_encoder_options` puts in the
            # encoder's parameter string. This is also the worst-case damage
            # a viewer sees: nothing decodes until the next key frame
            # arrives, so a lost one costs this many frames of picture.
            encoder.gop_size = max(1, int(framerate) or self.codec.keyint)
            # Thread the encoder. This was never set, and avcodec's auto value
            # for libvpx is effectively one thread: measured at 1080p60, VP8
            # went from 69 fps to 102 with 8 threads at the same settings, and
            # to 321 with cpu-used 16 as well. Left alone it used one core of
            # twelve. Eight rather than all of them - twelve measured *worse*
            # than eight (74 fps against 102), so the curve turns over.
            encoder.thread_count = max(
                1, min(os.cpu_count() or 1, ENCODER_THREADS)
            )
            options = _encoder_options(
                self.codec,
                preset,
                crf,
                bitrate=bitrate or 0,
                fps=framerate,
                profile=profile,
            )
            # A constant-quality profile keeps the frame sizes inside the
            # frame interval, which is what makes the stream sendable; see
            # `VideoCodec.quality`.
            options_cq = bool(self.codec.quality) or crf is not None
            if options:
                encoder.options = options
            self._encoder_options_set = options
            self._options_checked = False
            if bitrate:
                # A bitrate target and a constant-quality profile are two
                # different jobs and only one of them can be in charge. When
                # the codec has a quality profile, the quantizer wins and
                # `bit_rate` must stay unset: libvpx and x264 both read it as
                # the thing they are holding, and setting an average size as
                # well is what produced the 406 KB keyframes - a frame twelve
                # times over its own interval, which cannot be sent inside it.
                # `bitrate` is still used to decide remux vs re-encode, since
                # that comparison is about what the source already is.
                if not options_cq:
                    encoder.bit_rate = bitrate
                    # A ceiling as well as an average, where the encoder
                    # accepts one: the average alone still lets a key frame
                    # overshoot the target. libsvtav1 does not - setting
                    # `max_bit_rate` makes avcodec_open2 fail outright - which
                    # is what `VideoCodec.bitrate_args` records per codec.
                    if bitrate_max and "-maxrate" in self.codec.bitrate_args:
                        encoder.max_bit_rate = bitrate_max
                        # And the buffer it is measured against. x265 needs
                        # both or it discards the ceiling entirely - "VBV
                        # maxrate specified, but no bufsize, ignored" - so the
                        # only codec without a quality profile to size it (see
                        # `_encoder_options`, which builds the same box for the
                        # others) was the one quietly running unbounded.
                        # Two frame intervals, in bits, matching the profiles.
                        encoder.rc_buffer_size = max(
                            1, int(bitrate * 2 / max(framerate, 1))
                        )
            self.video_encoder = encoder
            logger.info(
                "PyAV encode: %s %dx%d @%dfps (source %s %dx%d @%s, keeping 1 in %d)",
                self.codec.encoder,
                width,
                height,
                framerate,
                in_codec,
                in_w,
                in_h,
                float(self.in_rate),
                self.step,
            )

        # --- audio: resample and encode to 20ms opus packets --------------
        self.audio_encoder: AudioCodecContext | None = None
        self.resampler: AudioResampler | None = None
        self.audio_decoder: Any = (
            self.audio_stream.codec_context if self.audio_stream is not None else None
        )
        if self.audio_stream is not None:
            audio = cast("AudioCodecContext", av.CodecContext.create("libopus", "w"))
            audio.rate = OPUS_RATE
            audio.layout = OPUS_LAYOUT
            audio.format = "s16"
            audio.bit_rate = OPUS_BITRATE
            self.audio_encoder = audio
            # The resampler does the rate conversion, so the encoder always
            # sees 48kHz - but a 44100Hz source still has to be delivered at
            # the 50 packets/s the RTP clock and the sender both assume.
            #
            # The frame size is what decides that, and it has to be scaled from
            # the *source* rate: asking a 44100Hz stream for 960 samples yields
            # 960/44100 = 21.8ms of audio per packet, so 44 packets/s, while
            # everything downstream paces and timestamps at 48kHz/960 = 50/s.
            # Supply below demand is not a hiccup, it is a permanent deficit:
            # the audio sender starves on the queue and its lag grows without
            # bound (measured at 40 packets/s produced against 50 wanted, and
            # 21.7s of accumulated lag).
            #
            # Deriving the frame size from the source's own sample rate keeps
            # one packet to one 20ms of audio whatever the source is: a
            # 44100Hz source is asked for 882 samples, resampled up to 960.
            source_rate = self.audio_stream.codec_context.sample_rate or OPUS_RATE
            resample_frame = max(
                1, round(OPUS_FRAME_SIZE * source_rate / OPUS_RATE)
            )
            self.resampler = av.AudioResampler(
                format="s16",
                layout=OPUS_LAYOUT,
                rate=OPUS_RATE,
                frame_size=resample_frame,
            )

        # --- routing --------------------------------------------------------
        # Deep enough for one demux run each, sized from the stream's own
        # rate. See QUEUE_SECONDS above.
        self._video_q: queue.Queue[Any] = queue.Queue(
            maxsize=max(2, round(float(self.out_rate) * QUEUE_SECONDS))
        )
        self._audio_q: queue.Queue[Any] = queue.Queue(
            maxsize=max(2, round(OPUS_PACKETS_PER_SECOND * QUEUE_SECONDS))
        )
        self._thread = threading.Thread(target=self._decode_loop, daemon=True)

    # -- readers ------------------------------------------------------------

    @property
    def video(self) -> "_VideoReader":
        # Bare frames: no IVF container framing.
        #
        # IVF is a *file* format. Its 32 byte file header and its 12 byte
        # per-frame header are for demuxing a `.ivf` file, and the only code
        # that strips them is `IvfFrameIterator` - which parses a raw byte
        # stream and is never on the send path. So the framing added here went
        # straight into the RTP payload: every VP8/VP9/AV1 packet carried 12
        # bytes of junk ahead of the frame, and the very first packet carried
        # the whole `DKIF` file header as if it were a picture. The decoder
        # then started 12 bytes into every frame, which is what made VP9 look
        # like it worked but had abysmal quality.
        return _VideoReader(self)

    @property
    def audio(self) -> "_AudioReader":
        return _AudioReader(self)

    @property
    def has_audio(self) -> bool:
        return self.audio_stream is not None

    def iter_packets(self) -> "Iterator[list[bytes] | bytes]":
        """Video frames only, for callers that do not want the split."""
        return self.video.iter_packets()

    # -- decoding -----------------------------------------------------------

    def _start(self) -> None:
        """Start decoding on the first read, not at construction."""
        if self._started:
            return
        self._started = True
        self._thread.start()

    def _decode_loop(self) -> None:
        try:
            self._decode_and_encode()
        except BaseException as exc:
            if not self._closed:
                logger.exception("PyAV source failed")
            else:
                # a deliberate close ends the stream; the decoders say so
                # with EOFError, which is not a failure worth a traceback
                logger.debug("PyAV source stopped: %s", exc)
        finally:
            # both readers must always be released, even on a broken source
            self._put_video(_SENTINEL)
            self._put_audio(_SENTINEL)

    def _put_video(self, item: Any) -> None:
        """Queue a video frame, applying back-pressure when the reader lags.

        This used to drop the oldest frame instead of waiting, on the reasoning
        that a live frame which has been superseded is worthless. That is true
        for a live source and catastrophic for a file. Decoding has no natural
        rate - the encoder produces frames far faster than the stream rate - so
        an unpaced decoder with a dropping queue consumes the whole source as
        fast as the CPU allows and throws away nearly all of it. Measured on
        Demo.mp4 at 640x360: 26365 frames encoded in 19 seconds (1374 fps,
        46x real time) against a sender pulling 30 fps, so 97.8% were dropped
        and the stream ended after 19 seconds having shown a sparse sample of
        a 14 minute file.

        Blocking here is the rate control: the queue holds a fixed number of
        frames, so once it fills the decoder waits for the sender, and the
        decode loop settles to exactly the rate frames leave at.

        The wait is bounded and gives up on a close, so a consumer that has
        gone away cannot wedge the decode thread.
        """
        self._offer(self._video_q, item)

    def _put_audio(self, item: Any) -> None:
        """Queue an audio packet, with the same back-pressure as video."""
        self._offer(self._audio_q, item)

    def _offer(self, q: "queue.Queue[Any]", item: Any) -> None:
        """Put, discarding the oldest if the queue is full. Never blocks.

        The queue is a handover buffer, not the rate control - that is
        `_RealtimePacer`, which holds the decode loop to the stream rate off
        the wall clock.

        It must not block, because one thread feeds both queues: waiting for
        room on a full queue stops the other stream being produced at all, and
        a source whose demuxer emits a long run of one stream fills that queue
        and wedges everything, with the senders still parked at the
        `MediaGate` and nothing sent at all. No queue depth fixes that,
        because the run length is a property of the file.

        So a full queue drops its oldest, which keeps the buffer bounded and
        the stream current. That only bites if the sender cannot keep up, and
        then the newest frame is the one worth having anyway.

        The drops are counted rather than swallowed. A drop is not just a
        missing frame: the sender advances its pacing index once per packet
        it receives, so frames lost here are schedule slots the sender never
        reaches, and the gap between the schedule and the wall clock opens up
        by one interval per dropped frame and never closes. That is the lag
        the sender reports on a stream that looks fine, and counting is the
        only way to tell it apart from a slow source.
        """
        while q.full() and not self._stop.is_set():
            try:
                q.get_nowait()
                if q is self._video_q:
                    self._video_dropped += 1
                else:
                    self._audio_dropped += 1
            except queue.Empty:  # pragma: no cover - raced a reader
                break
        if self._stop.is_set() and item is not _SENTINEL:
            # Never strand the reader waiting for a sentinel that is dropped.
            return
        try:
            q.put_nowait(item)
        except queue.Full:  # pragma: no cover - reader drained it
            pass

    def _iter(self, q: "queue.Queue[Any]", header: bytes) -> Iterator[Any]:
        self._start()
        yield from self._drain(q, header)

    def _drain(self, q: "queue.Queue[Any]", header: bytes) -> Iterator[Any]:
        if header:
            yield header
        while True:
            item = q.get()
            if item is _SENTINEL:
                return
            yield item[1]

    def _decode_and_encode(self) -> None:
        want_audio = self.audio_encoder is not None and self.resampler is not None
        # An audio stream in a *different* container still needs its own
        # thread: demuxing two containers in one loop interleaves badly.
        if self.audio_container is not None and want_audio:
            threading.Thread(
                target=self._decode_audio_only, args=(self.audio_container,),
                daemon=True,
            ).start()

        # Both streams in one loop, paced by one clock on the media timeline.
        #
        # The two streams are demuxed together because they often share one
        # connection - a live source serves one client, so they cannot each
        # have their own. They are still paced correctly: `_RealtimePacer`
        # waits on *absolute* media time, not on a per-stream interval, so a
        # packet is held until its own timestamp comes round rather than
        # until this stream's last packet was handled. Consecutive packets
        # have increasing deadlines, so the waits telescope instead of
        # accumulating - one second of media is one second of wall clock
        # whether it is 30 video frames, 50 audio packets or both.
        #
        # What this costs is that an audio packet can land up to one video
        # frame behind its own deadline, because the loop was asleep waiting
        # for video. That is jitter, not drift, and the audio queue is what
        # absorbs it.
        streams: list[Any] = [self.video_stream]
        if want_audio and self.audio_container is None:
            streams.append(self.audio_stream)
        encoded_v = emitted_a = 0
        self._pacer = _RealtimePacer(self.out_rate)
        for packet in self.container.demux(*streams):
            # Stop on a close rather than letting the decoder run to the end
            # of the file while the caller is waiting for close() to return.
            if self._stop.is_set():
                return
            if packet.stream == self.video_stream or packet.stream.type == "video":
                encoded_v = self._video_packet(packet, encoded_v, paced=True)
            elif want_audio:
                emitted_a = self._audio_frames(packet, emitted_a)
                self._pacer.tick(self._packet_seconds(packet))
        if self.reencoded:
            encoded_v = self._flush_video(encoded_v)
        if want_audio and self.audio_container is None:
            emitted_a = self._flush_audio(emitted_a)
        logger.info(
            "PyAV source finished: %d video frames (%d dropped), "
            "%d audio packets (%d dropped)",
            encoded_v,
            self._video_dropped,
            emitted_a,
            self._audio_dropped,
        )

    def _video_packet(self, packet: Any, encoded_v: int, paced: bool = True) -> int:
        if packet.dts is None or packet.size == 0:
            return encoded_v  # a flush packet, not data

        if not self.reencoded:
            # Hold the target rate on the remux path too. Skipping here is
            # what decides the RTP clock: the timestamp advances one frame
            # per *sent* frame, so shipping 60 source frames under a 30fps
            # clock puts two frames in every timestamp interval, and the
            # receiver answers with a PLI storm.
            seen = self._video_seen
            self._video_seen += 1
            if self.step > 1 and seen % self.step:
                return encoded_v
            if self._bsf is not None:
                for filtered in self._bsf.filter(packet):
                    self._put_video(
                        (encoded_v, _frame(self.codec, bytes(filtered), encoded_v))
                    )
                    encoded_v += 1
            else:
                # VPx/AV1 already come as whole frames; they go out bare.
                # Wrapping them in ivf_frame() put a 12 byte u32-size/u64-pts
                # header in front of every frame's RTP payload.
                self._put_video((encoded_v, bytes(packet)))
                encoded_v += 1
                self._pacer.tick(self._packet_seconds(packet))
            return encoded_v

        assert self.video_encoder is not None
        emitted = encoded_v
        # Decide *before* decoding. Decoding a 1080p frame and then throwing
        # it away is the single most expensive thing this pipeline can do,
        # and at 60fps source into a 30fps stream it happens for half of
        # every frame.
        skip = self.step > 1 and (self._decoded % self.step) != 0
        self._decoded += 1
        for frame in self.video_decoder.decode(packet):
            if skip:
                continue
            # The frame keeps the timestamp the decoder gave it, on the
            # stream's own time base. Overwriting it with a frame *index*
            # (`pts = emitted`) looked harmless - nothing downstream reads it,
            # since the RTP clock is the packetizer's - but x264 reads it to
            # work out each frame's duration, and a bare index is not a time.
            # It made rate control budget as though every frame were a whole
            # keyframe interval, which collapsed the encode from 9101 kbps to
            # 1128 kbps at the same 8 Mbps setting. That is the whole of the
            # "the bitrate is heavily restricted and the picture is awful".
            if frame.pts is None:
                frame.pts = emitted
            frame.time_base = self.video_stream.time_base
            out = frame.reformat(
                width=self.width, height=self.height, format="yuv420p"
            )
            # The decoder's picture type describes the *source* bitstream, not
            # what we are about to encode: it says how that encoder grouped
            # its frames. The reformat above carries it across, and ffmpeg's
            # encoder wrappers honour it by forcing the same type on our
            # output - x265 then objects at every keyframe boundary,
            # "specified frame type (3) at 60 is not compatible with keyframe
            # interval" (3 is X265_TYPE_P), and spends effort working around a
            # conflict of our own making. Measured at 1080p: 7% slower.
            # A re-encode picks its own frame types; say so.
            # the constructor raises when PyAV is missing, so by here it is
            # not optional; the checker cannot see across the constructor
            assert av is not None
            out.pict_type = av.video.frame.PictureType.NONE
            for encoded in self.video_encoder.encode(out):
                self._put_video(
                    (encoded_v, _frame(self.codec, bytes(encoded), encoded_v))
                )
                encoded_v += 1
                # Pace on the source's own presentation time, not a frame
                # count. See `_RealtimePacer`. Skipped when the caller paces
                # the demux loop itself, so one packet is not waited for
                # twice.
                if paced:
                    self._pacer.tick(self._media_seconds(frame))
            if not self._options_checked:
                self._options_checked = True
                self._verify_encoder_options()
            emitted += 1
        return encoded_v

    def _verify_encoder_options(self) -> None:
        """Report the rate control that actually took effect.

        PyAV does not fail when an encoder option is one the encoder does not
        have - it is dropped quietly and the encode continues at the
        encoder's defaults. For rate control that means the buffer model is
        simply absent and key frames are unbounded again, which is invisible
        from the outside, so it is worth saying out loud.

        What must *not* be read as a drop is an option consumed at init:
        libx264 and libvpx both take `b`, `crf`, `x264-params` and their
        tuning flags while initialising and remove them from `options` as
        they do. Judging by `options` afterwards therefore reports every
        encoder as having dropped everything - it said so for H264 while its
        CRF profile was demonstrably in force, measured at a 57 KB worst-case
        key frame against 279 KB before.

        So the observable check is used instead: constant quality means the
        encoder was not given a bitrate target, and `bit_rate` reflects that.
        The requested options are logged either way, because they cannot be
        read back and that is the only record of them.
        """
        if self.video_encoder is None or not self.codec.quality:
            return

        requested = dict(getattr(self, "_encoder_options_set", {}))
        logger.info(
            "%s encoder options requested: %s",
            self.codec.name,
            ", ".join(f"{k}={v}" for k, v in sorted(requested.items())) or "(none)",
        )

        # `b` is how libx264 and libvpx spell constant quality, and it is the
        # only thing that was read here. libsvtav1 spells the same intent as
        # `crf`, with no `b` key at all, so AV1 was judged *not* to be in
        # constant quality and the report below then formatted a bitrate that
        # does not exist. `bit_rate` is None precisely because the profile
        # took effect, so that raised TypeError on the decode thread: AV1
        # delivered one frame per restart and never streamed. Whether a
        # bitrate target is set is the observable answer, and it is the one
        # the docstring already describes.
        asked_for_constant_quality = self.codec.quality.get("b") == "0"
        bit_rate = self.video_encoder.bit_rate
        if asked_for_constant_quality and bit_rate:
            logger.warning(
                "%s was asked for constant quality (b=0) but opened with a "
                "%s bps target, so the quantiser is not what is being held "
                "and key frames are unbounded",
                self.codec.name,
                f"{bit_rate:,}",
            )
        else:
            logger.info(
                "%s rate control: %s",
                self.codec.name,
                f"{bit_rate:,} bps" if bit_rate else "constant quality",
            )

    def _media_seconds(self, frame: Any) -> "float | None":
        """A decoded frame's presentation time, in seconds.

        The pts is in the stream's own time base, so it has to be scaled by it
        to be comparable with wall clock. None when the source carries no
        usable timestamp, which is legal and just means we fall back to
        counting.
        """
        pts = getattr(frame, "pts", None)
        if pts is None:
            return None
        time_base = getattr(frame, "time_base", None) or self.video_stream.time_base
        return self._on_shared_timeline(pts, time_base, self.video_stream)

    def _on_shared_timeline(
        self, pts: int, time_base: Any, stream: Any
    ) -> "float | None":
        """Seconds from the *container's* start, not the stream's.

        Every stream numbers its own packets from its own zero, and those
        zeros do not line up: on Demo.mp4 the video packets run 0, 0.033,
        0.067 ... and the audio packets run 0, 0.023, 0.047 ... each from
        zero, half a second apart in reality. A pacer that takes those
        numbers at face value sees the stream jump backwards every time it
        crosses from one to the other, and spends its life re-pointing the
        schedule. Both streams then run at half rate: measured 15.9 video
        frames/s and 25.9 audio packets/s against 30 and 50.

        Subtracting each stream's own `start_time` puts both on one timeline,
        which is the only thing a single shared pacer can be driven by.
        """
        if time_base is None:
            return None
        start = getattr(stream, "start_time", None)
        if start is None:
            start = 0
        return (pts - start) * float(time_base)

    def _packet_seconds(
        self, packet: Any, stream: Any = None
    ) -> "float | None":
        """A demuxed packet's presentation time, in seconds."""
        pts = getattr(packet, "pts", None)
        if pts is None:
            return None
        stream = stream if stream is not None else self.video_stream
        time_base = getattr(packet, "time_base", None)
        if time_base is None:
            time_base = getattr(stream, "time_base", None)
        return self._on_shared_timeline(pts, time_base, stream)

    def _audio_frames(self, packet: Any, emitted_a: int) -> int:
        assert self.audio_encoder is not None and self.resampler is not None
        for frame in self.audio_decoder.decode(packet):
            for resampled in self.resampler.resample(frame):
                for encoded in self.audio_encoder.encode(resampled):
                    self._put_audio((emitted_a, bytes(encoded)))
                    emitted_a += 1
        return emitted_a

    def _decode_audio_only(self, container: Any) -> None:
        """Decode the audio stream on its own thread, paced at its own rate."""
        try:
            stream = container.streams.audio[0]
            emitted = 0
            # Opus packets go out at the rate the RTP clock implies, which is
            # fixed by the frame size: 48000/960 = 50/s. The source's own pts
            # drives the schedule so the queue is filled at the rate the sender
            # drains it, not in demux bursts.
            pacer = _RealtimePacer(OPUS_PACKETS_PER_SECOND)
            for packet in container.demux(stream):
                if self._stop.is_set():
                    return
                if packet.dts is None or packet.size == 0:
                    continue
                self.audio_decoder = stream.codec_context
                emitted = self._audio_frames(packet, emitted)
                pacer.tick(self._packet_seconds(packet, stream))
            emitted = self._flush_audio(emitted)
            logger.info("separate audio source: %d opus packets", emitted)
        except BaseException as exc:
            logger.exception("separate audio source failed: %s", exc)
        finally:
            container.close()

    def _flush_video(self, encoded_v: int) -> int:
        assert self.video_encoder is not None
        try:
            drained = [*self.video_decoder.decode(None)]
        except EOFError:
            drained = []
        for frame in drained:
            out = frame.reformat(
                width=self.width, height=self.height, format="yuv420p"
            )
            for encoded in self.video_encoder.encode(out):
                self._put_video(
                    (encoded_v, _frame(self.codec, bytes(encoded), encoded_v))
                )
                encoded_v += 1
        try:
            for encoded in self.video_encoder.encode(None):
                self._put_video(
                    (encoded_v, _frame(self.codec, bytes(encoded), encoded_v))
                )
                encoded_v += 1
        except EOFError:
            pass
        return encoded_v

    def _flush_audio(self, emitted_a: int) -> int:
        assert self.audio_encoder is not None and self.resampler is not None
        try:
            self.resampler.resample(None)
        except Exception:
            pass
        try:
            for encoded in self.audio_encoder.encode(None):
                self._put_audio((emitted_a, bytes(encoded)))
                emitted_a += 1
        except EOFError:
            pass
        return emitted_a

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        """Stop decoding, then release PyAV, in that order.

        Closing a container while the decode thread is still demuxing it is a
        use after free - libav reports it as `free(): invalid pointer`, not as
        an exception. So: drain the queues to release any producer, give the
        thread a moment to finish by itself, close the containers to break it
        out of a blocking demux, and wait for it to unwind.

        The encoder and decoder contexts belong to the stream and are freed
        with it; closing them separately is a double free.
        """
        if self._closed:
            return
        self._closed = True
        self._stop.set()

        for q in (self._video_q, self._audio_q):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

        if self._started:
            self._thread.join(timeout=5.0)

        # Only release the containers once the thread has actually stopped;
        # closing one it is still demuxing is a use after free.
        for container in (self.container, self.audio_container):
            if container is not None:
                try:
                    container.close()
                except Exception:
                    pass

    def __enter__(self) -> "PyAVSource":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class _VideoReader:
    """Video side of a `PyAVSource`."""

    def __init__(self, owner: PyAVSource) -> None:
        self._owner = owner

    def iter_packets(self) -> "Iterator[list[bytes] | bytes]":
        return iter(self._owner._iter(self._owner._video_q, b""))

    def close(self) -> None:
        self._owner.close()

    def __enter__(self) -> "_VideoReader":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class _AudioReader:
    """Audio side of a `PyAVSource`."""

    def __init__(self, owner: PyAVSource) -> None:
        self._owner = owner

    def iter_packets(self) -> "Iterator[bytes]":
        return iter(self._owner._iter(self._owner._audio_q, b""))

    def close(self) -> None:
        self._owner.close()

    def __enter__(self) -> "_AudioReader":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
