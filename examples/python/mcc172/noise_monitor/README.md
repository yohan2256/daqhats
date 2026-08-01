# MCC 172 Noise Monitor

A headless setup that turns a Raspberry Pi + **MCC 172** DAQ HAT into a
network sound/vibration probe you can **remote-control from a laptop**. On
boot the Pi powers an IEPE microphone, applies your calibration, and (by
default) starts streaming the raw waveform over TCP. Over the same
connection the laptop can drive every configurable feature of the MCC 172
and start/stop the stream on demand.

```
[IEPE mic] --2 mA IEPE--> [MCC 172 HAT] --SPI--> [Raspberry Pi]
                                                       |  (systemd @ boot)
                                                       |  noise_monitor.py
                                        raw waveform  <->  control commands
                                                    Wi-Fi / USB-ethernet
                                                       v
                                                   [Laptop]
                                                laptop_client.py
```

The MCC 172 is the only DAQ HAT in this library with built-in **IEPE
constant-current excitation** (2 mA) and per-channel **sensitivity
scaling**, which is what makes it suitable for calibrated sound pressure
measurement. It provides 2 channels of 24-bit simultaneous sampling at up
to **51.2 kHz per channel**.

## Files

| File | Runs on | Purpose |
|------|---------|---------|
| `noise_monitor.py`     | Raspberry Pi | IEPE + calibration + continuous scan; TCP server streaming raw waveform **and accepting control commands**. |
| `config.ini`           | Raspberry Pi | Boot defaults: sample rate, channels, IEPE, sensitivity, network, autostart. |
| `noise-monitor.service`| Raspberry Pi | systemd unit for automatic start at boot. |
| `laptop_client.py`     | Laptop       | Interactive control shell + live RMS/SPL meter + raw recording. |

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

Edit `config.ini`. These are the initial settings the Pi boots with; the
laptop can change most of them live afterwards.

- **`sample_rate`** — 51200 gives the full audio bandwidth (~25 kHz). Lower
  it (e.g. 25600, 10240) to reduce data volume if you don't need it.
- **`sensitivity_ch0` / `sensitivity_ch1`** — your microphone's calibrated
  sensitivity in **mV/Pa**.
  - Leave at `1000` (the default = no scaling) and the stream is in **volts**.
  - Set it to your mic's value (e.g. `50` for 50 mV/Pa) and the stream is in
    **pascals**, so the laptop reports true **SPL in dB** (re 20 µPa).
- **`autostart`** (`[control]`) — `true` starts streaming at boot; `false`
  boots idle and waits for the laptop to send `start`.

## 4. Run once by hand (recommended before enabling the service)

On the Pi:

```sh
cd ~/daqhats/examples/python/mcc172/noise_monitor
python3 noise_monitor.py
```

On the laptop (replace the IP with your Pi's address):

```sh
python3 laptop_client.py --host 192.168.1.50
```

You get an interactive prompt. Type `help` for the full list. Typical session:

```
> info                        # firmware / serial / calibration date
> stop                        # config changes require a stopped scan
> set_sensitivity 0 50        # channel 0 mic = 50 mV/Pa  -> data in Pa, SPL in dB
> set_rate 25600              # sample rate (Hz/ch), rounded to 51200/N
> set_channels 0 1            # scan both channels
> start                       # begin streaming; live meter appears
> record capture.f64          # record raw samples to a file
> status                      # scan status + overrun flags
> stoprec
> quit
```

You can also record immediately on connect with `--out`:

```sh
python3 laptop_client.py --host 192.168.1.50 --out capture.f64   # raw float64
python3 laptop_client.py --host 192.168.1.50 --out capture.npy   # numpy (channels x samples)
```

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

## Control commands

The laptop sends commands and the Pi executes them against the MCC 172 API.
Configuration commands require the scan to be **stopped** first (the hardware
rejects changes while a scan is active) — send `stop`, reconfigure, then
`start`. Queries and `start`/`stop`/`blink`/`info` work anytime.

| Client command | Server `cmd` | MCC 172 call | Needs stop |
|---|---|---|:-:|
| `start` | `start` | `a_in_scan_start` | — |
| `stop` | `stop` | `a_in_scan_stop` / `cleanup` | — |
| `status` | `status` | `a_in_scan_status`, `a_in_scan_buffer_size` | — |
| `get_config` | `get_config` | (state snapshot) | — |
| `info` | `info` | `info`, `firmware_version`, `serial`, `calibration_date` | — |
| `get_sensitivity <ch>` | `get_sensitivity` | `a_in_sensitivity_read` | — |
| `set_sensitivity <ch> <mV>` | `set_sensitivity` | `a_in_sensitivity_write` | ✓ |
| `get_iepe <ch>` | `get_iepe` | `iepe_config_read` | — |
| `set_iepe <ch> <on\|off>` | `set_iepe` | `iepe_config_write` | ✓ |
| `get_clock` | `get_clock` | `a_in_clock_config_read` | — |
| `set_rate <Hz>` | `set_sample_rate` | `a_in_clock_config_write` | ✓ |
| `set_channels <ch>...` | `set_channels` | (channel mask) | ✓ |
| `trigger <on\|off> [mode] [source]` | `set_trigger` | `trigger_config` | ✓ |
| `options [continuous=on\|off] [ext_clock=on\|off]` | `set_options` | scan option flags | ✓ |
| `calibration_read <ch>` | `calibration_read` | `calibration_coefficient_read` | — |
| `calibration_write <ch> <slope> <offset>` | `calibration_write` | `calibration_coefficient_write` | ✓ |
| `blink <n>` | `blink_led` | `blink_led` | — |
| `test_signals <mode> [clk] [sync]` | `test_signals_write` | `test_signals_write` | ✓ |
| `send <raw json>` | (any) | — | — |

`trigger` modes: `RISING_EDGE`, `FALLING_EDGE`, `ACTIVE_HIGH`, `ACTIVE_LOW`.
Clock/trigger sources: `LOCAL`, `MASTER`, `SLAVE`.

## Wire protocol

For anyone writing their own client.

**Downstream (Pi → laptop)** — every message is a typed, length-prefixed frame:

```
[1-byte type][4-byte little-endian uint32 = payload length][payload]
   type 0x01 DATA : payload = interleaved little-endian float64 samples,
                    channel-fastest: ch0[n], ch1[n], ch0[n+1], ch1[n+1], ...
   type 0x02 MSG  : payload = UTF-8 JSON
```

The first MSG on connect is the handshake (current configuration); later
MSGs are command responses and events (`overrun`, `stopped`). Example
handshake:

```json
{"type": "handshake", "protocol": "mcc172-noise-monitor/2",
 "running": false, "channels": [0, 1], "num_channels": 2,
 "sample_rate": 51200.0, "actual_rate": 51200.0, "clock_source": "LOCAL",
 "iepe": {"0": 1, "1": 1}, "sensitivity_mv_per_unit": {"0": 50.0, "1": 1000.0},
 "units": {"0": "Pa", "1": "V"}, "trigger": {"enabled": false, ...},
 "dtype": "float64", "byte_order": "little", "interleave": "channel-fastest"}
```

**Upstream (laptop → Pi)** — one UTF-8 JSON command per line (`\n`):

```json
{"id": 4, "cmd": "set_sensitivity", "channel": 0, "value": 50}
```

The optional `id` is echoed in the response so replies can be matched:

```json
{"type": "response", "id": 4, "ok": true, "cmd": "set_sensitivity",
 "result": {"channel": 0, "sensitivity": 50.0, "units": "Pa"}}
```

Errors come back as `{"type": "response", "id": 4, "ok": false, "error": "..."}`.

A slow client never stalls acquisition: the server keeps a bounded
per-client queue (`max_queue_blocks`) and drops the oldest DATA blocks when
a laptop cannot keep up. Multiple laptops can connect at once; DATA and
events are broadcast to all, command responses go only to the requester.

## Notes & tips

- **Changing settings while streaming.** The MCC 172 rejects configuration
  changes during an active scan, so the client does too. Send `stop`, make
  your change, then `start`. The live meter and units update automatically.
- **SPL / A-weighting.** The stream is unweighted (Z-weighting). Apply an
  A-weighting filter on the laptop if you need dB(A).
- **Raspberry Pi Zero 2 W.** Streaming raw 2 ch × 51.2 kHz float64 is
  ~820 kB/s. That is fine over Wi-Fi/USB-ethernet, but if you see buffer
  overruns lower the sample rate or use one channel.
- **Sensor calibration vs. factory calibration.** `set_sensitivity` scales
  data to engineering units (Pa) for SPL. `calibration_write` overrides the
  factory ADC slope/offset — use only if you know what you're doing.
