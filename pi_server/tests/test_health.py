import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import health


def test_cpu_delta_cache_and_memavailable(tmp_path, monkeypatch):
    clock = [1.0]
    monkeypatch.setattr(health.time, 'monotonic', lambda: clock[0])
    (tmp_path / 'stat').write_text('cpu 100 0 100 800 0 0 0 0 90 0\n')
    (tmp_path / 'meminfo').write_text('MemTotal: 4000 kB\nMemFree: 100 kB\nMemAvailable: 3000 kB\n')
    monitor = health.SystemHealth(proc=tmp_path, sys=tmp_path, disk=tmp_path)
    first = monitor.snapshot()
    assert first['cpu_percent'] is None
    assert first['memory_used_percent'] == 25
    assert first['memory_available_bytes'] == 3000 * 1024
    (tmp_path / 'stat').write_text('cpu 150 0 150 900 0 0 0 0 190 0\n')
    assert monitor.snapshot()['cpu_percent'] is None
    clock[0] += 2
    assert monitor.snapshot()['cpu_percent'] == 50  # guest must not count twice
    clock[0] += 2
    (tmp_path / 'stat').write_text('cpu 0 0 0 0 0 0 0 0\n')
    assert monitor.snapshot()['cpu_percent'] is None


def test_missing_system_fields(tmp_path):
    data = health.SystemHealth(proc=tmp_path, sys=tmp_path, disk=tmp_path / 'missing').snapshot()
    assert not data['available']
    assert data['cpu_percent'] is None
    json.dumps(data, allow_nan=False)


@pytest.mark.parametrize('data', [[], None, {'timestamp': 'bad'}, {'timestamp': float('nan')}, {'timestamp': True}])
def test_malformed_ups(tmp_path, data):
    path = tmp_path / 'ups.json'
    path.write_text(json.dumps(data))
    assert health.ups_snapshot(path) == {'available': False}


def test_ups_fresh_stale_future_and_invalid_values(tmp_path, monkeypatch):
    monkeypatch.setattr(health.time, 'time', lambda: 1000.)
    path = tmp_path / 'ups.json'
    assert not health.ups_snapshot(path)['available']
    for stamp, stale in [(995, False), (900, True), (1100, True)]:
        path.write_text(json.dumps(dict(timestamp=stamp, percent=50, current_ma=float('inf'))))
        data = health.ups_snapshot(path)
        assert data['stale'] == stale
        assert data['percent'] == 50
        assert data['current_ma'] is None
        json.dumps(data, allow_nan=False)
    path.write_text('{partial')
    assert not health.ups_snapshot(path)['available']


def test_status_exposes_health_without_running_scan(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location('health_server', Path(__file__).resolve().parents[1] / 'pislm.py')
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    ctl = server.Controller.__new__(server.Controller)
    ctl.trigger_cfg = {}
    ctl._running = False
    ctl._backends = []
    ctl._system_health = health.SystemHealth(proc=tmp_path, sys=tmp_path, disk=tmp_path)
    ctl.ups_cfg = {'status_file': str(tmp_path / 'missing')}
    result = ctl._cmd_status({})
    assert result['running'] is False
    assert result['devices'] == []
    assert result['system']['disk_total_bytes'] > 0
    assert result['ups'] == {'available': False}
    json.dumps(result, allow_nan=False)
