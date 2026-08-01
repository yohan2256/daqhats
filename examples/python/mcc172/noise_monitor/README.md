# MCC 172 Noise Monitor

A headless setup that turns a Raspberry Pi + **MCC 172** DAQ HAT into a
network sound/vibration probe you can **remote-control from a laptop**. On
boot the Pi powers an IEPE microphone, applies your calibration, and (by
default) starts streaming the raw waveform over TCP. A separate control port
lets a client drive every configurable feature of the MCC 172 and start/stop
the stream on demand.

```
[IEPE mic] --2 mA IEPE--> [MCC 172 HAT] --SPI--> [Raspberry Pi]
                                                       |  (systemd @ boot)
                                                       |  noise_monitor.py
                                    control port 5000  <->  commands / responses
                                    stream  port 5001   ->  raw waveform frames
                                                    Wi-Fi / USB-ethernet
                                                       v
                                                   [Laptop]
                                            (your own TCP client)
```

The MCC 172 is the only DAQ HAT in this library with built-in **IEPE
constant-current excitation** (2 mA) and per-channel **sensitivity
scaling**, which is what makes it suitable for calibrated sound pressure
measurement. It provides 2 channels of 24-bit simultaneous sampling at up
to **51.2 kHz per channel**.

## Two ports

| Port | Default | Direction | Content |
|------|:-------:|-----------|---------|
| **Control** | 5000 | both ways | Newline-delimited JSON commands, responses, and events. No binary. |
| **Stream**  | 5001 | Pi → client | Typed length-prefixed frames: a JSON handshake, then the raw waveform (`float64`), optional 1/3-octave band output, plus events. |

The client is yours to write — see **[`PROTOCOL.md`](PROTOCOL.md)** for the
complete, language-agnostic wire specification (framing, handshake, the full
command list with request/response schemas, events, and examples).

## Files

| File | Runs on | Purpose |
|------|---------|---------|
| `noise_monitor.py`     | Raspberry Pi | IEPE + calibration + continuous scan; control-port command server and stream-port waveform/band server. |
| `band_filter.py`       | Raspberry Pi | Fractional-octave (1/3-octave) decimating Butterworth filter bank (needs numpy/scipy; only used when band output is on). |
| `config.ini`           | Raspberry Pi | Boot defaults: sample rate, channels, IEPE, sensitivity, ports, autostart, bands. |
| `noise-monitor.service`| Raspberry Pi | systemd unit for automatic start at boot. |
| `PROTOCOL.md`          | —            | Communication protocol specification for your client. |

## 1. Hardware

1. Power off the Pi, seat the MCC 172 on the 40-pin header (single board =
   address 0, all address jumpers removed).
2. Connect the IEPE microphone to a channel input (e.g. CH0).
3. Power on the Pi.

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

- **`sample_rate`** — 51200 gives the full audio bandwidth (~25 kHz). Lower
  it (e.g. 25600, 10240) to reduce data volume if you don't need it.
- **`sensitivity_ch0` / `sensitivity_ch1`** — your microphone's calibrated
  sensitivity in **mV/Pa**.
  - Leave at `1000` (the default = no scaling) and the stream is in **volts**.
  - Set it to your mic's value (e.g. `50` for 50 mV/Pa) and the stream is in
    **pascals**, so SPL in dB (re 20 µPa) can be computed from the samples.
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
`start`, `stop`) and to the stream port to receive the raw waveform.

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

## 1/3-octave band output (optional)

The Pi can additionally stream fractional-octave (1/3-octave by default)
band-filtered audio: each band is a real-time Butterworth band-pass filter,
**decimated per band** to just above twice its upper edge, sent as `BAND`
frames on the stream port. The laptop then does time-weighting
(Fast/Slow/Impulse), Leq, band SPL, A/C-weighting, etc. (see PROTOCOL.md §5.1).

Enable it in `config.ini` (`[bands] enabled = true`) or at runtime with the
`set_bands` command. It needs numpy + scipy on the Pi:

```sh
sudo apt install python3-numpy python3-scipy
```

**Bandwidth — read this before enabling the full range.** High 1/3-octave
bands are wide in absolute Hz, so they barely decimate. For two channels at
51.2 kHz:

| Band range | Approx. band-output rate (2 ch) |
|------------|--------------------------------:|
| 20 Hz – 20 kHz (31 bands) | ~36 Mbit/s (≈5× the raw stream) |
| 20 Hz – 5 kHz (25 bands)  | ~7 Mbit/s |
| 20 Hz – 1 kHz (18 bands)  | ~1 Mbit/s |

The top few bands dominate, so **lowering `f_max` is the most effective way to
fit a Wi-Fi link.** Using one channel, or `stream_raw = false` (send only
band frames, no raw), also helps. CPU cost is modest — the full 31-band ×
2-channel bank is roughly one-third to one-half of one Pi Zero 2 W core.

For a broadband (overall) Leq or time-weighting, don't use bands at all — the
raw DATA stream is far lighter and the laptop can compute it directly.

## Notes & tips

- **Changing settings while streaming.** The MCC 172 rejects configuration
  changes during an active scan, so the server does too — send `stop`, make
  your change, then `start`.
- **Calibration → SPL.** With `set_sensitivity` in mV/Pa the samples are in
  pascals; `SPL = 20*log10(Prms/20e-6)` dB. The stream is unweighted
  (Z-weighting); apply A-weighting on the client for dB(A).
- **Raspberry Pi Zero 2 W.** Streaming raw 2 ch × 51.2 kHz float64 is
  ~820 kB/s. That is fine over Wi-Fi/USB-ethernet, but if you see buffer
  overruns lower the sample rate or use one channel.
- **Backpressure.** A slow stream client never stalls acquisition: the
  server drops its oldest DATA blocks (`[network] max_queue_blocks`).
