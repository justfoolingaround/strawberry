"""Media sources.

`PyAVSource` opens a container, decodes and encodes it in process, and hands
out independent video and audio readers. `Source` is the abstraction over
"what to stream": something to open, and a way to open it again.
"""

from .pyav_source import (
    Probe,
    ProbedAudio,
    ProbedVideo,
    PyAVSource,
    available,
)
from .source import (
    NoSuchStream,
    Opened,
    PlainSource,
    Source,
    SourceUnavailable,
    VideoPlan,
)
from .ytdlp import YouTubeDLPSource

__all__ = [
    "NoSuchStream",
    "Opened",
    "PlainSource",
    "Probe",
    "ProbedAudio",
    "ProbedVideo",
    "PyAVSource",
    "Source",
    "SourceUnavailable",
    "VideoPlan",
    "YouTubeDLPSource",
    "available",
]
