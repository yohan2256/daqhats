"""Reverberation time — T20 / T30 by Schroeder backward integration (ISO 3382-2).

Both L'nT and L'n need a reverberation time, so for floor impact measurement it
is a required input, not extra information. The Pi does not compute it, so the
laptop derives it from the decay waveform fetched with `get_raw`.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import signal as sig

from .bands import Spectrum, band_edges

#: [SPEC] ISO 3382-2 — evaluation ranges. Start at −5 dB, span 20 or 30 dB.
EVALUATION_RANGES = {"T20": (-5.0, -25.0), "T30": (-5.0, -35.0)}
#: Extrapolation factor: 20 dB scaled to 60 dB is ×3; 30 dB is ×2
EXTRAPOLATION = {"T20": 3.0, "T30": 2.0}


#: The tail of a Schroeder curve is an artefact of the record ending, not decay:
#: energy outside the integration window counts as zero. Evaluating there gives
#: an absurdly short T.
TRUNCATION_GUARD = 0.95

#: [PRACTICE] Curvature limit (%), the value usually quoted for ISO 3382-2 Engineering.
CURVATURE_LIMIT = 10.0


@dataclass(slots=True)
class DecayResult:
    """Reverberation time for one band, with its reliability.

    Always read the quality metrics too — if the decay is not a straight line
    the T value is meaningless, and the number alone will not tell you.
    """

    t60: float
    method: str
    #: [SPEC] ISO 3382-2 — correlation coefficient; closer to 1 is straighter
    correlation: float
    #: Usable decay range (dB), measured excluding the truncation artefact
    decay_range_db: float
    #: Curvature C = 100 × (T30/T20 − 1), in **%**, for spotting double slopes.
    #: Only present when both methods can be computed; None when they cannot
    #: (which means "unknown", not "straight").
    curvature_percent: float | None = None
    band: float | None = None

    @property
    def required_range(self) -> float:
        """Minimum decay range this method needs, plus 5 dB of headroom."""
        return abs(EVALUATION_RANGES[self.method][1]) + 5.0

    @property
    def reliable(self) -> bool:
        """[PRACTICE] Working acceptance criteria.

        The 10 % curvature limit is the figure usually quoted for the ISO 3382-2
        "Engineering" grade, but the per-grade table (Annex A) could not be
        verified. Check it if the report grade matters; tune `CURVATURE_LIMIT`.
        """
        if self.correlation < 0.98:
            return False
        if self.decay_range_db < self.required_range:
            return False
        if self.curvature_percent is not None and abs(self.curvature_percent) > CURVATURE_LIMIT:
            return False
        return True

    def warnings(self) -> list[str]:
        out = []
        if self.correlation < 0.98:
            out.append(f"correlation {self.correlation:.4f} — decay departs from a line")
        if self.decay_range_db < self.required_range:
            out.append(
                f"decay range {self.decay_range_db:.1f} dB — "
                f"{self.method} needs at least {self.required_range:.0f} dB"
            )
        if self.curvature_percent is not None and abs(self.curvature_percent) > CURVATURE_LIMIT:
            out.append(f"curvature {self.curvature_percent:+.1f}% — possible double slope")
        return out


def schroeder_curve(decay: np.ndarray, noise_floor: float | None = None) -> np.ndarray:
    """Schroeder backward integration curve (dB, normalised to 0 at the start).

    EDC(t) = 10 lg( ∫_t^∞ p²(u) du / ∫_0^∞ p²(u) du )

    Passing `noise_floor` (linear power) subtracts it before integrating, which
    reduces the tail bend caused by noise. Otherwise the last 10 % is used to
    estimate it automatically.
    """
    energy = np.asarray(decay, dtype=float) ** 2
    if energy.size < 16:
        raise ValueError("too few samples")

    if noise_floor is None:
        tail = energy[int(energy.size * 0.9) :]
        noise_floor = float(np.mean(tail)) if tail.size else 0.0

    compensated = np.maximum(energy - noise_floor, 0.0)
    # Cumulative from the end = remaining energy
    remaining = np.cumsum(compensated[::-1])[::-1]
    total = remaining[0]
    if total <= 0:
        raise ValueError("energy is zero — check the signal")
    with np.errstate(divide="ignore"):
        return 10.0 * np.log10(np.maximum(remaining / total, 1e-300))


def decay_time(
    decay: np.ndarray,
    sample_rate: float,
    method: str = "T20",
    band: float | None = None,
    *,
    with_curvature: bool = True,
) -> DecayResult:
    """Compute T20 or T30 from one decay waveform.

    `decay` must already be **filtered to the band of interest**. Start it at the
    moment the source stopped (or at the impulse) and include enough of the tail.

    The last 5 % of the Schroeder curve is a truncation artefact and is excluded.
    Without that guard the curve dives to −∞ and **"passes" −35 dB even on a
    signal that barely decayed at all**, quietly producing an absurdly short T.
    """
    if method not in EVALUATION_RANGES:
        raise ValueError(f"method must be one of {list(EVALUATION_RANGES)}")

    curve = schroeder_curve(decay)
    usable = max(16, int(len(curve) * TRUNCATION_GUARD))
    search = curve[:usable]
    upper_db, lower_db = EVALUATION_RANGES[method]

    decay_range = float(-search[-1])
    start = _first_below(search, upper_db)
    end = _first_below(search, lower_db)
    if start is None or end is None or end <= start + 4:
        raise ValueError(
            f"cannot place the {method} evaluation range — excluding the truncated "
            f"tail there is only {decay_range:.1f} dB of decay, short of "
            f"{abs(lower_db):.0f} dB. Raise the excitation, lower the background, "
            "or record for longer"
        )

    times = np.arange(start, end) / sample_rate
    values = search[start:end]
    slope, intercept = np.polyfit(times, values, 1)
    if slope >= 0:
        raise ValueError("decay slope is positive — check the input window")

    curvature = None
    if with_curvature:
        curvature = _curvature(curve, sample_rate, usable)

    return DecayResult(
        t60=float(-60.0 / slope),
        method=method,
        correlation=_correlation(values, slope * times + intercept),
        decay_range_db=decay_range,
        curvature_percent=curvature,
        band=band,
    )


def _fit_t60(curve: np.ndarray, sample_rate: float, usable: int, method: str) -> float | None:
    upper_db, lower_db = EVALUATION_RANGES[method]
    search = curve[:usable]
    start = _first_below(search, upper_db)
    end = _first_below(search, lower_db)
    if start is None or end is None or end <= start + 4:
        return None
    times = np.arange(start, end) / sample_rate
    slope = np.polyfit(times, search[start:end], 1)[0]
    return None if slope >= 0 else float(-60.0 / slope)


def _curvature(curve: np.ndarray, sample_rate: float, usable: int) -> float | None:
    """Curvature C = 100 × (T30/T20 − 1), %.

    A single exponential decay gives the same T both ways, so C ≈ 0. A double
    slope (e.g. a second path through the adjoining structure) stretches T30
    beyond T20 and C grows.

    Note: a single measurement's C wobbles by several % from realisation noise
    alone. T20 sees only a 20 dB window, so it is more sensitive to local ripple
    in the decay. That is why ISO 3382-2 asks for averaging over positions and
    repeats — which is what `average_reverberation()` is for.

    None when the decay is too short for T30 — that means "curvature unknown",
    not "the decay is straight".
    """
    t20 = _fit_t60(curve, sample_rate, usable, "T20")
    t30 = _fit_t60(curve, sample_rate, usable, "T30")
    if not t20 or not t30:
        return None
    return float(100.0 * (t30 / t20 - 1.0))


def _first_below(curve: np.ndarray, threshold: float) -> int | None:
    idx = np.flatnonzero(curve <= threshold)
    return int(idx[0]) if idx.size else None


def _correlation(values: np.ndarray, fitted: np.ndarray) -> float:
    residual = values - fitted
    variance = np.sum((values - values.mean()) ** 2)
    if variance <= 0:
        return 0.0
    return float(np.sqrt(max(0.0, 1.0 - np.sum(residual**2) / variance)))


# ── Band decomposition, all at once ─────────────────────────────────
def octave_filter(
    data: np.ndarray, sample_rate: float, center: float, fraction: int = 3, order: int = 6
) -> np.ndarray:
    """[SPEC] IEC 61260-1 bandpass, zero-phase to avoid phase distortion.

    `center` is the **nominal** frequency; the band edges come from the IEC
    61260-1 exact midband frequency (`f_m × G^(±1/2b)`, G = 10^0.3). The base-two
    approximation (`2^(±1/2b)`) misplaces the edges by several % at high bands.

    `filtfilt` runs forward and backward, doubling the effective order, but for
    reverberation only the energy decay matters and phase preservation wins.
    """
    low, high = band_edges(center, fraction)
    high = min(high, sample_rate / 2.0 * 0.999)
    if low >= high:
        raise ValueError(f"{center} Hz band exceeds Nyquist (fs={sample_rate})")
    sos = sig.butter(order, [low, high], btype="band", fs=sample_rate, output="sos")
    return sig.sosfiltfilt(sos, data)


def reverberation_spectrum(
    decay: np.ndarray,
    sample_rate: float,
    centers,
    *,
    method: str = "T20",
    fraction: int = 3,
) -> tuple[Spectrum, dict[float, DecayResult]]:
    """Reverberation time per band from one broadband decay waveform.

    Returns: (Spectrum holding T per band, detailed results per band).
    Bands that fail become NaN in the Spectrum and are absent from the details.
    """
    results: dict[float, DecayResult] = {}
    times: list[float] = []
    for center in centers:
        try:
            filtered = octave_filter(decay, sample_rate, center, fraction)
            result = decay_time(filtered, sample_rate, method, band=center)
        except (ValueError, RuntimeError):
            times.append(float("nan"))
            continue
        results[center] = result
        times.append(result.t60)

    return (
        Spectrum(
            centers=tuple(centers),
            levels=tuple(times),  # seconds here, not dB
            fraction=fraction,
            label=f"reverberation {method} (s)",
        ),
        results,
    )


def average_reverberation(results: list[dict[float, DecayResult]]) -> dict[float, float]:
    """Arithmetic mean of reverberation times per band (ISO 3382-2).

    T is not a level, so an **arithmetic** mean is correct — not an energy one.
    """
    if not results:
        raise ValueError("empty list")
    bands = sorted({band for entry in results for band in entry})
    out: dict[float, float] = {}
    for band in bands:
        values = [entry[band].t60 for entry in results if band in entry]
        if values:
            out[band] = float(np.mean(values))
    return out
