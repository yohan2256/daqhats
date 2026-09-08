"""Single-number rating — the ISO 717-2:2020 reference curve shifting method.

The reference values (Table 3) and the shifting rule (§4.3) were read from the text.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .bands import RATING_OCTAVE, RATING_THIRD_OCTAVE, Spectrum, round_half_up_1dp

# ── [SPEC] ISO 717-2:2020 Table 3 — impact sound reference values ───
REFERENCE_THIRD_OCTAVE: dict[float, float] = {
    100: 62, 125: 62, 160: 62,
    200: 62, 250: 62, 315: 62,
    400: 61, 500: 60, 630: 59,
    800: 58, 1000: 57, 1250: 54,
    1600: 51, 2000: 48, 2500: 45,
    3150: 42,
}

#: [SPEC] Table 3, octave column. The 125–1000 Hz values equal the energy sum of
#: the corresponding 1/3 octaves (1000 Hz stays at 62 for backwards compatibility),
#: and 2000 Hz is lowered to absorb the contribution of the 3150 Hz 1/3 octave.
REFERENCE_OCTAVE: dict[float, float] = {
    125: 67, 250: 67, 500: 65, 1000: 62, 2000: 49,
}

#: [SPEC] §4.3.1 — 1/3 octave: upper bound on the sum of unfavourable deviations
MAX_DEVIATION_THIRD_OCTAVE = 32.0
#: [SPEC] §4.3.2 — octave: bound of 10.0 dB, and 5 dB is subtracted from the result
MAX_DEVIATION_OCTAVE = 10.0
OCTAVE_RESULT_OFFSET = -5.0

#: [SPEC] §3.1 — the single number is the shifted curve's value at 500 Hz
RATING_FREQUENCY = 500.0


@dataclass(slots=True)
class RatingResult:
    """Everything about the shift — enough to draw the report graph."""

    value: float
    #: How far the reference curve moved, in dB (negative = downwards)
    shift: float
    #: The reference curve after shifting
    reference: Spectrum
    #: The measured spectrum (after rounding)
    measured: Spectrum
    #: Per-band unfavourable deviation (measured − reference, positives only)
    deviations: tuple[float, ...]
    fraction: int
    quantity: str = ""

    @property
    def deviation_sum(self) -> float:
        return float(sum(self.deviations))

    @property
    def worst_band(self) -> float | None:
        """Band with the largest unfavourable deviation — where to improve first."""
        if not any(self.deviations):
            return None
        idx = int(np.argmax(self.deviations))
        return self.measured.centers[idx]

    @property
    def limit(self) -> float:
        return MAX_DEVIATION_THIRD_OCTAVE if self.fraction == 3 else MAX_DEVIATION_OCTAVE

    def table(self) -> list[tuple[float, float, float, float]]:
        """Rows of (midband frequency, measured, reference, deviation)."""
        return list(
            zip(self.measured.centers, self.measured.levels,
                self.reference.levels, self.deviations)
        )

    def summary(self) -> str:
        name = self.quantity or ("Ln,w" if self.fraction == 3 else "Ln,w (octave)")
        lines = [
            f"{name} = {self.value:.0f} dB   "
            f"(curve shifted {self.shift:+.0f} dB, "
            f"deviation sum {self.deviation_sum:.1f}/{self.limit:.0f} dB)"
        ]
        worst = self.worst_band
        if worst is not None:
            lines.append(f"Largest deviation at {worst:g} Hz ({max(self.deviations):.1f} dB)")
        lines.append("  Hz     meas.   ref.   deviation")
        for center, measured, reference, deviation in self.table():
            mark = " ←" if deviation > 0 and center == worst else ""
            lines.append(
                f"  {center:>6g}  {measured:6.1f} {reference:6.1f}   "
                f"{deviation:5.1f}{mark}"
            )
        return "\n".join(lines)


def rate(
    spectrum: Spectrum,
    *,
    fraction: int | None = None,
    quantity: str = "",
    step: float = 1.0,
) -> RatingResult:
    """[SPEC] ISO 717-2:2020 §4.3 — shift the reference curve to a single number.

    Procedure:
      1. Round the measurements to one decimal place (§4.3.1 footnote 1)
      2. Shift the reference curve 1 dB at a time towards the measured curve,
         until the sum of unfavourable deviations is **as large as possible but
         no more than the bound**.
      3. The shifted curve's 500 Hz value is the rating; octave subtracts 5 dB.

    An unfavourable deviation is how far the measurement **exceeds** the
    reference. Favourable differences do not offset anything.

    `step=0.1` gives the 0.1 dB resolution used for uncertainty (§4.3.1).

    ⚠ Common misconception: a measured curve identical to the reference does
    **not** rate 60. Sixteen bands share the 32 dB budget, so an average 2 dB
    excess is allowed and the answer is 58. The octave path (5 bands, 10 dB)
    also gives 58. The reference curve is a shape ruler, not a pass line.
    """
    fraction = spectrum.fraction if fraction is None else fraction
    if fraction == 3:
        centers, table = RATING_THIRD_OCTAVE, REFERENCE_THIRD_OCTAVE
        limit, offset = MAX_DEVIATION_THIRD_OCTAVE, 0.0
    elif fraction == 1:
        centers, table = RATING_OCTAVE, REFERENCE_OCTAVE
        limit, offset = MAX_DEVIATION_OCTAVE, OCTAVE_RESULT_OFFSET
    else:
        raise ValueError("fraction must be 1 or 3")

    selected = spectrum.select(centers)
    measured = round_half_up_1dp(selected.array())
    reference = np.array([table[c] for c in centers], dtype=float)

    shift = _find_shift(measured, reference, limit, step)
    shifted = reference + shift
    deviations = np.maximum(0.0, measured - shifted)

    value = table[RATING_FREQUENCY] + shift + offset
    return RatingResult(
        value=float(value),
        shift=float(shift),
        reference=Spectrum(
            centers=centers,
            levels=tuple(float(v) for v in shifted),
            fraction=fraction,
            label="reference (shifted)",
        ),
        measured=Spectrum(
            centers=centers,
            levels=tuple(float(v) for v in measured),
            fraction=fraction,
            label=selected.label,
        ),
        deviations=tuple(float(v) for v in deviations),
        fraction=fraction,
        quantity=quantity,
    )


def _find_shift(
    measured: np.ndarray, reference: np.ndarray, limit: float, step: float
) -> float:
    """Find the **smallest** shift whose deviation sum is <= limit.

    A smaller shift (a lower reference curve) means a larger deviation sum, so
    "as large as possible but within the bound" is the same as "the smallest
    shift that satisfies it". The sum is monotone, so bisection works.

    Shifts live on a `k * step` integer grid. Repeatedly adding floats gives
    values like 0.30000000000000004 at step=0.1 and the comparisons drift.
    """
    if step <= 0:
        raise ValueError("step must be positive")

    def deviation_sum(k: int) -> float:
        return float(np.sum(np.maximum(0.0, measured - (reference + k * step))))

    # Lifting the curve above the highest measurement gives zero deviation
    k_high = int(np.ceil((measured - reference).max() / step)) + 1
    # Widen exponentially until we find a shift that definitely violates
    span = 1
    while deviation_sum(k_high - span) <= limit:
        span *= 2
        if span > (1 << 30):
            raise RuntimeError("shift search did not converge")
    k_low = k_high - span

    # Smallest satisfying point in (k_low, k_high]
    while k_low < k_high:
        mid = (k_low + k_high) // 2
        if deviation_sum(mid) <= limit:
            k_high = mid
        else:
            k_low = mid + 1
    return round(k_high * step, 10)


# ── Korean standards ────────────────────────────────────────────────
def a_weighted_max_level(broadband_fmax_per_position) -> float:
    """Energy average of per-position maxima (ISO 16283-2 formula 9).

    [SPEC] ISO 16283-2:2020 formula (9) — with the rubber ball excited at
    several positions, Li,Fmax = 10 lg( (1/m) Σ 10^(Li,Fmax,j/10) ).
    Li,Fmax = 10 lg( (1/m) Σ 10^(Li,Fmax,j/10) )

    Feed this the broadband single number per position. Note that ISO 717-2
    obtains that number by adding A-weighting **values** to the band spectrum
    (see `korea.a_weighted_single_number`), not by running an A filter on the
    signal.
    """
    from .bands import energy_average

    values = list(broadband_fmax_per_position)
    if not values:
        raise ValueError("at least one source position is required")
    return energy_average(values)


class KoreanCurveNotConfigured(NotImplementedError):
    """The Korean inverse-A reference curve has not been configured."""


def rate_inverse_a(
    spectrum: Spectrum,
    reference: dict[float, float] | None = None,
    max_deviation: float | None = None,
):
    """Rating against an inverse-A reference curve (KS F 2863).

    ⚠ The curve values are not bundled. The KS text is paywalled, so they could
    **not be verified, and guessing them would risk a wrong test report.**
    Copy the values out of the KS F 2863-1/-2 table and pass them as
    `reference`; the shifting method is identical to ISO.

        rate_inverse_a(spec, reference={63: 78, 125: 68, ...})

    Note that the Korean post-construction scheme moved heavy-impact rating from
    the inverse-A curve to an A-weighted basis, so new measurements usually want
    `korea.a_weighted_single_number()` instead. Check which scheme applies.
    """
    if reference is None:
        raise KoreanCurveNotConfigured(
            "inverse-A curve values were never verified, so they are not bundled. "
            "Pass the values from the KS F 2863 table as the reference argument."
        )
    centers = tuple(sorted(reference))
    selected = spectrum.select(centers)
    measured = round_half_up_1dp(selected.array())
    curve = np.array([reference[c] for c in centers], dtype=float)
    if max_deviation is None:
        # The KS deviation budget is also unverified. ISO values are used as a
        # stand-in, but a different band count needs a different budget — check.
        max_deviation = (
            MAX_DEVIATION_OCTAVE if spectrum.fraction == 1 else MAX_DEVIATION_THIRD_OCTAVE
        )
    limit = max_deviation
    shift = _find_shift(measured, curve, limit, 1.0)
    shifted = curve + shift
    return RatingResult(
        value=float(reference.get(RATING_FREQUENCY, curve[-1]) + shift),
        shift=float(shift),
        reference=Spectrum(centers, tuple(shifted), spectrum.fraction, "inverse-A (shifted)"),
        measured=Spectrum(centers, tuple(measured), spectrum.fraction, selected.label),
        deviations=tuple(float(v) for v in np.maximum(0.0, measured - shifted)),
        fraction=spectrum.fraction,
        quantity="inverse-A single number",
    )
