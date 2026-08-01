# PiSLM — Installation Manual

Complete build guide for a **PiSLM** node: a Raspberry Pi 4 with an
MCC 172 DAQ HAT and a Data Translation DT9837A, giving 6 calibrated IEPE
channels streamed to a laptop as sound-level-meter data.

Work through the sections in order. Sections 1–4 are hardware, 5–9 are
software, 10–13 are configuration and commissioning.

---

## 1. Bill of materials

### Required

| Item | Spec / note |
|------|-------------|
| Raspberry Pi 4 Model B | **4 GB** recommended (2 GB works with a shorter RAM buffer). The DSP needs the A72's single-core speed; a Zero 2 W / Pi 3 cannot run 6 ch with bands. |
| Official Pi 4 PSU | 5.1 V / 3 A USB-C. Do not use a phone charger — supply dips show up as noise. |
| microSD card | 32 GB, A1/A2 class. **OS only** — no measurement data is written to it. |
| MCC 172 DAQ HAT | 2 IEPE channels, 51.2 kS/s/ch. Address 0 (all address jumpers removed). |
| DT9837A | 4 IEPE channels, USB-powered. Optional if 2 channels are enough. |
| IEPE microphones | With a **calibrated sensitivity in mV/Pa** (from the datasheet or a calibration certificate). |
| Ethernet cable | Cat5e or better, Pi ↔ laptop (direct or via switch). |
| Heatsink or fan case | The MCC 172 spec limits operation to 0–55 °C. |

### Recommended

| Item | Why |
|------|-----|
| Powered USB hub | Keeps the DT9837A's 4 IEPE supplies off the Pi's 1.2 A USB budget. Required if you also attach USB storage. |
| Sound level calibrator | 94 dB / 114 dB @ 1 kHz — the only way to verify the calibration end to end (§12). |
| Jumper wires + 2-pin screw terminal | For the GPIO synchronized-start trigger (§4). |
| Acoustic calibrator adapter | Matches the calibrator cavity to your microphone diameter. |

---

## 2. Hardware assembly

**Power off the Pi and unplug it before touching the header.**

1. **Seat the MCC 172** on the Pi's 40-pin header, pressing evenly until it
   is fully home. With one HAT, leave **all address jumpers removed**
   (address 0) — one board must be at address 0 for the OS to read the HAT
   EEPROM.
2. **Fit the heatsink/fan** before mounting the HAT if your case requires it;
   the HAT covers the SoC.
3. **Connect the DT9837A** to a USB 3.0 port on the Pi (blue), or to a
   powered hub. Use the supplied USB cable — the DT9837A is bus-powered.
4. **Connect the microphones**:
   - MCC 172: 10-32 coaxial jacks (CH0, CH1), or the screw terminals — but
     **only one source per channel**, never both at once.
   - DT9837A: BNC inputs (CH0–CH3).
5. **Ethernet** from the Pi to the laptop or switch.

### Grounding (affects measurement quality)

The MCC 172 electrical specification states: *connect the signal source and
the Raspberry Pi to a common ground; if the source is floating, connect the
MCC 172 to earth ground via the DGND screw terminal to minimise common-mode
noise.*

- The DT9837A shares ground with the Pi through USB, so no extra bonding is
  needed between the two devices.
- Avoid ground loops: power the Pi from **one** supply, and prefer a single
  earth reference for the whole measurement chain.

---

## 3. Power

| Load | Draw |
|------|-----:|
| Raspberry Pi 4 (under load) | ~5.5 W |
| MCC 172 (incl. 2 IEPE supplies) | 0.7 W (140 mA @ 5 V max) |
| DT9837A (incl. 4 IEPE supplies) | up to 2.5 W (USB bus-powered) |
| **Total** | **~8.7 W** |

The official 5.1 V / 3 A (15.3 W) PSU covers this with margin. The Pi 4's
total USB budget is 1.2 A; the DT9837A's ≤500 mA fits, but add a **powered
hub** if you also attach USB storage.

**Battery operation**: any USB-PD bank that holds 5 V / 3 A works. At ~8.7 W
(≈7.5 W with §11 tuning), a 20 000 mAh (74 Wh) bank runs roughly 8 hours.

---

## 4. GPIO synchronized-start trigger (optional)

Wire this if you want both devices to begin their scans on the same edge.
Skip it for single-device use or if start alignment does not matter.

```
GPIO 17 (BCM, header pin 11) --+-- MCC 172 "TRIG"  (J5 pin 1)
                               +-- DT9837A "Ext Trigger" input
GND (header pin 9) ------------+-- MCC 172 "GND"   (J5 pin 2)
                               +-- DT9837A ground
```

- **Rising edge only** — the DT9837A's external digital trigger supports no
  other edge. 3.3 V GPIO satisfies both inputs (MCC 172 V_IH 1.48 V max).
- **Do not use** the pins the MCC 172 occupies: BCM 0, 1, 5, 6, 8–13, 16,
  19, 20, 26. Safe alternatives to 17: **27, 22**.
- Keep the trigger wire short and away from the microphone cables.

> This aligns the **start** of the scans (≈±1 sample per device plus each
> ADC's fixed group delay). It does **not** lock the ADC clocks — for that,
> enable the software clock alignment (`[resample]`, see §12 and the README),
> which measures each device's true rate and resamples both onto one grid.

---

## 5. Operating system

**Raspberry Pi OS (64-bit, Lite), Trixie.**

- **Raspberry Pi OS**, not Ubuntu/DietPi: the daqhats installer calls
  `raspi-config` directly to enable SPI and installs its dependencies with
  `apt`. Other distributions break that step.
- **64-bit**: faster numpy/scipy (this system is DSP-bound), and the RAM
  ring buffer reaches ~740 MB for 6 channels at 300 s.
- **Lite**: headless measurement node — no desktop background load. The
  CLI tools (`daqhats_list_boards` etc.) are all still there.
- **Trixie** ships **libgpiod v2** and **Python 3.13**. Both are supported:
  daqhats picks its GPIO backend from `pkg-config --modversion libgpiod`
  and builds `gpio_v2.c` for v2, and PiSLM's trigger tries libgpiod v2
  before v1 and RPi.GPIO. Trixie support in daqhats is recent, so if
  anything fails to build, Bookworm is the conservative fallback.

Steps:

1. Flash **Raspberry Pi OS (64-bit, Lite)** with Raspberry Pi Imager. In the
   Imager's settings (gear icon) pre-configure hostname, username, SSH and
   locale.

   - **Hostname**: `pislm` for a single node; number them (`pislm-01`,
     `pislm-02`, …) if you will ever run more than one — renaming later
     breaks `.local` addresses and your records. Lower-case letters,
     digits and hyphens only.
   - **Username**: Raspberry Pi OS has **no default `pi` account** any
     more, so you must choose one. Anything works; the rest of this manual
     derives the paths from `$USER` and `$HOME`, and §13 generates the
     systemd unit for whichever name you picked. Avoid `pi` itself — it is
     the first name anything scanning the network will try.
2. Boot the Pi, log in over SSH, and update:

   ```sh
   sudo apt update && sudo apt full-upgrade -y
   sudo reboot
   ```

3. Confirm SPI is not claimed by anything else. GPIO-header LCDs and other
   SPI HATs **will** break the MCC 172 — remove their overlays from
   `/boot/firmware/config.txt` if any were ever installed.

4. Check what you actually got — the rest of the manual assumes these:

   ```sh
   cat /etc/os-release | head -2      # expect trixie
   dpkg --print-architecture          # expect arm64
   pkg-config --modversion libgpiod   # expect 2.x on Trixie
   python3 --version                  # expect 3.13.x
   ```

---

## 6. Install the daqhats library (MCC 172)

```sh
cd ~
git clone https://github.com/mccdaq/daqhats.git
cd daqhats
sudo ./install.sh
```

The installer reads the HAT EEPROM and may prompt for a reboot. Verify:

```sh
daqhats_list_boards
```

You should see the MCC 172 at address 0. If not, re-seat the HAT and check
that no other SPI device is configured.

---

## 7. Install uldaq (DT9837A)

Skip this section if you are not using the DT9837A.

```sh
sudo apt install -y gcc g++ make libusb-1.0-0-dev

# C library (check the project page for the current release tag)
cd ~
wget https://github.com/mccdaq/uldaq/releases/download/v1.2.1/libuldaq-1.2.1.tar.bz2
tar -xvjf libuldaq-1.2.1.tar.bz2
cd libuldaq-1.2.1
./configure && make
sudo make install
sudo ldconfig
```

> **Trixie note.** This is the one build in the whole install that is not
> maintained against Trixie, and it uses the distribution's compiler
> (Trixie's GCC is much newer than what the release was written for). If
> `make` stops on an error, the usual fix is to relax the new default
> diagnostics:
>
> ```sh
> make CFLAGS="-w -std=gnu11" CXXFLAGS="-w -std=gnu++14"
> ```
>
> If it still will not build, that is the point to fall back to Bookworm —
> everything else here works on either release.

The Python binding is installed into the virtual environment in §8.

Allow non-root USB access:

```sh
sudo tee /etc/udev/rules.d/99-dt9837a.rules >/dev/null <<'EOF'
# Data Translation DT9837A — allow access for the plugdev group
SUBSYSTEM=="usb", ATTRS{idVendor}=="0a2d", MODE="0666", GROUP="plugdev"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger
sudo usermod -aG plugdev "$USER"
```

The service runs as your login user, so that user needs USB access — that
is what the `plugdev` group above is for. Log out and back in, then verify
the device enumerates:

```sh
lsusb | grep -i "data translation"
```

---

## 8. Python environment

Install the heavy scientific packages from `apt` — Debian's builds are
optimised for the platform, and letting `pip` compile numpy/scipy on a Pi is
both slow and slower at runtime:

```sh
sudo apt install -y python3-numpy python3-scipy python3-libgpiod python3-venv
```

- `numpy` / `scipy` — levels, metrics, band filtering, resampling.
- `python3-libgpiod` — on Trixie this is **libgpiod v2**; PiSLM's trigger
  detects v2, v1, or `RPi.GPIO` automatically.

Trixie enforces **PEP 668**, so system-wide `pip install` is refused. Create
a virtual environment that can still see the apt packages, and install the
device bindings into it:

```sh
python3 -m venv --system-site-packages ~/pislm-venv
~/pislm-venv/bin/pip install daqhats
~/pislm-venv/bin/pip install uldaq        # only if using the DT9837A
```

`--system-site-packages` is what makes the apt numpy/scipy visible inside
the venv — without it, pip would try to build them from source.

Verify everything imports together:

```sh
~/pislm-venv/bin/python -c "import numpy, scipy, gpiod, daqhats, uldaq; print('deps ok')"
```

(Drop `uldaq` from that line if you are not using the DT9837A.)

> Prefer not to use a venv? `sudo pip install daqhats --break-system-packages`
> works too, but the venv keeps the measurement system independent of OS
> package updates — worth it on an instrument.

---

## 9. Install PiSLM

PiSLM lives in the daqhats checkout from §6:

```sh
cd ~/daqhats/examples/python/mcc172/pislm
ls          # pislm.py, config.ini, devices.py, ...
```

Nothing to build — it is plain Python.

---

## 10. Network setup

For a direct Pi ↔ laptop link, give the Pi a static address on the wired
interface:

```sh
sudo nmcli con mod "Wired connection 1" \
    ipv4.method manual ipv4.addresses 192.168.50.1/24
sudo nmcli con up "Wired connection 1"
```

Set the laptop's wired interface to `192.168.50.2/24`. Verify from the
laptop:

```sh
ping 192.168.50.1
```

Gigabit Ethernet carries the full raw stream (6 ch ≈ 20 Mbit/s) with room to
spare, and keeps RF away from the microphone lines.

---

## 11. Low-power / low-noise tuning (recommended)

On a wired, headless measurement node, disable the radios and video:

```sh
sudo tee -a /boot/firmware/config.txt >/dev/null <<'EOF'

# --- PiSLM: headless measurement node ---
dtoverlay=disable-wifi
dtoverlay=disable-bt
EOF
sudo systemctl disable --now bluetooth
sudo reboot
```

Saves roughly 0.5–1 W and removes the on-board radios as a noise source.
Keep Wi-Fi enabled if a tablet will connect wirelessly for live readout.

---

## 12. Configure and calibrate

Edit `config.ini`. The settings you **must** get right:

```ini
[devices]
enabled = mcc172, dt9837a      ; omit dt9837a for a 2-channel node

[mcc172]
channels = 0, 1
iepe_enable = true
sensitivity_ch0 = 50           ; ← YOUR microphone, in mV/Pa
sensitivity_ch1 = 50

[dt9837a]
channels = 0, 1, 2, 3
iepe_enable = true
sensitivity_ch0 = 50
; ... ch1..ch3
```

**Sensitivity is the calibration.** Enter each microphone's mV/Pa value and
the data comes back in **pascals**, so all levels and metrics are true SPL
re 20 µPa. Left at the default `1000`, the data stays in volts.

Other settings worth reviewing (see the comments in the file): `sample_rate`,
`[weighting] frequency/time`, `[level] output_rate`, `[storage]
buffer_seconds`, `[bands]`, `[dsp] workers`, `[trigger] sync_start`.

### Calibrate with an acoustic calibrator

You do **not** have to know the sensitivity in advance, and you do not have
to do the arithmetic — `calibrate` derives it from the calibrator tone. This
is also how you calibrate later, in the field, without touching the Pi.

1. Start PiSLM (§13) and let the scan run for a few seconds.
2. Fit the calibrator to the microphone on channel 0 and switch it on
   (94 dB @ 1 kHz).
3. From the laptop:

   ```sh
   printf '{"id":1,"cmd":"calibrate","channel":0,"level_db":94}\n' \
       | nc 192.168.50.1 5000
   ```

   The response reports `measured_level_db` (what the old calibration
   thought the tone was), `new_sensitivity` in mV/Pa, and `change_db`. The
   new value is applied immediately — the scan is briefly stopped and
   restarted, which is normal.

4. Move the calibrator to the next microphone, wait a couple of seconds for
   the buffers to refill, and repeat with that channel number.
5. **Persist it** — otherwise the values are lost on the next restart:

   ```sh
   printf '{"id":2,"cmd":"save_config"}\n' | nc 192.168.50.1 5000
   ```

   This rewrites the `sensitivity_chN` values in `config.ini`, keeping the
   file's comments and adding a `; saved <date>` marker per line.

**Checking without changing anything** (drift check before a session):

```sh
printf '{"id":3,"cmd":"calibrate","channel":0,"level_db":94,"apply":false}\n' \
    | nc 192.168.50.1 5000
```

`change_db` is how far the channel has drifted. A well-behaved chain should
be within a few tenths of a dB; note the value in your measurement record.

**Verify** afterwards with the calibrator still fitted:

```sh
printf '{"id":4,"cmd":"get_metrics","seconds":5,"channels":[0]}\n' \
    | nc 192.168.50.1 5000
```

`Leq` should read **94 dB ±0.5** (A-weighting is ≈0 dB at 1 kHz).

> If you already know the sensitivity from the microphone's certificate,
> just put it in `config.ini` (§12) or send
> `{"cmd":"set_sensitivity","channel":0,"value":50}` with the scan stopped —
> `calibrate` is for deriving it from a calibrator instead.

---

## 13. First run and automatic start

**Step 1 — run by hand** (always do this before enabling the service):

```sh
cd ~/daqhats/examples/python/mcc172/pislm
~/pislm-venv/bin/python pislm.py
```

Expected output: each device detected, the DSP worker count, and the two
listening ports. From the laptop:

```sh
printf '{"id":1,"cmd":"status"}\n' | nc 192.168.50.1 5000
```

Stop with Ctrl-C.

**Step 2 — install the service:**

The shipped unit uses `pi` as a placeholder, but Raspberry Pi OS has no
default `pi` account any more. Generate the unit for whoever you actually
created, straight from the current login:

```sh
cd ~/daqhats/examples/python/mcc172/pislm
sed -e "s|User=pi|User=$USER|" \
    -e "s|/home/pi|$HOME|g" \
    pislm.service | sudo tee /etc/systemd/system/pislm.service >/dev/null

# Check it points at your user, your home, and the venv interpreter:
grep -E "^(User|WorkingDirectory|ExecStart|Environment)=" \
    /etc/systemd/system/pislm.service

sudo systemctl daemon-reload
sudo systemctl enable --now pislm
```

**Step 3 — check it:**

```sh
systemctl status pislm
journalctl -u pislm -f
```

After editing `config.ini`: `sudo systemctl restart pislm`.

---

## 14. Field checklist

Before each measurement session:

- [ ] Microphones connected; IEPE enabled for every channel in use
- [ ] `sensitivity_chN` matches the microphone actually fitted
- [ ] Calibrator check passed (§12) — note the deviation
- [ ] Windscreens fitted outdoors
- [ ] `systemctl status pislm` active; laptop can reach both ports
- [ ] `buffer_seconds` long enough to cover your longest event
- [ ] Laptop has disk space if recording raw (6 ch ≈ 8.8 GB/hour)

**Cross-device phase.** The two ADC clocks are independent (±50 ppm each).
Per-channel levels and metrics are unaffected either way, but for phase or
correlation *between* devices you need both the GPIO trigger (§4, aligns the
start) **and** `[resample] enabled = true` (aligns the rates). Check
`clock` in `status`: each device's `settled` should be true and the run
should be at least a minute old before you trust cross-device phase — the
rate estimate reaches ~2 ppm at 60 s and ~0.15 ppm at 300 s. Without
resampling, keep phase-coherent channel pairs on the same device.

---

## 15. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `daqhats_list_boards` finds nothing | HAT not fully seated, or another SPI device configured. Check `/boot/firmware/config.txt` for display/SPI overlays. |
| `No DT9837A device found` | udev rule not applied, or user not in `plugdev`. Re-log in; check `lsusb`. |
| `overrun` events, scan stops | The Pi cannot keep up. Lower `sample_rate`, lower `[bands] f_max`, turn off `[resample]`, or confirm `[dsp] workers` is `-1` (not `0`). |
| Cross-device phase drifts over time | Enable `[resample]`; wait for `clock.settled` on both devices (~60 s). |
| `clock.ppm` reads hundreds of ppm | Not a crystal error — usually a stalled or restarted scan. Restart and re-check; values beyond ±500 ppm are rejected as implausible. |
| Levels ~0 dB or nonsense | IEPE off, or `sensitivity` left at 1000 (data in volts, not Pa). |
| Level is off by a fixed amount | Recalibrate with the calibrator (§12). |
| Hum / mains buzz | Ground loop. Use one PSU, bond DGND to earth for floating sources, keep cables away from mains. |
| `trigger GPIO unavailable` | Missing `python3-libgpiod`, or the pin collides with the MCC 172 (§4). On Trixie this package is libgpiod v2, which PiSLM detects automatically. |
| `error: externally-managed-environment` from pip | Trixie enforces PEP 668. Install into the venv (§8), or append `--break-system-packages`. |
| `ModuleNotFoundError: daqhats` / `uldaq` under systemd but not by hand | The unit is running the system python. Point `ExecStart` at `~/pislm-venv/bin/python` (§13). |
| uldaq `make` fails on Trixie | Newer GCC diagnostics; retry with `make CFLAGS="-w -std=gnu11" CXXFLAGS="-w -std=gnu++14"` (§7). |
| Service dies at boot, works by hand | Wrong `User=`/paths in the unit, or it started before the HAT was ready — `Restart=always` retries; check `journalctl -u pislm`. |
| Client cannot connect | Check the Pi's IP and that `config.ini` binds `host = 0.0.0.0`. |

---

## 16. Next steps

- **Write your client** against [`PROTOCOL.md`](PROTOCOL.md) — the complete
  wire specification (both ports, frame types, every command).
- **Operating notes and tuning** are in [`README.md`](README.md).
