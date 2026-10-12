"""
Strawberry CLI.

Streams a source into a Discord voice channel (audio) or as a Go Live
stream (audio + video), end-to-end encrypted with DAVE over UDP.

    strawberry_yum.py "movie.mkv"
    strawberry_yum.py "ytsearch:lofi girl" --yt-dlp --stream
"""

import asyncio
import contextlib
import faulthandler
import logging
import shlex
from dataclasses import replace

import aiohttp
import click
import toml
import uvloop

from strawberry import utils
from strawberry.codecs import VIDEO_CODECS
from strawberry.codecs import video_codec as resolve_codec
from strawberry.config_model import Configuration
from strawberry.gateway import create_gateway_connection, fetch_optimal_voice_region
from strawberry.packetizers import BaseMediaPacketizer
from strawberry.sources import PlainSource, SourceUnavailable, YouTubeDLPSource
from strawberry.streamer import stream, stream_voice_audio

logger = logging.getLogger("strawberry")

# yt-dlp hands out URLs that 403 or have already expired, and the failure only
# shows up when something opens them. Resolve, prove it opens, and if it does
# not, resolve again - up to three times, three seconds apart. Discovering it
# later means the voice connection, the stream entry and the UDP socket are all
# built and then thrown away.
YTDLP_ATTEMPTS = 3
YTDLP_RETRY_SECONDS = 3.0


def split_ffmpeg_args(values) -> tuple[str, ...]:
    """Split each --ffmpeg-arg value into argv pieces.

    They have to be quoted on the command line (they start with a dash), so
    unpack them here rather than making callers repeat --ffmpeg-arg.
    """
    return tuple(piece for value in values for piece in shlex.split(value))


def load_config() -> Configuration:
    try:
        with open("strawberry_config.toml") as f:
            return Configuration.model_validate(toml.load(f))
    except FileNotFoundError:
        raise click.ClickException(
            "strawberry_config.toml not found (see strawberry_config.toml.example)"
        ) from None


async def infer_guild_id(gateway, channel_id: str, configured: "str | None"):
    """A missing guild id is looked up from the READY state."""
    if configured is not None:
        return configured

    ready_state = await gateway.fetch_ready_state()
    if utils.is_channel_private(channel_id, ready_state["private_channels"]):
        return None

    click.echo("No guild id given and the channel is not private; looking it up...")
    guild = utils.find_associated_guild(channel_id, ready_state["guilds"])
    if guild is None:
        click.echo("Could not find the guild; make sure it is accessible by you.")
        return None

    click.echo(f"Found guild: {guild['name']}@{guild['id']} (set it in the config)")
    return guild["id"]



async def run(
    source: str,
    *,
    mode: str,
    encoder: str,
    use_ytdlp: bool,
    guild_id: "str | None",
    region: "str | None",
    preview: bool,
    codec: str,
    profile: "str | None",
    video_extension: bool,
    ffmpeg_args: "tuple[str, ...]",
    dump_rtp: bool = False,
    rtx: bool = False,
):
    config = load_config()
    try:
        # acquired here, so a source that cannot be read fails before any of
        # the Discord work starts
        src = YouTubeDLPSource(source) if use_ytdlp else PlainSource(source)
    except SourceUnavailable as exc:
        raise click.ClickException(str(exc)) from exc

    session = aiohttp.ClientSession()
    try:
        async with create_gateway_connection(session, config.user.token) as gateway:
            channel_id = config.voice.channel_id
            guild_id = await infer_guild_id(gateway, channel_id, guild_id)
            region = (
                region
                or config.voice.preferred_region
                or await fetch_optimal_voice_region(
                    session, gateway.DISCORD_API_ENDPOINT, config.user.token
                )
            )
            click.echo(
                f"channel: {channel_id}  region: {region or 'auto'}  mode: {mode}"
            )

            conn = await gateway.join_voice_channel(
                session, channel_id, guild_id, region
            )

            async with contextlib.AsyncExitStack() as stack:
                if mode == "stream":
                    # returns a connection that is already transport-ready,
                    # so the stack only has to guarantee it gets closed
                    stream_conn = await gateway.create_stream(
                        session,
                        conn,
                        preferred_region=region,
                        video_codec=codec,
                        video_extension=video_extension,
                    )
                    stack.push_async_callback(stream_conn.close)
                    stream_conn.dump_rtp = dump_rtp
                    stream_conn.rtx_enabled = rtx
                    src.configure(
                        stream_conn.video_codec,
                        profile=profile,
                        extra_args=ffmpeg_args,
                    )
                    async with stream(stream_conn, src) as threads:
                        if preview:
                            await stream_conn.set_preview(
                                session,
                                gateway.DISCORD_API_ENDPOINT,
                                config.user.token,
                                open("assets/strawberry_preview.png", "rb").read(),
                                "image/png",
                            )
                        logger.info(
                            "streaming with %s sender thread(s)", len(threads)
                        )
                        # never block the loop on the sender threads: media
                        # packets are queued onto it via call_soon_threadsafe
                        await gateway.wait()
                else:
                    if mode == "camera":
                        # camera video rides the voice channel's own media
                        # session, so ask for it before connecting: the
                        # server then hands us the video ssrc in READY
                        await gateway.set_video_enabled(conn, True)

                    voice_conn = await gateway.create_voice_media(
                        session,
                        conn,
                        video_codec=codec,
                        camera=mode == "camera",
                    )
                    voice_conn.rtx_enabled = rtx
                    stack.push_async_callback(voice_conn.close)

                    src.configure(voice_conn.video_codec)
                    if mode == "camera":
                        try:
                            async with stream(voice_conn, src) as threads:
                                logger.info(
                                    "streaming with %s sender thread(s)", len(threads)
                                )
                                await gateway.wait()
                        finally:
                            await gateway.set_video_enabled(conn, False)
                    else:
                        async with stream_voice_audio(voice_conn, src) as threads:
                            logger.info(
                                "streaming with %s sender thread(s)", len(threads)
                            )
                            await gateway.wait()
    finally:
        await session.close()


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("source", type=str)
@click.option(
    "--stream",
    "mode",
    flag_value="stream",
    help="Go Live stream: a separate media session (default).",
)
@click.option(
    "--camera",
    "mode",
    flag_value="camera",
    help="Publish SOURCE as video in the voice channel, as if your camera were on.",
)
@click.option(
    "--audio-only",
    "mode",
    flag_value="audio",
    help="Audio only in the voice channel.",
)
@click.option(
    "--yt-dlp",
    "use_ytdlp",
    is_flag=True,
    help="Resolve SOURCE with yt-dlp (accepts ytsearch:... queries).",
)
@click.option(
    "--rtx/--no-rtx",
    default=False,
    show_default=True,
    help="Retransmit lost video packets when the far end NACKs them. Off by "
    "default: it is opportunistic, and it was the first suspect for a crash "
    "under load.",
)
@click.option("--guild-id", default=None, help="Override [voice].guild_id.")
@click.option(
    "--encoder",
    type=click.Choice(["software", "nvenc"], case_sensitive=False),
    default="software",
    show_default=True,
    help="Video encoder: software (libx264/libx265/libvpx) or NVIDIA NVENC.",
)
@click.option(
    "--profile",
    type=click.Choice(["high", "balanced", "fast"], case_sensitive=False),
    default="balanced",
    show_default=True,
    help="Speed/quality ladder. high = best picture, slowest (may not hold "
    "1080p60); balanced = the default; fast = fewer cycles, lower quality.",
)
@click.option(
    "--region",
    default=None,
    help="Override [voice].preferred_region (default: the optimal one).",
)
@click.option(
    "--codec",
    type=click.Choice(["auto", *sorted(VIDEO_CODECS)], case_sensitive=False),
    default="H264",
    show_default=True,
    help="Video codec. H264 is the only one Go Live streams accept over "
    "UDP; auto advertises every codec and follows the negotiation.",
)
@click.option(
    "--video-ext/--no-video-ext",
    "video_extension",  # click would otherwise call it video_ext
    default=True,
    help="Send the RTP header extension with video packets.",
)
@click.option(
    "--ffmpeg-arg",
    "ffmpeg_args",
    multiple=True,
    help="Extra ffmpeg encoder argument, repeatable. Quote each one, since "
    'they start with a dash: --ffmpeg-arg "-profile:v 2".',
)
@click.option(
    "--preview/--no-preview",
    default=True,
    help="Upload the stream preview image (Go Live only).",
)
@click.option(
    "--dump-rtp",
    is_flag=True,
    help="Print every RTP packet (pt, ssrc, seq, ts, marker, payload type).",
)
@click.option(
    "--pt",
    "payload_types",
    multiple=True,
    help="Override a video payload type, e.g. --pt av1=96. Diagnostic: the "
    "reference table (av1=109, h265=103) is known not to match Discord's own "
    "numbering, which puts H264 on 103.",
)
@click.option(
    "--no-rich-rtp-extension",
    is_flag=True,
    help="Send only the playout delay RTP header extension element instead of "
    "the full block (transmission offset, abs-send-time, video orientation, "
    "transport-cc, playout delay) the reference client sends.",
)
@click.option("-v", "--verbose", count=True, help="Use twice for debug logging.")
def main(
    source,
    mode,
    encoder,
    profile,
    use_ytdlp,
    guild_id,
    region,
    codec,
    video_extension,
    ffmpeg_args,
    preview,
    dump_rtp,
    rtx,
    payload_types,
    no_rich_rtp_extension,
    verbose,
):
    """Stream SOURCE into Discord, encrypted with DAVE."""
    # A segfault is in a C extension, and the traceback it leaves is otherwise
    # just "Segmentation fault". This prints the Python frames on the way down,
    # which is what names the library and the call.
    faulthandler.enable()
    logging.basicConfig(
        level=logging.WARNING if verbose > 1 else logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )
    BaseMediaPacketizer.rich_extension = not no_rich_rtp_extension

    for override in payload_types:
        name, _, value = override.partition("=")
        try:
            entry = resolve_codec(name)  # not `codec`: that is the --codec option
            VIDEO_CODECS[entry.name] = replace(entry, payload_type=int(value))
        except (ValueError, KeyError) as exc:
            raise click.BadParameter(f"--pt {override!r}: {exc}") from None

    # uvloop, not stdlib asyncio: 1.29x on the loop-bound work (measured 5.39
    # -> 4.19 us per timer+sendto). A loop_factory rather than uvloop.install()
    # so importing this module does not repoint anyone else's event loop.
    asyncio.run(
        run(
            source,
            mode=mode or "stream",
            encoder=encoder.lower(),
            profile=profile.lower(),
            use_ytdlp=use_ytdlp,
            guild_id=guild_id,
            region=region,
            preview=preview,
            codec=codec.upper(),
            video_extension=video_extension,
            ffmpeg_args=split_ffmpeg_args(ffmpeg_args),
            dump_rtp=dump_rtp,
            rtx=rtx,
        ),
        loop_factory=uvloop.new_event_loop,
    )


if __name__ == "__main__":
    main()
