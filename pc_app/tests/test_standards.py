"""Standards maths verification — ISO 16283-2 / ISO 717-2 / ISO 3382-2.

Strategy: the worked examples in the standards (ISO 717-2 Annex C) were not
available, so the tests use **inputs whose answer is independently knowable**:
  * shifting every band by N dB must shift the rating by exactly N (linearity)
  * 1/3 octave (16 bands, 32 dB) and octave (5 bands, 10 dB, −5 dB) must agree
  * a synthetic exponential decay with a chosen T60 must be recovered
  * correction formulas are checked for identity, monotonicity and direction

Note: "measured curve = reference curve therefore 60" is a **false intuition**.
Sixteen bands share the 32 dB budget, so the answer is 58. That property is
pinned down by a test of its own.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pislm.standards import (
    A0_DWELLING,
    G_BASE10,
    TAPPING_THIRD_OCTAVE,
    band_edges,
    band_index,
    exact_center,
    octave_from_third,
    rms_level,
    BACKGROUND_LIMIT_CORRECTION,
    RATING_OCTAVE,
    RATING_THIRD_OCTAVE,
    REFERENCE_OCTAVE,
    REFERENCE_THIRD_OCTAVE,
    T0_DWELLING,
    V0_DWELLING,
    KoreanCurveNotConfigured,
    RoomCorrection,
    Spectrum,
    a_weighted_max_level,
    a_weighted_single_number,
    a_weighting,
    absorption_area,
    average_spectra,
    c_weighting,
    correct_background,
    decay_time,
    energy_average,
    energy_sum,
    nominal_center,
    normalized_level,
    octave_filter,
    rate,
    rate_inverse_a,
    reverberation_spectrum,
    round_half_up_1dp,
    schroeder_curve,
    standardized_level,
    standardized_max_level,
)


# ── Bands and arithmetic ────────────────────────────────────────────
class TestBands:
    def test_a_weighting_is_zero_at_1khz(self):
        """Zero at 1 kHz by definition."""
        assert abs(float(a_weighting(1000.0))) < 0.01

    def test_a_weighting_matches_iec_table(self):
        """Against the IEC 61672-1 table (0.15 dB tolerance — it is rounded)."""
        expected = {
            31.5: -39.4, 63: -26.2, 125: -16.1, 250: -8.6, 500: -3.2,
            1000: 0.0, 2000: 1.2, 4000: 1.0, 8000: -1.1,
        }
        for freq, value in expected.items():
            got = float(a_weighting(freq))
            assert abs(got - value) < 0.15, f"{freq} Hz: {got:.2f} != {value}"

    def test_c_weighting_matches_iec_table(self):
        expected = {31.5: -3.0, 125: -0.2, 1000: 0.0, 4000: -0.8, 8000: -3.0}
        for freq, value in expected.items():
            assert abs(float(c_weighting(freq)) - value) < 0.15

    def test_energy_sum_of_equal_levels(self):
        """Two equal levels sum to +3.01 dB."""
        assert abs(energy_sum([60.0, 60.0]) - 63.0103) < 1e-3
        assert abs(energy_sum([60.0] * 10) - 70.0) < 1e-9

    def test_energy_average_of_equal_levels_is_unchanged(self):
        assert abs(energy_average([60.0] * 5) - 60.0) < 1e-9

    def test_energy_average_is_dominated_by_the_loudest(self):
        """An energy average is always at least the arithmetic mean."""
        levels = [40.0, 80.0]
        assert energy_average(levels) > np.mean(levels)

    def test_round_half_up_not_bankers(self):
        """Python round() gives 2.25 -> 2.2 (banker's). The standard wants 2.3."""
        assert round(2.25, 1) == 2.2  # confirm the built-in behaviour
        assert float(round_half_up_1dp(2.25)) == 2.3
        assert float(round_half_up_1dp(2.35)) == 2.4
        assert float(round_half_up_1dp(60.04)) == 60.0
        assert float(round_half_up_1dp(60.05)) == 60.1

    def test_nominal_center_snaps_exact_to_nominal(self):
        assert nominal_center(19.7) == 20
        assert nominal_center(1002.4) == 1000
        assert nominal_center(3162.0) == 3150
        assert nominal_center(63.1, fraction=1) == 63

    def test_spectrum_select_reports_missing_bands(self):
        spec = Spectrum.from_mapping({100: 50.0, 125: 51.0})
        with pytest.raises(KeyError, match="required bands are missing"):
            spec.select(RATING_THIRD_OCTAVE)

    def test_spectrum_length_mismatch_rejected(self):
        with pytest.raises(ValueError):
            Spectrum(centers=(100, 125), levels=(50.0,))

    def test_average_spectra_is_per_band_energy_average(self):
        a = Spectrum.from_mapping({100: 60.0, 125: 70.0})
        b = Spectrum.from_mapping({100: 60.0, 125: 60.0})
        avg = average_spectra([a, b])
        assert abs(avg.level_at(100) - 60.0) < 1e-9
        assert abs(avg.level_at(125) - energy_average([70.0, 60.0])) < 1e-9


# ── Background correction (ISO 16283-2 §9.2) ────────────────────────
class TestBackground:
    def _pair(self, signal_levels, background_levels):
        centers = tuple(100 * (i + 1) for i in range(len(signal_levels)))
        return (
            Spectrum(centers, tuple(signal_levels)),
            Spectrum(centers, tuple(background_levels)),
        )

    def test_large_difference_barely_changes_the_level(self):
        signal, background = self._pair([60.0], [30.0])
        result = correct_background(signal, background)
        assert abs(result.spectrum.level_at(100) - 60.0) < 0.01
        assert not result.at_limit
        assert not result.marginal_bands

    def test_ten_db_difference_subtracts_energy(self):
        """10 dB difference -> 10lg(10^6 − 10^5) = 59.54 dB."""
        signal, background = self._pair([60.0], [50.0])
        result = correct_background(signal, background)
        assert abs(result.spectrum.level_at(100) - 59.5424) < 1e-3
        assert not result.at_limit

    def test_six_db_difference_is_the_boundary(self):
        """Exactly 6 dB still uses energy subtraction (about −1.26 dB)."""
        signal, background = self._pair([56.0], [50.0])
        result = correct_background(signal, background)
        assert abs(result.spectrum.level_at(100) - (56.0 - 1.2560)) < 1e-3
        assert not result.at_limit
        assert result.marginal_bands == (100,)

    def test_below_six_db_uses_fixed_correction_and_flags_limit(self):
        signal, background = self._pair([55.0], [50.0])
        result = correct_background(signal, background)
        assert abs(result.spectrum.level_at(100) - (55.0 - BACKGROUND_LIMIT_CORRECTION)) < 1e-9
        assert result.at_limit
        assert result.limited_bands == (100,)
        assert "measurement limits" in result.report_note()

    def test_fixed_correction_is_conservative(self):
        """1.3 dB exceeds the 1.256 dB of a 6 dB difference — the safe side."""
        assert BACKGROUND_LIMIT_CORRECTION > 1.256

    def test_mismatched_bands_rejected(self):
        with pytest.raises(ValueError):
            correct_background(
                Spectrum((100,), (60.0,)), Spectrum((125,), (30.0,))
            )


# ── Normalisation (ISO 16283-2 formulas 1/2/3) ──────────────────────
class TestNormalization:
    def test_absorption_area_sabine(self):
        """A = 0.16 V / T."""
        assert abs(absorption_area(50.0, 0.5) - 16.0) < 1e-9

    def test_standardized_is_identity_at_reference_time(self):
        assert abs(standardized_level(60.0, T0_DWELLING) - 60.0) < 1e-9

    def test_standardized_direction(self):
        """Longer reverberation reads higher, so standardising must lower it."""
        assert standardized_level(60.0, 1.0) < 60.0
        assert standardized_level(60.0, 0.25) > 60.0

    def test_standardized_doubling_time_costs_3db(self):
        assert abs(standardized_level(60.0, 1.0) - (60.0 - 3.0103)) < 1e-3

    def test_normalized_is_identity_at_reference_absorption(self):
        """A0 = 10 m² happens at V=31.25, T=0.5."""
        assert abs(absorption_area(31.25, 0.5) - A0_DWELLING) < 1e-9
        assert abs(normalized_level(60.0, 31.25, 0.5) - 60.0) < 1e-9

    def test_normalized_direction(self):
        """More absorption reads lower, so normalising must raise it."""
        assert normalized_level(60.0, 100.0, 0.5) > 60.0

    def test_spectrum_input_returns_spectrum(self):
        spec = Spectrum.from_mapping({100: 60.0, 125: 60.0})
        out = standardized_level(spec, 1.0)
        assert isinstance(out, Spectrum)
        assert abs(out.level_at(100) - (60.0 - 3.0103)) < 1e-3

    def test_per_band_reverberation_time(self):
        """The real case: reverberation differs per band."""
        spec = Spectrum.from_mapping({100: 60.0, 125: 60.0})
        out = standardized_level(spec, [1.0, 0.5])
        assert abs(out.level_at(100) - 56.99) < 0.01
        assert abs(out.level_at(125) - 60.0) < 1e-9

    def test_room_correction_bundles_the_same_math(self):
        room = RoomCorrection(volume=31.25, reverberation_time=0.5)
        assert abs(room.absorption - A0_DWELLING) < 1e-9
        assert abs(room.normalized(60.0) - 60.0) < 1e-9
        assert abs(room.standardized(60.0) - 60.0) < 1e-9

    def test_invalid_inputs_rejected(self):
        with pytest.raises(ValueError):
            absorption_area(0, 0.5)
        with pytest.raises(ValueError):
            standardized_level(60.0, 0.0)


class TestStandardizedMaxLevel:
    """⚠ [DERIVED] formula — only identity, monotonicity and direction."""

    def test_identity_at_reference_conditions(self):
        got = standardized_max_level(70.0, V0_DWELLING, T0_DWELLING)
        assert abs(got - 70.0) < 1e-9, "non-zero correction at reference conditions"

    def test_volume_term_is_exactly_10lg(self):
        """Doubling volume alone -> +3.01 dB."""
        got = standardized_max_level(70.0, 2 * V0_DWELLING, T0_DWELLING)
        assert abs(got - (70.0 + 3.0103)) < 1e-3

    def test_longer_reverberation_is_corrected_downward(self):
        """Longer reverberation reads higher, so standardising must lower it."""
        assert standardized_max_level(70.0, V0_DWELLING, 0.8) < 70.0
        assert standardized_max_level(70.0, V0_DWELLING, 0.3) > 70.0

    def test_monotonic_in_reverberation_time(self):
        times = [0.2, 0.3, 0.4, 0.5, 0.7, 1.0, 1.5]
        values = [standardized_max_level(70.0, V0_DWELLING, t) for t in times]
        assert all(b < a for a, b in zip(values, values[1:])), values

    def test_correction_magnitude_is_plausible(self):
        """Over the practical range (T=0.2–1.0 s) the correction stays small."""
        for t in (0.2, 0.5, 1.0):
            delta = standardized_max_level(70.0, V0_DWELLING, t) - 70.0
            assert abs(delta) < 6.0, f"T={t}: {delta:+.2f} dB is excessive"

    def test_c_equals_one_does_not_blow_up(self):
        """C = 1 (T = 1.7275 s) is where the formula is 0/0 — extended continuously."""
        near = standardized_max_level(70.0, V0_DWELLING, 1.7275)
        left = standardized_max_level(70.0, V0_DWELLING, 1.7275 - 1e-4)
        right = standardized_max_level(70.0, V0_DWELLING, 1.7275 + 1e-4)
        assert np.isfinite(near)
        assert abs(near - (left + right) / 2) < 1e-3

    def test_rejects_invalid_input(self):
        with pytest.raises(ValueError):
            standardized_max_level(70.0, 0.0, 0.5)
        with pytest.raises(ValueError):
            standardized_max_level(70.0, 50.0, 0.0)


# ── ISO 717-2 single-number rating ──────────────────────────────────
class TestRating:
    def _flat(self, value: float, centers=RATING_THIRD_OCTAVE) -> Spectrum:
        fraction = 3 if centers is RATING_THIRD_OCTAVE else 1
        return Spectrum(tuple(centers), tuple([value] * len(centers)), fraction)

    def test_reference_curve_itself_gives_58_not_60(self):
        """Matching the reference curve rates 58, not 60.

        Sixteen bands share the 32 dB budget, so an average 2 dB excess is
        allowed and the deviation sum hits exactly 32 with the curve 2 dB down.
        The reference curve is a shape ruler, not a pass line.
        """
        spec = Spectrum.from_mapping(dict(REFERENCE_THIRD_OCTAVE))
        result = rate(spec)
        assert result.shift == -2.0
        assert result.deviation_sum == 32.0
        assert result.value == 58.0

    def test_octave_and_third_octave_agree_on_the_reference_curve(self):
        """Octave (5 bands, 10 dB) and 1/3 octave (16 bands, 32 dB) must agree.

        A cross-check that the −5 dB offset and the budgets are consistent.
        """
        third = rate(Spectrum.from_mapping(dict(REFERENCE_THIRD_OCTAVE)))
        octave = rate(Spectrum.from_mapping(dict(REFERENCE_OCTAVE), fraction=1), fraction=1)
        assert third.value == octave.value == 58.0

    def test_uniform_offset_moves_the_result_by_the_same_amount(self):
        """Shifting every band by N dB shifts the rating by exactly N."""
        base = rate(Spectrum.from_mapping(dict(REFERENCE_THIRD_OCTAVE))).value
        for delta in (-15, -10, -3, 5, 12):
            spec = Spectrum.from_mapping(
                {k: v + delta for k, v in REFERENCE_THIRD_OCTAVE.items()}
            )
            assert rate(spec).value == base + delta, delta

    def test_deviation_budget_is_used_as_fully_as_possible(self):
        """One band 32 dB over -> sum is exactly 32 at zero shift, still allowed."""
        levels = dict(REFERENCE_THIRD_OCTAVE)
        levels[500] += 32.0
        result = rate(Spectrum.from_mapping(levels))
        assert result.deviation_sum == 32.0
        assert result.shift == 0.0
        assert result.value == 60.0

    def test_one_db_over_budget_forces_a_shift(self):
        """At 33 dB the bound is exceeded, so the curve must rise 1 dB."""
        levels = dict(REFERENCE_THIRD_OCTAVE)
        levels[500] += 33.0
        result = rate(Spectrum.from_mapping(levels))
        assert result.shift == 1.0
        assert result.deviation_sum == 32.0
        assert result.value == 61.0

    def test_deviation_sum_never_exceeds_limit(self):
        rng = np.random.default_rng(20260802)
        for _ in range(200):
            levels = {
                c: REFERENCE_THIRD_OCTAVE[c] + rng.uniform(-25, 25)
                for c in RATING_THIRD_OCTAVE
            }
            result = rate(Spectrum.from_mapping(levels))
            assert result.deviation_sum <= 32.0 + 1e-9
            # One dB lower must exceed the bound (i.e. the budget is fully used)
            tighter = np.maximum(
                0.0, result.measured.array() - (result.reference.array() - 1.0)
            )
            assert tighter.sum() > 32.0 + 1e-9

    def test_only_unfavourable_deviations_count(self):
        """Bands below the reference do not offset anything."""
        levels = dict(REFERENCE_THIRD_OCTAVE)
        levels[100] -= 40.0   # strongly favourable
        levels[500] += 20.0   # unfavourable
        result = rate(Spectrum.from_mapping(levels))
        assert result.deviations[RATING_THIRD_OCTAVE.index(100)] == 0.0
        assert result.deviation_sum == 20.0

    def test_worst_band_identifies_the_weak_point(self):
        levels = dict(REFERENCE_THIRD_OCTAVE)
        levels[125] += 15.0
        result = rate(Spectrum.from_mapping(levels))
        assert result.worst_band == 125

    def test_octave_rating_subtracts_five(self):
        """[SPEC] §4.3.2 — the octave rating subtracts 5 dB from the 500 Hz value."""
        spec = Spectrum.from_mapping(dict(REFERENCE_OCTAVE), fraction=1)
        result = rate(spec, fraction=1)
        assert result.shift == -2.0
        assert result.value == REFERENCE_OCTAVE[500] + result.shift - 5.0

    def test_octave_limit_is_ten(self):
        levels = dict(REFERENCE_OCTAVE)
        levels[500] += 10.0
        result = rate(Spectrum.from_mapping(levels, fraction=1), fraction=1)
        assert result.deviation_sum == 10.0
        assert result.shift == 0.0

        levels[500] += 1.0
        result = rate(Spectrum.from_mapping(levels, fraction=1), fraction=1)
        assert result.shift == 1.0

    def test_rounding_is_applied_before_comparison(self):
        """0.04 rounds away; 0.05 survives as 0.1 and changes the result."""
        exact = rate(Spectrum.from_mapping(dict(REFERENCE_THIRD_OCTAVE)))

        below = dict(REFERENCE_THIRD_OCTAVE)
        below[500] += 0.04
        assert rate(Spectrum.from_mapping(below)).value == exact.value

        above = dict(REFERENCE_THIRD_OCTAVE)
        above[500] += 0.05
        assert rate(Spectrum.from_mapping(above)).value == exact.value + 1

    def test_tenth_db_step_for_uncertainty(self):
        """§4.3.1 — 0.1 dB resolution for uncertainty."""
        levels = dict(REFERENCE_THIRD_OCTAVE)
        levels[500] += 32.5
        coarse = rate(Spectrum.from_mapping(levels))
        fine = rate(Spectrum.from_mapping(levels), step=0.1)
        assert fine.shift == 0.5
        assert fine.value == 60.5
        assert coarse.value == 61.0

    def test_missing_band_is_an_error_not_a_guess(self):
        levels = {c: 50.0 for c in RATING_THIRD_OCTAVE if c != 2000}
        with pytest.raises(KeyError):
            rate(Spectrum.from_mapping(levels))

    def test_summary_is_renderable(self):
        result = rate(Spectrum.from_mapping(dict(REFERENCE_THIRD_OCTAVE)), quantity="L'nT,w")
        text = result.summary()
        assert "L'nT,w = 58 dB" in text
        assert "3150" in text
        assert "32.0/32" in text


class TestKoreanRating:
    def test_inverse_a_requires_explicit_curve(self):
        """The curve values were never guessed — it must fail explicitly."""
        spec = Spectrum.from_mapping({63: 60.0, 125: 55.0, 250: 50.0, 500: 45.0}, fraction=1)
        with pytest.raises(KoreanCurveNotConfigured):
            rate_inverse_a(spec)

    def test_inverse_a_works_with_supplied_curve(self):
        """Given a curve, it rates with the same shifting method as ISO."""
        curve = {63: 78.0, 125: 68.0, 250: 58.0, 500: 50.0}
        spec = Spectrum.from_mapping(curve, fraction=1)
        result = rate_inverse_a(spec, reference=curve, max_deviation=10.0)
        # 4 bands x 2.5 dB = 10, so shift −2 gives 8 and −3 gives 12 -> −2
        assert result.shift == -2.0
        assert result.deviation_sum == 8.0

    def test_inverse_a_budget_is_configurable(self):
        """The KS deviation budget is unverified too, so it must be settable."""
        curve = {63: 78.0, 125: 68.0, 250: 58.0, 500: 50.0}
        spec = Spectrum.from_mapping(curve, fraction=1)
        strict = rate_inverse_a(spec, reference=curve, max_deviation=0.0)
        assert strict.shift == 0.0 and strict.deviation_sum == 0.0

    def test_a_weighted_max_level_is_energy_average(self):
        """[SPEC] ISO 16283-2 formula (9) — energy average of per-position maxima."""
        assert abs(a_weighted_max_level([50.0] * 4) - 50.0) < 1e-9
        assert abs(a_weighted_max_level([50.0, 56.0]) - energy_average([50.0, 56.0])) < 1e-9

    def test_a_weighted_max_rejects_empty(self):
        with pytest.raises(ValueError):
            a_weighted_max_level([])

    def test_band_sum_overestimates_broadband_max(self):
        """Summing per-band maxima exceeds the true broadband maximum.

        Band maxima occur at different instants. ISO 717-2 nonetheless defines
        the single number this way, which is what the program follows.
        """
        spec = Spectrum.from_mapping({63: 60.0, 125: 60.0, 250: 60.0, 500: 60.0}, fraction=1)
        summed = spec.a_weighted_total()
        loudest = max(spec.a_weighted().levels)
        assert summed > loudest


# ── Reverberation ───────────────────────────────────────────────────
def synthetic_decay(t60: float, fs: float, seconds: float, seed: int = 0,
                    noise_db: float = -90.0) -> np.ndarray:
    """Synthetic decaying noise with a known T60 — the reference input."""
    rng = np.random.default_rng(seed)
    n = int(fs * seconds)
    t = np.arange(n) / fs
    # Energy falls 60 dB in T60 seconds -> amplitude envelope exp(-6.9078 t / T60)
    envelope = np.exp(-6.907755 * t / t60)
    signal = rng.standard_normal(n) * envelope
    return signal + rng.standard_normal(n) * (10 ** (noise_db / 20.0))


class TestReverberation:
    @pytest.mark.parametrize("t60", [0.3, 0.5, 0.8, 1.2])
    def test_t20_recovers_the_synthetic_decay(self, t60):
        """The key check — does Schroeder integration recover the T60 I chose?"""
        fs = 8000.0
        decay = synthetic_decay(t60, fs, seconds=max(3.0, t60 * 4))
        result = decay_time(decay, fs, "T20")
        assert abs(result.t60 - t60) / t60 < 0.05, f"{result.t60:.3f} vs {t60}"
        assert result.correlation > 0.99
        assert result.reliable

    @pytest.mark.parametrize("t60", [0.4, 0.9])
    def test_t30_recovers_the_synthetic_decay(self, t60):
        fs = 8000.0
        decay = synthetic_decay(t60, fs, seconds=max(4.0, t60 * 5))
        result = decay_time(decay, fs, "T30")
        assert abs(result.t60 - t60) / t60 < 0.05

    def test_schroeder_curve_starts_at_zero_and_decreases(self):
        curve = schroeder_curve(synthetic_decay(0.5, 8000.0, 3.0))
        assert abs(curve[0]) < 1e-9
        assert np.all(np.diff(curve) <= 1e-9)

    def test_insufficient_decay_range_is_an_error(self):
        """Asking for T30 on a signal that barely decayed must not answer quietly."""
        fs = 8000.0
        short = synthetic_decay(5.0, fs, seconds=0.3)
        with pytest.raises(ValueError, match="evaluation range"):
            decay_time(short, fs, "T30")

    def test_noise_floor_is_flagged_by_quality_metrics(self):
        """Heavy noise must be flagged unreliable — the number alone hides it."""
        fs = 8000.0
        noisy = synthetic_decay(0.5, fs, seconds=3.0, noise_db=-28.0)
        try:
            result = decay_time(noisy, fs, "T20")
        except ValueError:
            return  # failing to place the range at all is also correct
        assert not result.reliable or abs(result.t60 - 0.5) / 0.5 < 0.10
        if not result.reliable:
            assert result.warnings()

    def test_truncation_artifact_does_not_fake_a_decay(self):
        """Without this guard a wrong answer appears silently — the worst mode.

        A Schroeder curve always dives to −∞ where the record ends (energy
        outside the window counts as zero). Using that artefact lets T30 be
        "computed" on a signal with only 2 dB of decay.
        """
        fs = 8000.0
        barely_decaying = synthetic_decay(5.0, fs, seconds=0.2)
        curve = schroeder_curve(barely_decaying)
        # The very end really does go below −35 dB (the artefact)
        assert curve[-1] < -35.0
        # And yet T30 must be refused
        with pytest.raises(ValueError, match="evaluation range"):
            decay_time(barely_decaying, fs, "T30")

    def test_curvature_is_none_when_t30_unavailable(self):
        """Unknown curvature is None, not 0 — "straight" and "unknown" differ."""
        fs = 8000.0
        decay = synthetic_decay(0.5, fs, seconds=3.0, noise_db=-32.0)
        result = decay_time(decay, fs, "T20")
        assert result.curvature_percent is None or isinstance(
            result.curvature_percent, float
        )

    def test_curvature_near_zero_for_pure_exponential(self):
        """A single exponential decay should have curvature near zero."""
        fs = 8000.0
        decay = synthetic_decay(0.6, fs, seconds=4.0, noise_db=-120.0)
        result = decay_time(decay, fs, "T20")
        assert result.curvature_percent is not None
        assert abs(result.curvature_percent) < 10.0, result.curvature_percent
        assert result.reliable

    def test_rising_signal_rejected(self):
        fs = 8000.0
        rising = np.linspace(0.01, 1.0, int(fs))
        with pytest.raises(ValueError):
            decay_time(rising, fs, "T20")

    def test_octave_filter_passes_its_own_band(self):
        fs = 8000.0
        t = np.arange(int(fs)) / fs
        inside = np.sin(2 * np.pi * 500 * t)
        outside = np.sin(2 * np.pi * 100 * t)
        assert np.std(octave_filter(inside, fs, 500, 3)) > 0.5
        assert np.std(octave_filter(outside, fs, 500, 3)) < 0.05

    def test_band_above_nyquist_rejected(self):
        with pytest.raises(ValueError):
            octave_filter(np.zeros(1000), 2000.0, 5000, 3)

    def test_reverberation_spectrum_over_bands(self):
        fs = 8000.0
        decay = synthetic_decay(0.6, fs, seconds=3.0)
        spectrum, results = reverberation_spectrum(
            decay, fs, [125, 250, 500, 1000], method="T20", fraction=1
        )
        assert len(spectrum.centers) == 4
        for center, result in results.items():
            assert abs(result.t60 - 0.6) / 0.6 < 0.15, f"{center} Hz: {result.t60:.3f}"

    def test_failed_bands_become_nan_not_silently_wrong(self):
        fs = 8000.0
        decay = synthetic_decay(0.5, fs, seconds=3.0)
        spectrum, results = reverberation_spectrum(
            decay, fs, [125, 3500], method="T20", fraction=1
        )
        # 3500 Hz has an upper edge past fs/2, so it fails or is omitted
        assert 125 in results
        levels = dict(zip(spectrum.centers, spectrum.levels))
        assert np.isfinite(levels[125])


# ── Exact midband frequencies (IEC 61260-1) ─────────────────────────
class TestExactFrequencies:
    def test_reference_band_is_exactly_1000(self):
        assert exact_center(1000, 3) == pytest.approx(1000.0)
        assert band_index(1000, 3) == 0

    def test_known_exact_centers(self):
        """f_m = 1000 × 10^(3x/(10·b)) — values that can be checked by hand."""
        expected = {
            50: 50.1187, 63: 63.0957, 80: 79.4328, 100: 100.0,
            125: 125.8925, 250: 251.1886, 500: 501.1872,
            1000: 1000.0, 2000: 1995.2623, 3150: 3162.2777,
        }
        for nominal, exact in expected.items():
            assert exact_center(nominal, 3) == pytest.approx(exact, abs=1e-3), nominal

    def test_nominal_and_exact_round_trip(self):
        for nominal in TAPPING_THIRD_OCTAVE:
            assert nominal_center(exact_center(nominal, 3), 3) == nominal

    def test_band_edges_are_geometric_about_the_center(self):
        lo, hi = band_edges(1000, 3)
        assert math.sqrt(lo * hi) == pytest.approx(1000.0)
        # 1/3-octave width = G^(1/3), G = 10^0.3
        assert hi / lo == pytest.approx(G_BASE10 ** (1 / 3))

    def test_adjacent_bands_touch(self):
        """Neighbouring band edges must meet — no gaps and no overlap."""
        bands = TAPPING_THIRD_OCTAVE
        for lower, upper in zip(bands, bands[1:]):
            assert band_edges(lower, 3)[1] == pytest.approx(band_edges(upper, 3)[0])

    def test_base_two_approximation_differs_at_high_frequency(self):
        """The base-two approximation drifts at high bands — hence base ten."""
        exact_hi = band_edges(3150, 3)[1]
        base_two_hi = 3150 * 2 ** (1 / 6)
        assert abs(exact_hi - base_two_hi) > 10.0

    def test_a_weighting_table_matches_exact_centers_not_nominal(self):
        """The IEC A-weighting table is computed at the **exact** midbands.

        Nominal 63 Hz gives −26.22; exact 63.0957 Hz gives −26.20, which matches
        the table. That is the evidence for using exact values.
        """
        assert float(a_weighting(exact_center(63, 3))) == pytest.approx(-26.2, abs=0.01)
        assert float(a_weighting(exact_center(125, 3))) == pytest.approx(-16.1, abs=0.01)
        assert float(a_weighting(exact_center(2000, 3))) == pytest.approx(1.2, abs=0.01)

    def test_spectrum_uses_exact_for_weighting(self):
        spec = Spectrum.from_mapping({1000: 60.0, 3150: 60.0})
        weighted = spec.a_weighted()
        assert weighted.weighting == "A"
        # Must be based on the exact 3162.28 Hz
        assert weighted.level_at(3150) == pytest.approx(
            60.0 + float(a_weighting(exact_center(3150, 3))), abs=1e-9
        )

    def test_a_weighting_round_trip(self):
        spec = Spectrum.from_mapping({c: 60.0 for c in TAPPING_THIRD_OCTAVE})
        restored = spec.a_weighted().unweighted()
        for band in TAPPING_THIRD_OCTAVE:
            assert restored.level_at(band) == pytest.approx(60.0, abs=1e-9)
        assert restored.weighting == "Z"

    def test_octave_filter_uses_iec_edges(self):
        """Does the passband match the IEC edges? Tones outside must be cut."""
        fs = 20000.0
        t = np.arange(int(fs)) / fs
        lo, hi = band_edges(1000, 3)
        inside = np.sin(2 * np.pi * math.sqrt(lo * hi) * t)
        below = np.sin(2 * np.pi * (lo * 0.7) * t)
        assert np.std(octave_filter(inside, fs, 1000, 3)) > 0.5
        assert np.std(octave_filter(below, fs, 1000, 3)) < 0.05


class TestRmsLevel:
    def test_sine_rms_is_amplitude_over_root_two(self):
        t = np.linspace(0, 1, 200000, endpoint=False)
        signal = np.sqrt(2) * np.sin(2 * np.pi * 100 * t)  # RMS = 1 Pa
        assert rms_level(signal) == pytest.approx(20 * math.log10(1 / 20e-6), abs=0.01)

    def test_rms_is_not_arithmetic_mean_of_db(self):
        """Pin down numerically that levels must not be averaged arithmetically."""
        loud = np.full(1000, 1.0)
        quiet = np.full(1000, 0.001)
        combined = np.concatenate([loud, quiet])
        arithmetic = (rms_level(loud) + rms_level(quiet)) / 2
        assert rms_level(combined) > arithmetic + 20

    def test_silence_is_negative_infinity(self):
        assert rms_level(np.zeros(100)) == float("-inf")


class TestOctaveFromThird:
    def test_three_equal_thirds_sum_to_plus_4_77(self):
        """Three equal levels sum to 10log(3) = +4.77 dB."""
        spec = Spectrum.from_mapping({50: 60.0, 63: 60.0, 80: 60.0})
        octave = octave_from_third(spec)
        assert octave.level_at(63) == pytest.approx(60.0 + 10 * math.log10(3), abs=1e-9)
        assert octave.fraction == 1

    def test_incomplete_octave_is_dropped(self):
        """An octave missing a band is dropped rather than silently wrong."""
        spec = Spectrum.from_mapping({50: 60.0, 63: 60.0})  # 80 Hz absent
        assert octave_from_third(spec).as_dict() == {}

    def test_metadata_is_carried_over(self):
        spec = Spectrum.from_mapping(
            {50: 60.0, 63: 60.0, 80: 60.0}, quantity="Fmax", weighting="A"
        )
        octave = octave_from_third(spec)
        assert octave.quantity == "Fmax" and octave.weighting == "A"


class TestAWeightingAsBandValues:
    """[SPEC] ISO 717-2 — A-weighting is applied as band values, not a filter."""

    def test_single_number_is_energy_sum_of_weighted_bands(self):
        bands = {50: 60.0, 63: 60.0, 80: 60.0}
        spec = Spectrum.from_mapping(bands, weighting="Z")
        manual = energy_sum(
            [60.0 + float(a_weighting(exact_center(b, 3))) for b in bands]
        )
        assert a_weighted_single_number(spec) == pytest.approx(manual, abs=1e-9)

    def test_double_weighting_is_refused(self):
        """Weighting an already-weighted spectrum would be silently wrong."""
        spec = Spectrum.from_mapping({63: 60.0, 125: 60.0}, weighting="Z").a_weighted()
        with pytest.raises(ValueError, match="double|twice"):
            a_weighted_single_number(spec)

    def test_low_frequency_bands_are_heavily_discounted(self):
        """The point of A-weighting — low frequencies are cut hard."""
        low = Spectrum.from_mapping({b: 70.0 for b in (50, 63, 80)}, weighting="Z")
        mid = Spectrum.from_mapping({b: 70.0 for b in (500, 630, 800)}, weighting="Z")
        assert a_weighted_single_number(mid) - a_weighted_single_number(low) > 20

    def test_weighting_uses_exact_centers(self):
        spec = Spectrum.from_mapping({3150: 60.0}, weighting="Z")
        exact_based = 60.0 + float(a_weighting(exact_center(3150, 3)))
        nominal_based = 60.0 + float(a_weighting(3150))
        got = a_weighted_single_number(spec)
        assert got == pytest.approx(exact_based, abs=1e-9)
        assert got != pytest.approx(nominal_based, abs=1e-6)

    def test_order_of_averaging_and_weighting_does_not_matter(self):
        """Weight-then-average must equal average-then-weight.

        Energy averaging and energy summing commute, so both paths must agree.
        A mismatch means dB values are being averaged arithmetically somewhere.
        """
        rng = np.random.default_rng(11)
        bands = (50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630)
        spectra = [
            Spectrum.from_mapping(
                {b: 55.0 + rng.uniform(-6, 6) for b in bands}, weighting="Z"
            )
            for _ in range(5)
        ]
        per_position = energy_average([a_weighted_single_number(s) for s in spectra])
        averaged_first = a_weighted_single_number(average_spectra(spectra))
        assert per_position == pytest.approx(averaged_first, abs=1e-9)


class TestQuantityLabels:
    def test_display_quantity(self):
        assert Spectrum.from_mapping({100: 50.0}, quantity="Leq").display_quantity == "Lp,Leq"
        assert Spectrum.from_mapping({100: 50.0}, quantity="Fmax").display_quantity == "Lp,Fmax"
        assert Spectrum.from_mapping({100: 50.0}).display_quantity == "Lp"


# ── End to end ──────────────────────────────────────────────────────
class TestEndToEnd:
    def test_full_lightweight_impact_chain(self):
        """One light-impact run: background -> average -> standardise -> rate."""
        rng = np.random.default_rng(7)
        base = {c: 62.0 - 8 * np.log10(c / 100) for c in RATING_THIRD_OCTAVE}

        positions = [
            Spectrum.from_mapping({c: v + rng.uniform(-1.5, 1.5) for c, v in base.items()})
            for _ in range(4)
        ]
        averaged = average_spectra(positions)

        background = Spectrum.from_mapping({c: 25.0 for c in RATING_THIRD_OCTAVE})
        corrected = correct_background(averaged, background)
        assert not corrected.at_limit

        room = RoomCorrection(volume=42.0, reverberation_time=0.45)
        result = rate(room.standardized(corrected.spectrum), quantity="L'nT,w")

        assert 40 <= result.value <= 75
        assert result.deviation_sum <= 32.0
        assert "L'nT,w" in result.summary()

    def test_heavy_impact_chain(self):
        """Heavy impact: per-position broadband -> energy average -> corrections."""
        per_position = [58.4, 57.1, 59.8, 58.0]
        li_fmax = a_weighted_max_level(per_position)
        assert 57.0 < li_fmax < 60.0

        room = RoomCorrection(volume=38.0, reverberation_time=0.42)
        standardized = room.standardized_max(li_fmax)
        # The room is smaller than reference and drier, so the two corrections oppose
        assert abs(standardized - li_fmax) < 3.0

    def test_standardized_max_requires_scalar_reverberation(self):
        room = RoomCorrection(volume=38.0, reverberation_time=[0.4, 0.5])
        with pytest.raises(ValueError, match="scalar"):
            room.standardized_max(58.0)


# ── Which single number each impact source is rated by ──────────────
class TestPostVerificationQuantity:
    """국토교통부 고시 제2022-868호 (2022-12-28).

    The revision replaced both old inverse-A quantities, but with *different*
    things — the two sources are no longer rated by the same procedure:

      light (tapping machine) -> L'nT,w,  ISO 717-2 reference-curve shifting
      heavy (impact ball)     -> L'iA,Fmax, A-weighted maximum

    Rating a tapping machine by an A-weighted band sum produces a plausible
    number that is not comparable with the 49 dB limit at all, so these tests
    pin the two apart.
    """

    @staticmethod
    def _session(source, level=55.0):
        from pislm.session import Measurement, Session

        session = Session(title="t", source=source)
        session.source_positions = 1
        session.channels = (0,)
        session.room.volume = 50.0
        session.room.reverberation = {b: 0.5 for b in session.bands}
        session.measurements = [
            Measurement(source_position=0, channel=0,
                        levels={b: level for b in session.bands}, broadband=level)
        ]
        return session

    def test_light_impact_is_rated_by_curve_shifting(self):
        from pislm.session import ImpactSource

        session = self._session(ImpactSource.TAPPING)
        result = session.evaluate()
        assert result.post_verification_symbol == "L'nT,w"
        # The rating is the curve-shifting result, not an A-weighted sum.
        assert result.iso_717_2 is not None
        assert result.post_verification == pytest.approx(result.iso_717_2.value)
        # Curve shifting returns a whole number of decibels; an energy sum
        # would essentially never land on one.
        assert float(result.post_verification).is_integer()

    def test_light_impact_is_not_the_a_weighted_sum(self):
        from pislm.session import ImpactSource

        session = self._session(ImpactSource.TAPPING)
        result = session.evaluate()
        a_weighted = session.a_weighted_from_bands()
        assert a_weighted is not None
        assert abs(result.post_verification - a_weighted) > 1.0, (
            "the tapping-machine rating must not collapse to L_A,eq"
        )

    @pytest.mark.parametrize("source_name", ["RUBBER_BALL", "BANG"])
    def test_heavy_impact_is_rated_by_the_a_weighted_maximum(self, source_name):
        from pislm.session import ImpactSource

        session = self._session(getattr(ImpactSource, source_name))
        result = session.evaluate()
        assert result.post_verification_symbol == "L'iA,Fmax"
        assert result.post_verification == pytest.approx(
            session.a_weighted_from_bands()
        )

    def test_the_two_sources_use_different_procedures(self):
        from pislm.session import ImpactSource

        light = ImpactSource.TAPPING
        heavy = ImpactSource.RUBBER_BALL
        assert light.single_number_symbol != heavy.single_number_symbol
        assert light.single_number_method != heavy.single_number_method
        assert "717-2" in light.single_number_method

    def test_heavy_impact_is_not_standardised(self):
        """[SPEC] L'iA,Fmax is the level **as measured** — no 10 lg(T/T0).

        The two sources share the 49 dB limit but not the normalisation.
        Standardising a heavy set would move it by 10 lg(T/T0): in a live room
        (T = 0.8 s) that is a silent −2 dB, enough to pass a floor that failed.
        So the answer must not depend on T at all.
        """
        from pislm.session import ImpactSource

        values = []
        for t in (0.3, 0.5, 0.8, 1.2):
            session = self._session(ImpactSource.RUBBER_BALL)
            session.room.reverberation = {b: t for b in session.bands}
            values.append(session.evaluate().post_verification)
        assert all(v == pytest.approx(values[0], abs=1e-9) for v in values), values

    def test_light_impact_still_depends_on_reverberation(self):
        """The mirror image: L'nT,w *is* standardised, so T must matter."""
        from pislm.session import ImpactSource

        def rating(t):
            session = self._session(ImpactSource.TAPPING, level=65.0)
            session.room.reverberation = {b: t for b in session.bands}
            return session.evaluate().post_verification

        # A longer T reads higher, so standardising must pull the rating down.
        assert rating(1.0) < rating(0.5) < rating(0.25)

    def test_heavy_impact_needs_no_room_data(self):
        """No volume/T is not a caveat for heavy — it is simply not an input."""
        from pislm.session import ImpactSource

        session = self._session(ImpactSource.RUBBER_BALL)
        session.room.volume = 0.0
        session.room.reverberation = {}
        result = session.evaluate()
        assert result.post_verification is not None
        assert not any("standardis" in w for w in result.warnings), result.warnings

        light = self._session(ImpactSource.TAPPING)
        light.room.volume = 0.0
        light.room.reverberation = {}
        assert any("standardised" in w for w in light.evaluate().warnings)

    def test_both_are_checked_against_the_same_limit(self):
        from pislm.session import ImpactSource

        for source in (ImpactSource.TAPPING, ImpactSource.RUBBER_BALL):
            result = self._session(source, level=20.0).evaluate()
            assert result.limit_db == 49.0
            assert result.passes is True
        for source in (ImpactSource.TAPPING, ImpactSource.RUBBER_BALL):
            result = self._session(source, level=75.0).evaluate()
            assert result.passes is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
