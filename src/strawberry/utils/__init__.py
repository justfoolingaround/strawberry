import typing

# ffmpeg input options: HTTP protocol options and the HLS demuxer option.
# These only take effect *before* -i.
FFMPEG_INPUT_ARGS = (
    # reconnect on transport errors and dropped connections. NOTE: no
    # -reconnect_at_eof: a live HLS playlist reaching its edge (EOF) is
    # normal, and reconnecting there makes ffmpeg spin at 0-second delays
    # instead of waiting for the next live segment.
    "-reconnect",
    "1",
    "-reconnect_streamed",
    "1",
    "-reconnect_on_network_error",
    "1",
    "-reconnect_on_http_error",
    "4xx,5xx",
    "-reconnect_delay_max",
    "10",
    "-reconnect_max_retries",
    "-1",
)


def find_associated_guild(
    channel_id: str, user_guild_states: list[dict[str, typing.Any]]
):
    """
    user_guild_state is .d.guilds[]
    """

    for guild in user_guild_states:
        for channel in guild.get("channels", []):
            if channel.get("id") == channel_id:
                return guild


def is_channel_private(
    channel_id: str, private_channels: list[dict[str, typing.Any]]
):
    """
    private_channel is .d.private_channels[]
    """

    for channel in private_channels:
        if channel.get("id") == channel_id:
            return True

    return False


def checked_add(integer: int, quantity: int, limit: int):
    return (integer + quantity) % limit


def partition_chunks(data: bytes, chunk_size: int):
    for i in range(0, len(data), chunk_size):
        yield data[i : i + chunk_size]
