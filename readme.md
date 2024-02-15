<h1 align="center">Strawberry</h1>
<p align="center">A Discord video and audio streaming client designed for user-bots (aka self-bots.)</p>


<h3 align="center">You will most likely get banned for using this with your user token.</h3>

<h3>Requirements</h3>

- Python 3.11+
    - Mostly because the `match-case` syntax is lovely.
- `aiohttp`, `PyNaCl` (`libsodium`), `toml` (install from requirements.txt)
- `ffmpeg` and `ffprobe` in PATH.

<h3>Usage</h3>

```sh
# Configure the strawberry_config.toml first and then.
$ py strawberry_yum.py "?" "{file path or url to stream}"
# or just stream via yt-dlp
$ py strawberry_yum.py "yt-dlp" "{yt-dlp supported url to stream}"
```


<h3>What Strawberry can do?</h3>

- Auto-infer audio and video information from the source (through a layer of abstraction.)
    - If you try a video source, and it contains audio and subtitles, the client will try to embed both in the stream.
    - If only audio, and given that the connection is not a stream connection, the client will just stream the audio.
- Stream both audio and video to the said voice channel.
    - Streaming can be done at **any** video quality without any Nitro necessary. (Audio quality has not yet been tested.)
    - If a stream is not initiated, the video stream will open the user's video, not stream.
    - Pause can be achieved by using a `threading.Event` which will then hold the stream in place.
- Listen in on your conversations and watch your streams.
    - The max transmission unit (mtu) for Discord voice is `1200 Bytes`. This means that you can do the corresponding UDP read to get the packet.
        - Each of these packets have a header containing the `SSRC`, timestamp and the sequence that they belong to.
        - This means that this client may also be used to mirror your streams.
    - This hurts the connection so it is suggested to keep the user-bot server-deafened.
        - Your favorite Discord music bots also do this because it hurts the connection. They don't care about your privacy, it is a bug labelled as a feature.
- Be hosted, away from your home.
    - The source encoding and packetization code can be extracted and be hosted as one would host `Lavalink`.
        - After all, all you would need would be the UDP server's IP address and the secret key.
        - Make sure you identify with your VPS' IP address first and then send it the secret key.
    - This means that a single VPS can be used to effectively control a series of user-bots' streams.
