"""SET / repeated gated-noise workflow inspired by the XL2 public procedure.

A transparent independent implementation, not an emulation of XL2 firmware.
"""
import time
from dataclasses import asdict
import numpy as np
from scipy import signal
from .standards.bands import band_edges
from .standards.interrupted import interrupted_spectrum


def validate(pi, channels, seconds):
    pi.refresh()
    if not pi.config.running:
        raise ValueError('Start acquisition first')
    if not channels or len(set(channels)) != len(channels):
        raise ValueError('Select distinct microphones')
    if not np.isfinite(seconds) or seconds <= 0 or seconds+.5 > pi.config.buffer_seconds:
        raise ValueError('Increase raw buffer before SET; complete recording must fit')
    infos = [pi.config.channel_info(ch) for ch in channels]
    if not all(i.calibrated for i in infos):
        raise ValueError('All microphones must be calibrated in Pa')
    return [asdict(i) for i in infos]


def _wait(seconds, text, cancelled, progress):
    end = time.monotonic()+seconds
    last = None
    while True:
        if cancelled():
            raise RuntimeError('Cancelled; incomplete cycle discarded')
        left = end-time.monotonic()
        if left <= 0: break
        tick = int(np.ceil(left))
        if tick != last:
            progress(f'{text} · {tick} s'); last=tick
        time.sleep(min(.05,left))


def capture_raw(pi, channels, *, on_seconds=0, off_seconds=3,
                cancelled=lambda: False, progress=lambda text: None):
    seconds=on_seconds+off_seconds
    signature=validate(pi,channels,seconds)
    if on_seconds:
        _wait(on_seconds, 'NOISE — 잡음 ON, 일정하게 유지', cancelled, progress)
    _wait(off_seconds, 'DECAY — 잡음 OFF, 조용히 유지' if on_seconds else
          'SET — 음원 OFF, 배경소음 측정', cancelled, progress)
    if cancelled(): raise RuntimeError('Cancelled')
    # No retry with a shorter window: that could discard the interruption.
    dumps=pi.fetch_raw(seconds=seconds,timeout=30.)
    if cancelled(): raise RuntimeError('Cancelled')
    if signature != validate(pi,channels,seconds):
        raise ValueError('Configuration changed during recording; repeat SET')
    valid,reasons=pi.measurement_valid
    if not valid: raise ValueError('Acquisition invalid: '+'; '.join(reasons))
    for ch in channels:
        found=[d for d in dumps.values() if ch in d.channels]
        if len(found)!=1: raise ValueError(f'Missing/duplicate Ch{ch+1}')
        d=found[0]; x=d.channel(ch)
        if d.start_index<0 or len(x)<round(seconds*d.sample_rate) or not np.isfinite(x).all():
            raise ValueError('Incomplete or non-finite raw recording')
    return dumps,signature


def background(dumps, channels, bands, fraction):
    powers={}; rates={}
    for ch in channels:
        d=next(d for d in dumps.values() if ch in d.channels)
        rates[ch]=d.sample_rate; powers[ch]={}
        x=np.asarray(d.channel(ch),float)
        if len(x)<2*d.sample_rate or not np.isfinite(x).all():
            raise ValueError('SET requires at least 2 seconds of finite data')
        for b in bands:
            lo,hi=band_edges(b,fraction)
            sos=signal.butter(6,[lo,hi],btype='bandpass',fs=d.sample_rate,output='sos')
            y=signal.sosfilt(sos,x)
            power=float(np.mean(y[round(.5*d.sample_rate):]**2))
            if not np.isfinite(power) or power<=0:
                raise ValueError('Silent/invalid SET input; check microphone')
            powers[ch][b]=power
    return dict(powers=powers,rates=rates)


def analyse_cycle(dumps, channels, bands, fraction, method, baseline):
    cycle={}
    for ch in channels:
        d=next(d for d in dumps.values() if ch in d.channels)
        if d.sample_rate != baseline['rates'][ch]:
            raise ValueError('Sample rate changed; repeat SET')
        diagnostics={}
        try:
            _,results=interrupted_spectrum(d.channel(ch),d.sample_rate,bands,
                method=method,fraction=fraction,background_power=baseline['powers'][ch],
                headroom_db=10.,diagnostics=diagnostics)
        except ValueError as exc:
            results={}; diagnostics={b:dict(error=str(exc)) for b in bands}
        cycle[ch]={}
        for b in bands:
            r=results.get(b)
            cycle[ch][b]=dict(diagnostics.get(b,{}),
                t60=r.t60 if r else None, correlation=r.correlation if r else None,
                range_db=r.decay_range_db if r else None,
                curvature_percent=r.curvature_percent if r else None,
                warnings=r.warnings() if r else ['No usable fit'])
    return cycle


def summarise(cycles, channels, bands):
    """Arithmetic mean of cycle RT per microphone; sample SD, not XL2 uncertainty."""
    summary={}
    for ch in channels:
        summary[ch]={}
        for b in bands:
            entries=[c[ch][b] for c in cycles]
            vals=[e['t60'] for e in entries if e['t60'] is not None and
                  np.isfinite(e['t60']) and e['t60']>0]
            summary[ch][b]=dict(n=len(vals), mean_s=float(np.mean(vals)) if vals else None,
                sd_s=float(np.std(vals,ddof=1)) if len(vals)>1 else None,
                complete=len(vals)>=3 and len(vals)==len(entries),
                warnings=sorted({w for e in entries for w in e['warnings']}))
    return summary
