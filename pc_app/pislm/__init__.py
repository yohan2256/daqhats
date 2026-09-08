"""pislm — client library for the Raspberry Pi sound level / floor impact server.

A pure-Python implementation of PROTOCOL.md. The UI, the standards maths and
session storage all sit on top of this layer.

    from pislm import PiSLM

    with PiSLM("192.168.0.42") as pi:
        print(pi.config.num_channels, pi.config.sample_rate)
        pi.start()
"""

from .client import PiSLM
from .control import CommandError, CommandTimeout, ControlClient
from .frames import (
    LAYOUT_V3,
    LAYOUT_V4,
    NO_INDEX,
    BandFrame,
    BandInfo,
    BandLevelFrame,
    ChannelInfo,
    DataFrame,
    Handshake,
    LevelFrame,
    MsgFrame,
    RawDumpChunk,
    UnknownFrame,
    decode_frame,
    layout_of,
)
from .gaps import Gap, GapDetector, StreamTrack
from .framing import (
    FRAME_BAND,
    FRAME_BAND_LEVEL,
    FRAME_DATA,
    FRAME_LEVEL,
    FRAME_MSG,
    FRAME_NAMES,
    FRAME_RAW_DUMP,
    LineBuffer,
    PeerClosed,
    ProtocolError,
    read_frame,
    recv_exact,
)
from .stream import RawDump, StreamClient, StreamStats

#: Protocols this library can read. v4 carries a start_index in every frame.
SUPPORTED_PROTOCOLS = ("pislm/3", "pislm/4")

__version__ = "0.2.0"

__all__ = [
    "PiSLM",
    "GapDetector",
    "Gap",
    "StreamTrack",
    "layout_of",
    "LAYOUT_V3",
    "LAYOUT_V4",
    "NO_INDEX",
    "ControlClient",
    "StreamClient",
    "Handshake",
    "ChannelInfo",
    "BandInfo",
    "DataFrame",
    "BandFrame",
    "LevelFrame",
    "BandLevelFrame",
    "RawDumpChunk",
    "MsgFrame",
    "UnknownFrame",
    "RawDump",
    "StreamStats",
    "CommandError",
    "CommandTimeout",
    "PeerClosed",
    "ProtocolError",
    "decode_frame",
    "read_frame",
    "recv_exact",
    "LineBuffer",
    "FRAME_DATA",
    "FRAME_MSG",
    "FRAME_BAND",
    "FRAME_LEVEL",
    "FRAME_BAND_LEVEL",
    "FRAME_RAW_DUMP",
    "FRAME_NAMES",
    "SUPPORTED_PROTOCOLS",
]
