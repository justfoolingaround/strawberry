import asyncio
import sys

import toml

from strawberry.gateway import DiscordGateway
from strawberry.streamer import stream

with open("strawberry_config.toml") as f:
    config = toml.load(f)


async def main():
    stream_what = sys.argv[1]

    guild_id, channel_id, region = (
        config["voice"]["guild_id"],
        config["voice"]["channel_id"],
        config["voice"]["preferred_region"],
    )

    gateway_ws = DiscordGateway(
        config["user"]["token"],
    )

    await gateway_ws.ws_connect()
    conn = await gateway_ws.join_voice_channel(guild_id, channel_id, region or None)

    stream_conn = await gateway_ws.create_stream(conn)
    threads = await stream(
        stream_conn,
        stream_what,
    )
    # Do something with the threads
    await gateway_ws.wait()


asyncio.run(main())
