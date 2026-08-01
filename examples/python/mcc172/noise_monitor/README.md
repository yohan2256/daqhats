# MCC 172 Noise Monitor

A headless setup that turns a Raspberry Pi + **MCC 172** DAQ HAT into a
network sound/vibration probe. On boot the Pi powers an IEPE microphone,
applies your calibration, continuously acquires the raw waveform, and
streams it over TCP to a laptop.

```
[IEPE mic] --2 mA IEPE--> [MCC 172 HAT] --SPI--> [Raspberry Pi]
                                                       |  (systemd @ boot)
                                                       |  noise_monitor.py
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
| `noise_monitor.py`     | Raspberry Pi | IEPE + calibration + continuous scan; TCP server that streams the raw waveform. |
| `config.ini`           | Raspberry Pi | Sample rate, channels, IEPE, sensitivity, network settings. |
| `noise-monitor.service`| Raspberry Pi | systemd unit for automatic start at boot. |
| `laptop_client.py`     | Laptop       | Connects, shows live RMS/SPL, optionally records raw samples. |

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

## 3. Configure

Edit `config.ini`. The two settings you will most likely change:

- **`sample_rate`** — 51200 gives the full audio bandwidth (~25 kHz). Lower
  it (e.g. 25600, 10240) to reduce data volume if you don't need it.
- **`sensitivity_ch0` / `sensitivity_ch1`** — your microphone's calibrated
  sensitivity in **mV/Pa**.
  - Leave at `1000` (the default = no scaling) and the stream is in **volts**.
  - Set it to your mic's value (e.g. `50` for 50 mV/Pa) and the stream is in
    **pascals**, so the laptop reports true **SPL in dB** (re 20 µPa).

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

You should see a live readout. Record the raw waveform with `--out`:

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

## Wire protocol

For anyone writing their own client:

1. On connect the server sends **one line of UTF-8 JSON** terminated by
   `\n`, describing the stream:

   ```json
   {"protocol": "mcc172-noise-monitor/1", "sample_rate": 51200.0,
    "channels": [0, 1], "num_channels": 2, "iepe_enable": true,
    "sensitivity_mv_per_unit": {"0": 50.0, "1": 1000.0},
    "units": {"0": "Pa", "1": "V"}, "dtype": "float64",
    "byte_order": "little", "interleave": "channel-fastest"}
   ```

2. It then streams frames continuously:

   ```
   [4-byte little-endian uint32 = payload length in bytes]
   [payload = interleaved little-endian float64 samples]
   ```

   Interleave order matches the MCC 172 read buffer:
   `ch0[n], ch1[n], ch0[n+1], ch1[n+1], ...`

A slow client never stalls acquisition: the server keeps a bounded
per-client queue (`max_queue_blocks`) and drops the oldest blocks when a
laptop cannot keep up.

## Notes & tips

- **SPL / A-weighting.** The stream is unweighted (Z-weighting). Apply an
  A-weighting filter on the laptop if you need dB(A).
- **Raspberry Pi Zero 2 W.** Streaming raw 2 ch × 51.2 kHz float64 is
  ~820 kB/s. That is fine over Wi-Fi/USB-ethernet, but if you see buffer
  overruns lower the sample rate or use one channel.
- **Calibration coefficients.** For factory ADC calibration the MCC 172
  also exposes `calibration_coefficient_read/write`; this project uses the
  simpler, sensor-facing `a_in_sensitivity_write` path for SPL scaling.
