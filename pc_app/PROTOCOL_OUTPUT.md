# PiSLM Protocol Extension — Analog Output

> **Superseded.** This proposal was adopted into `PROTOCOL.md` as §10, which is
> now the authoritative text. It is kept only for background. §10 additionally
> specifies `end_index` and `completed` on `output_finished`, and documents the
> real accuracy of `start_index` — read it, not this.

**For:** the `pislm.py` implementer on the Raspberry Pi
**Purpose:** drive the DT9837A analog output so the laptop can excite the room
for reverberation measurement.
**Status:** proposed. The client, the simulator and the GUI already implement
it, so the day the Pi answers these commands it works end to end.

Reverberation time is a **required** input for both L'nT and L'n, and ISO
3382-2 needs an excitation source. Today the operator has to bring a separate
amplifier and signal source and coordinate it by hand with the measurement.
Driving the DT9837A output removes that: one button, and the software knows
exactly what was played and when.

---

## 1. Commands

All are ordinary control-port commands (§1 of PROTOCOL.md). They follow the
existing rules: `id` is echoed, `ok` must be checked.

### `set_output` — configure the generator

Requires the output to be stopped. Does **not** require the scan to be stopped:
the whole point is to play while measuring.

| field | type | meaning |
|---|---|---|
| `signal` | `"white"` \| `"pink"` \| `"sweep"` \| `"mls"` | waveform to synthesise |
| `seconds` | float | length of one pass (ignored for `mls`, which is a whole number of periods) |
| `level_dbfs` | float | peak amplitude relative to output full scale, ≤ 0 |
| `f_min`, `f_max` | float | band limit for noise; start and end for `sweep` |
| `mls_order` | int | register length; sequence is 2^order − 1 samples |
| `channel` | int | output channel on the generating device |
| `tail_seconds` | float | silence appended after the signal |
| `repeats` | int | how many times to play it |

```json
{"id": 11, "cmd": "set_output", "signal": "sweep", "seconds": 3.0,
 "level_dbfs": -20.0, "f_min": 50, "f_max": 5000, "channel": 0,
 "tail_seconds": 2.0, "repeats": 1}
```

Result: the settings as applied, plus what the Pi worked out from them:

```json
{"type": "response", "id": 11, "ok": true, "cmd": "set_output",
 "result": {"signal": "sweep", "seconds": 3.0, "level_dbfs": -20.0,
            "f_min": 50.0, "f_max": 5000.0, "mls_order": 16, "channel": 0,
            "tail_seconds": 2.0, "repeats": 1,
            "output_rate": 48000.0, "total_samples": 240000,
            "total_seconds": 5.0, "device": 1}}
```

### `output_start` / `output_stop`

```json
{"id": 12, "cmd": "output_start"}
```
```json
{"type": "response", "id": 12, "ok": true, "cmd": "output_start",
 "result": {"running": true, "signal": "sweep", "total_seconds": 5.0,
            "start_index": 480000, "device": 1}}
```

**`start_index` is the important field.** It is the DATA-grid index (§2.1 of
PROTOCOL.md) of the sample at which the first output sample leaves the
converter. With it the laptop can locate the excitation inside the recorded
waveform to the sample, which is what makes sweep and MLS deconvolution work
at all. Without it the operator is back to guessing where the signal started.

If the scan is not running there is no index to give; return
`"start_index": null` and the client falls back to a timer.

`output_stop` returns `{"running": false, "samples_played": N}`.

### `output_status`

Works any time: `{"running", "signal", "elapsed_seconds", "remaining_seconds",
"start_index", "device", "channel"}`.

---

## 2. Events

Broadcast on both ports like every other event (§3).

```json
{"type": "event", "event": "output_started", "signal": "sweep",
 "start_index": 480000, "total_seconds": 5.0, "device": 1, "channel": 0}
{"type": "event", "event": "output_finished", "samples_played": 240000,
 "end_index": 720000, "completed": true}
```

`completed` is `false` when `output_stop` cut it short. A client that is
waiting for the pass to end needs to tell the two apart — a truncated sweep
must not be deconvolved.

---

## 3. Handshake additions

```json
"output": {"available": true, "device": 1, "channels": [0, 1],
           "output_rate": 48000.0, "full_scale_volts": 10.0,
           "running": false, "signal": null}
```

`available: false` on hardware without an output; the client then greys out
the button rather than failing at the moment it is pressed.

---

## 4. Signal definitions

The Pi and the laptop must generate **bit-identical** signals for `sweep` and
`mls`, otherwise the deconvolution produces noise. The reference implementation
is `pislm/excitation.py`; port it rather than reimplementing from scratch.

**Exponential sine sweep** (Farina):

```
s(t) = sin( (w1·T / ln(w2/w1)) · (exp(t/T · ln(w2/w1)) − 1) )
w1 = 2π·f_min,  w2 = 2π·f_max,  T = seconds
```

Apply a 20 ms raised-cosine fade at both ends, then normalise to unit peak and
scale by `10^(level_dbfs/20)`.

**MLS** — Fibonacci LFSR, register initialised to all ones, output taken from
the last register cell, `{0,1}` mapped to `{−1,+1}`:

| order | taps (polynomial exponents) |
|---|---|
| 8 | 8, 6, 5, 4 |
| 10 | 10, 7 |
| 12 | 12, 6, 4, 1 |
| 14 | 14, 5, 3, 1 |
| 15 | 15, 14 |
| 16 | 16, 15, 13, 4 |
| 17 | 17, 14 |
| 18 | 18, 11 |

⚠ **Exponent `t` maps to register index `t − 1`.** The plausible-looking
`order − t` mapping also yields a full-period sequence, so a period check
passes, but the circular autocorrelation collapses from N to about 1 and the
deconvolution silently returns noise. This bit us during development. Verify
with: peak-to-sidelobe of the circular autocorrelation must equal exactly
2^order − 1.

**MLS is played back-to-back with no silence between periods**, at least two
of them. Deconvolution uses the last complete period, by which point the room
is in steady state — that is what makes the circular assumption valid.

Noise (`white`, `pink`) is band-limited to `[f_min, f_max]`. Pink is shaped as
1/√f in the frequency domain (exact −3 dB/octave), not with a filter cascade.

---

## 5. Safety

- Clamp `level_dbfs` to ≤ 0 and refuse anything above.
- **Ramp the output to zero over about 10 ms on `output_stop`** and on any
  error. A hard cut into a power amplifier makes a loud click and can damage
  a driver.
- Stop the output if the scan stops, and say so in the `output_finished`
  event.
- On startup the output must be at zero until `output_start`.

---

## 6. Implementation checklist

- [ ] `set_output` / `output_start` / `output_stop` / `output_status`
- [ ] `output` block in the handshake, `available` correct for the hardware
- [ ] `start_index` on the **DATA grid**, resampled if resampling is active
- [ ] `output_started` / `output_finished` events, `completed` flag correct
- [ ] Signals generated exactly as §4 — verify MLS peak-to-sidelobe = 2^n − 1
- [ ] MLS plays whole periods back to back, no silence between
- [ ] 10 ms ramp down on stop; zero output at startup
- [ ] `level_dbfs` clamped to ≤ 0

Verify against the laptop with:

```bash
python pislm_monitor.py <Pi> --output sweep --output-seconds 3
```
