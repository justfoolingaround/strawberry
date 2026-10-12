"""What a packetizer actually needs from the connection it sends on.

The packetizers and `BaseConnection` otherwise import each other: the
connection instantiates packetizers, and the packetizers take a connection.
That is a cycle, and it is only avoidable because the packetizers import
`BaseConnection` under `TYPE_CHECKING`.

Narrowing the dependency to the three methods the packetizers really call
breaks the cycle outright - this module imports nothing - and documents the
contract in one place. `BaseConnection` satisfies it structurally, so no
inheritance is involved and a test double needs no base class at all.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class MediaConnection(Protocol):
    #: `--dump-rtp`: print each packet's plaintext structure before encryption.
    dump_rtp: bool
    """The send side of a media connection, as packetizers see it."""

    def encrypt_data(self, header: bytes, payload: bytes) -> bytes:
        """Transport-encrypt one packet: (RTP header, body) -> datagram."""
        ...

    def send_packet(self, packet: bytes, media: "str | None" = None) -> None:
        """Hand a finished datagram to the transport."""
        ...

    def note_rtp_packet(
        self, media: str, size: int, ssrc: int = 0, seq: int = 0
    ) -> None:
        """Record a packet for the send summary and RTCP attribution."""
        ...
