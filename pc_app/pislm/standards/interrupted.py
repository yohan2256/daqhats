"""Interrupted-noise RT: detect switch-off, then fit short-time band power.

Deliberately separate from integrated impulse-response (Schroeder) analysis.
One interruption per record; retain at least 0.5 s source-on and 1 s noise tail.
"""
import numpy as np
from scipy import signal
from .bands import Spectrum, band_edges
from .reverberation import DecayResult, EVALUATION_RANGES, _correlation


def _power(x, n):
    return np.mean(x[:len(x)//n*n].reshape(-1, n)**2, axis=1)


def switch_off(x, fs):
    x = np.asarray(x, dtype=float)
    if x.ndim != 1 or not np.isfinite(x).all() or not np.isfinite(fs) or fs <= 0:
        raise ValueError('Invalid waveform or sample rate')
    if len(x) < 2*fs:
        raise ValueError('Record source-on, interruption and at least 1 s background')
    # Remove DC/subsonic drift from trigger only; band analysis uses original data.
    y = signal.sosfilt(signal.butter(2, 40, 'highpass', fs=fs, output='sos'), x)
    n = max(1, round(.02*fs))
    e = _power(y, n)
    top = np.median(np.sort(e)[-25:])
    ref = np.median(e[e > top*.1]) if top > 0 else 0.
    if ref <= 0 or ref < 100*np.mean(e[-max(1, round(fs/n)):]):
        raise ValueError('No clear source-on to background transition')
    high = e > ref*.5
    candidates = []
    for i in range(25, len(e)-50):
        if np.mean(high[i-25:i]) >= .8 and np.all(e[i:i+5] < ref*.25):
            candidates.append(i)
    if not candidates:
        raise ValueError('No sustained switch-off found; include source-on prehistory')
    i = candidates[0]
    before = np.flatnonzero(high[:i])
    off = (int(before[-1])+1)*n
    if np.any(e[i+5:] > ref*.5):
        raise ValueError('Multiple interruptions or later disturbance; analyse separate runs')
    return off


def interrupted_spectrum(x, fs, centers, *, method='T20', fraction=3,
                         background_power=None, headroom_db=5., diagnostics=None):
    if method not in EVALUATION_RANGES:
        raise ValueError('Choose T20 or T30')
    x = np.asarray(x, dtype=float)
    off = switch_off(x, fs)
    results, values = {}, []
    for center in centers:
        try:
            low, high = band_edges(center, fraction)
            if high >= fs/2:
                raise ValueError('Band exceeds Nyquist')
            y = signal.sosfilt(signal.butter(6, [low, high], 'bandpass', fs=fs, output='sos'), x)
            reference = np.mean(y[max(0, off-round(.5*fs)):off]**2)
            noise = np.mean(y[-round(fs):]**2)
            if background_power is not None:
                noise = max(noise, float(background_power[center]))
                if not np.isfinite(noise) or noise <= 0:
                    raise ValueError("Invalid SET background")
            snr = 10*np.log10(reference/max(noise, np.finfo(float).tiny))
            required = abs(EVALUATION_RANGES[method][1])+headroom_db
            if not np.isfinite(snr) or snr < required:
                raise ValueError('Insufficient band signal-to-background range')
            n = max(1, round(.01*fs))
            e = _power(y[off:], n)
            curve = 10*np.log10(np.maximum(e/reference, np.finfo(float).tiny))
            def fit(which):
                upper, lower = EVALUATION_RANGES[which]
                if snr < abs(lower)+headroom_db:
                    raise ValueError('Insufficient range')
                # A brief stochastic dip must not choose the end of the decay.
                def crossing(level):
                    for i in range(len(curve)-2):
                        if np.all(curve[i:i+3] <= level):
                            return i
                    raise ValueError('Missing decay crossing')
                a, b = crossing(upper), crossing(lower)
                if b-a < 6:
                    raise ValueError('Too few decay samples')
                t = (np.arange(a, b+1)+.5)*n/fs
                v = curve[a:b+1]
                slope, intercept = np.polyfit(t, v, 1)
                if slope >= 0:
                    raise ValueError('Non-decaying signal')
                rt = -60/slope
                if n/fs >= rt/12:
                    raise ValueError('Decay too short for averaging interval')
                return float(rt), _correlation(v, slope*t+intercept), (a, b, float(slope), float(intercept))
            rt, corr, fitted = fit(method)
            curvature = None
            try:
                t20, _, _ = fit('T20'); t30, _, _ = fit('T30')
                curvature = 100*(t30/t20-1)
            except ValueError:
                pass
            result = DecayResult(rt, method, corr, float(snr), curvature, center)
            if diagnostics is not None:
                a, b, slope, intercept = fitted
                diagnostics[center] = dict(off_seconds=off/fs,
                    time_s=((np.arange(len(curve))+.5)*n/fs).tolist(),
                    level_db=curve.tolist(), fit_start_s=(a+.5)*n/fs,
                    fit_end_s=(b+.5)*n/fs, slope=slope, intercept=intercept,
                    noise_db=-float(snr), reference_power=float(reference))
            results[center] = result
            values.append(rt)
        except (ValueError, FloatingPointError) as exc:
            if diagnostics is not None:
                diagnostics[center] = dict(error=str(exc))
            values.append(float('nan'))
    return Spectrum(centers=tuple(centers), levels=tuple(values), fraction=fraction, label=f"interrupted noise {method} (s)"), results
