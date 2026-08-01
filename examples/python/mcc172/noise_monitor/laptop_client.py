#!/usr/bin/env python3
#  -*- coding: utf-8 -*-
"""
MCC 172 Noise Monitor -- laptop side.

Connects to the Raspberry Pi running noise_monitor.py, reads the JSON
handshake and the raw waveform stream, and:

    * prints a live per-block RMS readout (and SPL in dB when the stream is
      calibrated to pascals), and
    * optionally records the raw samples to disk.

This client has no dependency on the daqhats library, so it runs on any
laptop with a standard Python 3 install. It uses numpy for the RMS/SPL
math when available and falls back to pure Python otherwise.

Examples:
    # Live readout only
    python3 laptop_client.py --host 192.168.1.50

    # Live readout and record raw interleaved float64 to a file
    python3 laptop_client.py --host 192.168.1.50 --out capture.f64

    # Record to a .npy file (numpy required), one row per channel
    python3 laptop_client.py --host 192.168.1.50 --out capture.npy
"""
import argparse
import json
import math
import socket
import struct
import sys
from array import array

try:
    import numpy as np
except ImportError:
    np = None

FRAME_HEADER = struct.Struct('<I')
REFERENCE_PRESSURE = 20e-6  # 20 uPa, standard SPL reference


def recv_exact(sock, num_bytes):
    """Read exactly num_bytes from the socket, or return None on EOF."""
    chunks = []
    remaining = num_bytes
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)


def read_handshake(sock):
    """Read the newline-terminated JSON metadata line sent on connect."""
    buf = bytearray()
    while b'\n' not in buf:
        chunk = sock.recv(1)
        if not chunk:
            raise ConnectionError('Server closed before sending handshake')
        buf.extend(chunk)
    return json.loads(buf.decode('utf-8').strip())


def rms_per_channel(samples, num_channels):
    """RMS of each channel from an interleaved sample sequence."""
    if np is not None:
        data = np.frombuffer(samples, dtype='<f8').reshape(-1, num_channels)
        return [float(math.sqrt(np.mean(np.square(data[:, c]))))
                for c in range(num_channels)]

    values = array('d')
    values.frombytes(samples)
    sums = [0.0] * num_channels
    count = len(values) // num_channels
    for i, sample in enumerate(values):
        chan = i % num_channels
        sums[chan] += sample * sample
    return [math.sqrt(s / count) if count else 0.0 for s in sums]


def spl_db(pressure_rms):
    """Sound pressure level in dB re 20 uPa. Returns None for silence."""
    if pressure_rms <= 0:
        return None
    return 20.0 * math.log10(pressure_rms / REFERENCE_PRESSURE)


def open_output(path, num_channels):
    """Return a writer callable and a finalizer for the requested output."""
    if path is None:
        return (lambda _samples: None), (lambda: None)

    if path.endswith('.npy'):
        if np is None:
            raise RuntimeError('.npy output requires numpy')
        blocks = []

        def write_npy(samples):
            block = np.frombuffer(samples, dtype='<f8').reshape(-1,
                                                                num_channels)
            blocks.append(block.copy())

        def finalize_npy():
            if blocks:
                np.save(path, np.concatenate(blocks, axis=0).T)
                print('\nSaved {} to {}'.format(
                    tuple(np.concatenate(blocks, axis=0).T.shape), path))

        return write_npy, finalize_npy

    # Raw interleaved little-endian float64 (.f64 / .bin).
    handle = open(path, 'wb')
    return handle.write, handle.close


def main():
    parser = argparse.ArgumentParser(
        description='Receive the MCC 172 raw waveform stream from a Pi.')
    parser.add_argument('--host', required=True,
                        help="Raspberry Pi IP address or hostname.")
    parser.add_argument('--port', type=int, default=5000,
                        help='TCP port (default 5000).')
    parser.add_argument('--out', default=None,
                        help='Optional file to record raw samples '
                             '(.f64/.bin = raw interleaved, .npy = numpy).')
    args = parser.parse_args()

    sock = socket.create_connection((args.host, args.port))
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    meta = read_handshake(sock)

    channels = meta['channels']
    num_channels = meta['num_channels']
    units = meta.get('units', {})
    print('Connected to {}:{}'.format(args.host, args.port))
    print('  sample rate : {} Hz/ch'.format(meta['sample_rate']))
    print('  channels    : {}'.format(channels))
    print('  units       : {}'.format(units))
    print('  IEPE        : {}'.format('on' if meta.get('iepe_enable')
                                      else 'off'))
    print('\nStreaming... press Ctrl-C to stop.\n')

    write, finalize = open_output(args.out, num_channels)
    total_samples = 0
    try:
        while True:
            header = recv_exact(sock, FRAME_HEADER.size)
            if header is None:
                print('\nServer closed the connection.')
                break
            (payload_len,) = FRAME_HEADER.unpack(header)
            payload = recv_exact(sock, payload_len)
            if payload is None:
                print('\nServer closed the connection.')
                break

            write(payload)
            total_samples += payload_len // 8 // num_channels

            rms_values = rms_per_channel(payload, num_channels)
            parts = []
            for idx, chan in enumerate(channels):
                rms = rms_values[idx]
                if units.get(str(chan)) == 'Pa':
                    level = spl_db(rms)
                    level_str = ('{:6.1f} dB'.format(level)
                                 if level is not None else '   -- dB')
                    parts.append('ch{}: {:.4e} Pa ({})'.format(
                        chan, rms, level_str))
                else:
                    parts.append('ch{}: {:.6f} Vrms'.format(chan, rms))
            print('\r{:>12} samples  |  {}'.format(total_samples,
                                                   '   '.join(parts)),
                  end='')
            sys.stdout.flush()
    except KeyboardInterrupt:
        print('\nStopping.')
    finally:
        finalize()
        sock.close()


if __name__ == '__main__':
    main()
