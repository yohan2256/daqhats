# PiSLM — Communication Protocol

Wire specification for talking to `pislm.py` running on the
Raspberry Pi. It is language-agnostic: any TCP client that follows this
document can receive levels/waveforms and control the acquisition devices.

- **Protocol version:** `pislm/3` (see the handshake).
- **Devices:** up to two IEPE acquisition devices — an MCC 172 DAQ HAT
  (2 channels) and a Data Translation DT9837A (4 channels) — for **6
  channels total**. Channels are numbered **globally** in device order
  (default: 0–1 = MCC 172, 2–5 = DT9837A); the handshake's `channel_map`
  gives the exact mapping. All frames and commands use global channel
  numbers.
- **Clocks are not synchronized between devices.** Each device runs its own
  ADC clock. Per-channel levels/metrics are unaffected; cross-channel phase
  analysis is only valid within one device.
- **Transport:** TCP. The Pi is the **server**; clients connect to it.
- **Two separate ports** (both configurable in `config.ini`, `[network]`):
  - **Control port** — default `5000`. Newline-delimited UTF-8 JSON, both
    directions: commands, responses, and events. No binary.
  - **Stream port** — default `5001`. Typed, length-prefixed binary frames:
    a JSON handshake, then the raw waveform, plus events. Bytes sent *to*
    this port are ignored.
- **Byte order:** little-endian for **all** binary fields.
- **Text encoding:** UTF-8 for all JSON.
- **Multiple clients:** allowed on both ports. DATA goes to stream clients;
  events are broadcast to all clients; a command response goes only to the
  control client that sent it. The two ports are independent — you can open
  the control port only, the stream port only, or both.

Each port sends a **handshake** immediately on connect (§3) describing the
current configuration, so a client can connect at any time and resynchronize.

---

## 1. Control port (default 5000)

Plain newline-delimited JSON, both directions. No framing, no binary.

### Sending commands (client → Pi)

Send **one UTF-8 JSON object per line**, terminated by `\n` (newline):

```
{"id": 4, "cmd": "set_sensitivity", "channel": 0, "value": 50}\n
```

- `cmd` (string, required): the command name (see §4).
- `id` (any, optional): echoed back in the response so you can match replies.
  Use a unique value (e.g. an incrementing integer) per command.
- Other fields are command-specific.

Commands may be pipelined (sent back-to-back). Responses preserve `id`, so
you do not need to wait for one reply before sending the next.

### Receiving (Pi → client)

Each line is one JSON object: the handshake (first line on connect), command
responses, and events (§3). Switch on the `type` field.

---

## 2. Stream port (default 5001)

Everything the server sends here is a **typed, length-prefixed frame**:

```
+--------+------------------+-------------------------+
| type   | length           | payload                 |
| 1 byte | 4 bytes uint32   | <length> bytes          |
| uint8  | little-endian    |                         |
+--------+------------------+-------------------------+
```

- `length` is the payload size in bytes (does **not** include the 5-byte header).
- Two frame types:

| type | name | payload |
|:----:|------|---------|
| `0x01` | DATA | `[4-byte device index]` + raw interleaved `float64` for that device, see §2.1. |
| `0x02` | MSG  | A UTF-8 JSON object (handshake on connect, then events). |
| `0x03` | BAND | Fractional-octave band **waveform** (decimated), see §2.2. |
| `0x04` | LEVEL | Broadband time-weighted **level** (dB), see §2.3. |
| `0x05` | BAND_LEVEL | Per-band time-weighted **level** (dB), see §2.4. |
| `0x06` | RAW_DUMP | One chunk of an on-demand `get_raw` buffer dump, see §2.5. |

**Reading loop (pseudocode):**

```
read 5 bytes -> (type, length)
read `length` bytes -> payload
if type == 0x01: device,float64[]         = payload            # raw waveform
if type == 0x02: json                     = payload            # msg / event
if type == 0x03: band_index,channel,float64[]  = payload       # band waveform
if type == 0x04: channel,float64[]        = payload            # level dB
if type == 0x05: band_index,channel,float64[]  = payload       # band level dB
if type == 0x06: dump_id,device,chunk,is_last,float64[] = payload  # raw dump
```

All `channel` fields are **global** channel numbers; `device` is the index
into the handshake's `devices` list.

**Which frames you get depends on configuration.** By default the monitor
behaves like a sound level meter: it sends `LEVEL` frames (and `BAND_LEVEL`
if bands are enabled) and keeps raw samples buffered for `get_metrics`
(§4). Raw `DATA` is sent only when `stream_raw` is on; `BAND` (waveform)
frames only when band output mode is `waveform`.

### 2.1 DATA payload layout

```
payload = [4-byte device uint32][interleaved float64 samples...]
```

Each DATA frame carries one **device's** block. The samples are
channel-fastest across that device's scanned channels:

```
chA[n], chB[n], chA[n+1], chB[n+1], ...
```

where `chA, chB, ...` are the device's channels in ascending order — their
**global** numbers are `devices[device].channels` in the handshake.

- Channels in the frame = `len(devices[device].channels)`.
- Samples per channel in a frame = `(length - 4) / 8 / num_device_channels`.
- Devices produce independent DATA frames (their clocks are not synced).
- Sample **value units** depend on calibration (see §5): volts (`V`) when the
  channel's sensitivity is `1000`, pascals (`Pa`) otherwise.

DATA frames are only sent while a scan is running (after `start`) and only
when `stream_raw` is enabled. The stream port ignores anything the client
sends to it.

### 2.2 BAND frames (type `0x03`) — fractional-octave output

Optional. When band output is enabled (`set_bands` / `[bands]` in
`config.ini`), the Pi runs a Butterworth band-pass filter per fractional-
octave band (1/3-octave by default) in real time, **decimates** each band to
just above twice its upper edge, and sends the result as BAND frames — one
frame per band per channel per block:

```
payload = [4-byte band_index uint32][4-byte channel uint32][decimated float64 samples...]
          |------------- 8-byte header ---------------|
```

- `band_index` maps to the `band_table` in the handshake (§3). The table is
  a **list with one entry per device** (band rates depend on each device's
  actual sample rate); resolve the channel's device via `channel_map`, then
  look the band up in that device's table entry.
- `channel` is the **global** channel number.
- Sample count in a frame = `(length - 8) / 8`.
- Low-frequency bands decimate heavily and therefore emit frames
  infrequently; high bands emit often. Frames with zero samples are not sent.
- The band samples are in the same units as the raw stream (Pa when the
  channel is calibrated, else V). The laptop applies time-weighting
  (Fast/Slow/Impulse), Leq, band SPL, A/C-weighting, etc.

Filtering is continuous across blocks (IIR state and decimation phase are
carried), so concatenating a band's frames reconstructs a single, gap-free
decimated time series for that band.

**Bandwidth:** high fractional-octave bands are wide in absolute Hz and
barely decimate, so the full 20 Hz–20 kHz set at 51.2 kHz is roughly **5× the
raw stream** for two channels (~36 Mbit/s) — marginal on Wi-Fi. Lower `f_max`
(the top bands dominate), use one channel, or set `stream_raw = false`. For a
sound level meter you usually want band **levels** (§2.4) instead, which are
tiny. See §5.1.

### 2.3 LEVEL frames (type `0x04`) — broadband time-weighted level

The sound-level-meter "needle": the Pi applies the configured **frequency
weighting** (A/C/Z) and **time weighting** (Fast/Slow/Impulse) to the
broadband signal and streams the resulting level in dB, downsampled to the
level output rate.

```
payload = [4-byte channel uint32][level float64 samples...]   # dB
```

- Sample count = `(length - 4) / 8`; the sample rate is `level.output_rate`
  from the handshake (e.g. 10 Hz).
- dB reference is 20 µPa when the channel is calibrated to Pa, else 1.0
  (see `units` in the handshake). This is L_AF, L_ZF, etc. per the weighting.

### 2.4 BAND_LEVEL frames (type `0x05`) — per-band time-weighted level

Same idea per fractional-octave band (the real-time spectrum). Each band's
decimated signal is Fast/Slow/Impulse time-weighted, and the A/C
frequency-weighting **offset for that band's center frequency** is added.

```
payload = [4-byte band_index uint32][4-byte channel uint32][level float64 samples...]  # dB
```

- `band_index` maps to the handshake `band_table`; sample rate is
  `level.output_rate`. This is what you display as bars in an octave analyzer.

### 2.5 RAW_DUMP frames (type `0x06`) — on-demand buffer dump

Raw samples are kept only in RAM ring buffers (`[storage] buffer_seconds`
per device; nothing is written to the SD card). The `get_raw` command (§4)
dumps the most recent window of those buffers to **all connected stream
clients** as a sequence of chunked frames:

```
payload = [4-byte dump_id uint32][4-byte device uint32]
          [4-byte chunk_index uint32][4-byte is_last uint32]
          [interleaved float64 samples...]
```

- `dump_id` matches the `get_raw` response, so concurrent dumps and live
  frames can be demuxed. Chunks of one device arrive in `chunk_index` order;
  `is_last = 1` marks that device's final chunk.
- Sample layout inside a chunk is identical to DATA (§2.1): channel-fastest
  across that device's channels. Chunk boundaries are **not** aligned to
  frame boundaries — concatenate all chunks of a device first, then reshape.
- The `get_raw` **response** (control port) carries the decode metadata per
  device: `num_channels`, `sample_rate`, `samples_per_channel`,
  `total_chunks`, plus `chunk_samples` (interleaved samples per full chunk).
- **Delivery is reliable, not best-effort**: dump chunks are never dropped
  by the backpressure mechanism (unlike live frames). A client that stalls
  longer than ~30 s per chunk forfeits the remainder of that chunk's slot.
- Live LEVEL/DATA frames continue during a dump and interleave with it;
  demux by frame type.

Typical SLM workflow: an event happens → the laptop sends
`{"cmd": "get_raw", "seconds": 30}` → receives the last 30 s of raw
waveform for post-analysis, while the live level stream continues unbroken.

---

## 3. Handshake, responses & events

The **handshake** is the first message on **either** port after connecting —
a JSON line on the control port, a MSG frame (type `0x02`) on the stream
port. Responses appear only on the control port; events appear on both.

### Handshake

The full configuration plus protocol metadata:

```json
{
  "type": "handshake",
  "protocol": "pislm/3",
  "running": false,
  "channels": [0, 1, 2, 3, 4, 5],
  "num_channels": 6,
  "channel_map": [
    {"global": 0, "device": 0, "device_type": "mcc172",  "local": 0},
    {"global": 1, "device": 0, "device_type": "mcc172",  "local": 1},
    {"global": 2, "device": 1, "device_type": "dt9837a", "local": 0},
    {"global": 3, "device": 1, "device_type": "dt9837a", "local": 1},
    {"global": 4, "device": 1, "device_type": "dt9837a", "local": 2},
    {"global": 5, "device": 1, "device_type": "dt9837a", "local": 3}
  ],
  "devices": [
    {"index": 0, "type": "mcc172",  "channels": [0, 1],
     "actual_rate": 51200.0},
    {"index": 1, "type": "dt9837a", "channels": [2, 3, 4, 5],
     "actual_rate": 51200.0}
  ],
  "sample_rate": 51200.0,
  "iepe": {"0": 1, "1": 1, "2": 1, "3": 1, "4": 1, "5": 1},
  "sensitivity_mv_per_unit": {"0": 50.0, "1": 1000.0, "2": 50.0,
                              "3": 50.0, "4": 50.0, "5": 50.0},
  "units": {"0": "Pa", "1": "V", "2": "Pa", "3": "Pa", "4": "Pa", "5": "Pa"},
  "stream_raw": false,
  "bands": {"enabled": true, "output": "level", "fraction": 3, "order": 6,
            "f_min": 20.0, "f_max": 20000.0},
  "weighting": {"frequency": "A", "time": "Fast"},
  "level": {"enabled": true, "output_rate": 10.0},
  "storage": {"buffer_seconds": 60.0},
  "trigger": {"enabled": false, "source": "gpio", "gpio_pin": 17,
              "pulse_ms": 10.0, "mode": "RISING_EDGE"},
  "dsp": {"workers_configured": -1, "workers": 3, "dropped_blocks": 0,
          "channels": [[0, 1], [2, 3], [4, 5]]},
  "resample": {"enabled": true, "active": true, "output_rate": 48000.0,
               "taps": 32, "phases": 4096},
  "clock": {"0": {"nominal_rate": 51200.0, "measured_rate": 51202.51,
                  "ppm": 49.02, "points": 5000, "elapsed": 250.0,
                  "settled": true},
            "1": {"nominal_rate": 51200.0, "measured_rate": 51197.44,
                  "ppm": -50.0, "points": 5000, "elapsed": 250.0,
                  "settled": true}},
  "clock_sync_note": "shared-trigger start aligns scan start; ADC clocks still drift (~ppm) between devices",
  "network": {"stream_clients": 1, "stream_frames_dropped": 0},
  "dtype": "float64",
  "byte_order": "little",
  "interleave": "channel-fastest-per-device"
}
```

When band output is **active** (i.e. during a running scan with bands
enabled, or in a `started` event), the handshake also carries a `band_table`:
a **list with one entry per device**, mapping each `band_index` used by
BAND / BAND_LEVEL frames to its parameters at that device's rate:

```json
"band_table": [
  {"device": 0, "fraction": 3, "order": 6, "input_rate": 51200.0,
   "channels": [0, 1],
   "bands": [
     {"index": 0, "center": 19.7, "f_lo": 17.5, "f_hi": 22.1,
      "decimation": 1158, "decimated_rate": 44.2}
     /* ... */
   ]},
  {"device": 1, "fraction": 3, "order": 6, "input_rate": 51200.0,
   "channels": [2, 3, 4, 5],
   "bands": [ /* ... */ ]}
]
```

To interpret a BAND/BAND_LEVEL frame: map its global `channel` to a device
via `channel_map`, then look `band_index` up in that device's table entry.

`network.stream_frames_dropped` counts stream frames evicted because a
stream client's send queue was full (a slow network link or a stalled
client) — the oldest queued frame is dropped so acquisition never stalls
(see §6). A rising count during a bandwidth test means the link cannot
keep up with the current streaming mode; see §8 for how to measure this.

### Command response (control port only)

```json
{"type": "response", "id": 4, "ok": true,
 "cmd": "set_sensitivity",
 "result": {"channel": 0, "sensitivity": 50.0, "units": "Pa"}}
```

On failure:

```json
{"type": "response", "id": 4, "ok": false,
 "error": "a scan is active; send \"stop\" first"}
```

- `ok` (bool): success flag. Always check it.
- `result` (object): present only when `ok` is `true`; per-command (see §4).
- `error` (string): present only when `ok` is `false`.

### Events (unsolicited, broadcast to all clients on both ports)

```json
{"type": "event", "event": "started", ...full handshake body incl. band_table...}
{"type": "event", "event": "triggered", "device": 0}
{"type": "event", "event": "overrun", "kind": "hardware"}
{"type": "event", "event": "stopped"}
```

| event | meaning |
|-------|---------|
| `started` | A scan has begun (or, with the sync trigger enabled, has been armed). Carries the full config (like the handshake), including `band_table` if band output is on, and `trigger` status when a synchronized start was used. |
| `triggered` (`device`: index) | An armed device received its trigger edge and its first samples arrived. Mainly useful with `source: "external"`. |
| `overrun` (`device`: index, `kind`: `hardware` \| `buffer`) | Data was lost on that device; the whole scan has stopped. Reconfigure/reduce rate and `start` again. |
| `stopped` | The scan has ended (after `stop`, or after an overrun). No more DATA until the next `start`. |

**Distinguishing message kinds:** switch on the `type` field — `handshake`,
`response`, or `event`. On the control port these arrive as JSON lines; on
the stream port as MSG frames (type `0x02`).

---

## 4. Command reference

**Config commands require the scan to be stopped** — the MCC 172 rejects
configuration changes during an active scan, so the server returns an error
(`"a scan is active; send \"stop\" first"`). Workflow: `stop` → change →
`start`. Queries, `start`, `stop`, `info`, and `blink_led` work anytime.

### Streaming

| cmd | fields | result | needs stop |
|-----|--------|--------|:----------:|
| `start` | — | full config snapshot (as in the handshake body) | — |
| `stop`  | — | `{"running": false}` | — |

### Queries (anytime)

All `channel` fields take **global** channel numbers.

| cmd | fields | result |
|-----|--------|--------|
| `ping` | — | `{"pong": true}` |
| `get_config` | — | full config snapshot (handshake body without the metadata fields) |
| `status` | — | `{"running", "devices": [{"index", "type", "running", "actual_rate"}]}` |
| `info` | — | `{"devices": [per-device info + "index" + global "channels"], "channel_map", "num_channels"}` |
| `get_sensitivity` | `channel` | `{"channel", "sensitivity"}` (mV/unit) |
| `get_iepe` | `channel` | `{"channel", "mode"}` (mode 0/1) |
| `get_clock` | — | `{"requested_rate", "devices": [{"index", "type", "actual_rate"}], "synchronized": false}` |
| `calibration_read` | `channel` (**mcc172 channels only**) | `{"channel", "slope", "offset"}` |

### Configuration (require stop)

| cmd | fields | result |
|-----|--------|--------|
| `set_sensitivity` | `channel` (global), `value` (mV per unit; e.g. 50 for a 50 mV/Pa mic; 1000 = no scaling/volts) | `{"channel", "sensitivity", "units"}` |
| `set_iepe` | `channel` (global), `mode` (`1`/`0`, or `"on"`/`"off"`) | `{"channel", "mode"}` |
| `set_sample_rate` | `sample_rate` (Hz/ch, applied to every device) | `{"requested_rate", "devices": [... per-device actual_rate]}` |
| `set_channels` | `device` (index), `channels` (that device's **local** channels; dt9837a must be contiguous from 0) | `{"device", "channels", "channel_map"}` — global numbering is rebuilt |
| `set_trigger` | `enable` (bool); optional `source` (`"gpio"`\|`"external"`), `gpio_pin` (BCM), `pulse_ms` | `{"enabled", "source", "gpio_pin", "pulse_ms", "mode": "RISING_EDGE", "note"}` |
| `set_options` | optional `stream_raw` (bool) | `{"stream_raw"}` |
| `set_bands` | optional `enabled` (bool), `output` (`level`\|`waveform`), `f_min`, `f_max`, `fraction`, `order`, `margin` | `{"enabled", "output", ..., "band_table": [per-device]}` |
| `set_weighting` | optional `frequency` (`A`\|`C`\|`Z`), `time` (`Fast`\|`Slow`\|`Impulse`) | `{"frequency", "time"}` |
| `set_level` | optional `enabled` (bool), `output_rate` (Hz) | `{"enabled", "output_rate"}` |
| `set_storage` | `buffer_seconds` (raw ring-buffer length) | `{"buffer_seconds"}` |
| `set_dsp` | `workers` (`-1` auto, `0` inline, `N` cap) | `{"workers_configured", "cpu_count", "note"}` |
| `set_resample` | optional `enabled` (bool), `output_rate` (Hz), `taps`, `phases` | `{"enabled", "output_rate", "taps", "phases", "note"}` |
| `save_config` | optional `path`, `include_settings` (bool) | `{"path", "saved": [...], "timestamp", "sensitivity_mv_per_unit"}` |
| `calibration_write` | `channel`, `slope`, `offset` (**mcc172 channels only**) | `{"channel", "slope", "offset"}` |
| `test_signals_write` | `mode` (int); optional `clock`, `sync` (**mcc172 only**) | `{"mode", "clock", "sync"}` |

`set_bands` validates the settings immediately (building a filter bank per
device) and returns the resulting per-device `band_table`; it errors if
`numpy`/`scipy` are not installed on the Pi. `set_bands output=level` sends
per-band levels (`BAND_LEVEL`), `waveform` sends decimated band signals
(`BAND`). `set_options stream_raw=false` stops raw `DATA` (levels still
stream). After `set_channels` the global numbering changes — re-read the
returned `channel_map`.

#### Synchronized start (`set_trigger` + `start`)

When the trigger is **enabled**, every device is armed to begin its scan on
a shared **rising edge** (rising only — the DT9837A's external digital
trigger supports no other edge), so all devices start on the same pulse:

- `source: "gpio"` (default): on `start`, the Pi arms both scans, waits
  ~0.25 s, then itself pulses BCM `gpio_pin` (default 17, high for
  `pulse_ms`). The pin must be wired to the MCC 172 `TRIG` terminal **and**
  the DT9837A `Ext Trigger` input, with a common ground. The `start`
  response then contains
  `"trigger_start": {"armed": true, "fired": true|false, "devices": {"0": true, ...}}`.
- `source: "external"`: the scans arm and wait; you supply the edge. The
  `start` response reports `{"armed": true, "fired": false}`, and a
  `triggered` event (see §3) is broadcast per device when its first samples
  arrive.

`gpio_pin` is validated against the pins the MCC 172 HAT itself uses
(0, 1, 5, 6, 8–13, 16, 19, 20, 26 are rejected; 17/27/22 are safe).
Alignment note: a shared trigger aligns the **start** of the scans to about
±1 sample per device plus each ADC's fixed group delay; it does **not** lock
the ADC clocks, which still drift a few ppm relative to each other.

### On-demand metrics (anytime — the sound-level-meter statistics)

| cmd | fields | result |
|-----|--------|--------|
| `get_metrics` | optional `seconds`, `weighting`, `time_weighting`, `percentiles` (list), `channels` (list), `include_bands` (bool) | per-channel `{Leq, Lmax, Lmin, Lpeak, LN:{L10,...}, units, calibrated, window_seconds, n_samples[, bands]}` |

`get_metrics` computes over the most-recent buffered raw samples (kept
`buffer_seconds` long, per device), so it works while running **and** after
`stop`. `channels` selects global channels (default: all). The
`weighting`/`time_weighting` fields override the current settings for that
one calculation. `include_bands` adds a per-band `Leq` list. Each channel's
result carries its `device` index. Example result:

```json
{"requested_seconds": 10,
 "channels": {"0": {"Leq": 74.8, "Lmax": 88.1, "Lmin": 61.2, "Lpeak": 96.0,
                    "LN": {"L10": 78.9, "L50": 72.5, "L90": 64.1},
                    "weighting": "A", "time_weighting": "Fast",
                    "units": "Pa", "calibrated": true, "device": 0,
                    "window_seconds": 10.0, "n_samples": 512000},
              "4": {"Leq": 71.2, "device": 1, "...": "..."}}}
```

### Cross-device clock alignment

The two devices have independent ADC crystals (each ±50 ppm), so their
streams slip by up to ~100 µs per second — 36° of phase at 1 kHz after one
second. The GPIO trigger aligns the scans' **start**; this keeps them
aligned afterwards.

**Rate tracking is always on.** Each device's true sample rate is measured
by regressing delivered frame counts against the Pi's monotonic clock, and
reported per device in `clock` (handshake / `get_config` / `status`):

| field | meaning |
|-------|---------|
| `nominal_rate` | the rate the device reports |
| `measured_rate` | the rate it is actually running at |
| `ppm` | offset between the two |
| `elapsed`, `points` | how much history the estimate is based on |
| `settled` | the estimate is tight enough to drive a resampler |

The Pi's own clock error **cancels** in the ratio between two devices
measured against it, so this is accurate even though the reference is an
ordinary system clock (verified: a 200 ppm reference bias leaves 0.05 ppm of
ratio error). Accuracy improves with run time — roughly 6 ppm at 30 s,
2 ppm at 60 s, 0.15 ppm at 300 s. Even with resampling off, these numbers
let a client correct the drift offline on `get_raw` data.

**Resampling** (`set_resample` / `[resample]` in `config.ini`) converts every
device onto one common `output_rate` using the *measured* rates, which is
what actually removes the drift — resampling to the nominal rates would only
put both on the same nominal grid and leave the slip untouched. When it is
active:

- `resample.active` is true, and every device's `effective_rate` (in
  `status`) equals `output_rate`.
- The device's own `actual_rate` is unchanged — the ADCs still run at their
  hardware rates; the conversion is in software.
- **Everything downstream is on the common grid**: ring buffers, `DATA`,
  `LEVEL`, band output, `get_metrics`, and `get_raw` metadata.
- 48 kHz is the usual choice. Note the MCC 172 can only sample at
  `51200/n`, so 48 kHz is not reachable in its hardware at all.

Resampling uses a windowed-sinc polyphase ASRC; at the defaults its error is
−100 dB at 1 kHz and −88 dB at 10 kHz, below the MCC 172's own −93 dB THD.
It costs roughly 20% of one Pi 4 core for 6 channels.

### Calibration (anytime — the scan may stay running)

| cmd | fields | result |
|-----|--------|--------|
| `calibrate` | `channel` (global); optional `level_db` (default 94), `seconds` (3), `freq` (1000), `bandpass` (true), `apply` (true) | `{"channel", "device", "target_level_db", "measured_level_db", "measured_units", "old_sensitivity", "new_sensitivity", "change_db", "seconds", "freq", "bandpass", "applied", "restarted", "note"}` |

Field calibration without arithmetic: fit an acoustic calibrator to the
microphone, leave the scan running, and send `calibrate`. The Pi measures the
buffered signal, derives the sensitivity that makes that tone read
`level_db`, and applies it.

- **`bandpass`** (default on) filters a 1/3-octave band around `freq` before
  the RMS, so background noise does not inflate the result. Set it to
  `false` for a quiet lab or a non-tonal reference.
- **`apply: false`** measures and reports only — nothing is changed. Use it
  to check drift, or to review the number before committing.
- Applying briefly **stops and restarts the scan** (sensitivity is a
  stopped-only device setting); `restarted: true` says it came back up. The
  ring buffers start empty again, so let the scan run a moment before
  calibrating another channel.
- `measured_level_db` is what the **current** calibration reports for the
  tone: true SPL once the channel is calibrated, dBV while it is not (see
  `measured_units`). After a successful calibration it reads `level_db`.
- The change is **runtime only** — follow with `save_config` to keep it.

```json
{"id": 7, "cmd": "calibrate", "channel": 0, "level_db": 94}
```
```json
{"type": "response", "id": 7, "ok": true, "cmd": "calibrate",
 "result": {"channel": 0, "device": 0, "target_level_db": 94.0,
            "measured_level_db": 117.02, "measured_units": "dB re 20uPa",
            "old_sensitivity": 50.0, "new_sensitivity": 705.432,
            "change_db": 23.0, "seconds": 2.9, "freq": 1000.0,
            "bandpass": true, "applied": true, "restarted": true,
            "units": "Pa", "saved": false,
            "note": "applied to the running configuration only; send save_config to keep it across restarts"}}
```

### Persisting settings

`save_config` writes the current **calibration** (every channel's
sensitivity) back to `config.ini`, so it survives a service restart or
reboot. Comments and layout in the file are preserved — only the affected
values are rewritten, with an inline `; saved <date>` marker.

- `include_settings: true` also persists sample rate, weighting, level rate,
  buffer length, band setup, DSP workers, and trigger settings.
- `path` writes somewhere else (e.g. to keep a dated calibration record).

Typical field sequence: `calibrate` each channel, then one `save_config`.

### On-demand raw dump (anytime — pull the buffered waveform)

| cmd | fields | result |
|-----|--------|--------|
| `get_raw` | optional `seconds` (default: full buffer), `devices` (list of device indexes, default: all) | `{"dump_id", "chunk_samples", "units", "devices": [{device, channels, num_channels, sample_rate, samples_per_channel, seconds, total_chunks}]}` |

`get_raw` dumps the most recent `seconds` of the RAM ring buffers to every
connected **stream** client as RAW_DUMP frames (§2.5) — connect to the
stream port before sending it (the command errors if no stream client is
connected). The response returns immediately with the decode metadata; the
chunks follow asynchronously on the stream port, interleaved with live
frames. Like `get_metrics`, it works while running and after `stop`.

### Controls (anytime)

| cmd | fields | result |
|-----|--------|--------|
| `blink_led` | `count` (0 = blink until next call); optional `device` index | `{"count", "devices": [indexes blinked]}` |

### Enumerations

- **Trigger source** (`set_trigger` `source`): `"gpio"` (Pi pulses the pin)
  or `"external"` (user-supplied edge). The trigger mode is always
  `RISING_EDGE` — the DT9837A supports no other edge.

### `sample_rate` note

The requested rate applies to every device; each rounds independently:

- **MCC 172** generates `51200 / N` Hz (`N` = 1..256).
- **DT9837A** supports nearly arbitrary rates up to 52.734 kHz; the exact
  achieved rate is reported when the scan starts.

Read the per-device real rates from the `set_sample_rate` result,
`get_clock`, or the handshake `devices` list. Because the devices round
differently and run separate clocks, do not assume their sample streams
align.

---

## 5. Calibration & sound-pressure level

`set_sensitivity` applies the sensor's calibrated sensitivity in **mV per
mechanical unit** on the device, so DATA samples come back in engineering
units:

- Microphone rated **50 mV/Pa** → `set_sensitivity` with `value = 50` →
  samples are in **pascals (Pa)**; the channel's `units` becomes `"Pa"`.
- Leave `value = 1000` (default) → no scaling → samples are in **volts (V)**.

With samples in pascals, compute sound-pressure level (dB re 20 µPa):

```
Prms = sqrt(mean(sample^2))          # over a window, per channel, in Pa
SPL  = 20 * log10(Prms / 20e-6)      # dB
```

The stream is unweighted (Z-weighting). Apply an A-weighting filter on your
side if you need dB(A).

### 5.1 What the Pi computes vs. what you can compute

The Pi already does the sound-level-meter math:

- **LEVEL / BAND_LEVEL frames** are frequency-weighted (A/C/Z per
  `weighting.frequency`) and time-weighted (Fast/Slow/Impulse per
  `weighting.time`) levels in dB — display them directly (L_AF etc.).
- **`get_metrics`** returns Leq, Lmax, Lmin, Lpeak, and LN percentiles over
  the buffered window, with optional per-band Leq.

If you prefer to compute on the client (from raw DATA or BAND waveforms),
the definitions used are:

```
# Time-weighted level (Fast tau=0.125 s, Slow tau=1 s, Impulse tau=0.035 s):
#   one-pole exponential average of the squared signal, then to dB
alpha  = exp(-1 / (fs * tau))
ms[n]  = alpha * ms[n-1] + (1 - alpha) * x[n]^2
L_tw   = 10 * log10(ms[n] / (20e-6)^2)

# Equivalent continuous level over T seconds (energy average):
Leq_T  = 10 * log10( mean(x^2 over T) / (20e-6)^2 )

# L_N: level exceeded N % of the time = (100-N)th percentile of the
# time-weighted level series. Lpeak = 20*log10(max|x| / 20e-6).
```

---

## 6. Reliability notes

- **Backpressure:** the server keeps a bounded per-client send queue
  (`config.ini`, `[network] max_queue_blocks`). If a client cannot keep up,
  the **oldest live stream frames (DATA and BAND) are dropped** so a slow
  reader never stalls acquisition or affects other clients. There is no
  guarantee every live frame is delivered; treat the live stream as
  best-effort real-time. (A dropped BAND frame leaves a gap in that band's
  decimated series.) RAW_DUMP chunks are the exception — they block instead
  of dropping (§2.5), so a `get_raw` after the fact is the reliable way to
  recover a gap in a live recording. Every eviction increments
  `network.stream_frames_dropped` in `status`/the handshake, so a client can
  tell the difference between "quiet because nothing changed" and "quiet
  because frames are being lost."
- **Ordering:** within a single connection, bytes are ordered (TCP). On the
  control port a command's `response` always follows the commands sent
  before it; an event may be interleaved (e.g. the `stopped` event can
  arrive just before the `stop` response). On the stream port, DATA and MSG
  frames are ordered but the two ports are not synchronized with each other.
- **Reconnect:** on connect (either port) you always get a fresh handshake
  reflecting the current state, so a client can reconnect at any time and
  resynchronize.
- **Data flows only while running.** After connecting to the stream port you
  may still need to send `start` on the control port (unless
  `[control] autostart = true` on the Pi and no one has stopped it).

---

## 7. Minimal example exchange

Control port (5000) — JSON lines both ways:

```
connect 5000
  <- {"type":"handshake", "protocol":"pislm/3", "num_channels":6, ...}
  -> {"id":1,"cmd":"stop"}                       (ignore error if already stopped)
  -> {"id":2,"cmd":"set_sensitivity","channel":3,"value":50}     (global ch 3 = DT9837A ch 1)
  <- {"type":"response","id":2,"ok":true,"result":{"channel":3,"sensitivity":50.0,"units":"Pa"}}
  -> {"id":3,"cmd":"set_sample_rate","sample_rate":25600}
  <- {"type":"response","id":3,"ok":true,"result":{"requested_rate":25600.0,"devices":[...]}}
  -> {"id":4,"cmd":"start"}
  <- {"type":"response","id":4,"ok":true,"result":{...config...}}
  ...
  -> {"id":5,"cmd":"get_metrics","seconds":10,"channels":[0,3]}
  <- {"type":"response","id":5,"ok":true,"result":{"channels":{"0":{...},"3":{...}}}}
  -> {"id":6,"cmd":"stop"}
  <- {"type":"event","event":"stopped"}          (may precede the response)
  <- {"type":"response","id":6,"ok":true,"result":{"running":false}}
```

Stream port (5001) — typed frames, Pi → client (default SLM configuration):

```
connect 5001
  <- MSG    {"type":"handshake", "protocol":"pislm/3", ...}
  <- LEVEL  ch0 <dB samples>                     (repeats for every channel
  <- LEVEL  ch2 <dB samples>                      at the level output rate)
  <- BAND_LEVEL band 12, ch 0 <dB samples>       (if bands enabled)
  <- DATA   device 0 <interleaved float64>       (only if stream_raw is on)
  ...
  <- MSG    {"type":"event","event":"stopped"}   (when the scan stops)
```

---

## 8. Bandwidth & performance testing

Streaming cost depends entirely on which of the three payload types (§2)
are switched on — the default SLM configuration (level-only) is tiny; raw
waveform streaming is not:

| Mode | Payload/sample | 6 ch @ 51.2 kHz | 6 ch @ 48 kHz (resampled) |
|------|-----------------|-----------------|---------------------------|
| Level-only (`stream_raw=false`, bands off) | one `float64` per channel per `[level] output_rate` tick | ≈ 1 KB/s (negligible) | same |
| Raw waveform (`stream_raw=true`) | one `float64` per channel per sample | ≈ 2.34 MiB/s ≈ 19.7 Mbps | ≈ 2.20 MiB/s ≈ 18.4 Mbps |
| Bands, `output=level` | one `float64` per band per channel per band's own decimated rate | a few KB/s to tens of KB/s, depends on `f_max`/`fraction` | — |
| Bands, `output=waveform` | decimated `float64` waveform per band per channel | well under raw, but adds up with `f_max`; measure it | — |

These are payload bytes; add ~5-10% for TCP/IP overhead. Numbers assume
`float64` throughout (`dtype` in the handshake) — there is currently no
option to stream a narrower type.

**Measuring it for real**: theoretical numbers don't capture Wi-Fi
retransmits, a busy switch, or a laptop CPU that can't keep up, so measure
on the actual link. `pislm_test.py` has a `bench <seconds>` shorthand that
counts received bytes/frames per frame type over a window and reports
throughput (KB/s and Mbps) alongside `dsp.dropped_blocks` (device-side DSP
overload) and `network.stream_frames_dropped` (§6, network-side backpressure)
deltas from `status`:

```
> bench 15
[bench] measuring for 15.0s ... control RTT ~2.1 ms
  total: 2312.4 KB/s  (18.94 Mbps), 1583.2 frames/s
  DATA        2312.4 KB/s   180.1 fps
  dsp dropped_blocks: +0   stream frames dropped: +0
```

A non-zero `stream frames dropped` delta while `dropped_blocks` stays at 0
means the **network**, not the Pi's DSP, is the bottleneck for the current
mode — switch to Ethernet, lower `sample_rate`, enable `[resample]` to a
lower common rate, or turn off `stream_raw` and rely on bands/levels plus
on-demand `get_raw` instead.
