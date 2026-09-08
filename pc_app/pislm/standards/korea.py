"""Korean rating systems — post-construction (A-weighted) and KS F 2863 (inverse-A).

They serve different purposes, so rather than picking one, **compute both and
put whichever the applicable scheme requires into the report.**

| Scheme | Source | Quantity | Basis |
|---|---|---|---|
| Post-construction | rubber ball | A-weighted Fast maximum | loudness weighted |
| KS F 2863-2 | bang machine / rubber ball | inverse-A curve shifting | low-frequency |
| KS F 2863-1 | tapping machine | inverse-A curve shifting | light impact |
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .bands import Spectrum, a_weighting, energy_sum, exact_center, round_half_up_1dp
from .rating import RatingResult, _find_shift

#: [SPEC] MOLIT post-construction verification scheme (in force 2022-08-04).
#: 49 dB for both light and heavy impact.
POST_VERIFICATION_LIMIT_DB = 49.0

#: [SPEC] ISO 16283-2:2020 §5.2 — rubber ball (heavy), required 1/3-octave bands.
#: Analysis is 1/3 octave; sum up here when a 1/1-octave report is needed.
HEAVY_THIRD_OCTAVE_BANDS = (50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630)
#: For 1/1-octave reporting (energy sum of three 1/3 octaves)
HEAVY_OCTAVE_BANDS = (63, 125, 250, 500)
#: [SPEC] ISO 16283-2:2020 §5.1 — tapping machine (light), required 1/3-octave bands
LIGHT_THIRD_OCTAVE_BANDS = (100, 125, 160, 200, 250, 315, 400, 500, 630, 800,
                            1000, 1250, 1600, 2000, 2500, 3150)

#: [SPEC] Usual order that satisfies IEC 61260-1 Class 1. Passed to `set_bands`.
FILTER_ORDER = 6


def a_weighted_single_number(spectrum: Spectrum) -> float:
    """[SPEC] ISO 717-2 — **add A-weighting values** to the band spectrum and sum.

        L_A = 10 lg Σ 10^((L_i + A_i)/10)

    A_i is the A-weighting at that band's **exact** midband frequency.

    Important: no A-weighting **filter** runs on the signal. Band levels are
    measured with Z (unweighted) and the weighting is added arithmetically, which
    is why the Pi frequency weighting stays at Z. Filtering instead would make the
    band levels themselves weighted, forcing the report spectrum to be un-weighted
    again and risking a double weighting.

    For heavy impact the input is per-band L_i,Fmax so the result is L_iA,Fmax;
    for light impact it is per-band Leq so the result is L_A,eq.
    """
    if spectrum.weighting == "A":
        raise ValueError(
            "this spectrum is already A-weighted — weighting it twice. "
            "Pass the Z spectrum, or call unweighted() first"
        )
    return spectrum.a_weighted().total()


def octave_from_third(spectrum: Spectrum) -> Spectrum:
    """Build a 1/1-octave spectrum by energy-summing 1/3 octaves in threes.

    Analysis stays 1/3 octave; use this when the report needs 1/1 octave.
    An octave is skipped unless all three of its 1/3-octave bands are present.
    """
    members = {
        63: (50, 63, 80), 125: (100, 125, 160), 250: (200, 250, 315),
        500: (400, 500, 630), 1000: (800, 1000, 1250), 2000: (1600, 2000, 2500),
        4000: (3150, 4000, 5000),
    }
    table = spectrum.as_dict()
    out: dict[float, float] = {}
    for octave, thirds in members.items():
        if all(t in table for t in thirds):
            out[octave] = energy_sum([table[t] for t in thirds])
    return Spectrum.from_mapping(
        out, fraction=1, label=spectrum.label,
        quantity=spectrum.quantity, weighting=spectrum.weighting,
    )


def inverse_a_curve(
    bands=HEAVY_THIRD_OCTAVE_BANDS, anchor_band: float = 500.0, anchor_level: float = 50.0
) -> dict[float, float]:
    """[DERIVED] Generate the inverse-A reference curve from the A-weighting.

    ⚠ **The KS F 2863 table values could not be verified.** What follows
    implements the literal definition of "inverse A" — a curve on which every
    band contributes equally after A-weighting. That is, reference value =
    anchor level − (A(f) − A(anchor)).

        L_ref(f) = anchor_level − (A(f) − A(anchor))

    It may differ from the KS table, so compare before issuing a report and use
    `InverseACurve.load()` with the real values if it does.

    Example (anchored at 500 Hz = 50 dB):
        63 Hz  → 50 − (−26.2 − (−3.2)) = 73.0
        125 Hz → 62.9,  250 Hz → 55.4,  500 Hz → 50.0
    """
    bands = tuple(bands)
    fraction = 3 if len(bands) > 6 else 1
    anchor_weight = float(a_weighting(exact_center(anchor_band, fraction)))
    return {
        float(b): round(
            anchor_level - (float(a_weighting(exact_center(b, fraction))) - anchor_weight), 1
        )
        for b in bands
    }


@dataclass(slots=True)
class InverseACurve:
    """One inverse-A curve, carrying its provenance."""

    values: dict[float, float]
    max_deviation: float
    source: str = "generated (unverified)"
    verified: bool = False

    @classmethod
    def legacy_heavy(cls) -> InverseACurve:
        """Published KS F 2863-2 method; see LEGACY_BANG.md for provenance.

        Table and 8 dB procedure checked against the 2013 published test
        report, not a licensed copy of every historical KS edition.
        """
        return cls(values={63.:83.,125.:73.,250.:66.,500.:60.},
                   max_deviation=8., source="KS F 2863-2 legacy; published test report (2013), p.34",
                   verified=False)

    @classmethod
    def generated(
        cls, bands=HEAVY_THIRD_OCTAVE_BANDS, max_deviation: float = 32.0
    ) -> InverseACurve:
        return cls(
            values=inverse_a_curve(bands),
            max_deviation=max_deviation,
            source="generated from A-weighting — not checked against KS",
            verified=False,
        )

    @classmethod
    def load(cls, path: str | Path) -> InverseACurve:
        """Load a curve the user copied out of the KS table.

        JSON format:
            {"values": {"63": 78, "125": 68, "250": 58, "500": 50},
             "max_deviation": 10, "source": "KS F 2863-2 Table 1", "verified": true}
        """
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            values={float(k): float(v) for k, v in data["values"].items()},
            max_deviation=float(data.get("max_deviation", 10.0)),
            source=data.get("source", str(path)),
            verified=bool(data.get("verified", False)),
        )

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(
                {
                    "values": {str(k): v for k, v in self.values.items()},
                    "max_deviation": self.max_deviation,
                    "source": self.source,
                    "verified": self.verified,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    @property
    def bands(self) -> tuple[float, ...]:
        return tuple(sorted(self.values))

    def warning(self) -> str:
        if self.verified:
            return ""
        return (
            f"Inverse-A curve source: {self.source}. "
            "It has not been checked against the KS text — do not put it in a report as is."
        )


def rate_with_curve(spectrum: Spectrum, curve: InverseACurve) -> RatingResult:
    """Inverse-A curve shifting. Same procedure as ISO 717-2, different curve."""
    centers = curve.bands
    if not centers or not np.isfinite(list(curve.values.values())).all():
        raise ValueError("inverse-A reference must be nonempty and finite")
    if not np.isfinite(curve.max_deviation) or curve.max_deviation < 0:
        raise ValueError("invalid inverse-A deviation budget")
    selected = spectrum.select(centers)
    measured = round_half_up_1dp(selected.array())
    if not np.isfinite(measured).all():
        raise ValueError("inverse-A measurements must be finite")
    reference = np.array([curve.values[c] for c in centers], dtype=float)
    shift = _find_shift(measured, reference, curve.max_deviation, 1.0)
    shifted = reference + shift
    anchor = 500.0 if 500.0 in curve.values else centers[-1]
    return RatingResult(
        value=float(curve.values[anchor] + shift),
        shift=float(shift),
        reference=Spectrum(centers, tuple(shifted), spectrum.fraction, "inverse-A (shifted)"),
        measured=Spectrum(centers, tuple(measured), spectrum.fraction, selected.label),
        deviations=tuple(float(v) for v in np.maximum(0.0, measured - shifted)),
        fraction=spectrum.fraction,
        quantity="inverse-A single number",
        deviation_limit=curve.max_deviation,
    )


# ── Both systems at once ────────────────────────────────────────────
@dataclass(slots=True)
class DualEvaluation:
    """Both rating systems applied to the same measurement data."""

    #: Post-construction single number (국토교통부 고시 제2022-868호).
    #: Heavy (impact ball) -> L'iA,Fmax, the A-weighted Fast maximum.
    #: Light (tapping machine) -> L'nT,w, from ISO 717-2 curve shifting.
    #: The two are computed by completely different procedures, so read
    #: `post_verification_symbol` before quoting this number.
    post_verification: float | None = None
    #: The name of the value above, used verbatim in reports
    post_verification_symbol: str = "L'iA,Fmax"
    #: KS F 2863 inverse-A curve shifting
    inverse_a: RatingResult | None = None
    #: ISO 717-2 curve shifting (for international comparison)
    iso_717_2: RatingResult | None = None
    legacy: bool = False
    limit_db: float = POST_VERIFICATION_LIMIT_DB
    warnings: list[str] = field(default_factory=list)

    @property
    def passes(self) -> bool | None:
        """Does it pass the post-construction limit? None when unknown."""
        if self.legacy or self.post_verification is None:
            return None
        return self.post_verification <= self.limit_db

    def summary(self) -> str:
        lines = []
        if self.post_verification is not None:
            verdict = "PASS" if self.passes else "FAIL"
            label = f"Post-construction {self.post_verification_symbol}"
            lines.append(
                f"{label:<24}: {self.post_verification:.1f} dB  "
                f"[limit {self.limit_db:.0f} dB -> {verdict}]"
            )
        if self.inverse_a is not None:
            label = "Legacy bang L'i,Fmax,AW" if self.legacy else "KS F 2863 (inverse-A)"
            lines.append(f"{label:<24}: {self.inverse_a.value:.0f} dB")
        if self.iso_717_2 is not None:
            lines.append(f"{'ISO 717-2 (reference)':<24}: {self.iso_717_2.value:.0f} dB")
        for warning in self.warnings:
            lines.append(f"  ⚠ {warning}")
        return "\n".join(lines) or "nothing to evaluate"
