"""Frame loss detection — PROTOCOL_v4_AMENDMENT.md §1.4.

Only works on v4 streams, which carry `start_index`. On v3 there is no way in
principle to notice loss, so the detector stays quietly inactive
(`active == False`).

A measurement program should check `GapDetector.report()` when a session ends
and either void the affected measurement or at least note it in the report.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .frames import (
    NO_INDEX,
    BandFrame,
    BandLevelFrame,
    DataFrame,
    LevelFrame,
    StreamFrame,
)

#: Frame types worth tracking (MSG / RAW_DUMP are not continuous streams)
_TRACKED = (DataFrame, LevelFrame, BandFrame, BandLevelFrame)

#: Streams that backpressure may drop — a gap here is designed behaviour
#: (§6, §9.12) and the lost span can be recovered with get_raw.
BEST_EFFORT = ("data", "band")

#: Streams that are practically never dropped (§6). A gap here is far more
#: serious: even the large reliable queue filled, and the meter's main output
#: has been damaged.
RELIABLE = ("level", "band_level")


@dataclass(slots=True)
class Gap:
    """One loss event."""

    stream_key: tuple
    expected_index: int
    got_index: int

    @property
    def missing(self) -> int:
        return self.got_index - self.expected_index

    def seconds(self, rate: float) -> float:
        return self.missing / rate if rate else 0.0

    def __str__(self) -> str:
        return f"{self.stream_key}: {self.missing} samples missing at {self.expected_index}"


@dataclass(slots=True)
class StreamTrack:
    """Progress of one stream (one continuous sample grid)."""

    key: tuple
    first_index: int
    next_index: int
    received: int = 0
    missing: int = 0
    gaps: list[Gap] = field(default_factory=list)
    out_of_order: int = 0

    @property
    def span(self) -> int:
        """Total span from first to last sample, including what was lost."""
        return self.next_index - self.first_index

    @property
    def loss_ratio(self) -> float:
        return self.missing / self.span if self.span else 0.0


class GapDetector:
    """Watches start_index continuity per stream.

        detector = GapDetector()
        pi.on_frame(detector.feed)
        ...
        if detector.total_missing:
            print(detector.report())
    """

    def __init__(self, *, device_channels: dict[int, int] | None = None, keep_gaps: int = 1000):
        #: DATA frames only reveal interleaved length, so channel counts are needed.
        self.device_channels = dict(device_channels or {})
        self.keep_gaps = keep_gaps
        #: LEVEL / BAND_LEVEL indices count on this grid, not the audio one.
        #: Used only to turn a sample count into seconds for the report.
        self.level_output_rate = 0.0
        self._tracks: dict[tuple, StreamTrack] = {}
        self._lock = threading.Lock()
        self._saw_index = False

    # ── Setup ──
    def configure(self, handshake) -> None:
        """Read per-device channel counts and the level rate out of the handshake."""
        try:
            self.device_channels = {
                int(d["index"]): len(d.get("channels", [])) for d in handshake.devices
            }
        except (AttributeError, KeyError, TypeError):
            pass
        try:
            rate = float(handshake.level_output_rate)
        except (AttributeError, TypeError, ValueError):
            rate = 0.0
        if rate > 0:
            self.level_output_rate = rate

    def reset(self) -> None:
        """Call when a new scan starts. Indices reset to 0 on every start (§1.3)."""
        with self._lock:
            self._tracks.clear()

    # ── Intake ──
    def feed(self, frame: StreamFrame) -> Gap | None:
        """Check one frame. Returns a Gap when loss is detected."""
        if not isinstance(frame, _TRACKED):
            return None
        index = frame.start_index
        if index == NO_INDEX:
            return None  # v3 — cannot be detected

        if isinstance(frame, DataFrame):
            nch = self.device_channels.get(frame.device)
            if not nch:
                return None  # without the channel count we cannot count samples
            count = frame.count_for(nch)
        else:
            count = frame.sample_count

        key = frame.stream_key
        with self._lock:
            self._saw_index = True
            track = self._tracks.get(key)
            if track is None:
                self._tracks[key] = StreamTrack(
                    key=key, first_index=index, next_index=index + count, received=count
                )
                return None

            gap: Gap | None = None
            if index > track.next_index:
                gap = Gap(stream_key=key, expected_index=track.next_index, got_index=index)
                track.missing += gap.missing
                if len(track.gaps) < self.keep_gaps:
                    track.gaps.append(gap)
            elif index < track.next_index:
                # Protocol violation (duplicate/reorder). Count it, never rewind.
                track.out_of_order += 1
                track.received += count
                return None

            track.received += count
            track.next_index = index + count
            return gap

    # ── Queries ──
    @property
    def active(self) -> bool:
        """Did we actually see any index at all (i.e. is this a v4 stream)?"""
        with self._lock:
            return self._saw_index

    @property
    def tracks(self) -> dict[tuple, StreamTrack]:
        with self._lock:
            return dict(self._tracks)

    def _sum(self, kinds: tuple[str, ...], attr: str) -> int:
        with self._lock:
            return sum(
                (len(t.gaps) if attr == "gaps" else getattr(t, attr))
                for k, t in self._tracks.items()
                if k[0] in kinds
            )

    @property
    def total_missing(self) -> int:
        with self._lock:
            return sum(t.missing for t in self._tracks.values())

    @property
    def total_gaps(self) -> int:
        with self._lock:
            return sum(len(t.gaps) for t in self._tracks.values())

    # ── By severity (§9.12) ──
    @property
    def best_effort_missing(self) -> int:
        """DATA/BAND samples lost. Normal under load; recoverable via get_raw."""
        return self._sum(BEST_EFFORT, "missing")

    @property
    def reliable_missing(self) -> int:
        """LEVEL/BAND_LEVEL samples lost. Anything but zero is serious."""
        return self._sum(RELIABLE, "missing")

    @property
    def out_of_order(self) -> int:
        """Protocol violations (duplicate / out-of-order)."""
        with self._lock:
            return sum(t.out_of_order for t in self._tracks.values())

    @property
    def levels_intact(self) -> bool:
        """Is the meter's primary output (LEVEL/BAND_LEVEL) intact?

        This is the key criterion for validity. DATA loss does not affect it.
        """
        with self._lock:
            if not self._saw_index:
                return False
        return self.reliable_missing == 0 and self.out_of_order == 0

    @property
    def clean(self) -> bool:
        """Were there no gaps or reorders on any stream at all?

        A strict criterion. DATA loss is normal per §6, so validity checks
        normally use `levels_intact` instead.
        """
        with self._lock:
            if not self._saw_index:
                return False
            return all(t.missing == 0 and t.out_of_order == 0 for t in self._tracks.values())

    def reliable_summary(self) -> str:
        """One line about reliable-stream loss, in units an operator can act on.

        A bare sample count misleads badly. `reliable_missing` sums **every**
        LEVEL and BAND_LEVEL stream — with six channels and sixteen bands that
        is nearly a hundred counters — and the indices count on the level
        output grid, not the audio one. So "16,122 samples" sounds like a
        catastrophe and is often a second or two of one dropout.
        """
        missing = self.reliable_missing
        if not missing:
            return ""
        with self._lock:
            reliable = [t for k, t in self._tracks.items() if k[0] in RELIABLE]
        affected = sum(1 for t in reliable if t.missing)
        parts = [f"{missing:,} samples summed over {affected}/{len(reliable)} streams"]
        if self.level_output_rate > 0 and affected:
            seconds = missing / (self.level_output_rate * affected)
            parts.append(f"≈{seconds:.1f} s per affected stream")
        gaps = sum(len(t.gaps) for t in reliable)
        parts.append(f"{gaps} gap(s)")
        return ", ".join(parts)

    def diagnosis(self) -> str:
        """Is the reliable-stream loss real, or an index-grid mismatch?

        The two look identical in the totals and completely different in the
        detail, so this reads the detail.

        **A grid mismatch** is the failure mode where the server puts a LEVEL
        frame's `start_index` on the *audio* sample grid while the client
        counts the values inside the frame. Every frame then looks like a gap
        of exactly `rate/output_rate − count` samples, on every stream, from
        the very first frame. It is not loss at all.

        **A real dropout** is a handful of gaps, of differing sizes, appearing
        on all streams at the same moment because the link or the Pi stalled.
        """
        with self._lock:
            reliable = {k: t for k, t in self._tracks.items()
                        if k[0] in RELIABLE and t.missing}
        if not reliable:
            return "No loss on the reliable streams."

        # The discriminator is **how often** a stream gaps, not how big the
        # gaps are. A counting error gaps on every single frame, always by
        # the same amount; a stall gaps once or twice, when the link stalled.
        sizes: list[int] = []
        uniform_streams = 0
        gaps_per_stream: list[int] = []
        for track in reliable.values():
            sizes.extend(g.missing for g in track.gaps)
            gaps_per_stream.append(len(track.gaps))
            if len(track.gaps) >= 3 and len({g.missing for g in track.gaps}) == 1:
                uniform_streams += 1

        lines = [self.reliable_summary()]
        typical = max(gaps_per_stream) if gaps_per_stream else 0
        if uniform_streams >= max(1, len(reliable) // 2) and sizes:
            lines.append(
                f"Every gap is exactly {sizes[0]:,} samples, repeating up to "
                f"{typical} times per stream, on {uniform_streams} of "
                f"{len(reliable)} streams."
            )
            lines.append(
                "That is the signature of an index-grid mismatch, not loss: "
                "the server is numbering LEVEL frames on the audio sample "
                "grid while the client counts the values inside each frame. "
                "Nothing is actually missing — check the server's start_index "
                "convention against PROTOCOL.md §2.3."
            )
        else:
            biggest = max(sizes) if sizes else 0
            lines.append(
                f"{len(sizes)} gap(s) in total, at most {typical} per stream, "
                f"largest {biggest:,} samples — too few to be a per-frame "
                "counting error, so this is a genuine stall."
            )
            lines.append(
                "Usual causes, in order: raw WAV recording or stream_raw left "
                "on over a wireless link (~18 Mbit/s), the Pi's DSP saturating, "
                "or the network dropping out."
            )
        return "\n".join(lines)

    def report(self) -> str:
        with self._lock:
            if not self._saw_index:
                return "Gap detection unavailable — server is pislm/3 (no start_index)."
            if not self._tracks:
                return "No stream frames received."
            lines = []
            for key, t in sorted(self._tracks.items(), key=lambda kv: str(kv[0])):
                if t.missing == 0 and t.out_of_order == 0:
                    status = "ok"
                elif key[0] in BEST_EFFORT:
                    status = "lost (allowed)"  # designed behaviour per §6
                else:
                    status = "lost (serious)"
                line = (
                    f"  {'/'.join(str(p) for p in key):<20} {status:<10} "
                    f"received {t.received:>9,}  lost {t.missing:>7,}"
                )
                if t.missing:
                    line += f" ({t.loss_ratio * 100:.3f}%, {len(t.gaps)} gap(s))"
                if t.out_of_order:
                    line += f"  [{t.out_of_order} out of order]"
                lines.append(line)
            total = sum(t.missing for t in self._tracks.values())

        if total == 0:
            head = "No gaps — time axis continuous"
        else:
            best, rel = self.best_effort_missing, self.reliable_missing
            parts = []
            if rel:
                parts.append(f"level {rel:,} samples (serious)")
            if best:
                parts.append(f"raw/band {best:,} samples (allowed per §6, recoverable with get_raw)")
            head = "Lost: " + ", ".join(parts)
        return head + "\n" + "\n".join(lines)


__all__ = ["GapDetector", "Gap", "StreamTrack", "BEST_EFFORT", "RELIABLE"]
