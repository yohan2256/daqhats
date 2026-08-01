#!/usr/bin/env python3
#  -*- coding: utf-8 -*-
"""
MCC 172 Noise Monitor -- Raspberry Pi side (bidirectional control).

Runs on a headless Raspberry Pi with an MCC 172 DAQ HAT. It acquires the
raw waveform from an IEPE microphone/accelerometer and serves it over TCP,
while also accepting control commands so a laptop can drive every
configurable feature of the MCC 172 (sensitivity, sample rate, IEPE power,
trigger, calibration, LED, test signals, ...) and start/stop streaming on
demand. Designed to be launched automatically at boot by systemd (see
noise-monitor.service).

Two separate TCP ports (see PROTOCOL.md for the full specification):

  Control port (default 5000) -- newline-delimited UTF-8 JSON, both ways.
      On connect the server sends a handshake JSON line describing the
      current configuration. The client then sends command lines, e.g.
          {"id": 4, "cmd": "set_sensitivity", "channel": 0, "value": 50}
      and receives response lines and event lines. The optional "id" is
      echoed back so replies can be matched.

  Streaming port (default 5001) -- typed, length-prefixed binary frames:
          [1-byte type][4-byte little-endian uint32 length][payload]
      type 0x01 DATA : payload = interleaved little-endian float64 samples,
                       channel-fastest: ch0[n], ch1[n], ch0[n+1], ...
      type 0x02 MSG  : payload = UTF-8 JSON (handshake on connect, then
                       events). Upstream bytes on this port are ignored.
      type 0x03 BAND : optional fractional-octave band WAVEFORM output.
                       payload = [4-byte band index][4-byte channel] then
                       decimated little-endian float64 samples for that band.
                       The handshake's "band_table" maps index -> center
                       frequency and decimated rate.
      type 0x04 LEVEL: broadband time-weighted level (Fast/Slow/Impulse) in
                       dB. payload = [4-byte channel] then float64 dB samples
                       at the level output rate.
      type 0x05 BAND_LEVEL : per-band time-weighted level in dB (with the
                       A/C frequency-weighting offset applied per band).
                       payload = [4-byte band index][4-byte channel] then
                       float64 dB samples.

This behaves like a sound level meter: it streams Fast time-weighted levels
continuously (light), keeps recent raw samples in a ring buffer, and computes
Leq / Lmax / Lmin / Lpeak / LN over a window on the "get_metrics" command.
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

from daqhats import (mcc172, OptionFlags, SourceType, TriggerModes, HatIDs,
                     HatError, hat_list)

READ_ALL_AVAILABLE = -1

# Downstream frame types.
TYPE_DATA = 0x01        # raw interleaved waveform
TYPE_MSG = 0x02         # JSON handshake / events (and responses on ctrl port)
TYPE_BAND = 0x03        # decimated fractional-octave band waveform
TYPE_LEVEL = 0x04       # broadband time-weighted level (dB)
TYPE_BAND_LEVEL = 0x05  # per-band time-weighted level (dB)
FRAME_HEADER = struct.Struct('<BI')    # type byte + payload length
BAND_HEADER = struct.Struct('<II')     # band index + channel
LEVEL_HEADER = struct.Struct('<I')     # channel
BAND_LEVEL_HEADER = struct.Struct('<II')  # band index + channel

# Name <-> value maps so the laptop can use readable strings in commands.
SOURCE_TYPES = {'LOCAL': SourceType.LOCAL, 'MASTER': SourceType.MASTER,
                'SLAVE': SourceType.SLAVE}
TRIGGER_MODES = {'RISING_EDGE': TriggerModes.RISING_EDGE,
                 'FALLING_EDGE': TriggerModes.FALLING_EDGE,
                 'ACTIVE_HIGH': TriggerModes.ACTIVE_HIGH,
                 'ACTIVE_LOW': TriggerModes.ACTIVE_LOW}


class CommandError(Exception):
    """Raised for invalid/rejected commands; reported back to the client."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
def load_config(path):
    """Load config.ini into a plain settings dict (the initial device state)."""
    parser = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
    if not parser.read(path):
        raise RuntimeError('Could not read configuration file: {}'.format(path))

    channels = [int(c) for c in parser.get('acquisition', 'channels').split(',')
                if c.strip() != '']
    if not channels or any(c not in (0, 1) for c in channels):
        raise ValueError('channels must be a subset of 0, 1')

    return {
        'channels': channels,
        'sample_rate': parser.getfloat('acquisition', 'sample_rate'),
        'iepe_enable': parser.getboolean('acquisition', 'iepe_enable'),
        'stream_raw': parser.getboolean('acquisition', 'stream_raw',
                                        fallback=False),
        'sensitivity': {
            0: parser.getfloat('calibration', 'sensitivity_ch0'),
            1: parser.getfloat('calibration', 'sensitivity_ch1'),
        },
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
    }


def channels_to_mask(channels):
    """Convert a list of channel numbers to an MCC 172 channel mask."""
    mask = 0
    for chan in channels:
        mask |= 0x01 << chan
    return mask


# --------------------------------------------------------------------------
# Framing helpers
# --------------------------------------------------------------------------
def build_frame(frame_type, payload):
    return FRAME_HEADER.pack(frame_type, len(payload)) + payload


def data_frame(payload_bytes):
    return build_frame(TYPE_DATA, payload_bytes)


def msg_frame(obj):
    return build_frame(TYPE_MSG, json.dumps(obj).encode('utf-8'))


def band_frame(band_index, channel, sample_bytes):
    """A BAND frame: [band_index][channel] header, then decimated float64."""
    return build_frame(TYPE_BAND,
                       BAND_HEADER.pack(band_index, channel) + sample_bytes)


def level_frame(channel, sample_bytes):
    """A LEVEL frame: [channel] header, then float64 level(dB) samples."""
    return build_frame(TYPE_LEVEL, LEVEL_HEADER.pack(channel) + sample_bytes)


def band_level_frame(band_index, channel, sample_bytes):
    """A BAND_LEVEL frame: [band_index][channel] header, then float64 dB."""
    return build_frame(
        TYPE_BAND_LEVEL,
        BAND_LEVEL_HEADER.pack(band_index, channel) + sample_bytes)


# --------------------------------------------------------------------------
# Raw sample ring buffer -- keeps the most recent N seconds so the client can
# request Leq / Lmax / Lmin / LN over a window on demand (like an SLM).
# --------------------------------------------------------------------------
class RawRingBuffer:
    """Rolling store of interleaved raw samples, trimmed to a max duration."""

    def __init__(self, max_seconds, num_channels, sample_rate):
        self._num_channels = num_channels
        self._max_interleaved = int(max_seconds * sample_rate) * num_channels
        self._blocks = []            # list of interleaved sample lists/arrays
        self._count = 0              # total interleaved samples held
        self._lock = threading.Lock()

    def append(self, interleaved):
        with self._lock:
            self._blocks.append(interleaved)
            self._count += len(interleaved)
            while (self._blocks and
                   self._count - len(self._blocks[0]) >= self._max_interleaved):
                self._count -= len(self._blocks.pop(0))

    def get_recent(self, seconds, sample_rate):
        """Return the most recent ``seconds`` as a flat interleaved list."""
        want = int(seconds * sample_rate) * self._num_channels
        with self._lock:
            blocks = list(self._blocks)
        flat = []
        for block in reversed(blocks):
            flat.append(block)
            if sum(len(b) for b in flat) >= want:
                break
        flat.reverse()
        data = []
        for block in flat:
            data.extend(block)
        if want and len(data) > want:
            data = data[-want:]
        # Trim to a whole number of frames.
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

    def broadcast_data(self, payload_bytes):
        """Send a DATA frame to stream clients."""
        self.broadcast_stream_frame(data_frame(payload_bytes))

    def broadcast_message(self, obj):
        """Send a MSG/event to every client, encoded per client kind."""
        with self._lock:
            targets = list(self._clients.values())
        for kind, send_queue in targets:
            self._enqueue(send_queue, self._encode(kind, obj))

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
        except HatError as err:
            reply = {'type': 'response', 'id': cmd_id, 'ok': False,
                     'error': 'HatError: {}'.format(err)}
        except (ValueError, KeyError, TypeError) as err:
            reply = {'type': 'response', 'id': cmd_id, 'ok': False,
                     'error': '{}: {}'.format(type(err).__name__, err)}
        self._enqueue(send_queue, self._encode('control', reply))


# --------------------------------------------------------------------------
# Device controller: owns the MCC 172 and all its configurable state
# --------------------------------------------------------------------------
class Controller:
    """Serializes access to the MCC 172 and executes laptop commands."""

    def __init__(self, hat, settings, product_name='MCC 172'):
        self._hat = hat
        self._product_name = product_name
        self._lock = threading.RLock()          # guards device + state
        self._control_lock = threading.Lock()   # serializes start/stop
        self._registry = None

        # Mutable device state (the authoritative configuration).
        self.channels = list(settings['channels'])
        self.sample_rate = settings['sample_rate']
        self.actual_rate = settings['sample_rate']
        self.clock_source = SourceType.LOCAL
        self.iepe = {0: 1 if settings['iepe_enable'] else 0,
                     1: 1 if settings['iepe_enable'] else 0}
        self.sensitivity = dict(settings['sensitivity'])
        self.trigger_source = SourceType.LOCAL
        self.trigger_mode = TriggerModes.RISING_EDGE
        self.use_trigger = False
        self.continuous = True
        self.ext_clock = False
        self.stream_raw = settings.get('stream_raw', False)

        # Fractional-octave band-filter output (optional; needs numpy/scipy).
        self.band_config = dict(settings.get('bands', {'enabled': False}))
        self._band_bank = None      # active BandFilterBank while running

        # Sound-level-meter processing (frequency + time weighting, storage).
        weighting = settings.get('weighting', {})
        self.freq_weighting = weighting.get('frequency', 'A')
        self.time_weighting = weighting.get('time', 'Fast')
        level = settings.get('level', {})
        self.level_enabled = level.get('enabled', True)
        self.level_rate = level.get('output_rate', 10.0)
        storage = settings.get('storage', {})
        self.buffer_seconds = storage.get('buffer_seconds', 60.0)

        # Runtime SLM state, built on start():
        self._raw_buffer = None
        self._wsos = None           # broadband frequency-weighting SOS
        self._wzi = {}              # per-channel weighting filter state
        self._level = {}            # per-channel ExpLevel (broadband)
        self._band_level = {}       # (band_index, channel) -> ExpLevel
        self._band_offset = {}      # band_index -> A/C weighting offset (dB)

        self._running = False
        self._stop_event = threading.Event()
        self._scan_thread = None

        # Apply the initial IEPE + sensitivity so a fresh boot is calibrated
        # even before the first "start".
        self._apply_static_config()

    def attach_registry(self, registry):
        self._registry = registry

    # -- helpers -----------------------------------------------------------
    @property
    def running(self):
        with self._lock:
            return self._running

    def _apply_static_config(self):
        """Write IEPE + sensitivity to the device (valid only while stopped)."""
        with self._lock:
            for chan in (0, 1):
                self._hat.iepe_config_write(chan, self.iepe[chan])
                self._hat.a_in_sensitivity_write(chan, self.sensitivity[chan])

    def _require_stopped(self):
        if self._running:
            raise CommandError('a scan is active; send "stop" first')

    def _units(self):
        return {str(c): ('Pa' if self.sensitivity[c] != 1000 else 'V')
                for c in self.channels}

    def config_snapshot(self):
        with self._lock:
            return {
                'running': self._running,
                'channels': list(self.channels),
                'sample_rate': self.sample_rate,
                'actual_rate': self.actual_rate,
                'clock_source': self.clock_source.name,
                'iepe': {str(c): self.iepe[c] for c in self.channels},
                'sensitivity_mv_per_unit': {str(c): self.sensitivity[c]
                                            for c in self.channels},
                'units': self._units(),
                'trigger': {'enabled': self.use_trigger,
                            'source': self.trigger_source.name,
                            'mode': self.trigger_mode.name},
                'options': {'continuous': self.continuous,
                            'ext_clock': self.ext_clock},
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
            }

    def handshake(self):
        snap = self.config_snapshot()
        snap.update({'type': 'handshake',
                     'protocol': 'mcc172-noise-monitor/2',
                     'dtype': 'float64', 'byte_order': 'little',
                     'interleave': 'channel-fastest',
                     'num_channels': len(self.channels)})
        # When a scan with band output is active, include the full band table
        # (center frequencies + decimated rates) so the client can demux it.
        if self._band_bank is not None:
            snap['band_table'] = self._band_bank.metadata()
        return snap

    # -- streaming lifecycle ----------------------------------------------
    def start(self):
        with self._control_lock:
            with self._lock:
                if self._running:
                    raise CommandError('already running')
                # Re-apply the full configuration to the hardware.
                for chan in (0, 1):
                    self._hat.iepe_config_write(chan, self.iepe[chan])
                    self._hat.a_in_sensitivity_write(
                        chan, self.sensitivity[chan])
                self._hat.a_in_clock_config_write(
                    self.clock_source, self.sample_rate)
                self._wait_for_sync_locked()
                if self.use_trigger:
                    self._hat.trigger_config(
                        self.trigger_source, self.trigger_mode)

                options = OptionFlags.DEFAULT
                if self.continuous:
                    options |= OptionFlags.CONTINUOUS
                if self.ext_clock:
                    options |= OptionFlags.EXTCLOCK
                if self.use_trigger:
                    options |= OptionFlags.EXTTRIGGER

                self._band_bank = self._build_band_bank()
                self._build_slm()

                self._hat.a_in_scan_start(
                    channels_to_mask(self.channels), 0, options)
                self._running = True
                self._stop_event.clear()

            self._scan_thread = threading.Thread(
                target=self._acquire, daemon=True)
            self._scan_thread.start()
            snapshot = self.handshake()
            snapshot['type'] = 'event'
            snapshot['event'] = 'started'
            if self._registry:
                self._registry.broadcast_message(snapshot)
            return self.config_snapshot()

    def _build_band_bank(self):
        """Create the band-filter bank for the current rate/channels, or None
        if band output is disabled. Import errors disable the feature."""
        cfg = self.band_config
        if not cfg.get('enabled'):
            return None
        try:
            from band_filter import BandFilterBank
        except ImportError as err:
            print('[bands] disabled (missing dependency: {})'.format(err),
                  flush=True)
            return None
        bank = BandFilterBank(
            self.actual_rate, self.channels,
            f_min=cfg.get('f_min', 20.0), f_max=cfg.get('f_max', 20000.0),
            fraction=cfg.get('fraction', 3), order=cfg.get('order', 6),
            margin=cfg.get('margin', 1.0))
        print('[bands] {} bands, {} .. {} Hz'.format(
            len(bank.bands), cfg.get('f_min'), cfg.get('f_max')), flush=True)
        return bank

    def _ref_for(self, channel):
        """SPL reference: 20 uPa when the channel is calibrated to Pa, else 1."""
        return 20e-6 if self.sensitivity[channel] != 1000 else 1.0

    def _build_slm(self):
        """Build the sound-level-meter processing state (raw buffer, weighting
        filters, and time-weighted level integrators) for the current rate."""
        num_channels = len(self.channels)
        self._raw_buffer = RawRingBuffer(
            self.buffer_seconds, num_channels, self.actual_rate)
        self._wsos = None
        self._wzi = {}
        self._level = {}
        self._band_level = {}
        self._band_offset = {}

        need_level = self.level_enabled
        need_band_level = (self._band_bank is not None and
                           self.band_config.get('output', 'level') == 'level')
        if not (need_level or need_band_level):
            return
        try:
            import slm
            from scipy import signal as _sig  # noqa: F401 (ensures scipy)
        except ImportError as err:
            print('[slm] level output disabled (missing dependency: {})'
                  .format(err), flush=True)
            self.level_enabled = False
            return

        tau = slm.tau_for(self.time_weighting)
        if need_level:
            self._wsos = slm.design_weighting_sos(
                self.freq_weighting, self.actual_rate)
            n_sec = self._wsos.shape[0] if self._wsos is not None else 0
            import numpy as _np
            for chan in self.channels:
                self._wzi[chan] = _np.zeros((n_sec, 2))
                self._level[chan] = slm.ExpLevel(
                    self.actual_rate, tau, self.level_rate,
                    ref=self._ref_for(chan))
        if need_band_level:
            for band in self._band_bank.bands:
                self._band_offset[band['index']] = slm.weighting_offset_db(
                    self.freq_weighting, band['center'])
                for chan in self.channels:
                    self._band_level[(band['index'], chan)] = slm.ExpLevel(
                        band['decimated_rate'], tau, self.level_rate,
                        ref=self._ref_for(chan))

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

    def _emit_bands(self, raw_data):
        """Filter + decimate the raw block and send BAND (waveform) frames."""
        for band_index, channel, samples in self._band_bank.process(raw_data):
            frame = band_frame(band_index, channel,
                               samples.astype('<f8').tobytes())
            self._registry.broadcast_stream_frame(frame)

    def _emit_levels(self, raw_data):
        """Apply frequency + Fast time weighting to the broadband signal and
        send LEVEL frames (time-weighted level in dB)."""
        import numpy as np
        from scipy import signal
        num_channels = len(self.channels)
        data = np.asarray(raw_data, dtype=np.float64).reshape(-1, num_channels)
        for ci, chan in enumerate(self.channels):
            x = data[:, ci]
            if self._wsos is not None:
                x, self._wzi[chan] = signal.sosfilt(
                    self._wsos, x, zi=self._wzi[chan])
            levels = self._level[chan].process(x)
            if levels.size:
                self._registry.broadcast_stream_frame(
                    level_frame(chan, levels.astype('<f8').tobytes()))

    def _emit_band_levels(self, raw_data):
        """Time-weight each decimated band and send BAND_LEVEL frames (dB),
        with the A/C frequency-weighting offset applied per band center."""
        for band_index, channel, samples in self._band_bank.process(raw_data):
            integrator = self._band_level.get((band_index, channel))
            if integrator is None:
                continue
            levels = integrator.process(samples)
            if levels.size:
                levels = levels + self._band_offset.get(band_index, 0.0)
                self._registry.broadcast_stream_frame(
                    band_level_frame(band_index, channel,
                                     levels.astype('<f8').tobytes()))

    def _wait_for_sync_locked(self):
        synced = False
        while not synced:
            _source, self.actual_rate, synced = \
                self._hat.a_in_clock_config_read()
            if not synced:
                sleep(0.005)

    def _acquire(self):
        """Background scan loop: read blocks and broadcast DATA frames."""
        num_channels = len(self.channels)
        try:
            while not self._stop_event.is_set():
                with self._lock:
                    result = self._hat.a_in_scan_read(READ_ALL_AVAILABLE, 0)

                if result.hardware_overrun or result.buffer_overrun:
                    kind = ('hardware' if result.hardware_overrun
                            else 'buffer')
                    print('[scan] {} overrun'.format(kind), flush=True)
                    if self._registry:
                        self._registry.broadcast_message(
                            {'type': 'event', 'event': 'overrun',
                             'kind': kind})
                    break

                if result.data:
                    # Always store raw for on-demand Leq/Lmax/Lmin/LN metrics.
                    if self._raw_buffer is not None:
                        self._raw_buffer.append(result.data)
                    if self._registry:
                        if self.stream_raw:
                            self._registry.broadcast_data(
                                array('d', result.data).tobytes())
                        if self.level_enabled and self._level:
                            self._emit_levels(result.data)
                        if self._band_bank is not None:
                            # The band bank is stateful: consume it exactly once
                            # per block, as waveform OR levels (not both).
                            if self.band_config.get('output', 'level') == \
                                    'waveform':
                                self._emit_bands(result.data)
                            else:
                                self._emit_band_levels(result.data)
                else:
                    sleep(0.002)
        finally:
            with self._lock:
                try:
                    self._hat.a_in_scan_stop()
                except HatError:
                    pass
                try:
                    self._hat.a_in_scan_cleanup()
                except HatError:
                    pass
                self._band_bank = None
                self._running = False
            print('[scan] stopped', flush=True)
            if self._registry:
                self._registry.broadcast_message(
                    {'type': 'event', 'event': 'stopped'})

    # -- command dispatch --------------------------------------------------
    def dispatch(self, request):
        cmd = request.get('cmd')
        if not cmd:
            raise CommandError('missing "cmd"')
        handler = self._HANDLERS.get(cmd)
        if handler is None:
            raise CommandError('unknown command: {}'.format(cmd))
        # start/stop manage their own locking (they join the scan thread,
        # which itself needs the device lock); everything else runs under it.
        if cmd in ('start', 'stop'):
            return handler(self, request)
        with self._lock:
            return handler(self, request)

    # ---- query handlers (allowed anytime) ----
    def _cmd_ping(self, _req):
        return {'pong': True}

    def _cmd_get_config(self, _req):
        return self.config_snapshot()

    def _cmd_status(self, _req):
        result = {'running': self._running}
        if self._running:
            status = self._hat.a_in_scan_status()
            result.update({
                'hardware_overrun': status.hardware_overrun,
                'buffer_overrun': status.buffer_overrun,
                'triggered': status.triggered,
                'samples_available': status.samples_available,
                'buffer_size': self._hat.a_in_scan_buffer_size()})
        return result

    def _cmd_info(self, _req):
        info = self._hat.info()
        fw = self._hat.firmware_version()
        return {'address': self._hat.address(),
                'product_name': self._product_name,
                'firmware_version': fw.version,
                'serial': self._hat.serial(),
                'calibration_date': self._hat.calibration_date(),
                'num_ai_channels': info.NUM_AI_CHANNELS,
                'ai_min_voltage': info.AI_MIN_VOLTAGE,
                'ai_max_voltage': info.AI_MAX_VOLTAGE}

    def _cmd_get_sensitivity(self, req):
        chan = self._channel(req)
        return {'channel': chan,
                'sensitivity': self._hat.a_in_sensitivity_read(chan)}

    def _cmd_get_iepe(self, req):
        chan = self._channel(req)
        return {'channel': chan, 'mode': self._hat.iepe_config_read(chan)}

    def _cmd_get_clock(self, _req):
        source, rate, synced = self._hat.a_in_clock_config_read()
        return {'clock_source': SourceType(source).name,
                'sample_rate': rate, 'synced': synced}

    def _cmd_calibration_read(self, req):
        chan = self._channel(req)
        slope, offset = self._hat.calibration_coefficient_read(chan)
        return {'channel': chan, 'slope': slope, 'offset': offset}

    def _cmd_blink_led(self, req):
        count = int(req.get('count', 1))
        self._hat.blink_led(count)
        return {'count': count}

    # ---- streaming ----
    def _cmd_start(self, _req):
        return self.start()

    def _cmd_stop(self, _req):
        return self.stop()

    # ---- configuration handlers (require the scan to be stopped) ----
    def _cmd_set_sensitivity(self, req):
        self._require_stopped()
        chan = self._channel(req)
        value = float(req['value'])
        self._hat.a_in_sensitivity_write(chan, value)
        self.sensitivity[chan] = value
        return {'channel': chan, 'sensitivity': value,
                'units': self._units().get(str(chan))}

    def _cmd_set_iepe(self, req):
        self._require_stopped()
        chan = self._channel(req)
        mode = 1 if req.get('mode') in (1, '1', True, 'true', 'on') else 0
        self._hat.iepe_config_write(chan, mode)
        self.iepe[chan] = mode
        return {'channel': chan, 'mode': mode}

    def _cmd_set_sample_rate(self, req):
        self._require_stopped()
        rate = float(req['sample_rate'])
        source = self._source(req.get('clock_source'), self.clock_source)
        self._hat.a_in_clock_config_write(source, rate)
        self._wait_for_sync_locked()
        self.sample_rate = rate
        self.clock_source = source
        self._raw_buffer = None   # buffered raw no longer matches the rate
        return {'requested_rate': rate, 'actual_rate': self.actual_rate,
                'clock_source': source.name}

    def _cmd_set_channels(self, req):
        self._require_stopped()
        channels = req.get('channels')
        if not isinstance(channels, list) or not channels:
            raise CommandError('"channels" must be a non-empty list')
        channels = [int(c) for c in channels]
        if any(c not in (0, 1) for c in channels):
            raise CommandError('channels must be a subset of 0, 1')
        self.channels = channels
        self._raw_buffer = None   # buffered raw no longer matches channels
        return {'channels': channels}

    def _cmd_set_trigger(self, req):
        self._require_stopped()
        enable = req.get('enable', True)
        self.use_trigger = bool(enable) and enable not in ('false', 'off', 0)
        self.trigger_source = self._source(req.get('source'),
                                           self.trigger_source)
        self.trigger_mode = self._trigger_mode(req.get('mode'),
                                               self.trigger_mode)
        if self.use_trigger:
            self._hat.trigger_config(self.trigger_source, self.trigger_mode)
        return {'enabled': self.use_trigger,
                'source': self.trigger_source.name,
                'mode': self.trigger_mode.name}

    def _cmd_set_options(self, req):
        self._require_stopped()
        if 'continuous' in req:
            self.continuous = bool(req['continuous'])
        if 'ext_clock' in req:
            self.ext_clock = bool(req['ext_clock'])
        if 'stream_raw' in req:
            self.stream_raw = bool(req['stream_raw'])
        return {'continuous': self.continuous, 'ext_clock': self.ext_clock,
                'stream_raw': self.stream_raw}

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
        # Build a throwaway bank to validate settings and report the resulting
        # band table (also surfaces a missing numpy/scipy immediately).
        table = None
        if cfg.get('enabled'):
            try:
                from band_filter import BandFilterBank
            except ImportError as err:
                raise CommandError(
                    'band output needs numpy + scipy on the Pi ({})'.format(
                        err))
            bank = BandFilterBank(
                self.actual_rate, self.channels,
                f_min=cfg.get('f_min', 20.0), f_max=cfg.get('f_max', 20000.0),
                fraction=cfg.get('fraction', 3), order=cfg.get('order', 6),
                margin=cfg.get('margin', 1.0))
            table = bank.metadata()
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
            self._raw_buffer = None
        return {'buffer_seconds': self.buffer_seconds}

    def _cmd_get_metrics(self, req):
        """Compute SLM metrics (Leq, Lmax, Lmin, Lpeak, LN) over the most
        recent buffered raw data. Works while running or after a stop."""
        if self._raw_buffer is None:
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

        flat = self._raw_buffer.get_recent(seconds, self.actual_rate)
        nch = len(self.channels)
        if len(flat) < nch:
            raise CommandError('not enough data buffered')
        data = np.asarray(flat, dtype=np.float64).reshape(-1, nch)

        results = {}
        for chan in want:
            if chan not in self.channels:
                continue
            ci = self.channels.index(chan)
            metrics = slm.window_metrics(
                data[:, ci], self.actual_rate, weighting=weighting,
                time_weighting=time_w, ref=self._ref_for(chan),
                percentiles=pct)
            metrics['units'] = self._units().get(str(chan))
            metrics['calibrated'] = self.sensitivity[chan] != 1000
            if req.get('include_bands') and self.band_config.get('enabled'):
                metrics['bands'] = self._band_metrics(
                    data[:, ci], chan, weighting)
            results[str(chan)] = metrics

        return {'sample_rate': self.actual_rate,
                'requested_seconds': seconds,
                'channels': results}

    def _band_metrics(self, x, chan, weighting):
        """Per-band Leq (with A/C offset) over a window, computed on demand."""
        import numpy as np
        import math
        import slm
        from band_filter import BandFilterBank
        cfg = self.band_config
        bank = BandFilterBank(
            self.actual_rate, [chan],
            f_min=cfg.get('f_min', 20.0), f_max=cfg.get('f_max', 20000.0),
            fraction=cfg.get('fraction', 3), order=cfg.get('order', 6),
            margin=cfg.get('margin', 1.0))
        segments = {}
        for band_index, _c, samples in bank.process(list(x)):
            segments.setdefault(band_index, []).append(samples)
        ref2 = self._ref_for(chan) ** 2
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
        chan = self._channel(req)
        slope = float(req['slope'])
        offset = float(req['offset'])
        self._hat.calibration_coefficient_write(chan, slope, offset)
        return {'channel': chan, 'slope': slope, 'offset': offset}

    def _cmd_test_signals_write(self, req):
        self._require_stopped()
        mode = int(req['mode'])
        clock = int(req.get('clock', 0))
        sync = int(req.get('sync', 0))
        self._hat.test_signals_write(mode, clock, sync)
        return {'mode': mode, 'clock': clock, 'sync': sync}

    # -- small parsers -----------------------------------------------------
    @staticmethod
    def _channel(req):
        chan = int(req['channel'])
        if chan not in (0, 1):
            raise CommandError('channel must be 0 or 1')
        return chan

    @staticmethod
    def _source(value, default):
        if value is None:
            return default
        if isinstance(value, str):
            key = value.upper()
            if key not in SOURCE_TYPES:
                raise CommandError('source must be one of {}'.format(
                    list(SOURCE_TYPES)))
            return SOURCE_TYPES[key]
        return SourceType(int(value))

    @staticmethod
    def _trigger_mode(value, default):
        if value is None:
            return default
        if isinstance(value, str):
            key = value.upper()
            if key not in TRIGGER_MODES:
                raise CommandError('mode must be one of {}'.format(
                    list(TRIGGER_MODES)))
            return TRIGGER_MODES[key]
        return TriggerModes(int(value))

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
        'calibration_write': _cmd_calibration_write,
        'test_signals_write': _cmd_test_signals_write,
    }


# --------------------------------------------------------------------------
# Device open + main
# --------------------------------------------------------------------------
def open_mcc172():
    hats = hat_list(filter_by_id=HatIDs.MCC_172)
    if not hats:
        raise HatError(0, 'No MCC 172 HAT device found')
    descriptor = hats[0]
    print('[hat] using {} at address {}'.format(
        descriptor.product_name, descriptor.address), flush=True)
    return mcc172(descriptor.address), descriptor.product_name


def main():
    config_path = os.environ.get(
        'NOISE_MONITOR_CONFIG',
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.ini'))
    settings = load_config(config_path)

    hat, product_name = open_mcc172()
    controller = Controller(hat, settings, product_name)
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

    # Auto-start streaming at boot if configured (original headless behavior).
    if settings['autostart']:
        try:
            controller.start()
            print('[scan] autostart: streaming', flush=True)
        except HatError as err:
            print('[scan] autostart failed: {}'.format(err), flush=True)

    # Accept stream connections in a background thread; control in the main one.
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
        except HatError:
            pass
        for srv in (control_server, stream_server):
            try:
                srv.close()
            except OSError:
                pass
        # Power the sensors down on exit.
        for chan in (0, 1):
            try:
                hat.iepe_config_write(chan, 0)
            except HatError:
                pass


if __name__ == '__main__':
    try:
        main()
    except (HatError, ValueError, RuntimeError) as err:
        print('\nError: {}'.format(err), file=sys.stderr)
        sys.exit(1)
