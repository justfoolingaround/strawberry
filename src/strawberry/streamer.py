"""
Send-side streaming: encoded frames -> DAVE end-to-end encryption ->
RTP packetization -> transport encryption.

Video (H264) frames are DAVE-encrypted as one Annex B access unit, then
split back into NAL units for packetization. Audio (opus) packets are
DAVE-encrypted per packet; davey passes silence frames through untouched.
"""

import contextlib
import logging
import threading
import time
import typing

import davey

from .connection.stream_connection import ANNOUNCED_MAX_BITRATE
from .containers.annexb import split_annex_b as containers_split_annex_b
from .sources import Source

ANNEX_B_START_CODE = b"\x00\x00\x00\x01"
# How long to wait for senders to notice they were asked to stop.
THREAD_JOIN_SECONDS = 2.0
RESTART_DELAY = 3.0  # seconds before re-opening a dead source

# How far behind schedule a sender may drift before it stops trying to claw
# the time back and rejoins the clock instead.
#
# It has to be small. The offset it corrects is created once - a startup slip,
# or the source hitching - and then frozen, because the source is paced at the
# same rate as the sender and will not hand frames over any faster. So a
# sender that is behind stays exactly that far behind for the rest of the
# session, and a two second threshold means an offset of 1.9s never trips it
# and is carried forever.
#
# This is schedule bookkeeping rather than wire timing: the RTP clock advances
# one step per frame sent and the RTCP sender report maps that clock to the
# wall clock, so the receiver plays what arrives as it arrives whatever this
# offset says. What it does govern is how the sender paces *itself* - past
# this it sleeps the minimum and forwards whatever the source produces,
# including any burst from the source paying off its own debt.
LAG_GIVE_UP_SECONDS = 0.5
# Warn on the way there, once per episode. Below the give-up on purpose: the
# offset is corrected before it can reach a larger value, so a warning set
# above the give-up would never fire at all.
LAG_WARN_SECONDS = 0.25
# How close to on-schedule the sender must come before the episode is over and
# the warning is re-armed. It has to be well below the warning line, not equal
# to it: an offset that sits *at* the line crosses it on jitter alone, and with
# the boundary at the same value every crossing opened a new episode and the
# next frame closed it - a warning and an "on schedule" line every few frames,
# which is the spam this was meant to stop.
LAG_RECOVERED_SECONDS = 0.05

assert LAG_RECOVERED_SECONDS < LAG_WARN_SECONDS < LAG_GIVE_UP_SECONDS, (
    "the thresholds must nest, or a warning either never fires or never stops"
)
# How often a paused sender checks whether it has been asked to stop.
PAUSE_POLL_SECONDS = 0.1
# Floor on the pacing sleep. Enough to hand the GIL to the other sender and
# to the source threads; not a frame interval. See the note at the sleep.
MIN_SLEEP = 0.0005
# SVT-AV1's scale is inverted: higher is faster. 12 sustains 1080p realtime.
_SVT_PRESET = 12
# frames a source must deliver before its restart tally resets
_RECOVERED_AFTER = 30

logger = logging.getLogger("streamer")


def annex_b(nalus: list[bytes]) -> bytes:
    return b"".join(ANNEX_B_START_CODE + nalu for nalu in nalus)


def split_annex_b(frame: bytes) -> list[bytes]:
    return containers_split_annex_b(frame)


def send_video_access_unit(conn, nalus: "list[bytes]"):
    """H26x: DAVE transforms the whole access unit (Annex B)."""
    conn.note_encoded_frame("video")
    dave_frame = conn.dave.encrypt_frame(
        davey.MediaType.video, conn.video_codec.davey_codec, annex_b(nalus)
    )
    conn.send_video_frame(split_annex_b(dave_frame))


def send_video_frame(conn, frame: bytes):
    """VP8/VP9/AV1: one encoded frame in, one DAVE frame out.

    davey leaves the frame parseable for every codec we send: NAL unit
    start codes and headers for H26x, OBU headers and size fields for AV1
    (it only drops the temporal delimiter). So the packetizer sees a
    DAVE'd frame and reads its structure out of that, for all three.

    The frame is wrapped in a one element sequence because that is the
    shape every packetizer takes: NAL units for H26x, a single temporal
    unit here.
    """
    conn.note_encoded_frame("video")
    dave_frame = conn.dave.encrypt_frame(
        davey.MediaType.video, conn.video_codec.davey_codec, frame
    )
    conn.send_video_frame([dave_frame])


def send_audio_packet(conn, packet: bytes):
    conn.note_encoded_frame("audio")
    conn.send_audio_frame(
        conn.dave.encrypt_frame(davey.MediaType.audio, davey.Codec.opus, packet)
    )


class _RestartTally:
    """Logs source restarts, collapsing repeats.

    A source that cannot be opened logs on every attempt, every few
    seconds, forever. The interesting part is the first failure and then the
    summary ("failed 47 times over 4 minutes"), so repeats are counted and
    only periodically flushed - which is also what keeps a genuine problem
    from pushing everything else out of the log.
    """

    #: log the first N failures in full, then only every k-th
    VERBOSE_FAILURES = 3
    SUMMARY_EVERY = 20

    def __init__(
        self,
        log: logging.Logger,
        media: str,
    ):
        self.log = log
        self.media = media
        self.count = 0
        self.since = time.monotonic()

    def record(self, reason: str, *, exc_info: bool = False) -> None:
        self.count += 1
        if self.count <= self.VERBOSE_FAILURES:
            self.log.error(
                "%s: %s (attempt %d, retrying in %.0fs)",
                self.media,
                reason,
                self.count,
                RESTART_DELAY,
                exc_info=exc_info,
            )
            return
        if self.count % self.SUMMARY_EVERY:
            return
        self.log.error(
            "%s: still failing after %d attempts over %.0fs (last: %s)",
            self.media,
            self.count,
            time.monotonic() - self.since,
            reason,
        )

    def ok(self) -> None:
        """Reset the tally after the source has actually delivered."""
        if self.count:
            self.log.info(
                "%s source recovered after %d failures (%.0fs)",
                self.media,
                self.count,
                time.monotonic() - self.since,
            )
        self.count = 0
        self.since = time.monotonic()


class MediaClock:
    """One wall-clock origin shared by every sender on a connection.

    Without this each sender paces itself from the moment its own first
    frame arrived. The video encoder takes seconds to produce its first
    frame while audio starts almost immediately, so the two anchors end up
    seconds apart and every later frame inherits that offset: the audio and
    video of the same moment are sent seconds apart, permanently.

    Sharing one origin makes `origin + interval * index` mean the same
    instant for audio and video, which is what keeps them in step.
    """

    def __init__(self):
        self._origin: float | None = None
        self._lock = threading.Lock()

    @property
    def origin(self) -> float:
        # Fast path: once set, the origin never changes, and every sender reads
        # it once per packet. Only the first read needs the lock, and taking it
        # on the first is what makes the value the same for both senders.
        origin = self._origin
        if origin is not None:
            return origin
        with self._lock:
            if self._origin is None:
                self._origin = time.perf_counter()
            return self._origin

    def elapsed(self) -> float:
        return time.perf_counter() - self.origin

    def align(self, interval: float) -> int:
        """Frame index a source joining late should resume at.

        A source that starts `t` seconds in should send its next frame at the
        next multiple of its interval after now, not at `origin + interval`.
        Replaying the intervals it missed would dump a burst of stale frames
        to catch up.
        """
        if interval <= 0:
            return 0
        return int(self.elapsed() // interval)


class MediaGate:
    """Holds every sender until all of them have produced a first frame.

    `MediaClock` gives the two senders a shared pacing origin, but it cannot
    fix a frame that was already sent: each sender's *first* packet goes on
    the wire the moment its own source produces it. Audio starts almost
    immediately while a video encoder needs seconds to emit its first
    frame, so the early sender puts the first seconds of its content on the
    wire alone, and every later frame inherits that offset. The receiver
    then sees the two streams permanently apart by the startup difference.

    Waiting costs nothing - the early sender's frames would have been paced
    at the same wall-clock rate anyway - and makes both begin from their
    own source-time zero at the same instant.

    The barrier is deliberately breakable: a source that stalls before its
    first frame (network hiccup, encoder warming up) must not hang the
    whole connection, so it starts alone with a warning rather than never.
    """

    #: How long a sender waits for its peers before giving up and starting.
    #: A live source only needs a second or two to emit its first frame, so
    #: this only ever fires on a source that has stalled outright, where
    #: starting alone is better than holding the whole connection.
    TIMEOUT = 10.0

    def __init__(self, parties: int, timeout: float = TIMEOUT):
        self.parties = parties
        self._barrier = threading.Barrier(parties)
        self._timeout = timeout

    def wait(self, label: str) -> None:
        """Block until every sender has a first frame, or the timeout."""
        if self.parties <= 1:
            return
        try:
            self._barrier.wait(self._timeout)
        except threading.BrokenBarrierError:
            logger.warning(
                "%s started without the other %d sender(s) after %.0fs",
                label,
                self.parties - 1,
                self._timeout,
            )


class _LagReport:
    """The "behind schedule" warning, once per episode.

    A sender that has slipped cannot outrun its own source to catch up - both
    are paced at the same rate - so the offset persists and the same warning
    would repeat for every packet. Warn once, and say when it is over.
    """

    def __init__(self, log: logging.Logger, media: str) -> None:
        self._log = log
        self._media = media
        self._since: float | None = None
        self._worst = 0.0

    def note(self, delay: float) -> None:
        """One packet's schedule error: negative is behind."""
        if delay < 0 and -delay > LAG_WARN_SECONDS:
            if self._since is None:
                self._since = time.perf_counter()
                self._worst = -delay
                self._log.warning(
                    "%s is %.0f ms behind schedule; past %.0f ms the backlog "
                    "is dropped and the stream rejoins the clock (logged once "
                    "per episode)",
                    self._media,
                    -delay * 1000,
                    LAG_GIVE_UP_SECONDS * 1000,
                )
            elif -delay > self._worst:
                self._worst = -delay
        elif self._since is not None and -delay < LAG_RECOVERED_SECONDS:
            self._log.info(
                "%s back on schedule after %.1fs, worst %.0f ms behind",
                self._media,
                time.perf_counter() - self._since,
                self._worst * 1000,
            )
            self._since = None


def invoke_source_stream(
    source: Source,
    conn,
    media: str,
    send,
    in_between_delay: float,
    pause_event: "threading.Event | None" = None,
    clock: "MediaClock | None" = None,
    gate: "MediaGate | None" = None,
    stop: "threading.Event | None" = None,
) -> None:
    """Send one stream's packets until stopped.

    Restartable because `source` is: when its reader ends, `release` drops it
    and the next `reader` acquires a fresh one - a new URL, if the old one
    expired. Pacing is against a shared clock, so the streams stay together.
    """
    log = logger.getChild(media)
    clock = clock if clock is not None else MediaClock()
    restarts = _RestartTally(log, media)
    lag = _LagReport(log, media)

    while stop is None or not stop.is_set():
        try:
            current, reader = source.reader(media)
        except Exception:
            restarts.record("could not open the source", exc_info=True)
            time.sleep(RESTART_DELAY)
            continue

        # `loops` is the packet index on the shared timeline, and it is
        # anchored when the first packet is in hand rather than when the
        # source was opened: for anything remote those are seconds apart, and
        # anchoring early carries that gap forever.
        loops = 0
        paused_duration = 0.0
        delivered = 0
        since = time.perf_counter()

        try:
            for packet in reader.iter_packets():
                if stop is not None and stop.is_set():
                    return
                if delivered == 0 and gate is not None:
                    # hold the first frame until every sender has one, so the
                    # streams begin from their own zero at the same instant
                    gate.wait(media)
                if delivered == 0:
                    loops = clock.align(in_between_delay)
                loops += 1

                if pause_event is not None and pause_event.is_set():
                    paused = time.perf_counter()
                    # Polled rather than a bare wait: a stop that arrives during
                    # a pause would otherwise not be noticed until the pause
                    # lifted, and the thread would hang past the join.
                    while pause_event.is_set():
                        if stop is not None and stop.is_set():
                            return
                        pause_event.wait(timeout=PAUSE_POLL_SECONDS)
                    paused_duration += time.perf_counter() - paused

                send(conn, packet)
                delivered += 1
                if loops == 1:
                    log.info("first frame from %s", type(source).__name__)
                elif loops == 2:
                    log.info("sent first packet to the wire")
                if delivered >= _RECOVERED_AFTER:
                    restarts.ok()  # delivering again, so stop counting failures

                due = clock.origin + in_between_delay * loops - paused_duration
                delay = due - time.perf_counter()
                lag.note(delay)

                # Past the give-up point, stop trying to win the time back: a
                # live viewer wants the current frame at the right rate, not a
                # backlog replayed at them.
                if delay < -LAG_GIVE_UP_SECONDS:
                    log.info(
                        "%s dropped %.0f ms of backlog and rejoined the clock",
                        media,
                        -delay * 1000,
                    )
                    loops = clock.align(in_between_delay)
                    due = clock.origin + in_between_delay * loops
                    delay = due - time.perf_counter()
                # The floor is only enough to yield: sleeping the whole
                # interval adds the cost of sending to the schedule every
                # time round, and a sender that is behind never catches up.
                time.sleep(max(delay, MIN_SLEEP))

            restarts.record(
                f"source ended after {delivered} frames "
                f"({time.perf_counter() - since:.0f}s)"
            )
        except Exception:
            restarts.record(
                f"source failed after {delivered} frames "
                f"({time.perf_counter() - since:.0f}s)",
                exc_info=True,
            )
        finally:
            source.release(current)

        if stop is not None and stop.is_set():
            return
        time.sleep(RESTART_DELAY)


@contextlib.asynccontextmanager
async def stream_voice_audio(
    conn, source: Source, *, pause_event: "threading.Event | None" = None
):
    """Send `source`'s audio to the voice channel. No video ops."""
    await conn.wait_dave_ready()
    await conn.set_speaking(True)

    stop = threading.Event()
    clock = MediaClock()
    threads = [
        _spawn_sender(
            source,
            conn,
            "audio",
            send_audio_packet,
            20 / 1000,
            pause_event,
            clock,
            MediaGate(1),
            stop,
        )
    ]
    try:
        yield threads
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=THREAD_JOIN_SECONDS)


@contextlib.asynccontextmanager
async def stream(conn, source: Source, *, pause_event: "threading.Event | None" = None):
    """Send `source` on `conn` until the context is exited.

    Entering announces the stream and starts the senders; leaving stops them and
    joins them. The source is opened by the caller, so one that cannot be opened
    fails before any of this happens.
    """
    await conn.wait_dave_ready()

    stop = threading.Event()
    plan = source.plan()
    conn.video_packetizer.fps = plan.fps
    logger.info(
        "streaming video %sx%s @ %sfps to ssrc %s, %s bps encoding, %s announced"
        " (dave ready: %s)",
        plan.width,
        plan.height,
        plan.fps,
        conn.video_ssrc,
        f"{plan.bitrate:,}",
        f"{ANNOUNCED_MAX_BITRATE:,}",
        conn.dave.ready,
    )
    # announce the Go Live stream before any media goes out (speaking: 2)
    await conn.set_speaking(True)
    # What we announce is not what we encode: the reference hardcodes 10 Mbps
    # (BaseMediaConnection.ts:614) regardless, and announcing less than we send
    # gets the stream throttled by the server.
    await conn.set_video_state(
        True, plan.width, plan.height, plan.fps, bitrate=ANNOUNCED_MAX_BITRATE
    )
    conn.set_video_bitrate(plan.bitrate_max)
    conn.video_frame_interval = 1 / plan.fps

    logger.info("media source: PyAV (in-process)")

    # H26x frames itself access-unit by access-unit; VPx/AV1 as whole units.
    video_sender = (
        send_video_access_unit if conn.video_codec.nal is not None else send_video_frame
    )
    senders: list[tuple[str, typing.Any, float]] = [
        ("video", video_sender, 1 / plan.fps)
    ]
    if source.has_audio:
        senders.append(("audio", send_audio_packet, 20 / 1000))

    clock = MediaClock()
    # no sender begins until every one has a first frame
    gate = MediaGate(len(senders))
    threads = [
        _spawn_sender(source, conn, media, send, delay, pause_event, clock, gate, stop)
        for media, send, delay in senders
    ]
    try:
        yield threads
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=THREAD_JOIN_SECONDS)


def _spawn_sender(
    source, conn, media, send, delay, pause_event, clock, gate=None, stop=None
):
    thread = threading.Thread(
        target=invoke_source_stream,
        args=(source, conn, media, send, delay, pause_event, clock, gate, stop),
        daemon=True,
    )
    thread.start()
    return thread
