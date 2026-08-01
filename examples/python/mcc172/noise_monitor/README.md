# Noise Monitor (MCC 172 + DT9837A)

A headless setup that turns a Raspberry Pi with an **MCC 172** DAQ HAT and
(optionally) a **Data Translation DT9837A** USB module into a network
**sound level meter** you control from a laptop — up to **6 IEPE channels**.
On boot the Pi powers the IEPE microphones, applies your calibration, and
streams the **Fast time-weighted, A-weighted level** (the SLM "needle")
continuously. Raw samples are kept in ring buffers on the Pi, and on request
it computes **Leq, Lmax, Lmin, Lpeak, and LN percentiles** over a window —
like a class sound level meter. A separate control port drives every
configurable feature of both devices.

```
[IEPE mics] --2mA--> [MCC 172 HAT] ---SPI--> [Raspberry Pi]
                       (2 ch, global 0-1)          |  (systemd @ boot)
[IEPE mics] --4mA--> [DT9837A USB] ---USB--> ...   |  noise_monitor.py
                       (4 ch, global 2-5)          |  + raw ring buffers
                                  control port 5000 <-> commands / metrics
                                  stream  port 5001  -> levels (+ raw/bands)
                                                Wi-Fi / USB-ethernet
                                                       v
                                                   [Laptop]
                                            (your own TCP client)
```

Both devices provide 24-bit simultaneous IEPE sampling: the MCC 172 at up
to 51.2 kHz/ch (2 ch), the DT9837A at up to 52.7 kHz/ch (4 ch). Channels
are numbered **globally** in device order (0–1 = MCC 172, 2–5 = DT9837A);
all commands and stream frames use global numbers.

> **Clock sync caveat:** the two devices run separate, unsynchronized ADC
> clocks. Per-channel levels/metrics are unaffected, but cross-channel
> phase analysis is only valid within one device.

## Two ports

| Port | Default | Direction | Content |
|------|:-------:|-----------|---------|
| **Control** | 5000 | both ways | Newline-delimited JSON commands, responses (incl. on-demand metrics), and events. No binary. |
| **Stream**  | 5001 | Pi → client | Typed length-prefixed frames: JSON handshake, time-weighted **LEVEL** frames (default), optional per-band levels, optional raw waveform / band waveforms, plus events. |

The client is yours to write — see **[`PROTOCOL.md`](PROTOCOL.md)** for the
complete, language-agnostic wire specification (framing, handshake, the full
command list with request/response schemas, events, and examples).

## Files

| File | Runs on | Purpose |
|------|---------|---------|
| `noise_monitor.py`     | Raspberry Pi | Multi-device acquisition; level streaming, raw ring buffers + on-demand metrics; control/stream servers. |
| `devices.py`           | Raspberry Pi | Device backends: MCC 172 (daqhats) and DT9837A (uldaq), plus the global channel map. |
| `slm.py`               | Raspberry Pi | Sound-level-meter DSP: IEC 61672 A/C/Z weighting, Fast/Slow/Impulse time weighting, Leq/Lmax/Lmin/Lpeak/LN. |
| `band_filter.py`       | Raspberry Pi | Fractional-octave (1/3-octave) decimating Butterworth filter bank. |
| `config.ini`           | Raspberry Pi | Boot defaults: devices, channels, IEPE, sensitivity, sample rate, weighting, level rate, buffer, bands, ports. |
| `noise-monitor.service`| Raspberry Pi | systemd unit for automatic start at boot. |
| `PROTOCOL.md`          | —            | Communication protocol specification for your client. |

Python dependencies on the Pi (for the level/metrics DSP):

```sh
sudo apt install python3-numpy python3-scipy
```

For the DT9837A, additionally build/install the **uldaq** C library and its
Python binding (https://github.com/mccdaq/uldaq):

```sh
sudo apt install gcc g++ make libusb-1.0-0-dev
# build & install the C library from the uldaq release tarball, then:
pip3 install uldaq
```

A device listed in `config.ini` but not attached is skipped at startup with
a log message — the monitor runs with whatever is present.

## 1. Hardware

1. Power off the Pi, seat the MCC 172 on the 40-pin header (single board =
   address 0, all address jumpers removed).
2. Connect the DT9837A to a USB port (a powered hub is recommended on a
   Pi Zero 2 W — the DT9837A is USB-powered and drives 4 IEPE supplies).
3. Connect the IEPE microphones to the BNC inputs.
4. Power on the Pi.

## 2. Install the daqhats library on the Pi

Follow the top-level repository instructions:

```sh
cd ~
git clone https://github.com/mccdaq/daqhats.git
cd daqhats
sudo ./install.sh
```

Verify the board is detected:

```sh
daqhats_list_boards
```

## 3. Configure boot defaults

Edit `config.ini`. These are the initial settings the Pi boots with; a
client can change most of them live afterwards over the control port.

- **`[devices] enabled`** — which devices to open (`mcc172, dt9837a`), in
  order. Global channel numbers follow this order.
- **`[mcc172]` / `[dt9837a]`** — per-device local channels, IEPE on/off, and
  per-channel **sensitivity in mV/Pa**.
  - Leave at `1000` (the default = no scaling) and the data is in **volts**.
  - Set it to your mic's value (e.g. `50` for 50 mV/Pa) and the data is in
    **pascals**, so levels and metrics are true SPL (re 20 µPa).
- **`sample_rate`** (`[acquisition]`) — requested rate for every device;
  51200 gives the full audio bandwidth (~25 kHz).
- **`control_port` / `stream_port`** — the two TCP ports.
- **`autostart`** (`[control]`) — `true` starts streaming at boot; `false`
  boots idle and waits for a `start` command.

## 4. Run by hand (recommended before enabling the service)

On the Pi:

```sh
cd ~/daqhats/examples/python/mcc172/noise_monitor
python3 noise_monitor.py
```

Quick smoke test from any machine (no custom client needed):

```sh
# Control port: read the handshake, then request status.
printf '{"id":1,"cmd":"status"}\n' | nc 192.168.1.50 5000
```

Then implement your client against [`PROTOCOL.md`](PROTOCOL.md): connect to
the control port for commands (e.g. `set_sensitivity`, `set_sample_rate`,
`start`, `stop`, `get_metrics`) and to the stream port for levels and
waveforms. All channels are global (0–5 with both devices attached).

## 5. Start automatically at boot (systemd)

Edit `noise-monitor.service` if your username/paths differ from `pi`, then:

```sh
sudo cp noise-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now noise-monitor
```

Check status and logs:

```sh
systemctl status noise-monitor
journalctl -u noise-monitor -f
```

After editing `config.ini`, apply changes with:

```sh
sudo systemctl restart noise-monitor
```

## Sound-level-meter behavior

What streams continuously, and what is computed on demand:

- **LEVEL frames (default on).** The Pi applies the configured frequency
  weighting (A/C/Z) and time weighting (Fast/Slow/Impulse) and streams the
  broadband level in dB at `[level] output_rate` (default 10/s) — the live
  needle. A few hundred bytes per second.
- **Raw ring buffer + `get_metrics` + `get_raw`.** Raw samples are kept in
  RAM only (`[storage] buffer_seconds`, default 300 s, packed 8 B/sample —
  **nothing is ever written to the SD card**). The `get_metrics` command
  computes **Leq, Lmax, Lmin, Lpeak, LN (L10/L50/L90...)** over a requested
  window — while running or after a stop — optionally with per-band Leq
  (`include_bands`). The `get_raw` command dumps the buffered raw waveform
  itself to the laptop (chunked `RAW_DUMP` frames, reliable delivery), so
  after an event you can pull the last N seconds for post-analysis.
- **1/3-octave spectrum (optional).** `[bands] enabled = true` adds per-band
  output. `output = level` (default) streams Fast time-weighted band levels
  in dB (`BAND_LEVEL` frames) — the octave-analyzer bar display, tiny
  bandwidth. `output = waveform` streams each band's decimated time signal
  (`BAND` frames) — heavy; the full 20 Hz–20 kHz set for 2 ch is ~36 Mbit/s,
  so lower `f_max` to fit Wi-Fi if you need waveforms.
- **Raw waveform streaming (optional).** `stream_raw = true` additionally
  streams the full-rate raw waveform (~6.6 Mbit/s for 2 ch) if you want to
  record everything on the laptop.

Frequency weighting, time weighting, level rate, buffer length, and band
setup are all changeable at runtime (`set_weighting`, `set_level`,
`set_storage`, `set_bands`) — stop the scan, set, start.

## Notes & tips

- **Changing settings while streaming.** The MCC 172 rejects configuration
  changes during an active scan, so the server does too — send `stop`, make
  your change, then `start`.
- **Calibration → SPL.** With `set_sensitivity` in mV/Pa the samples are in
  pascals; `SPL = 20*log10(Prms/20e-6)` dB. The stream is unweighted
  (Z-weighting); apply A-weighting on the client for dB(A).
- **Storage-free operation.** With a wired-LAN Pi 4 the recommended workflow
  needs no SSD/SD data storage at all: record live on the laptop
  (`stream_raw` at ~0.82 MB/s per 2 ch is trivial for gigabit Ethernet), and
  use `get_raw` to backfill any gap or grab the pre-event window from the
  RAM buffer. RAM budget for the buffer: 2 ch ≈ 0.82 MB/s (300 s ≈ 246 MB),
  6 ch ≈ 2.46 MB/s (300 s ≈ 740 MB — fine on a 4 GB Pi 4).
- **Raspberry Pi Zero 2 W.** Streaming raw 2 ch × 51.2 kHz float64 is
  ~820 kB/s. That is fine over Wi-Fi/USB-ethernet, but if you see buffer
  overruns lower the sample rate or use one channel — and shorten
  `buffer_seconds` to fit its 512 MB RAM.
- **Backpressure.** A slow stream client never stalls acquisition: the
  server drops its oldest live frames (`[network] max_queue_blocks`).
  `get_raw` dump chunks are the exception — they are delivered reliably.
