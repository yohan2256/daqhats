import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.main import MainWindow, build_app
from app.health import HealthPanel


def test_health_panel_unknown_stale_and_warning():
    app = build_app([])
    panel = HealthPanel()
    panel.update_snapshot({})  # Old server compatibility
    assert '정보 없음' in panel.system.text()
    assert '0%' not in panel.ups.text()
    panel.update_snapshot({'system': {'available': True, 'cpu_percent': 95, 'temperature_c': 81},
                           'ups': {'available': True, 'stale': False, 'percent': 10}})
    assert 'CPU 부하 높음' in panel.system.text()
    assert '온도 높음' in panel.system.text()
    assert '배터리 부족' in panel.ups.text()
    panel.update_snapshot({'ups': {'available': True, 'stale': True, 'percent': 80}})
    assert '갱신 지연' in panel.ups.text()
    assert '80' not in panel.ups.text()
    panel.unavailable('연결 안 됨')
    assert '연결 안 됨' in panel.ups.text()
    panel.close()


def test_poll_bounds_and_ignores_old_connection():
    app = build_app([])
    window = MainWindow()
    window.health_timer.stop()
    queued = []
    window.health_worker.submit = lambda *args: queued.append(args)
    old = SimpleNamespace(health=lambda: {'system': {'available': True, 'cpu_percent': 42}})
    window.pi = old
    window._poll_health()
    window._poll_health()
    assert len(queued) == 1
    result = queued.pop()[1]()
    window.pi = SimpleNamespace()
    window._on_health_done('health', result)
    assert '42' not in window.health_panel.system.text()
    assert not window._health_pending
    window.pi = old
    window._on_health_done('health', (old, None, 'timeout'))
    assert '조회 실패' in window.health_panel.system.text()
    window.pi = None
    window.close()


def test_health_updates_while_measurement_worker_is_busy():
    import threading
    import time
    app = build_app([])
    window = MainWindow()
    release = threading.Event()
    started = threading.Event()
    def measurement():
        started.set()
        release.wait(5)
    window.worker.submit('test_block', measurement)
    client = SimpleNamespace(health=lambda: {'system': {'available': True, 'cpu_percent': 42}})
    window.pi = client
    try:
        assert started.wait(1)
        window._poll_health()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and '42.0%' not in window.health_panel.system.text():
            app.processEvents()
            time.sleep(.01)
        assert '42.0%' in window.health_panel.system.text()
        assert not release.is_set()
    finally:
        release.set()
        window.pi = None
        window.close()
