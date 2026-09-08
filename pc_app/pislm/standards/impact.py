"""Raw-waveform heavy impact levels, independent of the live display stream.

The window ends at the server's get_raw snapshot (not the GUI click). The
preceding three seconds initialise the causal filters and are excluded from
Fmax. There is deliberately no shorter-window or live-level fallback.
"""
from __future__ import annotations

import math
import numpy as np
from scipy import signal

from .bands import band_edges

PRE_ROLL_SECONDS = 3.0


def raw_band_fmax(samples, fs, seconds, bands, *, fraction=3, order=6):
    """Z-weighted band Fast maxima in dB re 20 µPa; input must be Pa.

    Filter, square and integrate at the original sample rate. Only the final
    maximum reduces the time series, so 20 Hz display timing cannot miss a peak.
    Band edges use the existing client's base-ten frequency convention.
    """
    fs, seconds = float(fs), float(seconds)
    if not (math.isfinite(fs) and fs > 0 and math.isfinite(seconds) and seconds > 0):
        raise ValueError('sample rate and duration must be finite and positive')
    x = np.asarray(samples, dtype=np.float64)
    n = int(seconds * fs)
    # Match get_raw's floor-to-whole-frames rule for fractional rates.
    warm = int((seconds + PRE_ROLL_SECONDS) * fs) - n
    if x.ndim != 1 or n < 1 or x.size < n + warm:
        raise ValueError('not enough raw waveform: capture duration plus 3 s pre-roll required')
    x = x[-(n + warm):]
    if not np.isfinite(x).all():
        raise ValueError('raw waveform contains NaN or infinity')
    bands = tuple(bands)
    if not bands:
        raise ValueError('no measurement bands selected')
    alpha = math.exp(-1.0 / (fs * 0.125))
    levels = {}
    for band in bands:
        lo, hi = band_edges(band, fraction)
        if not 0 < lo < hi < fs / 2:
            raise ValueError(f'{band:g} Hz band is not fully below Nyquist')
        sos = signal.butter(order, [lo, hi], btype='bandpass', fs=fs, output='sos')
        radius = max(float(np.max(np.abs(np.roots(row[3:])))) for row in sos)
        settling = math.log(1e-6) / math.log(radius) / fs + 10 * 0.125
        if settling > PRE_ROLL_SECONDS:
            raise ValueError(f'{band:g} Hz filter needs more than 3 s pre-roll')
        filtered = signal.sosfilt(sos, x)
        ms = signal.lfilter([1 - alpha], [1, -alpha], np.square(filtered))
        value = float(np.max(ms[-n:]))
        if not math.isfinite(value):
            raise ValueError('non-finite Fast detector result')
        levels[float(band)] = 10 * math.log10(max(value, 1e-30) / (20e-6)**2)
    return levels


def fetch_heavy_levels(pi, seconds, channels, bands, *, fraction=3, order=6):
    """Fetch complete calibrated data; fail closed on absent/short channels."""
    config = pi.config
    wanted = seconds + PRE_ROLL_SECONDS
    if config.buffer_seconds and config.buffer_seconds < wanted:
        raise ValueError(f'Raw buffer must hold at least {wanted:g} s; increase it before capture')
    for channel in channels:
        if not config.channel_info(channel).calibrated:
            raise ValueError(f'Channel {channel} is not calibrated in Pa')
    dumps = pi.fetch_raw(seconds=wanted, timeout=45.0)
    levels, windows = {}, []
    for channel in channels:
        candidates = [dump for dump in dumps.values() if channel in dump.channels]
        if len(candidates) != 1:
            raise ValueError(f'Expected one complete raw dump for channel {channel}')
        dump = candidates[0]
        if dump.start_index < 0:
            raise ValueError('Raw dump has no sample index; pislm/4 is required')
        levels[channel] = raw_band_fmax(
            dump.channel(channel), dump.sample_rate, seconds, bands,
            fraction=fraction, order=order)
        end = dump.end_index
        windows.append(f'ch{channel} [{end-int(seconds*dump.sample_rate)}, {end}) '
                       f'at {dump.sample_rate:g} Hz')
    return levels, 'Raw Z/Fast Fmax; server snapshot window; ' + '; '.join(windows)
