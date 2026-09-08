"""Stream port client (default 5001) — PROTOCOL.md §2, §9.1, §9.7.

This port is receive-only; bytes sent to it are ignored.
"""

from __future__ import annotations

import logging
import queue
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from .frames import (
    LAYOUT_V3,
    BandFrame,
    BandLevelFrame,
    DataFrame,
    LevelFrame,
    MsgFrame,
    RawDumpChunk,
    StreamFrame,
    decode_frame,
    layout_of,
)
from .framing import PeerClosed, read_frame

log = logging.getLogger("pislm.stream")

FrameHandler = Callable[[StreamFrame], None]


# ── RAW_DUMP reassembly (§2.5) ──────────────────────────────────────
@dataclass(slots=True)
class RawDump:
    """A completed get_raw dump for one device."""

    dump_id: int
    device: int
    samples: np.ndarray  # (samples_per_channel, num_channels)
    channels: list[int] = field(default_factory=list)
    sample_rate: float = 0.0
    units: str = ""
    #: Index of the first sample, same grid as DATA (§2.5), for lining up live.
    start_index: int = -1

    @property
    def seconds(self) -> float:
        return self.samples.shape[0] / self.sample_rate if self.sample_rate else 0.0

    @property
    def end_index(self) -> int:
        return self.start_index + self.samples.shape[0] if self.start_index >= 0 else -1

    def channel(self, global_channel: int) -> np.ndarray:
        return self.samples[:, self.channels.index(global_channel)]

    def offset_of(self, data_start_index: int) -> int:
        """Where an index seen on the live DATA stream sits inside this dump (§2.5).

        Raises ValueError when out of range. Use it to locate an event in a dump.
        """
        if self.start_index < 0:
            raise ValueError("this dump has no start_index (pislm/3 server)")
        offset = data_start_index - self.start_index
        if not 0 <= offset < self.samples.shape[0]:
            raise ValueError(
                f"index {data_start_index} is outside the dump range "
                f"[{self.start_index}, {self.end_index})"
            )
        return offset


class _DumpAssembler:
    """Collects chunks per (dump_id, device).

    Chunk boundaries do not line up with frame boundaries (§2.5), so everything
    is concatenated first and reshaped afterwards. Channel count and sample rate
    only arrive in the get_raw response, so nothing is reshaped before that.

    **Race warning (§9.13):** the control response and the stream frames travel
    on different connections, so their order is not guaranteed. Chunks can
    complete a dump before the response lands; such dumps are held and released
    when `register()` is called. Without this the channel count is unknown and
    the data reshapes to a single channel.
    """

    #: How many recent dumps keep their metadata. A dump completes per device, so
    #: discarding metadata after the first device leaves the rest unable to reshape.
    KEEP_DUMPS = 8

    def __init__(self) -> None:
        self._chunks: dict[tuple[int, int], dict[int, np.ndarray]] = {}
        self._last: dict[tuple[int, int], int] = {}
        self._meta: dict[int, dict] = {}
        self._order: list[int] = []
        #: Completed dumps waiting for metadata: dump_id -> [(device, flat), ...]
        self._orphans: dict[int, list[tuple[int, np.ndarray]]] = {}
        self._lock = threading.Lock()

    # ── Registration ──
    def register(self, dump_id: int, response: dict) -> list[RawDump]:
        """Register the decode metadata from a get_raw response (§4).

        Any dumps that arrived before the response are completed and returned.
        """
        with self._lock:
            self._meta[dump_id] = response
            if dump_id in self._order:
                self._order.remove(dump_id)
            self._order.append(dump_id)
            while len(self._order) > self.KEEP_DUMPS:
                stale = self._order.pop(0)
                self._meta.pop(stale, None)
                self._orphans.pop(stale, None)
                for key in [k for k in self._chunks if k[0] == stale]:
                    self._chunks.pop(key, None)
                    self._last.pop(key, None)
            pending = self._orphans.pop(dump_id, [])

        if pending:
            log.debug("dump %d: %d dump(s) arrived before the response (§9.13)", dump_id, len(pending))
        return [self._build(dump_id, device, flat, response) for device, flat in pending]

    # ── Chunk intake ──
    def add(self, chunk: RawDumpChunk) -> RawDump | None:
        """Add one chunk. Returns a RawDump when that device is complete.

        If it is complete but the metadata has not arrived yet, it is held and
        None is returned — `register()` will release it.
        """
        key = (chunk.dump_id, chunk.device)
        with self._lock:
            store = self._chunks.setdefault(key, {})
            store[chunk.chunk_index] = chunk.interleaved
            if chunk.is_last:
                self._last[key] = chunk.chunk_index
            expected = self._last.get(key)
            if expected is None:
                return None
            if len(store) != expected + 1:
                return None  # chunks still missing
            ordered = [store[i] for i in range(expected + 1)]
            self._chunks.pop(key, None)
            self._last.pop(key, None)

            flat = np.concatenate(ordered) if len(ordered) > 1 else ordered[0]
            meta = self._meta.get(chunk.dump_id)
            if meta is None:
                # Response not here yet — channel count unknown, do not reshape (§9.13)
                self._orphans.setdefault(chunk.dump_id, []).append((chunk.device, flat))
                return None

        return self._build(chunk.dump_id, chunk.device, flat, meta)

    # ── Assembly ──
    def _build(self, dump_id: int, device: int, flat: np.ndarray, meta: dict) -> RawDump:
        dev_meta = next((d for d in meta.get("devices", []) if d.get("device") == device), {})
        channels = list(dev_meta.get("channels", []))
        num_ch = int(dev_meta.get("num_channels", len(channels) or 1))
        usable = (flat.size // num_ch) * num_ch
        if usable != flat.size:
            log.warning(
                "dump %d device %d: %d samples not divisible by %d channels; truncating",
                dump_id, device, flat.size, num_ch,
            )
        return RawDump(
            dump_id=dump_id,
            device=device,
            samples=flat[:usable].reshape(-1, num_ch),
            channels=channels,
            sample_rate=float(dev_meta.get("sample_rate", 0.0)),
            units=meta.get("units", ""),
            start_index=int(dev_meta.get("start_index", -1)),
        )

    def forget(self, dump_id: int) -> None:
        """Drop this dump's metadata and any partial chunks immediately.

        Rarely needed — KEEP_DUMPS prunes automatically. Only use it when you are
        certain every device has been received.
        """
        with self._lock:
            self._meta.pop(dump_id, None)
            self._orphans.pop(dump_id, None)
            if dump_id in self._order:
                self._order.remove(dump_id)
            for key in [k for k in self._chunks if k[0] == dump_id]:
                self._chunks.pop(key, None)
                self._last.pop(key, None)


@dataclass(slots=True)
class StreamStats:
    """Client-side counters. Server-side drops come from get_config (§6, §9.10)."""

    frames: int = 0
    bytes_: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    queue_overflows: int = 0
    started_at: float = field(default_factory=time.monotonic)

    def note(self, name: str, payload_bytes: int) -> None:
        self.frames += 1
        self.bytes_ += payload_bytes + 5
        self.by_type[name] = self.by_type.get(name, 0) + 1

    def reset(self) -> None:
        self.frames = 0
        self.bytes_ = 0
        self.by_type = {}
        self.queue_overflows = 0
        self.started_at = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def kb_per_s(self) -> float:
        return self.bytes_ / 1024 / self.elapsed if self.elapsed else 0.0

    @property
    def mbps(self) -> float:
        return self.bytes_ * 8 / 1e6 / self.elapsed if self.elapsed else 0.0


class StreamClient:
    """Reads the stream port on a background thread and emits decoded frames.

    Two ways to consume:
      * on_frame(handler) — called on the reader thread; must be quick.
      * frames() / get(timeout) — pull from an internal queue; fine when slow.
    """

    def __init__(
        self,
        host: str,
        port: int = 5001,
        *,
        connect_timeout: float = 5.0,
        queue_size: int = 4096,
        enable_queue: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout

        self._sock: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._closing = threading.Event()
        self._queue: queue.Queue[StreamFrame] | None = (
            queue.Queue(maxsize=queue_size) if enable_queue else None
        )
        self._handlers: list[FrameHandler] = []
        self._dump_handlers: list[Callable[[RawDump], None]] = []
        self._msg_handlers: list[Callable[[dict], None]] = []
        self._disconnect_handlers: list[Callable[[BaseException | None], None]] = []

        self.dumps = _DumpAssembler()
        self.stats = StreamStats()
        self._handshake: dict | None = None
        self._handshake_arrived = threading.Event()
        self._lock = threading.Lock()
        #: Frame payload layout. Fixed by the handshake that arrives on connect,
        #: so it is already correct before the first data frame (§5.3).
        self._layout = LAYOUT_V3

    # ── Connection lifetime ─────────────────────────────────────
    def connect(self, *, wait_handshake: bool = True) -> dict | None:
        if self._sock is not None:
            raise RuntimeError("already connected")
        self._closing.clear()
        self._handshake_arrived.clear()

        sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        sock.settimeout(None)  # nothing arrives while the scan is stopped (§9.3)
        self._sock = sock

        self._reader = threading.Thread(
            target=self._read_loop, name=f"pislm-stream-{self.host}", daemon=True
        )
        self._reader.start()

        if wait_handshake and not self._handshake_arrived.wait(self.connect_timeout):
            self.close()
            raise TimeoutError("no stream handshake within connect_timeout")
        return self._handshake

    def close(self) -> None:
        self._closing.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        reader, self._reader = self._reader, None
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=2.0)

    @property
    def connected(self) -> bool:
        return self._sock is not None and not self._closing.is_set()

    def __enter__(self) -> StreamClient:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def handshake(self) -> dict | None:
        with self._lock:
            return self._handshake

    @property
    def layout(self) -> int:
        """Frame layout version in use on this connection (3 or 4)."""
        with self._lock:
            return self._layout

    # ── Callbacks / queue ───────────────────────────────────────
    def on_frame(self, handler: FrameHandler) -> FrameHandler:
        self._handlers.append(handler)
        return handler

    def on_dump(self, handler: Callable[[RawDump], None]):
        """Called once per completed RAW_DUMP (one per device)."""
        self._dump_handlers.append(handler)
        return handler

    def on_message(self, handler: Callable[[dict], None]):
        """JSON body of MSG frames (handshake / events)."""
        self._msg_handlers.append(handler)
        return handler

    def on_disconnect(self, handler: Callable[[BaseException | None], None]):
        self._disconnect_handlers.append(handler)
        return handler

    def register_dump(self, dump_id: int, response: dict) -> None:
        """Register metadata from a get_raw response.

        Dumps whose chunks arrived before the response are completed here and
        handed to the on_dump handlers (§9.13).
        """
        for dump in self.dumps.register(dump_id, response):
            self._emit_dump(dump)

    def _emit_dump(self, dump: RawDump) -> None:
        for handler in list(self._dump_handlers):
            try:
                handler(dump)
            except Exception:  # noqa: BLE001
                log.exception("dump handler failed")

    def get(self, timeout: float | None = None) -> StreamFrame | None:
        if self._queue is None:
            raise RuntimeError("queue is disabled; use on_frame()")
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def frames(self, timeout: float | None = None):
        """Generator yielding decoded frames."""
        while self.connected:
            frame = self.get(timeout=timeout)
            if frame is not None:
                yield frame

    def drain(self) -> list[StreamFrame]:
        out: list[StreamFrame] = []
        if self._queue is None:
            return out
        while True:
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                return out

    # ── Reader thread ───────────────────────────────────────────
    def _read_loop(self) -> None:
        sock = self._sock
        error: BaseException | None = None
        try:
            while not self._closing.is_set():
                ftype, payload = read_frame(sock)
                try:
                    frame = decode_frame(ftype, payload, self._layout)
                except Exception:  # noqa: BLE001
                    # The header was already consumed, so sync is intact (§9.7).
                    log.exception("failed to decode frame type 0x%02x", ftype)
                    continue
                self._handle(frame, len(payload))
        except BaseException as exc:  # noqa: BLE001
            if not self._closing.is_set():
                error = exc
                log.debug("stream reader stopped: %r", exc)
        finally:
            self._closing.set()
            for handler in list(self._disconnect_handlers):
                try:
                    handler(error)
                except Exception:  # noqa: BLE001
                    log.exception("stream disconnect handler failed")

    def _handle(self, frame: StreamFrame, payload_bytes: int) -> None:
        self.stats.note(type(frame).__name__, payload_bytes)

        if isinstance(frame, MsgFrame):
            body = frame.payload
            if body.get("type") in ("handshake", "event"):
                if body.get("type") == "handshake" or body.get("event") == "started":
                    with self._lock:
                        self._handshake = body
                        self._layout = layout_of(body)
                    self._handshake_arrived.set()
            for handler in list(self._msg_handlers):
                try:
                    handler(body)
                except Exception:  # noqa: BLE001
                    log.exception("message handler failed")

        elif isinstance(frame, RawDumpChunk):
            completed = self.dumps.add(frame)
            if completed is not None:
                self._emit_dump(completed)

        for handler in list(self._handlers):
            try:
                handler(frame)
            except Exception:  # noqa: BLE001
                log.exception("frame handler failed")

        if self._queue is not None:
            try:
                self._queue.put_nowait(frame)
            except queue.Full:
                # Drop the oldest when the consumer lags — same policy as the server.
                self.stats.queue_overflows += 1
                try:
                    self._queue.get_nowait()
                    self._queue.put_nowait(frame)
                except (queue.Empty, queue.Full):
                    pass


__all__ = [
    "StreamClient",
    "StreamStats",
    "RawDump",
    "DataFrame",
    "BandFrame",
    "LevelFrame",
    "BandLevelFrame",
    "RawDumpChunk",
    "MsgFrame",
    "PeerClosed",
]
