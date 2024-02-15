import subprocess

from .h264_source import VideoSource
from .opus_source import AudioSource

__all__ = ["VideoSource", "AudioSource", "try_probe_source"]


def try_probe_source(source: str):
    ffprobe = subprocess.Popen(
        (
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=width,height,avg_frame_rate,duration,codec_type",
            "-of",
            "default=noprint_wrappers=1",
            source,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    stdout, stderr = ffprobe.communicate()

    if stderr:
        raise ValueError(stderr.decode("utf-8"))

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
