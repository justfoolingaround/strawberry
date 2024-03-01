import logging
import threading
import time

from .connection import StreamConnection, UDPConnection, VoiceConnection
from .sources import (
    AudioSource,
    VideoSource,
    create_av_sources_from_single_process,
    try_probe_source,
)


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


def ffmpeg_fps_eval(fps: str):
    numerator, denominator = map(int, fps.split("/", 1))

    if denominator == 0:
        return None

    return numerator / denominator


async def stream(
    conn: VoiceConnection,
    source: str,
    *,
    audio_source: "str | None" = None,
    forced_width: int = 0,
    forced_height: int = 0,
    pause_event: "threading.Event | None" = None,
):
    probes = try_probe_source(source)

    has_audio_in_source = probes["audio"]
    has_audio = audio_source or has_audio_in_source

    sources = []
    is_udp_source = source[:6] == "udp://"

    if probes["video"]:
        max_video_res = max(
            probes["video"], key=lambda x: int(x["width"]) * int(x["height"])
        )

        width = forced_width or int(max_video_res["width"])
        height = forced_height or int(max_video_res["height"])

        fps = round(ffmpeg_fps_eval(max_video_res["avg_frame_rate"]) or 30)

        await conn.set_video_state(
            True,
            width,
            height,
            fps,
        )
        conn.udp_connection.video_packetizer.fps = fps

        if has_audio_in_source and is_udp_source:
            # You can only open 1 udp server at a time.
            # For some reason the stderr source has serious
            # latency (>1s).
            video, audio = create_av_sources_from_single_process(
                source,
                has_burned_in_subtitles=bool(probes["subtitle"]),
                width=width,
                height=height,
                audio_source=audio_source,
                framerate=fps,
            )

            sources.extend(
                (
                    (
                        video,
                        1 / fps,
                    ),
                    (
                        audio,
                        20 / 1000,
                    ),
                )
            )
        else:
            sources.append(
                (
                    VideoSource.from_source(
                        source,
                        has_burned_in_subtitles=bool(probes["subtitle"]),
                        width=width,
                        height=height,
                        framerate=int(fps),
                    ),
                    1 / fps,
                )
            )

    else:
        await conn.set_video_state(False)

        if isinstance(source, StreamConnection):
            raise ValueError("StreamConnection requires a video source")

    if not (has_audio_in_source and is_udp_source) and (
        audio_source is not None or has_audio
    ):
        if audio_source is not None:
            asrc = AudioSource.from_source(audio_source)
        else:
            asrc = AudioSource.from_source(source)

        sources.append(
            (
                asrc,
                1 / 50,
            )
        )

    threads = [
        threading.Thread(
            target=invoke_source_stream,
            args=(
                src,
                conn.udp_connection,
                delay,
                pause_event,
            ),
        )
        for src, delay in sources
    ]

    for thread in threads:
        thread.start()

    return threads
