#!/usr/bin/env python3
#  -*- coding: utf-8 -*-
"""
MCC 172 Noise Monitor -- Raspberry Pi side.

Continuously acquires the raw waveform from an IEPE microphone (or
accelerometer) on an MCC 172 DAQ HAT and streams it, unmodified, to any
laptop that connects over TCP. Designed to be launched automatically at
boot by systemd (see noise-monitor.service).

MCC 172 functions demonstrated:
    mcc172.iepe_config_write        - turn the 2 mA IEPE supply on/off
    mcc172.a_in_sensitivity_write   - apply the sensor calibration factor
    mcc172.a_in_clock_config_write  - set the sampling rate
    mcc172.a_in_clock_config_read   - wait for clock synchronization
    mcc172.a_in_scan_start          - start the continuous scan
    mcc172.a_in_scan_read           - read blocks of samples
    mcc172.a_in_scan_stop / cleanup - stop cleanly on exit

Wire protocol (see laptop_client.py for the matching reader):
    1. On connect the server sends one line of UTF-8 JSON terminated by
       '\\n' describing the stream (sample_rate, channels, units, dtype).
    2. It then sends a continuous sequence of frames:
           [4-byte little-endian uint32 = payload length in bytes]
           [payload = interleaved little-endian float64 samples]
       Sample interleave order matches the MCC 172 read buffer:
           ch0[n], ch1[n], ch0[n+1], ch1[n+1], ...
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

from daqhats import mcc172, OptionFlags, SourceType, HatIDs, HatError, hat_list

# Read all available samples on each a_in_scan_read call.
READ_ALL_AVAILABLE = -1

# Magic length prefix size for each streamed frame.
FRAME_HEADER = struct.Struct('<I')


def load_config(path):
    """Load and validate config.ini, returning a plain settings dict."""
    parser = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
    if not parser.read(path):
        raise RuntimeError('Could not read configuration file: {}'.format(path))

    channels = [int(c) for c in parser.get('acquisition', 'channels').split(',')
                if c.strip() != '']
    if not channels or any(c not in (0, 1) for c in channels):
        raise ValueError('channels must be a subset of 0, 1')

    settings = {
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
    }
    return settings


def channels_to_mask(channels):
    """Convert a list of channel numbers to an MCC 172 channel mask."""
    mask = 0
    for chan in channels:
        mask |= 0x01 << chan
    return mask


class ClientRegistry:
    """Thread-safe set of connected laptops, each with its own send queue."""

    def __init__(self, handshake_bytes, max_queue_blocks):
        self._handshake = handshake_bytes
        self._max_queue_blocks = max_queue_blocks
        self._clients = {}          # socket -> queue.Queue
        self._lock = threading.Lock()

    def add(self, conn, addr):
        """Register a new client and start its dedicated sender thread."""
        block_queue = queue.Queue(maxsize=self._max_queue_blocks)
        with self._lock:
            self._clients[conn] = block_queue
        sender = threading.Thread(
            target=self._serve, args=(conn, addr, block_queue), daemon=True)
        sender.start()
        print('[net] client connected: {}'.format(addr), flush=True)

    def broadcast(self, frame):
        """Queue a frame for every client, dropping oldest on overflow."""
        with self._lock:
            queues = list(self._clients.values())
        for block_queue in queues:
            try:
                block_queue.put_nowait(frame)
            except queue.Full:
                # Slow client: discard its oldest block to stay real-time.
                try:
                    block_queue.get_nowait()
                    block_queue.put_nowait(frame)
                except queue.Empty:
                    pass

    def _remove(self, conn):
        with self._lock:
            self._clients.pop(conn, None)

    def _serve(self, conn, addr, block_queue):
        """Send the handshake, then stream queued frames until disconnect."""
        try:
            conn.sendall(self._handshake)
            while True:
                frame = block_queue.get()
                if frame is None:      # shutdown sentinel
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

    def shutdown(self):
        with self._lock:
            conns = list(self._clients.keys())
        for conn in conns:
            try:
                conn.close()
            except OSError:
                pass


def open_mcc172():
    """Find and open the first MCC 172 HAT, or raise if none is present."""
    hats = hat_list(filter_by_id=HatIDs.MCC_172)
    if not hats:
        raise HatError(0, 'No MCC 172 HAT device found')
    address = hats[0].address
    print('[hat] using MCC 172 at address {}'.format(address), flush=True)
    return mcc172(address)


def configure_device(hat, settings):
    """Apply IEPE power, calibration, and clock; wait for sync. Returns the
    actual scan rate reported by the hardware."""
    iepe = 1 if settings['iepe_enable'] else 0
    for chan in settings['channels']:
        hat.iepe_config_write(chan, iepe)
        # Sensitivity must be set before the scan starts.
        hat.a_in_sensitivity_write(chan, settings['sensitivity'][chan])

    hat.a_in_clock_config_write(SourceType.LOCAL, settings['sample_rate'])

    synced = False
    actual_rate = settings['sample_rate']
    while not synced:
        _source, actual_rate, synced = hat.a_in_clock_config_read()
        if not synced:
            sleep(0.005)
    return actual_rate


def build_handshake(settings, actual_rate):
    """JSON metadata line sent to each client on connect."""
    # Units are pascals only when the sensitivity has been calibrated away
    # from the default 1000 (= no scaling -> volts).
    units = {}
    for chan in settings['channels']:
        units[chan] = 'Pa' if settings['sensitivity'][chan] != 1000 else 'V'

    meta = {
        'protocol': 'mcc172-noise-monitor/1',
        'sample_rate': actual_rate,
        'channels': settings['channels'],
        'num_channels': len(settings['channels']),
        'iepe_enable': settings['iepe_enable'],
        'sensitivity_mv_per_unit': {
            str(c): settings['sensitivity'][c] for c in settings['channels']},
        'units': {str(c): units[c] for c in settings['channels']},
        'dtype': 'float64',
        'byte_order': 'little',
        'interleave': 'channel-fastest',
    }
    return (json.dumps(meta) + '\n').encode('utf-8')


def acquire_and_stream(hat, settings, registry, stop_event):
    """Continuous scan loop: read blocks and broadcast them as framed bytes."""
    mask = channels_to_mask(settings['channels'])
    hat.a_in_scan_start(mask, 0, OptionFlags.CONTINUOUS)
    print('[scan] started; streaming raw waveform', flush=True)

    timeout = 5.0
    try:
        while not stop_event.is_set():
            result = hat.a_in_scan_read(READ_ALL_AVAILABLE, timeout)

            if result.hardware_overrun:
                print('[scan] HARDWARE OVERRUN', flush=True)
                break
            if result.buffer_overrun:
                print('[scan] BUFFER OVERRUN', flush=True)
                break

            if not result.data:
                sleep(0.005)
                continue

            # Pack the float samples once, then fan out to all clients.
            payload = array('d', result.data).tobytes()
            frame = FRAME_HEADER.pack(len(payload)) + payload
            registry.broadcast(frame)
    finally:
        hat.a_in_scan_stop()
        hat.a_in_scan_cleanup()
        for chan in settings['channels']:
            try:
                hat.iepe_config_write(chan, 0)   # power the sensor down
            except HatError:
                pass
        print('[scan] stopped', flush=True)


def main():
    config_path = os.environ.get(
        'NOISE_MONITOR_CONFIG',
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.ini'))
    settings = load_config(config_path)

    hat = open_mcc172()
    actual_rate = configure_device(hat, settings)
    print('[hat] requested {} Hz, actual {} Hz/ch'.format(
        settings['sample_rate'], actual_rate), flush=True)

    handshake = build_handshake(settings, actual_rate)
    registry = ClientRegistry(handshake, settings['max_queue_blocks'])
    stop_event = threading.Event()

    # Start the TCP server that accepts laptop connections.
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((settings['host'], settings['port']))
    server.listen(5)
    server.settimeout(1.0)
    print('[net] listening on {}:{}'.format(
        settings['host'], settings['port']), flush=True)

    def accept_loop():
        while not stop_event.is_set():
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            registry.add(conn, addr)

    accept_thread = threading.Thread(target=accept_loop, daemon=True)
    accept_thread.start()

    # Clean shutdown on Ctrl-C or systemd stop (SIGTERM).
    def handle_signal(_signum, _frame):
        stop_event.set()
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        acquire_and_stream(hat, settings, registry, stop_event)
    finally:
        stop_event.set()
        try:
            server.close()
        except OSError:
            pass
        registry.shutdown()


if __name__ == '__main__':
    try:
        main()
    except (HatError, ValueError, RuntimeError) as err:
        print('\nError: {}'.format(err), file=sys.stderr)
        # Non-zero exit lets systemd Restart=always retry (e.g. HAT not ready).
        sys.exit(1)
