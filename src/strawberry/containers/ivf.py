"""IVF: the container VP8, VP9 and AV1 arrive in.

A 32 byte file header, then a 12 byte header per frame with its length and
timestamp - a raw VPx/AV1 pipe has no framing of its own. Nothing here is sent;
the packetizer sees the frame bodies with these headers already gone.
"""

from collections.abc import Iterator

#: Per-frame header: u32 length, u64 timestamp.
IVF_FRAME_HEADER_SIZE = 12
#: File header: "DKIF", version, header length, fourcc, size, rate, scale,
#: frame count, unused.
IVF_FILE_HEADER_SIZE = 32
#: What a file header starts with.
IVF_MAGIC = b"DKIF"


class IvfFrameIterator:
    """Whole frames out of an IVF stream, fed a chunk at a time.

    Chunk fed rather than file-at-once because the same parser serves a live
    pipe: frames are yielded as soon as their bytes arrive, and a partial
    frame is kept until the rest of it does.
    """

    __slots__: tuple[str, ...] = ("buffer", "started")

    buffer: bytes
    started: bool

    def __init__(self) -> None:
        self.buffer = b""
        self.started = False

    def iter_frames(self, chunk: bytes) -> Iterator[bytes]:
        self.buffer += chunk
        if not self.started:
            if len(self.buffer) < IVF_FILE_HEADER_SIZE:
                return
            if self.buffer[:4] != IVF_MAGIC:
                return
            # ffmpeg writes 32 bytes, but the field is authoritative: read it
            # rather than assuming, and fall back to the spec length if it is
            # empty.
            header_length = int.from_bytes(self.buffer[6:8], "little")
            if not header_length:
                header_length = IVF_FILE_HEADER_SIZE
            self.buffer = self.buffer[header_length:]
            self.started = True

        while len(self.buffer) >= IVF_FRAME_HEADER_SIZE:
            size = int.from_bytes(self.buffer[0:4], "little")
            if len(self.buffer) < IVF_FRAME_HEADER_SIZE + size:
                return
            yield self.buffer[IVF_FRAME_HEADER_SIZE : IVF_FRAME_HEADER_SIZE + size]
            self.buffer = self.buffer[IVF_FRAME_HEADER_SIZE + size :]
