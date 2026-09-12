import enum
import functools
import io
import struct
import subprocess

EPB_PREFIX = b"\x00\x00\x03"
NAL_SUFFIX = b"\x00\x00\x01"


class NalUnitTypes(enum.IntEnum):
    Unspecified = 0
    CodedSliceNonIDR = enum.auto()
    CodedSlicePartitionA = enum.auto()
    CodedSlicePartitionB = enum.auto()
    CodedSlicePartitionC = enum.auto()
    CodedSliceIdr = enum.auto()
    SEI = enum.auto()
    SPS = enum.auto()
    PPS = enum.auto()
    AccessUnitDelimiter = enum.auto()
    EndOfSequence = enum.auto()
    EndOfStream = enum.auto()
    FillerData = enum.auto()
    SEIExtenstion = enum.auto()
    PrefixNalUnit = enum.auto()
    SubsetSPS = enum.auto()


@functools.lru_cache()
def get_raw_byte_sequence_payload(frame: bytes):
    raw = b""

    while (epbs_pos := frame.find(EPB_PREFIX)) != -1:
        size = 3

        if frame[epbs_pos + 3] <= 0x03:
            size -= 1

        raw += frame[: epbs_pos + size]
        frame = frame[epbs_pos + 3 :]

    return raw + frame


class H264NalPacketIterator:
    def __init__(self):
        self.buffer = b""
        self.access_unit = []

    def iter_access_units(self, chunk: bytes):
        self.buffer += chunk

        *frames, self.buffer = self.buffer.split(NAL_SUFFIX)

        for frame in frames:
            if frame[-1] == 0:
                frame = frame[:-1]

            if not frame:
                continue

            unit_type = frame[0] & 0x1F

            if unit_type == NalUnitTypes.AccessUnitDelimiter:
                if self.access_unit:
                    yield self.access_unit
                    self.access_unit.clear()
            else:
                if unit_type in (NalUnitTypes.SPS, NalUnitTypes.SEI):
                    self.access_unit.append(get_raw_byte_sequence_payload(frame))
                else:
                    self.access_unit.append(frame)

    def iter_packets(self, chunk: bytes):
        for access_unit in self.iter_access_units(chunk):
            yield b"".join(struct.pack(">I", len(nalu)) + nalu for nalu in access_unit)


class VideoSource:
    def __init__(
        self,
        input_stream: "io.BufferedIOBase",
    ):
        self.input = input_stream

        self.packet_iter = H264NalPacketIterator()

    def iter_packets(self):
        for chunk in iter(lambda: self.input.read(8192), b""):
            yield from self.packet_iter.iter_access_units(chunk)

    @classmethod
    def from_source(
        cls,
        source: "str | io.BufferedIOBase",
        width: int = 1280,
        height: int = 720,
        has_burned_in_subtitles: bool = False,
        *,
        framerate: "int | None" = None,
        crf: "int | None" = None,
    ):
        subprocess_kwargs = {
            "stdout": subprocess.PIPE,
        }

        if isinstance(source, str):
            args = ("ffmpeg", "-i", source)
        else:
            subprocess_kwargs["stdin"] = source
            args = ("ffmpeg", "-i", "pipe:0")

        if crf is not None:
            args += ("-crf", str(crf))

        if framerate is not None:
            args += (
                "-r",
                str(framerate),
                "-x264opts",
                f"keyint={framerate}:min-keyint={framerate}",
                "-g",
                str(framerate),
            )

        vf = f"scale={width}:{height}"

        if has_burned_in_subtitles:
            if isinstance(source, str):
                escaped_source = (
                    source.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
                )

                vf += ",subtitles=" + f"'{escaped_source}'" + ":si=0"
            else:
                vf += ",subtitles=pipe\\:0:si=0"

        args += (
            "-f",
            "h264",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
            "-vf",
            vf,
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "ultrafast",
            "-profile:v",
            "baseline",
            "-bsf:v",
            "h264_metadata=aud=insert",
            "-an",
            "-loglevel",
            "warning",
            "pipe:1",
        )

        process = subprocess.Popen(args, **subprocess_kwargs)

        return cls(process.stdout)
