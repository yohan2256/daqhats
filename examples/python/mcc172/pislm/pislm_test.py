#!/usr/bin/env python3
#  -*- coding: utf-8 -*-
"""
PiSLM test client -- a simple, dependency-free tool for exercising a PiSLM
node from a laptop.

Connects to both ports (see PROTOCOL.md):
  * control port: sends commands, prints responses/events
  * stream port:  decodes frames and prints a live per-channel level readout

No third-party packages required (stdlib only: socket, json, struct, array),
so it runs on any laptop with Python 3.

Usage:
    python3 pislm_test.py --host 192.168.50.1

Then, at the prompt, type commands (see `help`). A few examples:
    status
    start
    metrics 5
    calibrate 0 94
    save
    sens 0 51.3
    stop
    quit

Anything not recognized as a shorthand is sent as raw JSON, e.g.:
    send {"cmd": "set_weighting", "frequency": "C"}
"""
from __future__ import print_function

import argparse
import json
import socket
import struct
import sys
import threading
from array import array
from time import sleep, time

TYPE_DATA = 0x01
TYPE_MSG = 0x02
TYPE_BAND = 0x03
TYPE_LEVEL = 0x04
TYPE_BAND_LEVEL = 0x05
TYPE_RAW_DUMP = 0x06

TYPE_NAMES = {
    TYPE_DATA: 'DATA', TYPE_MSG: 'MSG', TYPE_BAND: 'BAND',
    TYPE_LEVEL: 'LEVEL', TYPE_BAND_LEVEL: 'BAND_LEVEL',
    TYPE_RAW_DUMP: 'RAW_DUMP',
}

FRAME_HEADER = struct.Struct('<BI')
DATA_HEADER = struct.Struct('<I')
BAND_HEADER = struct.Struct('<II')
LEVEL_HEADER = struct.Struct('<I')
RAW_DUMP_HEADER = struct.Struct('<IIII')


def recv_exact(sock, n):
    """Read exactly n bytes, or return None if the peer closed first."""
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)


def read_frame(sock):
    """Read one typed stream-port frame; returns (type, payload) or
    (None, None) on disconnect."""
    header = recv_exact(sock, FRAME_HEADER.size)
    if header is None:
        return None, None
    frame_type, length = FRAME_HEADER.unpack(header)
    payload = recv_exact(sock, length) if length else b''
    if payload is None:
        return None, None
    return frame_type, payload


# --------------------------------------------------------------------------
# Control port: newline-delimited JSON, both directions
# --------------------------------------------------------------------------
class ControlClient:
    def __init__(self, host, port):
        # timeout=10.0 only bounds the initial TCP handshake, so we fail
        # fast if the host is unreachable. Once connected, the reader
        # thread blocks on recv() indefinitely -- events and responses are
        # irregular, and a bare read timeout must not be mistaken for the
        # connection being closed (see ReaderThread._read_line).
        self.sock = socket.create_connection((host, port), timeout=10.0)
        self.sock.settimeout(None)
        self._buf = b''
        self._id = 0
        self._lock = threading.Lock()

    def _read_line(self):
        while b'\n' not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError('control port closed')
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b'\n')
        return json.loads(line.decode('utf-8'))

    def handshake(self):
        return self._read_line()

    def send(self, cmd_obj):
        """Send a command dict, return the id it was tagged with."""
        with self._lock:
            self._id += 1
            cmd_id = self._id
            cmd_obj = dict(cmd_obj, id=cmd_id)
            self.sock.sendall((json.dumps(cmd_obj) + '\n').encode('utf-8'))
        return cmd_id

    def read_message(self):
        """Read and return one line (response or event); never blocks
        forever if the reader thread owns the socket -- see ReaderThread."""
        return self._read_line()


class ReaderThread(threading.Thread):
    """Owns the control-socket reads: prints events as they arrive and
    hands responses to whoever is waiting for that id."""

    def __init__(self, ctrl):
        super(ReaderThread, self).__init__(daemon=True)
        self.ctrl = ctrl
        self._waiters = {}
        self._lock = threading.Lock()
        self.alive = True

    def wait_for(self, cmd_id, timeout=15.0):
        event = threading.Event()
        box = {}
        with self._lock:
            self._waiters[cmd_id] = (event, box)
        if not event.wait(timeout):
            with self._lock:
                self._waiters.pop(cmd_id, None)
            raise TimeoutError('no response for id={}'.format(cmd_id))
        return box.get('msg')

    def run(self):
        try:
            while True:
                msg = self.ctrl.read_message()
                mtype = msg.get('type')
                if mtype == 'response':
                    cmd_id = msg.get('id')
                    with self._lock:
                        waiter = self._waiters.pop(cmd_id, None)
                    if waiter is not None:
                        event, box = waiter
                        box['msg'] = msg
                        event.set()
                    else:
                        _print_async(fmt_response(msg))
                elif mtype == 'event':
                    _print_async('[event] {}'.format(json.dumps(msg)))
                # handshake-on-reconnect etc.: ignore silently
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            self.alive = False
            with self._lock:
                waiters = list(self._waiters.values())
                self._waiters.clear()
            for event, box in waiters:
                box['msg'] = {'ok': False, 'error': 'connection closed'}
                event.set()
            _print_async('[control] connection closed')


def fmt_response(msg):
    tag = 'ok' if msg.get('ok') else 'ERROR'
    body = msg.get('result') if msg.get('ok') else msg.get('error')
    return '[{}] {}'.format(tag, json.dumps(body))


_print_lock = threading.Lock()


def _print_async(text):
    """Print without clobbering whatever the user is mid-typing."""
    with _print_lock:
        sys.stdout.write('\n' + text + '\n> ')
        sys.stdout.flush()


# --------------------------------------------------------------------------
# Stream port: typed frames -> live per-channel level readout
# --------------------------------------------------------------------------
class StreamReader(threading.Thread):
    def __init__(self, host, port, meter_interval=0.5):
        super(StreamReader, self).__init__(daemon=True)
        # Same reasoning as ControlClient: bound only the initial connect,
        # then block indefinitely -- e.g. while the scan is stopped, no
        # frames arrive at all, and that must not look like a disconnect.
        self.sock = socket.create_connection((host, port), timeout=10.0)
        self.sock.settimeout(None)
        self.meter_interval = meter_interval
        self.show_meter = True
        self.channel_map = {}       # global channel -> device_type/local
        self.units = {}
        self.alive = True
        self._levels = {}           # global channel -> latest dB
        self._last_print = 0.0
        self._dump_files = {}       # dump_id -> {device: file handle}
        self._dump_meta = {}        # dump_id -> response 'devices' info
        # Cumulative bandwidth counters, keyed by frame type; only ever
        # written from this thread (run()), so plain dicts are fine to read
        # from elsewhere without a lock (see Shell._bench).
        self.bytes_by_type = {}
        self.frames_by_type = {}

    def start_dump(self, dump_id, meta_devices, path_template):
        """Register file handles for an in-flight get_raw dump. Call this
        right after sending get_raw, using its response's 'devices' list."""
        files = {}
        for dev in meta_devices:
            path = path_template.format(device=dev['device'])
            files[dev['device']] = open(path, 'wb')
        self._dump_files[dump_id] = files
        self._dump_meta[dump_id] = {d['device']: d for d in meta_devices}

    def _apply_handshake(self, msg):
        for entry in msg.get('channel_map', []):
            self.channel_map[entry['global']] = entry
        self.units.update(msg.get('units', {}))

    def run(self):
        try:
            while True:
                ftype, payload = read_frame(self.sock)
                if ftype is None:
                    break
                frame_len = FRAME_HEADER.size + len(payload)
                self.bytes_by_type[ftype] = (
                    self.bytes_by_type.get(ftype, 0) + frame_len)
                self.frames_by_type[ftype] = (
                    self.frames_by_type.get(ftype, 0) + 1)
                if ftype == TYPE_MSG:
                    self._on_msg(payload)
                elif ftype == TYPE_LEVEL:
                    self._on_level(payload)
                elif ftype == TYPE_BAND_LEVEL:
                    pass    # not shown in the simple meter; see PROTOCOL.md
                elif ftype == TYPE_DATA:
                    pass    # raw waveform; use --dump-raw or get_raw instead
                elif ftype == TYPE_BAND:
                    pass
                elif ftype == TYPE_RAW_DUMP:
                    self._on_raw_dump(payload)
        except (OSError, socket.error):
            pass
        finally:
            self.alive = False
            for files in self._dump_files.values():
                for handle in files.values():
                    handle.close()
            _print_async('[stream] connection closed')

    def _on_msg(self, payload):
        try:
            msg = json.loads(payload.decode('utf-8'))
        except ValueError:
            return
        if msg.get('type') in ('handshake', 'event'):
            self._apply_handshake(msg)
        if msg.get('type') == 'event':
            _print_async('[stream event] {}'.format(json.dumps(msg)))

    def _on_level(self, payload):
        (chan,) = LEVEL_HEADER.unpack(payload[:4])
        values = array('d')
        values.frombytes(payload[4:])
        if values:
            self._levels[chan] = values[-1]
        now = time()
        if self.show_meter and now - self._last_print >= self.meter_interval:
            self._last_print = now
            self._print_meter()

    def _print_meter(self):
        if not self._levels:
            return
        parts = []
        for chan in sorted(self._levels):
            unit = self.units.get(str(chan), '?')
            level_unit = 'dB' if unit == 'Pa' else 'dBV'
            parts.append('ch{}: {:6.1f} {}'.format(
                chan, self._levels[chan], level_unit))
        with _print_lock:
            sys.stdout.write('\r[level] ' + '  '.join(parts) + '   ')
            sys.stdout.flush()

    def _on_raw_dump(self, payload):
        dump_id, dev, chunk_i, is_last = RAW_DUMP_HEADER.unpack(payload[:16])
        files = self._dump_files.get(dump_id)
        if files is None or dev not in files:
            return       # not one we're capturing; ignore
        handle = files[dev]
        handle.write(payload[16:])
        if is_last:
            handle.close()
            del files[dev]
            if not files:
                del self._dump_files[dump_id]
                meta = self._dump_meta.pop(dump_id, {})
                _print_async('[raw] dump {} complete ({} device(s))'.format(
                    dump_id, len(meta)))


# --------------------------------------------------------------------------
# Command shell
# --------------------------------------------------------------------------
HELP = """\
Commands:
  start | stop | status | info | ping
  metrics [seconds] [ch ch ...]        get_metrics (default: 5 s, all channels)
  calibrate <ch> [level_db]            fit a calibrator and apply (default 94 dB)
  calibrate <ch> [level_db] check      same, but measure only (apply=false)
  save                                 save_config (persists calibration)
  sens <ch> <mV_per_Pa>                set_sensitivity (scan must be stopped)
  iepe <ch> on|off                     set_iepe (scan must be stopped)
  rate <hz>                            set_sample_rate (scan must be stopped)
  raw <seconds> <file_prefix>          get_raw, saved to <prefix>_devN.f64
                                        (raw interleaved little-endian float64)
  bench [seconds]                      measure stream bandwidth for N s
                                        (default 10); reports KB/s, Mbps,
                                        frames/s per frame type, plus
                                        dropped-block/frame deltas
  blink [count]                        blink_led
  meter on|off                         toggle the live level readout
  send <raw json>                      send any command verbatim, e.g.
                                        send {"cmd":"set_weighting","frequency":"C"}
  help
  quit
"""


class Shell:
    def __init__(self, ctrl, reader, stream):
        self.ctrl = ctrl
        self.reader = reader
        self.stream = stream

    def _call(self, cmd_obj, timeout=15.0):
        cmd_id = self.ctrl.send(cmd_obj)
        try:
            msg = self.reader.wait_for(cmd_id, timeout=timeout)
        except TimeoutError as err:
            print('[timeout] {}'.format(err))
            return None
        print(fmt_response(msg))
        return msg

    def run(self):
        print(HELP)
        while self.reader.alive:
            try:
                line = input('> ').strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            if not self._dispatch(line):
                break

    def _bench(self, seconds):
        """Measure stream-port throughput over `seconds`, cross-checked
        against the server's device-side (dsp) and network-side (stream
        queue eviction) drop counters -- see PROTOCOL.md section 8."""
        if seconds <= 0:
            print('[bench] seconds must be > 0')
            return
        status0 = self._call({'cmd': 'status'})
        bytes0 = dict(self.stream.bytes_by_type)
        frames0 = dict(self.stream.frames_by_type)
        rtt0 = time()
        self._call({'cmd': 'ping'})
        rtt_ms = (time() - rtt0) * 1000.0
        print('[bench] measuring for {:.1f}s ...'.format(seconds))
        t0 = time()
        sleep(seconds)
        elapsed = time() - t0
        bytes1 = dict(self.stream.bytes_by_type)
        frames1 = dict(self.stream.frames_by_type)
        status1 = self._call({'cmd': 'status'})

        types = sorted(set(bytes0) | set(bytes1))
        total_bytes = sum(bytes1.get(t, 0) - bytes0.get(t, 0) for t in types)
        total_frames = sum(frames1.get(t, 0) - frames0.get(t, 0)
                          for t in types)
        print('[bench] {:.1f}s window, control RTT ~{:.1f} ms'.format(
            elapsed, rtt_ms))
        print('  total: {:8.1f} KB/s  ({:.2f} Mbps), {:.1f} frames/s'.format(
            total_bytes / elapsed / 1024.0,
            total_bytes * 8 / elapsed / 1e6,
            total_frames / elapsed))
        for ftype in types:
            db = bytes1.get(ftype, 0) - bytes0.get(ftype, 0)
            df = frames1.get(ftype, 0) - frames0.get(ftype, 0)
            if db == 0 and df == 0:
                continue
            name = TYPE_NAMES.get(ftype, 'type0x{:02x}'.format(ftype))
            print('  {:<12s}{:8.1f} KB/s  {:7.1f} fps'.format(
                name, db / elapsed / 1024.0, df / elapsed))

        if status0 and status0.get('ok') and status1 and status1.get('ok'):
            r0, r1 = status0['result'], status1['result']
            dsp_dropped = (r1.get('dsp', {}).get('dropped_blocks', 0) -
                          r0.get('dsp', {}).get('dropped_blocks', 0))
            net_dropped = (r1.get('network', {}).get(
                              'stream_frames_dropped', 0) -
                          r0.get('network', {}).get(
                              'stream_frames_dropped', 0))
            print('  dsp dropped_blocks: +{}   '
                  'stream frames dropped: +{}'.format(
                      dsp_dropped, net_dropped))
            if net_dropped:
                print('  [bench] network dropped frames during the window -- '
                      'the link cannot keep up with the current streaming '
                      'mode.')

    def _dispatch(self, line):
        parts = line.split()
        cmd = parts[0].lower()
        args = parts[1:]
        try:
            if cmd in ('quit', 'exit'):
                return False
            if cmd == 'help':
                print(HELP)
            elif cmd in ('start', 'stop', 'status', 'info', 'ping'):
                self._call({'cmd': cmd})
            elif cmd == 'metrics':
                seconds = 5.0
                channels = None
                if args:
                    try:
                        seconds = float(args[0])
                        channels = [int(a) for a in args[1:]] or None
                    except ValueError:
                        channels = [int(a) for a in args]
                req = {'cmd': 'get_metrics', 'seconds': seconds}
                if channels:
                    req['channels'] = channels
                self._call(req)
            elif cmd == 'calibrate':
                if not args:
                    print('usage: calibrate <ch> [level_db] [check]')
                    return True
                chan = int(args[0])
                level_db = 94.0
                apply_ = True
                rest = args[1:]
                if rest and rest[-1].lower() == 'check':
                    apply_ = False
                    rest = rest[:-1]
                if rest:
                    level_db = float(rest[0])
                self._call({'cmd': 'calibrate', 'channel': chan,
                           'level_db': level_db, 'apply': apply_},
                          timeout=20.0)
            elif cmd == 'save':
                self._call({'cmd': 'save_config'})
            elif cmd == 'sens':
                self._call({'cmd': 'set_sensitivity',
                           'channel': int(args[0]), 'value': float(args[1])})
            elif cmd == 'iepe':
                self._call({'cmd': 'set_iepe', 'channel': int(args[0]),
                           'mode': args[1]})
            elif cmd == 'rate':
                self._call({'cmd': 'set_sample_rate',
                           'sample_rate': float(args[0])})
            elif cmd == 'raw':
                if len(args) < 2:
                    print('usage: raw <seconds> <file_prefix>')
                    return True
                seconds, prefix = float(args[0]), args[1]
                msg = self._call({'cmd': 'get_raw', 'seconds': seconds})
                if msg and msg.get('ok'):
                    result = msg['result']
                    self.stream.start_dump(
                        result['dump_id'], result['devices'],
                        prefix + '_dev{device}.f64')
                    print('[raw] writing to {}_dev*.f64 ...'.format(prefix))
            elif cmd == 'bench':
                seconds = float(args[0]) if args else 10.0
                self._bench(seconds)
            elif cmd == 'blink':
                count = int(args[0]) if args else 1
                self._call({'cmd': 'blink_led', 'count': count})
            elif cmd == 'meter':
                self.stream.show_meter = (not args or args[0].lower() != 'off')
                print('[meter] {}'.format(
                    'on' if self.stream.show_meter else 'off'))
            elif cmd == 'send':
                raw = line[len('send'):].strip()
                self._call(json.loads(raw))
            else:
                print('[client] unknown command "{}"; type help'.format(cmd))
        except (IndexError, ValueError) as err:
            print('[client] bad arguments: {}'.format(err))
        return True


def main():
    parser = argparse.ArgumentParser(
        description='Simple test client for a PiSLM node.')
    parser.add_argument('--host', required=True,
                        help='PiSLM IP address or hostname.')
    parser.add_argument('--control-port', type=int, default=5000)
    parser.add_argument('--stream-port', type=int, default=5001)
    args = parser.parse_args()

    print('Connecting to {}:{} (control) and :{} (stream) ...'.format(
        args.host, args.control_port, args.stream_port))

    ctrl = ControlClient(args.host, args.control_port)
    handshake = ctrl.handshake()
    print('Connected. protocol={} running={} channels={}'.format(
        handshake.get('protocol'), handshake.get('running'),
        handshake.get('channels')))

    reader = ReaderThread(ctrl)
    reader.start()

    stream = StreamReader(args.host, args.stream_port)
    stream._apply_handshake(handshake)
    stream.start()

    shell = Shell(ctrl, reader, stream)
    try:
        shell.run()
    finally:
        try:
            ctrl.sock.close()
        except OSError:
            pass
        try:
            stream.sock.close()
        except OSError:
            pass


if __name__ == '__main__':
    main()
