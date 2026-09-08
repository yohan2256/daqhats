"""Low-level wire framing — PROTOCOL.md §2, §9.1, §9.2.

This module only turns socket bytes into frames and JSON lines. Threading,
reconnection and command semantics live elsewhere.
"""

from __future__ import annotations

import json
import socket
import struct
from typing import Iterator

# ── Frame types (§2) ────────────────────────────────────────────────
FRAME_DATA = 0x01
FRAME_MSG = 0x02
FRAME_BAND = 0x03
FRAME_LEVEL = 0x04
FRAME_BAND_LEVEL = 0x05
FRAME_RAW_DUMP = 0x06

FRAME_NAMES = {
    FRAME_DATA: "DATA",
    FRAME_MSG: "MSG",
    FRAME_BAND: "BAND",
    FRAME_LEVEL: "LEVEL",
    FRAME_BAND_LEVEL: "BAND_LEVEL",
    FRAME_RAW_DUMP: "RAW_DUMP",
}

HEADER = struct.Struct("<BI")  # type uint8, length uint32 LE
HEADER_SIZE = HEADER.size  # 5
U32 = struct.Struct("<I")


class ProtocolError(Exception):
    """Something syntactically wrong arrived on the wire."""


class PeerClosed(ConnectionError):
    """recv() returned zero bytes — the only reliable disconnect signal (§9.3)."""


# ── §9.1 read exactly N bytes ───────────────────────────────────────
def recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes from the socket.

    TCP is a byte stream: one recv() is not guaranteed to return what you asked
    for. n == 0 returns empty bytes (a zero-length payload is legitimate).
    """
    if n == 0:
        return b""
    chunks: list[bytes] = []
    got = 0
    while got < n:
        chunk = sock.recv(n - got)
        if not chunk:
            raise PeerClosed(f"peer closed after {got}/{n} bytes")
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks) if len(chunks) > 1 else chunks[0]


def read_frame(sock: socket.socket, max_payload: int = 256 << 20) -> tuple[int, bytes]:
    """Read one frame from the stream port and return (type, payload).

    Even for a frame type you ignore, the header must be parsed — it is how you
    find the start of the next frame (§9.7). This function always consumes the
    """
    head = recv_exact(sock, HEADER_SIZE)
    ftype, length = HEADER.unpack(head)
    if length > max_payload:
        raise ProtocolError(
            f"frame type 0x{ftype:02x} declares {length} bytes "
            f"(> max_payload {max_payload}); stream is probably desynced"
        )
    return ftype, recv_exact(sock, length)


# ── §9.2 newline-delimited JSON line buffer ─────────────────────────
class LineBuffer:
    """For the control port. Push bytes in, get complete JSON objects out.

    One recv() may contain zero, one, several or a truncated line, so whatever
    is left over is kept here for the next read.
    """

    __slots__ = ("_buf", "_max")

    def __init__(self, max_line: int = 64 << 20) -> None:
        self._buf = bytearray()
        self._max = max_line

    def feed(self, data: bytes) -> Iterator[dict]:
        self._buf.extend(data)
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                if len(self._buf) > self._max:
                    raise ProtocolError(
                        f"no newline within {self._max} bytes; control stream desynced"
                    )
                return
            line = bytes(self._buf[:idx])
            del self._buf[: idx + 1]
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProtocolError(f"bad JSON line: {exc}") from exc
            if not isinstance(obj, dict):
                raise ProtocolError(f"expected JSON object, got {type(obj).__name__}")
            yield obj

    @property
    def pending(self) -> int:
        return len(self._buf)


def encode_command(obj: dict) -> bytes:
    """Encode one command as a newline-terminated UTF-8 JSON line (§1)."""
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
