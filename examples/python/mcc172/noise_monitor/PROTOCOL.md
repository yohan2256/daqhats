# MCC 172 Noise Monitor — Communication Protocol

Wire specification for talking to `noise_monitor.py` running on the
Raspberry Pi. It is language-agnostic: any TCP client that follows this
document can receive the raw waveform and control the MCC 172.

- **Protocol version:** `mcc172-noise-monitor/2` (see the handshake).
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
| `0x01` | DATA | Raw waveform: interleaved `float64` (8 bytes each), little-endian. |
| `0x02` | MSG  | A UTF-8 JSON object (handshake on connect, then events). |
| `0x03` | BAND | Fractional-octave band **waveform** (decimated), see §2.2. |
| `0x04` | LEVEL | Broadband time-weighted **level** (dB), see §2.3. |
| `0x05` | BAND_LEVEL | Per-band time-weighted **level** (dB), see §2.4. |

**Reading loop (pseudocode):**

```
read 5 bytes -> (type, length)
read `length` bytes -> payload
if type == 0x01: float64[]                = payload            # raw waveform
if type == 0x02: json                     = payload            # msg / event
if type == 0x03: band_index,channel,float64[]  = payload       # band waveform
if type == 0x04: channel,float64[]        = payload            # level dB
if type == 0x05: band_index,channel,float64[]  = payload       # band level dB
```

**Which frames you get depends on configuration.** By default the monitor
behaves like a sound level meter: it sends `LEVEL` frames (and `BAND_LEVEL`
if bands are enabled) and keeps raw samples buffered for `get_metrics`
(§4). Raw `DATA` is sent only when `stream_raw` is on; `BAND` (waveform)
frames only when band output mode is `waveform`.

### DATA payload layout

The payload is a flat array of `float64` samples, **channel-fastest**
(the same order the MCC 172 returns):

```
ch0[n], ch1[n], ch0[n+1], ch1[n+1], ...        (when scanning channels 0 and 1)
```

- Number of active channels = `num_channels` from the handshake / `get_config`.
- Samples per channel in a frame = `length / 8 / num_channels`.
- Sample **value units** depend on calibration (see §5): volts (`V`) when the
  channel's sensitivity is `1000`, pascals (`Pa`) otherwise.

DATA frames are only sent while a scan is running (after `start`). The stream
port ignores anything the client sends to it.

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

- `band_index` maps to the `band_table` in the handshake (§3), which gives
  each band's center frequency, edges, decimation factor, and **decimated
  sample rate**. Each band streams at its own rate, so demux by `band_index`.
- `channel` is the MCC 172 channel number (0 or 1).
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
  "protocol": "mcc172-noise-monitor/2",
  "running": false,
  "channels": [0, 1],
  "num_channels": 2,
  "sample_rate": 51200.0,
  "actual_rate": 51200.0,
  "clock_source": "LOCAL",
  "iepe": {"0": 1, "1": 1},
  "sensitivity_mv_per_unit": {"0": 50.0, "1": 1000.0},
  "units": {"0": "Pa", "1": "V"},
  "trigger": {"enabled": false, "source": "LOCAL", "mode": "RISING_EDGE"},
  "options": {"continuous": true, "ext_clock": false},
  "stream_raw": false,
  "bands": {"enabled": true, "output": "level", "fraction": 3, "order": 6,
            "f_min": 20.0, "f_max": 20000.0},
  "weighting": {"frequency": "A", "time": "Fast"},
  "level": {"enabled": true, "output_rate": 10.0},
  "storage": {"buffer_seconds": 60.0},
  "dtype": "float64",
  "byte_order": "little",
  "interleave": "channel-fastest"
}
```

When band output is **active** (i.e. during a running scan with bands
enabled, or in a `started` event), the handshake also carries a `band_table`
mapping each `band_index` used by BAND frames (§2.2) to its parameters:

```json
"band_table": {
  "fraction": 3, "order": 6, "input_rate": 51200.0, "channels": [0, 1],
  "bands": [
    {"index": 0, "center": 19.7, "f_lo": 17.5, "f_hi": 22.1,
     "decimation": 1158, "decimated_rate": 44.2},
    {"index": 1, "center": 24.8, "f_lo": 22.1, "f_hi": 27.8,
     "decimation": 919,  "decimated_rate": 55.7}
    /* ... */
  ]
}
```

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
{"type": "event", "event": "overrun", "kind": "hardware"}
{"type": "event", "event": "stopped"}
```

| event | meaning |
|-------|---------|
| `started` | A scan has begun. Carries the full config (like the handshake), including `band_table` if band output is on — so clients connected before `start` learn the band layout. |
| `overrun` (`kind`: `hardware` \| `buffer`) | Data was lost; the scan has stopped. Reconfigure/reduce rate and `start` again. |
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

| cmd | fields | result |
|-----|--------|--------|
| `ping` | — | `{"pong": true}` |
| `get_config` | — | full config snapshot (handshake body without the metadata fields) |
| `status` | — | `{"running": bool}`; while running also `hardware_overrun`, `buffer_overrun`, `triggered` (bool), `samples_available` (int), `buffer_size` (int) |
| `info` | — | `{"address", "product_name", "firmware_version", "serial", "calibration_date", "num_ai_channels", "ai_min_voltage", "ai_max_voltage"}` |
| `get_sensitivity` | `channel` | `{"channel", "sensitivity"}` (mV/unit) |
| `get_iepe` | `channel` | `{"channel", "mode"}` (mode 0/1) |
| `get_clock` | — | `{"clock_source", "sample_rate", "synced"}` |
| `calibration_read` | `channel` | `{"channel", "slope", "offset"}` |

### Configuration (require stop)

| cmd | fields | result |
|-----|--------|--------|
| `set_sensitivity` | `channel` (0/1), `value` (mV per unit; e.g. 50 for a 50 mV/Pa mic; 1000 = no scaling/volts) | `{"channel", "sensitivity", "units"}` |
| `set_iepe` | `channel`, `mode` (`1`/`0`, or `"on"`/`"off"`) | `{"channel", "mode"}` |
| `set_sample_rate` | `sample_rate` (Hz/ch); optional `clock_source` | `{"requested_rate", "actual_rate", "clock_source"}` |
| `set_channels` | `channels` (list, subset of `[0,1]`) | `{"channels"}` |
| `set_trigger` | `enable` (bool); optional `mode`, `source` | `{"enabled", "source", "mode"}` |
| `set_options` | optional `continuous` (bool), `ext_clock` (bool), `stream_raw` (bool) | `{"continuous", "ext_clock", "stream_raw"}` |
| `set_bands` | optional `enabled` (bool), `output` (`level`\|`waveform`), `f_min`, `f_max`, `fraction`, `order`, `margin` | `{"enabled", "output", "fraction", "order", "f_min", "f_max", "band_table"}` |
| `set_weighting` | optional `frequency` (`A`\|`C`\|`Z`), `time` (`Fast`\|`Slow`\|`Impulse`) | `{"frequency", "time"}` |
| `set_level` | optional `enabled` (bool), `output_rate` (Hz) | `{"enabled", "output_rate"}` |
| `set_storage` | `buffer_seconds` (raw ring-buffer length) | `{"buffer_seconds"}` |
| `calibration_write` | `channel`, `slope`, `offset` (overrides factory ADC cal) | `{"channel", "slope", "offset"}` |
| `test_signals_write` | `mode` (int); optional `clock`, `sync` (int) | `{"mode", "clock", "sync"}` |

`set_bands` validates the settings immediately (building the filter bank) and
returns the resulting `band_table`; it errors if `numpy`/`scipy` are not
installed on the Pi. `set_bands output=level` sends per-band levels
(`BAND_LEVEL`), `waveform` sends decimated band signals (`BAND`).
`set_options stream_raw=false` stops raw `DATA` (levels still stream).

### On-demand metrics (anytime — the sound-level-meter statistics)

| cmd | fields | result |
|-----|--------|--------|
| `get_metrics` | optional `seconds`, `weighting`, `time_weighting`, `percentiles` (list), `channels` (list), `include_bands` (bool) | per-channel `{Leq, Lmax, Lmin, Lpeak, LN:{L10,...}, units, calibrated, window_seconds, n_samples[, bands]}` |

`get_metrics` computes over the most-recent buffered raw samples (kept
`buffer_seconds` long), so it works while running **and** after `stop`. The
`weighting`/`time_weighting` fields override the current settings for that
one calculation. `include_bands` adds a per-band `Leq` list. Example result:

```json
{"sample_rate": 51200.0, "requested_seconds": 10,
 "channels": {"0": {"Leq": 74.8, "Lmax": 88.1, "Lmin": 61.2, "Lpeak": 96.0,
                    "LN": {"L10": 78.9, "L50": 72.5, "L90": 64.1},
                    "weighting": "A", "time_weighting": "Fast",
                    "units": "Pa", "calibrated": true,
                    "window_seconds": 10.0, "n_samples": 512000}}}
```

### Controls (anytime)

| cmd | fields | result |
|-----|--------|--------|
| `blink_led` | `count` (0 = blink until next call) | `{"count"}` |

### Enumerations

- **Clock / trigger source** (`clock_source`, `source`): `"LOCAL"`,
  `"MASTER"`, `"SLAVE"`. Integers `0/1/2` are also accepted.
- **Trigger mode** (`mode`): `"RISING_EDGE"`, `"FALLING_EDGE"`,
  `"ACTIVE_HIGH"`, `"ACTIVE_LOW"`. Integers `0..3` are also accepted.

### `sample_rate` note

The MCC 172 generates `51200 / N` Hz (`N` = 1..256). A requested rate is
rounded to the nearest achievable value; read the real rate from
`actual_rate` (in the `set_sample_rate` result, or `get_config`).

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
  the **oldest stream frames (DATA and BAND) are dropped** so a slow reader
  never stalls acquisition or affects other clients. There is no guarantee
  every frame is delivered; treat the stream as best-effort real-time. (A
  dropped BAND frame leaves a gap in that band's decimated series.)
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
  <- {"type":"handshake", "protocol":"mcc172-noise-monitor/2", "running":false, ...}
  -> {"id":1,"cmd":"stop"}                       (ignore error if already stopped)
  -> {"id":2,"cmd":"set_sensitivity","channel":0,"value":50}
  <- {"type":"response","id":2,"ok":true,"result":{"channel":0,"sensitivity":50.0,"units":"Pa"}}
  -> {"id":3,"cmd":"set_sample_rate","sample_rate":25600}
  <- {"type":"response","id":3,"ok":true,"result":{"requested_rate":25600.0,"actual_rate":25600.0,"clock_source":"LOCAL"}}
  -> {"id":4,"cmd":"start"}
  <- {"type":"response","id":4,"ok":true,"result":{...config...}}
  ...
  -> {"id":5,"cmd":"stop"}
  <- {"type":"event","event":"stopped"}          (may precede the response)
  <- {"type":"response","id":5,"ok":true,"result":{"running":false}}
```

Stream port (5001) — typed frames, Pi → client:

```
connect 5001
  <- MSG  {"type":"handshake", "protocol":"mcc172-noise-monitor/2", ...}
  <- DATA <interleaved float64 samples>          (repeats continuously while running)
  <- DATA <interleaved float64 samples>
  ...
  <- MSG  {"type":"event","event":"stopped"}     (when the scan stops)
```
