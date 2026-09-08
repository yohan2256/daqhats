# Floor Impact Sound Meter

Laptop-side measurement software for the Raspberry Pi acquisition server
(`pislm.py`). Communication, standards maths, session handling and the GUI all
work, and it runs without hardware.

## 2026-09-08 measurement recovery update

See [PATCH_NOTES_20260908.md](PATCH_NOTES_20260908.md) for installation,
changed capture semantics, regression results and hardware validation limits.
Heavy-impact Fmax now comes from full-rate raw waveform with a 3 s pre-roll;
the live band stream is used only for the display. Background remains Leq.

## Running

```
run.bat --demo     ← no hardware, uses the built-in simulator
run.bat            ← real hardware
```

The first run installs the required packages. To do it by hand:

```bash
pip install -r requirements.txt
python run_gui.py --demo
python run_gui.py --host 192.168.0.42
```

Once the window is up: **Connect → 1. Session → Apply session → Start scan →
2. Measure → Measure background, then Capture per source position →
3. Reverberation → 4. Rating**. **Reset…** on the Measure tab discards data
at a scope you choose — one source position, all measurements, the background,
the reverberation, or the lot — and puts the current position back to 1. Instrument details (resampling, sample rate,
filter order, weighting, WAV recording) live under **Settings → Instrument
options** (Ctrl+,), split across four tabs so the dialog fits on a laptop screen.

## How a measurement is taken

Five source positions by default. The measurement table shows **every
1/3-octave band** rather than a broadband figure — the broadband number is
derived from the bands and tells you nothing they do not, while a single bad
band is invisible without them. Columns are rebuilt when the impact source
changes, so heavy readings can never appear under light band headings.

Below the table is a **running estimate of the single number**, so a result
nowhere near the 49 dB limit shows up at position 2 rather than after packing
up. It is marked `provisional` and lists what is still missing — positions not
yet measured, no reverberation, no background — because an estimate from half
the data can move several decibels before the end. It says `final` only once
nothing is outstanding, and it lists only what this source actually needs: a
heavy set never asks for a reverberation time, because `L'iA,Fmax` is not
standardised.

**You move the source; the receivers are recorded together.** With six channels
one excitation records every ticked channel at once. Exciting once per channel
would let excitation variation leak between receiver positions.

- Analysis is **always 1/3 octave with a 6th-order Butterworth filter**
- Band edges come from the **IEC 61260-1 exact midband frequencies**
  (nominal 3150 Hz is really 3162.28 Hz)
- The spectrum display is **one panel per channel**, with the vertical axis
  labelled `Lp,Fmax [Z]` or `Lp,Leq [Z]` so the quantity is never ambiguous

| Source | Bands | Per band | Single number | Time weighting |
|---|---|---|---|---|
| Tapping machine (light) | 100–3150 Hz | `Leq` (RMS, computed by the Pi) | **`L'nT,w`** | Fast, display only |
| Rubber ball / bang (heavy) | 50–630 Hz | `Fmax` | **`L'iA,Fmax`** | Fast (the quantity) |

### Heavy and light are measured as one set

Both halves stay open at once, because the source is often walked position by
position — position 1 heavy then light, position 2 heavy then light — rather
than finishing one source before starting the other. **Switch source** on the
Measure tab moves between them, and the label beside it shows both halves'
progress so neither is forgotten.

The Pi has to be reconfigured on every switch: the two need different bands
(50–630 vs 100–3150 Hz) *and* different per-band quantities (Fmax vs Leq), so
there is no way to record both from one scan. The scan stops and restarts.

What is shared, and measured only once:

| Shared | Why |
|---|---|
| Room volume, reverberation | Same room either way. Two answers for one room would be worse than useless. Only the light half actually consumes them — heavy is not standardised |
| Calibration, channels, site, operator | Same rig, same job |
| **Background, 50–3150 Hz in one sweep** | Same room noise. Each half trims it to its own bands at analysis time |

The two sessions save to separate files, and each carries its own
measurements, band set and rating. A background that does not cover every band
a source needs counts as *unmeasured* for that source rather than being
partially applied — half a correction applied silently is worse than none.

### The two sources are rated by different procedures

국토교통부 고시 제2022-868호 (2022-12-28) replaced both of the old inverse-A
quantities — but with different things, so there is no single "the rating"
step:

| Source | Rated by | Standardised? | Quantity |
|---|---|---|---|
| Tapping machine | ISO 717-2 reference-curve shifting | yes, `−10 lg(T/T0)` | `L'nT,w` (가중 표준화 바닥충격음레벨) |
| Impact ball | A-weighted energy sum of the bands | **no** | `L'iA,Fmax` (A-가중 최대 바닥충격음레벨) |

Both are checked against the same 49 dB post-construction limit, which makes
the distinction easy to lose: summing A-weighted bands for a tapping machine
yields a perfectly plausible number that simply is not `L'nT,w` and is not
comparable with the limit. The rating is a whole number of decibels for light
impact (curve shifting always is) and a fractional one for heavy — a quick way
to tell at a glance which procedure produced a figure.

**Heavy impact is not normalised.** `L'iA,Fmax` is the A-weighted Fast maximum
*as measured*; the `−10 lg(T/T0)` of formula (1) belongs to `L'nT` and to light
impact only. Applying it to a heavy set anyway is a silent error of the worst
kind — the number stays plausible and the sign follows the room, so a live room
(T = 0.8 s) reads about 2 dB low and a dry one reads high, against a 49 dB pass
line. Measured on this code before it was fixed: the same data rated 60.2 dB at
T = 0.3 s and 54.2 dB at T = 1.2 s. `Session.evaluate()` now standardises only
when `ImpactSource.requires_standardisation` says so, and three tests pin it.

### A-weighting is applied as band values, not as a filter

ISO 717-2 applies A-weighting **by adding per-band values to the 1/3-octave
spectrum**, not by filtering the signal.

```
measure  : the Pi always sends Z (unweighted) band levels
compute  : L_A = 10 lg Σ 10^((L_i + A_i)/10),  A_i at the exact midband frequency
```

So the device frequency weighting is **fixed at Z** regardless of source.
Filtering instead would make the band levels themselves weighted, forcing the
report spectrum to be un-weighted again and risking a double weighting.
`a_weighted_single_number()` refuses an already-weighted spectrum.

Cross-check: **weight-then-average** and **average-then-weight** must agree
exactly, because energy averaging and energy summing commute. Measured
difference: 0.000000 dB, pinned by a test. A mismatch would mean dB values are
being averaged arithmetically somewhere.

## Reverberation and the analog output

Reverberation time is a **required** input for L'nT and L'n, which means it is
required for the **light** half — the heavy rating `L'iA,Fmax` is not
standardised and needs no T at all, so a heavy-only job can skip this tab. The
**3. Reverberation** tab offers three ways to get it:

| Excitation source | How it works |
|---|---|
| **Pi analog output** | Drives the DT9837A DAC. **Output** plays only; **Output + measure** plays and analyses one pass |
| **External speaker** | Three routes: play through **this computer's sound card** (pick the device from the list), bracket an externally-played signal with **Start recording** / **Stop and analyse**, or export a sweep file and play that |
| **Manual entry** | Type one T per band — for reverberation already measured on other equipment |

**Playing through this computer** is the easiest of the three: the program
knows which signal went out and when, so nothing has to be kept in step by
hand. It needs the optional `sounddevice` package (`pip install sounddevice`);
without it that option reports itself unavailable and the other routes carry
on working. The signal is generated at the Pi's own sample rate, which matters
for MLS — its sequence is a list of samples, so a rate mismatch returns noise.

For the exported sweep, use **Save sweep WAV…** and play *that exact file*.
The analysis builds its inverse filter from the same parameters, so a
different file silently returns noise instead of an impulse response.

### The decay is kept as a WAV too

With recording enabled, every reverberation pass writes
`<time>_<heavy|light>_reverb_<method>_<signal>.wav` alongside the impact
recordings. A T20 is a fitted number, and without the decay it came from there
is no way to check it later or to re-run it as T30.

This is the RAW_DUMP the analysis actually ran on, not a second recording of
roughly the same moment — the data is already in hand, so it costs nothing on
the wire. Columns are the microphones that were analysed, in ascending channel
order, and the sidecar carries `purpose`, `method` and `excitation`. If the
write fails the measurement still stands; the reverberation time matters more
than the souvenir of it.

### The ring buffer sets the longest window

`get_raw` pulls from the Pi's RAM ring buffer and **refuses a window longer
than the buffer** — "not enough data buffered". Only `set_storage` can grow it
and that needs a stop, which empties the buffer, so it cannot be fixed after
the sound has been made. The program trims the request to what is there and
says so; that keeps the decay, because `get_raw` returns the most *recent*
samples. Playing through the sound card checks up front instead, and refuses
rather than making a sound it could not analyse.

Raise **Raw buffer** under *Settings → Instrument options → Sampling* before
measuring if you need long passes.

The whole fetch runs against **one wall-clock budget** covering the retries as
well as each attempt. Giving every retry the full timeout is what made a
failure look like a hang — three retries of a 90 s fetch is four and a half
minutes of a frozen-looking window, which the operator cannot tell from a
crash. Progress is reported while it waits, and it gives up inside the budget.

**Several microphones at once.** Tick as many receiver channels as you have
positions; T is computed per channel and averaged per band. The mean is
**arithmetic**, per ISO 3382-2 — T is a time, not a level, so the energy
averaging used everywhere else in this program would be wrong here. A channel
whose decay cannot be fitted in some band is left out of that band's mean
rather than dragging it down, and the table shows the *worst* position's
correlation so one bad microphone stays visible.

Choose the signal on the **3. Reverberation** tab and press **Output** (play
only) or **Output + measure** (play and analyse in one go).

| Signal | Method | Notes |
|---|---|---|
| White / pink noise | Interrupted noise | Simple; each run is one stochastic realisation, so average several |
| Exponential sweep | Impulse response (ISO 18233) | Best signal-to-noise; distortion lands before the direct sound |
| MLS | Impulse response (ISO 18233) | Deterministic; the period must exceed the decay |

The server reports a `start_index` on the DATA grid when playback begins, which
locates the excitation inside the recording. §10 is explicit that this is a
software timestamp, not a hardware-verified alignment: the USB latency before
the DAC's first real sample is neither measured nor removed.

A sweep does not care — convolving with the inverse filter finds its own peak.
MLS very much does, because its deconvolution is circular:

| `start_index` error | Sweep | MLS, uncorrected |
|---|---|---|
| 50 samples (1 ms) | 0.393 s | 0.419 s |
| 480 samples (10 ms) | 0.393 s | **fails** |
| 4800 samples (100 ms) | 0.393 s | **fails** |

Since a plausible USB latency sits inside that range, the client recovers the
error itself rather than trusting the index. Slicing a steady-state period δ
samples early is exactly a circular shift, so the arrival comes back at
`length − δ`; finding it gives δ, and the analysis window is re-sliced by that
much. The arrival is located with ISO 3382-2's start-point rule (20 dB below
the peak of a smoothed energy envelope) rather than by the largest single
sample — see the traps below. Corrected, every offset from 0 to 24 000 samples
returns the same answer.

Traps worth knowing about, all hit during development:

- **MLS tap indexing.** Exponent `t` maps to register index `t − 1`. The
  plausible `order − t` mapping still yields a full-period sequence, so a
  period check passes, but the circular autocorrelation collapses from N to
  about 1 and the deconvolution silently returns noise. `mls_peak_to_sidelobe()`
  must equal exactly 2^order − 1.
- **MLS period versus decay.** Deconvolution is circular, so a decay longer
  than one period wraps onto the start of the impulse response.
  `mls_minimum_order()` picks a period of at least twice the expected T60.
- **Correcting the alignment by the loudest sample.** In any response with a
  noise-like head the biggest sample sits slightly after the true arrival, and
  correcting by that error rotates the loudest part of the impulse response to
  the end of the array — where Schroeder integrates it as a noise floor. It
  read 0.552 s instead of 0.440 s and made a 0.8 s room unmeasurable.
- **Analysing a period the recording does not contain.** Falling back to an
  earlier period lands in the build-up, before steady state, where the circular
  assumption does not hold. A 0.6 s room read 6.98 s. The deconvolver now
  refuses a recording shorter than the excitation instead.
- **`repeats` meaning different things on each side.** §10 has the server play
  exactly `repeats` whole periods; the client used to generate `repeats + 1`
  and so sliced one period too late, into the decay tail. `to_command()` now
  sends the effective period count so the two cannot disagree, and the
  protocol default of 1 is raised to 2 — a single period cannot be
  deconvolved at all.

The protocol side is now `PROTOCOL.md` §10. The Pi does not implement it yet,
but the client, the simulator and the GUI do, so it works the day the server
answers those commands. One gap remains in the spec: §10 lets `output_rate`
differ from the AI `sample_rate`, which is harmless for a sweep (defined in
continuous time) but silently breaks MLS, whose sequence is a list of samples.
Worth requiring the two to match for `signal: "mls"`.

## Raw WAV recording

Enable it under **Settings → Instrument options → Recording**. The program
turns the raw DATA stream on for the duration and puts it back afterwards —
the recorder is fed by DATA frames, and raw is the heaviest thing on the link
(§6: it crowds out everything else), so it is not left on.

| Setting | Choices |
|---|---|
| Scope | **Capture windows only** (recommended) / continuously while scanning |
| Format | 32-bit float (lossless) / 24-bit PCM / 16-bit PCM |
| PCM full scale | The Pa or V value that maps to full scale |

- **float32 stores Pa/V values as they are** — no scaling, no loss. The standard
  `wave` module cannot read it (format code 3), so check your analysis tool.
  PCM opens anywhere but clips above full scale; clipped samples are counted in
  the sidecar.
- **One file, every channel.** All six channels are one measurement, so they
  belong together; columns follow the global channel number, matching what is
  on screen. The file only advances to the point the slowest device has
  reached — writing each device as it arrives would stagger the columns
  against each other — and a device that never sends is left as silence and
  named in the sidecar rather than taking the whole recording down.
- **Recording is not the same as `stream_raw`.** The checkbox *Stream raw
  waveform continuously* makes the Pi push DATA frames the whole time, which
  is the heaviest thing on the link (§6). Recording switches it on only for
  the capture and puts it back afterwards. Reverberation does not need it at
  all — RAW_DUMP is an on-demand pull from the ring buffer (§4), so asking for
  a dump no longer turns raw streaming on.
- **`sample_locked` says whether the columns really line up.** Separate
  devices share a time base only while resampling is active (on by default).
  With it off they run on independent clocks, so the sidecar carries a warning
  and the file must not be used for cross-device timing.
- **Lost spans are zero-filled to preserve the time axis.** Without that the
  file gets shorter at the gap and every impact after it shifts earlier —
  invisible in the file itself. `start_index` (pislm/4) gives the exact sample
  count, and gap positions go into the sidecar.
- A **`.json` sidecar** of the same name carries channel order, per-device
  channel lists, units, sensitivity, sample rate, epoch and gaps. A WAV header
  cannot hold any of it.
- **Recording is independent of the display.** Hiding a channel on screen does
  not drop it from the file — the recording is the raw record of the whole rig.

Size is about **659 MB (float32) or 494 MB (24-bit) per 10 minutes** at
6 channels / 48 kHz. The dialog shows a live estimate.

Both live meters show **only the ticked receiver channels**. An unused input
still streams, and drawing it squeezes the ones that matter.

**The meters show the maximum over each refresh interval, not a point sample.**
A tapping machine runs at ten impacts a second and the display refreshes at
20 Hz — exactly two samples per impact, right at Nyquist. Keeping one value
per frame made the reading land on a peak or in a gap depending on drift, and
the meter flickered over tens of decibels while the level itself moved five.
An interval with no frame holds the previous reading rather than blanking.
The capture path always used the whole frame, so this was only ever a display
fault; recorded results and WAV re-analysis were unaffected.

## Layout

| Path | Contents |
|---|---|
| `PROTOCOL.md` | Pi wire specification (`pislm/4`, final) |
| `PROTOCOL_v4_AMENDMENT.md` | The amendment proposal (adopted; kept for background) |
| `PROTOCOL_OUTPUT.md` | The output proposal (adopted as §10; kept for background) |
| `pislm/` | Communication library (reads both v3 and v4) |
| `pislm/standards/` | **Standards maths** — background, normalisation, rating, reverberation |
| `pislm/session.py` | **Session** — positions, channels, JSON save/load, both rating systems |
| `pislm/recorder.py` | **WAV recording** — streaming writer, gap filling, sidecars |
| `pislm/excitation.py` | **Excitation** — white/pink/sweep/MLS and their deconvolution |
| `app/audio.py` | Playback through this computer's sound card (optional) |
| `check_device.py` | Field check against real hardware before measuring |
| `app/` | **GUI** (PySide6) — live meters, multi-channel capture, rating, options |
| `pislm_sim.py` | Protocol-compatible simulator, for development and CI |
| `pislm_monitor.py` | CLI check (levels, band bars, benchmark, integrity verdict) |
| `run_gui.py`, `run.bat` | Launchers |
| `tests/` | 373 tests |

## Protocol `pislm/4`

The amendment was adopted and `PROTOCOL.md` is now `pislm/4`. The client detects
the version from the handshake `protocol` string, so it still works against an
older server.

| Item | Purpose |
|---|---|
| `start_index` | `u64` on every stream frame; locates loss exactly |
| `epoch` | Wall-clock time of sample index 0 |
| `overload` | Per-channel clipping events and counters |
| Drop policy | DATA/BAND are best-effort; **LEVEL/BAND_LEVEL/MSG are not dropped** |

### Loss is graded by severity (§6, §9.12)

| Stream | What a gap means | Verdict |
|---|---|---|
| `DATA`, `BAND` | Designed behaviour; recoverable with `get_raw` | warning (still valid) |
| `LEVEL`, `BAND_LEVEL` | Even the reliable queue filled — main output damaged | **invalid** |
| Out of order / overload | Protocol violation / clipping | **invalid** |

```python
valid, reasons = pi.measurement_valid    # what blocks a report
warnings = pi.measurement_warnings       # what you should still know
```

## Provenance of the standards maths

Wrong standards maths still produces plausible numbers, so every constant and
procedure is tagged in the source.

| Tag | Meaning |
|---|---|
| `[SPEC]` | Read directly from the ISO text |
| `[DERIVED]` | Derived from physics because the text was unavailable — **verify** |
| `[PRACTICE]` | Not in the standard; standard industry handling |

**Read from the text**: 국토교통부 고시 제2022-868호 (which single number applies
to which source), ISO 717-2:2020 Table 3 and the shifting rule (32.0 /
10.0 dB, octave −5 dB), the rounding algorithm (§4.3.1 footnote 1),
ISO 16283-2:2020 formulas (1)(2)(3), (7)(8)(9) and (5)(6), the 6 dB / 1.3 dB
background rule, and the required band sets.

**Not verified — check before issuing a report**:

1. **`standardized_max_level()` (ISO 16283-2 formula 4)** — the equation layout
   was broken in the public preview PDF. Derived from formulas (5)(6) of the
   same standard plus a physical model; the derivation reproduces the standard's
   own `C = 1.7275/T`, which is a good sign. Identity, monotonicity and
   direction are tested, but **the absolute value is not verified**. It is not
   on the Korean path — the post-construction heavy rating is not normalised —
   so nothing in `Session.evaluate()` calls it; verify it only if you report
   `L'i,Fmax,V,T` deliberately.
2. **KS F 2863 inverse-A curve** — paywalled, so the values are **not bundled**
   and calling it fails explicitly rather than guessing. Supply the table via
   `InverseACurve.load()`; the default generated curve is flagged unverified in
   the UI.

### Things the tests pinned down

- **"Measured curve equals the reference curve" rates 58, not 60.** Sixteen
  bands share the 32 dB budget, so an average 2 dB excess is allowed. The octave
  path (5 bands, 10 dB, −5 dB) also gives 58 — a useful cross-check.
- **Schroeder truncation artefact.** The curve always dives to −∞ where the
  record ends, so T30 could be "computed" on a signal with 2 dB of decay. The
  last 5 % is excluded and the failure mode is a test.
- **Python `round()` disagrees with the standard.** Banker's rounding gives
  2.25 → 2.2; ISO 717-2 wants 2.3.
- **Curvature wobbles by a few % on a single measurement.** T20 sees only a
  20 dB window, which is why ISO 3382-2 asks for averaging.

## Testing

```bash
python -m pytest tests -q      # 373 tests, about 130 s
```

Run in two halves if you are impatient; `test_protocol_v4.py` is the slow one.
The simulator runs with fragmenting and response shuffling enabled so the
protocol traps in §9 are exercised for real, and it can inject deliberate frame
loss (`--drop`) and overloads (`--overload`).

### Bugs the tests caught

1. **RAW_DUMP metadata discarded too early** — the second device lost its
   channel count and reshaped to one channel.
2. **§9.13 ordering race** — chunks arriving before the `get_raw` response were
   reshaped without metadata, silently producing a single-channel array.
3. **Shutdown order** — closing the socket before draining the worker broke the
   pending command and popped an error over a closing window.
4. **`str`-subclass Enum through QVariant** — `currentData()` returned a plain
   string, so changing the impact source raised `AttributeError`.
5. **Simulator send deadlock** — `sendall` on the generator thread stalled the
   whole server when the socket buffer filled. Fixed with a send queue per §6;
   the first attempt dropped the queue head regardless of frame type, which
   removed LEVEL frames and was caught by an existing test.
6. **Band table cleared on apply** — `get_config` legitimately omits
   `band_table` while stopped, and clearing the index map there discarded every
   incoming BAND_LEVEL frame. The spectrum stayed blank until the scan was
   toggled by hand.
7. **WAV recording never wrote anything** — the recorder consumes DATA frames
   and nothing switched `stream_raw` on, so unless the operator had ticked it
   by hand no file appeared at all, while the recorder reported itself as
   running the whole time. It now fails loudly instead of silently.
8. **Two test classes with the same name** — the second silently shadowed the
   first, so a whole class of recording tests stopped being collected while
   the suite still went green.
9. **A shared simulator left mid-scan** — a test that deliberately closes a
   window with a stop command queued left the scan running, so the next test's
   Start click *stopped* it. The symptom appeared much later as "no data".
10. **The source selector wrote to the wrong half of the set** — picking the
    tapping machine while the heavy half was active gave the heavy session a
    light source, and with it light bands and a light rating for data measured
    with a ball.
11. **Capturing straight after a band reconfigure** — `get_config` still
    reports the previous table for a moment, and BAND_LEVEL frames whose index
    is not in the map are dropped, so the whole window went silently missing.
    The wait now compares the band *count*, not merely that some table exists.
12. **Switch source left the scan stopped while the window said "Scanning"** —
    `configure()` calls `pi.stop()` first, so the config commands no longer
    *need* stopping and `send_stopped()` never restarted anything. Worse, the
    job handler set the button to "Stop scan" unconditionally, so the next
    click went to `stop` and the operator had to press it twice. Both halves
    are fixed: the scan is put back the way it was found, and the button is
    read from the actual running state.
13. **A pislm/4 server reported as pislm/3** — `measurement_valid` read an
    inactive `GapDetector` as "the server is old", but the detector only
    becomes active once a frame carrying a `start_index` has arrived, which
    is simply false while the scan is idle. `measurement_status` now reads
    the version from the handshake and keeps `unknown` apart from `invalid`;
    a real pislm/3 server still says so.
14. **"lost 16,122 samples" was unreadable and undiagnosable** —
    `reliable_missing` sums every LEVEL and BAND_LEVEL counter (over a hundred
    on a six-channel rig) on the level output grid, so a second of dropout
    reads as a catastrophe. The message now gives streams affected and
    seconds, and `gaps.diagnosis()` tells a real stall from a server that
    numbers LEVEL frames on the audio grid — identical totals, opposite
    causes, distinguished by the pattern of the gaps.
15. **A frame queue nobody drained** — `StreamClient` keeps a 4,096-frame
    queue for `next_frames()` consumers. The GUI reads through `on_frame`
    instead and never touched it, so it filled in seconds and then counted
    every frame as an overflow: 5,344 in twelve seconds, reported as "the
    consumer is slower than the stream". Nothing was lost — callbacks and the
    gap detector run before the queue — but the warning was permanent and
    meaningless. The GUI now opens the stream with `stream_queue_size=0`.
16. **The level meter aliased against the tapping machine** — the display took
    one value per LEVEL frame and threw the rest away. At ten impacts a second
    against a 20 Hz refresh that is exactly two samples per impact, right at
    Nyquist, so the reading landed on a peak or in a gap depending on drift:
    worst frame-to-frame jump 37.7 dB on a level that moved 5 dB. A rubber
    ball gets forty samples per impact and never aliased, which is why heavy
    looked fine and light did not. The display now keeps the maximum over each
    refresh interval (2.6 dB worst jump) and holds the last value through an
    interval with no frame. Capture always used the whole frame, so no
    recorded result was ever affected.
7. **MLS tap indexing** — a full-period sequence with a flat autocorrelation;
   the deconvolution returned noise and nothing else would have revealed it.
8. **MLS period selection** — the deconvolver counted periods from the
   recording length, which lands past the end of the excitation in the decay
   tail, where there is no sequence to correlate against.

## Not done yet

1. **Report output** — currently text export only; a docx/PDF template is needed
2. **Position layout checks** — ISO 16283-2 Annexes E/F position rules
3. **Continuous session recording** — only ring-buffer snapshots today
