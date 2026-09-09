"""Read-only acoustic capture jobs. All blocking work runs outside the GUI."""
import time
import numpy as np
from .control import CommandError
from .standards.sound_level import PREHISTORY, levels
from .standards.airborne import BANDS
from .standards.interrupted import interrupted_spectrum


def capture(pi, seconds, channels, *, role='SLM', time_weighting='Fast', fraction=3,
            bands=BANDS, cancelled=lambda: False, progress=lambda text: None):
    seconds = float(seconds)
    pi.refresh()
    if not pi.config.running:
        raise ValueError('Start the scan in the main window first')
    if not channels or len(set(channels)) != len(channels):
        raise ValueError('Select distinct measurement channels')
    for ch in channels:
        if not pi.config.channel_info(ch).calibrated:
            raise ValueError(f'Ch{ch+1} is not calibrated in Pa')
    if not np.isfinite(seconds) or seconds <= 0 or seconds + PREHISTORY > pi.config.buffer_seconds:
        raise ValueError('Capture plus 10 s history must fit the raw buffer')
    progress('Preparing raw history — keep the source stable')
    deadline = time.monotonic() + 20
    def check():
        if cancelled():
            raise RuntimeError('Capture cancelled')
    while True:
        check()
        left = deadline-time.monotonic()
        if left <= 0:
            raise TimeoutError('Raw history unavailable; check scan/trigger')
        try:
            probe = pi.fetch_raw(seconds=PREHISTORY+.25, timeout=min(2.,left))
        except CommandError as exc:
            if 'buffered' not in exc.error:
                raise
            probe = {}
        if all(any(ch in d.channels and d.start_index >= 0 and
                   d.channel(ch).size >= int((PREHISTORY+.25)*d.sample_rate)
                   for d in probe.values()) for ch in channels):
            break
        time.sleep(.5)
    check()
    progress('Capture now — switch the noise OFF for decay' if role == 'T' else f'Capture now — {seconds:g} s')
    end = time.monotonic()+seconds
    while time.monotonic() < end:
        check()
        time.sleep(min(.1, max(0,end-time.monotonic())))
    check()
    dumps = pi.fetch_raw(seconds=seconds+PREHISTORY, timeout=30.)
    check()
    valid,reasons = pi.measurement_valid
    if not valid:
        raise ValueError('Acquisition integrity check failed: ' + '; '.join(reasons))
    output = {}
    for ch in channels:
        found = [d for d in dumps.values() if ch in d.channels]
        if len(found) != 1 or found[0].start_index < 0:
            raise ValueError(f'Complete indexed raw dump missing for Ch{ch+1}')
        d = found[0]
        x = d.channel(ch)
        if role == 'T':
            n = int(seconds*d.sample_rate)
            if x.size < n or not np.isfinite(x).all():
                raise ValueError('Incomplete/non-finite decay')
            _,decays = interrupted_spectrum(x, d.sample_rate, BANDS, method='T20')
            if any(b not in decays or not decays[b].reliable for b in BANDS):
                raise ValueError('Decay fit incomplete/unreliable; increase signal-to-noise and repeat')
            result = dict(bands={b:decays[b].t60 for b in BANDS},
                          decay_quality={b:dict(correlation=decays[b].correlation,
                                               range_db=decays[b].decay_range_db) for b in BANDS})
        else:
            result = levels(x,d.sample_rate,seconds,time_weighting=time_weighting,bands=bands,fraction=fraction)
        result.update(start_index=d.end_index-int(seconds*d.sample_rate), end_index=d.end_index,
                      sample_rate=d.sample_rate, channel=ch)
        output[ch] = result
    return output,dumps
