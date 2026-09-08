"""Frequency bands, weighting curves and level arithmetic — the foundation.

Provenance tags used throughout this package:
  [SPEC]      value or procedure taken directly from the ISO/KS text
  [DERIVED]   derived from physics because the text was unavailable; verify
  [PRACTICE]  not in the standard, but standard industry handling
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# ── Nominal midband frequencies (IEC 61260 R10 series) ──────────────
#: 1/3-octave nominal values — the labels used in reports, not exact centres.
THIRD_OCTAVE_NOMINAL = (
    12.5, 16, 20, 25, 31.5, 40, 50, 63, 80,
    100, 125, 160, 200, 250, 315, 400, 500, 630, 800,
    1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000,
    10000, 12500, 16000, 20000,
)

#: 1/1-octave nominal values.
OCTAVE_NOMINAL = (16, 31.5, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)

# ── Bands the standards require ─────────────────────────────────────
#: [SPEC] ISO 16283-2:2020 §5.1 — tapping machine (light), required 1/3-octave bands
TAPPING_THIRD_OCTAVE = (
    100, 125, 160, 200, 250, 315, 400, 500, 630, 800,
    1000, 1250, 1600, 2000, 2500, 3150,
)
#: [SPEC] §5.1 — optional low-frequency extension; mandatory below 25 m³
TAPPING_LOW_FREQUENCY = (50, 63, 80)

#: [SPEC] ISO 16283-2:2020 §5.2 — rubber ball (heavy), required 1/3-octave bands
RUBBER_BALL_THIRD_OCTAVE = (50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630)
#: [PRACTICE] Korean heavy-impact reports are often given as 1/1 octave, 63–500 Hz
RUBBER_BALL_OCTAVE = (63, 125, 250, 500)

#: [SPEC] ISO 717-2:2020 §4.1 — bands used for the single-number rating
RATING_THIRD_OCTAVE = TAPPING_THIRD_OCTAVE
RATING_OCTAVE = (125, 250, 500, 1000, 2000)


def nominal_center(exact_center: float, fraction: int = 3) -> float:
    """Map an exact centre (e.g. 19.7 Hz) to its nominal label (20 Hz).

    The Pi band_table gives exact values, so run them through this for reports.
    """
    table = THIRD_OCTAVE_NOMINAL if fraction == 3 else OCTAVE_NOMINAL
    if exact_center <= 0:
        raise ValueError("center frequency must be positive")
    return min(table, key=lambda n: abs(math.log(n) - math.log(exact_center)))


# ── Exact midband frequencies (IEC 61260-1) ─────────────────────────
#: [SPEC] IEC 61260-1 — base-ten octave ratio, G = 10^(3/10) ≈ 1.99526.
#: This is the preferred system; base-two (G=2) diverges at high frequencies.
G_BASE10 = 10.0 ** 0.3

#: Reference frequency. Every band is counted from here.
REFERENCE_FREQUENCY = 1000.0


def band_index(nominal: float, fraction: int = 3) -> int:
    """Band number for a nominal frequency (1000 Hz = 0)."""
    if nominal <= 0:
        raise ValueError("frequency must be positive")
    return round(fraction * math.log(nominal / REFERENCE_FREQUENCY, G_BASE10))


def exact_center(nominal: float, fraction: int = 3) -> float:
    """**Exact** midband frequency for a nominal band (IEC 61260-1).

        f_m = 1000 × G^(x/b),   G = 10^(3/10)

    Nominal values are rounded labels for humans. Filter design, A-weighting
    and band edges must all use the exact value.

        63 Hz  → 63.0957 Hz
        125 Hz → 125.8925 Hz
        1000 Hz → 1000.0 Hz
        3150 Hz → 3162.2777 Hz
    """
    return REFERENCE_FREQUENCY * G_BASE10 ** (band_index(nominal, fraction) / fraction)


def band_edges(nominal: float, fraction: int = 3) -> tuple[float, float]:
    """[SPEC] IEC 61260-1 — lower and upper band edge.

        f1 = f_m × G^(−1/2b),  f2 = f_m × G^(+1/2b)
    """
    center = exact_center(nominal, fraction)
    half = G_BASE10 ** (1.0 / (2 * fraction))
    return center / half, center * half


def exact_centers(nominals, fraction: int = 3) -> np.ndarray:
    return np.array([exact_center(f, fraction) for f in nominals], dtype=float)


# ── Weighting curves ────────────────────────────────────────────────
def a_weighting(frequency: float | np.ndarray) -> np.ndarray:
    """[SPEC] IEC 61672-1 A-weighting (dB).

    Computed from the standard's analytic expression rather than a transcribed
    table: no transcription errors, and it works at any frequency.

        Normalised with +2.00 dB so that 1000 Hz is exactly 0 dB.
    """
    f = np.asarray(frequency, dtype=float)
    f2 = f * f
    numerator = (12194.0**2) * f2 * f2
    denominator = (
        (f2 + 20.6**2)
        * np.sqrt((f2 + 107.7**2) * (f2 + 737.9**2))
        * (f2 + 12194.0**2)
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        return 20.0 * np.log10(numerator / denominator) + 2.0


def c_weighting(frequency: float | np.ndarray) -> np.ndarray:
    """[SPEC] IEC 61672-1 C-weighting (dB)."""
    f = np.asarray(frequency, dtype=float)
    f2 = f * f
    numerator = (12194.0**2) * f2
    denominator = (f2 + 20.6**2) * (f2 + 12194.0**2)
    with np.errstate(divide="ignore", invalid="ignore"):
        return 20.0 * np.log10(numerator / denominator) + 0.062


# ── Level arithmetic ────────────────────────────────────────────────
def energy_sum(levels) -> float:
    """Energy sum of levels, dB. Band levels -> broadband level."""
    arr = np.asarray(levels, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return float("-inf")
    return float(10.0 * np.log10(np.sum(10.0 ** (arr / 10.0))))


def energy_average(levels) -> float:
    """Energy average of levels, dB.

    Used to average over receiver and source positions — it must be an energy
    average, not an arithmetic one (ISO 16283-2 formulas 7/8/9).
    """
    arr = np.asarray(levels, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return float("-inf")
    return float(10.0 * np.log10(np.mean(10.0 ** (arr / 10.0))))


def energy_difference(total, part) -> float:
    """Energy subtraction, dB. The core operation of background correction.

    Returns -inf when part >= total (a physically impossible input).
    """
    t = 10.0 ** (np.asarray(total, dtype=float) / 10.0)
    p = 10.0 ** (np.asarray(part, dtype=float) / 10.0)
    diff = t - p
    if np.ndim(diff) == 0:
        return float(10.0 * np.log10(diff)) if diff > 0 else float("-inf")
    out = np.full_like(diff, -np.inf, dtype=float)
    np.log10(diff, out=out, where=diff > 0)
    return 10.0 * out


def round_half_up_1dp(value: float | np.ndarray) -> np.ndarray:
    """[SPEC] ISO 717-2:2020 §4.3.1 footnote 1 — round to one decimal place.

    One of the rare cases where the standard specifies the algorithm: multiply
    by 10, add 0.5, take the integer part, divide by 10. Python's built-in
    `round()` is banker's rounding, so 2.25 -> 2.2, which disagrees. Use this.

    The standard also insists it applies to the true value, not what is displayed.
    """
    arr = np.asarray(value, dtype=float)
    return np.floor(arr * 10.0 + 0.5) / 10.0


# ── Spectrum container ──────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Spectrum:
    """One set of band levels.

    `centers` holds **nominal** midband frequencies, because the standard tables
    (ISO 717-2 and friends) are indexed by nominal value and reports use them.

    **Physics, however, uses the exact midband frequency** — A-weighting and
    filter band edges. `exact()` / `edges()` convert via IEC 61260-1.
    Nominal 3150 Hz is really 3162.28 Hz; the A-weighting differs by ~0.02 dB.
    """

    centers: tuple[float, ...]
    levels: tuple[float, ...]
    fraction: int = 3  # 3 = 1/3 octave, 1 = 1/1 octave
    label: str = ""
    #: What this spectrum holds, e.g. "Leq" or "Fmax". For display and reports.
    quantity: str = ""
    #: Frequency weighting: "Z" | "A" | "C". For display and reports.
    weighting: str = "Z"

    def __post_init__(self) -> None:
        if len(self.centers) != len(self.levels):
            raise ValueError(
                f"centers({len(self.centers)}) and levels({len(self.levels)}) differ in length"
            )
        if self.fraction not in (1, 3):
            raise ValueError("fraction must be 1 or 3")

    # ── Construction ──
    @classmethod
    def from_mapping(
        cls,
        mapping: dict[float, float],
        fraction: int = 3,
        label: str = "",
        quantity: str = "",
        weighting: str = "Z",
    ) -> Spectrum:
        items = sorted(mapping.items())
        return cls(
            centers=tuple(f for f, _ in items),
            levels=tuple(v for _, v in items),
            fraction=fraction,
            label=label,
            quantity=quantity,
            weighting=weighting,
        )

    # ── Exact frequencies (for computation) ──
    def exact(self) -> np.ndarray:
        """IEC 61260-1 exact midband frequencies."""
        return exact_centers(self.centers, self.fraction)

    def edges(self) -> list[tuple[float, float]]:
        """Per-band (lower, upper) edges — for filter design."""
        return [band_edges(c, self.fraction) for c in self.centers]

    @property
    def display_quantity(self) -> str:
        """Display name such as `Lp,Leq` or `Lp,Fmax`."""
        if not self.quantity:
            return "Lp"
        return f"Lp,{self.quantity}"

    # ── Lookup ──
    def as_dict(self) -> dict[float, float]:
        return dict(zip(self.centers, self.levels))

    def level_at(self, center: float) -> float:
        try:
            return self.levels[self.centers.index(center)]
        except ValueError as exc:
            raise KeyError(f"no {center} Hz band: {self.centers}") from exc

    def select(self, centers) -> Spectrum:
        """Select just the bands a standard requires. Missing bands raise."""
        wanted = tuple(centers)
        table = self.as_dict()
        missing = [c for c in wanted if c not in table]
        if missing:
            raise KeyError(f"required bands are missing: {missing} (have: {self.centers})")
        return Spectrum(
            centers=wanted,
            levels=tuple(table[c] for c in wanted),
            fraction=self.fraction,
            label=self.label,
        )

    # ── Operations ──
    def array(self) -> np.ndarray:
        return np.asarray(self.levels, dtype=float)

    def offset(self, delta) -> Spectrum:
        """Per-band (or uniform) dB offset."""
        return self._replace_levels(self.array() + np.asarray(delta, dtype=float))

    def a_weighted(self) -> Spectrum:
        """Add A-weighting. The weights come from the **exact** midband frequency."""
        if self.weighting == "A":
            return self
        weighted = Spectrum(
            centers=self.centers,
            levels=tuple(float(v) for v in self.array() + a_weighting(self.exact())),
            fraction=self.fraction,
            label=(self.label + " (A-weighted)").strip(),
            quantity=self.quantity,
            weighting="A",
        )
        return weighted

    def unweighted(self) -> Spectrum:
        """Undo A-weighting back to Z. For bands the Pi sent already weighted."""
        if self.weighting != "A":
            return self
        return Spectrum(
            centers=self.centers,
            levels=tuple(float(v) for v in self.array() - a_weighting(self.exact())),
            fraction=self.fraction,
            label=self.label,
            quantity=self.quantity,
            weighting="Z",
        )

    def total(self) -> float:
        """Energy sum of the band levels = broadband level."""
        return energy_sum(self.levels)

    def a_weighted_total(self) -> float:
        """[SPEC] ISO 717-2 style A-weighted single number.

        A-weighting **values are added** to the band levels and the result is
        energy-summed. No A filter runs on the signal — measurement is Z and the
        weighting is applied arithmetically. This is how L_iA,Fmax is obtained.
        """
        return self.a_weighted().total()

    def rounded(self) -> Spectrum:
        """Round to one decimal place before an ISO 717-2 calculation."""
        return self._replace_levels(round_half_up_1dp(self.array()))

    def _replace_levels(self, levels, label: str | None = None) -> Spectrum:
        return Spectrum(
            centers=self.centers,
            levels=tuple(float(v) for v in np.asarray(levels, dtype=float)),
            fraction=self.fraction,
            label=self.label if label is None else label,
            quantity=self.quantity,
            weighting=self.weighting,
        )

    def __repr__(self) -> str:
        body = ", ".join(f"{c:g}:{v:.1f}" for c, v in zip(self.centers, self.levels))
        tag = f" {self.label!r}" if self.label else ""
        return f"<Spectrum 1/{self.fraction}oct{tag} {self.display_quantity} {body}>"


def average_spectra(spectra: list[Spectrum]) -> Spectrum:
    """Per-band energy average over positions (formulas 7/8/9)."""
    if not spectra:
        raise ValueError("empty list")
    first = spectra[0]
    for other in spectra[1:]:
        if other.centers != first.centers:
            raise ValueError("cannot average spectra with different band sets")
        if other.fraction != first.fraction:
            raise ValueError("fraction differs between spectra")
    stack = np.vstack([s.array() for s in spectra])
    averaged = 10.0 * np.log10(np.mean(10.0 ** (stack / 10.0), axis=0))
    return Spectrum(
        centers=first.centers,
        levels=tuple(float(v) for v in averaged),
        fraction=first.fraction,
        label=first.label,
        quantity=first.quantity,
        weighting=first.weighting,
    )


def rms_level(pressure, reference: float = 20e-6) -> float:
    """[SPEC] Equivalent level from a time waveform.

        Leq = 10 lg( mean(p²) / p0² ) = 20 lg( RMS(p) / p0 )

    Averaging dB values arithmetically is wrong; take the **root mean square**
    of the pressure. Input is Pa when calibrated; pass reference=1.0 for volts.
    """
    arr = np.asarray(pressure, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return float("-inf")
    mean_square = float(np.mean(arr * arr))
    if mean_square <= 0:
        return float("-inf")
    return 10.0 * math.log10(mean_square / (reference * reference))
