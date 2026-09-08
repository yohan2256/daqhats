"""Live acquisition state — a buffer between the stream reader and the GUI.

Hundreds of frames per second can arrive. Emitting a Qt signal per frame
would drown the event loop, so values accumulate here and the GUI samples
them on a ~20 Hz timer instead.

The display keeps a maximum over each GUI refresh interval. Short network
pauses hold the latest value for up to 0.75 s; longer per-stream pauses become
missing readings. Live maxima are for display only: final heavy-impact Fmax is
recomputed from the buffered raw waveform in pislm.standards.impact.

"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import numpy as np

from pislm import BandLevelFrame, DataFrame, LevelFrame
from pislm.standards import nominal_center


@dataclass(slots=True)
class CaptureResult:
    """One capture, holding **all channels at once**.

    The rig has six channels, so a single excitation records every receiver
    position simultaneously. Exciting once per channel would let excitation
    variation leak in between channels; simultaneous capture avoids that and
    is what the standard expects.
    """

    seconds: float
    #: channel -> {nominal frequency: maximum level} — heavy impact Fmax
    band_max: dict[int, dict[float, float]] = field(default_factory=dict)
    #: channel -> broadband maximum level, at the configured weightings
    broadband_max: dict[int, float] = field(default_factory=dict)


class LiveState:
    """Collects stream frames into current values and capture maxima."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        #: channel -> most recent broadband level
        self.levels: dict[int, float] = {}
        #: (channel, band index) -> most recent band level
        self.band_levels: dict[tuple[int, int], float] = {}
        #: band index -> nominal midband frequency, taken from the handshake
        self.band_centers: dict[int, float] = {}
        self.last_frame_at: float = 0.0
        self._level_at: dict[int, float] = {}
        self._band_at: dict[tuple[int, int], float] = {}
        self.max_hold_seconds = 0.75
        self.raw_peak: dict[int, float] = {}

        #: Maximum since the GUI last read, per channel / per band. Cleared on
        #: read; `levels` / `band_levels` hold the last value so an interval
        #: with no frame shows the previous reading instead of blanking.
        self._level_since_read: dict[int, float] = {}
        self._band_since_read: dict[tuple[int, int], float] = {}

        self._capturing = False
        self._capture_started = 0.0
        self._capture_band_max: dict[tuple[int, int], float] = {}
        self._capture_level_max: dict[int, float] = {}

    # ── Band table from the handshake ──
    def configure_bands(self, config) -> bool:
        """Build the band index -> nominal midband frequency lookup.

        Returns True when a table was found.

        **An empty result never clears an existing mapping.** `band_table` is
        only present while band output is active (§3), so `get_config` on a
        stopped scan legitimately omits it. Wiping the mapping there left every
        incoming BAND_LEVEL frame unmatched and the spectrum permanently blank —
        that was a real bug, and it only showed after applying a session.
        """
        mapping: dict[int, float] = {}
        for device in config.raw.get("band_table", []) or []:
            for band in device.get("bands", []):
                try:
                    mapping[int(band["index"])] = nominal_center(
                        float(band["center"]), config.bands.get("fraction", 3)
                    )
                except (KeyError, ValueError):
                    continue
        if not mapping:
            return False
        with self._lock:
            if mapping == self.band_centers:
                return True
            # Only when the table actually changes: band indices get reassigned,
            # so readings held under the old indices would be mislabelled.
            # Clearing on every call would also throw away good data, because
            # this runs again on each handshake and each started event.
            self.band_centers = mapping
            self.band_levels.clear()
            # The pending-maximum accumulator holds readings under the *old*
            # indices too, and would put them straight back on the next
            # refresh under the new labels.
            self._band_since_read.clear()
        return True

    @property
    def bands_known(self) -> bool:
        with self._lock:
            return bool(self.band_centers)

    # ── Frame intake (stream reader thread) ──
    def feed(self, frame) -> None:
        now = time.monotonic()
        if isinstance(frame, LevelFrame):
            if frame.levels_db.size == 0:
                return
            values = frame.levels_db[np.isfinite(frame.levels_db)]
            if not values.size:
                return
            peak = float(np.max(values))
            with self._lock:
                self._level_at[frame.channel] = now
                self.levels[frame.channel] = peak
                current = self._level_since_read.get(frame.channel, -np.inf)
                self._level_since_read[frame.channel] = max(current, peak)
                self.last_frame_at = now
                if self._capturing:
                    current = self._capture_level_max.get(frame.channel, -np.inf)
                    self._capture_level_max[frame.channel] = max(current, peak)

        elif isinstance(frame, BandLevelFrame):
            if frame.levels_db.size == 0:
                return
            key = (frame.channel, frame.band_index)
            values = frame.levels_db[np.isfinite(frame.levels_db)]
            if not values.size:
                return
            peak = float(np.max(values))
            with self._lock:
                self._band_at[key] = now
                self.band_levels[key] = peak
                current = self._band_since_read.get(key, -np.inf)
                self._band_since_read[key] = max(current, peak)
                self.last_frame_at = now
                if self._capturing:
                    current = self._capture_band_max.get(key, -np.inf)
                    self._capture_band_max[key] = max(current, peak)

        elif isinstance(frame, DataFrame):
            if frame.interleaved.size:
                with self._lock:
                    self.raw_peak[frame.device] = float(np.max(np.abs(frame.interleaved)))

    # ── Queries (GUI thread) ──
    def snapshot_levels(self) -> dict[int, float]:
        """Highest level per channel since the last call — see the module note.

        Reading clears the accumulator, so each GUI refresh reports its own
        interval. Channels that produced no frame fall back to their held
        value rather than disappearing.
        """
        with self._lock:
            now = time.monotonic()
            out = {ch: v for ch, v in self.levels.items()
                   if now - self._level_at.get(ch, 0) <= self.max_hold_seconds}
            out.update({ch: v for ch, v in self._level_since_read.items()
                        if ch in out and np.isfinite(v)})
            self._level_since_read.clear()
            return out

    def snapshot_spectrum(self, channel: int) -> dict[float, float]:
        """Current band levels for one channel, keyed by nominal frequency."""
        with self._lock:
            now = time.monotonic()
            centers = dict(self.band_centers)
            bands = {
                index: value
                for (ch, index), value in self.band_levels.items()
                if ch == channel and now - self._band_at.get((ch, index), 0) <= self.max_hold_seconds
            }
            for (ch, index), value in list(self._band_since_read.items()):
                if ch == channel:
                    if index in bands and np.isfinite(value):
                        bands[index] = value
                    del self._band_since_read[(ch, index)]
        return {centers[i]: v for i, v in bands.items() if i in centers}

    @property
    def stale_seconds(self) -> float:
        with self._lock:
            if not self.last_frame_at:
                return float("inf")
            return time.monotonic() - self.last_frame_at

    # ── Capture ──
    def start_capture(self) -> None:
        with self._lock:
            self._capturing = True
            self._capture_started = time.monotonic()
            self._capture_band_max.clear()
            self._capture_level_max.clear()

    @property
    def capturing(self) -> bool:
        with self._lock:
            return self._capturing

    @property
    def capture_elapsed(self) -> float:
        with self._lock:
            return time.monotonic() - self._capture_started if self._capturing else 0.0

    def finish_capture(self, channels=None) -> CaptureResult:
        """End the capture and return results **per channel**.

        Pass `channels` to restrict the result, otherwise every channel seen
        during the window is included.
        """
        with self._lock:
            self._capturing = False
            elapsed = time.monotonic() - self._capture_started
            centers = dict(self.band_centers)
            wanted = (
                set(channels)
                if channels is not None
                else {ch for ch, _ in self._capture_band_max} | set(self._capture_level_max)
            )
            band_max: dict[int, dict[float, float]] = {ch: {} for ch in wanted}
            for (ch, index), value in self._capture_band_max.items():
                if ch in wanted and index in centers and np.isfinite(value):
                    band_max[ch][centers[index]] = value
            broadband = {
                ch: v
                for ch, v in self._capture_level_max.items()
                if ch in wanted and np.isfinite(v)
            }
        return CaptureResult(seconds=elapsed, band_max=band_max, broadband_max=broadband)
