"""Decoded stream frames and the handshake model — PROTOCOL.md §2, §3."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any, Iterator

import numpy as np

from .framing import (
    FRAME_BAND,
    FRAME_BAND_LEVEL,
    FRAME_DATA,
    FRAME_LEVEL,
    FRAME_MSG,
    FRAME_RAW_DUMP,
    U32,
    ProtocolError,
)

_F64 = np.dtype("<f8")  # every binary field is little-endian (see preamble)
_U64 = struct.Struct("<Q")

#: Frame layout version.
#: 3 = original pislm/3. 4 = every frame carries a u64 start_index (§1 of the amendment).
LAYOUT_V3 = 3
LAYOUT_V4 = 4

#: Value used when a frame has no start_index (v3).
NO_INDEX = -1


def layout_of(handshake: dict | None) -> int:
    """Work out the frame layout version from the handshake.

    The final spec only uses the `protocol` string (`pislm/4`). A separate
    `frame_layout` integer was not adopted, but if one ever appears it wins —
    that leaves room for a revision that changes JSON but not the wire layout.
    """
    if not handshake:
        return LAYOUT_V3
    explicit = handshake.get("frame_layout")
    if isinstance(explicit, int):
        return explicit
    protocol = handshake.get("protocol", "")
    if not protocol.startswith("pislm/"):
        return LAYOUT_V3
    try:
        return max(LAYOUT_V3, int(protocol.split("/", 1)[1]))
    except (ValueError, IndexError):
        return LAYOUT_V3


def _f64(payload: bytes, offset: int) -> np.ndarray:
    body = payload[offset:]
    if len(body) % 8:
        raise ProtocolError(f"payload body {len(body)} bytes is not a multiple of 8")
    return np.frombuffer(body, dtype=_F64)


def _split(payload: bytes, fixed: int, layout: int, name: str) -> tuple[int, np.ndarray]:
    """Pull (start_index, samples) out of the payload after the fixed header.

    On v4 the eight bytes after the fixed header are the u64 start_index.
    """
    need = fixed + (8 if layout >= LAYOUT_V4 else 0)
    if len(payload) < need:
        raise ProtocolError(f"{name} payload shorter than its {need}-byte header")
    if layout >= LAYOUT_V4:
        (start_index,) = _U64.unpack_from(payload, fixed)
        return start_index, _f64(payload, fixed + 8)
    return NO_INDEX, _f64(payload, fixed)


# ── Frame dataclasses ───────────────────────────────────────────────
@dataclass(slots=True)
class DataFrame:
    """0x01 — raw waveform for one device block (§2.1).

    `samples` reshapes to (samples_per_channel, num_device_channels); the column
    order is devices[device].channels from the handshake, ascending.
    """

    device: int
    interleaved: np.ndarray
    start_index: int = NO_INDEX  #: v4 only, on the device effective-rate grid.

    #: Stream key for gap detection — frames sharing a grid group together.
    @property
    def stream_key(self) -> tuple:
        return ("data", self.device)

    @property
    def sample_count(self) -> int:
        """Interleaved length, since per-channel count needs the channel count.

        Use `count_for(num_channels)` for gap arithmetic.
        """
        return self.interleaved.size

    def count_for(self, num_channels: int) -> int:
        return self.interleaved.size // num_channels

    def reshape(self, num_channels: int) -> np.ndarray:
        if num_channels <= 0:
            raise ValueError("num_channels must be positive")
        if self.interleaved.size % num_channels:
            raise ProtocolError(
                f"{self.interleaved.size} samples not divisible by {num_channels} channels"
            )
        return self.interleaved.reshape(-1, num_channels)


@dataclass(slots=True)
class BandFrame:
    """0x03 — decimated band waveform (§2.2)."""

    band_index: int
    channel: int
    samples: np.ndarray
    start_index: int = NO_INDEX  #: v4 only, on that band's decimated_rate grid.

    @property
    def stream_key(self) -> tuple:
        return ("band", self.channel, self.band_index)

    @property
    def sample_count(self) -> int:
        return self.samples.size


@dataclass(slots=True)
class LevelFrame:
    """0x04 — broadband time-weighted level, dB (§2.3)."""

    channel: int
    levels_db: np.ndarray
    start_index: int = NO_INDEX  #: v4 only, on the level.output_rate grid.

    @property
    def stream_key(self) -> tuple:
        return ("level", self.channel)

    @property
    def sample_count(self) -> int:
        return self.levels_db.size


@dataclass(slots=True)
class BandLevelFrame:
    """0x05 — per-band time-weighted level, dB (§2.4)."""

    band_index: int
    channel: int
    levels_db: np.ndarray
    start_index: int = NO_INDEX  #: v4 only, on the level.output_rate grid.

    @property
    def stream_key(self) -> tuple:
        return ("band_level", self.channel, self.band_index)

    @property
    def sample_count(self) -> int:
        return self.levels_db.size


@dataclass(slots=True)
class RawDumpChunk:
    """0x06 — one chunk of a get_raw dump (§2.5)."""

    dump_id: int
    device: int
    chunk_index: int
    is_last: bool
    interleaved: np.ndarray
    start_index: int = NO_INDEX  #: v4 only. Same grid as DATA, so it lines up live.


@dataclass(slots=True)
class MsgFrame:
    """0x02 — JSON (handshake or event)."""

    payload: dict


@dataclass(slots=True)
class UnknownFrame:
    """Unknown type. The header was parsed, so stream sync is intact (§9.7)."""

    frame_type: int
    payload: bytes


StreamFrame = (
    DataFrame | BandFrame | LevelFrame | BandLevelFrame | RawDumpChunk | MsgFrame | UnknownFrame
)


def decode_frame(ftype: int, payload: bytes, layout: int = LAYOUT_V3) -> StreamFrame:
    """(type, payload) -> frame object. Unknown types come back without raising.

    `layout` is the frame layout version taken from the handshake (3 or 4).
    StreamClient passes it in, so callers rarely set it by hand.
    """
    if ftype == FRAME_LEVEL:
        index, samples = _split(payload, 4, layout, "LEVEL")
        (channel,) = U32.unpack_from(payload, 0)
        return LevelFrame(channel=channel, levels_db=samples, start_index=index)

    if ftype == FRAME_BAND_LEVEL:
        index, samples = _split(payload, 8, layout, "BAND_LEVEL")
        band_index, channel = U32.unpack_from(payload, 0)[0], U32.unpack_from(payload, 4)[0]
        return BandLevelFrame(
            band_index=band_index, channel=channel, levels_db=samples, start_index=index
        )

    if ftype == FRAME_DATA:
        index, samples = _split(payload, 4, layout, "DATA")
        (device,) = U32.unpack_from(payload, 0)
        return DataFrame(device=device, interleaved=samples, start_index=index)

    if ftype == FRAME_BAND:
        index, samples = _split(payload, 8, layout, "BAND")
        band_index, channel = U32.unpack_from(payload, 0)[0], U32.unpack_from(payload, 4)[0]
        return BandFrame(
            band_index=band_index, channel=channel, samples=samples, start_index=index
        )

    if ftype == FRAME_RAW_DUMP:
        index, samples = _split(payload, 16, layout, "RAW_DUMP")
        dump_id, device, chunk_index, is_last = (
            U32.unpack_from(payload, o)[0] for o in (0, 4, 8, 12)
        )
        return RawDumpChunk(
            dump_id=dump_id,
            device=device,
            chunk_index=chunk_index,
            is_last=bool(is_last),
            interleaved=samples,
            start_index=index,
        )

    if ftype == FRAME_MSG:
        import json

        try:
            obj = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProtocolError(f"bad MSG JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise ProtocolError("MSG payload is not a JSON object")
        return MsgFrame(payload=obj)

    return UnknownFrame(frame_type=ftype, payload=payload)


# ── Handshake / config snapshot (§3) ────────────────────────────────
@dataclass(frozen=True, slots=True)
class ChannelInfo:
    """Everything settled about one global channel."""

    global_index: int
    device: int
    device_type: str
    local: int
    units: str  # "Pa" or "V"
    sensitivity_mv_per_unit: float
    iepe: int

    @property
    def calibrated(self) -> bool:
        return self.units == "Pa"


@dataclass(frozen=True, slots=True)
class BandInfo:
    index: int
    center: float
    f_lo: float
    f_hi: float
    decimation: int
    decimated_rate: float

    @property
    def nominal_center(self) -> float:
        """Nominal midband frequency for reports (IEC 61260 R10 series).

        The handshake gives exact centres (e.g. 19.7 Hz); map them to 20/25/31.5/…
        """
        return _nearest_nominal(self.center)


_R10 = [1.0, 1.25, 1.6, 2.0, 2.5, 3.15, 4.0, 5.0, 6.3, 8.0]
_NOMINALS = sorted(m * (10**d) for d in range(-1, 5) for m in _R10)


def _nearest_nominal(center: float) -> float:
    if center <= 0:
        return center
    best = min(_NOMINALS, key=lambda n: abs(np.log(n) - np.log(center)))
    return round(best, 4) if best < 10 else round(best)


@dataclass(slots=True)
class Handshake:
    """Typed view over a handshake / get_config / started event body.

    The protocol evolves, so the original dict stays available as `raw`.
    """

    raw: dict = field(default_factory=dict)

    # ── Convenience accessors ──
    @property
    def protocol(self) -> str:
        return self.raw.get("protocol", "")

    @property
    def running(self) -> bool:
        return bool(self.raw.get("running", False))

    @property
    def channels(self) -> list[int]:
        return list(self.raw.get("channels", []))

    @property
    def num_channels(self) -> int:
        return int(self.raw.get("num_channels", len(self.channels)))

    @property
    def sample_rate(self) -> float:
        return float(self.raw.get("sample_rate", 0.0))

    @property
    def devices(self) -> list[dict]:
        return list(self.raw.get("devices", []))

    @property
    def stream_raw(self) -> bool:
        return bool(self.raw.get("stream_raw", False))

    @property
    def bands(self) -> dict:
        return dict(self.raw.get("bands", {}))

    @property
    def weighting(self) -> dict:
        return dict(self.raw.get("weighting", {}))

    @property
    def level_output_rate(self) -> float:
        return float(self.raw.get("level", {}).get("output_rate", 0.0))

    @property
    def buffer_seconds(self) -> float:
        return float(self.raw.get("storage", {}).get("buffer_seconds", 0.0))

    @property
    def resample(self) -> dict:
        return dict(self.raw.get("resample", {}))

    @property
    def resample_active(self) -> bool:
        return bool(self.resample.get("active", False))

    @property
    def frame_layout(self) -> int:
        """Frame payload layout version (3 or 4)."""
        return layout_of(self.raw)

    @property
    def has_sample_index(self) -> bool:
        """Do frames carry a start_index (§2)?"""
        return self.frame_layout >= LAYOUT_V4

    @property
    def clock_settled(self) -> bool:
        """Has every device's rate estimate settled enough to drive the resampler (§4)?

        This matters procedurally — 6 ppm at 30 s, 0.15 ppm at 300 s — so warm up
        until this reads True before the real measurement.
        """
        clock = self.raw.get("clock") or {}
        if not clock:
            return False
        return all(bool(c.get("settled")) for c in clock.values())

    @property
    def clock_ppm(self) -> dict[int, float]:
        clock = self.raw.get("clock") or {}
        return {int(k): float(v.get("ppm", 0.0)) for k, v in clock.items()}

    @property
    def epoch(self) -> dict:
        """Wall-clock time of sample index 0 (§2 of the amendment). Empty on v3."""
        return dict(self.raw.get("epoch", {}))

    def time_of(self, start_index: int, rate: float) -> float | None:
        """Convert a sample index to Unix time. None when there is no epoch.

        `rate` is the rate of the grid that index belongs to — device_rate() for
        DATA, level_output_rate for LEVEL, the band decimated_rate for BAND.
        """
        unix = self.epoch.get("unix")
        if unix is None or not rate or start_index < 0:
            return None
        return float(unix) + start_index / rate

    @property
    def overload_counts(self) -> dict[int, int]:
        """Cumulative clipped samples per channel (§3.2). Empty on v3."""
        raw = self.raw.get("overload") or {}
        return {int(k): int(v) for k, v in raw.items()}

    @property
    def dropped_by_type(self) -> dict[str, int]:
        """Per-type drop counters (§4.3). Empty when absent."""
        net = self.raw.get("network") or {}
        return dict(net.get("stream_frames_dropped_by_type") or {})

    @property
    def stream_frames_dropped(self) -> int | None:
        """Present in the handshake and get_config only, never in status (§3, §9.10)."""
        net = self.raw.get("network")
        return None if net is None else int(net.get("stream_frames_dropped", 0))

    @property
    def dropped_blocks(self) -> int | None:
        dsp = self.raw.get("dsp")
        return None if dsp is None else int(dsp.get("dropped_blocks", 0))

    # ── Derived lookups ──
    def channel_info(self, global_channel: int) -> ChannelInfo:
        key = str(global_channel)
        for entry in self.raw.get("channel_map", []):
            if entry.get("global") == global_channel:
                return ChannelInfo(
                    global_index=global_channel,
                    device=entry.get("device", -1),
                    device_type=entry.get("device_type", ""),
                    local=entry.get("local", -1),
                    units=self.raw.get("units", {}).get(key, "V"),
                    sensitivity_mv_per_unit=float(
                        self.raw.get("sensitivity_mv_per_unit", {}).get(key, 1000.0)
                    ),
                    iepe=int(self.raw.get("iepe", {}).get(key, 0)),
                )
        raise KeyError(f"global channel {global_channel} is not in channel_map")

    def all_channels(self) -> list[ChannelInfo]:
        return [self.channel_info(c) for c in self.channels]

    def device_of(self, global_channel: int) -> int:
        return self.channel_info(global_channel).device

    def device_channels(self, device: int) -> list[int]:
        for dev in self.devices:
            if dev.get("index") == device:
                return list(dev.get("channels", []))
        raise KeyError(f"no device with index {device}")

    def device_rate(self, device: int) -> float:
        """The rate that actually applies downstream.

        When resampling is active every device shares output_rate (§4).
        """
        if self.resample_active:
            rate = self.resample.get("output_rate")
            if rate:
                return float(rate)
        for dev in self.devices:
            if dev.get("index") == device:
                return float(dev.get("effective_rate") or dev.get("actual_rate") or 0.0)
        raise KeyError(f"no device with index {device}")

    def band_info(self, device: int, band_index: int) -> BandInfo:
        """Read a BAND/BAND_LEVEL frame: channel -> device -> that device's band_table (§3)."""
        for entry in self.raw.get("band_table", []) or []:
            if entry.get("device") != device:
                continue
            for band in entry.get("bands", []):
                if band.get("index") == band_index:
                    return BandInfo(
                        index=band_index,
                        center=float(band.get("center", 0.0)),
                        f_lo=float(band.get("f_lo", 0.0)),
                        f_hi=float(band.get("f_hi", 0.0)),
                        decimation=int(band.get("decimation", 1)),
                        decimated_rate=float(band.get("decimated_rate", 0.0)),
                    )
        raise KeyError(f"band {band_index} not found for device {device}")

    def band_info_for_channel(self, global_channel: int, band_index: int) -> BandInfo:
        return self.band_info(self.device_of(global_channel), band_index)

    def bands_of_device(self, device: int) -> list[BandInfo]:
        for entry in self.raw.get("band_table", []) or []:
            if entry.get("device") == device:
                return [self.band_info(device, b["index"]) for b in entry.get("bands", [])]
        return []

    @property
    def has_band_table(self) -> bool:
        return bool(self.raw.get("band_table"))

    def __iter__(self) -> Iterator[tuple[str, Any]]:
        return iter(self.raw.items())
