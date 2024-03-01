import asyncio
import shutil
import subprocess
import sys

import toml

from strawberry.gateway import DiscordGateway
from strawberry.streamer import stream

with open("strawberry_config.toml") as f:
    config = toml.load(f)

with open("assets/strawberry_preview.png", "rb") as f:
    thumbnail = f.read()


def invoke_ytdlp(query: str):
    if not shutil.which("yt-dlp"):
        return None

    proc = subprocess.Popen(
        [
            "yt-dlp",
            "-g",
            query,
            "--format",
            "bestvideo[height<=720]+bestaudio/best[height<=720]",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = proc.communicate()

    if proc.returncode != 0 and stderr:
        raise ValueError(stderr.decode("utf-8"))

    streams = stdout.decode("utf-8").strip().split("\n")
    if not streams:
        return None

    mapped = {"video": streams[0]}

    if len(streams) > 1:
        mapped["audio"] = streams[1]

    return mapped


async def main():
    args_copy = sys.argv.copy()

    stream_with_ytdlp = "--yt-dlp" in args_copy

    if stream_with_ytdlp:
        args_copy.remove("--yt-dlp")

    stream_what = sys.argv[1]

    guild_id, channel_id, region = (
        config["voice"].get("guild_id"),
        config["voice"]["channel_id"],
        config["voice"]["preferred_region"],
    )

    gateway_ws = DiscordGateway(
        config["user"]["token"],
    )

    if stream_with_ytdlp:
        media = invoke_ytdlp(stream_what)

        if media is None:
            kwargs = {
                "source": stream_what,
            }
        else:
            if "audio" in media:
                kwargs = {
                    "source": media["video"],
                    "audio_source": media["audio"],
                }
            else:
                kwargs = {
                    "source": media["video"],
                }
    else:
        kwargs = {
            "source": stream_what,
        }

    await gateway_ws.ws_connect()
    conn = await gateway_ws.join_voice_channel(channel_id, guild_id, region or None)
    stream_conn = await gateway_ws.create_stream(conn)
    threads = await stream(stream_conn, **kwargs)
    # # Do something with the threads
    await stream_conn.set_preview(gateway_ws, thumbnail, "image/png")
    await gateway_ws.wait()


asyncio.run(main())
