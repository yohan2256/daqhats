# DSP recovery update — 2026-09-08

A DSP worker can drop a block while acquisition and the raw ring buffer keep
running. Resetting a narrow band filter and immediately publishing its output
turns the restart transient into a false impact on the meter.

This change runs the filter and detector through a recovery interval before
publishing again. RecoveryGate uses a slowest-pole decay estimate of 120 dB
plus ten detector time constants; this is a numerical guard, not a verified
IEC filter-class tolerance. Pool LEVEL/BAND_LEVEL/BAND and inline levels use
startup guards. Pool gaps reset broadband SOS state as well as band/detector
state and rearm the guards. Inline waveform BAND retains its existing startup
behavior. Skipped outputs advance the existing start_index counters; they are
never replaced by fabricated zero-dB samples. A repeatedly overloaded worker
may remain unavailable, which clients must show as missing/recovering data.

The slot-truncation tail is now carried to the next task, preserving sample
order. Raw dump start_index is obtained under the same ring-buffer snapshot
lock as the data; an append during copying cannot relabel an old waveform.

Live levels remain decimated display values and are unsuitable for final
heavy-impact Fmax. The paired PC update computes Z band filtering, squaring,
Fast integration and maximum at the original device rate. The PC uses its
existing base-ten band convention; this server retains its base-two band
convention. Consequently live readings and final raw results need not match.

## Verification

Run from this directory:

```sh
python -m pytest tests/test_recovery.py -q
```

Five hardware-free tests cover tone/gap recovery, exact index accounting,
truncation ordering, concurrent raw append and inline startup suppression.
At 51.2 kHz, order 6, Z/Fast/20 Hz, a 1 kHz tone and 1024-frame gap produced
no above-zero-dB false peaks in the <=250 Hz bands; all bands recovered.
The 1 kHz reading remained approximately 90.99 dB re 20 µPa for 1 Pa peak.

## Installation / field check

Use the fix branch together with the updated PC package. Restart the PiSLM
service using its existing deployment procedure. Preserve the site's config
and microphone calibration. On the PC set the raw buffer to at least capture
seconds + 3, start scanning and wait at least 3 s before a heavy capture.

Recheck on the actual MCC172/DT9837 hardware under the normal channel count,
network load and band settings. Log DSP drops and overruns, compare captured
results to a calibrated reference instrument, and verify that a temporary DSP
gap is marked unavailable then recovers. A synthetic regression is not a
hardware validation or a certification of KS/ISO/IEC compliance.
