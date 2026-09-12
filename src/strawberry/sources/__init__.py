import subprocess
import typing

from .h264_source import VideoSource
from .opus_source import AudioSource

if typing.TYPE_CHECKING:
    pass

__all__ = [
    "VideoSource",
    "AudioSource",
    "try_probe_source",
    "create_av_sources_from_single_process",
]


def create_av_sources_from_single_process(
    source: str,
    width: int = 1280,
    height: int = 720,
    has_burned_in_subtitles: bool = False,
    *,
    framerate: "int | None" = None,
    crf: "int | None" = None,
    audio_source: "str | None" = None,
):
    subprocess_kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }

    args = ("ffmpeg", "-hide_banner", "-loglevel", "quiet", "-i", source)

    if crf is not None:
        args += ("-crf", str(crf))

    if framerate is not None:
        args += ("-r", str(framerate))

    vf = f"scale={width}:{height}"

    if has_burned_in_subtitles:
        escaped_source = source.replace(":", "\\:").replace("'", "\\'")

        vf += ",subtitles=" + f"'{escaped_source}'" + ":si=0"

    args += (
        "-f",
        "h264",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "5",
        "-vf",
        vf,
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "ultrafast",
        "-profile:v",
        "baseline",
        "-bsf:v",
        "h264_metadata=aud=insert",
        "pipe:1",
    )

    if audio_source is not None:
        args += (
            "-i",
            audio_source,
        )

    args += (
        "-map_metadata",
        "-1",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "5",
        "-f",
        "opus",
        "-c:a",
        "libopus",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-b:a",
        f"{AudioSource.bitrate}k",
        "pipe:2",
    )

    process = subprocess.Popen(args, **subprocess_kwargs)

    return VideoSource(process.stdout), AudioSource(process.stderr)


def try_probe_source(source: str):
    ffprobe = subprocess.Popen(
        (
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=width,height,avg_frame_rate,duration,codec_type,bit_rate",
            "-of",
            "default=noprint_wrappers=1",
            source,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    stdout, stderr = ffprobe.communicate()

    assert ffprobe.returncode == 0, stderr.decode("utf-8")
    stdout_text = stdout.decode("utf-8")

    video_probes = []
    audio_probes = []
    subtitle_probes = []
    attachment_probes = []

    curr_probe = None

    for line in stdout_text.splitlines():
        key, value = line.split("=")

        if key == "codec_type":
            curr_probe = {}

            match value:
                case "video":
                    video_probes.append(curr_probe)
                case "audio":
                    audio_probes.append(curr_probe)
                case "subtitle":
                    subtitle_probes.append(curr_probe)
                case "attachment":
                    attachment_probes.append(curr_probe)
                case _:
                    curr_probe = None
        else:
            if curr_probe is not None:
                curr_probe[key] = value if value != "N/A" else None

    return {
        "video": video_probes,
        "audio": audio_probes,
        "subtitle": subtitle_probes,
        "attachment": attachment_probes,
    }
