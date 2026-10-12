"""Annex B: the framing H.264 and H.265 arrive in.

No length prefix - a NAL unit ends where the next start code begins, which is
why emulation prevention bytes exist. Not a wire format: RTP carries NAL units
and never a start code.
"""

from collections.abc import Iterator

from strawberry.codecs import NalSpec, nal_type

#: 3 and 4 byte start codes. Both occur; the 4 byte one is the same thing with
#: a leading zero, and a parser has to accept either.
NAL_START_CODE = b"\x00\x00\x01"
ANNEX_B_START_CODE = b"\x00\x00\x00\x01"


def split_annex_b(payload: bytes) -> list[bytes]:
    """NAL units out of an Annex B buffer, start codes stripped.

    Splitting on the 3 byte start code, rather than scanning byte by byte, is
    not a micro-optimisation: the scan costs 2.65 ms on a 1080p key frame and
    runs once per frame on the decode thread, which is 159 ms of every second
    spent in Python. Splitting is ~265x faster.

    A 4 byte start code is the same thing with a leading zero, so splitting on
    the 3 byte form leaves that zero on the end of the unit before it - drop
    it, exactly as consuming four bytes did. Not for the last unit, which has
    no start code after it and may legitimately end in a trailing zero byte.
    """
    units = []
    pieces = payload.split(NAL_START_CODE)
    last = len(pieces) - 1
    for index, unit in enumerate(pieces):
        if not unit:
            continue
        if index != last and unit.endswith(b"\x00"):
            unit = unit[:-1]
        if unit:
            units.append(unit)
    return units


class NalPacketIterator:
    """Splits an Annex B stream into access units of NAL units.

    Chunk fed, and access-unit aware rather than NAL aware: what goes on the
    wire is one access unit at a time, because that is the unit DAVE encrypts
    and the unit a frame's RTP timestamp describes.
    """

    def __init__(self, spec: NalSpec) -> None:
        self.spec = spec
        self.buffer = b""
        self.access_unit: list[bytes] = []

    def iter_access_units(self, chunk: bytes) -> Iterator[list[bytes]]:
        frames: list[bytes] = []
        if chunk:
            self.buffer += chunk
            *frames, self.buffer = self.buffer.split(NAL_START_CODE)

        for frame in frames:
            # A 4-byte start code splits on the 3-byte one, so the 4th byte
            # rides along - on the tail of the NAL unit before it, and on its
            # own before the first one. Drop the former: a NAL unit must not
            # carry start code bytes. (samples/discord-native-voice
            # src/video/h26x.rs split_annex_b takes the bytes strictly
            # between start codes and does the same.)
            if frame and frame[-1] == 0:
                frame = frame[:-1]

            if not frame:
                continue

            if nal_type(self.spec, frame) == self.spec.access_unit_delimiter:
                # The delimiter closes the previous access unit and leads the
                # next one. Both references packetize it like any other NAL
                # unit (samples/discord-native-voice calls split_annex_b,
                # which keeps the AUD; libdatachannel's splitFrame does too).
                if self.access_unit:
                    yield self.access_unit
                    self.access_unit = []
                self.access_unit.append(frame)
            else:
                # NAL units go out verbatim: receivers expect EBSP, and
                # stripping emulation prevention bytes rewrites every
                # SPS/VPS into something the decoder will not parse.
                self.access_unit.append(frame)

        # End of stream: flush what is left. Without this the final access
        # unit is never sent, and a stream carrying no access unit delimiter
        # - AUD is an encoder option, not part of the format - yields nothing
        # whatsoever.
        if not chunk and self.access_unit:
            yield self.access_unit
            self.access_unit = []
