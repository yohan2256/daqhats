#!/usr/bin/env python3
#  -*- coding: utf-8 -*-
"""
Sound-level-meter DSP for the MCC 172 noise monitor.

Provides the pieces needed to behave like a class sound level meter:

* IEC 61672 frequency weighting (A / C / Z) as digital filters, plus the
  analog weighting offset in dB at a given frequency (used for octave bands).
* Exponential time weighting (Fast / Slow / Impulse) as a stateful one-pole
  integrator on the squared signal, for a continuous time-weighted level.
* Window metrics computed from stored raw samples on request: Leq, Lmax,
  Lmin, Lpeak, and LN percentiles (e.g. L10/L50/L90).

Requires numpy + scipy (only when level output or metrics are used).
"""
from __future__ import print_function

import math

import numpy as np
from scipy import signal

REFERENCE_PRESSURE = 20e-6      # 20 uPa, SPL reference

# Exponential time-weighting constants (seconds). Impulse is approximated by
# its 35 ms rise time constant (the standard also has a slow 1.5 s decay).
TIME_CONSTANTS = {'Fast': 0.125, 'Slow': 1.0, 'Impulse': 0.035}

# IEC 61672 analog pole frequencies (Hz).
_F1 = 20.598997
_F2 = 107.65265
_F3 = 737.86223
_F4 = 12194.217


def _a_weighting_zpk():
    z = [0.0, 0.0, 0.0, 0.0]
    p = [-2 * math.pi * _F1, -2 * math.pi * _F1,
         -2 * math.pi * _F2, -2 * math.pi * _F3,
         -2 * math.pi * _F4, -2 * math.pi * _F4]
    k = (2 * math.pi * _F4) ** 2 * 10 ** (1.9997 / 20.0)
    return np.array(z), np.array(p), k


def _c_weighting_zpk():
    z = [0.0, 0.0]
    p = [-2 * math.pi * _F1, -2 * math.pi * _F1,
         -2 * math.pi * _F4, -2 * math.pi * _F4]
    k = (2 * math.pi * _F4) ** 2 * 10 ** (0.0619 / 20.0)
    return np.array(z), np.array(p), k


def design_weighting_sos(kind, fs):
    """Return second-order sections for the weighting, or None for 'Z'."""
    kind = (kind or 'Z').upper()
    if kind == 'Z':
        return None
    if kind == 'A':
        z, p, k = _a_weighting_zpk()
    elif kind == 'C':
        z, p, k = _c_weighting_zpk()
    else:
        raise ValueError("weighting must be 'A', 'C', or 'Z'")
    z_d, p_d, k_d = signal.bilinear_zpk(z, p, k, fs)
    return signal.zpk2sos(z_d, p_d, k_d)


def weighting_offset_db(kind, freq):
    """Analog A/C weighting value in dB at ``freq`` (0.0 for Z). Used to
    weight octave-band levels without a per-band filter."""
    kind = (kind or 'Z').upper()
    if kind == 'Z' or freq <= 0:
        return 0.0
    f2 = freq * freq
    if kind == 'A':
        ra = ((_F4 ** 2 * f2 ** 2) /
              ((f2 + _F1 ** 2) * (f2 + _F4 ** 2) *
               math.sqrt((f2 + _F2 ** 2) * (f2 + _F3 ** 2))))
        return 20 * math.log10(ra) + 2.00
    if kind == 'C':
        rc = (_F4 ** 2 * f2) / ((f2 + _F1 ** 2) * (f2 + _F4 ** 2))
        return 20 * math.log10(rc) + 0.06
    raise ValueError("weighting must be 'A', 'C', or 'Z'")


def tau_for(time_weighting):
    try:
        return TIME_CONSTANTS[time_weighting]
    except KeyError:
        raise ValueError("time weighting must be Fast, Slow, or Impulse")


class ExpLevel:
    """Stateful exponential-time-weighted level, downsampled to out_rate.

    Feed successive signal blocks with :meth:`process`; it maintains the
    one-pole mean-square state and the decimation phase across blocks, so the
    concatenated output is a gap-free level time series in dB.
    """

    def __init__(self, fs, tau, out_rate, ref=REFERENCE_PRESSURE):
        alpha = math.exp(-1.0 / (fs * tau))
        self._b = np.array([1.0 - alpha])
        self._a = np.array([1.0, -alpha])
        self._zi = None          # primed from the first block's level
        self._step = max(1, int(round(fs / out_rate)))
        self._phase = 0
        self._ref2 = ref * ref

    def process(self, weighted_block):
        x2 = np.square(np.asarray(weighted_block, dtype=np.float64))
        if self._zi is None:
            # Start the integrator at the first block's mean-square so the
            # stream has no startup dip.
            self._zi = signal.lfilter_zi(self._b, self._a) * (
                x2[0] if x2.size else 0.0)
        ms, self._zi = signal.lfilter(self._b, self._a, x2, zi=self._zi)
        start = self._phase
        if start >= ms.size:
            self._phase = start - ms.size
            return np.empty(0)
        idx = np.arange(start, ms.size, self._step)
        self._phase = idx[-1] + self._step - ms.size
        out = np.maximum(ms[idx], 1e-30)
        return 10.0 * np.log10(out / self._ref2)


def window_metrics(samples, fs, weighting='A', time_weighting='Fast',
                   ref=REFERENCE_PRESSURE, percentiles=(10, 50, 90),
                   level_rate=100.0):
    """Compute sound-level-meter metrics over a block of raw samples.

    Args:
        samples (numpy.ndarray): one channel of raw samples (Pa if calibrated).
        fs (float): sample rate.
        weighting (str): 'A', 'C', or 'Z' frequency weighting.
        time_weighting (str): 'Fast', 'Slow', or 'Impulse' for Lmax/Lmin/LN.
        ref (float): pressure reference (20 uPa for SPL).
        percentiles (iterable[int]): N values for LN (level exceeded N %).
        level_rate (float): rate at which the time-weighted level is sampled
            for the statistical metrics.

    Returns:
        dict with Leq, Lmax, Lmin, Lpeak, LN, and metadata.
    """
    x = np.asarray(samples, dtype=np.float64)
    if x.size == 0:
        return {'error': 'no samples'}

    sos = design_weighting_sos(weighting, fs)
    w = x if sos is None else signal.sosfilt(sos, x)
    ref2 = ref * ref

    leq = 10.0 * math.log10(max(np.mean(np.square(w)), 1e-30) / ref2)
    lpeak = 20.0 * math.log10(max(np.max(np.abs(w)), 1e-30) / ref)

    tau = tau_for(time_weighting)
    alpha = math.exp(-1.0 / (fs * tau))
    # Prime the integrator at the first sample's mean-square, then discard a
    # few time constants so the startup transient does not bias Lmin/LN.
    b = np.array([1.0 - alpha])
    a = np.array([1.0, -alpha])
    zi = signal.lfilter_zi(b, a) * float(w[0] ** 2)
    ms, _ = signal.lfilter(b, a, np.square(w), zi=zi)
    step = max(1, int(round(fs / level_rate)))
    levels = 10.0 * np.log10(np.maximum(ms[::step], 1e-30) / ref2)
    settle = min(len(levels) - 1, int(round(5.0 * tau * level_rate)))
    stat_levels = levels[settle:] if settle > 0 else levels

    ln = {}
    for n in percentiles:
        # L_N is the level exceeded N % of the measurement time.
        ln['L{}'.format(int(n))] = float(np.percentile(stat_levels, 100 - n))

    return {
        'Leq': float(leq),
        'Lmax': float(np.max(stat_levels)),
        'Lmin': float(np.min(stat_levels)),
        'Lpeak': float(lpeak),
        'LN': ln,
        'weighting': weighting.upper(),
        'time_weighting': time_weighting,
        'window_seconds': x.size / fs,
        'n_samples': int(x.size),
    }
