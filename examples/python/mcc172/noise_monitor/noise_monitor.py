#!/usr/bin/env python3
#  -*- coding: utf-8 -*-
"""
MCC 172 Noise Monitor -- Raspberry Pi side (bidirectional control).

Runs on a headless Raspberry Pi with an MCC 172 DAQ HAT. It acquires the
raw waveform from an IEPE microphone/accelerometer and streams it to one or
more laptops over TCP, while also accepting control commands so the laptop
can drive every configurable feature of the MCC 172 (sensitivity,
sample rate, IEPE power, trigger, calibration, LED, test signals, ...) and
start/stop streaming on demand. Designed to be launched automatically at
boot by systemd (see noise-monitor.service).

Wire protocol
-------------
Downstream (Pi -> laptop) frames:
    [1-byte type][4-byte little-endian uint32 length][payload]
      type 0x01 DATA : payload = interleaved little-endian float64 samples,
                       channel-fastest: ch0[n], ch1[n], ch0[n+1], ...
      type 0x02 MSG  : payload = UTF-8 JSON. The first MSG on connect is the
                       handshake; the rest are command responses and events.

Upstream (laptop -> Pi) commands:
    One UTF-8 JSON object per line ('\\n' terminated), e.g.
        {"id": 4, "cmd": "set_sensitivity", "channel": 0, "value": 50}
    Every command may include an optional "id" which is echoed back in the
    response so the laptop can match replies. See COMMANDS below for the
    full list.
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
TYPE_DATA = 0x01
TYPE_MSG = 0x02
FRAME_HEADER = struct.Struct('<BI')   # type byte + payload length

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
        'sensitivity': {
            0: parser.getfloat('calibration', 'sensitivity_ch0'),
            1: parser.getfloat('calibration', 'sensitivity_ch1'),
        },
        'host': parser.get('network', 'host'),
        'port': parser.getint('network', 'port'),
        'max_queue_blocks': parser.getint('network', 'max_queue_blocks'),
        'autostart': parser.getboolean('control', 'autostart', fallback=True),
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


# --------------------------------------------------------------------------
# Client registry (each laptop gets its own send queue + reader/sender)
# --------------------------------------------------------------------------
class ClientRegistry:
    """Manages connected laptops: fan out data/events, deliver replies."""

    def __init__(self, controller, max_queue_blocks):
        self._controller = controller
        self._max_queue_blocks = max_queue_blocks
        self._clients = {}          # conn -> queue.Queue of framed bytes
        self._lock = threading.Lock()

    def add(self, conn, addr):
        send_queue = queue.Queue(maxsize=self._max_queue_blocks)
        with self._lock:
            self._clients[conn] = send_queue
        threading.Thread(target=self._sender,
                         args=(conn, addr, send_queue), daemon=True).start()
        threading.Thread(target=self._reader,
                         args=(conn, addr, send_queue), daemon=True).start()
        # Greet the new client with the current configuration.
        self._enqueue(send_queue, msg_frame(self._controller.handshake()))
        print('[net] client connected: {}'.format(addr), flush=True)

    def broadcast_data(self, frame):
        """Fan a DATA frame out to every client, dropping oldest on overflow."""
        with self._lock:
            queues = list(self._clients.values())
        for send_queue in queues:
            self._enqueue(send_queue, frame, drop_oldest=True)

    def broadcast_message(self, obj):
        """Send a MSG (e.g. an event) to every connected client."""
        frame = msg_frame(obj)
        with self._lock:
            queues = list(self._clients.values())
        for send_queue in queues:
            self._enqueue(send_queue, frame)

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

    def _reader(self, conn, addr, send_queue):
        """Read newline-delimited JSON commands and reply on this client's
        queue."""
        buf = bytearray()
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
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
        self._enqueue(send_queue, msg_frame(reply))


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
            }

    def handshake(self):
        snap = self.config_snapshot()
        snap.update({'type': 'handshake',
                     'protocol': 'mcc172-noise-monitor/2',
                     'dtype': 'float64', 'byte_order': 'little',
                     'interleave': 'channel-fastest',
                     'num_channels': len(self.channels)})
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

                self._hat.a_in_scan_start(
                    channels_to_mask(self.channels), 0, options)
                self._running = True
                self._stop_event.clear()

            self._scan_thread = threading.Thread(
                target=self._acquire, daemon=True)
            self._scan_thread.start()
            return self.config_snapshot()

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
                    payload = array('d', result.data).tobytes()
                    if self._registry:
                        self._registry.broadcast_data(data_frame(payload))
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
        return {'continuous': self.continuous, 'ext_clock': self.ext_clock}

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

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((settings['host'], settings['port']))
    server.listen(5)
    server.settimeout(1.0)
    print('[net] listening on {}:{}'.format(
        settings['host'], settings['port']), flush=True)

    stop_event = threading.Event()

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

    try:
        while not stop_event.is_set():
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            registry.add(conn, addr)
    finally:
        try:
            if controller.running:
                controller.stop()
        except HatError:
            pass
        try:
            server.close()
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
