"""Full-sample calibrated sound-level analysis on the PC, with real prehistory.

A/C analog pole constants match pi_server/slm.py; digital bilinear filters are
not a claim of IEC instrument class compliance. No artificial high-pass is used.
"""
import math
import numpy as np
from scipy import signal
from .bands import band_edges

PREHISTORY = 10.0


def weighting_sos(kind, fs):
    if kind == 'Z':
        return None
    if kind not in ('A', 'C'):
        raise ValueError('Weighting must be A, C or Z')
    f1,f2,f3,f4 = 20.598997,107.65265,737.86223,12194.217
    poles = [-2*math.pi*f1]*2 + [-2*math.pi*f4]*2
    if kind == 'A':
        poles += [-2*math.pi*f2, -2*math.pi*f3]
    zeros = [0.] * (4 if kind == 'A' else 2)
    gain = (2*math.pi*f4)**2 * 10**((1.9997 if kind == 'A' else .0619)/20)
    return signal.zpk2sos(*signal.bilinear_zpk(zeros, poles, gain, fs))


def window(samples, fs, seconds):
    fs, seconds = float(fs), float(seconds)
    if not (math.isfinite(fs) and fs > 0 and math.isfinite(seconds) and seconds > 0):
        raise ValueError('Positive finite sample rate and duration required')
    x = np.asarray(samples, dtype=float)
    n = int(fs * seconds)
    want = int(fs * (seconds + PREHISTORY))
    if x.ndim != 1 or n < 1 or x.size < want or not np.isfinite(x).all():
        raise ValueError('Complete finite raw waveform plus 10 s history required')
    return x[-want:], n


def levels(samples, fs, seconds, *, time_weighting='Fast', bands=(), fraction=3):
    x,n = window(samples, fs, seconds)
    if time_weighting not in ('Fast','Slow'):
        raise ValueError('Time weighting must be Fast or Slow')
    tau = .125 if time_weighting == 'Fast' else 1.
    alpha = math.exp(-1/(fs*tau))
    result = {}
    ref2 = (20e-6)**2
    for kind in ('A','C','Z'):
        sos = weighting_sos(kind, fs)
        w = x if sos is None else signal.sosfilt(sos, x)
        square = w*w
        ms = signal.lfilter([1-alpha], [1,-alpha], square)[-n:]
        db = 10*np.log10(np.maximum(ms, 1e-30)/ref2)
        leq = float(10*np.log10(max(float(np.mean(square[-n:])),1e-30)/ref2))
        # LN/time history sampled every ~10 ms. Max/min use EVERY input sample.
        stride = max(1, round(fs*.01))
        stat = db[::stride]
        result[kind] = dict(Leq=leq, Lmax=float(db.max()), Lmin=float(db.min()),
                            Lpeak=float(10*np.log10(max(float(square[-n:].max()),1e-30)/ref2)),
                            SEL=leq+10*math.log10(n/fs),
                            LN={f'L{p}':float(np.percentile(stat,100-p)) for p in (10,50,90)},
                            history_db=stat.tolist(), history_step_seconds=stride/fs)
    band_values = {}
    for band in bands:
        lo,hi = band_edges(band,fraction)
        if hi >= fs/2:
            raise ValueError(f'{band:g} Hz band exceeds Nyquist')
        y = signal.sosfilt(signal.butter(6,[lo,hi],btype='bandpass',fs=fs,output='sos'),x)[-n:]
        band_values[band] = float(10*np.log10(max(float(np.mean(y*y)),1e-30)/ref2))
    return dict(weightings=result, bands=band_values, time_weighting=time_weighting,
                seconds=n/fs, fraction=fraction, units='Pa', prehistory_seconds=PREHISTORY)
