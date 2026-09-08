"""Background correction and normalisation — ISO 16283-2:2020.

Every formula here was read straight out of ISO 16283-2:2020 (§3.13–3.16, §7.3,
§9.2). The one exception is the rubber-ball formula (4), flagged on its function.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .bands import Spectrum, energy_difference

# ── Reference values ────────────────────────────────────────────────
#: [SPEC] §3.13 — reference reverberation time for dwellings
T0_DWELLING = 0.5
#: [SPEC] §3.15 — reference equivalent absorption area for dwellings
A0_DWELLING = 10.0
#: [SPEC] §3.16 — reference receiving room volume for dwellings
V0_DWELLING = 50.0

#: [SPEC] §9.2 — below this signal-to-background difference the value is a limit
BACKGROUND_MIN_DIFFERENCE = 6.0
#: [SPEC] §9.2 — the fixed correction then applied. 10log(1-10^-0.6) = -1.26 dB
BACKGROUND_LIMIT_CORRECTION = 1.3
#: [SPEC] §9.1 — at or above this the background is negligible
BACKGROUND_CLEAN_DIFFERENCE = 10.0


# ── Background correction (§9.2) ────────────────────────────────────
@dataclass(slots=True)
class BackgroundResult:
    """Background correction result and how much to trust it."""

    spectrum: Spectrum
    #: Per-band signal minus background (dB)
    differences: tuple[float, ...] = ()
    #: Bands where the difference was under 6 dB, so 1.3 dB was used — **limits**
    limited_bands: tuple[float, ...] = ()
    #: Bands with 6–10 dB difference — valid, but with larger uncertainty
    marginal_bands: tuple[float, ...] = ()

    @property
    def at_limit(self) -> bool:
        return bool(self.limited_bands)

    def report_note(self) -> str:
        """[SPEC] §9.2 — using the limit correction must be stated in the report."""
        if not self.limited_bands:
            return ""
        bands = ", ".join(f"{b:g}" for b in self.limited_bands)
        return (
            f"Signal-to-background difference below {BACKGROUND_MIN_DIFFERENCE:g} dB, so a "
            f"{BACKGROUND_LIMIT_CORRECTION:g} dB correction was applied to: {bands} Hz. "
            "Those band values are measurement limits."
        )


def correct_background(signal: Spectrum, background: Spectrum) -> BackgroundResult:
    """[SPEC] ISO 16283-2:2020 §9.2 — background noise correction.

    Rules:
      * difference >= 6 dB -> energy subtraction: L = 10 lg(10^(Lsb/10) − 10^(Lb/10))
      * difference < 6 dB  -> L = Lsb − 1.3 dB, reported as a **measurement limit**
      * >= 10 dB is preferred (§9.1); 6–10 dB is valid but less certain

    Both spectra must have the same band set.
    """
    if signal.centers != background.centers:
        raise ValueError("signal and background have different band sets")

    sig = signal.array()
    bkg = background.array()
    diff = sig - bkg

    corrected = np.empty_like(sig)
    limited: list[float] = []
    marginal: list[float] = []

    for i, (center, d) in enumerate(zip(signal.centers, diff)):
        if d >= BACKGROUND_MIN_DIFFERENCE:
            corrected[i] = energy_difference(sig[i], bkg[i])
            if d < BACKGROUND_CLEAN_DIFFERENCE:
                marginal.append(center)
        else:
            corrected[i] = sig[i] - BACKGROUND_LIMIT_CORRECTION
            limited.append(center)

    return BackgroundResult(
        spectrum=Spectrum(
            centers=signal.centers,
            levels=tuple(float(v) for v in corrected),
            fraction=signal.fraction,
            label=(signal.label + " (bg corrected)").strip(),
        ),
        differences=tuple(float(v) for v in diff),
        limited_bands=tuple(limited),
        marginal_bands=tuple(marginal),
    )


# ── Absorption area (§3.14 formula 2) ───────────────────────────────
def absorption_area(volume: float, reverberation_time: float) -> float:
    """[SPEC] Formula (2) — Sabine, A = 0.16 V / T."""
    if volume <= 0 or reverberation_time <= 0:
        raise ValueError("volume and reverberation time must be positive")
    return 0.16 * volume / reverberation_time


# ── Standardised / normalised (§3.13 f.1, §3.15 f.3) ────────────────
def standardized_level(
    li: Spectrum | float, reverberation_time, t0: float = T0_DWELLING
):
    """[SPEC] Formula (1) — standardised level L'nT = Li − 10 lg(T/T0).

    Refers the level to a 0.5 s reverberation time. `reverberation_time` may be
    a scalar or a per-band array.
    """
    t = np.asarray(reverberation_time, dtype=float)
    if np.any(t <= 0):
        raise ValueError("reverberation time must be positive")
    correction = 10.0 * np.log10(t / t0)
    if isinstance(li, Spectrum):
        return li._replace_levels(  # noqa: SLF001
            li.array() - correction, label=(li.label + " → L'nT").strip()
        )
    return float(li - correction)


def normalized_level(
    li: Spectrum | float,
    volume: float,
    reverberation_time,
    a0: float = A0_DWELLING,
):
    """[SPEC] Formula (3) — normalised level L'n = Li + 10 lg(A/A0).

    Refers the level to a 10 m² absorption area. `A` comes from formula (2),
    i.e. from the volume and the reverberation time.
    """
    t = np.asarray(reverberation_time, dtype=float)
    if np.any(t <= 0):
        raise ValueError("reverberation time must be positive")
    area = 0.16 * volume / t
    correction = 10.0 * np.log10(area / a0)
    if isinstance(li, Spectrum):
        return li._replace_levels(  # noqa: SLF001
            li.array() + correction, label=(li.label + " → L'n").strip()
        )
    return float(li + correction)


# ── Rubber ball: standardised maximum level (§3.16 f. 4/5/6) ────────
#: [SPEC] Formulas (5)(6) — C0 = 1.7275/T0, C = 1.7275/T
#: 1.7275 ≈ 0.125 × 13.8155 = (Fast time constant) × ln(10^6), so C is the ratio
#: of the Fast time constant to the room energy decay time constant.
FAST_DECAY_CONSTANT = 1.7275


def _c(t: float) -> float:
    return FAST_DECAY_CONSTANT / t


def standardized_max_level(
    li_fmax: float,
    volume: float,
    reverberation_time: float,
    v0: float = V0_DWELLING,
    t0: float = T0_DWELLING,
) -> float:
    """L'i,Fmax,V,T — maximum level corrected for volume, T and Fast weighting.

    ⚠ **The Korean post-construction scheme does not use this.** 국토교통부 고시
    제2022-868호 rates heavy impact by L'iA,Fmax — the A-weighted Fast maximum
    **as measured**, with no volume or reverberation normalisation — so
    `Session.evaluate()` deliberately never calls it. Kept for the ISO quantity
    and for anyone reporting L'i,Fmax,V,T on purpose.

    ⚠ **[DERIVED] — needs verification.**
    ISO 16283-2:2020 §3.16 formula (4) came out with broken equation layout in
    the public preview PDF, so it could not be transcribed. What follows is
    **derived** from formulas (5)(6) of the same standard plus a physical model.
    Check it against the published formula (4) before issuing a report; if it
    differs, this one function is all that needs fixing.

    Derivation:
      room energy decay  p²(t) = P0 · exp(−δt),  δ = ln(10⁶)/T = 13.8155/T
      Fast detector      ms(t) = (1/τ)∫ exp(−(t−u)/τ) p²(u) du,  τ = 0.125 s
      C ≡ δτ = 1.7275/T  (matches the standard's formula 6 — a good sign)

      Integrating and maximising over t gives
          max ms = P0 · C^(C/(1−C))
      For equal impact energy W, P0 ∝ W/V, so moving from (V, T) to the
      reference (V0, T0):

          L'i,Fmax,V,T = Li,Fmax + 10 lg(V/V0) − 10 lg[ C^(C/(1−C)) / C0^(C0/(1−C0)) ]

    Properties that hold:
      * T = T0, V = V0 gives zero correction (identity)
      * T > T0 (longer decay reads higher) gives a negative correction
      * V > V0 (bigger room reads lower) gives a positive correction
      Both directions are physically right.
    """
    if volume <= 0:
        raise ValueError("volume must be positive")
    if reverberation_time <= 0 or t0 <= 0:
        raise ValueError("reverberation time must be positive")

    volume_term = 10.0 * np.log10(volume / v0)
    decay_term = 10.0 * np.log10(_decay_factor(_c(reverberation_time)) / _decay_factor(_c(t0)))
    return float(li_fmax + volume_term - decay_term)


def _decay_factor(c: float) -> float:
    """C^(C/(1−C)). Continuously extended to exp(−1) at C → 1."""
    if abs(c - 1.0) < 1e-9:
        return float(np.exp(-1.0))
    return float(c ** (c / (1.0 - c)))


@dataclass(slots=True)
class RoomCorrection:
    """One receiving room condition, bundled for repeated use."""

    volume: float
    reverberation_time: float | np.ndarray
    t0: float = T0_DWELLING
    a0: float = A0_DWELLING
    v0: float = V0_DWELLING
    notes: list[str] = field(default_factory=list)

    @property
    def absorption(self):
        t = np.asarray(self.reverberation_time, dtype=float)
        return 0.16 * self.volume / t

    def standardized(self, li):
        return standardized_level(li, self.reverberation_time, self.t0)

    def normalized(self, li):
        return normalized_level(li, self.volume, self.reverberation_time, self.a0)

    def standardized_max(self, li_fmax: float) -> float:
        t = self.reverberation_time
        if np.ndim(t) != 0:
            raise ValueError("L'i,Fmax,V,T is a broadband scalar, so T must be scalar too")
        return standardized_max_level(li_fmax, self.volume, float(t), self.v0, self.t0)
