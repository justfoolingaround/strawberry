"""RTX: retransmission of lost RTP packets (RFC 4588).

What is cached, in what order, and how much may be replayed. The wire side
stays with the connection, because sending one needs the key and the socket.
"""

import threading
from collections import OrderedDict
from dataclasses import dataclass

#: How many sent video packets stay eligible for retransmission. Large enough
#: to cover a NACK that arrives a second late, small enough that a long stream
#: does not grow without bound (the reference uses the same bound).
RTX_CACHE_MAX_PACKETS = 16384
#: A single NACK names up to seventeen sequences per FCI entry, so cap what
#: one report may trigger. Without a cap a hostile or corrupted report could
#: ask for a flood of retransmissions.
RTX_MAX_PER_NACK = 64
#: Coarse backstop across a whole second, so a receiver that keeps asking (or
#: a loop between the server and a viewer) cannot keep this busy forever.
RTX_MAX_PER_SECOND = 400


@dataclass(frozen=True, slots=True)
class CachedPacket:
    """One sent video packet, kept so it can be retransmitted.

    `payload` is the plaintext body (extension plus media chunk) rather than
    the ciphertext: the RTP header is the AEAD's associated data, so a
    retransmission under a new RTX header has to be re-encrypted from the
    original bytes.
    """

    payload: bytes
    timestamp: bytes
    marker: bool


class RtxCache:
    """Bounded, insertion-ordered store of recently sent video packets.

    One `OrderedDict` rather than a cache plus a parallel order deque: the
    eviction order *is* the insertion order, so the two can never drift
    apart, and a hit does not reorder (a retransmitted packet is still
    eligible for a later retransmission, within the bound).
    """

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._entries: OrderedDict[int, CachedPacket] = OrderedDict()
        self._sequence = 0
        self.sent = 0

    def record(self, sequence: int, packet: CachedPacket) -> None:
        self._entries[sequence] = packet
        self._entries.move_to_end(sequence)
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)

    def get(self, sequence: int) -> "CachedPacket | None":
        return self._entries.get(sequence)

    def next_sequence(self) -> int:
        """Sequence number for the next RTX packet; its own space."""
        self._sequence = (self._sequence + 1) & 0xFFFF
        return self._sequence

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


class NackGate:
    """Rate-limits how much retransmission one connection may produce.

    The per-NACK cap alone is not a bound: a receiver that keeps asking
    (or a loop between the server and a viewer) would otherwise keep this
    busy indefinitely. This is the coarse backstop.
    """

    def __init__(self, per_nack: int, per_second: int) -> None:
        self._per_nack = per_nack
        self._per_second = per_second
        self._lock = threading.Lock()
        self._window_started = 0.0
        self._in_window = 0

    def allow(self, now: float, wanted: int) -> int:
        """How many of the `wanted` retransmissions are permitted now."""
        limit = min(wanted, self._per_nack)
        with self._lock:
            if now - self._window_started >= 1.0:
                self._window_started = now
                self._in_window = 0
            room = self._per_second - self._in_window
            granted = max(0, min(limit, room))
            self._in_window += granted
        return granted
