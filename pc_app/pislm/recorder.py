"""Raw waveform WAV recording — DATA frames streamed straight to disk.

Two things matter in the design:

1. **Incremental writing.** A long measurement cannot be buffered in memory.
   The header is written with placeholder sizes and patched on close.
2. **Gaps are filled with silence.** When a frame is lost (§6), simply
   concatenating makes the file **shorter** at that point — invisible in the
   file itself, and every impact after it shifts earlier. `start_index` gives
   the exact number of missing samples, so zeros preserve the time axis and
   the gap positions are recorded in the sidecar.

Calibration state (Pa/V, sensitivity) cannot live in a WAV file, so it goes
into a `.json` sidecar of the same name. Without it levels cannot be restored.
"""

from __future__ import annotations

import json
import logging
import struct
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from .frames import NO_INDEX, DataFrame

log = logging.getLogger("pislm.recorder")

#: WAV format codes
WAVE_FORMAT_PCM = 1
WAVE_FORMAT_IEEE_FLOAT = 3

FORMATS = {
    "float32": (WAVE_FORMAT_IEEE_FLOAT, 32),
    "int24": (WAVE_FORMAT_PCM, 24),
    "int16": (WAVE_FORMAT_PCM, 16),
}


class WavWriter:
    """A minimal WAV writer that supports incremental writing.

    The standard `wave` module cannot write float formats and
    `scipy.io.wavfile` only writes whole arrays. Neither suits long streaming.
    """

    def __init__(
        self,
        path: str | Path,
        channels: int,
        sample_rate: float,
        fmt: str = "float32",
        full_scale: float = 1.0,
    ) -> None:
        if fmt not in FORMATS:
            raise ValueError(f"unsupported format: {fmt} (choose from {list(FORMATS)})")
        self.path = Path(path)
        self.channels = channels
        self.sample_rate = int(round(sample_rate))
        self.fmt = fmt
        #: When writing PCM, ±full_scale maps to ±full-scale integer
        self.full_scale = full_scale
        self.frames_written = 0
        self.clipped = 0

        self._format_code, self._bits = FORMATS[fmt]
        self._block_align = channels * self._bits // 8
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("wb")
        self._write_header()

    # ── Header ──
    def _write_header(self) -> None:
        byte_rate = self.sample_rate * self._block_align
        self._file.write(b"RIFF")
        self._file.write(struct.pack("<I", 0))  # size patched in close()
        self._file.write(b"WAVE")
        self._file.write(b"fmt ")
        self._file.write(struct.pack("<I", 16))
        self._file.write(
            struct.pack(
                "<HHIIHH",
                self._format_code,
                self.channels,
                self.sample_rate,
                byte_rate,
                self._block_align,
                self._bits,
            )
        )
        self._file.write(b"data")
        self._file.write(struct.pack("<I", 0))  # patched in close() too
        self._data_start = self._file.tell()

    # ── Writing ──
    def write(self, samples: np.ndarray) -> None:
        """Write an (n, channels) array or a 1-D interleaved array."""
        block = np.asarray(samples, dtype=np.float64)
        if block.ndim == 1:
            if block.size % self.channels:
                raise ValueError("interleaved length is not divisible by the channel count")
            block = block.reshape(-1, self.channels)
        elif block.shape[1] != self.channels:
            raise ValueError(f"channel count mismatch: {block.shape[1]} != {self.channels}")

        self._file.write(self._encode(block))
        self.frames_written += block.shape[0]

    def _encode(self, block: np.ndarray) -> bytes:
        if self.fmt == "float32":
            return block.astype("<f4").tobytes()

        scaled = block / self.full_scale
        over = int(np.count_nonzero(np.abs(scaled) > 1.0))
        if over:
            self.clipped += over
        scaled = np.clip(scaled, -1.0, 1.0)

        if self.fmt == "int16":
            return (scaled * 32767.0).astype("<i2").tobytes()

        # int24 — numpy has no 24-bit type, so take the low 3 bytes of int32
        as_int = (scaled * 8388607.0).astype("<i4")
        raw = as_int.tobytes()
        return b"".join(raw[i : i + 3] for i in range(0, len(raw), 4))

    def write_silence(self, frames: int) -> None:
        """Fill a lost span with zeros so the time axis is preserved."""
        if frames <= 0:
            return
        self.write(np.zeros((frames, self.channels), dtype=np.float64))

    @property
    def seconds(self) -> float:
        return self.frames_written / self.sample_rate if self.sample_rate else 0.0

    def close(self) -> None:
        if self._file.closed:
            return
        data_bytes = self.frames_written * self._block_align
        # RIFF chunks must be even-sized, so pad when necessary
        if data_bytes % 2:
            self._file.write(b"\x00")
        self._file.seek(4)
        self._file.write(struct.pack("<I", 36 + data_bytes + (data_bytes % 2)))
        self._file.seek(self._data_start - 4)
        self._file.write(struct.pack("<I", data_bytes))
        self._file.close()

    def __enter__(self) -> WavWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@dataclass
class RecordingOptions:
    """Recording settings."""

    enabled: bool = False
    directory: str = ""
    fmt: str = "float32"
    #: When writing PCM, ±full_scale maps to full amplitude (Pa or V)
    full_scale: float = 10.0


@dataclass
class DeviceRecording:
    """One device's recording in progress."""

    writer: WavWriter
    device: int
    channels: list[int]
    next_index: int = -1
    gaps: list[dict] = field(default_factory=list)
    missing_samples: int = 0


@dataclass
class DeviceStream:
    """One device's samples, waiting to be lined up with the others."""

    device: int
    channels: list[int]
    #: Absolute DATA-grid index of `pending[0]`
    cursor: int = -1
    pending: "np.ndarray | None" = None
    gaps: list[dict] = field(default_factory=list)
    missing_samples: int = 0


@dataclass
class CombinedRecording:
    """Every channel of every device in a single file.

    All the channels belong to one measurement, so one file is what an
    operator expects to open. The catch is that separate devices only share a
    time base when resampling is active (§4) — interleaving them into one file
    asserts sample alignment, so when resampling is off the sidecar says so
    and the caller warns.
    """

    writer: WavWriter
    #: Global channel number per column, in file order
    channels: list[int]
    streams: dict[int, DeviceStream] = field(default_factory=dict)
    #: Column index in the file for each (device, position within device)
    columns: dict[int, list[int]] = field(default_factory=dict)
    sample_locked: bool = True
    dropped_head: int = 0
    #: Devices that never delivered a frame; their columns are silence
    silent_devices: list[int] = field(default_factory=list)


class WavRecorder:
    """Streams DATA frames into WAV files.

    `feed()` is called from the stream reader thread, so the state is locked.
    One file per device — the devices are not clock-synchronised unless
    resampling is active, so mixing them into one file would fake synchrony.
    """

    def __init__(self, options: RecordingOptions) -> None:
        self.options = options
        self._lock = threading.Lock()
        self._combined: CombinedRecording | None = None
        self._wanted: list[int] | None = None
        self._meta: dict = {}
        self._config = None
        self._label = ""
        self._started_at: datetime | None = None
        self.finished: list[Path] = []

    # ── Lifetime ──
    def start(self, config, label: str = "", channels=None, meta: dict | None = None) -> None:
        """Start recording.

        `config` is a `Handshake`. `channels` are the receiver positions to
        record — one WAV column each, in ascending channel order. Passing None
        records every channel the hardware has.
        """
        self.stop()
        with self._lock:
            self._config = config
            self._label = label or datetime.now().strftime("%H%M%S")
            self._started_at = datetime.now()
            self._wanted = None if channels is None else sorted(set(channels))
            self._meta = dict(meta or {})
            self._combined = None

    @property
    def recording(self) -> bool:
        with self._lock:
            return self._config is not None

    def stop(self) -> list[Path]:
        """Close the file, write the sidecar, return the paths written."""
        with self._lock:
            combined, self._combined = self._combined, None
            config, self._config = self._config, None
            label, started = self._label, self._started_at

        if combined is None:
            return []
        self._flush(combined, final=True)   # anything still lined up
        combined.writer.close()
        self._write_sidecar(combined, config, label, started)
        written = [combined.writer.path]
        self.finished.extend(written)
        return written

    # ── Frame intake ──
    def feed(self, frame) -> None:
        if not isinstance(frame, DataFrame):
            return
        with self._lock:
            if self._config is None:
                return
            if self._combined is None:
                self._combined = self._open()
                if self._combined is None:
                    return
            combined = self._combined
            stream = combined.streams.get(frame.device)
            if stream is None:
                return   # a device that was not in the handshake

            width = len(stream.channels)
            if frame.interleaved.size % width:
                log.warning(
                    "device %d: interleaved length does not match channel count",
                    frame.device,
                )
                return
            block = frame.interleaved.reshape(-1, width)

            if frame.start_index != NO_INDEX:
                if stream.cursor < 0 and stream.pending is None:
                    stream.cursor = frame.start_index
                else:
                    expected = stream.cursor + (
                        0 if stream.pending is None else len(stream.pending)
                    )
                    if frame.start_index > expected:
                        # Zero-fill this device's own gap so its timeline —
                        # and therefore its alignment with the others —
                        # survives the loss.
                        gap = frame.start_index - expected
                        block = np.vstack([np.zeros((gap, width)), block])
                        stream.missing_samples += gap
                        stream.gaps.append({
                            "at_sample": expected,
                            "missing_samples": gap,
                            "seconds": gap / combined.writer.sample_rate,
                        })
                    elif frame.start_index < expected:
                        log.warning(
                            "device %d: index went backwards — dropping frame",
                            frame.device,
                        )
                        return

            stream.pending = (
                block if stream.pending is None
                else np.vstack([stream.pending, block])
            )
            self._flush(combined)

    def _flush(self, combined: CombinedRecording, final: bool = False) -> None:
        """Write every row for which all devices have samples.

        Devices are read by independent threads and their frames arrive at
        different times, so the file can only advance to the point the
        slowest device has reached. Writing a device as soon as it arrives
        would stagger the columns against each other.

        `final` relaxes that at the end of a recording: a device that never
        sent anything must not take the whole file down with it, so its
        columns are left at zero and the sidecar names it.
        """
        streams = [s for s in combined.streams.values() if s.pending is not None]
        if not streams:
            return
        if not final and len(streams) != len(combined.streams):
            return
        if final:
            silent = [d for d, s in combined.streams.items() if s.pending is None]
            if silent:
                combined.silent_devices = silent
                log.warning("devices %s sent nothing — their columns are silent", silent)

        # Line them all up on the latest common start. Devices rarely begin on
        # exactly the same index, and the difference is a real offset, not
        # something to ignore — so trim rather than assume.
        base = max(s.cursor for s in streams)
        for stream in streams:
            skip = base - stream.cursor
            if skip > 0:
                stream.pending = stream.pending[skip:]
                stream.cursor = base
                combined.dropped_head = max(combined.dropped_head, skip)

        rows = min(len(s.pending) for s in streams)
        if rows <= 0:
            return

        out = np.zeros((rows, len(combined.channels)))
        for stream in streams:
            for position, column in combined.columns[stream.device]:
                out[:, column] = stream.pending[:rows, position]
            stream.pending = stream.pending[rows:]
            stream.cursor += rows

        try:
            combined.writer.write(out)
        except OSError:
            log.exception("WAV write failed — stopping the recording")
            self._config = None

    # ── Internals ──
    def _open(self) -> CombinedRecording | None:
        config = self._config
        devices = sorted(
            d.get("index") for d in (getattr(config, "devices", []) or [])
            if d.get("index") is not None
        )
        if not devices:
            log.warning("no devices in the handshake, not recording")
            return None

        streams: dict[int, DeviceStream] = {}
        order: list[int] = []
        rates: set[float] = set()
        for device in devices:
            try:
                channels = list(config.device_channels(device))
                rate = float(config.device_rate(device))
            except (KeyError, AttributeError):
                log.warning("no information for device %d, not recording it", device)
                continue
            if not channels or rate <= 0:
                continue
            keep = [c for c in channels
                    if self._wanted is None or c in self._wanted]
            if not keep:
                # No receiver position on this device, so it is not part of
                # the recording at all — and must not be waited for either,
                # or the file would never advance.
                continue
            streams[device] = DeviceStream(device=device, channels=channels)
            order.extend(keep)
            rates.add(rate)
        if not streams:
            log.warning("none of the requested channels exist, not recording")
            return None

        if len(rates) > 1:
            log.warning("devices disagree on sample rate %s — not combining", rates)
            return None

        # One column per receiver position, in global channel order, so the
        # file order matches what the operator sees on screen.
        file_order = sorted(order)
        columns = {
            device: [
                (position, file_order.index(channel))
                for position, channel in enumerate(stream.channels)
                if channel in file_order
            ]
            for device, stream in streams.items()
        }

        stamp = (self._started_at or datetime.now()).strftime("%Y%m%d_%H%M%S")
        name = f"{stamp}_{self._label}.wav" if self._label else f"{stamp}.wav"
        path = Path(self.options.directory or ".") / name
        rate = rates.pop()
        writer = WavWriter(
            path,
            channels=len(file_order),
            sample_rate=rate,
            fmt=self.options.fmt,
            full_scale=self.options.full_scale,
        )
        # A single device is trivially self-consistent. Two only share a time
        # base when the Pi is resampling them onto one grid (§4).
        locked = len(streams) == 1 or bool(getattr(config, "resample_active", False))
        log.info(
            "recording started: %s (%d ch @ %g Hz, %d device(s), sample-locked=%s)",
            path, len(file_order), rate, len(streams), locked,
        )
        return CombinedRecording(
            writer=writer, channels=file_order, streams=streams,
            columns=columns, sample_locked=locked,
        )

    def _write_sidecar(self, recording: CombinedRecording, config, label, started) -> None:
        """What is needed to restore levels. A WAV header cannot carry it."""
        gaps: list[dict] = []
        missing = 0
        for stream in recording.streams.values():
            missing += stream.missing_samples
            for gap in stream.gaps:
                gaps.append({"device": stream.device, **gap})
        gaps.sort(key=lambda g: g["at_sample"])

        info: dict = {
            "wav": recording.writer.path.name,
            "label": label,
            "recorded_at": (started or datetime.now()).isoformat(timespec="seconds"),
            "sample_rate": recording.writer.sample_rate,
            "format": recording.writer.fmt,
            "frames": recording.writer.frames_written,
            "seconds": round(recording.writer.seconds, 3),
            "column_channels": recording.channels,
            # Column 0 is receiver position 1. Operators count from 1 and the
            # wire counts from 0, so spell the mapping out rather than leaving
            # it to be inferred.
            "receiver_positions": {
                str(column): channel + 1
                for column, channel in enumerate(recording.channels)
            },
            "devices": {
                str(d): s.channels for d, s in sorted(recording.streams.items())
            },
            "sample_locked": recording.sample_locked,
            "missing_samples": missing,
            "gaps": gaps[:100],
            "note": (
                "Sample values are the real physical quantity per channel (Pa or V). "
                "Column order is `column_channels`. Lost spans were zero-filled to "
                "preserve the time axis \u2014 see gaps."
            ),
        }
        info.update(self._meta)
        if recording.silent_devices:
            info["silent_devices"] = recording.silent_devices
        if recording.dropped_head:
            # Devices do not start on the same sample; the file begins at the
            # latest of them, and this says how much was trimmed to get there.
            info["trimmed_head_samples"] = recording.dropped_head
        if not recording.sample_locked:
            info["warning"] = (
                "Devices were NOT sample-locked (resampling was off), so they run "
                "on independent clocks. The columns share one file but drift "
                "relative to each other \u2014 do not use this file for "
                "cross-device timing."
            )
        if recording.writer.fmt != "float32":
            info["full_scale"] = recording.writer.full_scale
            info["clipped_samples"] = recording.writer.clipped
        if config is not None:
            try:
                info["units"] = {
                    str(c): config.channel_info(c).units for c in recording.channels
                }
                info["sensitivity_mv_per_unit"] = {
                    str(c): config.channel_info(c).sensitivity_mv_per_unit
                    for c in recording.channels
                }
            except (KeyError, AttributeError):
                pass
            epoch = getattr(config, "epoch", {}) or {}
            if epoch:
                info["epoch"] = epoch
        recording.writer.path.with_suffix(".json").write_text(
            json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def estimate_size(channels: int, sample_rate: float, seconds: float, fmt: str) -> int:
    """Expected file size in bytes, so the user can be warned up front."""
    bits = FORMATS[fmt][1]
    return int(channels * sample_rate * seconds * bits / 8)


def format_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} GB"


def write_dump_wav(path: str | Path, dumps: dict, channels, config=None,
                   fmt: str = "float32", full_scale: float = 1.0,
                   meta: dict | None = None) -> Path | None:
    """Write the exact waveform an analysis ran on.

    Reverberation works from a RAW_DUMP pulled out of the Pi's ring buffer
    rather than from the live DATA stream, so there is nothing for the
    streaming recorder to catch. Writing the dump itself is better anyway: it
    costs no extra bandwidth, and the file is bit-for-bit what produced the
    reverberation time rather than a second recording of roughly the same
    moment.

    Devices are trimmed to their shortest common length. They are already on
    one grid whenever resampling is active; when it is not, the sidecar says
    so, exactly as for a streamed recording.
    """
    wanted = sorted(set(channels))
    columns: list[np.ndarray] = []
    kept: list[int] = []
    for channel in wanted:
        try:
            device = config.device_of(channel) if config is not None else 0
            dump = dumps.get(device)
            if dump is None:
                continue
            columns.append(np.asarray(dump.channel(channel), dtype=np.float64))
            kept.append(channel)
        except (KeyError, AttributeError, IndexError):
            continue
    if not columns:
        return None

    rows = min(column.size for column in columns)
    if rows <= 0:
        return None
    block = np.column_stack([column[:rows] for column in columns])

    first = dumps[config.device_of(kept[0])] if config is not None else next(iter(dumps.values()))
    rate = float(first.sample_rate)
    writer = WavWriter(path, len(kept), rate, fmt=fmt, full_scale=full_scale)
    try:
        writer.write(block)
    finally:
        writer.close()

    info: dict = {
        "wav": writer.path.name,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "sample_rate": writer.sample_rate,
        "format": writer.fmt,
        "frames": writer.frames_written,
        "seconds": round(writer.seconds, 3),
        "column_channels": kept,
        "receiver_positions": {str(i): c + 1 for i, c in enumerate(kept)},
        "source": "raw dump (get_raw)",
        "note": (
            "This is the waveform the analysis actually ran on, pulled from "
            "the Pi's ring buffer — not a separate recording."
        ),
    }
    if writer.fmt != "float32":
        info["full_scale"] = writer.full_scale
        info["clipped_samples"] = writer.clipped
    if config is not None:
        info["sample_locked"] = bool(
            len({config.device_of(c) for c in kept}) == 1
            or getattr(config, "resample_active", False)
        )
        try:
            info["units"] = {str(c): config.channel_info(c).units for c in kept}
            info["sensitivity_mv_per_unit"] = {
                str(c): config.channel_info(c).sensitivity_mv_per_unit for c in kept
            }
        except (KeyError, AttributeError):
            pass
    info.update(meta or {})
    writer.path.with_suffix(".json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return writer.path


def write_wav(path: str | Path, waveform, sample_rate: float,
              fmt: str = "float32", full_scale: float = 1.0) -> Path:
    """Write a mono waveform to a WAV file in one go.

    Used for the excitation signal an operator plays through an external
    amplifier. It is deliberately the same writer the recorder uses, so the
    file that goes out and the files that come back share a format.
    """
    writer = WavWriter(path, 1, sample_rate, fmt=fmt, full_scale=full_scale)
    try:
        writer.write(np.asarray(waveform, dtype=np.float64).reshape(-1, 1))
    finally:
        writer.close()
    return writer.path
