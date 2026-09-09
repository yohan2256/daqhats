# Interrupted-noise reverberation correction

The raw 27 s field recording `20260824_091455_light_reverb_T20_decay.wav`
contains about 20 s source-on. Passing the entire record to the legacy
Schroeder routine produced approximately 27–38 s fits at 125–3150 Hz, all
unreliable. The raw recording is not committed.

Noise mode now detects one sustained switch-off, retains source-on filter
history, and fits 10 ms linear mean-square band levels after interruption.
There is no Fast/Slow weighting or reverse integration in this path.
ISO 3382-2:2008 section 4.2.2.2 requires linear averaging shorter than T/12;
fits violating this interval are rejected.
Reference: https://standards.iteh.ai/catalog/standards/cen/703281f0-4554-4f7d-87ed-77c7b0fc83da/en-iso-3382-2-2008

Record at least 0.5 s steady source-on, the interruption, and at least 1 s
background after the decay. A background-only recording is rejected. The
trigger needs 20 dB broadband separation and supports one interruption per
record. Band fits separately require 30 dB range for T20 / 40 dB for T30.
Existing correlation/curvature gates remain conservative; selected microphones
and required bands must all pass before room correction can be stored.
The acoustic modes T capture includes its existing raw prehistory.
Sweep/MLS deconvolution and impulse-response integration are unchanged.

Validation: four unittest cases cover known synthetic 0.6 s decay, source-on
length, tail length, invalid inputs and insufficient background separation.
Run from pc_app: `python -m unittest discover -s tests -p test_interrupted.py`.
The field recording triggers at 20.20–20.26 s across five channels. Only
0/0/2/3/2 of 16 bands (100–3150 Hz) respectively pass existing quality gates;
thus the recording does NOT produce an accepted room correction. The two
August 12 background-only decay files were also rejected on all channels.

Limitations: no reference-instrument RT was supplied, so absolute field
accuracy is not established. No repeated-decay ensemble averaging or manual
trigger UI is added here. Low-frequency single-run fluctuations and filter
ring-down still limit fits. This is a conservative first correction, not a
claim of complete standards compliance. Full GUI regression was not rerun:
pytest/PySide6 are unavailable in this runtime. Modified Python files compile.

## XL2-style guided procedure (September 9 follow-up)

Open **Measurement modes → Reverberation — XL2 procedure / comparison**, or
**SET / 3 cycles — XL2** on the reverberation tab. Airborne/facade phase T also
opens this workflow. External noise recording uses the same dialog. The
operator controls the external pink-noise source; this does not control an
XL2 or its generator by USB.

1. Keep the source OFF and click SET. Three seconds of fresh background are
   captured. Each band shows its background level and target source-on level:
   background +35 dB for T20, +45 dB for T30.
2. Click START and follow NOISE (ON) / DECAY (OFF) countdowns for three cycles.
   Default ON/OFF durations are 5/5 seconds and adjustable before SET. Allow
   enough OFF time for the decay to reach background with at least 1 s tail.
3. Inspect CYC curves, regression lines and noise floors. AVRG is the arithmetic
   mean of per-cycle RT at each microphone. Repeat sample SD is displayed;
   it is NOT the XL2 uncertainty factor. Add cycles or exclude a bad cycle;
   excluded diagnostics remain in the JSON audit record.
4. Every selected band/microphone needs at least three usable fits. Incomplete
   bands cannot be applied. Correlation/curvature warnings are visible for
   operator review rather than automatically labelling the room invalid based
   on the legacy r>=0.98 gate. Applying requires an explicit curve/quality
   review checkbox. This review is not a standards-compliance certification.
5. Enter the XL2's band RT values to view differences in seconds. Match T20/T30,
   band resolution, position and source cycles. The optional 1/1-octave view
   covers 63–8000 Hz; mismatched bands cannot be applied to the impact session.
   No 1/3-octave RT values are inferred by combining octave RTs.
6. Export JSON (curves, SET, cycles, exclusions, quality and reference values)
   and CSV (cycles, mean, SD, XL2 differences). Applied impact results also
   retain diagnostics in the saved session; acoustic-mode sessions retain them
   in their T records. A fresh SET is required after calibration/rate changes.

Sources: NTi's [public procedure](https://www.nti-audio.com/en/applications/room-building-acoustics/reverberation-time)
requires SET in a quiet room and three on/off cycles. The manufacturer's
[XL2 manual](https://www.nti-audio.com/wp-content/uploads/XL2-Manual.pdf)
describes CYC/AVRG, per-band level markers, correlation and uncertainty, and
uses the term Schroeder method for its RT function. This implementation does
NOT claim firmware equivalence: trigger details, internal decay processing,
acceptance logic and uncertainty formula are not reproduced. Our noise path
remains documented direct short-time-power regression, with no blanket
Schroeder integration of the entire source-on recording. Matching the visible
procedure is a starting point for comparison against the user's XL2.

Validation: 10 sequence/backend tests plus the four prior interrupted-noise
tests pass. Two added GUI state tests are available but skipped in this runtime
because PySide6 is unavailable; no physical XL2 or Pi was exercised. The
uploaded 27 s file has only one interruption and is not treated as three
independent measurements. Absolute agreement with XL2 remains unverified.
