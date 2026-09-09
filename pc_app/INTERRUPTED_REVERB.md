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
