import typing
from collections.abc import Iterator, Sequence

from strawberry.codecs import VIDEO_CODECS

from .base_packetizer import BaseMediaPacketizer

if typing.TYPE_CHECKING:
    from strawberry.connection.protocol import MediaConnection

# AV1 RTP: https://aomediacodec.github.io/av1-rtp-spec/v1.0.0.html
# (still an IETF draft - IANA lists video/AV1 as [AV1-Media-Type], not an
# RFC - so the aomediacodec spec is the normative reference here).


def leb128(value: int) -> bytes:
    """AV1's variable-length integer (AV1 spec 4.10.5), little-endian."""
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0x00))
        if not value:
            return bytes(out)


def read_leb128(data: bytes, pos: int) -> "tuple[int, int]":
    """(value, bytes consumed) for a leb128 integer at `pos`."""
    value = 0
    for i in range(8):
        if pos + i >= len(data):
            raise ValueError("malformed leb128")
        byte = data[pos + i]
        value |= (byte & 0x7F) << (i * 7)
        if not byte & 0x80:
            return value, i + 1
    raise ValueError("malformed leb128")


def leb128_size(value: int) -> int:
    """Bytes :func:`leb128` would emit for `value`."""
    size = 1
    while value >= 0x80:
        value >>= 7
        size += 1
    return size


# (obu_type, the OBU's bytes with its own size field removed)
Obu = tuple[int, bytes]
ObuList = list[Obu]


class _Packet:
    """One packet's worth of OBUs, before it is rendered to bytes.

    Mirrors av1.rs `AV1Packet`. `first_obu` indexes the temporal unit's OBU
    list, `first_obu_offset` is where that OBU's bytes resume if this packet
    is a continuation, and `packet_size` counts payload bytes *excluding* the
    aggregation header - all as in the reference.
    """

    __slots__ = (
        "first_obu",
        "first_obu_offset",
        "last_obu_size",
        "num_elements",
        "packet_size",
    )

    def __init__(self, first_obu: int) -> None:
        self.first_obu = first_obu
        self.first_obu_offset = 0
        self.num_elements = 0
        self.last_obu_size = 0
        self.packet_size = 0


class AV1Packetizer(BaseMediaPacketizer):
    """AV1 RTP (aomediacodec av1-rtp-spec v1.0.0).

    The RTP payload is a one byte aggregation header followed by OBU
    elements, each preceded by its length as leb128. OBUs are aggregated
    greedily and an OBU too large for one packet is fragmented, with the
    aggregation header's Z and Y bits marking the partial ends.

    W is set to the number of OBU elements while that count is 3 or fewer,
    which is how libwebrtc (and therefore Discord's own client) sends it:
    the last element then carries no length field and the receiver derives
    it from the packet size. Past three elements every element is length
    prefixed and W is 0.
    """

    MEDIA = "video"

    codec = VIDEO_CODECS["AV1"].name

    SEQUENCE_HEADER = 1
    TEMPORAL_DELIMITER = 2  # "SHOULD be removed when transmitting"
    TILE_LIST = 8  # not supported ... SHOULD be removed. NB: 4 is TILE_GROUP.
    PADDING = 15
    # libwebrtc omits the last OBU element's length field when the packet
    # holds this many or fewer (rtp_packetizer_av1.cc kMaxNumObusToOmitSize),
    # signalling the count in W. Above it, every element is length prefixed
    # and W is 0.
    MAX_OBUS_TO_OMIT_SIZE = 3

    def __init__(self, extensions_enabled: bool = True):
        super().__init__(
            VIDEO_CODECS["AV1"].payload_type, extensions_enabled=extensions_enabled
        )
        self.fps = 30

    # -- OBUs ---------------------------------------------------------------

    def iter_obus(self, data: bytes) -> "Iterator[Obu]":
        """Yield (ob_type, obu_bytes) over a temporal unit.

        The OBU's own `obu_size` field is dropped and its
        `obu_has_size_field` bit cleared: libwebrtc removes both and
        signals the length in the RTP payload instead. See
        `RemovesObuSizeField` in rtp_packetizer_av1_unittest.cc, whose
        expected packet for a type 6 OBU is
        `0x10, 0x06, <payload...>` - header byte 0x06, not 0x32.
        """
        pos = 0
        while pos < len(data):
            header = data[pos]
            pos += 1
            if header & 0x80:  # obu_forbidden_bit, must be zero
                return  # corrupt: stop rather than emit garbage
            obu_type = (header >> 3) & 0x0F
            prefix = bytes([header & ~0x02])  # clear obu_has_size_field
            if header & 0x04:  # obu_extension_flag: keep the extension header
                prefix += data[pos : pos + 1]
                pos += 1
            if header & 0x02:  # obu_has_size_field
                size, used = read_leb128(data, pos)
                pos += used
            else:
                size = len(data) - pos  # runs to the end of the temporal unit
            yield obu_type, prefix + data[pos : pos + size]
            pos += size

    def describe_payload(self, body: bytes) -> str:
        if not body:
            return "empty"
        agg = body[0]
        return (
            f"AV1 Z={agg >> 7} Y={(agg >> 6) & 1} W={(agg >> 4) & 3} "
            f"N={(agg >> 3) & 1}"
        )

    def is_keyframe(self, frame: bytes) -> bool:
        """Whether a temporal unit starts a coded video sequence.

        AV1 has no IDR bit to read: the encoder emits a sequence header OBU
        only on a random access point, and that is what the RTP aggregation
        header's N bit is derived from. Same signal, so a frame a receiver
        can start from is exactly a frame this returns True for.

        The reference never caches an AV1 key frame at all - its
        _is_config_keyframe looks the codec up in _H26X_PARAMETER_SET_TYPES,
        which has no AV1 entry, so it returns False for every AV1 frame and
        the replay list stays empty. This is the case it is missing.
        """
        return any(
            obu_type == self.SEQUENCE_HEADER for obu_type, _ in self.iter_obus(frame)
        )

    # -- packetization ------------------------------------------------------
    # av1.rs: MIN_BYTES_SAVED_PER_PACKET_WITH_EVEN_DISTRIBUTION /
    # BYTES_OVERHEAD_EVEN_DISTRIBUTION
    EVEN_DISTRIBUTION_MIN_SAVING = 10
    EVEN_DISTRIBUTION_OVERHEAD = 1
    AGGREGATION_HEADER_SIZE = 1

    def _max_fragment_size(self, remaining: int) -> int:
        """Largest fragment whose length field still fits in `remaining`.

        av1.rs max_fragment_size: a fragment of `n` bytes costs
        `n + leb128_size(n)`, so step up until the two together fit.
        """
        if remaining <= 1:
            return 0
        nbytes = 1
        while True:
            if remaining < (1 << (7 * nbytes)) + nbytes:
                return remaining - nbytes
            nbytes += 1

    def _previous_extra(self, packet: "_Packet") -> int:
        """Size field the packet's last element will have to grow by.

        An element is written without a length field only when it stays the
        packet's last one and the packet holds 3 or fewer elements. Adding
        another element to the packet can break that, so the bytes have to be
        found afterwards - which is why the reference books them against the
        element that *precedes* the one being added.
        """
        if packet.packet_size == 0 or packet.num_elements > self.MAX_OBUS_TO_OMIT_SIZE:
            return 0
        return leb128_size(packet.last_obu_size)

    def _packetize(self, obus: "ObuList", max_payload_len: int) -> "list[_Packet]":
        """Distribute OBUs into packets (av1.rs packetize_internal).

        `max_payload_len` is the whole RTP payload budget; the aggregation
        header takes its first byte. Sizes recorded on the packets are the
        payload size *excluding* that header, matching the reference.

        The reference reserves a few bytes off the first and last packets
        (AV1PayloadSizeLimits) so the frame's edges are not crowded. Every
        one of those reductions is zero here, so they are left out rather
        than carried as dead parameters.
        """
        if not obus:
            return []

        if max_payload_len - self.AGGREGATION_HEADER_SIZE < 3:
            raise ValueError("payload too small for an AV1 aggregation header")

        room = max_payload_len - self.AGGREGATION_HEADER_SIZE
        packets = [_Packet(0)]
        remaining = room

        for index, (_obu_type, obu) in enumerate(obus):
            packet = packets[-1]
            extra = self._previous_extra(packet)
            # Room for at least the OBU header byte, or 2 when a length field
            # is already certain (the packet is full enough that this element
            # cannot be the size-less last one).
            minimum = 2 if packet.num_elements >= self.MAX_OBUS_TO_OMIT_SIZE else 1
            if remaining < extra + minimum:
                packets.append(_Packet(index))
                remaining = room
                extra = 0

            packet = packets[-1]
            packet.packet_size += extra
            remaining -= extra
            packet.num_elements += 1

            must_write_size = packet.num_elements > self.MAX_OBUS_TO_OMIT_SIZE
            size = len(obu)
            required = size + (leb128_size(size) if must_write_size else 0)

            if required <= remaining:
                packet.last_obu_size = size
                packet.packet_size += required
                remaining -= required
                continue

            # Too big for what's left: fragment it, taking as much of this
            # packet as we can first.
            limit = self._max_fragment_size(remaining) if must_write_size else remaining
            first_fragment = min(size - 1, limit)

            packet = packets[-1]
            if first_fragment == 0:
                # Nothing fits; the element never made it into this packet.
                packet.num_elements -= 1
                packet.packet_size -= extra
            else:
                packet.packet_size += first_fragment
                if must_write_size:
                    packet.packet_size += leb128_size(first_fragment)
                packet.last_obu_size = first_fragment

            offset = first_fragment
            # Full packets in the middle of the OBU.
            while offset + room < size:
                middle = _Packet(index)
                middle.num_elements = 1
                middle.first_obu_offset = offset
                middle.last_obu_size = room
                middle.packet_size = room
                packets.append(middle)
                offset += room

            last_fragment = size - offset
            tail = _Packet(index)
            tail.num_elements = 1
            tail.first_obu_offset = offset
            tail.last_obu_size = last_fragment
            tail.packet_size = last_fragment
            packets.append(tail)
            remaining = room - last_fragment

        return packets

    def _packetize_obus(self, obus: "ObuList", max_payload_len: int) -> "list[_Packet]":
        """Pack, then optionally re-pack at a tighter, more even budget.

        The first pass leaves a lot of slack in the early packets and crams
        the tail. The reference shrinks the budget by the average unused
        capacity and packs again, keeping the result only if it still yields
        the same packet count (av1.rs packetize_obus).
        """
        packets = self._packetize(obus, max_payload_len)
        if len(packets) <= 1:
            return packets

        room = max_payload_len - self.AGGREGATION_HEADER_SIZE
        unused = sum(room - p.packet_size for p in packets if room >= p.packet_size)
        if unused <= len(packets) * self.EVEN_DISTRIBUTION_MIN_SAVING:
            return packets

        reduction = unused // len(packets)
        if max_payload_len <= reduction or reduction <= self.EVEN_DISTRIBUTION_OVERHEAD:
            return packets

        reduced = max_payload_len - reduction + self.EVEN_DISTRIBUTION_OVERHEAD
        if reduced - 1 < 3:
            return packets

        even = self._packetize(obus, reduced)
        return even if len(even) == len(packets) else packets

    def _aggregation_header(self, obus: "ObuList", packet: _Packet, index: int) -> int:
        """The one byte that opens every payload (av1.rs aggregation_header)."""
        header = 0

        if packet.first_obu_offset > 0:
            header |= 0x80  # Z: this packet starts mid-OBU

        last_index = packet.first_obu + packet.num_elements - 1
        # Only a single-element packet can start partway through its OBU;
        # otherwise the last element starts at 0.
        last_offset = packet.first_obu_offset if packet.num_elements == 1 else 0
        if last_offset + packet.last_obu_size < len(obus[last_index][1]):
            header |= 0x40  # Y: this packet ends mid-OBU

        if packet.num_elements <= self.MAX_OBUS_TO_OMIT_SIZE:
            header |= packet.num_elements << 4  # W, count with size omitted

        # N marks the start of a coded video sequence. libwebrtc takes it
        # from a caller-declared key frame; we have no such hint, so we use
        # the only signal in the bitstream - a leading sequence header OBU,
        # which encoders emit only on key frames.
        if index == 0 and obus[0][0] == self.SEQUENCE_HEADER:
            header |= 0x08

        return header

    def _write_packet(self, obus: "ObuList", packet: _Packet, index: int) -> bytes:
        """Render one packet's payload (av1.rs write_packet_payload)."""
        payload = bytearray()
        payload.append(self._aggregation_header(obus, packet, index))

        offset = packet.first_obu_offset
        for position in range(packet.num_elements - 1):
            element = obus[packet.first_obu + position][1]
            fragment = len(element) - offset
            payload.extend(leb128(fragment))
            payload.extend(element[offset : offset + fragment])
            offset = 0

        element = obus[packet.first_obu + packet.num_elements - 1][1]
        if packet.num_elements > self.MAX_OBUS_TO_OMIT_SIZE:
            payload.extend(leb128(packet.last_obu_size))
        payload.extend(element[offset : offset + packet.last_obu_size])

        return bytes(payload)

    def send_frame(self, conn: "MediaConnection", chunks: "Sequence[bytes]"):
        # One temporal unit per call, same as VPx: reject a multi-chunk frame
        # rather than slicing it into a single frame, which would put two
        # pictures in one RTP timestamp.
        if len(chunks) != 1:
            raise ValueError(
                f"AV1Packetizer takes exactly one temporal unit per call, "
                f"got {len(chunks)}"
            )
        frame = chunks[0]
        obus = [
            (obu_type, obu)
            for obu_type, obu in self.iter_obus(frame)
            if obu_type
            not in (self.TEMPORAL_DELIMITER, self.TILE_LIST, self.PADDING)
        ]
        if not obus:
            return

        packets = self._packetize_obus(obus, self.mtu)
        self.emit(
            conn,
            [
                self._write_packet(obus, packet, index)
                for index, packet in enumerate(packets)
            ],
        )
        self.advance_timestamp()
