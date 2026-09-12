import asyncio
import logging
import shutil
import subprocess
import sys

import aiohttp
import toml

from strawberry.config_model import Configuration
from strawberry.gateway import DiscordGateway, create_gateway_connection
from strawberry.streamer import stream

logging.basicConfig(level=0)

with open("strawberry_config.toml") as f:
    config = Configuration.model_validate(toml.load(f))

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

    session = aiohttp.ClientSession()
    async with create_gateway_connection(session, config.user.token) as gateway_ws:
        audio_src, video_src = None, stream_what

        if stream_with_ytdlp:
            media = invoke_ytdlp(stream_what)
            if media is not None:
                video_src = media["video"]
                if "audio" in media:
                    audio_src = media["audio"]

        conn = await gateway_ws.join_voice_channel(
            session,
            config.voice.channel_id,
            config.voice.guild_id,
            config.voice.preferred_region,
        )

        stream_conn = await gateway_ws.create_stream(conn)
        _threads = await stream(stream_conn, source=video_src, audio_source=audio_src)
        await stream_conn.set_preview(gateway_ws, thumbnail, "image/png")
        await gateway_ws.wait()


asyncio.run(main())
