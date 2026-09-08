"""Check a real Pi before trusting a measurement to it.

    python check_device.py                  # 192.168.50.1
    python check_device.py --host 10.0.0.5
    python check_device.py --seconds 3      # longer stream sample

Runs through the things that have actually gone wrong in development, in the
order they matter: can we connect, does the handshake say what we expect, do
band levels flow, does a raw dump come back whole, and is the analog output
there. Everything is read-only apart from starting and stopping the scan, and
the scan is put back the way it was found.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pislm import PiSLM  # noqa: E402

OK, BAD, WARN, INFO = "[ ok ]", "[FAIL]", "[warn]", "      "


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.50.1")
    parser.add_argument("--control", type=int, default=5000)
    parser.add_argument("--stream", type=int, default=5001)
    parser.add_argument("--seconds", type=float, default=2.0)
    args = parser.parse_args()

    problems: list[str] = []
    print(f"\n=== {args.host}:{args.control}/{args.stream} ===\n")

    # 1) Connect
    pi = PiSLM(args.host, args.control, args.stream, connect_timeout=5.0)
    try:
        started = time.monotonic()
        pi.connect()
        pi.refresh()
    except Exception as exc:  # noqa: BLE001
        print(f"{BAD} cannot connect: {type(exc).__name__}: {exc}")
        print(f"{INFO} check the Pi is powered, pislm.py is running, and the")
        print(f"{INFO} laptop is on the same subnet (ping {args.host}).")
        return 1
    config = pi.config
    print(f"{OK} connected in {time.monotonic() - started:.2f} s")

    # 2) Handshake
    protocol = config.protocol
    mark = OK if protocol == "pislm/4" else WARN
    print(f"{mark} protocol {protocol}")
    if protocol != "pislm/4":
        problems.append(
            f"protocol is {protocol}; start_index and the drop policy need pislm/4"
        )

    devices = config.devices
    print(f"{OK} {len(devices)} device(s), {config.num_channels} channel(s), "
          f"{config.sample_rate:g} Hz")
    for device in devices:
        index = device.get("index")
        try:
            channels = config.device_channels(index)
            rate = config.device_rate(index)
        except Exception:  # noqa: BLE001
            continue
        units = []
        for channel in channels:
            try:
                units.append(config.channel_info(channel).units)
            except Exception:  # noqa: BLE001
                units.append("?")
        print(f"{INFO}   dev{index}: ch{channels} @ {rate:g} Hz  units={units}")

    # Resampling decides whether one WAV can hold every channel honestly.
    if len(devices) > 1:
        if config.resample_active:
            print(f"{OK} resampling active — devices share one time base")
        else:
            print(f"{WARN} resampling OFF with {len(devices)} devices")
            problems.append(
                "devices are not sample-locked; a combined WAV will drift "
                "(Settings -> Instrument options -> Sampling)"
            )

    if getattr(config, "epoch", None):
        print(f"{OK} epoch reported")
    else:
        print(f"{WARN} no epoch — wall-clock time of sample 0 is unknown")

    was_running = bool(config.running)
    print(f"{INFO} scan was {'running' if was_running else 'stopped'} on arrival")

    try:
        # 3) Band levels
        if not was_running:
            pi.start()
        time.sleep(0.5)
        pi.refresh()

        table = config.raw.get("band_table") or []
        bands = sum(len(d.get("bands", [])) for d in table)
        if bands:
            print(f"{OK} band table: {bands} band(s) over {len(table)} device(s)")
        else:
            print(f"{BAD} no band table — the spectrum will stay blank")
            problems.append("no band_table while scanning; check set_bands")

        seen: dict[str, int] = {}
        pi.stream.on_frame(
            lambda f: seen.__setitem__(
                type(f).__name__, seen.get(type(f).__name__, 0) + 1
            )
        )
        time.sleep(args.seconds)
        if seen:
            print(f"{OK} frames in {args.seconds:g} s: " +
                  ", ".join(f"{k}={v}" for k, v in sorted(seen.items())))
        else:
            print(f"{BAD} no stream frames arrived")
            problems.append("stream port is silent; check port 5001 is reachable")
        if "LevelFrame" not in seen:
            problems.append("no LEVEL frames — the live meters will not move")
        if "BandLevelFrame" not in seen:
            problems.append("no BAND_LEVEL frames — no spectrum, no measurement")

        # 4) Raw dump — WAV recording and reverberation both depend on it
        raw_was_on = bool(config.stream_raw)
        try:
            if not raw_was_on:
                pi.set_options(stream_raw=True)
            time.sleep(0.3)
            dumps = pi.fetch_raw(seconds=1.0, timeout=30.0)
            if dumps:
                for device, dump in sorted(dumps.items()):
                    samples = dump.channel(config.device_channels(device)[0]).size
                    print(f"{OK} raw dump dev{device}: {samples} samples "
                          f"@ {dump.sample_rate:g} Hz")
            else:
                print(f"{BAD} get_raw returned nothing")
                problems.append("no raw dump; WAV recording and T20 will not work")
        except Exception as exc:  # noqa: BLE001
            print(f"{BAD} raw dump failed: {type(exc).__name__}: {exc}")
            problems.append(f"get_raw failed: {exc}")
        finally:
            if not raw_was_on:
                pi.set_options(stream_raw=False)

        # 5) Integrity verdict from the client's own loss accounting.
        #    "unknown" is not a failure — it means no indexed frame has
        #    arrived yet, which is simply true while the scan is idle.
        status, reasons = pi.measurement_status
        if status == "ok":
            print(f"{OK} no loss that would invalidate a measurement")
        elif status == "unknown":
            print(f"{WARN} integrity not verifiable yet: {'; '.join(reasons)}")
        elif status == "warning":
            for reason in reasons:
                print(f"{WARN} {reason}")
        else:
            print(f"{BAD} integrity: {reasons}")
            # A summed sample count cannot say whether the meter really lost
            # data. The pattern of the gaps can.
            for line in pi.gaps.diagnosis().splitlines():
                print(f"     {line}")
            problems.extend(reasons)
        if status != "warning":
            for warning in pi.measurement_warnings:
                print(f"{WARN} {warning}")

        # 6) Analog output (PROTOCOL.md §10) — not implemented on the Pi yet
        if pi.output_available:
            print(f"{OK} analog output available: {config.raw.get('output')}")
        else:
            print(f"{WARN} no analog output — use an external speaker for T20")

    finally:
        try:
            if not was_running:
                pi.stop()
        except Exception:  # noqa: BLE001
            pass
        pi.close()

    print()
    if problems:
        print(f"{BAD} {len(problems)} thing(s) to look at:")
        for problem in problems:
            print(f"       - {problem}")
        return 1
    print(f"{OK} everything checked out")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
