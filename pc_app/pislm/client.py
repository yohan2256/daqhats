"""PiSLM facade — control port + stream port, plus every §4 command.

    from pislm import PiSLM

    with PiSLM("192.168.0.42") as pi:
        pi.set_sensitivity(0, 50)
        pi.set_bands(enabled=True, output="level", fraction=1, f_min=50, f_max=630)
        pi.start()
        for frame in pi.stream.frames(timeout=1.0):
            ...
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Iterable, Sequence

from .control import CommandError, CommandTimeout, ControlClient
from .frames import Handshake
from .gaps import GapDetector
from .stream import RawDump, StreamClient

log = logging.getLogger("pislm")


class PiSLM:
    """High-level facade over both ports.

    Both are opened by default (§9.4): even if you only want levels, the
    control port is what starts the scan.
    """

    def __init__(
        self,
        host: str,
        control_port: int = 5000,
        stream_port: int = 5001,
        *,
        connect_timeout: float = 5.0,
        command_timeout: float = 10.0,
        #: 0 disables the frame queue — use it when reading via `on_frame`.
        stream_queue_size: int = 4096,
        open_stream: bool = True,
    ) -> None:
        self.host = host
        self.open_stream = open_stream
        self.control = ControlClient(
            host, control_port, connect_timeout=connect_timeout, default_timeout=command_timeout
        )
        # `stream_queue_size=0` disables the queue entirely. A consumer that
        # works through `on_frame` callbacks — which is what both GUIs do —
        # never drains it, so it fills within seconds and then counts every
        # subsequent frame as an overflow, for ever. The frames are not lost;
        # `on_frame` and the gap detector run before the queue. But the
        # counter reads as "the consumer is slower than the stream" when the
        # truth is that there is no consumer.
        self.stream = StreamClient(
            host, stream_port, connect_timeout=connect_timeout,
            queue_size=stream_queue_size or 1,
            enable_queue=bool(stream_queue_size),
        )

        self._config = Handshake()
        self._config_lock = threading.Lock()

        #: Frame-loss watchdog (v4 only). Check `gaps.clean` when a session ends.
        self.gaps = GapDetector()
        #: Overload (clipping) events, accumulated — §3 of the amendment.
        self.overloads: list[dict] = []
        self.acquisition_errors: list[dict] = []

        self.control.on_handshake(self._absorb)
        self.control.on_event(self._absorb)
        self.stream.on_message(self._absorb)
        self.stream.on_frame(self.gaps.feed)

    # ── Connection ──
    def connect(self) -> Handshake:
        """Open both ports. Ports already open are skipped, so this is re-callable."""
        if not self.control.connected:
            self.control.connect()
        if self.open_stream and not self.stream.connected:
            self.stream.connect()
        return self.config

    def close(self) -> None:
        try:
            self.stream.close()
        finally:
            self.control.close()

    def __enter__(self) -> PiSLM:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        return self.control.connected

    # ── Config cache (§9.6: do not cache, re-read the latest) ──
    def _absorb(self, msg: dict) -> None:
        """Refresh the config from a handshake, started event or get_config."""
        kind = msg.get("type")
        if kind == "handshake" or (kind == "event" and msg.get("event") == "started"):
            cfg = Handshake(raw=dict(msg))
            with self._config_lock:
                self._config = cfg
            # Indices reset to 0 on every start, so the watchdog resets too (§1.3)
            if kind == "event":
                self.gaps.reset()
                self.overloads.clear()
                self.acquisition_errors.clear()
            self.gaps.configure(cfg)
        elif kind == "event" and msg.get("event") == "overload":
            self.overloads.append(dict(msg))
        elif kind == "event" and msg.get("event") == "overrun":
            self.acquisition_errors.append(dict(msg))
            with self._config_lock:
                self._config = Handshake(raw={**self._config.raw, "running": False})
        elif kind == "event" and msg.get("event") == "stopped":
            with self._config_lock:
                if self._config.raw:
                    self._config = Handshake(raw={**self._config.raw, "running": False})

    @property
    def config(self) -> Handshake:
        """Last observed config. Call refresh() when you need certainty."""
        with self._config_lock:
            return self._config

    def refresh(self) -> Handshake:
        """Re-read via get_config — dsp/network fields live only there (§9.10)."""
        result = self.control.send("get_config")
        cfg = Handshake(raw=dict(result))
        with self._config_lock:
            self._config = cfg
        return cfg

    # ── Raw command access ──
    def send(self, cmd: str, **fields: Any) -> dict:
        return self.control.send(cmd, **fields)

    def send_stopped(self, cmd: str, **fields: Any) -> dict:
        """Send a config command, recovering with stop -> retry -> start (§9.8)."""
        try:
            return self.control.send(cmd, **fields)
        except CommandError as exc:
            if not exc.needs_stop:
                raise
        # needs_stop means a scan was running, so bring it back when we are done.
        self.stop()
        try:
            return self.control.send(cmd, **fields)
        finally:
            self.start()

    # ── Streaming (§4) ──
    def start(self) -> Handshake:
        self.gaps.reset()
        self.overloads.clear()
        self.acquisition_errors.clear()
        result = self.control.send("start")
        cfg = Handshake(raw=dict(result))
        with self._config_lock:
            self._config = cfg
        self.gaps.configure(cfg)
        return cfg

    @property
    def protocol_version(self) -> int:
        """Major protocol version from the handshake, 0 when not yet known.

        `protocol` is a string like `"pislm/4"`. Anything unparseable counts
        as unknown rather than old — guessing "old" would put a false
        "cannot be verified" on a server that simply has not answered yet.
        """
        _, _, tail = self.config.protocol.partition("/")
        try:
            return int(tail.split(".")[0])
        except (TypeError, ValueError):
            return 0

    @property
    def measurement_status(self) -> tuple[str, list[str]]:
        """Integrity verdict: (`ok` | `warning` | `invalid` | `unknown`, reasons).

        Criteria (PROTOCOL.md §6, §9.12):
          * **LEVEL / BAND_LEVEL loss** -> invalid. The meter's main output is
            damaged. These frames are practically never dropped, so a gap means
            even the large reliable queue filled up.
          * **Overload** -> invalid, as most SLM standards require.
          * **DATA / BAND loss** -> a warning, not invalid. It is designed
            behaviour under network pressure and is recoverable with `get_raw`.
          * **pislm/3 server** -> invalid, because loss cannot be detected at all.
          * **Nothing received yet** -> `unknown`. Not a verdict.

        That last state is why this exists. The detector only becomes active
        once a frame carrying a `start_index` has arrived, so before the scan
        starts — and any time it is stopped — there is nothing to judge. The
        old code read that as "server is pislm/3", which put a red
        "frame loss cannot be verified" on a perfectly healthy pislm/4 server
        for as long as the scan was idle, and named the wrong cause while
        doing it.
        """
        reasons: list[str] = []
        version = self.protocol_version

        if version and version < 4:
            return "invalid", [
                f"server is {self.config.protocol} — frame loss cannot be verified"
            ]
        if self.acquisition_errors:
            return "invalid", ["acquisition overrun: raw waveform is incomplete; restart and recapture"]
        if not self.gaps.active:
            return "unknown", [
                "no stream frames received yet — nothing to verify"
                if version else "waiting for the handshake"
            ]

        if self.gaps.reliable_missing:
            # Say it in units that can be acted on. The raw count sums every
            # LEVEL and BAND_LEVEL stream — nearly a hundred counters on a
            # six-channel rig — so it reads as a catastrophe when it is often
            # a second of one dropout. `gaps.diagnosis()` has the rest.
            reasons.append(
                f"level stream loss: {self.gaps.reliable_summary()} — "
                "the meter's primary output may be damaged"
            )
        if self.gaps.out_of_order:
            reasons.append(
                f"{self.gaps.out_of_order} out-of-order frame(s) (protocol violation)")
        if self.overloads:
            channels = sorted({o.get("channel") for o in self.overloads})
            reasons.append(f"overload on channel(s) {channels}")
        if reasons:
            return "invalid", reasons

        warnings = self.measurement_warnings
        return ("warning", warnings) if warnings else ("ok", [])

    @property
    def measurement_valid(self) -> tuple[bool, list[str]]:
        """Is this scan usable as a measurement? Returns (valid, reasons).

        `unknown` counts as not valid — a report must not be issued on data
        whose integrity was never checked — but see `measurement_status` when
        the difference matters, which it does for anything on screen.
        """
        status, reasons = self.measurement_status
        if status in ("ok", "warning"):
            return True, []
        return False, reasons

    @property
    def measurement_warnings(self) -> list[str]:
        """Not disqualifying, but worth knowing about."""
        warnings: list[str] = []
        lost = self.gaps.best_effort_missing
        if lost:
            warnings.append(
                f"{lost:,} raw/band samples lost (allowed per §6). "
                "If you post-analyse the raw waveform, re-fetch that span with get_raw"
            )
        if self.stream.stats.queue_overflows:
            warnings.append(
                f"{self.stream.stats.queue_overflows} client queue overflow(s) — "
                "the consumer is slower than the stream"
            )
        return warnings

    def stop(self) -> bool:
        try:
            return not self.control.send("stop").get("running", False)
        except CommandError as exc:
            if "not running" in exc.error or "already" in exc.error:
                return True
            raise

    @property
    def running(self) -> bool:
        return bool(self.control.send("status").get("running", False))

    # ── Queries (§4) ──
    def ping(self, timeout: float = 3.0) -> bool:
        return self.control.ping(timeout)

    def status(self) -> dict:
        """Small per-device snapshot. No dsp/network — use get_config (§9.10)."""
        return self.control.send("status")

    def info(self) -> dict:
        return self.control.send("info")

    def get_clock(self) -> dict:
        return self.control.send("get_clock")

    def get_sensitivity(self, channel: int) -> float:
        return float(self.control.send("get_sensitivity", channel=channel)["sensitivity"])

    def get_iepe(self, channel: int) -> int:
        return int(self.control.send("get_iepe", channel=channel)["mode"])

    def calibration_read(self, channel: int) -> dict:
        """mcc172 channels only."""
        return self.control.send("calibration_read", channel=channel)

    # ── Configuration (requires the scan to be stopped, §4) ──
    def set_sensitivity(self, channel: int, value: float) -> dict:
        """mV per unit. A 50 mV/Pa mic -> 50, and units become Pa. 1000 = volts."""
        return self.send_stopped("set_sensitivity", channel=channel, value=value)

    def set_iepe(self, channel: int, mode: bool | int | str) -> dict:
        if isinstance(mode, bool):
            mode = 1 if mode else 0
        return self.send_stopped("set_iepe", channel=channel, mode=mode)

    def set_sample_rate(self, sample_rate: float) -> dict:
        """Applies to every device; each rounds the rate differently (§4)."""
        return self.send_stopped("set_sample_rate", sample_rate=sample_rate)

    def set_channels(self, device: int, channels: Sequence[int]) -> dict:
        """Device-local channels. Global numbering is rebuilt, so re-read channel_map."""
        result = self.send_stopped("set_channels", device=device, channels=list(channels))
        self.refresh()
        return result

    def set_trigger(
        self,
        enable: bool,
        *,
        source: str | None = None,
        gpio_pin: int | None = None,
        pulse_ms: float | None = None,
    ) -> dict:
        fields: dict[str, Any] = {"enable": enable}
        if source is not None:
            fields["source"] = source
        if gpio_pin is not None:
            fields["gpio_pin"] = gpio_pin
        if pulse_ms is not None:
            fields["pulse_ms"] = pulse_ms
        return self.send_stopped("set_trigger", **fields)

    def set_options(self, *, stream_raw: bool | None = None) -> dict:
        fields = {} if stream_raw is None else {"stream_raw": stream_raw}
        return self.send_stopped("set_options", **fields)

    def set_bands(
        self,
        *,
        enabled: bool | None = None,
        output: str | None = None,
        f_min: float | None = None,
        f_max: float | None = None,
        fraction: int | None = None,
        order: int | None = None,
        margin: float | None = None,
    ) -> dict:
        """fraction=1 -> 1/1 octave, fraction=3 -> 1/3 octave."""
        fields = {
            k: v
            for k, v in dict(
                enabled=enabled, output=output, f_min=f_min, f_max=f_max,
                fraction=fraction, order=order, margin=margin,
            ).items()
            if v is not None
        }
        return self.send_stopped("set_bands", **fields)

    def set_weighting(self, *, frequency: str | None = None, time_weighting: str | None = None) -> dict:
        """frequency: A|C|Z, time_weighting: Fast|Slow|Impulse."""
        fields: dict[str, Any] = {}
        if frequency is not None:
            fields["frequency"] = frequency
        if time_weighting is not None:
            fields["time"] = time_weighting
        return self.send_stopped("set_weighting", **fields)

    def set_level(self, *, enabled: bool | None = None, output_rate: float | None = None) -> dict:
        fields = {
            k: v for k, v in dict(enabled=enabled, output_rate=output_rate).items() if v is not None
        }
        return self.send_stopped("set_level", **fields)

    def set_storage(self, buffer_seconds: float) -> dict:
        """Raw ring buffer length — the largest window get_raw / get_metrics can see."""
        return self.send_stopped("set_storage", buffer_seconds=buffer_seconds)

    def set_dsp(self, workers: int) -> dict:
        return self.send_stopped("set_dsp", workers=workers)

    def set_resample(
        self,
        *,
        enabled: bool | None = None,
        output_rate: float | None = None,
        taps: int | None = None,
        phases: int | None = None,
    ) -> dict:
        """The only thing that actually removes inter-device clock drift (§4)."""
        fields = {
            k: v
            for k, v in dict(enabled=enabled, output_rate=output_rate, taps=taps, phases=phases).items()
            if v is not None
        }
        return self.send_stopped("set_resample", **fields)

    def save_config(self, *, path: str | None = None, include_settings: bool | None = None) -> dict:
        fields = {
            k: v for k, v in dict(path=path, include_settings=include_settings).items() if v is not None
        }
        return self.send_stopped("save_config", **fields)

    def calibration_write(self, channel: int, slope: float, offset: float) -> dict:
        return self.send_stopped("calibration_write", channel=channel, slope=slope, offset=offset)

    # ── Calibration (scan may keep running, §4) ──
    def calibrate(
        self,
        channel: int,
        *,
        level_db: float = 94.0,
        seconds: float = 3.0,
        freq: float = 1000.0,
        bandpass: bool = True,
        apply: bool = True,
        timeout: float | None = None,
    ) -> dict:
        """Match sensitivity to an acoustic calibrator. apply=False only reports.

        Applying briefly stops and restarts the scan, so the ring buffers empty —
        let it run for a moment before calibrating the next channel.
        """
        limit = timeout if timeout is not None else max(15.0, seconds * 3 + 10)
        return self.control.send(
            "calibrate",
            channel=channel,
            level_db=level_db,
            seconds=seconds,
            freq=freq,
            bandpass=bandpass,
            apply=apply,
            timeout=limit,
        )

    # ── Metrics (§4) ──
    def get_metrics(
        self,
        *,
        seconds: float | None = None,
        weighting: str | None = None,
        time_weighting: str | None = None,
        percentiles: Iterable[float] | None = None,
        channels: Iterable[int] | None = None,
        include_bands: bool = False,
        timeout: float | None = 30.0,
    ) -> dict:
        """Leq/Lmax/Lmin/Lpeak/LN over the buffered raw samples. Works when stopped."""
        fields: dict[str, Any] = {"include_bands": include_bands}
        if seconds is not None:
            fields["seconds"] = seconds
        if weighting is not None:
            fields["weighting"] = weighting
        if time_weighting is not None:
            fields["time_weighting"] = time_weighting
        if percentiles is not None:
            fields["percentiles"] = list(percentiles)
        if channels is not None:
            fields["channels"] = list(channels)
        return self.control.send("get_metrics", timeout=timeout, **fields)

    # ── Raw dump (§2.5, §4) ──
    def get_raw(
        self,
        *,
        seconds: float | None = None,
        devices: Iterable[int] | None = None,
    ) -> dict:
        """Request a buffer dump. Chunks arrive asynchronously on the stream port.

        The stream port must be open. Use `fetch_raw()` to get the completed
        dumps synchronously.
        """
        if not self.stream.connected:
            raise RuntimeError("get_raw requires a connected stream client (§4)")
        fields: dict[str, Any] = {}
        if seconds is not None:
            fields["seconds"] = seconds
        if devices is not None:
            fields["devices"] = list(devices)
        result = self.control.send("get_raw", **fields)
        # Chunks may have arrived before this response — register releases them (§9.13)
        self.stream.register_dump(int(result["dump_id"]), result)
        return result

    def fetch_raw(
        self,
        *,
        seconds: float | None = None,
        devices: Iterable[int] | None = None,
        timeout: float = 60.0,
    ) -> dict[int, RawDump]:
        """Send get_raw and wait until every device dump is complete.

        Returns: {device_index: RawDump}
        """
        by_dump: dict[int, dict[int, RawDump]] = {}
        lock = threading.Lock()
        done = threading.Event()
        dump_id: int | None = None
        expected: set[int] = set()

        def collector(dump: RawDump) -> None:
            with lock:
                if dump_id is not None and dump.dump_id != dump_id:
                    return
                by_dump.setdefault(dump.dump_id, {})[dump.device] = dump
                if (dump_id is not None and expected
                        and set(by_dump.get(dump_id, {})) >= expected):
                    done.set()

        self.stream.on_dump(collector)
        try:
            result = self.get_raw(seconds=seconds, devices=devices)
            with lock:
                dump_id = int(result["dump_id"])
                expected = {int(d["device"]) for d in result.get("devices", [])}
                collected = by_dump.setdefault(dump_id, {})
                if not expected:
                    return {}
                if set(collected) >= expected:
                    done.set()
            if not done.wait(timeout):
                raise TimeoutError(
                    f"raw dump {dump_id} incomplete after {timeout}s "
                    f"(got devices {sorted(collected)}, expected {sorted(expected)})"
                )
            with lock:
                return {device: collected[device] for device in expected}
        finally:
            try:
                self.stream._dump_handlers.remove(collector)  # noqa: SLF001
            except ValueError:
                pass

    # ── Analog output (PROTOCOL_OUTPUT.md) ──
    @property
    def output_available(self) -> bool:
        """Does this hardware have an analog output the server can drive?"""
        return bool((self.config.raw.get("output") or {}).get("available", False))

    def set_output(self, settings) -> dict:
        """Configure the generator. `settings` is an `ExcitationSettings`.

        Unlike the acquisition settings this does not need the scan stopped —
        the point is to play while measuring.
        """
        return self.control.send("set_output", **settings.to_command())

    def output_start(self) -> dict:
        """Start playing. The result carries `start_index` on the DATA grid.

        That index is what lets the client locate the excitation inside the
        recorded waveform, which sweep and MLS deconvolution depend on.
        """
        return self.control.send("output_start")

    def output_stop(self) -> dict:
        return self.control.send("output_stop")

    def output_status(self) -> dict:
        return self.control.send("output_status")

    # ── Controls (§4) ──
    def blink_led(self, count: int = 3, *, device: int | None = None) -> dict:
        fields: dict[str, Any] = {"count": count}
        if device is not None:
            fields["device"] = device
        return self.control.send("blink_led", **fields)

    # ── Bandwidth benchmark (§8) ──
    def bench(self, seconds: float = 15.0) -> dict:
        """Measure received throughput and the drop counter deltas.

        If dropped_blocks stays at 0 while stream_frames_dropped rises, the
        bottleneck is the network, not the Pi DSP (§8).
        """
        before = self.refresh()
        t0 = time.monotonic()
        self.stream.stats.reset()

        rtt_samples = []
        for _ in range(5):
            t = time.monotonic()
            self.control.send("ping", timeout=5.0)
            rtt_samples.append((time.monotonic() - t) * 1000)

        time.sleep(max(0.0, seconds - (time.monotonic() - t0)))
        stats = self.stream.stats
        after = self.refresh()

        def delta(a: int | None, b: int | None) -> int | None:
            return None if a is None or b is None else b - a

        return {
            "seconds": stats.elapsed,
            "kb_per_s": stats.kb_per_s,
            "mbps": stats.mbps,
            "frames_per_s": stats.frames / stats.elapsed if stats.elapsed else 0.0,
            "by_type": dict(stats.by_type),
            "control_rtt_ms": sum(rtt_samples) / len(rtt_samples),
            "dropped_blocks_delta": delta(before.dropped_blocks, after.dropped_blocks),
            "stream_frames_dropped_delta": delta(
                before.stream_frames_dropped, after.stream_frames_dropped
            ),
            "client_queue_overflows": stats.queue_overflows,
        }

    # ── Callback delegation ──
    def on_event(self, handler: Callable[[dict], None]):
        return self.control.on_event(handler)

    def on_frame(self, handler):
        return self.stream.on_frame(handler)

    def on_dump(self, handler):
        return self.stream.on_dump(handler)


__all__ = ["PiSLM", "CommandError", "CommandTimeout"]
