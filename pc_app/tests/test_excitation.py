"""Excitation signals and deconvolution.

The decisive test is the round trip: convolve a known synthetic room with each
excitation, deconvolve, and check the reverberation time comes back. Anything
less would not catch a broken MLS — a wrong tap mapping still produces a
full-period sequence, so only the autocorrelation reveals it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from scipy import signal as sig

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pislm.excitation import (  # noqa: E402
    ExcitationSettings,
    Signal,
    band_limit,
    generate,
    impulse_response,
    mls,
    mls_minimum_order,
    mls_peak_to_sidelobe,
    mls_periods,
    pink_noise,
    sine_sweep,
    white_noise,
)
from pislm.standards import decay_time, octave_filter  # noqa: E402

RATE = 48000.0


def synthetic_room(t60: float, rate: float = RATE, seconds: float = 2.0,
                   seed: int = 1) -> np.ndarray:
    """A room impulse response: noise with an exponential envelope.

    Flat spectrum and a known decay, which is what a real room approximates.
    """
    rng = np.random.default_rng(seed)
    n = int(rate * seconds)
    t = np.arange(n) / rate
    return rng.standard_normal(n) * np.exp(-6.907755 * t / t60)


def spectral_slope(data: np.ndarray, rate: float = RATE) -> float:
    """dB per octave between 50 Hz and 5 kHz."""
    freqs, power = sig.welch(data, rate, nperseg=8192)
    band = (freqs > 50) & (freqs < 5000)
    per_decade = np.polyfit(np.log10(freqs[band]), 10 * np.log10(power[band]), 1)[0]
    return float(per_decade * np.log10(2))


class TestNoise:
    def test_white_noise_is_flat(self):
        assert abs(spectral_slope(white_noise(4.0, RATE, seed=0))) < 0.3

    def test_pink_noise_falls_3db_per_octave(self):
        """The defining property of pink noise; a filter cascade only approximates it."""
        assert spectral_slope(pink_noise(4.0, RATE, seed=0)) == pytest.approx(-3.0, abs=0.3)

    def test_band_limit_removes_out_of_band_energy(self):
        limited = band_limit(white_noise(2.0, RATE, seed=0), RATE, 200, 2000)
        freqs, power = sig.welch(limited, RATE, nperseg=8192)
        inside = power[(freqs > 300) & (freqs < 1500)].mean()
        below = power[(freqs > 20) & (freqs < 80)].mean()
        assert 10 * np.log10(inside / below) > 25

    def test_noise_is_reproducible_from_a_seed(self):
        assert np.array_equal(white_noise(0.5, RATE, seed=7), white_noise(0.5, RATE, seed=7))


class TestMLS:
    @pytest.mark.parametrize("order", [8, 10, 12, 14, 15, 16])
    def test_length_is_two_to_the_order_minus_one(self, order):
        assert mls(order).size == (1 << order) - 1

    @pytest.mark.parametrize("order", [8, 10, 12, 14, 15, 16])
    def test_peak_to_sidelobe_equals_n(self, order):
        """This is what catches a wrong tap mapping.

        The `order − t` indexing also yields a full-period sequence, so a period
        check passes, but the autocorrelation collapses to about 1 and the
        deconvolution silently returns noise. It must be exactly N.
        """
        expected = (1 << order) - 1
        assert mls_peak_to_sidelobe(order) == pytest.approx(expected, rel=1e-6)

    def test_values_are_plus_or_minus_one(self):
        assert set(np.unique(mls(10))) == {-1.0, 1.0}

    def test_is_deterministic(self):
        assert np.array_equal(mls(12), mls(12))

    def test_unsupported_order_rejected(self):
        with pytest.raises(ValueError, match="unsupported MLS order"):
            mls(9)

    def test_generate_emits_at_least_two_periods(self):
        """Circular deconvolution needs the room already in steady state."""
        settings = ExcitationSettings(signal=Signal.MLS, mls_order=12)
        waveform = generate(settings, RATE)
        period = (1 << 12) - 1
        assert mls_periods(settings) >= 2
        assert waveform.size == period * mls_periods(settings)

    @pytest.mark.parametrize("repeats", [1, 2, 3, 5])
    def test_repeats_means_whole_periods_exactly_as_the_server_plays_them(self, repeats):
        """PROTOCOL.md §10: the server plays `repeats` whole periods.

        The client used to emit `repeats + 1`, so the deconvolver sliced one
        period too late — into the decay tail, where there is no sequence to
        correlate against. It failed silently, which is the whole danger.
        """
        settings = ExcitationSettings(signal=Signal.MLS, mls_order=12, repeats=repeats)
        period = (1 << 12) - 1
        played = generate(settings, RATE).size // period
        assert played == mls_periods(settings)
        # And the command must ask the server for that same number, so the two
        # sides cannot disagree about what is on the wire.
        assert settings.to_command()["repeats"] == played

    def test_a_single_period_is_never_requested(self):
        """`repeats: 1` is the protocol default but cannot be deconvolved."""
        settings = ExcitationSettings(signal=Signal.MLS, repeats=1)
        assert settings.to_command()["repeats"] == 2

    def test_repeats_is_untouched_for_other_signals(self):
        settings = ExcitationSettings(signal=Signal.PINK, repeats=1)
        assert settings.to_command()["repeats"] == 1


class TestSweep:
    def test_sweep_spans_the_requested_range(self):
        sweep = sine_sweep(3.0, RATE, 100, 4000)
        freqs, power = sig.welch(sweep, RATE, nperseg=4096)
        inside = power[(freqs > 200) & (freqs < 3000)].mean()
        above = power[(freqs > 8000) & (freqs < 15000)].mean()
        assert 10 * np.log10(inside / above) > 25

    def test_instantaneous_frequency_rises(self):
        sweep = sine_sweep(2.0, RATE, 100, 4000)
        half = sweep.size // 2
        first = np.sum(np.abs(np.diff(np.sign(sweep[:half])))) / 2
        second = np.sum(np.abs(np.diff(np.sign(sweep[half:])))) / 2
        assert second > first * 2, "an exponential sweep must accelerate"

    def test_is_deterministic(self):
        assert np.array_equal(sine_sweep(1.0, RATE, 50, 5000),
                              sine_sweep(1.0, RATE, 50, 5000))


class TestLevels:
    def test_level_scales_the_amplitude(self):
        quiet = generate(ExcitationSettings(signal=Signal.PINK, seconds=0.5,
                                            level_dbfs=-40, tail_seconds=0), RATE, seed=0)
        loud = generate(ExcitationSettings(signal=Signal.PINK, seconds=0.5,
                                           level_dbfs=-20, tail_seconds=0), RATE, seed=0)
        ratio = 20 * np.log10(np.max(np.abs(loud)) / np.max(np.abs(quiet)))
        assert ratio == pytest.approx(20.0, abs=0.5)

    def test_tail_is_appended_for_noise(self):
        settings = ExcitationSettings(signal=Signal.PINK, seconds=1.0, tail_seconds=0.5)
        assert generate(settings, RATE).size == int(1.5 * RATE)

    def test_never_exceeds_full_scale(self):
        for signal in Signal:
            waveform = generate(
                ExcitationSettings(signal=signal, seconds=0.5, level_dbfs=0.0,
                                   mls_order=10, tail_seconds=0), RATE, seed=0
            )
            assert np.max(np.abs(waveform)) <= 1.0 + 1e-9, signal


class TestDeconvolution:
    @pytest.mark.parametrize("t60", [0.4, 0.8])
    def test_sweep_recovers_a_known_reverberation_time(self, t60):
        settings = ExcitationSettings(signal=Signal.SWEEP, seconds=3.0,
                                      f_min=50, f_max=5000, tail_seconds=2.0)
        excitation = generate(settings, RATE, seed=0)
        recorded = sig.fftconvolve(excitation, synthetic_room(t60), mode="full")
        response = impulse_response(recorded, settings, RATE)
        measured = decay_time(octave_filter(response, RATE, 500, 3), RATE, "T20")
        assert measured.t60 == pytest.approx(t60, rel=0.10)
        assert measured.correlation > 0.95

    @pytest.mark.parametrize("t60", [0.4, 0.8])
    def test_mls_recovers_a_known_reverberation_time(self, t60):
        # The period must exceed the decay, otherwise the circular
        # deconvolution wraps the tail onto the start of the response.
        order = mls_minimum_order(t60, RATE)
        settings = ExcitationSettings(signal=Signal.MLS, mls_order=order)
        excitation = generate(settings, RATE, seed=0)
        recorded = sig.fftconvolve(excitation, synthetic_room(t60), mode="full")
        response = impulse_response(recorded, settings, RATE)
        measured = decay_time(octave_filter(response, RATE, 500, 3), RATE, "T20")
        assert measured.t60 == pytest.approx(t60, rel=0.15)

    @pytest.mark.parametrize("t60", [0.4, 0.8])
    def test_mls_works_against_a_waveform_built_from_the_spec_alone(self, t60):
        """Deconvolve what PROTOCOL.md §10 says the Pi plays, not our own output.

        Feeding `generate()`'s output back into the deconvolver only proves the
        client agrees with itself. The Pi is a separate implementation, so the
        excitation here is built straight from the specified text: `repeats`
        whole periods, back to back, no silence between them.
        """
        order = mls_minimum_order(t60, RATE)
        settings = ExcitationSettings(signal=Signal.MLS, mls_order=order, repeats=3)
        periods = settings.to_command()["repeats"]
        as_the_pi_plays_it = np.tile(mls(order) * settings.amplitude, periods)

        recorded = sig.fftconvolve(as_the_pi_plays_it, synthetic_room(t60), mode="full")
        response = impulse_response(recorded, settings, RATE)
        measured = decay_time(octave_filter(response, RATE, 500, 3), RATE, "T20")
        assert measured.t60 == pytest.approx(t60, rel=0.15)

    @pytest.mark.parametrize("offset", [0, 5, 50, 480, 4800, 24000])
    def test_mls_survives_a_wrong_start_index(self, offset):
        """§10: `start_index` is a software timestamp, not a hardware alignment.

        The USB latency it cannot measure is plausibly 1-10 ms, and before the
        alignment correction MLS already failed outright at 480 samples (10 ms
        at 48 kHz). Everything here must land on the same answer.
        """
        t60 = 0.4
        order = mls_minimum_order(t60, RATE)
        settings = ExcitationSettings(signal=Signal.MLS, mls_order=order, repeats=3)
        excitation = np.tile(mls(order) * settings.amplitude,
                             settings.to_command()["repeats"])
        recorded = sig.fftconvolve(excitation, synthetic_room(t60), mode="full")

        response = impulse_response(recorded[offset:], settings, RATE)
        measured = decay_time(octave_filter(response, RATE, 500, 3), RATE, "T20")
        assert measured.t60 == pytest.approx(t60, rel=0.15)

    def test_alignment_correction_is_not_fooled_by_a_noisy_head(self):
        """The correction must find the arrival, not the largest sample.

        In a response whose head is noise-like the biggest single sample sits
        a little after the true arrival. Correcting by that error rotates the
        loudest part of the impulse response to the end of the array, where
        Schroeder integrates it as a noise floor — it read 0.552 s instead of
        0.440 s, and made a 0.8 s room unmeasurable. Hence the smoothed
        envelope and the ISO 3382-2 start-point rule.
        """
        from pislm.excitation import _alignment_error

        t60 = 0.4
        order = mls_minimum_order(t60, RATE)
        settings = ExcitationSettings(signal=Signal.MLS, mls_order=order, repeats=3)
        excitation = np.tile(mls(order) * settings.amplitude, 3)
        recorded = sig.fftconvolve(excitation, synthetic_room(t60), mode="full")

        length = (1 << order) - 1
        aligned = recorded[2 * length : 3 * length]
        raw = np.fft.irfft(
            np.fft.rfft(aligned) * np.conj(np.fft.rfft(mls(order))), length
        ) / length
        # The largest sample is demonstrably not at the arrival ...
        assert int(np.argmax(np.abs(raw))) > 100
        # ... but the correction still reports the window as already aligned.
        assert abs(_alignment_error(raw, RATE)) <= 16

    def test_sweep_is_immune_to_a_wrong_start_index(self):
        """No correction needed: the inverse filter finds its own peak."""
        t60 = 0.4
        settings = ExcitationSettings(signal=Signal.SWEEP, seconds=3.0,
                                      f_min=50, f_max=5000, tail_seconds=2.0)
        recorded = sig.fftconvolve(generate(settings, RATE, seed=0),
                                   synthetic_room(t60), mode="full")
        answers = [
            decay_time(
                octave_filter(impulse_response(recorded[offset:], settings, RATE),
                              RATE, 500, 3), RATE, "T20"
            ).t60
            for offset in (0, 50, 4800, 48000)
        ]
        assert max(answers) - min(answers) < 0.02

    def test_noise_cannot_be_deconvolved(self):
        """Noise carries no phase reference, so it must refuse rather than guess."""
        for signal in (Signal.WHITE, Signal.PINK):
            settings = ExcitationSettings(signal=signal)
            with pytest.raises(ValueError, match="cannot be deconvolved"):
                impulse_response(np.zeros(int(RATE)), settings, RATE)

    def test_minimum_order_grows_with_the_decay(self):
        """A longer room needs a longer sequence."""
        assert mls_minimum_order(0.3, RATE) < mls_minimum_order(1.5, RATE)
        order = mls_minimum_order(0.8, RATE)
        assert (1 << order) - 1 >= 2 * 0.8 * RATE

    def test_mls_needs_two_periods(self):
        settings = ExcitationSettings(signal=Signal.MLS, mls_order=12)
        one_period = np.zeros((1 << 12) - 1)
        with pytest.raises(ValueError, match="at least two periods"):
            impulse_response(one_period, settings, RATE)

    def test_a_recording_shorter_than_the_excitation_is_refused(self):
        """Analysing an earlier period would land in the build-up.

        Steady state is what makes the circular assumption valid, so a
        build-up period yields a plausible-looking but badly wrong answer
        rather than an obvious failure — a 0.6 s room read 6.98 s.
        """
        settings = ExcitationSettings(signal=Signal.MLS, mls_order=12, repeats=4)
        length = (1 << 12) - 1
        assert mls_periods(settings) == 4
        short = np.zeros(3 * length)  # three periods recorded, four played
        with pytest.raises(ValueError, match="shorter than the excitation"):
            impulse_response(short, settings, RATE)

    def test_interrupted_noise_measures_the_decay_directly(self):
        """Pink noise needs no deconvolution — the decay after switch-off is used."""
        settings = ExcitationSettings(signal=Signal.PINK, seconds=2.0,
                                      f_min=50, f_max=5000, tail_seconds=3.0)
        excitation = generate(settings, RATE, seed=0)
        recorded = sig.fftconvolve(excitation, synthetic_room(0.6), mode="full")
        switch_off = int(2.0 * RATE)
        decay = recorded[switch_off : switch_off + int(2.5 * RATE)]
        measured = decay_time(octave_filter(decay, RATE, 500, 3), RATE, "T20")
        assert measured.t60 == pytest.approx(0.6, rel=0.10)


class TestSettings:
    def test_method_reflects_the_signal(self):
        assert Signal.SWEEP.method == "impulse response"
        assert Signal.MLS.method == "impulse response"
        assert Signal.PINK.method == "interrupted noise"
        assert Signal.WHITE.method == "interrupted noise"

    def test_command_payload_round_trips(self):
        settings = ExcitationSettings(signal=Signal.SWEEP, seconds=3.0, level_dbfs=-18.0)
        payload = settings.to_command()
        assert payload["signal"] == "sweep"
        assert payload["level_dbfs"] == -18.0
        assert set(payload) >= {"signal", "seconds", "level_dbfs", "f_min", "f_max",
                                "mls_order", "channel", "tail_seconds", "repeats"}

    def test_amplitude_from_dbfs(self):
        assert ExcitationSettings(level_dbfs=0.0).amplitude == pytest.approx(1.0)
        assert ExcitationSettings(level_dbfs=-20.0).amplitude == pytest.approx(0.1)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
