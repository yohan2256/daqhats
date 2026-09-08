#!/usr/bin/env python3
"""CLI connectivity check — run this first against real hardware.

    # against the simulator
    python pislm_sim.py &
    python pislm_monitor.py 127.0.0.1

    # real hardware
    python pislm_monitor.py 192.168.0.42
    python pislm_monitor.py 192.168.0.42 --bands 3 --fmin 50 --fmax 630   # heavy impact
    python pislm_monitor.py 192.168.0.42 --bench 15                        # bandwidth
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from pislm import BandLevelFrame, CommandError, LevelFrame, PiSLM


def describe(pi: PiSLM) -> None:
    cfg = pi.config
    print(f"protocol      : {cfg.protocol}"
          + ("  (frame indices present — loss is detectable)" if cfg.has_sample_index
             else "  \u26a0 no frame index — frame loss cannot be detected"))
    print(f"running       : {cfg.running}")
    print(f"sample rate   : {cfg.sample_rate:g} Hz")
    if cfg.resample_active:
        print(f"resample      : active \u2192 {cfg.resample.get('output_rate'):g} Hz (common grid)")
    elif cfg.resample.get("enabled"):
        print("resample      : \u26a0 enabled but not active — drift remains")
    else:
        print("resample      : off — cross-device timing is not comparable (§9.9)")
    ppm = cfg.clock_ppm
    if ppm:
        detail = "  ".join(f"dev{d}: {p:+.2f} ppm" for d, p in sorted(ppm.items()))
        state = "settled" if cfg.clock_settled else "\u26a0 not settled — warm up first"
        print(f"clock         : {detail}   ({state})")
    print(f"weighting     : {cfg.weighting.get('frequency')} / {cfg.weighting.get('time')}")
    print(f"level rate    : {cfg.level_output_rate:g} Hz")
    print(f"buffer        : {cfg.buffer_seconds:g} s")
    print(f"bands         : {cfg.bands}")
    print(f"stream_raw    : {cfg.stream_raw}")
    print("channels:")
    for ch in cfg.all_channels():
        mark = "calibrated" if ch.calibrated else "uncalibrated (V)"
        print(
            f"  ch{ch.global_index}  dev{ch.device} {ch.device_type:<8} local{ch.local}"
            f"  {ch.sensitivity_mv_per_unit:>8.3f} mV/{ch.units}  IEPE={ch.iepe}  {mark}"
        )
    if cfg.has_band_table:
        bands = cfg.bands_of_device(0)
        print(f"band table    : {len(bands)} bands, nominal "
              f"{[b.nominal_center for b in bands[:8]]}{' ...' if len(bands) > 8 else ''}")


def main() -> int:
    ap = argparse.ArgumentParser(description="PiSLM live monitor")
    ap.add_argument("host")
    ap.add_argument("--control", type=int, default=5000)
    ap.add_argument("--stream", type=int, default=5001)
    ap.add_argument("--seconds", type=float, default=10.0, help="monitoring time")
    ap.add_argument("--bands", type=int, choices=(0, 1, 3), default=None,
                    help="0=off, 1=1/1 octave, 3=1/3 octave")
    ap.add_argument("--fmin", type=float, default=None)
    ap.add_argument("--fmax", type=float, default=None)
    ap.add_argument("--weighting", choices=("A", "C", "Z"), default=None)
    ap.add_argument("--time-weighting", choices=("Fast", "Slow", "Impulse"), default=None)
    ap.add_argument("--bench", type=float, default=None, help="benchmark then exit (seconds)")
    ap.add_argument("--raw", action="store_true", help="also stream raw DATA (watch bandwidth)")
    ap.add_argument("--metrics", action="store_true", help="print get_metrics on exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)

    try:
        pi = PiSLM(args.host, args.control, args.stream)
        pi.connect()
    except OSError as exc:
        print(f"connection failed: {exc}", file=sys.stderr)
        return 1

    with pi:
        pi.refresh()
        describe(pi)
        print()

        try:
            pi.stop()
            if args.bands is not None:
                if args.bands == 0:
                    pi.set_bands(enabled=False)
                else:
                    pi.set_bands(enabled=True, output="level", fraction=args.bands,
                                 f_min=args.fmin or (63 if args.bands == 1 else 50),
                                 f_max=args.fmax or (500 if args.bands == 1 else 630))
            if args.weighting or args.time_weighting:
                pi.set_weighting(frequency=args.weighting, time_weighting=args.time_weighting)
            pi.set_options(stream_raw=args.raw)
        except CommandError as exc:
            print(f"configuration failed: {exc}", file=sys.stderr)
            return 2

        pi.start()
        print("Scanning. Ctrl-C to stop.\n")

        if args.bench is not None:
            report = pi.bench(args.bench)
            pi.stop()
            print(f"[bench] {report['seconds']:.1f}s, control RTT ~{report['control_rtt_ms']:.1f} ms")
            print(f"  total {report['kb_per_s']:.1f} KB/s ({report['mbps']:.2f} Mbps), "
                  f"{report['frames_per_s']:.1f} frames/s")
            for name, count in sorted(report["by_type"].items()):
                print(f"    {name:<16} {count}")
            print(f"  dsp dropped_blocks: {report['dropped_blocks_delta']:+d}   "
                  f"stream frames dropped: {report['stream_frames_dropped_delta']:+d}   "
                  f"client queue overflows: {report['client_queue_overflows']}")
            return 0

        latest: dict[int, float] = {}
        band_latest: dict[tuple[int, int], float] = {}
        deadline = time.monotonic() + args.seconds
        try:
            while time.monotonic() < deadline:
                frame = pi.stream.get(timeout=0.2)
                if isinstance(frame, LevelFrame) and frame.levels_db.size:
                    latest[frame.channel] = float(frame.levels_db[-1])
                elif isinstance(frame, BandLevelFrame) and frame.levels_db.size:
                    band_latest[(frame.channel, frame.band_index)] = float(frame.levels_db[-1])
                if latest:
                    line = "  ".join(f"ch{c}:{latest[c]:6.1f}" for c in sorted(latest))
                    print(f"\r{line} dB    ", end="", flush=True)
        except KeyboardInterrupt:
            pass
        print("\n")

        if band_latest:
            cfg = pi.config
            ch0 = min(c for c, _ in band_latest)
            rows = sorted(
                (cfg.band_info_for_channel(ch0, b).nominal_center, v)
                for (c, b), v in band_latest.items() if c == ch0
            )
            print(f"ch{ch0} band levels (latest):")
            for center, level in rows:
                bar = "█" * max(0, int((level - 20) / 2))
                print(f"  {center:>7g} Hz  {level:6.1f} dB  {bar}")
            print()

        if args.metrics:
            result = pi.get_metrics(seconds=min(args.seconds, pi.config.buffer_seconds))
            print("get_metrics:")
            for ch, m in sorted(result["channels"].items(), key=lambda kv: int(kv[0])):
                print(f"  ch{ch}: Leq={m['Leq']:.1f}  Lmax={m['Lmax']:.1f}  "
                      f"Lmin={m['Lmin']:.1f}  Lpeak={m['Lpeak']:.1f}  "
                      f"({m['weighting']}/{m['time_weighting']}, {m['units']})")

        pi.stop()

        print("Time axis integrity:")
        print(pi.gaps.report())
        if pi.overloads:
            channels = sorted({o.get("channel") for o in pi.overloads})
            print(f"\n\u26a0 {len(pi.overloads)} overload event(s) on channel(s) {channels}")

        valid, reasons = pi.measurement_valid
        print()
        if valid:
            print("Verdict: usable as a measurement")
        else:
            print("Verdict: NOT usable as a measurement")
            for reason in reasons:
                print(f"  - {reason}")
        for warning in pi.measurement_warnings:
            print(f"  note: {warning}")

        after = pi.refresh()
        if after.stream_frames_dropped:
            detail = after.dropped_by_type or ""
            print(f"\nServer drops: {after.stream_frames_dropped} stream frame(s) (§6) {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
