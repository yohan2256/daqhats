#!/usr/bin/env python3
"""pislm simulator — a protocol-compatible fake Pi server.

For developing and testing the client and UI without hardware. It reproduces
the awkward parts of the protocol on purpose:

  * splits frames into small writes to force recv() reassembly (--fragment)
  * concatenates several JSON lines into one write
  * shuffles response order (--shuffle) to test id matching
  * refuses config commands while a scan is running
  * chunks RAW_DUMP and interleaves it with live frames

    python pislm_sim.py --control 5000 --stream 5001
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import queue
from collections import deque
import random
import socket
import socketserver
import struct
import threading
import time

import numpy as np
from scipy import signal as sig

HEADER = struct.Struct("<BI")
U32 = struct.Struct("<I")
U64 = struct.Struct("<Q")

FRAME_DATA, FRAME_MSG, FRAME_BAND, FRAME_LEVEL, FRAME_BAND_LEVEL, FRAME_RAW_DUMP = range(1, 7)

log = logging.getLogger("pislm.sim")


# ── State ───────────────────────────────────────────────────────────
class SimState:
    """Server-wide configuration plus the synthetic signal source."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.running = False
        self.sample_rate = 51200.0
        self.devices = [
            {"index": 0, "type": "mcc172", "channels": [0, 1], "actual_rate": 51200.0},
            {"index": 1, "type": "dt9837a", "channels": [2, 3, 4, 5], "actual_rate": 51200.0},
        ]
        self.sensitivity = {c: (50.0 if c != 1 else 1000.0) for c in range(6)}
        self.iepe = {c: 1 for c in range(6)}
        self.stream_raw = False
        self.bands = {
            "enabled": True, "output": "level", "fraction": 3,
            "order": 6, "f_min": 20.0, "f_max": 20000.0,
        }
        self.weighting = {"frequency": "A", "time": "Fast"}
        self.level = {"enabled": True, "output_rate": 10.0}
        self.storage = {"buffer_seconds": 60.0}
        self.trigger = {
            "enabled": False, "source": "gpio", "gpio_pin": 17,
            "pulse_ms": 10.0, "mode": "RISING_EDGE",
        }
        self.resample = {
            "enabled": True, "active": True, "output_rate": 48000.0,
            "taps": 32, "phases": 4096,
        }
        self.dropped_blocks = 0
        self.frames_dropped = 0
        self.frames_dropped_by_type: dict[str, int] = {}
        self.dump_counter = 1000

        # ── pislm/4 (PROTOCOL_v4_AMENDMENT.md) ──
        self.protocol_version = 4
        self.epoch: dict | None = None
        self.overload_counts = {c: 0 for c in range(6)}
        #: Per-stream sample counters. They advance even when a frame is dropped (§1.3).
        self.counters: dict[tuple, int] = {}

        # ── Analog output (PROTOCOL_OUTPUT.md) ──
        self.output = {
            "available": True, "device": 1, "channels": [0, 1],
            "output_rate": 48000.0, "full_scale_volts": 10.0,
            "running": False, "signal": None,
        }
        self.output_settings: dict | None = None
        self.output_signal: np.ndarray | None = None
        self.output_position = 0
        self.output_start_index: int | None = None
        #: Synthetic room state per device (see _room_filter)
        self.room_state: dict = {}

        self.stream_clients: list[StreamHandler] = []
        self.control_clients: list[ControlHandler] = []
        self.buffers = {0: None, 1: None}  # stand-in for the ring buffers

    def next_index(self, key: tuple, count: int) -> int:
        """Take the next start_index for this stream and advance the counter."""
        with self.lock:
            start = self.counters.get(key, 0)
            self.counters[key] = start + count
            return start

    def reset_counters(self) -> None:
        with self.lock:
            self.counters.clear()
            self.overload_counts = {c: 0 for c in self.all_channels()}
            now = time.time()
            self.epoch = {
                "index": 0,
                "unix": now,
                "utc": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
                       + f".{int(now % 1 * 1e6):06d}Z",
                "monotonic": time.monotonic(),
                "source": "system_clock",
                "note": "NTP synchronization is not guaranteed",
            }

    # ── Derived ──
    def units(self, ch: int) -> str:
        return "Pa" if self.sensitivity[ch] != 1000.0 else "V"

    def effective_rate(self) -> float:
        if self.resample.get("enabled") and self.resample.get("active"):
            return float(self.resample["output_rate"])
        return self.sample_rate

    def channel_map(self) -> list[dict]:
        out = []
        for dev in self.devices:
            for local, g in enumerate(dev["channels"]):
                out.append(
                    {"global": g, "device": dev["index"], "device_type": dev["type"], "local": local}
                )
        return sorted(out, key=lambda e: e["global"])

    def all_channels(self) -> list[int]:
        return sorted(c for dev in self.devices for c in dev["channels"])

    def band_centers(self) -> list[float]:
        fraction = int(self.bands["fraction"])
        f_min, f_max = float(self.bands["f_min"]), float(self.bands["f_max"])
        # IEC 61260 base-10: f_c = 1000 * 10^(3k / (10 * b))
        centers, k = [], -60
        while True:
            f = 1000.0 * 10 ** (3.0 * k / (10.0 * fraction))
            if f > f_max * 1.3:
                break
            if f_min * 0.85 <= f <= f_max * 1.15:
                centers.append(f)
            k += 1
            if k > 200:
                break
        return centers

    def band_table(self) -> list[dict]:
        fraction = int(self.bands["fraction"])
        ratio = 2 ** (1.0 / (2 * fraction))
        table = []
        for dev in self.devices:
            rate = self.effective_rate()
            bands = []
            for i, center in enumerate(self.band_centers()):
                f_hi = center * ratio
                dec = max(1, int(rate / (2.2 * f_hi)))
                bands.append({
                    "index": i,
                    "center": round(center, 4),
                    "f_lo": round(center / ratio, 4),
                    "f_hi": round(f_hi, 4),
                    "decimation": dec,
                    "decimated_rate": round(rate / dec, 4),
                })
            table.append({
                "device": dev["index"], "fraction": fraction,
                "order": int(self.bands["order"]), "input_rate": rate,
                "channels": list(dev["channels"]), "bands": bands,
            })
        return table

    def snapshot(self, *, include_band_table: bool | None = None) -> dict:
        with self.lock:
            # The final spec did not adopt a frame_layout field — the client
            # determines the layout from the protocol string (frames.layout_of).
            body = {
                "protocol": f"pislm/{self.protocol_version}",
                "running": self.running,
                "channels": self.all_channels(),
                "num_channels": len(self.all_channels()),
                "channel_map": self.channel_map(),
                "devices": [
                    {**d, "effective_rate": self.effective_rate()} for d in self.devices
                ],
                "sample_rate": self.sample_rate,
                "iepe": {str(c): self.iepe[c] for c in self.all_channels()},
                "sensitivity_mv_per_unit": {
                    str(c): self.sensitivity[c] for c in self.all_channels()
                },
                "units": {str(c): self.units(c) for c in self.all_channels()},
                "stream_raw": self.stream_raw,
                "bands": dict(self.bands),
                "weighting": dict(self.weighting),
                "level": dict(self.level),
                "storage": dict(self.storage),
                "trigger": dict(self.trigger),
                "dsp": {
                    "workers_configured": -1, "workers": 3,
                    "dropped_blocks": self.dropped_blocks,
                    "channels": [[0, 1], [2, 3], [4, 5]],
                },
                "resample": dict(self.resample),
                "clock": {
                    "0": {"nominal_rate": 51200.0, "measured_rate": 51202.51, "ppm": 49.02,
                          "points": 5000, "elapsed": 250.0, "settled": True},
                    "1": {"nominal_rate": 51200.0, "measured_rate": 51197.44, "ppm": -50.0,
                          "points": 5000, "elapsed": 250.0, "settled": True},
                },
                "clock_sync_note": "simulated",
                "network": {
                    "stream_clients": len(self.stream_clients),
                    "stream_frames_dropped": self.frames_dropped,
                    "stream_frames_dropped_by_type": dict(self.frames_dropped_by_type),
                },
                "output": {**self.output, "output_rate": self.effective_rate()},
                "dtype": "float64",
                "byte_order": "little",
                "interleave": "channel-fastest-per-device",
            }
            if self.protocol_version >= 4:
                body["overload"] = {str(c): v for c, v in self.overload_counts.items()}
                if self.epoch is not None:
                    body["epoch"] = dict(self.epoch)
            want_table = (
                self.bands.get("enabled") and (self.running if include_band_table is None else include_band_table)
            )
            if want_table:
                body["band_table"] = self.band_table()
            return body


STATE = SimState()
OPTS = argparse.Namespace(fragment=False, shuffle=False, jitter=0.0, drop=0.0, overload=0.0)


def reset_state() -> None:
    """Swap in a fresh global state (for test isolation).

    The simulator shares one module-level STATE. When several test modules each
    start a server, settings changed by an earlier test and leftover client
    handlers leak into the next one, so fixtures call this.
    """
    global STATE
    try:
        stop_scan()
    except Exception:  # noqa: BLE001
        pass
    for client in list(STATE.stream_clients) + list(STATE.control_clients):
        try:
            client.request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    STATE = SimState()


# ── Frame transmission ──────────────────────────────────────────────
#: How much of a frame to split up. The point is to check that the client
#: reassembles across the 5-byte header and the start of the payload, so
#: splitting the front is enough. Sending a whole 23 KB DATA frame 1–7 bytes at
#: a time means thousands of sendall calls, the sender thread falls behind, the
#: queue overflows and spurious drops appear.
FRAGMENT_PREFIX = 32


def send_frame(sock: socket.socket, ftype: int, payload: bytes) -> None:
    blob = HEADER.pack(ftype, len(payload)) + payload
    if OPTS.fragment and len(blob) > 8:
        pos = 0
        limit = min(len(blob), FRAGMENT_PREFIX)
        while pos < limit:
            n = random.randint(1, max(1, min(limit - pos, 7)))
            sock.sendall(blob[pos : pos + n])
            pos += n
        if pos < len(blob):
            sock.sendall(blob[pos:])
    else:
        sock.sendall(blob)


#: §4.2 of the amendment — level and message frames are never dropped.
DROPPABLE = {FRAME_DATA, FRAME_BAND}

_TYPE_NAMES = {FRAME_DATA: "DATA", FRAME_BAND: "BAND",
               FRAME_LEVEL: "LEVEL", FRAME_BAND_LEVEL: "BAND_LEVEL"}


def broadcast_stream(ftype: int, payload: bytes) -> None:
    """Send to every stream client. With --drop, some are discarded on purpose.

    Important: the STATE sample counters have already advanced even for a frame
    that is dropped. That is how the client spots loss via a start_index jump (§1.3).
    """
    if OPTS.drop and ftype in DROPPABLE and random.random() < OPTS.drop:
        with STATE.lock:
            STATE.frames_dropped += 1
            name = _TYPE_NAMES.get(ftype, str(ftype))
            STATE.frames_dropped_by_type[name] = STATE.frames_dropped_by_type.get(name, 0) + 1
        return
    for client in list(STATE.stream_clients):
        try:
            client.send_frame(ftype, payload)
        except OSError:
            pass


def indexed(key: tuple, count: int) -> bytes:
    """Encode this stream's start_index as u64 (v4); empty bytes on v3."""
    start = STATE.next_index(key, count)
    return U64.pack(start) if STATE.protocol_version >= 4 else b""


def broadcast_event(event: dict) -> None:
    line = (json.dumps(event) + "\n").encode()
    blob = json.dumps(event).encode()
    for client in list(STATE.control_clients):
        try:
            client.send_raw(line)
        except OSError:
            pass
    broadcast_stream(FRAME_MSG, blob)


# ── Signal generation ───────────────────────────────────────────────
class Generator(threading.Thread):
    """Produces LEVEL / BAND_LEVEL / DATA frames while a scan runs."""

    def __init__(self) -> None:
        super().__init__(name="pislm-sim-gen", daemon=True)
        self.stop_flag = threading.Event()
        self.block = 0

    def run(self) -> None:
        rate = STATE.effective_rate()
        level_rate = float(STATE.level["output_rate"])
        block_seconds = 0.1
        block_n = int(rate * block_seconds)
        rings = {d["index"]: [] for d in STATE.devices}
        ring_max = int(STATE.storage["buffer_seconds"] / block_seconds)

        while not self.stop_flag.is_set():
            t0 = time.monotonic()
            phase = self.block * block_n

            for dev in STATE.devices:
                chans = dev["channels"]
                t = (np.arange(block_n) + phase) / rate
                cols = []
                for k, ch in enumerate(chans):
                    # 1 kHz tone + a low-frequency impact component + noise
                    sig = 0.02 * np.sin(2 * np.pi * 1000 * t + k)
                    sig += 0.05 * np.sin(2 * np.pi * 63 * t) * np.exp(-((t % 1.0) * 8) ** 2)
                    sig += 0.002 * np.random.randn(block_n)
                    cols.append(sig)
                # Mix the analog output back in — as if the loudspeaker were
                # in the room. That makes sweep/MLS deconvolution testable.
                excitation = _next_output_block(block_n)
                if excitation is not None:
                    # Through the synthetic room, so what comes back has a real
                    # decay and the reverberation maths has something to find.
                    # The ambient tone is muted meanwhile: it is a constant
                    # floor that the decay can never fall below, which would
                    # flatten the Schroeder curve and give an absurd T.
                    reverberated = _room_filter(dev["index"], excitation, rate)
                    for k in range(len(cols)):
                        cols[k] = cols[k] * 0.001 + reverberated * 20.0
                interleaved = np.stack(cols, axis=1).astype("<f8")
                rings[dev["index"]].append(interleaved)
                if len(rings[dev["index"]]) > ring_max:
                    rings[dev["index"]].pop(0)
                STATE.buffers[dev["index"]] = rings[dev["index"]]

                if STATE.stream_raw:
                    key = ("data", dev["index"])
                    broadcast_stream(
                        FRAME_DATA,
                        U32.pack(dev["index"]) + indexed(key, block_n)
                        + interleaved.ravel().tobytes(),
                    )

            n_level = max(1, int(level_rate * block_seconds))
            if STATE.level.get("enabled"):
                for ch in STATE.all_channels():
                    levels = 60 + 8 * np.sin(2 * np.pi * 0.2 * (phase / rate) + ch)
                    arr = (levels + np.random.randn(n_level) * 0.4).astype("<f8")
                    broadcast_stream(
                        FRAME_LEVEL,
                        U32.pack(ch) + indexed(("level", ch), n_level) + arr.tobytes(),
                    )

            if STATE.bands.get("enabled") and STATE.bands.get("output") == "level":
                table = STATE.band_table()
                for entry in table:
                    for band in entry["bands"]:
                        for ch in entry["channels"]:
                            arr = (
                                45 + 10 * math.log10(max(band["center"], 1) / 1000 + 1.1)
                                + np.random.randn(n_level) * 0.5
                            ).astype("<f8")
                            broadcast_stream(
                                FRAME_BAND_LEVEL,
                                U32.pack(band["index"]) + U32.pack(ch)
                                + indexed(("band_level", ch, band["index"]), n_level)
                                + arr.tobytes(),
                            )

            # Inject overload events (§3 of the amendment)
            if OPTS.overload and random.random() < OPTS.overload:
                ch = random.choice(STATE.all_channels())
                dev = STATE.channel_map()[ch]["device"]
                samples = random.randint(20, 300)
                with STATE.lock:
                    STATE.overload_counts[ch] = STATE.overload_counts.get(ch, 0) + samples
                    idx = STATE.counters.get(("data", dev), 0)
                broadcast_event({
                    "type": "event", "event": "overload",
                    "device": dev, "channel": ch,
                    "start_index": idx, "samples": samples,
                    "peak": 5.02, "units": "V", "full_scale": 5.0,
                })

            self.block += 1
            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, block_seconds - elapsed))


GENERATOR: Generator | None = None


def start_scan() -> dict:
    global GENERATOR
    with STATE.lock:
        if not STATE.running:
            STATE.running = True
            STATE.reset_counters()  # indices reset to 0 on every start (§1.3)
            GENERATOR = Generator()
            GENERATOR.start()
        body = STATE.snapshot(include_band_table=True)
    broadcast_event({"type": "event", "event": "started", **body})
    return body


def stop_scan() -> dict:
    global GENERATOR
    with STATE.lock:
        was = STATE.running
        STATE.running = False
    if GENERATOR is not None:
        GENERATOR.stop_flag.set()
        GENERATOR.join(timeout=2.0)
        GENERATOR = None
    if was:
        broadcast_event({"type": "event", "event": "stopped"})
    return {"running": False}


# ── Command handling ────────────────────────────────────────────────
NEEDS_STOP = {
    "set_sensitivity", "set_iepe", "set_sample_rate", "set_channels", "set_trigger",
    "set_options", "set_bands", "set_weighting", "set_level", "set_storage",
    "set_dsp", "set_resample", "save_config", "calibration_write", "test_signals_write",
}


def handle_command(msg: dict) -> dict:
    cmd = msg.get("cmd")
    if not isinstance(cmd, str):
        return {"ok": False, "error": "missing cmd"}

    if cmd in NEEDS_STOP and STATE.running:
        return {"ok": False, "error": 'a scan is active; send "stop" first'}

    with STATE.lock:
        if cmd == "ping":
            return {"ok": True, "result": {"pong": True}}
        if cmd == "get_config":
            return {"ok": True, "result": STATE.snapshot()}
        if cmd == "status":
            return {"ok": True, "result": {
                "running": STATE.running,
                "devices": [
                    {"index": d["index"], "type": d["type"], "running": STATE.running,
                     "actual_rate": d["actual_rate"], "effective_rate": STATE.effective_rate()}
                    for d in STATE.devices
                ],
            }}
        if cmd == "info":
            return {"ok": True, "result": {
                "devices": STATE.devices, "channel_map": STATE.channel_map(),
                "num_channels": len(STATE.all_channels()),
            }}
        if cmd == "get_clock":
            return {"ok": True, "result": {
                "requested_rate": STATE.sample_rate,
                "devices": [{"index": d["index"], "type": d["type"],
                             "actual_rate": d["actual_rate"]} for d in STATE.devices],
                "synchronized": False,
            }}
        if cmd == "get_sensitivity":
            ch = int(msg["channel"])
            return {"ok": True, "result": {"channel": ch, "sensitivity": STATE.sensitivity[ch]}}
        if cmd == "get_iepe":
            ch = int(msg["channel"])
            return {"ok": True, "result": {"channel": ch, "mode": STATE.iepe[ch]}}
        if cmd == "blink_led":
            return {"ok": True, "result": {"count": msg.get("count", 0),
                                           "devices": [d["index"] for d in STATE.devices]}}

        if cmd == "set_sensitivity":
            ch, val = int(msg["channel"]), float(msg["value"])
            STATE.sensitivity[ch] = val
            return {"ok": True, "result": {"channel": ch, "sensitivity": val,
                                           "units": STATE.units(ch)}}
        if cmd == "set_iepe":
            ch = int(msg["channel"])
            mode = msg["mode"]
            mode = 1 if mode in (1, "1", "on", True) else 0
            STATE.iepe[ch] = mode
            return {"ok": True, "result": {"channel": ch, "mode": mode}}
        if cmd == "set_sample_rate":
            rate = float(msg["sample_rate"])
            STATE.sample_rate = rate
            for d in STATE.devices:
                d["actual_rate"] = 51200.0 / max(1, round(51200.0 / rate)) if d["type"] == "mcc172" else rate
            return {"ok": True, "result": {"requested_rate": rate, "devices": STATE.devices}}
        if cmd == "set_options":
            if "stream_raw" in msg:
                STATE.stream_raw = bool(msg["stream_raw"])
            return {"ok": True, "result": {"stream_raw": STATE.stream_raw}}
        if cmd == "set_bands":
            for key in ("enabled", "output", "f_min", "f_max", "fraction", "order", "margin"):
                if key in msg:
                    STATE.bands[key] = msg[key]
            return {"ok": True, "result": {**STATE.bands, "band_table": STATE.band_table()}}
        if cmd == "set_weighting":
            if "frequency" in msg:
                STATE.weighting["frequency"] = msg["frequency"]
            if "time" in msg:
                STATE.weighting["time"] = msg["time"]
            return {"ok": True, "result": dict(STATE.weighting)}
        if cmd == "set_level":
            for key, dst in (("enabled", "enabled"), ("output_rate", "output_rate")):
                if key in msg:
                    STATE.level[dst] = msg[key]
            return {"ok": True, "result": dict(STATE.level)}
        if cmd == "set_storage":
            STATE.storage["buffer_seconds"] = float(msg["buffer_seconds"])
            return {"ok": True, "result": dict(STATE.storage)}
        if cmd == "set_resample":
            for key in ("enabled", "output_rate", "taps", "phases"):
                if key in msg:
                    STATE.resample[key] = msg[key]
            STATE.resample["active"] = bool(STATE.resample.get("enabled"))
            return {"ok": True, "result": dict(STATE.resample)}
        if cmd == "set_trigger":
            STATE.trigger["enabled"] = bool(msg.get("enable", False))
            for key in ("source", "gpio_pin", "pulse_ms"):
                if key in msg:
                    STATE.trigger[key] = msg[key]
            return {"ok": True, "result": dict(STATE.trigger)}
        if cmd == "set_dsp":
            return {"ok": True, "result": {"workers_configured": msg.get("workers", -1),
                                           "cpu_count": 4, "note": "simulated"}}
        if cmd == "save_config":
            return {"ok": True, "result": {"path": msg.get("path", "config.ini"),
                                           "saved": ["sensitivity"],
                                           "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                           "sensitivity_mv_per_unit": dict(STATE.sensitivity)}}

    if cmd == "start":
        return {"ok": True, "result": start_scan()}
    if cmd == "stop":
        return {"ok": True, "result": stop_scan()}

    if cmd == "calibrate":
        ch = int(msg["channel"])
        target = float(msg.get("level_db", 94.0))
        with STATE.lock:
            old = STATE.sensitivity[ch]
            measured = 117.02
            new = round(old * (10 ** ((measured - target) / 20.0)), 3)
            if msg.get("apply", True):
                STATE.sensitivity[ch] = new
        return {"ok": True, "result": {
            "channel": ch, "device": 0, "target_level_db": target,
            "measured_level_db": measured, "measured_units": "dB re 20uPa",
            "old_sensitivity": old, "new_sensitivity": new,
            "change_db": round(measured - target, 3), "seconds": msg.get("seconds", 3.0),
            "freq": msg.get("freq", 1000.0), "bandpass": msg.get("bandpass", True),
            "applied": bool(msg.get("apply", True)), "restarted": False,
            "units": STATE.units(ch), "saved": False, "note": "simulated",
        }}

    if cmd == "get_metrics":
        chans = msg.get("channels") or STATE.all_channels()
        seconds = float(msg.get("seconds", 10.0))
        result = {"requested_seconds": seconds, "channels": {}}
        for ch in chans:
            entry = {
                "Leq": round(random.uniform(60, 80), 2),
                "Lmax": round(random.uniform(80, 95), 2),
                "Lmin": round(random.uniform(40, 55), 2),
                "Lpeak": round(random.uniform(95, 110), 2),
                "LN": {"L10": 78.9, "L50": 72.5, "L90": 64.1},
                "weighting": msg.get("weighting", STATE.weighting["frequency"]),
                "time_weighting": msg.get("time_weighting", STATE.weighting["time"]),
                "units": STATE.units(ch), "calibrated": STATE.units(ch) == "Pa",
                "device": STATE.channel_map()[ch]["device"],
                "window_seconds": seconds,
                "n_samples": int(seconds * STATE.effective_rate()),
            }
            if msg.get("include_bands"):
                entry["bands"] = [
                    {"index": b["index"], "center": b["center"],
                     "Leq": round(random.uniform(40, 70), 2)}
                    for b in STATE.band_table()[0]["bands"]
                ]
            result["channels"][str(ch)] = entry
        return {"ok": True, "result": result}

    if cmd in ("set_output", "output_start", "output_stop", "output_status"):
        return handle_output(cmd, msg)

    if cmd == "get_raw":
        if not STATE.stream_clients:
            return {"ok": False, "error": "no stream client connected"}
        # The real Pi refuses a window longer than the ring buffer rather than
        # quietly returning a short one — a caller handed less than it asked
        # for would analyse the wrong span without noticing. Checked here, in
        # the envelope, because `start_dump` returns only the result body.
        asked = msg.get("seconds")
        if asked is not None and float(asked) > float(STATE.storage["buffer_seconds"]):
            return {"ok": False, "error": "not enough data buffered"}
        return {"ok": True, "result": start_dump(msg)}

    return {"ok": False, "error": f"unknown command: {cmd}"}


def handle_output(cmd: str, msg: dict) -> dict:
    """Analog output commands (PROTOCOL_OUTPUT.md).

    The simulator actually synthesises the waveform and mixes it into the
    acquired signal, so sweep/MLS deconvolution can be exercised end to end
    without hardware.
    """
    from pislm.excitation import ExcitationSettings, Signal, generate

    with STATE.lock:
        if cmd == "output_status":
            return {"ok": True, "result": _output_status()}

        if cmd == "set_output":
            if STATE.output["running"]:
                return {"ok": False, "error": "output is running; send output_stop first"}
            level = float(msg.get("level_dbfs", -20.0))
            if level > 0:
                return {"ok": False, "error": "level_dbfs must be <= 0"}
            try:
                signal = Signal(msg.get("signal", "sweep"))
            except ValueError:
                return {"ok": False, "error": f"unknown signal: {msg.get('signal')!r}"}
            # Defaults are the ones in PROTOCOL.md §10, which differ from the
            # client's own — the simulator stands in for the Pi, so it has to
            # behave like the spec rather than like the client.
            channel = int(msg.get("channel", 0))
            if channel != 0:
                return {"ok": False, "error": "channel must be 0 (the only output)"}
            order = int(msg.get("mls_order", 16))
            if order not in (8, 10, 12, 14, 15, 16, 17, 18):
                return {"ok": False, "error": f"unsupported mls_order: {order}"}
            settings = ExcitationSettings(
                signal=signal,
                seconds=float(msg.get("seconds", 3.0)),
                level_dbfs=level,
                f_min=float(msg.get("f_min", 50.0)),
                f_max=float(msg.get("f_max", 5000.0)),
                mls_order=order,
                channel=channel,
                tail_seconds=float(msg.get("tail_seconds", 1.0)),
                repeats=int(msg.get("repeats", 1)),
            )
            rate = STATE.effective_rate()
            waveform = generate(settings, rate, seed=1234)
            STATE.output_settings = settings.to_command()
            STATE.output_signal = waveform
            STATE.output["signal"] = signal.value
            return {"ok": True, "result": {
                **settings.to_command(),
                "output_rate": rate,
                "total_samples": int(waveform.size),
                "total_seconds": waveform.size / rate,
                "device": STATE.output["device"],
            }}

        if cmd == "output_start":
            if STATE.output_signal is None:
                return {"ok": False, "error": "no signal configured; send set_output first"}
            if STATE.output["running"]:
                return {"ok": False, "error": "output is already running"}
            device = STATE.output["device"]
            # start_index is on the DATA grid so the client can locate the
            # excitation inside the recording to the sample.
            STATE.output_start_index = (
                STATE.counters.get(("data", device), 0) if STATE.running else None
            )
            STATE.output_position = 0
            STATE.output["running"] = True
            rate = STATE.effective_rate()
            result = {
                "running": True,
                "signal": STATE.output["signal"],
                "total_seconds": STATE.output_signal.size / rate,
                "start_index": STATE.output_start_index,
                "device": device,
            }
        else:  # output_stop
            if not STATE.output["running"]:
                return {"ok": True, "result": {"running": False, "samples_played": 0}}
            STATE.output["running"] = False
            played = STATE.output_position
            result = {"running": False, "samples_played": int(played)}

    if cmd == "output_start":
        broadcast_event({
            "type": "event", "event": "output_started",
            "signal": result["signal"], "start_index": result["start_index"],
            "total_seconds": result["total_seconds"],
            "device": result["device"], "channel": STATE.output_settings.get("channel", 0),
        })
    else:
        broadcast_event({
            "type": "event", "event": "output_finished",
            "samples_played": result["samples_played"],
            "end_index": STATE.counters.get(("data", STATE.output["device"]), 0),
            "completed": False,
        })
    return {"ok": True, "result": result}


def _output_status() -> dict:
    rate = STATE.effective_rate()
    total = STATE.output_signal.size if STATE.output_signal is not None else 0
    played = STATE.output_position
    return {
        "running": STATE.output["running"],
        "signal": STATE.output["signal"],
        "elapsed_seconds": played / rate if rate else 0.0,
        "remaining_seconds": max(0, total - played) / rate if rate else 0.0,
        "start_index": STATE.output_start_index,
        "device": STATE.output["device"],
        "channel": (STATE.output_settings or {}).get("channel", 0),
    }


#: Reverberation time of the simulator's synthetic room, seconds.
#: Without a room the excitation comes back dry and the measured T is
#: meaningless — this makes `--demo` actually demonstrate reverberation.
SIM_ROOM_T60 = 0.6

#: Mutually prime comb delays (samples). A parallel bank of feedback combs is
#: the classic Schroeder reverberator: each comb contributes an impulse train
#: decaying at the same rate, and together they fill the spectrum.
#: A single one-pole would also decay exponentially, but it is a ~2 Hz lowpass,
#: so nothing survives in the analysis bands and the measured T is nonsense.
SIM_COMB_DELAYS = (1557, 1617, 1491, 1422, 1277, 1116)


class _CombReverb:
    """Streaming parallel feedback combs with a known T60."""

    def __init__(self, rate: float, t60: float) -> None:
        self.buffers = [np.zeros(d) for d in SIM_COMB_DELAYS]
        self.positions = [0] * len(SIM_COMB_DELAYS)
        # y[n] = x[n] + g·y[n−D] decays 60 dB after t60 seconds when
        # g = 10^(−3D/(t60·fs)).
        self.gains = [
            10.0 ** (-3.0 * d / max(1e-9, t60 * rate)) for d in SIM_COMB_DELAYS
        ]

    def process(self, block: np.ndarray) -> np.ndarray:
        out = np.zeros_like(block)
        for index, (buf, gain) in enumerate(zip(self.buffers, self.gains)):
            size = buf.size
            pos = self.positions[index]
            for i in range(block.size):
                delayed = buf[pos]
                value = block[i] + gain * delayed
                buf[pos] = value
                out[i] += delayed
                pos += 1
                if pos >= size:
                    pos = 0
            self.positions[index] = pos
        return out / len(self.buffers)


def _room_filter(device: int, block: np.ndarray, rate: float) -> np.ndarray:
    """Run one block through the synthetic room, carrying state between blocks."""
    if block.size == 0:
        return block
    room = STATE.room_state.get(device)
    if room is None:
        room = _CombReverb(rate, SIM_ROOM_T60)
        STATE.room_state[device] = room
    return room.process(block)


def _next_output_block(count: int) -> np.ndarray | None:
    """Take the next `count` samples of the excitation, or None when idle.

    Emits `output_finished` when the waveform runs out.
    """
    finished = False
    with STATE.lock:
        if not STATE.output["running"] or STATE.output_signal is None:
            return None
        start = STATE.output_position
        block = STATE.output_signal[start : start + count]
        STATE.output_position = start + block.size
        if STATE.output_position >= STATE.output_signal.size:
            STATE.output["running"] = False
            finished = True
            played = STATE.output_position
            device = STATE.output["device"]
    if block.size < count:
        block = np.concatenate([block, np.zeros(count - block.size)])
    if finished:
        broadcast_event({
            "type": "event", "event": "output_finished",
            "samples_played": int(played),
            "end_index": STATE.counters.get(("data", device), 0),
            "completed": True,
        })
    return block


def start_dump(msg: dict) -> dict:
    with STATE.lock:
        STATE.dump_counter += 1
        dump_id = STATE.dump_counter
        seconds = float(msg.get("seconds") or STATE.storage["buffer_seconds"])
        want = msg.get("devices") or [d["index"] for d in STATE.devices]
        rate = STATE.effective_rate()

    chunk_samples = 65536  # counted in interleaved samples
    meta_devices, payloads = [], {}
    for dev in STATE.devices:
        if dev["index"] not in want:
            continue
        nch = len(dev["channels"])
        n = int(seconds * rate)
        blocks = STATE.buffers.get(dev["index"])
        if blocks:
            data = np.concatenate(blocks, axis=0)[-n:]
        else:
            data = np.zeros((n, nch), dtype="<f8")
        flat = data.ravel()
        total_chunks = max(1, math.ceil(flat.size / chunk_samples))
        # Dump start index = samples produced so far − the slice length (§1.2).
        # It must share the DATA grid so it lines up with the live stream.
        with STATE.lock:
            produced = STATE.counters.get(("data", dev["index"]), data.shape[0])
        dump_start = max(0, produced - data.shape[0])
        payloads[dev["index"]] = (flat, total_chunks, dump_start, nch)
        meta_devices.append({
            "device": dev["index"], "channels": list(dev["channels"]), "num_channels": nch,
            "sample_rate": rate, "samples_per_channel": int(data.shape[0]),
            "seconds": data.shape[0] / rate, "total_chunks": total_chunks,
            "start_index": dump_start,
        })

    result = {"dump_id": dump_id, "chunk_samples": chunk_samples,
              "units": "Pa", "devices": meta_devices}

    def pump() -> None:
        for device, (flat, total, dump_start, nch) in payloads.items():
            for i in range(total):
                part = flat[i * chunk_samples : (i + 1) * chunk_samples]
                # Chunk start = dump start + per-channel samples in earlier chunks
                chunk_start = dump_start + (i * chunk_samples) // nch
                index = U64.pack(chunk_start) if STATE.protocol_version >= 4 else b""
                payload = (
                    U32.pack(dump_id) + U32.pack(device) + U32.pack(i)
                    + U32.pack(1 if i == total - 1 else 0) + index + part.tobytes()
                )
                # RAW_DUMP is never dropped (§2.5), so send straight to clients
                for client in list(STATE.stream_clients):
                    try:
                        client.send_frame(FRAME_RAW_DUMP, payload)
                    except OSError:
                        pass
                time.sleep(0.001)

    threading.Thread(target=pump, name="pislm-sim-dump", daemon=True).start()
    return result


# ── Server handlers ─────────────────────────────────────────────────
class ControlHandler(socketserver.BaseRequestHandler):
    def setup(self) -> None:
        self.lock = threading.Lock()
        STATE.control_clients.append(self)

    def finish(self) -> None:
        try:
            STATE.control_clients.remove(self)
        except ValueError:
            pass

    def send_raw(self, data: bytes) -> None:
        with self.lock:
            self.request.sendall(data)

    def handle(self) -> None:
        log.info("control client connected: %s", self.client_address)
        handshake = {"type": "handshake", **STATE.snapshot()}
        self.send_raw((json.dumps(handshake) + "\n").encode())

        buf = bytearray()
        try:
            while True:
                chunk = self.request.recv(65536)
                if not chunk:
                    break
                buf.extend(chunk)
                batch = []
                while b"\n" in buf:
                    idx = buf.index(b"\n")
                    line = bytes(buf[:idx]).strip()
                    del buf[: idx + 1]
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    response = {"type": "response", "cmd": msg.get("cmd")}
                    if "id" in msg:
                        response["id"] = msg["id"]
                    try:
                        response.update(handle_command(msg))
                    except Exception as exc:  # noqa: BLE001
                        response.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                    batch.append(response)
                if OPTS.shuffle and len(batch) > 1:
                    random.shuffle(batch)  # check that id matching really works
                if batch:
                    if OPTS.jitter:
                        time.sleep(random.uniform(0, OPTS.jitter))
                    # Concatenate several lines into one write (§9.2)
                    self.send_raw(b"".join((json.dumps(r) + "\n").encode() for r in batch))
        except OSError:
            pass
        finally:
            log.info("control client gone: %s", self.client_address)


class StreamHandler(socketserver.BaseRequestHandler):
    """One stream client.

    Per §6 it keeps a **send queue with a dedicated sender thread**. Without
    that, `sendall` blocks the generator thread when the socket buffer fills and
    the whole server stalls — which really deadlocked the GUI tests. The real Pi
    also states that a slow reader must not stall acquisition.
    """

    #: Queue depth for droppable frames (max_queue_blocks in config.ini)
    QUEUE_LIMIT = 512

    def setup(self) -> None:
        self.lock = threading.Lock()
        self._buffer: deque = deque()
        self._cv = threading.Condition()
        self._alive = True
        self._sender = threading.Thread(target=self._send_loop, daemon=True)
        self._sender.start()
        STATE.stream_clients.append(self)

    def finish(self) -> None:
        with self._cv:
            self._alive = False
            self._cv.notify_all()
        try:
            STATE.stream_clients.remove(self)
        except ValueError:
            pass

    def send_frame(self, ftype: int, payload: bytes) -> None:
        """Only enqueues — never blocks.

        On overflow it discards **the oldest droppable (DATA/BAND) frame only**.
        Dropping the head regardless of type would remove LEVEL/BAND_LEVEL and
        break the §6 guarantee that levels are practically never dropped.
        """
        if not self._alive:
            return
        with self._cv:
            if len(self._buffer) >= self.QUEUE_LIMIT:
                victim = next(
                    (i for i, (t, _) in enumerate(self._buffer) if t in DROPPABLE), None
                )
                if victim is not None:
                    dropped_type, _ = self._buffer[victim]
                    del self._buffer[victim]
                    with STATE.lock:
                        STATE.frames_dropped += 1
                        name = _TYPE_NAMES.get(dropped_type, str(dropped_type))
                        STATE.frames_dropped_by_type[name] = (
                            STATE.frames_dropped_by_type.get(name, 0) + 1
                        )
                # With nothing droppable, overrun the queue to protect reliable frames
            self._buffer.append((ftype, payload))
            self._cv.notify()

    def _send_loop(self) -> None:
        while True:
            with self._cv:
                while self._alive and not self._buffer:
                    self._cv.wait(0.5)
                if not self._alive and not self._buffer:
                    break
                if not self._buffer:
                    continue
                ftype, payload = self._buffer.popleft()
            try:
                with self.lock:
                    send_frame(self.request, ftype, payload)
            except OSError:
                # The socket died. Leave the roster at once, otherwise frames keep
                # piling up for a client that is already gone and the drop counter
                # climbs spuriously, polluting the live clients' statistics.
                self._alive = False
                try:
                    STATE.stream_clients.remove(self)
                except ValueError:
                    pass
                break

    def handle(self) -> None:
        log.info("stream client connected: %s", self.client_address)
        self.send_frame(FRAME_MSG, json.dumps({"type": "handshake", **STATE.snapshot()}).encode())
        try:
            while True:
                # Bytes sent to this port are ignored (§2); only detect close.
                if not self.request.recv(4096):
                    break
        except OSError:
            pass
        finally:
            log.info("stream client gone: %s", self.client_address)


class Threaded(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(control_port: int, stream_port: int, host: str = "127.0.0.1"):
    # The simulator is a *fake* Pi running on this machine, so it binds to
    # loopback. Pointing it at the real Pi's address cannot work — nothing
    # here can bind an address the laptop does not own — and it takes the
    # whole test suite down with it. To talk to real hardware, leave the
    # simulator alone and use `python run_gui.py --host 192.168.50.1`.
    ctl = Threaded((host, control_port), ControlHandler)
    stm = Threaded((host, stream_port), StreamHandler)
    threading.Thread(target=ctl.serve_forever, daemon=True).start()
    threading.Thread(target=stm.serve_forever, daemon=True).start()
    return ctl, stm


def main() -> None:
    parser = argparse.ArgumentParser(description="pislm protocol simulator")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address; 0.0.0.0 to accept from other machines")
    parser.add_argument("--control", type=int, default=5000)
    parser.add_argument("--stream", type=int, default=5001)
    parser.add_argument("--autostart", action="store_true", help="start scanning immediately")
    parser.add_argument("--stream-raw", action="store_true", help="also stream raw DATA")
    parser.add_argument("--fragment", action="store_true", help="split frames into small writes")
    parser.add_argument("--shuffle", action="store_true", help="shuffle response order")
    parser.add_argument("--jitter", type=float, default=0.0, help="max response delay, seconds")
    parser.add_argument("--protocol", type=int, choices=(3, 4), default=4,
                        help="3 = legacy layout, 4 = with start_index (default)")
    parser.add_argument("--drop", type=float, default=0.0,
                        help="drop DATA/BAND frames at this probability (e.g. 0.02)")
    parser.add_argument("--overload", type=float, default=0.0,
                        help="overload event probability per block (e.g. 0.05)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    OPTS.fragment, OPTS.shuffle, OPTS.jitter = args.fragment, args.shuffle, args.jitter
    OPTS.drop, OPTS.overload = args.drop, args.overload
    STATE.stream_raw = args.stream_raw
    STATE.protocol_version = args.protocol

    serve(args.control, args.stream, args.host)
    log.info("simulator on %s control=%d stream=%d protocol=pislm/%d",
             args.host, args.control, args.stream, args.protocol)
    if args.drop:
        log.warning("deliberate frame loss %.1f%% enabled — for gap detection tests", args.drop * 100)
    if args.autostart:
        start_scan()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop_scan()


if __name__ == "__main__":
    main()
