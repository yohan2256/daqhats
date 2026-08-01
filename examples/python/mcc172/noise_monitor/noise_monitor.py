#!/usr/bin/env python3
#  -*- coding: utf-8 -*-
"""
Multi-device noise monitor -- Raspberry Pi side.

Runs on a headless Raspberry Pi and behaves like a networked sound level
meter across up to two IEPE acquisition devices:

    * MCC 172 DAQ HAT (daqhats library)  -- 2 IEPE channels
    * Data Translation DT9837A (uldaq)   -- 4 IEPE channels

for a total of 6 channels, numbered globally in device order (mcc172 first
by default: global 0-1 = MCC 172, global 2-5 = DT9837A). All streaming and
commands use global channel numbers.

NOTE -- the two devices run separate, unsynchronized ADC clocks. Levels and
metrics per channel are unaffected, but cross-channel phase comparisons are
only valid within one device.

It streams Fast/Slow/Impulse time-weighted levels continuously, keeps raw
samples in per-device ring buffers, and computes Leq / Lmax / Lmin / Lpeak /
LN over a window on the "get_metrics" command. Designed to be launched at
boot by systemd (see noise-monitor.service).

Two separate TCP ports (see PROTOCOL.md for the full specification):

  Control port (default 5000) -- newline-delimited UTF-8 JSON, both ways.
      Handshake on connect, then commands / responses / events.

  Streaming port (default 5001) -- typed, length-prefixed binary frames:
          [1-byte type][4-byte little-endian uint32 length][payload]
      type 0x01 DATA : [4-byte device index] then interleaved little-endian
                       float64 samples for that device's channels
                       (channel-fastest within the device).
      type 0x02 MSG  : UTF-8 JSON (handshake on connect, then events).
      type 0x03 BAND : fractional-octave band waveform.
                       [4-byte band index][4-byte GLOBAL channel] + float64.
      type 0x04 LEVEL: broadband time-weighted level in dB.
                       [4-byte GLOBAL channel] + float64 dB samples.
      type 0x05 BAND_LEVEL : per-band time-weighted level in dB.
                       [4-byte band index][4-byte GLOBAL channel] + float64.
      type 0x06 RAW_DUMP : one chunk of an on-demand "get_raw" dump of the
                       RAM ring buffer. [4-byte dump id][4-byte device index]
                       [4-byte chunk index][4-byte is_last] + interleaved
                       float64 (channel-fastest within the device). Chunks
                       are delivered reliably (blocking, not dropped).

Storage note: raw samples live only in RAM (packed float64 ring buffers,
[storage] buffer_seconds each per device). Nothing is written to the SD
card; the laptop records via stream_raw live streaming and/or pulls the
buffered window with get_raw after an event.
"""
from __future__ import print_function

import configparser
import json
import os
import signal
import socket
import struct
import sys
import threading
from array import array
from time import sleep

try:
    import queue
except ImportError:  # pragma: no cover - Python 2 fallback
    import Queue as queue

from devices import open_backends, ChannelMap, Mcc172Backend

PROTOCOL_VERSION = 'noise-monitor/3'

# Downstream frame types.
TYPE_DATA = 0x01        # raw interleaved waveform (per device)
TYPE_MSG = 0x02         # JSON handshake / events (and responses on ctrl port)
TYPE_BAND = 0x03        # decimated fractional-octave band waveform
TYPE_LEVEL = 0x04       # broadband time-weighted level (dB)
TYPE_BAND_LEVEL = 0x05  # per-band time-weighted level (dB)
TYPE_RAW_DUMP = 0x06    # on-demand chunked dump of the buffered raw samples
FRAME_HEADER = struct.Struct('<BI')       # type byte + payload length
DATA_HEADER = struct.Struct('<I')         # device index
BAND_HEADER = struct.Struct('<II')        # band index + global channel
LEVEL_HEADER = struct.Struct('<I')        # global channel
BAND_LEVEL_HEADER = struct.Struct('<II')  # band index + global channel
RAW_DUMP_HEADER = struct.Struct('<IIII')  # dump id, device, chunk idx, is_last

# Interleaved samples per RAW_DUMP chunk (x8 bytes = 512 KiB per frame).
RAW_DUMP_CHUNK = 65536


class CommandError(Exception):
    """Raised for invalid/rejected commands; reported back to the client."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
def _channel_list(text):
    return [int(c) for c in text.split(',') if c.strip() != '']


def load_config(path):
    """Load config.ini into a plain settings dict (the initial state)."""
    parser = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
    if not parser.read(path):
        raise RuntimeError('Could not read configuration file: {}'.format(path))

    device_names = [n.strip() for n in parser.get(
        'devices', 'enabled', fallback='mcc172').split(',') if n.strip()]
    devices = []
    for name in device_names:
        section = name
        channels = None
        if parser.has_option(section, 'channels'):
            channels = _channel_list(parser.get(section, 'channels'))
        devices.append({'type': name, 'channels': channels,
                        'iepe_enable': parser.getboolean(
                            section, 'iepe_enable', fallback=True),
                        'sensitivity': {
                            ch: parser.getfloat(
                                section, 'sensitivity_ch{}'.format(ch),
                                fallback=1000.0)
                            for ch in range(8)}})

    return {
        'devices': devices,
        'sample_rate': parser.getfloat('acquisition', 'sample_rate',
                                       fallback=51200.0),
        'stream_raw': parser.getboolean('acquisition', 'stream_raw',
                                        fallback=False),
        'host': parser.get('network', 'host'),
        'control_port': parser.getint('network', 'control_port'),
        'stream_port': parser.getint('network', 'stream_port'),
        'max_queue_blocks': parser.getint('network', 'max_queue_blocks'),
        'autostart': parser.getboolean('control', 'autostart', fallback=True),
        'bands': {
            'enabled': parser.getboolean('bands', 'enabled', fallback=False),
            'output': parser.get('bands', 'output', fallback='level'),
            'f_min': parser.getfloat('bands', 'f_min', fallback=20.0),
            'f_max': parser.getfloat('bands', 'f_max', fallback=20000.0),
            'fraction': parser.getint('bands', 'fraction', fallback=3),
            'order': parser.getint('bands', 'order', fallback=6),
            'margin': parser.getfloat('bands', 'decimation_margin',
                                      fallback=1.0),
        },
        'weighting': {
            'frequency': parser.get('weighting', 'frequency', fallback='A'),
            'time': parser.get('weighting', 'time', fallback='Fast'),
        },
        'level': {
            'enabled': parser.getboolean('level', 'enabled', fallback=True),
            'output_rate': parser.getfloat('level', 'output_rate',
                                           fallback=10.0),
        },
        'storage': {
            'buffer_seconds': parser.getfloat('storage', 'buffer_seconds',
                                              fallback=60.0),
        },
        'trigger': {
            'enabled': parser.getboolean('trigger', 'sync_start',
                                         fallback=False),
            'source': parser.get('trigger', 'source', fallback='gpio'),
            'gpio_pin': parser.getint('trigger', 'gpio_pin', fallback=17),
            'pulse_ms': parser.getfloat('trigger', 'pulse_ms', fallback=10.0),
        },
    }


# --------------------------------------------------------------------------
# Framing helpers
# --------------------------------------------------------------------------
def build_frame(frame_type, payload):
    return FRAME_HEADER.pack(frame_type, len(payload)) + payload


def data_frame(device_index, payload_bytes):
    """A DATA frame: [device index] then that device's interleaved float64."""
    return build_frame(TYPE_DATA,
                       DATA_HEADER.pack(device_index) + payload_bytes)


def msg_frame(obj):
    return build_frame(TYPE_MSG, json.dumps(obj).encode('utf-8'))


def band_frame(band_index, channel, sample_bytes):
    """A BAND frame: [band_index][global channel], then decimated float64."""
    return build_frame(TYPE_BAND,
                       BAND_HEADER.pack(band_index, channel) + sample_bytes)


def level_frame(channel, sample_bytes):
    """A LEVEL frame: [global channel], then float64 level(dB) samples."""
    return build_frame(TYPE_LEVEL, LEVEL_HEADER.pack(channel) + sample_bytes)


def band_level_frame(band_index, channel, sample_bytes):
    """A BAND_LEVEL frame: [band_index][global channel], then float64 dB."""
    return build_frame(
        TYPE_BAND_LEVEL,
        BAND_LEVEL_HEADER.pack(band_index, channel) + sample_bytes)


def raw_dump_frame(dump_id, device_index, chunk_index, is_last, sample_bytes):
    """A RAW_DUMP frame: one chunk of a get_raw buffer dump."""
    return build_frame(
        TYPE_RAW_DUMP,
        RAW_DUMP_HEADER.pack(dump_id, device_index, chunk_index, is_last) +
        sample_bytes)


# --------------------------------------------------------------------------
# Raw sample ring buffer -- per device; keeps the most recent N seconds so
# the client can request Leq / Lmax / Lmin / LN over a window on demand.
# --------------------------------------------------------------------------
class RawRingBuffer:
    """Rolling store of interleaved raw samples, trimmed to a max duration.

    Blocks are stored as packed ``array('d')`` (8 bytes per sample), so the
    RAM cost is the theoretical minimum -- e.g. 2 ch x 51.2 kHz for 300 s is
    ~246 MB -- and blocks are never mutated after append, so readers can
    snapshot the block list under the lock and copy outside it.
    """

    def __init__(self, max_seconds, num_channels, sample_rate):
        self._num_channels = num_channels
        self._max_interleaved = int(max_seconds * sample_rate) * num_channels
        self._blocks = []            # list of packed array('d') blocks
        self._count = 0              # total interleaved samples held
        self._lock = threading.Lock()

    def append(self, interleaved):
        if not isinstance(interleaved, array):
            interleaved = array('d', interleaved)
        with self._lock:
            self._blocks.append(interleaved)
            self._count += len(interleaved)
            while (self._blocks and
                   self._count - len(self._blocks[0]) >= self._max_interleaved):
                self._count -= len(self._blocks.pop(0))

    def get_recent(self, seconds, sample_rate):
        """Return the most recent ``seconds`` as one packed array('d')."""
        want = int(seconds * sample_rate) * self._num_channels
        with self._lock:
            blocks = list(self._blocks)
        picked = []
        total = 0
        for block in reversed(blocks):
            picked.append(block)
            total += len(block)
            if total >= want:
                break
        picked.reverse()
        data = array('d')
        for block in picked:
            data.extend(block)
        if want and len(data) > want:
            data = data[-want:]
        extra = len(data) % self._num_channels
        return data[extra:] if extra else data


# --------------------------------------------------------------------------
# Client registry -- two client kinds on two ports:
#   'control' : newline-delimited JSON, both directions (commands/responses).
#   'stream'  : typed length-prefixed frames (handshake + DATA + events);
#               anything the client sends on this port is ignored.
# --------------------------------------------------------------------------
class ClientRegistry:
    """Manages connected clients: fan out data/events, deliver replies."""

    def __init__(self, controller, max_queue_blocks):
        self._controller = controller
        self._max_queue_blocks = max_queue_blocks
        self._clients = {}          # conn -> (kind, queue.Queue of bytes)
        self._lock = threading.Lock()

    @staticmethod
    def _encode(kind, obj):
        """Encode a JSON message the way the given client kind expects."""
        if kind == 'stream':
            return msg_frame(obj)
        return (json.dumps(obj) + '\n').encode('utf-8')

    def add(self, conn, addr, kind):
        send_queue = queue.Queue(maxsize=self._max_queue_blocks)
        with self._lock:
            self._clients[conn] = (kind, send_queue)
        threading.Thread(target=self._sender,
                         args=(conn, addr, send_queue), daemon=True).start()
        threading.Thread(target=self._reader,
                         args=(conn, addr, send_queue, kind),
                         daemon=True).start()
        # Greet the new client with the current configuration.
        self._enqueue(send_queue,
                      self._encode(kind, self._controller.handshake()))
        print('[net] {} client connected: {}'.format(kind, addr), flush=True)

    def broadcast_stream_frame(self, frame):
        """Send a prebuilt frame to stream clients, dropping oldest on
        overflow (so a slow client never stalls acquisition)."""
        with self._lock:
            queues = [q for (kind, q) in self._clients.values()
                      if kind == 'stream']
        for send_queue in queues:
            self._enqueue(send_queue, frame, drop_oldest=True)

    def broadcast_message(self, obj):
        """Send a MSG/event to every client, encoded per client kind."""
        with self._lock:
            targets = list(self._clients.values())
        for kind, send_queue in targets:
            self._enqueue(send_queue, self._encode(kind, obj))

    def stream_client_count(self):
        with self._lock:
            return sum(1 for (kind, _q) in self._clients.values()
                       if kind == 'stream')

    def send_stream_reliable(self, frame, timeout=30.0):
        """Send a frame to stream clients WITHOUT dropping it when the queue
        is full: block until there is room (or the per-client timeout runs
        out). Used for get_raw dumps, where every chunk matters."""
        with self._lock:
            queues = [q for (kind, q) in self._clients.values()
                      if kind == 'stream']
        for send_queue in queues:
            try:
                send_queue.put(frame, timeout=timeout)
            except queue.Full:
                pass    # client stalled for the whole timeout; it loses this chunk

    @staticmethod
    def _enqueue(send_queue, frame, drop_oldest=False):
        try:
            send_queue.put_nowait(frame)
        except queue.Full:
            if not drop_oldest:
                return
            try:
                send_queue.get_nowait()
                send_queue.put_nowait(frame)
            except (queue.Empty, queue.Full):
                pass

    def _remove(self, conn):
        with self._lock:
            self._clients.pop(conn, None)

    def _sender(self, conn, addr, send_queue):
        try:
            while True:
                frame = send_queue.get()
                if frame is None:
                    break
                conn.sendall(frame)
        except (OSError, socket.error):
            pass
        finally:
            self._remove(conn)
            try:
                conn.close()
            except OSError:
                pass
            print('[net] client disconnected: {}'.format(addr), flush=True)

    def _reader(self, conn, addr, send_queue, kind):
        """For control clients, read newline-delimited JSON commands and reply
        on their queue. For stream clients, just watch for disconnect."""
        buf = bytearray()
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                if kind != 'control':
                    continue   # ignore any upstream bytes on the stream port
                buf.extend(chunk)
                while b'\n' in buf:
                    line, _, rest = buf.partition(b'\n')
                    buf = bytearray(rest)
                    self._handle_line(line, send_queue)
        except (OSError, socket.error):
            pass
        finally:
            self._remove(conn)
            self._enqueue(send_queue, None)   # unblock the sender

    def _handle_line(self, line, send_queue):
        text = line.decode('utf-8', errors='replace').strip()
        if not text:
            return
        cmd_id = None
        try:
            request = json.loads(text)
            if not isinstance(request, dict):
                raise CommandError('command must be a JSON object')
            cmd_id = request.get('id')
            result = self._controller.dispatch(request)
            reply = {'type': 'response', 'id': cmd_id, 'ok': True,
                     'cmd': request.get('cmd'), 'result': result}
        except CommandError as err:
            reply = {'type': 'response', 'id': cmd_id, 'ok': False,
                     'error': str(err)}
        except (ValueError, KeyError, TypeError) as err:
            reply = {'type': 'response', 'id': cmd_id, 'ok': False,
                     'error': '{}: {}'.format(type(err).__name__, err)}
        except Exception as err:    # noqa: BLE001 - device library errors
            reply = {'type': 'response', 'id': cmd_id, 'ok': False,
                     'error': '{}: {}'.format(type(err).__name__, err)}
        self._enqueue(send_queue, self._encode('control', reply))


# --------------------------------------------------------------------------
# Controller: owns the acquisition devices and all configurable state
# --------------------------------------------------------------------------
class Controller:
    """Serializes access to the devices and executes client commands."""

    def __init__(self, backends, settings):
        self._backends = backends
        self._chan_map = ChannelMap(backends)
        self._lock = threading.RLock()          # guards devices + state
        self._control_lock = threading.Lock()   # serializes start/stop
        self._registry = None

        # Global mutable state, keyed by GLOBAL channel where per-channel.
        self.sample_rate = settings['sample_rate']
        self.stream_raw = settings.get('stream_raw', False)
        self.iepe = {}
        self.sensitivity = {}
        for dev_cfg_idx, dev_cfg in enumerate(settings['devices']):
            if dev_cfg_idx >= len(backends):
                break
            backend = backends[dev_cfg_idx]
            for g in self._chan_map.globals_for_device(dev_cfg_idx):
                _d, local = self._chan_map.resolve(g)
                self.iepe[g] = 1 if dev_cfg.get('iepe_enable', True) else 0
                self.sensitivity[g] = dev_cfg.get('sensitivity', {}).get(
                    local, 1000.0)

        # Synchronized start: arm every device on a shared rising edge.
        # source 'gpio' = the Pi pulses trigger_gpio_pin itself; 'external'
        # = the user supplies the edge and the scans wait for it.
        self.trigger_cfg = dict(settings.get('trigger', {'enabled': False}))
        self._gpio = None           # GpioTrigger, opened lazily
        self._trigger_pending = set()   # dev_idx awaiting first data

        self.band_config = dict(settings.get('bands', {'enabled': False}))
        weighting = settings.get('weighting', {})
        self.freq_weighting = weighting.get('frequency', 'A')
        self.time_weighting = weighting.get('time', 'Fast')
        level = settings.get('level', {})
        self.level_enabled = level.get('enabled', True)
        self.level_rate = level.get('output_rate', 10.0)
        storage = settings.get('storage', {})
        self.buffer_seconds = storage.get('buffer_seconds', 60.0)

        # Runtime state, rebuilt on start(): all keyed by device index or
        # global channel as noted.
        self._raw_buffers = {}      # dev_idx -> RawRingBuffer
        self._band_banks = {}       # dev_idx -> BandFilterBank
        self._wsos = {}             # dev_idx -> weighting SOS (rate-specific)
        self._wzi = {}              # global chan -> filter state
        self._level = {}            # global chan -> ExpLevel
        self._band_level = {}       # (dev_idx, band_index, global) -> ExpLevel
        self._band_offset = {}      # (dev_idx, band_index) -> dB offset

        self._running = False
        self._stop_event = threading.Event()
        self._scan_thread = None
        self._dump_id = 0           # sequence for get_raw dumps

        # Apply initial IEPE + sensitivity so a fresh boot is calibrated.
        self._apply_static_config()

    def attach_registry(self, registry):
        self._registry = registry

    # -- helpers -----------------------------------------------------------
    @property
    def running(self):
        with self._lock:
            return self._running

    @property
    def channels(self):
        return list(range(len(self._chan_map)))

    def _apply_static_config(self):
        with self._lock:
            for dev_idx, backend in enumerate(self._backends):
                iepe_local = {}
                sens_local = {}
                for g in self._chan_map.globals_for_device(dev_idx):
                    _d, local = self._chan_map.resolve(g)
                    iepe_local[local] = self.iepe.get(g, 1)
                    sens_local[local] = self.sensitivity.get(g, 1000.0)
                backend.configure(self.sample_rate, iepe_local, sens_local)

    def _require_stopped(self):
        if self._running:
            raise CommandError('a scan is active; send "stop" first')

    def _resolve(self, global_chan):
        try:
            return self._chan_map.resolve(int(global_chan))
        except ValueError as err:
            raise CommandError(str(err))

    def _units(self):
        return {str(g): ('Pa' if self.sensitivity.get(g, 1000.0) != 1000
                         else 'V') for g in self.channels}

    def _ref_for(self, global_chan):
        return (20e-6 if self.sensitivity.get(global_chan, 1000.0) != 1000
                else 1.0)

    def _mcc(self):
        """First MCC 172 backend, for HAT-specific commands."""
        for backend in self._backends:
            if isinstance(backend, Mcc172Backend):
                return backend
        raise CommandError('no mcc172 device present')

    def config_snapshot(self):
        with self._lock:
            return {
                'running': self._running,
                'channels': self.channels,
                'num_channels': len(self._chan_map),
                'channel_map': self._chan_map.table(self._backends),
                'devices': [{'index': i, 'type': b.name,
                             'channels': self._chan_map.globals_for_device(i),
                             'actual_rate': b.actual_rate}
                            for i, b in enumerate(self._backends)],
                'sample_rate': self.sample_rate,
                'iepe': {str(g): self.iepe.get(g, 0) for g in self.channels},
                'sensitivity_mv_per_unit': {
                    str(g): self.sensitivity.get(g, 1000.0)
                    for g in self.channels},
                'units': self._units(),
                'stream_raw': self.stream_raw,
                'bands': {'enabled': bool(self.band_config.get('enabled')),
                          'output': self.band_config.get('output', 'level'),
                          'fraction': self.band_config.get('fraction', 3),
                          'order': self.band_config.get('order', 6),
                          'f_min': self.band_config.get('f_min', 20.0),
                          'f_max': self.band_config.get('f_max', 20000.0)},
                'weighting': {'frequency': self.freq_weighting,
                              'time': self.time_weighting},
                'level': {'enabled': self.level_enabled,
                          'output_rate': self.level_rate},
                'storage': {'buffer_seconds': self.buffer_seconds},
                'trigger': {
                    'enabled': bool(self.trigger_cfg.get('enabled')),
                    'source': self.trigger_cfg.get('source', 'gpio'),
                    'gpio_pin': self.trigger_cfg.get('gpio_pin', 17),
                    'pulse_ms': self.trigger_cfg.get('pulse_ms', 10.0),
                    'mode': 'RISING_EDGE',
                },
                'clock_sync_note': ('shared-trigger start aligns scan start; '
                                    'ADC clocks still drift (~ppm) between '
                                    'devices'),
            }

    def handshake(self):
        snap = self.config_snapshot()
        snap.update({'type': 'handshake',
                     'protocol': PROTOCOL_VERSION,
                     'dtype': 'float64', 'byte_order': 'little',
                     'interleave': 'channel-fastest-per-device'})
        if self._band_banks:
            snap['band_table'] = [
                dict(self._band_banks[i].metadata(), device=i)
                for i in sorted(self._band_banks)]
        return snap

    # -- streaming lifecycle ----------------------------------------------
    def start(self):
        with self._control_lock:
            with self._lock:
                if self._running:
                    raise CommandError('already running')
                for dev_idx, backend in enumerate(self._backends):
                    iepe_local = {}
                    sens_local = {}
                    for g in self._chan_map.globals_for_device(dev_idx):
                        _d, local = self._chan_map.resolve(g)
                        iepe_local[local] = self.iepe.get(g, 1)
                        sens_local[local] = self.sensitivity.get(g, 1000.0)
                    backend.configure(self.sample_rate, iepe_local, sens_local)

                triggered = bool(self.trigger_cfg.get('enabled'))
                use_gpio = (triggered and
                            self.trigger_cfg.get('source', 'gpio') == 'gpio')
                if use_gpio:
                    # Fail fast (before arming scans) if GPIO is unusable.
                    self._ensure_gpio()

                self._build_processing()

                started = []
                try:
                    for backend in self._backends:
                        if triggered:
                            backend.arm_trigger()
                        backend.start(triggered=triggered)
                        started.append(backend)
                except Exception:
                    for backend in started:
                        backend.stop()
                    raise
                self._running = True
                self._stop_event.clear()
                self._trigger_pending = (set(range(len(self._backends)))
                                         if triggered else set())

            self._scan_thread = threading.Thread(
                target=self._acquire, daemon=True)
            self._scan_thread.start()

            # Broadcast 'started' BEFORE any trigger pulse so it always
            # precedes the first data frames on the stream port.
            snapshot = self.handshake()
            snapshot['type'] = 'event'
            snapshot['event'] = 'started'
            if triggered:
                snapshot['trigger'] = {'armed': True, 'fired': False}
            if self._registry:
                self._registry.broadcast_message(snapshot)

            trigger_result = None
            if triggered:
                if use_gpio:
                    # Give the armed scans a moment to reach the wait state,
                    # then fire one rising edge for all devices.
                    sleep(0.25)
                    self._gpio.pulse(
                        self.trigger_cfg.get('pulse_ms', 10.0) / 1000.0)
                    trigger_result = self._wait_for_trigger(timeout=2.0)
                else:
                    trigger_result = {'armed': True, 'fired': False,
                                      'note': 'waiting for external edge'}

            result = self.config_snapshot()
            if trigger_result is not None:
                result['trigger_start'] = trigger_result
            return result

    def _ensure_gpio(self):
        """Open (or re-open) the trigger GPIO line for the configured pin."""
        from gpio_trigger import GpioTrigger
        pin = int(self.trigger_cfg.get('gpio_pin', 17))
        if self._gpio is not None and self._gpio.pin != pin:
            self._gpio.close()
            self._gpio = None
        if self._gpio is None:
            try:
                self._gpio = GpioTrigger(pin)
            except (RuntimeError, ValueError) as err:
                raise CommandError('trigger GPIO unavailable: {}'.format(err))
        return self._gpio

    def _wait_for_trigger(self, timeout):
        """Poll the devices until they all report the trigger edge."""
        deadline = 0.0
        step = 0.05
        status = {}
        while deadline <= timeout:
            with self._lock:
                status = {i: b.has_triggered()
                          for i, b in enumerate(self._backends)}
            if all(status.values()):
                break
            sleep(step)
            deadline += step
        return {'armed': True, 'fired': all(status.values()),
                'devices': {str(i): bool(v) for i, v in status.items()}}

    def _build_processing(self):
        """(Re)build ring buffers, band banks, weighting filters, and level
        integrators for the devices' current actual rates."""
        self._raw_buffers = {}
        self._band_banks = {}
        self._wsos = {}
        self._wzi = {}
        self._level = {}
        self._band_level = {}
        self._band_offset = {}

        for dev_idx, backend in enumerate(self._backends):
            self._raw_buffers[dev_idx] = RawRingBuffer(
                self.buffer_seconds, backend.num_channels, backend.actual_rate)

        if self.band_config.get('enabled'):
            try:
                from band_filter import BandFilterBank
                for dev_idx, backend in enumerate(self._backends):
                    cfg = self.band_config
                    self._band_banks[dev_idx] = BandFilterBank(
                        backend.actual_rate,
                        self._chan_map.globals_for_device(dev_idx),
                        f_min=cfg.get('f_min', 20.0),
                        f_max=cfg.get('f_max', 20000.0),
                        fraction=cfg.get('fraction', 3),
                        order=cfg.get('order', 6),
                        margin=cfg.get('margin', 1.0))
            except ImportError as err:
                print('[bands] disabled (missing dependency: {})'.format(err),
                      flush=True)

        need_level = self.level_enabled
        need_band_level = (self._band_banks and
                           self.band_config.get('output', 'level') == 'level')
        if not (need_level or need_band_level):
            return
        try:
            import numpy as np
            import slm
        except ImportError as err:
            print('[slm] level output disabled (missing dependency: {})'
                  .format(err), flush=True)
            self.level_enabled = False
            return

        tau = slm.tau_for(self.time_weighting)
        if need_level:
            for dev_idx, backend in enumerate(self._backends):
                self._wsos[dev_idx] = slm.design_weighting_sos(
                    self.freq_weighting, backend.actual_rate)
                n_sec = (self._wsos[dev_idx].shape[0]
                         if self._wsos[dev_idx] is not None else 0)
                for g in self._chan_map.globals_for_device(dev_idx):
                    self._wzi[g] = np.zeros((n_sec, 2))
                    self._level[g] = slm.ExpLevel(
                        backend.actual_rate, tau, self.level_rate,
                        ref=self._ref_for(g))
        if need_band_level:
            for dev_idx, bank in self._band_banks.items():
                for band in bank.bands:
                    self._band_offset[(dev_idx, band['index'])] = \
                        slm.weighting_offset_db(
                            self.freq_weighting, band['center'])
                    for g in self._chan_map.globals_for_device(dev_idx):
                        self._band_level[(dev_idx, band['index'], g)] = \
                            slm.ExpLevel(band['decimated_rate'], tau,
                                         self.level_rate,
                                         ref=self._ref_for(g))

    def stop(self):
        with self._control_lock:
            with self._lock:
                if not self._running:
                    raise CommandError('not running')
            self._stop_event.set()
            thread = self._scan_thread
        if thread is not None:
            thread.join(timeout=5.0)
        return {'running': self.running}

    def _acquire(self):
        """Poll every device for new samples; store, process, broadcast."""
        try:
            while not self._stop_event.is_set():
                got_any = False
                for dev_idx, backend in enumerate(self._backends):
                    with self._lock:
                        data, overrun = backend.read_new()
                    if overrun:
                        print('[scan] overrun on device {} ({})'.format(
                            dev_idx, backend.name), flush=True)
                        if self._registry:
                            self._registry.broadcast_message(
                                {'type': 'event', 'event': 'overrun',
                                 'device': dev_idx, 'kind': 'buffer'})
                        return
                    if not data:
                        continue
                    got_any = True
                    if dev_idx in self._trigger_pending:
                        # First samples after an armed start: edge arrived.
                        self._trigger_pending.discard(dev_idx)
                        if self._registry:
                            self._registry.broadcast_message(
                                {'type': 'event', 'event': 'triggered',
                                 'device': dev_idx})
                    # Pack once; the buffer, DATA frame, and DSP all share it.
                    block = array('d', data)
                    self._raw_buffers[dev_idx].append(block)
                    if not self._registry:
                        continue
                    if self.stream_raw:
                        self._registry.broadcast_stream_frame(data_frame(
                            dev_idx, block.tobytes()))
                    if self.level_enabled and self._level:
                        self._emit_levels(dev_idx, backend, block)
                    bank = self._band_banks.get(dev_idx)
                    if bank is not None:
                        if self.band_config.get('output', 'level') == \
                                'waveform':
                            self._emit_bands(bank, block)
                        else:
                            self._emit_band_levels(dev_idx, bank, block)
                if not got_any:
                    sleep(0.002)
        finally:
            with self._lock:
                for backend in self._backends:
                    try:
                        backend.stop()
                    except Exception:   # noqa: BLE001
                        pass
                self._running = False
            print('[scan] stopped', flush=True)
            if self._registry:
                self._registry.broadcast_message(
                    {'type': 'event', 'event': 'stopped'})

    def _emit_bands(self, bank, raw_data):
        """BAND (waveform) frames; bank yields GLOBAL channel labels."""
        for band_index, g_chan, samples in bank.process(raw_data):
            self._registry.broadcast_stream_frame(band_frame(
                band_index, g_chan, samples.astype('<f8').tobytes()))

    def _emit_levels(self, dev_idx, backend, raw_data):
        """Broadband weighted level (dB) LEVEL frames for one device block."""
        import numpy as np
        from scipy import signal
        data = np.asarray(raw_data, dtype=np.float64).reshape(
            -1, backend.num_channels)
        sos = self._wsos.get(dev_idx)
        for ci, g_chan in enumerate(
                self._chan_map.globals_for_device(dev_idx)):
            x = data[:, ci]
            if sos is not None:
                x, self._wzi[g_chan] = signal.sosfilt(
                    sos, x, zi=self._wzi[g_chan])
            levels = self._level[g_chan].process(x)
            if levels.size:
                self._registry.broadcast_stream_frame(level_frame(
                    g_chan, levels.astype('<f8').tobytes()))

    def _emit_band_levels(self, dev_idx, bank, raw_data):
        """Per-band weighted level (dB) BAND_LEVEL frames for one block."""
        for band_index, g_chan, samples in bank.process(raw_data):
            integrator = self._band_level.get((dev_idx, band_index, g_chan))
            if integrator is None:
                continue
            levels = integrator.process(samples)
            if levels.size:
                levels = levels + self._band_offset.get(
                    (dev_idx, band_index), 0.0)
                self._registry.broadcast_stream_frame(band_level_frame(
                    band_index, g_chan, levels.astype('<f8').tobytes()))

    # -- command dispatch --------------------------------------------------
    def dispatch(self, request):
        cmd = request.get('cmd')
        if not cmd:
            raise CommandError('missing "cmd"')
        handler = self._HANDLERS.get(cmd)
        if handler is None:
            raise CommandError('unknown command: {}'.format(cmd))
        # start/stop manage their own locking (they join the scan thread,
        # which itself needs the device lock). get_raw/get_metrics only read
        # the ring buffers (own lock) and run-frozen config, and can take
        # seconds on large windows -- run them without the device lock so
        # they never stall acquisition. Everything else runs under it.
        if cmd in ('start', 'stop', 'get_raw', 'get_metrics'):
            return handler(self, request)
        with self._lock:
            return handler(self, request)

    # ---- query handlers (allowed anytime) ----
    def _cmd_ping(self, _req):
        return {'pong': True}

    def _cmd_get_config(self, _req):
        return self.config_snapshot()

    def _cmd_status(self, _req):
        armed = bool(self.trigger_cfg.get('enabled')) and self._running
        return {'running': self._running,
                'devices': [{'index': i, 'type': b.name,
                             'running': b.running,
                             'actual_rate': b.actual_rate,
                             'triggered': (b.has_triggered()
                                           if armed else None)}
                            for i, b in enumerate(self._backends)]}

    def _cmd_info(self, _req):
        return {'devices': [dict(b.info(), index=i,
                                 channels=self._chan_map.globals_for_device(i))
                            for i, b in enumerate(self._backends)],
                'channel_map': self._chan_map.table(self._backends),
                'num_channels': len(self._chan_map)}

    def _cmd_get_sensitivity(self, req):
        g = int(req['channel'])
        self._resolve(g)
        return {'channel': g,
                'sensitivity': self.sensitivity.get(g, 1000.0)}

    def _cmd_get_iepe(self, req):
        g = int(req['channel'])
        self._resolve(g)
        return {'channel': g, 'mode': self.iepe.get(g, 0)}

    def _cmd_get_clock(self, _req):
        return {'devices': [{'index': i, 'type': b.name,
                             'actual_rate': b.actual_rate}
                            for i, b in enumerate(self._backends)],
                'requested_rate': self.sample_rate,
                'synchronized': False}

    def _cmd_calibration_read(self, req):
        g = int(req['channel'])
        dev_idx, local = self._resolve(g)
        backend = self._backends[dev_idx]
        if not isinstance(backend, Mcc172Backend):
            raise CommandError('calibration_read is mcc172-only')
        slope, offset = backend.calibration_read(local)
        return {'channel': g, 'slope': slope, 'offset': offset}

    def _cmd_blink_led(self, req):
        count = int(req.get('count', 1))
        blinked = []
        want = req.get('device')
        for i, backend in enumerate(self._backends):
            if want is not None and int(want) != i:
                continue
            try:
                backend.blink(count)
                blinked.append(i)
            except Exception:   # noqa: BLE001 - not all devices support it
                pass
        return {'count': count, 'devices': blinked}

    # ---- streaming ----
    def _cmd_start(self, _req):
        return self.start()

    def _cmd_stop(self, _req):
        return self.stop()

    # ---- configuration handlers (require the scan to be stopped) ----
    def _cmd_set_sensitivity(self, req):
        self._require_stopped()
        g = int(req['channel'])
        dev_idx, local = self._resolve(g)
        value = float(req['value'])
        self._backends[dev_idx].set_sensitivity(local, value)
        self.sensitivity[g] = value
        return {'channel': g, 'sensitivity': value,
                'units': self._units().get(str(g))}

    def _cmd_set_iepe(self, req):
        self._require_stopped()
        g = int(req['channel'])
        dev_idx, local = self._resolve(g)
        mode = 1 if req.get('mode') in (1, '1', True, 'true', 'on') else 0
        self._backends[dev_idx].set_iepe(local, mode)
        self.iepe[g] = mode
        return {'channel': g, 'mode': mode}

    def _cmd_set_sample_rate(self, req):
        self._require_stopped()
        rate = float(req['sample_rate'])
        self.sample_rate = rate
        self._apply_static_config()   # each device recomputes its actual rate
        self._raw_buffers = {}        # buffered raw no longer matches
        return {'requested_rate': rate,
                'devices': [{'index': i, 'type': b.name,
                             'actual_rate': b.actual_rate}
                            for i, b in enumerate(self._backends)]}

    def _cmd_set_channels(self, req):
        self._require_stopped()
        dev_idx = int(req.get('device', 0))
        if not 0 <= dev_idx < len(self._backends):
            raise CommandError('device must be 0..{}'.format(
                len(self._backends) - 1))
        channels = req.get('channels')
        if not isinstance(channels, list) or not channels:
            raise CommandError('"channels" must be a non-empty list')
        backend = self._backends[dev_idx]
        old = backend.channels
        backend.channels = sorted(set(int(c) for c in channels))
        try:
            if backend.name == 'dt9837a' and \
                    backend.channels != list(range(len(backend.channels))):
                raise CommandError(
                    'dt9837a channels must be contiguous from 0')
            backend.num_channels = len(backend.channels)
        except CommandError:
            backend.channels = old
            backend.num_channels = len(old)
            raise
        # Global numbering changes: rebuild the map and per-channel state.
        self._chan_map = ChannelMap(self._backends)
        self.iepe = {g: self.iepe.get(g, 1) for g in self.channels}
        self.sensitivity = {g: self.sensitivity.get(g, 1000.0)
                            for g in self.channels}
        self._raw_buffers = {}
        return {'device': dev_idx, 'channels': backend.channels,
                'channel_map': self._chan_map.table(self._backends)}

    def _cmd_set_trigger(self, req):
        """Configure the synchronized (shared rising-edge) start.

        Fields: enable (bool); source 'gpio' (the Pi pulses gpio_pin on
        start) or 'external' (the scans arm and wait for a user-supplied
        edge); gpio_pin; pulse_ms. Applies from the next start.
        """
        from gpio_trigger import MCC172_RESERVED_PINS
        self._require_stopped()
        cfg = self.trigger_cfg
        enable = req.get('enable', True)
        cfg['enabled'] = bool(enable) and enable not in ('false', 'off', 0)
        if 'source' in req:
            source = str(req['source']).lower()
            if source not in ('gpio', 'external'):
                raise CommandError("source must be 'gpio' or 'external'")
            cfg['source'] = source
        if 'gpio_pin' in req:
            pin = int(req['gpio_pin'])
            if pin in MCC172_RESERVED_PINS:
                raise CommandError(
                    'GPIO {} is used by the MCC 172 HAT; choose another '
                    'pin (e.g. 17, 27, 22)'.format(pin))
            if not 0 <= pin <= 27:
                raise CommandError('gpio_pin must be a BCM number 0..27')
            cfg['gpio_pin'] = pin
        if 'pulse_ms' in req:
            pulse = float(req['pulse_ms'])
            if pulse <= 0:
                raise CommandError('pulse_ms must be > 0')
            cfg['pulse_ms'] = pulse
        return {'enabled': cfg['enabled'],
                'source': cfg.get('source', 'gpio'),
                'gpio_pin': cfg.get('gpio_pin', 17),
                'pulse_ms': cfg.get('pulse_ms', 10.0),
                'mode': 'RISING_EDGE',
                'note': 'rising edge only (DT9837A external trigger '
                        'supports rising edges only)'}

    def _cmd_set_options(self, req):
        self._require_stopped()
        if 'stream_raw' in req:
            self.stream_raw = bool(req['stream_raw'])
        return {'stream_raw': self.stream_raw}

    def _cmd_set_bands(self, req):
        self._require_stopped()
        cfg = self.band_config
        if 'enabled' in req:
            cfg['enabled'] = bool(req['enabled'])
        if 'output' in req:
            if req['output'] not in ('level', 'waveform'):
                raise CommandError("band output must be 'level' or 'waveform'")
            cfg['output'] = req['output']
        for key in ('f_min', 'f_max', 'margin'):
            if key in req:
                cfg[key] = float(req[key])
        for key in ('fraction', 'order'):
            if key in req:
                cfg[key] = int(req[key])
        table = None
        if cfg.get('enabled'):
            try:
                from band_filter import BandFilterBank
            except ImportError as err:
                raise CommandError(
                    'band output needs numpy + scipy on the Pi ({})'.format(
                        err))
            table = []
            for dev_idx, backend in enumerate(self._backends):
                bank = BandFilterBank(
                    backend.actual_rate,
                    self._chan_map.globals_for_device(dev_idx),
                    f_min=cfg.get('f_min', 20.0),
                    f_max=cfg.get('f_max', 20000.0),
                    fraction=cfg.get('fraction', 3),
                    order=cfg.get('order', 6),
                    margin=cfg.get('margin', 1.0))
                table.append(dict(bank.metadata(), device=dev_idx))
        return {'enabled': bool(cfg.get('enabled')),
                'output': cfg.get('output', 'level'),
                'fraction': cfg.get('fraction', 3),
                'order': cfg.get('order', 6),
                'f_min': cfg.get('f_min', 20.0),
                'f_max': cfg.get('f_max', 20000.0),
                'band_table': table}

    def _cmd_set_weighting(self, req):
        self._require_stopped()
        if 'frequency' in req:
            freq = str(req['frequency']).upper()
            if freq not in ('A', 'C', 'Z'):
                raise CommandError("frequency weighting must be A, C, or Z")
            self.freq_weighting = freq
        if 'time' in req:
            tw = str(req['time']).capitalize()
            if tw not in ('Fast', 'Slow', 'Impulse'):
                raise CommandError("time weighting must be Fast, Slow, Impulse")
            self.time_weighting = tw
        return {'frequency': self.freq_weighting, 'time': self.time_weighting}

    def _cmd_set_level(self, req):
        self._require_stopped()
        if 'enabled' in req:
            self.level_enabled = bool(req['enabled'])
        if 'output_rate' in req:
            rate = float(req['output_rate'])
            if rate <= 0:
                raise CommandError('output_rate must be > 0')
            self.level_rate = rate
        return {'enabled': self.level_enabled, 'output_rate': self.level_rate}

    def _cmd_set_storage(self, req):
        self._require_stopped()
        if 'buffer_seconds' in req:
            seconds = float(req['buffer_seconds'])
            if seconds <= 0:
                raise CommandError('buffer_seconds must be > 0')
            self.buffer_seconds = seconds
            self._raw_buffers = {}
        return {'buffer_seconds': self.buffer_seconds}

    def _cmd_get_raw(self, req):
        """Dump the most recent buffered raw samples to the stream clients as
        chunked RAW_DUMP frames. The copy and the send run outside the device
        lock so a large dump can never stall acquisition."""
        if not self._raw_buffers:
            raise CommandError('no data buffered yet; start a scan first')
        if self._registry is None or \
                self._registry.stream_client_count() == 0:
            raise CommandError(
                'no stream client connected to receive the dump')
        seconds = float(req.get('seconds', self.buffer_seconds))
        if seconds <= 0:
            raise CommandError('seconds must be > 0')
        want_devices = req.get('devices')
        if want_devices is not None:
            want_devices = [int(d) for d in want_devices]

        self._dump_id += 1
        dump_id = self._dump_id
        plan = []
        info = []
        for dev_idx, backend in enumerate(self._backends):
            if want_devices is not None and dev_idx not in want_devices:
                continue
            buffer_ = self._raw_buffers.get(dev_idx)
            if buffer_ is None:
                continue
            data = buffer_.get_recent(seconds, backend.actual_rate)
            nch = backend.num_channels
            if len(data) < nch:
                continue
            total_chunks = (len(data) + RAW_DUMP_CHUNK - 1) // RAW_DUMP_CHUNK
            plan.append((dev_idx, data, total_chunks))
            info.append({
                'device': dev_idx,
                'channels': self._chan_map.globals_for_device(dev_idx),
                'num_channels': nch,
                'sample_rate': backend.actual_rate,
                'samples_per_channel': len(data) // nch,
                'seconds': round(len(data) / nch / backend.actual_rate, 3),
                'total_chunks': total_chunks,
            })
        if not plan:
            raise CommandError('not enough data buffered')

        registry = self._registry

        def send_dump():
            for dev_idx, dump_data, total_chunks in plan:
                for i in range(total_chunks):
                    chunk = dump_data[i * RAW_DUMP_CHUNK:
                                      (i + 1) * RAW_DUMP_CHUNK]
                    registry.send_stream_reliable(raw_dump_frame(
                        dump_id, dev_idx, i,
                        1 if i == total_chunks - 1 else 0, chunk.tobytes()))

        threading.Thread(target=send_dump, daemon=True).start()
        return {'dump_id': dump_id, 'chunk_samples': RAW_DUMP_CHUNK,
                'units': self._units(), 'devices': info}

    def _cmd_get_metrics(self, req):
        """SLM metrics over the buffered raw data, per global channel."""
        if not self._raw_buffers:
            raise CommandError('no data buffered yet; start a scan first')
        try:
            import numpy as np
            import slm
        except ImportError as err:
            raise CommandError(
                'metrics need numpy + scipy on the Pi ({})'.format(err))

        seconds = float(req.get('seconds', self.buffer_seconds))
        weighting = req.get('weighting', self.freq_weighting)
        time_w = req.get('time_weighting', self.time_weighting)
        pct = req.get('percentiles', [10, 50, 90])
        want = req.get('channels', self.channels)

        results = {}
        for g in want:
            g = int(g)
            try:
                dev_idx, local = self._chan_map.resolve(g)
            except ValueError:
                continue
            backend = self._backends[dev_idx]
            buffer_ = self._raw_buffers.get(dev_idx)
            if buffer_ is None:
                continue
            flat = buffer_.get_recent(seconds, backend.actual_rate)
            nch = backend.num_channels
            if len(flat) < nch:
                continue
            data = np.asarray(flat, dtype=np.float64).reshape(-1, nch)
            ci = backend.channels.index(local)
            metrics = slm.window_metrics(
                data[:, ci], backend.actual_rate, weighting=weighting,
                time_weighting=time_w, ref=self._ref_for(g),
                percentiles=pct)
            metrics['units'] = self._units().get(str(g))
            metrics['calibrated'] = self.sensitivity.get(g, 1000.0) != 1000
            metrics['device'] = dev_idx
            if req.get('include_bands') and self.band_config.get('enabled'):
                metrics['bands'] = self._band_metrics(
                    data[:, ci], backend.actual_rate, g, weighting)
            results[str(g)] = metrics

        if not results:
            raise CommandError('not enough data buffered')
        return {'requested_seconds': seconds, 'channels': results}

    def _band_metrics(self, x, rate, g_chan, weighting):
        """Per-band Leq (with A/C offset) over a window, computed on demand."""
        import math
        import numpy as np
        import slm
        from band_filter import BandFilterBank
        cfg = self.band_config
        bank = BandFilterBank(
            rate, [g_chan],
            f_min=cfg.get('f_min', 20.0), f_max=cfg.get('f_max', 20000.0),
            fraction=cfg.get('fraction', 3), order=cfg.get('order', 6),
            margin=cfg.get('margin', 1.0))
        segments = {}
        for band_index, _c, samples in bank.process(list(x)):
            segments.setdefault(band_index, []).append(samples)
        ref2 = self._ref_for(g_chan) ** 2
        out = []
        for band in bank.bands:
            segs = segments.get(band['index'])
            if not segs:
                continue
            sig = np.concatenate(segs)
            leq = 10.0 * math.log10(max(np.mean(sig * sig), 1e-30) / ref2)
            leq += slm.weighting_offset_db(weighting, band['center'])
            out.append({'index': band['index'],
                        'center': round(band['center'], 2),
                        'Leq': round(leq, 2)})
        return out

    def _cmd_calibration_write(self, req):
        self._require_stopped()
        g = int(req['channel'])
        dev_idx, local = self._resolve(g)
        backend = self._backends[dev_idx]
        if not isinstance(backend, Mcc172Backend):
            raise CommandError('calibration_write is mcc172-only')
        slope = float(req['slope'])
        offset = float(req['offset'])
        backend.calibration_write(local, slope, offset)
        return {'channel': g, 'slope': slope, 'offset': offset}

    def _cmd_test_signals_write(self, req):
        self._require_stopped()
        mode = int(req['mode'])
        clock = int(req.get('clock', 0))
        sync = int(req.get('sync', 0))
        self._mcc().test_signals_write(mode, clock, sync)
        return {'mode': mode, 'clock': clock, 'sync': sync}

    _HANDLERS = {
        'ping': _cmd_ping,
        'get_config': _cmd_get_config,
        'status': _cmd_status,
        'info': _cmd_info,
        'get_sensitivity': _cmd_get_sensitivity,
        'get_iepe': _cmd_get_iepe,
        'get_clock': _cmd_get_clock,
        'calibration_read': _cmd_calibration_read,
        'blink_led': _cmd_blink_led,
        'start': _cmd_start,
        'stop': _cmd_stop,
        'set_sensitivity': _cmd_set_sensitivity,
        'set_iepe': _cmd_set_iepe,
        'set_sample_rate': _cmd_set_sample_rate,
        'set_channels': _cmd_set_channels,
        'set_trigger': _cmd_set_trigger,
        'set_options': _cmd_set_options,
        'set_bands': _cmd_set_bands,
        'set_weighting': _cmd_set_weighting,
        'set_level': _cmd_set_level,
        'set_storage': _cmd_set_storage,
        'get_metrics': _cmd_get_metrics,
        'get_raw': _cmd_get_raw,
        'calibration_write': _cmd_calibration_write,
        'test_signals_write': _cmd_test_signals_write,
    }


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main():
    config_path = os.environ.get(
        'NOISE_MONITOR_CONFIG',
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.ini'))
    settings = load_config(config_path)

    backends, errors = open_backends(settings['devices'])
    for name, err in errors.items():
        print('[dev] {}: {}'.format(name, err), flush=True)
    if not backends:
        raise RuntimeError('no acquisition devices available: {}'.format(
            errors))
    for i, backend in enumerate(backends):
        print('[dev] {}: {} ({} ch)'.format(i, backend.name,
                                            backend.num_channels), flush=True)

    controller = Controller(backends, settings)
    registry = ClientRegistry(controller, settings['max_queue_blocks'])
    controller.attach_registry(registry)

    stop_event = threading.Event()

    def make_server(port):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((settings['host'], port))
        srv.listen(5)
        srv.settimeout(1.0)
        return srv

    def accept_loop(srv, kind):
        while not stop_event.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            registry.add(conn, addr, kind)

    control_server = make_server(settings['control_port'])
    stream_server = make_server(settings['stream_port'])
    print('[net] control port {}:{}, stream port {}:{}'.format(
        settings['host'], settings['control_port'],
        settings['host'], settings['stream_port']), flush=True)

    def handle_signal(_signum, _frame):
        stop_event.set()
    try:
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
    except ValueError:
        # Not running in the main thread (e.g. under a test harness); the
        # systemd service always runs in the main thread and gets signals.
        pass

    if settings['autostart']:
        try:
            controller.start()
            print('[scan] autostart: streaming', flush=True)
        except Exception as err:    # noqa: BLE001 - report and stay up
            print('[scan] autostart failed: {}'.format(err), flush=True)

    stream_thread = threading.Thread(
        target=accept_loop, args=(stream_server, 'stream'), daemon=True)
    stream_thread.start()
    try:
        accept_loop(control_server, 'control')
    finally:
        stop_event.set()
        try:
            if controller.running:
                controller.stop()
        except Exception:   # noqa: BLE001
            pass
        for srv in (control_server, stream_server):
            try:
                srv.close()
            except OSError:
                pass
        for backend in backends:
            try:
                backend.close()
            except Exception:   # noqa: BLE001
                pass
        if controller._gpio is not None:
            controller._gpio.close()


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError) as err:
        print('\nError: {}'.format(err), file=sys.stderr)
        sys.exit(1)
