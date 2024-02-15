import struct
import subprocess
from typing import IO


class OggPage:
    flag: int
    gran_pos: int
    serial: int
    pagenum: int
    crc: int
    segnum: int

    def __init__(self, stream: IO[bytes]) -> None:
        header = stream.read(0x17)

        (
            self.flag,
            self.gran_pos,
            self.serial,
            self.pagenum,
            self.crc,
            self.segnum,
        ) = struct.unpack("<xBQIIIB", header)

        self.segtable: bytes = stream.read(self.segnum)
        self.data = stream.read(sum(struct.unpack("B" * self.segnum, self.segtable)))

    def iter_packets(self):
        packetlen = offset = 0
        partial = True

        for seg in self.segtable:
            if seg == 0xFF:
                packetlen += 0xFF
                partial = True
            else:
                packetlen += seg
                yield self.data[offset : offset + packetlen], True
                offset += packetlen
                packetlen = 0
                partial = False

        if partial:
            yield self.data[offset:], False


class OggStream:
    def __init__(self, stream: IO[bytes]) -> None:
        self.stream: IO[bytes] = stream

    def __iter__(self):
        buffer = b""

        for frame in iter(lambda: self.stream.read(4), b""):
            if frame == b"OggS":
                for data, is_complete in OggPage(self.stream).iter_packets():
                    buffer += data
                    if is_complete:
                        yield buffer
                        buffer = b""


class AudioSource:
    bitrate = 128

    def __init__(self, source: "str | IO[bytes]"):
        self.input = source
        subprocess_kwargs = {
            "stdout": subprocess.PIPE,
        }

        if isinstance(source, str):
            args = ("ffmpeg", "-i", source)
        else:
            subprocess_kwargs["stdin"] = source
            args = ("ffmpeg", "-i", "pipe:0")

        args += (
            "-map_metadata",
            "-1",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
            "-f",
            "opus",
            "-c:a",
            "libopus",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-b:a",
            f"{self.bitrate}k",
            "-vn",
            "-loglevel",
            "warning",
            "-hls_time",
            "10",
            "pipe:1",
        )

        self.process = subprocess.Popen(args, **subprocess_kwargs)

        self.packet_iter = OggStream(self.process.stdout)

    def iter_packets(self):
        yield from self.packet_iter
