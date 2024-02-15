import logging
import threading
import time

from .connection import StreamConnection, UDPConnection, VoiceConnection
from .sources import AudioSource, VideoSource, try_probe_source


def invoke_source_stream(
    source,
    udp: UDPConnection,
    in_between_delay: float,
    pause_event: "threading.Event | None" = None,
):
    logger = logging.getLogger("streamer")

    if isinstance(source, AudioSource):
        sender = udp.send_audio_frame
        logger = logger.getChild("audio")
    else:
        sender = udp.send_video_frame
        logger = logger.getChild("video")

    paused_duration = 0
    start = None

    for loops, packet in enumerate(source.iter_packets(), 1):
        if start is None:
            start = time.perf_counter()

        if pause_event is not None and pause_event.is_set():
            paused_start = time.perf_counter()
            pause_event.wait()
            paused_duration += time.perf_counter() - paused_start

        sender(packet)

        delay = start + in_between_delay * loops - time.perf_counter() - paused_duration

        if delay < 0:
            behind_by = -delay * 1000
            if behind_by > 1000:
                logger.warning(
                    "Stream is lagging by %.2f ms, experiencing poor connection.",
                    behind_by,
                )

        time.sleep(max(0, delay))


def ffmpeg_fps_eval(fps: str) -> int:
    numerator, denominator = fps.split("/", 1)
    return int(numerator) / (int(denominator) or 1)


async def stream(
    conn: VoiceConnection,
    source: str,
    *,
    audio_source: "str | None" = None,
    forced_width: int = 0,
    forced_height: int = 0,
    pause_event: "threading.Event | None" = None,
):
    t: list[threading.Thread] = []

    probes = try_probe_source(source)

    if probes["video"]:
        max_video_res = max(
            probes["video"], key=lambda x: int(x["width"]) * int(x["height"])
        )

        width = forced_width or int(max_video_res["width"])
        height = forced_height or int(max_video_res["height"])

        fps = ffmpeg_fps_eval(max_video_res["avg_frame_rate"])
        duration = max_video_res["duration"]

        await conn.set_video_state(
            True,
            width,
            height,
            int(fps),
        )

        video_source = VideoSource(
            source,
            has_burned_in_subtitles=bool(probes["subtitle"]),
            width=width,
            height=height,
            duration=float(duration) if duration else None,
        )
        conn.udp_connection.video_packetizer.fps = fps

        video_thread = threading.Thread(
            target=invoke_source_stream,
            args=(video_source, conn.udp_connection, 1 / fps, pause_event),
        )
    else:
        await conn.set_video_state(False)

        if isinstance(source, StreamConnection):
            raise ValueError("StreamConnection requires a video source")

    t.append(video_thread)

    has_audio = audio_source or probes["audio"]

    if audio_source is not None or has_audio:
        if audio_source is not None:
            asrc = AudioSource(audio_source)
        else:
            asrc = AudioSource(source, duration=float(duration) if duration else None)

        audio_thread = threading.Thread(
            target=invoke_source_stream,
            args=(asrc, conn.udp_connection, 20 / 1000, pause_event),
        )
        t.append(audio_thread)

    for thread in t:
        thread.start()

    return t
