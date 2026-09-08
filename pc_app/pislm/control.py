"""Control port client (default 5000) — PROTOCOL.md §1, §3, §4.

The rules that matter (§9):
  2. JSON is newline-delimited, not one-object-per-recv() → use LineBuffer.
  3. Never put a socket timeout on a long-lived listener; bound connect() only.
     Timeouts belong on individual request/response pairs.
  5. Match responses by id, not by arrival order.
"""

from __future__ import annotations

import itertools
import logging
import socket
import threading
from typing import Any, Callable

from .framing import LineBuffer, PeerClosed, encode_command

log = logging.getLogger("pislm.control")

EventHandler = Callable[[dict], None]


class CommandError(RuntimeError):
    """The server answered with ok:false."""

    def __init__(self, cmd: str, error: str) -> None:
        super().__init__(f"{cmd}: {error}")
        self.cmd = cmd
        self.error = error

    @property
    def needs_stop(self) -> bool:
        """§4 / §9.8 — a config command refused because a scan is running."""
        return "scan is active" in self.error


class CommandTimeout(TimeoutError):
    """No response with that id arrived within the deadline."""


class _Waiter:
    __slots__ = ("event", "response")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.response: dict | None = None


class ControlClient:
    """Thread-safe client for one control port.

    A background reader thread reads lines and dispatches them as handshake,
    response or event.
    """

    def __init__(
        self,
        host: str,
        port: int = 5000,
        *,
        connect_timeout: float = 5.0,
        default_timeout: float = 10.0,
    ) -> None:
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.default_timeout = default_timeout

        self._sock: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._waiters: dict[Any, _Waiter] = {}
        self._ids = itertools.count(1)
        self._closing = threading.Event()

        self._handshake: dict | None = None
        self._handshake_arrived = threading.Event()

        self._event_handlers: list[EventHandler] = []
        self._handshake_handlers: list[EventHandler] = []
        self._disconnect_handlers: list[Callable[[BaseException | None], None]] = []

    # ── Connection lifetime ─────────────────────────────────────
    def connect(self, *, wait_handshake: bool = True) -> dict | None:
        if self._sock is not None:
            raise RuntimeError("already connected")
        self._closing.clear()
        self._handshake_arrived.clear()

        # Bound connect() so an unreachable host still fails fast.
        sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        # Then clear it — silence is normal on this port (§9.3).
        sock.settimeout(None)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock

        self._reader = threading.Thread(
            target=self._read_loop, name=f"pislm-control-{self.host}", daemon=True
        )
        self._reader.start()

        if wait_handshake:
            if not self._handshake_arrived.wait(self.connect_timeout):
                self.close()
                raise CommandTimeout("no handshake within connect_timeout")
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
        self._fail_all_waiters(PeerClosed("client closed"))

    @property
    def connected(self) -> bool:
        return self._sock is not None and not self._closing.is_set()

    def __enter__(self) -> ControlClient:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── Callback registration ───────────────────────────────────
    def on_event(self, handler: EventHandler) -> EventHandler:
        self._event_handlers.append(handler)
        return handler

    def on_handshake(self, handler: EventHandler) -> EventHandler:
        self._handshake_handlers.append(handler)
        return handler

    def on_disconnect(self, handler: Callable[[BaseException | None], None]):
        self._disconnect_handlers.append(handler)
        return handler

    @property
    def handshake(self) -> dict | None:
        with self._state_lock:
            return self._handshake

    # ── Commands ────────────────────────────────────────────────
    def send(self, cmd: str, *, timeout: float | None = None, **fields: Any) -> dict:
        """Send a command and return its result. Raises CommandError on ok:false.

        The timeout applies to this request/response pair only, never to the
        socket itself (§9.3).
        """
        response = self.request(cmd, timeout=timeout, **fields)
        if not response.get("ok"):
            raise CommandError(cmd, response.get("error", "unknown error"))
        return response.get("result", {})

    def request(self, cmd: str, *, timeout: float | None = None, **fields: Any) -> dict:
        """Like send() but returns the whole response instead of raising."""
        sock = self._sock
        if sock is None:
            raise PeerClosed("not connected")

        req_id = next(self._ids)
        waiter = _Waiter()
        with self._state_lock:
            self._waiters[req_id] = waiter

        payload = encode_command({"id": req_id, "cmd": cmd, **fields})
        try:
            with self._send_lock:
                sock.sendall(payload)
        except OSError:
            with self._state_lock:
                self._waiters.pop(req_id, None)
            raise

        limit = self.default_timeout if timeout is None else timeout
        if not waiter.event.wait(limit):
            with self._state_lock:
                self._waiters.pop(req_id, None)
            raise CommandTimeout(f"no response to {cmd!r} (id={req_id}) within {limit}s")

        response = waiter.response
        if response is None:  # woken because the connection dropped
            raise PeerClosed(f"connection lost while waiting for {cmd!r}")
        return response

    def send_nowait(self, cmd: str, **fields: Any) -> None:
        """Send without waiting for a response (pipelining, §1)."""
        sock = self._sock
        if sock is None:
            raise PeerClosed("not connected")
        with self._send_lock:
            sock.sendall(encode_command({"cmd": cmd, **fields}))

    def ping(self, timeout: float = 3.0) -> bool:
        """Liveness check. The timeout is on this pair, not on the socket (§9.3)."""
        try:
            return bool(self.send("ping", timeout=timeout).get("pong"))
        except (CommandTimeout, OSError):
            return False

    # ── Reader thread ───────────────────────────────────────────
    def _read_loop(self) -> None:
        sock = self._sock
        buf = LineBuffer()
        error: BaseException | None = None
        try:
            while not self._closing.is_set():
                chunk = sock.recv(65536)
                if not chunk:  # the only reliable disconnect signal (§9.3)
                    raise PeerClosed("control port closed by peer")
                for msg in buf.feed(chunk):
                    self._dispatch(msg)
        except BaseException as exc:  # noqa: BLE001 — thread boundary
            if not self._closing.is_set():
                error = exc
                log.debug("control reader stopped: %r", exc)
        finally:
            self._closing.set()
            self._fail_all_waiters(error or PeerClosed("control reader stopped"))
            for handler in list(self._disconnect_handlers):
                try:
                    handler(error)
                except Exception:  # noqa: BLE001
                    log.exception("disconnect handler failed")

    def _dispatch(self, msg: dict) -> None:
        kind = msg.get("type")

        if kind == "response":
            req_id = msg.get("id")
            with self._state_lock:
                waiter = self._waiters.pop(req_id, None)
            if waiter is None:
                log.debug("unmatched response id=%r cmd=%r", req_id, msg.get("cmd"))
                return
            waiter.response = msg
            waiter.event.set()
            return

        if kind == "handshake":
            with self._state_lock:
                self._handshake = msg
            self._handshake_arrived.set()
            self._notify(self._handshake_handlers, msg)
            return

        if kind == "event":
            # A started event carries the whole config → refresh the cache (§9.6)
            if msg.get("event") == "started":
                with self._state_lock:
                    self._handshake = msg
            elif msg.get("event") == "stopped":
                with self._state_lock:
                    if self._handshake is not None:
                        self._handshake = {**self._handshake, "running": False}
            self._notify(self._event_handlers, msg)
            return

        log.debug("unknown control message type=%r", kind)

    @staticmethod
    def _notify(handlers: list[EventHandler], msg: dict) -> None:
        for handler in list(handlers):
            try:
                handler(msg)
            except Exception:  # noqa: BLE001 — one callback must not kill the reader
                log.exception("handler failed for %r", msg.get("type"))

    def _fail_all_waiters(self, _exc: BaseException) -> None:
        with self._state_lock:
            waiters, self._waiters = self._waiters, {}
        for waiter in waiters.values():
            waiter.response = None
            waiter.event.set()
