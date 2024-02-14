import enum
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


def get_raw_byte_sequence_payload(frame: bytearray):
    raw = bytearray()
    frame_copy = bytes(frame)

    epbs_pos = frame_copy.find(EPB_PREFIX)

    while epbs_pos != -1:
        if frame_copy[epbs_pos + 3] <= 0x03:
            size = 2
        else:
            size = 3

        raw += frame_copy[: epbs_pos + size]
        frame_copy = frame_copy[epbs_pos + 3 :]

        epbs_pos = frame_copy.find(EPB_PREFIX)

    return raw + frame_copy


def find_start_of_nal(buf: bytearray):
    pos = buf.find(NAL_SUFFIX)

    if pos == -1:
        return None

    if buf[pos - 1] == 0:
        return pos - 1, 4

    return pos, 3


class H264NalPacketIterator:
    def __init__(self):
        self.buffer = bytearray()
        self.access_unit = []

    def iter_packets(self, chunk: bytes):
        chunk = self.buffer + chunk

        while nal_start := find_start_of_nal(chunk):
            pos, length = nal_start

            frame = chunk[:pos]
            chunk = chunk[pos + length :]

            if not frame:
                return

            header = frame[0]
            unit_type = header & 0x1F

            if unit_type == NalUnitTypes.AccessUnitDelimiter:
                if self.access_unit:
                    yield b"".join(
                        struct.pack(">I", len(nalu)) + nalu for nalu in self.access_unit
                    )

                    self.access_unit.clear()
            else:
                if unit_type == NalUnitTypes.SPS or unit_type == NalUnitTypes.SEI:
                    self.access_unit.append(get_raw_byte_sequence_payload(frame))
                else:
                    self.access_unit.append(frame)

        self.buffer = chunk


class VideoSource:
    def __init__(
        self,
        source: "str | io.BufferedIOBase",
        width: int = 1280,
        height: int = 720,
        has_burned_in_subtitles: bool = False,
        *,
        framerate: "int | None" = 24,
        crf: "int | None" = None,
    ):
        self.input = source
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
            args += ("-r", str(framerate))

        vf = f"scale={width}:{height}"

        if has_burned_in_subtitles:
            if isinstance(source, str):
                escaped_source = source.replace(":", "\\:").replace("'", "\\'")

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

        self.process = subprocess.Popen(args, **subprocess_kwargs)

        self.packet_iter = H264NalPacketIterator()

    def iter_packets(self):
        for chunk in iter(lambda: self.process.stdout.read(1024 * 16), b""):
            for packet in self.packet_iter.iter_packets(chunk):
                yield packet
