"""A source that yt-dlp resolves to direct URLs.

yt-dlp hands out URLs that 403 or have already expired, and the failure only
shows when something opens them - so a resolve is proved by opening it, and one
that will not open is retried with a *fresh* resolve, because the same URL never
starts working.
"""

import logging
import shutil
import subprocess
import time

from .pyav_source import PyAVSource
from .source import NoSuchStream, Opened, Source, SourceUnavailable

logger = logging.getLogger("sources.ytdlp")


class YouTubeDLPSource(Source):
    """A query, resolved to URLs and re-resolved on every acquisition.

    Re-resolving is the point: it is what lets a stream outlive the URL it
    started on. A plain URL cannot - once it expires there is nothing to fall
    back to. The query is kept because re-resolving needs it.
    """

    #: Resolve, prove it opens, and if it does not, resolve again.
    ATTEMPTS = 3
    RETRY_SECONDS = 3.0

    def __init__(self, query: str) -> None:
        self.query = query
        super().__init__()

    @staticmethod
    def invoke(query: str) -> "dict[str, str] | None":
        """Resolve `query` to direct media URLs: {video, audio?}.

        A staticmethod, not a classmethod: nothing here is constructed and
        `cls` would be unused.
        """
        if not shutil.which("yt-dlp"):
            return None

        proc = subprocess.run(
            [
                "yt-dlp",
                "-g",
                query,
                "--format",
                "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
            ],
            capture_output=True,
        )
        if proc.returncode != 0 and proc.stderr:
            raise SourceUnavailable(proc.stderr.decode("utf-8", "replace").strip())

        streams = proc.stdout.decode("utf-8").strip().splitlines()
        if not streams:
            return None

        media = {"video": streams[0]}
        if len(streams) > 1:
            media["audio"] = streams[1]
        return media

    def acquire(self) -> Opened:
        """Resolve and prove it opens, retrying with a fresh resolve."""
        last = "unknown"
        for attempt in range(1, self.ATTEMPTS + 1):
            try:
                return self._acquire_once()
            except NoSuchStream:
                raise  # nothing to retry: resolving produced no stream at all
            except SourceUnavailable as exc:
                last = str(exc)
                if attempt < self.ATTEMPTS:
                    logger.warning(
                        "yt-dlp gave a stream that will not open (%s); "
                        "resolving again in %.0fs (%d/%d)",
                        last,
                        self.RETRY_SECONDS,
                        attempt,
                        self.ATTEMPTS,
                    )
                    time.sleep(self.RETRY_SECONDS)

        raise SourceUnavailable(
            f"yt-dlp gave {self.ATTEMPTS} streams for {self.query!r} and none "
            f"would open - last error: {last}"
        )

    def _acquire_once(self) -> Opened:
        media = self.invoke(self.query)
        if not media:
            raise NoSuchStream(f"yt-dlp found no stream for {self.query!r}")
        video = media["video"]
        audio = media.get("audio")

        try:
            probe, container = PyAVSource.probe_open(video)
        except Exception as exc:
            raise SourceUnavailable(f"cannot open {video!r}: {exc}") from exc
        if not probe.video:
            container.close()
            raise NoSuchStream(f"{video!r} has no video stream")

        audio_container = None
        if audio and audio != video:
            try:
                _, audio_container = PyAVSource.probe_open(audio)
            except Exception as exc:
                container.close()
                raise SourceUnavailable(f"cannot open {audio!r}: {exc}") from exc

        return Opened(
            probe=probe,
            video=video,
            audio=audio,
            container=container,
            audio_container=audio_container,
        )
