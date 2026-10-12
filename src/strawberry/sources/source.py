"""What to stream, and how to open it again.

A live URL expires, so a source is really a *way* to open one. The class exists
for that: constructing it acquires a source, and `respawn` acquires another
using the same protocol when the first one dies.

Acquiring is a subclass's business - a plain URL reopens itself, a yt-dlp query
resolves again - so the base class only owns what they have in common: the
probe, the open containers, and the plan that comes from them.
"""

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import Any

from strawberry.codecs import VideoCodec

from .pyav_source import Probe, PyAVSource


class SourceUnavailable(RuntimeError):
    """The source cannot be opened."""


class NoSuchStream(SourceUnavailable):
    """Resolving produced nothing. Retrying will not help."""


@dataclass(frozen=True)
class VideoPlan:
    """The numbers every part of the video path has to agree on.

    The same rate is the encoder's frame rate, the RTP timestamp step, the
    sender's interval and the announced stream state; deciding them in one
    place is what keeps them from disagreeing.
    """

    width: int
    height: int
    fps: int
    bitrate: int
    bitrate_max: int

    @classmethod
    def from_probe(
        cls, probe: Probe, *, width: int = 0, height: int = 0
    ) -> "VideoPlan":
        """Choose what to encode, from what the source is."""
        if not probe.video:
            raise NoSuchStream("the source has no video stream")

        largest = max(probe.video, key=lambda v: v.width * v.height)
        chosen_width = width or largest.width
        chosen_height = height or largest.height

        # The source's own rate, uncapped. It was clamped to [15, 30] because
        # Go Live was thought to negotiate at most 30, which overrode the
        # source and made every rate-derived number wrong.
        fps = max(1, round(largest.fps))

        # The reference's tier: 8 Mbps at 720p or above, or at 31+ fps
        # (samples/discord-native-voice transcoding.py _native_video_bitrate).
        # The send pacer derives its per-packet interval from this.
        bitrate = 8_000_000 if chosen_height >= 721 or fps >= 31 else 4_000_000
        return cls(
            width=chosen_width,
            height=chosen_height,
            fps=fps,
            bitrate=bitrate,
            bitrate_max=int(bitrate * 1.4),
        )


@dataclass(frozen=True)
class Opened:
    """One acquisition: what the source is, where from, and the handles.

    The containers are left open. Opening a remote source costs a request and
    can be refused, so whoever opened it to check it works keeps it rather
    than opening the same URL again to use it.
    """

    probe: Probe
    video: str
    audio: "str | None"
    container: Any = None
    audio_container: Any = None


class Source(ABC):
    """A media source. Constructing one has acquired it.

    `configure` is separate because what to encode as is only known after the
    server names a codec, which is long after the source has been opened.
    """

    def __init__(self) -> None:
        self.opened = self.acquire()
        self._current: PyAVSource | None = None
        # Both senders ask for a reader from their own thread, and building one
        # opens a container and starts an encoder. Without this they can both
        # find nothing in place and build their own - two pipelines for one
        # stream, one of them leaked and still decoding.
        self._lock = threading.Lock()
        self._codec: VideoCodec | None = None
        self._profile: str | None = None
        self._extra_args: tuple[str, ...] = ()

    @abstractmethod
    def acquire(self) -> Opened:
        """Open the source, or raise `SourceUnavailable`.

        Called on construction and again whenever a fresh one is needed, so a
        backend whose URLs expire resolves new ones here.
        """

    @property
    def probe(self) -> Probe:
        return self.opened.probe

    @property
    def has_audio(self) -> bool:
        """Whether there is audio to send: here, or as a separate URL."""
        return bool(self.probe.audio or self.opened.audio)

    def plan(self) -> VideoPlan:
        return VideoPlan.from_probe(self.probe)

    def configure(
        self,
        codec: VideoCodec,
        *,
        profile: "str | None" = None,
        extra_args: "tuple[str, ...]" = (),
    ) -> "Source":
        self._codec = codec
        self._profile = profile
        self._extra_args = extra_args
        return self

    def reader(self, media: str) -> "tuple[PyAVSource, Any]":
        """The source in use, and the reader for `media`.

        Both senders call this, and both get the *same* `PyAVSource` - a live
        source can be single-connection, and opening it twice starves the
        second read.
        """
        if self._codec is None:
            raise RuntimeError("configure the source before opening it")
        with self._lock:
            if self.opened.container is None:
                self.opened = self.acquire()
            if self._current is None:
                self._current = self._build(self.opened)
            current = self._current
        return current, getattr(current, media)

    def release(self, source: PyAVSource) -> None:
        """Drop `source` if it is still the one in use, so the next reader
        acquires afresh.

        Guarded because both senders release: the first to notice the source
        ended must not have its replacement dropped by the second.
        """
        with self._lock:
            if self._current is source:
                self._current = None
                self.opened = replace(
                    self.opened, container=None, audio_container=None
                )

    def _build(self, opened: Opened) -> PyAVSource:
        codec = self._codec
        if codec is None:
            raise RuntimeError("configure the source before opening it")
        plan = self.plan()
        return PyAVSource(
            opened.video,
            codec=codec,
            width=plan.width,
            height=plan.height,
            framerate=plan.fps,
            bitrate=plan.bitrate,
            bitrate_max=plan.bitrate_max,
            profile=self._profile,
            container=opened.container,
            audio_container=opened.audio_container,
            audio_source=opened.audio,
            video_extra_args=self._extra_args,
        )


class PlainSource(Source):
    """A path or URL. Kept because reopening it needs it."""

    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__()

    def acquire(self) -> Opened:
        try:
            probe, container = PyAVSource.probe_open(self.url)
        except Exception as exc:
            raise SourceUnavailable(f"cannot open {self.url!r}: {exc}") from exc

        if not probe.video:
            container.close()
            raise NoSuchStream(f"{self.url!r} has no video stream")
        return Opened(probe=probe, video=self.url, audio=None, container=container)
