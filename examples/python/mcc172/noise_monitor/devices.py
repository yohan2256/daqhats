#!/usr/bin/env python3
#  -*- coding: utf-8 -*-
"""
Acquisition-device backends for the noise monitor.

Two backends behind one small interface, so the monitor can run an MCC 172
DAQ HAT (2 IEPE channels, daqhats library) and a Data Translation DT9837A
(4 IEPE channels, uldaq library) side by side -- 6 channels total.

Global channel numbering: devices are opened in the order listed in
config.ini and their channels are numbered sequentially, e.g.

    mcc172  local 0,1   -> global 0,1
    dt9837a local 0..3  -> global 2..5

IMPORTANT -- clocks are NOT synchronized between devices. Each device runs
its own ADC clock, so blocks from different devices drift relative to each
other. Per-channel levels and metrics are unaffected; cross-channel phase
analysis is only valid within a single device.

Backend interface (duck-typed):
    name, num_channels, actual_rate, running
    configure(rate, iepe_by_local_chan, sensitivity_mv_by_local_chan)
    start()                 -> begin the continuous scan
    read_new()              -> (interleaved_samples, overrun_flag); non-blocking
    stop()
    close()
    set_sensitivity(local_chan, mv_per_unit)   (only while stopped)
    set_iepe(local_chan, on)                   (only while stopped)
    info()                  -> dict for the "info" command
    blink(count)            -> flash the device LED if supported

Sensitivity is expressed in mV per mechanical unit everywhere (the MCC 172
convention); the uldaq backend converts to V/unit internally. 1000 mV/unit
means "no scaling" (data in volts) for both backends.
"""
from __future__ import print_function


class Mcc172Backend:
    """MCC 172 DAQ HAT via the daqhats library (2 IEPE channels)."""

    name = 'mcc172'

    def __init__(self, channels=None, address=None):
        from daqhats import hat_list, mcc172, HatIDs, HatError
        self._HatError = HatError
        hats = hat_list(filter_by_id=HatIDs.MCC_172)
        if not hats:
            raise RuntimeError('No MCC 172 HAT device found')
        descriptor = hats[0] if address is None else next(
            (h for h in hats if h.address == address), None)
        if descriptor is None:
            raise RuntimeError('No MCC 172 at address {}'.format(address))
        self._hat = mcc172(descriptor.address)
        self._product = descriptor.product_name
        self._address = descriptor.address
        self.channels = sorted(set(channels)) if channels else [0, 1]
        if any(c not in (0, 1) for c in self.channels):
            raise ValueError('mcc172 channels must be a subset of 0, 1')
        self.num_channels = len(self.channels)
        self.actual_rate = 51200.0
        self.running = False

    # -- configuration (call while stopped) --
    def configure(self, rate, iepe, sensitivity_mv):
        from daqhats import SourceType
        from time import sleep
        for chan in self.channels:
            self._hat.iepe_config_write(chan, 1 if iepe.get(chan) else 0)
            self._hat.a_in_sensitivity_write(
                chan, sensitivity_mv.get(chan, 1000.0))
        self._hat.a_in_clock_config_write(SourceType.LOCAL, rate)
        synced = False
        while not synced:
            _src, self.actual_rate, synced = self._hat.a_in_clock_config_read()
            if not synced:
                sleep(0.005)

    def set_sensitivity(self, chan, mv_per_unit):
        self._hat.a_in_sensitivity_write(chan, mv_per_unit)

    def set_iepe(self, chan, on):
        self._hat.iepe_config_write(chan, 1 if on else 0)

    # -- triggered start --
    def arm_trigger(self):
        """Configure the TRIG input for a rising edge (call while stopped).
        Rising is the common denominator: the DT9837A's external digital
        trigger only supports rising edges."""
        from daqhats import SourceType, TriggerModes
        self._hat.trigger_config(SourceType.LOCAL, TriggerModes.RISING_EDGE)

    def has_triggered(self):
        """True once the armed scan has seen its trigger edge."""
        try:
            return bool(self._hat.a_in_scan_status().triggered)
        except self._HatError:
            return False

    # -- streaming --
    def start(self, triggered=False):
        from daqhats import OptionFlags
        mask = 0
        for chan in self.channels:
            mask |= 1 << chan
        options = OptionFlags.CONTINUOUS
        if triggered:
            options |= OptionFlags.EXTTRIGGER
        self._hat.a_in_scan_start(mask, 0, options)
        self.running = True

    def read_new(self):
        result = self._hat.a_in_scan_read(-1, 0)
        overrun = result.hardware_overrun or result.buffer_overrun
        return list(result.data), overrun

    def stop(self):
        try:
            self._hat.a_in_scan_stop()
        except self._HatError:
            pass
        try:
            self._hat.a_in_scan_cleanup()
        except self._HatError:
            pass
        self.running = False

    def close(self):
        for chan in self.channels:
            try:
                self._hat.iepe_config_write(chan, 0)
            except self._HatError:
                pass

    # -- misc --
    def info(self):
        fw = self._hat.firmware_version()
        return {'type': 'mcc172', 'product_name': self._product,
                'address': self._address,
                'firmware_version': fw.version,
                'serial': self._hat.serial(),
                'calibration_date': self._hat.calibration_date()}

    def blink(self, count):
        self._hat.blink_led(count)

    # MCC-172-specific extras used by some commands.
    def calibration_read(self, chan):
        return self._hat.calibration_coefficient_read(chan)

    def calibration_write(self, chan, slope, offset):
        self._hat.calibration_coefficient_write(chan, slope, offset)

    def trigger_config(self, source, mode):
        self._hat.trigger_config(source, mode)

    def test_signals_write(self, mode, clock, sync):
        self._hat.test_signals_write(mode, clock, sync)


class Dt9837aBackend:
    """Data Translation DT9837A via the uldaq library (4 IEPE channels).

    The DT9837A is a USB dynamic-signal-analyzer module: 4 simultaneous
    24-bit IEPE inputs, up to 52.734 kHz per channel. uldaq handles IEPE
    excitation, AC coupling, and per-channel sensor sensitivity (V/unit,
    converted from the monitor's mV/unit convention).
    """

    name = 'dt9837a'

    #: seconds of circular buffer allocated for the background scan
    BUFFER_SECONDS = 4.0

    def __init__(self, channels=None, unique_id=None):
        import uldaq
        self._ul = uldaq
        inventory = uldaq.get_daq_device_inventory(uldaq.InterfaceType.ANY)
        matches = [d for d in inventory
                   if 'DT9837' in (d.product_name or '')]
        if unique_id is not None:
            matches = [d for d in matches if d.unique_id == unique_id]
        if not matches:
            raise RuntimeError('No DT9837A device found')
        self._descriptor = matches[0]
        self._device = uldaq.DaqDevice(self._descriptor)
        self._device.connect()
        self._ai = self._device.get_ai_device()
        self._ai_config = self._ai.get_config()
        info = self._ai.get_info()
        max_chans = info.get_num_chans()
        # uldaq scans a contiguous low..high range, so channels must be 0..N.
        self.channels = sorted(set(channels)) if channels else list(
            range(min(4, max_chans)))
        if self.channels != list(range(len(self.channels))):
            raise ValueError('dt9837a channels must be contiguous from 0 '
                             '(e.g. 0,1,2)')
        if len(self.channels) > max_chans:
            raise ValueError('dt9837a has only {} channels'.format(max_chans))
        self.num_channels = len(self.channels)
        self._range = info.get_ranges(self._input_mode())[0]
        self._buffer = None
        self._last_total = 0
        self.actual_rate = 51200.0
        self.running = False

    def _input_mode(self):
        return self._ul.AiInputMode.DIFFERENTIAL

    # -- configuration (call while stopped) --
    def configure(self, rate, iepe, sensitivity_mv):
        ul = self._ul
        for chan in self.channels:
            mode = (ul.IepeMode.ENABLED if iepe.get(chan)
                    else ul.IepeMode.DISABLED)
            self._ai_config.set_chan_iepe_mode(chan, mode)
            self._ai_config.set_chan_coupling_mode(chan, ul.CouplingMode.AC)
            # uldaq sensitivity is V/unit; the monitor speaks mV/unit.
            self._ai_config.set_chan_sensor_sensitivity(
                chan, sensitivity_mv.get(chan, 1000.0) / 1000.0)
        self._requested_rate = rate
        # The exact achieved rate is only reported when the scan starts;
        # until then the requested rate is the best estimate (the DT9837A
        # supports nearly arbitrary rates up to 52.734 kHz).
        self.actual_rate = rate

    def set_sensitivity(self, chan, mv_per_unit):
        self._ai_config.set_chan_sensor_sensitivity(chan, mv_per_unit / 1000.0)

    def set_iepe(self, chan, on):
        ul = self._ul
        self._ai_config.set_chan_iepe_mode(
            chan, ul.IepeMode.ENABLED if on else ul.IepeMode.DISABLED)

    # -- triggered start --
    def arm_trigger(self):
        """Arm the external digital trigger (rising edge -- the only edge the
        DT9837A supports). Call while stopped, before start()."""
        ul = self._ul
        self._ai.set_trigger(ul.TriggerType.POS_EDGE, 0, 0.0, 0.0, 0)

    def has_triggered(self):
        """True once samples have started flowing (the DT9837A has no
        explicit 'triggered' status; first data implies the edge arrived)."""
        try:
            _status, transfer = self._ai.get_scan_status()
            return transfer.current_total_count > 0
        except self._ul.ULException:
            return False

    # -- streaming --
    def start(self, triggered=False):
        ul = self._ul
        samples_per_chan = max(1000, int(self._requested_rate *
                                         self.BUFFER_SECONDS))
        self._buffer = ul.create_float_buffer(self.num_channels,
                                              samples_per_chan)
        self._last_total = 0
        options = ul.ScanOption.CONTINUOUS
        if triggered:
            options |= ul.ScanOption.EXTTRIGGER
        self.actual_rate = self._ai.a_in_scan(
            0, self.num_channels - 1, self._input_mode(), self._range,
            samples_per_chan, self._requested_rate,
            options, ul.AInScanFlag.DEFAULT, self._buffer)
        self.running = True

    def read_new(self):
        """Return samples written to the circular buffer since the last call."""
        ul = self._ul
        status, transfer = self._ai.get_scan_status()
        total = transfer.current_total_count      # cumulative, interleaved
        new = total - self._last_total
        buf_len = len(self._buffer)
        overrun = False
        if new > buf_len:
            # The writer lapped us; drop to the newest full buffer.
            self._last_total = total - buf_len
            new = buf_len
            overrun = True
        start = self._last_total % buf_len
        end = (start + new) % buf_len
        if new == 0:
            data = []
        elif start < end:
            data = list(self._buffer[start:end])
        else:
            data = list(self._buffer[start:]) + list(self._buffer[:end])
        self._last_total = total
        # A non-RUNNING status is an error only once data has flowed; an
        # armed scan waiting for its trigger edge must not be flagged.
        if status != ul.ScanStatus.RUNNING and self.running and total > 0:
            overrun = True
        return data, overrun

    def stop(self):
        try:
            self._ai.scan_stop()
        except self._ul.ULException:
            pass
        self.running = False

    def close(self):
        try:
            self.stop()
        except Exception:   # noqa: BLE001 - releasing best-effort
            pass
        try:
            self._device.disconnect()
            self._device.release()
        except self._ul.ULException:
            pass

    # -- misc --
    def info(self):
        return {'type': 'dt9837a',
                'product_name': self._descriptor.product_name,
                'unique_id': self._descriptor.unique_id,
                'interface': str(self._descriptor.dev_interface)}

    def blink(self, count):
        self._device.flash_led(count)


def open_backends(device_configs):
    """Open the configured backends in order, returning (backends, errors).

    ``device_configs`` is a list of dicts: {'type': 'mcc172'|'dt9837a',
    'channels': [...]}. Unavailable devices are reported, not fatal, so a Pi
    with only one of the two devices attached still starts with what it has.
    """
    backends = []
    errors = {}
    for cfg in device_configs:
        name = cfg.get('type')
        try:
            if name == 'mcc172':
                backends.append(Mcc172Backend(channels=cfg.get('channels')))
            elif name == 'dt9837a':
                backends.append(Dt9837aBackend(channels=cfg.get('channels')))
            else:
                errors[name] = 'unknown device type'
        except Exception as err:    # noqa: BLE001 - report and continue
            errors[name] = str(err)
    return backends, errors


class ChannelMap:
    """Maps global channel numbers to (device_index, local_channel)."""

    def __init__(self, backends):
        self.entries = []            # global index -> (dev_idx, local_chan)
        for dev_idx, backend in enumerate(backends):
            for local in backend.channels:
                self.entries.append((dev_idx, local))

    def __len__(self):
        return len(self.entries)

    def resolve(self, global_chan):
        if 0 <= global_chan < len(self.entries):
            return self.entries[global_chan]
        raise ValueError('channel must be 0..{}'.format(len(self.entries) - 1))

    def globals_for_device(self, dev_idx):
        return [g for g, (d, _l) in enumerate(self.entries) if d == dev_idx]

    def table(self, backends):
        return [{'global': g, 'device': d, 'device_type': backends[d].name,
                 'local': l} for g, (d, l) in enumerate(self.entries)]
