"""Read-only Linux health snapshots; no sleep, subprocess, or extra dependency.

CPU counters and MemAvailable follow https://docs.kernel.org/filesystems/proc.html.
CPU is aggregate busy time (0..100 across all cores), excluding idle/iowait.
"""
import json
import math
from pathlib import Path
import shutil
import threading
import time


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def ups_snapshot(path, stale_after=60.0):
    try:
        with open(path) as stream:
            data = json.loads(stream.read(16385))
        if not isinstance(data, dict):
            return {'available': False}
        stamp = number(data.get('timestamp'))
        if stamp is None:
            return {'available': False}
        age = time.time() - stamp
        values = {key: number(data.get(key)) for key in (
            'percent', 'bus_voltage_v', 'current_ma', 'power_w',
            'low_battery_hold_seconds')}
        if values['percent'] is not None and not 0 <= values['percent'] <= 100:
            values['percent'] = None
        return dict(values, available=any(v is not None for v in values.values()),
                    stale=age < 0 or age > stale_after, age_seconds=round(age, 1))
    except (OSError, ValueError, TypeError):
        return {'available': False}


class SystemHealth:
    def __init__(self, proc='/proc', sys='/sys', disk='/'):
        self.proc, self.sys, self.disk = Path(proc), Path(sys), disk
        self._previous = None
        self._cached = None
        self._sampled = float('-inf')
        self._lock = threading.Lock()

    def snapshot(self):
        with self._lock:
            now = time.monotonic()
            if self._cached is not None and now - self._sampled < 1.0:
                return dict(self._cached)
            result = {key: None for key in (
                'cpu_percent', 'temperature_c', 'cpu_frequency_mhz',
                'memory_total_bytes', 'memory_available_bytes', 'memory_used_percent',
                'disk_total_bytes', 'disk_free_bytes', 'uptime_seconds')}
            try:
                # guest/guest_nice are already included in user/nice.
                ticks = [int(x) for x in (self.proc / 'stat').read_text().splitlines()[0].split()[1:9]]
                total, idle = sum(ticks), ticks[3] + ticks[4]
                if self._previous is not None:
                    dt, di = total - self._previous[0], idle - self._previous[1]
                    if dt > 0 and 0 <= di <= dt:
                        result['cpu_percent'] = round(100 * (dt - di) / dt, 1)
                self._previous = total, idle
            except (OSError, ValueError, IndexError):
                self._previous = None
            try:
                mem = {line.split(':')[0]: int(line.split()[1]) * 1024
                       for line in (self.proc / 'meminfo').read_text().splitlines()}
                total, available = mem['MemTotal'], mem['MemAvailable']
                if total > 0 and 0 <= available <= total:
                    result.update(memory_total_bytes=total, memory_available_bytes=available,
                                  memory_used_percent=round(100 * (total - available) / total, 1))
            except (OSError, ValueError, KeyError, IndexError):
                pass
            for key, path, divisor in (
                ('temperature_c', self.sys / 'class/thermal/thermal_zone0/temp', 1000),
                ('cpu_frequency_mhz', self.sys / 'devices/system/cpu/cpu0/cpufreq/scaling_cur_freq', 1000),
                ('uptime_seconds', self.proc / 'uptime', 1),
            ):
                try:
                    result[key] = number(float(path.read_text().split()[0]) / divisor)
                except (OSError, ValueError, IndexError):
                    pass
            try:
                disk = shutil.disk_usage(self.disk)
                result.update(disk_total_bytes=disk.total, disk_free_bytes=disk.free)
            except OSError:
                pass
            result['available'] = any(value is not None for value in result.values())
            result['timestamp'] = time.time()
            self._cached, self._sampled = result, now
            return dict(result)
