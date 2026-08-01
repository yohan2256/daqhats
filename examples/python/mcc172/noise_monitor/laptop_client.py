#!/usr/bin/env python3
#  -*- coding: utf-8 -*-
"""
MCC 172 Noise Monitor -- laptop side (interactive control client).

Connects to a Raspberry Pi running noise_monitor.py and gives you a small
command shell to drive the MCC 172 remotely -- change sensitivity, sample
rate, IEPE power, trigger, options, calibration, blink the LED, read info,
and start/stop the raw-waveform stream -- while showing a live RMS/SPL meter
and optionally recording the raw samples.

No daqhats dependency, so it runs on any laptop with Python 3. numpy is used
for the RMS/SPL math when available, with a pure-Python fallback.

Type "help" at the prompt for the command list. Examples:
    start                       begin streaming
    stop                        stop streaming
    set_sensitivity 0 50        channel 0 mic = 50 mV/Pa (data becomes Pa)
    set_rate 25600              sample rate (Hz/ch)
    set_iepe 0 on               IEPE 2 mA supply on for channel 0
    set_channels 0 1            scan both channels
    trigger on RISING_EDGE      arm the external trigger
    info                        firmware / serial / calibration date
    status                      scan status + overrun flags
    record capture.f64          start recording raw samples to a file
    stoprec                     stop recording
    meter off                   hide the live meter
    send {"cmd":"blink_led","count":3}   send any raw JSON command
    quit
"""
import argparse
import json
import math
import socket
import struct
import sys
import threading
from array import array
from time import time

try:
    import numpy as np
except ImportError:
    np = None

TYPE_DATA = 0x01
TYPE_MSG = 0x02
FRAME_HEADER = struct.Struct('<BI')
REFERENCE_PRESSURE = 20e-6   # 20 uPa SPL reference


# --------------------------------------------------------------------------
# Receiver: reads typed frames, updates the meter/recording, prints replies
# --------------------------------------------------------------------------
class Receiver(threading.Thread):
    def __init__(self, sock):
        super().__init__(daemon=True)
        self.sock = sock
        self.meta = {}
        self.channels = []
        self.num_channels = 1
        self.units = {}
        self.show_meter = True
        self._meter_interval = 0.5
        self._last_meter = 0.0
        self._total_samples = 0
        self._record_lock = threading.Lock()
        self._record_write = None
        self._record_finalize = None
        self._record_path = None
        self.alive = True

    # -- recording control (called from the main thread) --
    def start_recording(self, path):
        write, finalize = open_output(path, self.num_channels)
        with self._record_lock:
            self._stop_recording_locked()
            self._record_write = write
            self._record_finalize = finalize
            self._record_path = path
        print('[rec] recording raw samples to {}'.format(path))

    def stop_recording(self):
        with self._record_lock:
            path = self._record_path
            self._stop_recording_locked()
        if path:
            print('[rec] stopped ({})'.format(path))
        else:
            print('[rec] not recording')

    def _stop_recording_locked(self):
        if self._record_finalize:
            self._record_finalize()
        self._record_write = None
        self._record_finalize = None
        self._record_path = None

    # -- frame loop --
    def run(self):
        try:
            while True:
                header = recv_exact(self.sock, FRAME_HEADER.size)
                if header is None:
                    break
                frame_type, length = FRAME_HEADER.unpack(header)
                payload = recv_exact(self.sock, length) if length else b''
                if payload is None:
                    break
                if frame_type == TYPE_DATA:
                    self._on_data(payload)
                elif frame_type == TYPE_MSG:
                    self._on_message(payload)
        except (OSError, socket.error):
            pass
        finally:
            self.alive = False
            with self._record_lock:
                self._stop_recording_locked()
            print('\n[net] connection closed. Press ENTER to exit.')

    def _on_data(self, payload):
        with self._record_lock:
            if self._record_write:
                self._record_write(payload)
        self._total_samples += len(payload) // 8 // self.num_channels
        if not self.show_meter:
            return
        now = time()
        if now - self._last_meter < self._meter_interval:
            return
        self._last_meter = now
        self._print_meter(payload)

    def _print_meter(self, payload):
        rms_values = rms_per_channel(payload, self.num_channels)
        parts = []
        for idx, chan in enumerate(self.channels):
            rms = rms_values[idx]
            if self.units.get(str(chan)) == 'Pa':
                level = spl_db(rms)
                level_str = ('{:6.1f} dB'.format(level)
                             if level is not None else '   -- dB')
                parts.append('ch{}: {:.3e} Pa {}'.format(chan, rms, level_str))
            else:
                parts.append('ch{}: {:.5f} Vrms'.format(chan, rms))
        sys.stdout.write('\r[meter] {:>11} samp | {}   '.format(
            self._total_samples, '  '.join(parts)))
        sys.stdout.flush()

    def _on_message(self, payload):
        try:
            msg = json.loads(payload.decode('utf-8'))
        except ValueError:
            return
        mtype = msg.get('type')
        if mtype == 'handshake':
            self._apply_meta(msg)
            print('\n[handshake] {}'.format(json.dumps(msg)))
        elif mtype == 'event':
            print('\n[event] {}'.format(json.dumps(msg)))
        else:  # command response
            self._apply_meta(msg.get('result', {}))
            ok = msg.get('ok')
            tag = '[ok]' if ok else '[error]'
            body = msg.get('result') if ok else msg.get('error')
            print('\n{} {}'.format(tag, json.dumps(body)))
        sys.stdout.write('> ')
        sys.stdout.flush()

    def _apply_meta(self, meta):
        """Track channel/unit info from handshakes and config responses so the
        meter stays correct after reconfiguration."""
        if not isinstance(meta, dict):
            return
        if 'channels' in meta:
            self.channels = meta['channels']
            self.num_channels = meta.get('num_channels', len(self.channels))
        if 'units' in meta:
            self.units = meta['units']


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------
def recv_exact(sock, num_bytes):
    chunks = []
    remaining = num_bytes
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)


def rms_per_channel(samples, num_channels):
    if np is not None:
        data = np.frombuffer(samples, dtype='<f8').reshape(-1, num_channels)
        return [float(math.sqrt(np.mean(np.square(data[:, c]))))
                for c in range(num_channels)]
    values = array('d')
    values.frombytes(samples)
    sums = [0.0] * num_channels
    count = len(values) // num_channels
    for i, sample in enumerate(values):
        sums[i % num_channels] += sample * sample
    return [math.sqrt(s / count) if count else 0.0 for s in sums]


def spl_db(pressure_rms):
    if pressure_rms <= 0:
        return None
    return 20.0 * math.log10(pressure_rms / REFERENCE_PRESSURE)


def open_output(path, num_channels):
    if path.endswith('.npy'):
        if np is None:
            raise RuntimeError('.npy output requires numpy')
        blocks = []

        def write_npy(samples):
            blocks.append(np.frombuffer(samples, dtype='<f8').reshape(
                -1, num_channels).copy())

        def finalize_npy():
            if blocks:
                data = np.concatenate(blocks, axis=0).T
                np.save(path, data)
                print('\n[rec] saved {} to {}'.format(data.shape, path))

        return write_npy, finalize_npy

    handle = open(path, 'wb')
    return handle.write, handle.close


# --------------------------------------------------------------------------
# Command shell: translate friendly commands into JSON, send them
# --------------------------------------------------------------------------
class CommandShell:
    HELP = """\
Commands (server side is authoritative; config changes require a stopped scan):
  start | stop
  status | get_config | info | ping
  get_sensitivity <ch> | get_iepe <ch> | get_clock
  set_sensitivity <ch> <mV/unit>     e.g. set_sensitivity 0 50  (mic mV/Pa)
  set_iepe <ch> <on|off>
  set_rate <Hz>                       (alias of set_sample_rate)
  set_channels <ch> [<ch> ...]
  trigger <on|off> [RISING_EDGE|FALLING_EDGE|ACTIVE_HIGH|ACTIVE_LOW] [LOCAL|MASTER|SLAVE]
  options [continuous=on|off] [ext_clock=on|off]
  calibration_read <ch>
  calibration_write <ch> <slope> <offset>
  blink <count>
  test_signals <mode> [clock] [sync]
  record <file.f64|file.npy> | stoprec
  meter <on|off>
  send <raw json>                     e.g. send {"cmd":"status"}
  help | quit"""

    def __init__(self, sock, receiver):
        self.sock = sock
        self.receiver = receiver
        self._id = 0

    def _send(self, obj):
        self._id += 1
        obj['id'] = self._id
        self.sock.sendall((json.dumps(obj) + '\n').encode('utf-8'))

    def run(self):
        print(self.HELP)
        while self.receiver.alive:
            try:
                line = input('> ').strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            if not self._dispatch(line):
                break
        # Local cleanup.
        self.receiver.stop_recording()

    def _dispatch(self, line):
        parts = line.split()
        cmd = parts[0].lower()
        args = parts[1:]
        try:
            if cmd in ('quit', 'exit'):
                return False
            if cmd == 'help':
                print(self.HELP)
            elif cmd == 'meter':
                self.receiver.show_meter = (not args or args[0] != 'off')
                print('[meter] {}'.format(
                    'on' if self.receiver.show_meter else 'off'))
            elif cmd == 'record':
                self.receiver.start_recording(args[0])
            elif cmd == 'stoprec':
                self.receiver.stop_recording()
            elif cmd == 'send':
                self._send(json.loads(line[len('send'):].strip()))
            elif cmd in ('start', 'stop', 'status', 'get_config', 'info',
                         'ping', 'get_clock'):
                self._send({'cmd': cmd})
            elif cmd in ('get_sensitivity', 'get_iepe', 'calibration_read'):
                self._send({'cmd': cmd, 'channel': int(args[0])})
            elif cmd == 'set_sensitivity':
                self._send({'cmd': 'set_sensitivity',
                            'channel': int(args[0]), 'value': float(args[1])})
            elif cmd == 'set_iepe':
                self._send({'cmd': 'set_iepe', 'channel': int(args[0]),
                            'mode': args[1]})
            elif cmd in ('set_rate', 'set_sample_rate'):
                self._send({'cmd': 'set_sample_rate',
                            'sample_rate': float(args[0])})
            elif cmd == 'set_channels':
                self._send({'cmd': 'set_channels',
                            'channels': [int(a) for a in args]})
            elif cmd == 'trigger':
                req = {'cmd': 'set_trigger',
                       'enable': (not args or args[0] != 'off')}
                if len(args) >= 2:
                    req['mode'] = args[1]
                if len(args) >= 3:
                    req['source'] = args[2]
                self._send(req)
            elif cmd == 'options':
                req = {'cmd': 'set_options'}
                for arg in args:
                    key, _, val = arg.partition('=')
                    req[key] = val.lower() in ('on', 'true', '1', 'yes')
                self._send(req)
            elif cmd == 'calibration_write':
                self._send({'cmd': 'calibration_write', 'channel': int(args[0]),
                            'slope': float(args[1]), 'offset': float(args[2])})
            elif cmd == 'blink':
                self._send({'cmd': 'blink_led',
                            'count': int(args[0]) if args else 1})
            elif cmd == 'test_signals':
                req = {'cmd': 'test_signals_write', 'mode': int(args[0])}
                if len(args) >= 2:
                    req['clock'] = int(args[1])
                if len(args) >= 3:
                    req['sync'] = int(args[2])
                self._send(req)
            else:
                print('[client] unknown command "{}". Type help.'.format(cmd))
        except (IndexError, ValueError) as err:
            print('[client] bad arguments: {}'.format(err))
        return True


def main():
    parser = argparse.ArgumentParser(
        description='Interactive control + raw waveform client for the '
                    'MCC 172 noise monitor.')
    parser.add_argument('--host', required=True,
                        help='Raspberry Pi IP address or hostname.')
    parser.add_argument('--port', type=int, default=5000,
                        help='TCP port (default 5000).')
    parser.add_argument('--out', default=None,
                        help='Immediately record raw samples to this file '
                             '(.f64/.bin raw interleaved, .npy numpy).')
    args = parser.parse_args()

    sock = socket.create_connection((args.host, args.port))
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    print('Connected to {}:{}'.format(args.host, args.port))

    receiver = Receiver(sock)
    receiver.start()
    if args.out:
        # Wait briefly for the handshake so num_channels is known.
        for _ in range(50):
            if receiver.channels:
                break
            threading.Event().wait(0.02)
        receiver.start_recording(args.out)

    shell = CommandShell(sock, receiver)
    try:
        shell.run()
    finally:
        try:
            sock.close()
        except OSError:
            pass


if __name__ == '__main__':
    main()
