"""RTCP: the control half of RTP.

The payload type alone does not identify a packet: 205 is RTPFB, whose low
five bits are a *format* (1 Generic NACK, 15 transport-cc), and 206 is PSFB
(1 PLI, 4 FIR). Reading the type and ignoring the format made every congestion
report look like packet loss.
"""

import struct
from collections.abc import Iterator

#: RTCP Sender Report.
RTCP_SR = 200
#: RTCP Receiver Report.
RTCP_RR = 201
#: RTPFB: feedback about RTP itself. Its format field selects which.
RTCP_RTPFB = 205
#: PSFB: payload-specific feedback.
RTCP_PSFB = 206

#: RTPFB format 1.
RTCP_GENERIC_NACK = 1
#: RTPFB format 15.
RTCP_TRANSPORT_CC = 15
#: PSFB format 1 and 4.
RTCP_PLI = 1
RTCP_FIR = 4

#: Bytes of an RTCP packet the transport leaves in the clear: the header and
#: the sender SSRC. They are authenticated rather than encrypted, so they are
#: the AEAD's associated data and the rest is the ciphertext.
RTCP_HEADER_LEN = 8
#: Milliseconds between the NTP epoch (1900) and the Unix epoch (1970).
NTP_EPOCH_MS = -2_208_988_800_000
#: Largest value the 32-bit RTP fields wrap at.
MAX_UINT32 = 0xFFFFFFFF

#: A sender report: `V=2 P=0 RC=0 | 200 | length | SSRC` plus twenty bytes of
#: sender info. Length is in 32-bit words *minus one*, hence the 6.
_SENDER_REPORT = struct.Struct(">BBBBI" "IIIII")
#: Header of every RTCP packet: version/flags/report-count, type, length.
_HEADER = struct.Struct(">BBH")
#: The SSRC that follows the header in every feedback and report packet.
_SSRC = struct.Struct(">I")
#: A NACK's FCI entry: the sequence number and a bitmask of the 16 after it.
_NACK_ENTRY = struct.Struct(">HH")

#: Payload types that carry no format of their own.
RTCP_NAMES: "dict[int, str]" = {
    RTCP_SR: "SR",
    RTCP_RR: "RR",
    202: "SDES",
    203: "BYE",
    204: "APP",
    207: "REMB",
    64: "FIR",  # the old standalone type; 206/4 is what is in use
}

#: Payload types whose meaning is selected by their format field.
RTCP_FEEDBACK_NAMES: "dict[tuple[int, int], str]" = {
    (RTCP_RTPFB, RTCP_GENERIC_NACK): "NACK",
    (RTCP_RTPFB, RTCP_TRANSPORT_CC): "transport-cc",
    (RTCP_PSFB, RTCP_PLI): "PLI",
    (RTCP_PSFB, RTCP_FIR): "FIR",
}


def rtcp_kind(data: bytes) -> "str | None":
    """Name an RTCP packet, or None if it is not one.

    The format field matters and is not optional: 205 is both Generic NACK
    (format 1) and transport-wide congestion control (format 15), and 206 is
    both PLI (format 1) and FIR (format 4).
    """
    if len(data) < 2:
        return None
    payload_type = data[1]
    fmt = data[0] & 0x1F
    named = RTCP_FEEDBACK_NAMES.get((payload_type, fmt))
    if named is not None:
        return named
    return RTCP_NAMES.get(payload_type)


def parse_rtcp_nack(body: bytes) -> Iterator[tuple[int, int]]:
    """Yield `(media_ssrc, sequence)` for the packets a NACK asks for.

    `body` is a whole RTCP packet as it comes back from the transport
    decryption - header included:

        V=2 P=0 FMT=1 | 205 | length    | sender SSRC
        media SSRC    | PID | BLP  ...  (one entry per four bytes)

    A BLP bit means "the sequence 1..16 after the PID", so one entry names up
    to seventeen sequence numbers.
    """
    if len(body) < _HEADER.size + 2 * _SSRC.size:
        return
    first, payload_type, _length = _HEADER.unpack_from(body)
    # the format is the low five bits of the first byte, not the whole byte
    if payload_type != RTCP_RTPFB or first & 0x1F != RTCP_GENERIC_NACK:
        return

    media_ssrc, = _SSRC.unpack_from(body, _HEADER.size + _SSRC.size)
    offset = _HEADER.size + 2 * _SSRC.size
    while offset + _NACK_ENTRY.size <= len(body):
        pid, blp = _NACK_ENTRY.unpack_from(body, offset)
        yield media_ssrc, pid
        for bit in range(16):
            if blp & (1 << bit):
                yield media_ssrc, (pid + bit + 1) & 0xFFFF
        offset += _NACK_ENTRY.size


def sender_report(
    ssrc: int,
    rtp_timestamp: int,
    packets_sent: int,
    octets_sent: int,
    wall_clock_ms: int,
) -> bytes:
    """An RFC 3550 Sender Report: 28 bytes, no report blocks.

    The NTP field is what ties the RTP clock to a wall clock, and the counts
    are what a forwarding middlebox uses to keep its own accounting straight.
    Without it the stream declares no timing reference at all.
    """
    seconds = (wall_clock_ms - NTP_EPOCH_MS) / 1000.0
    whole = int(seconds)
    fraction = int((seconds - whole) * MAX_UINT32)
    return _SENDER_REPORT.pack(
        0x80,
        RTCP_SR,
        0x00,
        0x06,
        ssrc,
        whole,
        fraction & MAX_UINT32,
        rtp_timestamp,
        packets_sent & MAX_UINT32,
        octets_sent & MAX_UINT32,
    )
