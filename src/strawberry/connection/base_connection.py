"""
The seam between packetizers and a media connection.

Packetizers are handed a connection per call instead of holding a
reference to one, so neither side owns the other: no reference cycles, no
packetizer keeping a dead connection alive.

This class also owns the pieces both sides need - the packetizers
themselves, the SSRCs, the transport encryption and the send accounting -
which leaves StreamConnection to the parts that really are connection
specific: the websocket, the UDP endpoint and the DAVE session.
"""

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from nacl.bindings import (
    crypto_aead_aes256gcm_decrypt,
    crypto_aead_aes256gcm_encrypt,
    crypto_aead_xchacha20poly1305_ietf_decrypt,
    crypto_aead_xchacha20poly1305_ietf_encrypt,
)

from strawberry.codecs import OPUS_PAYLOAD_TYPE, VIDEO_CODECS

# Aliased deliberately: __init__ takes a `video_codec` keyword, so importing
# the helper under its own name would be shadowed inside every method body
# (a `video_codec(x)` call there resolves to the string parameter).
from strawberry.codecs import video_codec as resolve_codec
from strawberry.packetizers import AudioPacketizer, video_packetizer
from strawberry.protocols.rtx import (
    RTX_CACHE_MAX_PACKETS,
    RTX_MAX_PER_NACK,
    RTX_MAX_PER_SECOND,
    CachedPacket,
    NackGate,
    RtxCache,
)

from .state import (
    CodecAdvertisement,
    FeedbackCounters,
    ReportWindow,
    SendStats,
)

logger = logging.getLogger("base_connection")


TRANSPORT_MODE = "aead_xchacha20_poly1305_rtpsize"
AES_TRANSPORT_MODE = "aead_aes256_gcm_rtpsize"

# Ordered by preference: the server is told the first one we both support in
# select_protocol, so this list is what actually picks the cipher.
# discord.py does the same ("supported_modes is ordered by preference",
# gateway.py initial_connection).
SUPPORTED_MODES = (AES_TRANSPORT_MODE, TRANSPORT_MODE)


@dataclass(frozen=True)
class EncryptionMode:
    """One of Discord's `aead_*_rtpsize` transport modes.

    Both carry a 4-byte little-endian counter after the ciphertext and build
    the nonce from it, so no counter is exchanged - that is what "rtpsize"
    means. They differ in the cipher, and so in how long a nonce it takes.
    """

    encrypt: Callable[[bytes, bytes, bytes, bytes], bytes]
    decrypt: Callable[[bytes, bytes, bytes, bytes], bytes]
    #: Nonce length the cipher wants, counter included.
    nonce_size: int

    def nonce(self, counter: int) -> bytes:
        return counter.to_bytes(4, "little") + b"\x00" * (self.nonce_size - 4)


#: Every mode we can speak, by the name Discord uses for it. Dispatch goes
#: through here rather than `if mode == ...` so adding a cipher is a row in a
#: table and not a change to each function that touches one.
ENCRYPTION_MODES: "dict[str, EncryptionMode]" = {
    TRANSPORT_MODE: EncryptionMode(
        crypto_aead_xchacha20poly1305_ietf_encrypt,
        crypto_aead_xchacha20poly1305_ietf_decrypt,
        24,
    ),
    AES_TRANSPORT_MODE: EncryptionMode(
        crypto_aead_aes256gcm_encrypt,
        crypto_aead_aes256gcm_decrypt,
        12,
    ),
}

# How often the send summary is logged.
REPORT_INTERVAL = 10.0

# -- transport encryption (aead_*_rtpsize) -------------------------------
#
# Both modes carry a 4-byte counter suffix after the ciphertext and rebuild
# the nonce from it, so a receiver needs no counter exchange ("rtpsize").
# The reference negotiates either
# (samples/discord-native-voice src/transport/crypto.rs).


# The counter starts here rather than at zero, and is written little endian.
# Both match samples/discord-native-voice src/transport/crypto.rs
# (INITIAL_LITE_NONCE, and `nonce_value.to_le_bytes()` on the wire). A wrong
# start or byte order still decrypts against a tolerant peer, which is why it
# went unnoticed while H264 kept working.
INITIAL_LITE_NONCE = 0x8000_0000


def rtpsize_nonce(counter: int, mode: str = TRANSPORT_MODE) -> bytes:
    """The on-wire 4-byte counter, zero-padded to the cipher's nonce length."""
    return ENCRYPTION_MODES[mode].nonce(counter)


def _aead_encrypt(
    message: bytes, aad: bytes, nonce: bytes, key: bytes, mode: str
) -> bytes:
    return ENCRYPTION_MODES[mode].encrypt(message, aad, nonce, key)


def _aead_decrypt(
    ciphertext: bytes, aad: bytes, nonce: bytes, key: bytes, mode: str
) -> bytes:
    return ENCRYPTION_MODES[mode].decrypt(ciphertext, aad, nonce, key)


def encrypt_rtp(
    header: bytes,
    payload: bytes,
    key: bytes,
    counter: int,
    mode: str = TRANSPORT_MODE,
) -> bytes:
    """Encrypt one RTP packet. `payload` is everything after the RTP header
    (the RFC 5285 extension body and the media chunk). If the extension bit
    is set, the 4-byte extension preamble is authenticated, not encrypted."""
    if header[0] & 0x10:
        aad, message = bytes(header + payload[:4]), bytes(payload[4:])
    else:
        aad, message = bytes(header), bytes(payload)
    ciphertext = _aead_encrypt(message, aad, rtpsize_nonce(counter, mode), key, mode)
    return aad + ciphertext + counter.to_bytes(4, "little")


def decrypt_rtp(
    packet: bytes, key: bytes, mode: str = TRANSPORT_MODE
) -> tuple[bytes, bytes]:
    """Inverse of `encrypt_rtp`; returns (header, payload-after-header)."""
    header_size = 12 + 4 * (packet[0] & 0x0F)
    if packet[0] & 0x10:
        header_size += 4
    header, ciphertext = packet[:header_size], packet[header_size:-4]
    counter = int.from_bytes(packet[-4:], "little")
    message = _aead_decrypt(ciphertext, header, rtpsize_nonce(counter, mode), key, mode)
    return header, message


# RTCP carries 8 authenticated bytes in front of the ciphertext, where RTP
# carries a 12 byte header (or 16 with the extension preamble). The shape is
# otherwise identical: ciphertext, then a 4 byte counter that forms the nonce.
RTCP_HEADER_LEN = 8


def decrypt_rtcp(packet: bytes, key: bytes, mode: str = TRANSPORT_MODE) -> bytes:
    """Decrypt one RTCP packet, returning it whole: header plus body.

    The first 8 bytes (the RTCP header and the sender SSRC) are
    authenticated in the clear; the rest - media SSRC and FCI - is
    ciphertext. They are rejoined here so callers get a packet they can
    parse the same way regardless of the transport encryption.

    Raises on anything that does not authenticate - which is the whole
    point. Media forwarded to us is transport-encrypted too, so it arrives
    as ciphertext; without this check random bytes would be parsed as RTCP
    and, since the NACK FCI is a list of 16 bit sequence numbers, would
    match against our sent-packet window constantly. An earlier attempt did
    exactly that and turned 10k junk packets into 6.1M injected datagrams,
    drowning the audio and the video. Only authenticated feedback is acted
    on.
    """
    if len(packet) < RTCP_HEADER_LEN + 4 + 16:
        raise ValueError("RTCP packet too short")
    header = packet[:RTCP_HEADER_LEN]
    ciphertext = packet[RTCP_HEADER_LEN:-4]
    # Little endian, the same as `encrypt_rtp` writes it and the same as the
    # reference (samples/discord-native-voice src/transport/crypto.rs line
    # 100: `nonce_value.to_le_bytes()` for RTCP as well as RTP). Reading it
    # big endian built a different nonce, so every RTCP packet failed
    # authentication and was counted as `rtcp-ish` instead of being acted on:
    # the NACKs that reported our lost video packets and the PLIs asking for a
    # keyframe were all discarded unread. The stream looked like it was
    # working while every retransmission request was thrown away.
    counter = int.from_bytes(packet[-4:], "little")
    body = _aead_decrypt(ciphertext, header, rtpsize_nonce(counter, mode), key, mode)
    return header + body


class BaseConnection:
    """Media side of a connection: packetizers, SSRCs, transport crypto."""

    def __init__(
        self,
        video_codec: str = "H264",
        video_extension: bool = True,
        audio_packetizer=AudioPacketizer,
        clock=None,
    ):
        self.logger = logging.getLogger("base_connection")
        # a plain clock, not an event loop: senders call in from threads
        self._clock = clock or time.monotonic

        self.secret_key: bytes | None = None
        self.nonce = INITIAL_LITE_NONCE
        self._nonce_lock = threading.Lock()  # sender threads share the nonce

        # "auto" (or empty) advertises every codec we can send and lets
        # Discord choose, which is what the reference client does; naming a
        # codec restricts the advertisement to that one. The value arrives
        # from the CLI already upper-cased, so match loosely.
        choice = (video_codec or "").strip().lower()
        self.requested_codec = None if choice in ("", "auto") else resolve_codec(choice)
        self.video_codec = self.requested_codec or next(iter(VIDEO_CODECS.values()))
        # one-byte RTP header extension (playout delay); receivers that did
        # not negotiate one misparse every video packet
        self.video_extension = video_extension

        self.ssrc = 0
        self.video_ssrc = 0
        self.rtx_ssrc = 0
        # aead cipher for the transport layer; op 4 tells us which one the
        # server picked, --transport-mode can force the other
        self.transport_mode = TRANSPORT_MODE
        # --dump-rtp: print the plaintext RTP header and payload structure of
        # every packet before encryption, so a codec a receiver silently
        # drops can be compared field by field against a working one
        self.dump_rtp = False
        # announced up front for camera sessions; the streamer replaces
        # these with the real ones once the source has been probed
        self.camera_params = (1280, 720, 30)
        # the resolution we are actually encoding, needed by packetizers
        # that have to signal it in-band (VP9 has no SPS to carry it)
        self.video_resolution: tuple[int, int] | None = None
        # the most recent key frame, as it was sent (DAVE-encrypted chunks),
        # so a viewer's PLI can be answered without waiting for the encoder's
        # next one - which is up to a keyint away, and a black picture in the
        # meantime
        self.keyframe: list[bytes] | None = None
        # the DAVE epoch `keyframe` is ciphertext under, or None for
        # transport-only. Only meaningful while `keyframe` is set.
        self.keyframe_epoch: object | None = None

        self.audio_packetizer = audio_packetizer()
        self.video_packetizer = video_packetizer(self.video_codec)(
            extensions_enabled=self.video_extension
        )
        self.video_packetizer.ssrc = self.video_ssrc

        # Send accounting and RTCP bookkeeping, as typed records rather than
        # parallel lists indexed by hand (see connection/state.py).
        self.stats = SendStats()
        self.feedback = FeedbackCounters()
        self.report_window = ReportWindow(self._clock)

        # RTX: retransmission. `rtx_enabled` is the policy switch; `rtx_ssrc`
        # is what the server handed us, and 0 means there is nowhere to send
        # a retransmission, so it disables the feature in practice.
        self.rtx_enabled = True
        self.rtx_cache = RtxCache(RTX_CACHE_MAX_PACKETS)
        self.nack_gate = NackGate(RTX_MAX_PER_NACK, RTX_MAX_PER_SECOND)

    # -- codecs and ssrcs --------------------------------------------------

    def set_video_codec(self, negotiated: "str | None"):
        """Adopt the codec the server picked, if we can encode and send it.

        With `--codec auto` we advertised every codec and simply follow the
        negotiation; with a pinned codec the server can only pick that one
        anyway. Either way the encoder and the packetizer end up agreeing,
        because both read `video_codec` after this runs.
        """
        if negotiated:
            try:
                self.video_codec = resolve_codec(negotiated)
            except ValueError:
                self.logger.warning(
                    "server negotiated %s, keeping %s",
                    negotiated,
                    self.video_codec.name,
                )
        # The rid belongs to the *stream*, not to the codec, and the server
        # allocates it in READY - which arrives before the
        # SESSION_DESCRIPTION that calls this and rebuilds the packetizer
        # from scratch. Rebuilding without carrying it over silently put the
        # hardcoded "100" back on every packet, so the RID extension (id 11),
        # the thing that ties a packet to the stream it belongs to, was wrong
        # for the whole session with nothing in the log to say so.
        previous = self.video_packetizer
        self.video_packetizer = video_packetizer(self.video_codec)(
            extensions_enabled=self.video_extension
        )
        self.video_packetizer.rid = previous.rid
        self.video_packetizer.ssrc = self.video_ssrc
        self.logger.info(
            "video codec: %s (rtp pt %s, ssrc %s, rtx %s)",
            self.video_codec.name,
            self.video_codec.payload_type,
            self.video_ssrc,
            self.rtx_ssrc,
        )

    def set_ssrc(
        self,
        ssrc: int,
        video_ssrc: "int | None" = None,
        rtx_ssrc: "int | None" = None,
    ):
        """READY hands out the audio ssrc, and a stream entry with the
        video/rtx ones. Applying them here keeps the packetizers and the
        VIDEO payload from drifting apart."""
        self.ssrc = ssrc
        self.video_ssrc = 0 if video_ssrc is None else video_ssrc
        self.rtx_ssrc = 0 if rtx_ssrc is None else rtx_ssrc
        self.audio_packetizer.ssrc = self.ssrc
        self.video_packetizer.ssrc = self.video_ssrc

    # -- transport ---------------------------------------------------------

    def encrypt_data(self, header: bytes, payload: bytes) -> bytes:
        assert self.secret_key is not None, "no session key yet"
        # Cached before encryption: RTX re-encrypts the payload under a new
        # header, and the header is the AEAD's associated data, so the
        # original ciphertext cannot be replayed as-is.
        self._cache_for_rtx(header, payload)
        with self._nonce_lock:
            self.nonce = (self.nonce + 1) & 0xFFFFFFFF
            counter = self.nonce
        return encrypt_rtp(
            header, payload, self.secret_key, counter, self.transport_mode
        )

    # -- RTX ---------------------------------------------------------------

    def _cache_for_rtx(self, header: bytes, payload: bytes) -> None:
        """Keep enough of each video packet to rebuild it if it is lost.

        Stores the *media* payload, not the whole body: an RTX packet is
        rebuilt with a fresh header extension (its own transport-wide
        sequence number), so caching the extension as well would duplicate
        it on retransmission. The fields RTX must preserve - the timestamp
        and the marker bit - are kept alongside, keyed by sequence number.
        """
        if not self.rtx_enabled or len(header) < 12:
            return
        # Only the negotiated video payload type. This also keeps RTX packets
        # out of the cache, so a retransmission can never be retransmitted.
        if header[1] & 0x7F != self.video_codec.payload_type:
            return
        # RFC 5285 preamble: the first 4 bytes of the body carry the profile
        # and a length in 32-bit words. (This implementation leaves the
        # header's CSRC count at 0 and keeps only the preamble authenticated,
        # so the length has to be read from the preamble rather than CC.)
        extension_bytes = 0
        if header[0] & 0x10 and len(payload) >= 4:
            declared = 4 + 4 * int.from_bytes(payload[2:4], "big")
            # Clamp: a truncated or nonsense length must not silently eat
            # the whole payload and cache an empty packet for retransmission.
            extension_bytes = min(declared, len(payload))
        self.rtx_cache.record(
            int.from_bytes(header[2:4], "big"),
            CachedPacket(
                bytes(payload[extension_bytes:]),
                bytes(header[4:8]),
                bool(header[1] & 0x80),
            ),
        )

    def send_rtx(self, sequence: int) -> bool:
        """Resend one video packet under the RTX payload type (RFC 4588).

        Returns True if the packet was in the cache and has been resent.
        """
        key = self.secret_key
        if not self.rtx_enabled or not self.rtx_ssrc or key is None:
            return False
        entry = self.rtx_cache.get(sequence)
        if entry is None:
            return False

        header = (
            bytes(
                (
                    0x80 | (0x10 if self.video_extension else 0x00),
                    self.video_codec.rtx_payload_type
                    | (0x80 if entry.marker else 0x00),
                )
            )
            + self.rtx_cache.next_sequence().to_bytes(2, "big")
            + entry.timestamp
            + self.rtx_ssrc.to_bytes(4, "big")
        )
        # OSN tells the receiver which sequence number this replaces; the
        # original payload follows verbatim, minus its own header.
        extension = self.video_packetizer.header_extension
        body = extension + sequence.to_bytes(2, "big") + entry.payload

        with self._nonce_lock:
            self.nonce = (self.nonce + 1) & 0xFFFFFFFF
            counter = self.nonce
        packet = encrypt_rtp(header, body, key, counter, self.transport_mode)
        self.send_packet(packet, "video")
        self.rtx_cache.sent += 1
        return True

    def send_packet(self, packet: bytes, media: "str | None" = None) -> None:
        raise NotImplementedError

    # -- media -------------------------------------------------------------

    def send_audio_frame(self, frame: bytes):
        # one opus packet per call; wrapped so the packetizer interface is
        # the same shape for audio and video
        self.audio_packetizer.send_frame(self, [frame])

    def send_video_frame(self, chunks: "Sequence[bytes]"):
        """Packetize and send one encoded video frame.

        `chunks` is the frame in the codec's own units: NAL units for
        H264/H265, a single element holding the whole temporal unit for
        VP8/VP9/AV1. Every packetizer takes the same shape, so this entry
        point does not have to branch on the codec.

        Key frames are kept so a PLI can be answered by replaying one. A
        viewer that has lost everything asks for a key frame; without one to
        send it cannot decode a single following frame either, so the request
        has to be honoured promptly or the stream stays black. Which frames
        count is decided here rather than by the caller, so no caller can
        forget to say.
        """
        if self.is_keyframe(chunks):
            self._cache_keyframe(chunks)
        self.video_packetizer.send_frame(self, chunks)

    def is_keyframe(self, chunks: "Sequence[bytes]") -> bool:
        """Whether one access unit is a random access point.

        H26x: an IDR/IRAP slice *and* the parameter sets in front of it, so
        the frame can be decoded standing alone (the reference's
        `_is_config_keyframe` checks the same two things). AV1: a sequence
        header OBU, which is the only key frame signal the format has - the
        packetizer owns that rule because it already parses OBUs.
        """
        if not chunks:
            return False
        spec = self.video_codec.nal
        if spec is None:
            probe = getattr(self.video_packetizer, "is_keyframe", None)
            return bool(probe(chunks[0])) if probe is not None else False
        types = {(nalu[0] >> spec.shift) & spec.mask for nalu in chunks}
        return spec.parameter_set_types.issubset(types) and bool(
            spec.keyframe_types & types
        )

    def _cache_keyframe(self, chunks: "Sequence[bytes]") -> None:
        """Remember the most recent key frame, for PLI/FIR.

        The chunks are kept as they were sent, not joined into one buffer:
        for H26x they are start-code-stripped NAL units, and joining then
        splitting them on start codes loses every boundary and hands the
        packetizer one enormous "NAL". Storing the list keeps the shape the
        packetizer already understands, for every codec.

        They are ciphertext, so the epoch they were encrypted under is kept
        with them - see `resend_keyframe`.
        """
        try:
            if chunks:
                self.keyframe = list(chunks)
                self.keyframe_epoch = self._dave_epoch()
        except TypeError:
            return

    def _dave_epoch(self) -> "object | None":
        """The DAVE epoch media is encrypted under, or None if there is none.

        Before the MLS group exists `encrypt_frame` is a passthrough, so
        frames sent then are not DAVE-encrypted at all.
        """
        dave = getattr(self, "dave", None)
        session = dave.session_or_none if dave is not None else None
        return None if session is None else getattr(session, "epoch", None)

    def resend_keyframe(self) -> bool:
        """Replay the cached key frame, on request from a viewer.

        The reference keeps the last few key frames and replays them
        (samples/discord-native-voice client.py
        `_send_requested_video_keyframes`); one is enough here, since the
        point is to give the decoder something to start from.
        """
        payload = self.keyframe
        if not payload:
            return False
        # Only under the epoch it was encrypted with. A key frame cached
        # before a DAVE transition is ciphertext under the old key, and one
        # cached before the group existed is not encrypted at all - either
        # way the receiver cannot decrypt it, so replaying it produces a
        # frame it drops and another PLI. Dropping the cache instead lets the
        # encoder's next key frame refill it, a second away at most.
        if self.keyframe_epoch != self._dave_epoch():
            self.keyframe = None
            return False
        # Replayed exactly as it was sent, so the packetizer sees the shape it
        # expects; it is already DAVE-encrypted, which is what went on the
        # wire the first time.
        self.video_packetizer.send_frame(self, list(payload))
        return True

    def codecs(self) -> list[CodecAdvertisement]:
        """The codec list advertised in select_protocol.

        With no explicit codec we advertise every video codec we can both
        encode and packetize, at the reference client's priority, and follow
        whatever the server negotiates. Naming a codec restricts the list to
        it, which is how you pin H264.
        """
        video = (
            [self.requested_codec]
            if self.requested_codec is not None
            else list(VIDEO_CODECS.values())
        )
        return [
            {
                "name": self.audio_packetizer.codec,
                "type": "audio",
                "priority": 1000,
                "clockRate": 48000,
                "payload_type": OPUS_PAYLOAD_TYPE,
            },
            *[
                {
                    "name": codec.name,
                    "type": "video",
                    "priority": codec.priority,
                    "clockRate": 90000,
                    "payload_type": codec.payload_type,
                    "rtx_payload_type": codec.rtx_payload_type,
                    "encode": True,
                    "decode": True,
                }
                for codec in video
            ],
        ]

    # -- accounting --------------------------------------------------------

    def note_rtcp_feedback(
        self, kind: str, ssrc: int, requested: int = 0, answered: int = 0
    ):
        """Attribute a viewer's RTCP feedback to us or to another member.

        `requested` is how many sequence numbers a NACK named and `answered`
        how many we retransmitted, so the report can say "satisfied 3 of 5"
        instead of just counting reports.
        """
        if kind in ("NACK", "PLI", "FIR"):
            ours = ssrc in (self.ssrc, self.video_ssrc)
            if kind == "NACK" and requested:
                label = (
                    f"nack ours {answered}/{requested}"
                    if ours
                    else f"nack other-member ssrc={ssrc}"
                )
            else:
                label = f"{'ours' if ours else 'other'} {kind.lower()} ssrc={ssrc}"
        else:
            label = f"{kind} ssrc={ssrc}"
        self.feedback.note(label)

    def note_encoded_frame(self, media: str):
        """Called once per encoded frame handed to a packetizer."""
        self.stats.note_frame(media)

    def note_rtp_packet(self, media: str, size: int, ssrc: int = 0, seq: int = 0):
        """Called by the packetizers for every RTP packet they emit."""
        self.stats.note_packet(media, size)
        self.maybe_report()

    def maybe_report(self) -> None:
        """Log the periodic send summary and the feedback seen since."""
        if not self.report_window.due(REPORT_INTERVAL):
            return
        window = self.report_window.since()

        feedback = self.feedback.drain()
        if feedback:
            self.logger.info(
                "rtcp feedback: %s",
                "; ".join(f"{k} x{v}" for k, v in sorted(feedback.items())),
            )
        if self.rtx_cache.sent:
            self.logger.info(
                "rtx: %d video packets retransmitted (cache holds %d)",
                self.rtx_cache.sent,
                len(self.rtx_cache),
            )
            self.rtx_cache.sent = 0

        for name in ("video", "audio"):
            packets, total, frames = self.stats.since_last_report(name)
            if not packets:
                continue
            self.logger.info(
                "sent %-5s %5d packets (%3d/s), %5d frames (%2d/s), "
                "%9s bytes (%.0f kbps)",
                name,
                packets,
                packets // int(window),
                frames,
                frames // int(window),
                f"{total:,}",
                total * 8 / window / 1000,
            )
        self.report_window.restart()
        self.stats.take_snapshot()
