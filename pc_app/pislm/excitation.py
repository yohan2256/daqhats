"""Excitation signals for reverberation measurement, and their deconvolution.

ISO 3382-2 allows two families:

* **Interrupted noise** (white / pink) — excite the room, switch off, measure
  the decay directly. Simple, but each run is one stochastic realisation, so
  several averages are needed.
* **Integrated impulse response** (sweep / MLS, ISO 18233) — recover the
  impulse response by deconvolution, then Schroeder-integrate it. Far better
  signal-to-noise for the same level, and repeatable rather than stochastic.

The deconvolution differs per signal, so the excitation and the analysis have
to agree. That is why the generator and the inverse filter live together here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

import numpy as np
from scipy import signal as sig


class Signal(str, Enum):
    """Excitation signal type."""

    WHITE = "white"
    PINK = "pink"
    SWEEP = "sweep"
    MLS = "mls"

    @property
    def label(self) -> str:
        return {
            "white": "White noise",
            "pink": "Pink noise",
            "sweep": "Exponential sine sweep",
            "mls": "MLS",
        }[self.value]

    @property
    def is_deterministic(self) -> bool:
        """Can an impulse response be deconvolved from it (ISO 18233)?"""
        return self in (Signal.SWEEP, Signal.MLS)

    @property
    def method(self) -> str:
        return "impulse response" if self.is_deterministic else "interrupted noise"


@dataclass(slots=True)
class ExcitationSettings:
    """What to play out of the analog output."""

    signal: Signal = Signal.PINK
    seconds: float = 5.0
    level_dbfs: float = -20.0
    f_min: float = 50.0
    f_max: float = 5000.0
    #: MLS register length; sequence length is 2^order − 1.
    #: The period must exceed the room decay or the circular deconvolution
    #: wraps the tail — see `mls_minimum_order()`.
    mls_order: int = 16
    #: Output channel on the generating device
    channel: int = 0
    #: Silence appended after the signal so the decay is captured
    tail_seconds: float = 2.0
    #: How many times the signal is played. For MLS this is the number of
    #: whole periods played back to back (PROTOCOL.md §10), and it must be at
    #: least 2 — see `mls_periods()`.
    repeats: int = 1

    @property
    def amplitude(self) -> float:
        """Peak amplitude as a fraction of full scale."""
        return float(10.0 ** (self.level_dbfs / 20.0))

    def to_command(self) -> dict:
        """Fields for the `set_output` command (PROTOCOL.md §10).

        `repeats` is sent as the *effective* period count for MLS rather than
        the raw field. The server plays exactly `repeats` whole periods, so if
        the client silently used a different number the deconvolution would
        slice the wrong period — and it fails quietly, returning noise instead
        of an impulse response.
        """
        return {
            "signal": self.signal.value,
            "seconds": self.seconds,
            "level_dbfs": self.level_dbfs,
            "f_min": self.f_min,
            "f_max": self.f_max,
            "mls_order": self.mls_order,
            "channel": self.channel,
            "tail_seconds": self.tail_seconds,
            "repeats": mls_periods(self) if self.signal is Signal.MLS else self.repeats,
        }


# ── Generation ──────────────────────────────────────────────────────
def white_noise(seconds: float, rate: float, seed: int | None = None) -> np.ndarray:
    """Gaussian white noise, normalised to unit peak."""
    rng = np.random.default_rng(seed)
    out = rng.standard_normal(max(1, int(seconds * rate)))
    return _normalise(out)


def pink_noise(seconds: float, rate: float, seed: int | None = None) -> np.ndarray:
    """Pink (1/f) noise by shaping white noise in the frequency domain.

    Shaping the spectrum directly is exact, unlike the usual cascade of
    first-order filters which only approximates −3 dB/octave.
    """
    rng = np.random.default_rng(seed)
    n = max(2, int(seconds * rate))
    spectrum = np.fft.rfft(rng.standard_normal(n))
    freqs = np.fft.rfftfreq(n, 1.0 / rate)
    shape = np.ones_like(freqs)
    # Power ∝ 1/f, so amplitude ∝ 1/sqrt(f). DC is left alone.
    shape[1:] = 1.0 / np.sqrt(freqs[1:])
    shape[0] = 0.0
    return _normalise(np.fft.irfft(spectrum * shape, n))


def band_limit(data: np.ndarray, rate: float, f_min: float, f_max: float,
               order: int = 4) -> np.ndarray:
    """Band-limit an excitation signal. Zero-phase, so the spectrum is what matters."""
    low = max(1.0, f_min)
    high = min(f_max, rate / 2.0 * 0.99)
    if low >= high:
        return data
    sos = sig.butter(order, [low, high], btype="band", fs=rate, output="sos")
    return _normalise(sig.sosfiltfilt(sos, data))


def sine_sweep(seconds: float, rate: float, f_min: float, f_max: float) -> np.ndarray:
    """Exponential (logarithmic) sine sweep — Farina's method.

        s(t) = sin( (w1·T / ln(w2/w1)) · (exp(t/T · ln(w2/w1)) − 1) )

    Exponential rather than linear because it puts equal energy in every
    octave and pushes harmonic distortion products *before* the direct sound,
    where they can be windowed away after deconvolution.
    """
    n = max(2, int(seconds * rate))
    t = np.arange(n) / rate
    duration = n / rate
    w1, w2 = 2 * np.pi * max(1.0, f_min), 2 * np.pi * min(f_max, rate / 2 * 0.99)
    ratio = math.log(w2 / w1)
    sweep = np.sin((w1 * duration / ratio) * (np.exp(t / duration * ratio) - 1.0))
    # Fade the ends so the abrupt start/stop does not smear the spectrum
    return _normalise(sweep * _fade_window(n, rate))


def mls(order: int = 16) -> np.ndarray:
    """Maximum length sequence of length 2^order − 1, as ±1.

    Generated with a linear feedback shift register using primitive
    polynomial taps. An MLS is deterministic and has an almost flat spectrum,
    so its circular autocorrelation is nearly a delta — which is exactly what
    makes the deconvolution a simple cross-correlation.
    """
    #: Primitive polynomial taps. Exponent t maps to register index t−1; the
    #: obvious-looking `order − t` mapping also produces a full-period sequence
    #: but destroys the autocorrelation, so the deconvolution silently returns
    #: noise. Verified: peak-to-sidelobe equals N for every order below.
    taps = {
        8: (8, 6, 5, 4), 10: (10, 7), 12: (12, 6, 4, 1), 14: (14, 5, 3, 1),
        15: (15, 14), 16: (16, 15, 13, 4), 17: (17, 14), 18: (18, 11),
    }
    if order not in taps:
        raise ValueError(f"unsupported MLS order: {order} (have {sorted(taps)})")
    length = (1 << order) - 1
    register = np.ones(order, dtype=np.int8)
    out = np.empty(length, dtype=np.float64)
    tap_positions = [t - 1 for t in taps[order]]
    for i in range(length):
        out[i] = register[-1]
        feedback = 0
        for position in tap_positions:
            feedback ^= register[position]
        register[1:] = register[:-1]
        register[0] = feedback
    return out * 2.0 - 1.0  # {0,1} -> {-1,+1}


def mls_peak_to_sidelobe(order: int) -> float:
    """Circular-autocorrelation peak divided by the largest sidelobe.

    A correct MLS gives exactly N. Anything near 1 means the taps or the
    register indexing are wrong and the deconvolution will return noise.
    """
    sequence = mls(order)
    auto = np.fft.irfft(np.abs(np.fft.rfft(sequence)) ** 2, sequence.size)
    return float(auto[0] / np.abs(auto[1:]).max())


def generate(settings: ExcitationSettings, rate: float, seed: int | None = None
             ) -> np.ndarray:
    """Build the excitation waveform, scaled to the requested level.

    A tail of silence is appended so the room decay is inside the recording.
    Without it the decay is cut off and the reverberation fit has nothing to
    work with.
    """
    if settings.signal is Signal.WHITE:
        core = band_limit(
            white_noise(settings.seconds, rate, seed), rate, settings.f_min, settings.f_max
        )
    elif settings.signal is Signal.PINK:
        core = band_limit(
            pink_noise(settings.seconds, rate, seed), rate, settings.f_min, settings.f_max
        )
    elif settings.signal is Signal.SWEEP:
        core = sine_sweep(settings.seconds, rate, settings.f_min, settings.f_max)
    elif settings.signal is Signal.MLS:
        # MLS deconvolution is *circular*, so the room must already be in
        # steady state when the analysed period begins. Play `repeats` whole
        # periods back to back with no silence between them (PROTOCOL.md §10)
        # and analyse the last one. A single period would fold the room's own
        # decay back onto the start of the impulse response, which is why
        # `mls_periods()` enforces a floor of two.
        one_period = _normalise(mls(settings.mls_order)) * settings.amplitude
        return np.tile(one_period, mls_periods(settings))
    else:
        raise ValueError(f"unknown signal: {settings.signal}")

    core = core * settings.amplitude
    tail = np.zeros(max(0, int(settings.tail_seconds * rate)))
    one = np.concatenate([core, tail])
    return np.tile(one, max(1, settings.repeats))


def mls_minimum_order(t60: float, rate: float) -> int:
    """Smallest MLS order whose period comfortably exceeds the decay.

    Deconvolution is circular, so a decay longer than one period wraps around
    and folds onto the start of the impulse response. The usable dynamic range
    collapses and T20 can no longer be placed. Rule of thumb: give the period
    at least twice the expected T60.
    """
    needed = 2.0 * t60 * rate
    for order in (8, 10, 12, 14, 15, 16, 17, 18):
        if (1 << order) - 1 >= needed:
            return order
    return 18


def mls_periods(settings: ExcitationSettings) -> int:
    """How many whole MLS periods are played.

    This is `repeats` (PROTOCOL.md §10 — the server plays exactly that many
    periods back to back), with a floor of two. The floor matters: circular
    deconvolution assumes the room is already in steady state, so the first
    period is build-up and only the second onwards is usable. `repeats: 1`,
    the protocol default, cannot be deconvolved at all.

    Three or more is better on real hardware. `start_index` lands slightly
    early relative to the DAC's first sample, so the analysed window is
    nudged backwards, and a third period keeps it clear of the build-up.
    """
    return max(2, settings.repeats)


# ── Deconvolution to an impulse response ────────────────────────────
def impulse_response(response: np.ndarray, settings: ExcitationSettings,
                     rate: float) -> np.ndarray:
    """Recover the impulse response from a recorded sweep or MLS (ISO 18233).

    Noise signals cannot be deconvolved — use the interrupted-noise method for
    those and feed the decay straight to `decay_time()`.
    """
    if not settings.signal.is_deterministic:
        raise ValueError(
            f"{settings.signal.label} cannot be deconvolved; use interrupted noise"
        )
    if settings.signal is Signal.SWEEP:
        return _deconvolve_sweep(response, settings, rate)
    return _deconvolve_mls(response, settings, rate)


def _deconvolve_sweep(response: np.ndarray, settings: ExcitationSettings,
                      rate: float) -> np.ndarray:
    """Convolve with the inverse filter of an exponential sweep.

    The inverse filter is the sweep reversed in time with a −6 dB/octave
    amplitude envelope, which compensates the sweep's pink spectrum. The
    result is a delta-like excitation, so the convolution yields the IR.
    """
    sweep = sine_sweep(settings.seconds, rate, settings.f_min, settings.f_max)
    n = sweep.size
    ratio = math.log(
        min(settings.f_max, rate / 2 * 0.99) / max(1.0, settings.f_min)
    )
    # Amplitude falls 6 dB per octave across the sweep
    envelope = np.exp(-np.arange(n) / n * ratio)
    inverse = sweep[::-1] * envelope

    full = sig.fftconvolve(response, inverse, mode="full")
    # The IR starts where the compressed sweep peaks
    peak = int(np.argmax(np.abs(full)))
    return full[peak:]


def _deconvolve_mls(response: np.ndarray, settings: ExcitationSettings,
                    rate: float) -> np.ndarray:
    """Circular cross-correlation with the MLS.

    An MLS has a near-perfect circular autocorrelation, so correlating the
    response with the sequence recovers the impulse response directly.

    The **last complete period** is used: by then the room is in steady state,
    which is what makes the circular assumption valid. Analysing the first
    period instead wraps the room's build-up into the answer.
    """
    sequence = mls(settings.mls_order)
    length = sequence.size
    if response.size < 2 * length:
        raise ValueError(
            f"MLS deconvolution needs at least two periods "
            f"({response.size} < {2 * length} samples). Play the sequence "
            "repeatedly and record through the second period."
        )
    # Use the last period that was actually *played*, not the last period that
    # fits in the recording. The response is longer than the excitation (the
    # room keeps ringing), so counting periods from the response length lands
    # past the end of the excitation, in the decay tail, where there is no
    # sequence to correlate against.
    played = mls_periods(settings)
    available = response.size // length
    if available < played:
        # Falling back to an earlier period would land in the build-up, before
        # the room reached steady state, and the circular assumption does not
        # hold there. It does not fail loudly — it returns a plausible number
        # that is badly wrong (a 0.6 s room read 6.98 s), so refuse instead.
        raise ValueError(
            f"the recording is shorter than the excitation "
            f"({available} of {played} periods, "
            f"{response.size / length:.2f} periods of samples). Record for "
            f"longer, or lower the MLS order"
        )
    index = played
    start = (index - 1) * length
    answer = _correlate(response[start : start + length], sequence)

    # Correct the window rather than rotating the answer (see below).
    shift = _alignment_error(answer, rate)
    if shift:
        limit = min(played * length, response.size)
        corrected = start + shift
        # A positive shift walks the window forwards, possibly past the end of
        # the excitation into the decay, where there is no sequence left to
        # correlate against. Step back a period if one is available.
        if corrected + length > limit and corrected - length >= 0:
            corrected -= length
        if 0 <= corrected and corrected + length <= response.size:
            answer = _correlate(response[corrected : corrected + length], sequence)
    return answer


def _correlate(period: np.ndarray, sequence: np.ndarray) -> np.ndarray:
    """Circular cross-correlation of one recorded period with the sequence."""
    spectrum = np.fft.rfft(period) * np.conj(np.fft.rfft(sequence))
    return np.fft.irfft(spectrum, sequence.size) / sequence.size


def _alignment_error(response: np.ndarray, rate: float) -> int:
    """How far the analysed window sits from the true period boundary.

    `start_index` is a software timestamp taken as the analog-output scan is
    issued, and PROTOCOL.md §10 is explicit that the USB/scheduling latency
    before the DAC's first real sample is neither measured nor removed. So the
    window handed to the deconvolver is offset by some unknown δ.

    Measured tolerance: MLS still reads correctly at δ = 50 samples (~1 ms)
    but fails outright by δ = 480 (~10 ms), squarely inside the plausible
    latency range — so recovering δ is not optional on real hardware. (A sweep
    is immune; convolving with the inverse filter finds its own peak.)

    Slicing a steady-state period δ samples early is exactly a circular shift
    of that period, so the deconvolved response comes back circularly shifted
    and the arrival appears at `length − δ`. Reading anything past the half-way
    point as a negative shift recovers the sign.

    Finding the arrival by the largest single sample does not work. In a real
    room the direct sound dominates, but in any response with a noisy head the
    largest sample sits a little after the true arrival, and correcting by that
    error rotates the loudest part of the impulse response around to the end of
    the array. Schroeder integrates backwards from there, so that energy reads
    as a noise floor and the decay flattens — it turned a correct 0.440 s into
    0.552 s, and made T60 = 0.8 s unmeasurable outright.

    So use ISO 3382-2's own start-point rule instead: on a short-time energy
    envelope, walk back from the peak to where the level first drops 20 dB
    below it. Smoothing is what makes this robust — individual samples of a
    noise-like response dip below any threshold constantly, but the envelope
    does not.
    """
    length = response.size
    if length == 0:
        return 0
    # ~1 ms of smoothing: long enough to ride over individual dips, far
    # shorter than the shifts being detected.
    span = max(8, int(0.001 * rate))
    energy = np.convolve(response.astype(float) ** 2, np.ones(span) / span, mode="same")
    peak = int(np.argmax(energy))
    threshold = energy[peak] / 100.0  # 20 dB below the peak
    if threshold <= 0:
        return 0

    arrival = peak
    for step in range(1, length // 2):
        candidate = (peak - step) % length
        if energy[candidate] < threshold:
            break
        arrival = candidate
    return arrival - length if arrival > length // 2 else arrival


# ── Helpers ─────────────────────────────────────────────────────────
def _normalise(data: np.ndarray) -> np.ndarray:
    peak = float(np.max(np.abs(data))) if data.size else 0.0
    return data / peak if peak > 0 else data


def _fade_window(n: int, rate: float, fade_seconds: float = 0.02) -> np.ndarray:
    fade = min(int(fade_seconds * rate), n // 2)
    window = np.ones(n)
    if fade > 0:
        ramp = 0.5 * (1 - np.cos(np.pi * np.arange(fade) / fade))
        window[:fade] = ramp
        window[-fade:] = ramp[::-1]
    return window
