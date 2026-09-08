"""Compact read-only Pi health panel; unknown values are never rendered as zero."""
import math
from PySide6 import QtWidgets


def numeric(data, key):
    value = data.get(key)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def display(data, key, unit='', scale=1):
    value = numeric(data, key)
    return '—' if value is None else f'{value / scale:.1f}{unit}'


class HealthPanel(QtWidgets.QGroupBox):
    def __init__(self, parent=None):
        super().__init__('Raspberry Pi 상태', parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 6)
        self.system = QtWidgets.QLabel()
        self.ups = QtWidgets.QLabel()
        for label in (self.system, self.ups):
            label.setWordWrap(True)
            layout.addWidget(label)
        self.unavailable('연결 안 됨')

    def unavailable(self, reason):
        self.system.setText(f'CPU · 메모리 · 온도: {reason}')
        self.ups.setText(f'UPS: {reason}')
        self.system.setStyleSheet('color:#9a6700;')
        self.ups.setStyleSheet('color:#9a6700;')

    def update_snapshot(self, payload):
        system = payload.get('system')
        ups = payload.get('ups')
        system = system if isinstance(system, dict) else {}
        ups = ups if isinstance(ups, dict) else {}
        if not system.get('available'):
            self.system.setText('시스템 정보 없음 — 서버 업데이트 또는 수집 환경 확인')
            self.system.setStyleSheet('color:#9a6700;')
        else:
            parts = [f"CPU {display(system, 'cpu_percent', '%')}",
                     f"온도 {display(system, 'temperature_c', ' °C')}",
                     f"클럭 {display(system, 'cpu_frequency_mhz', ' MHz')}",
                     f"메모리 {display(system, 'memory_used_percent', '%')} 사용",
                     f"가용 {display(system, 'memory_available_bytes', ' GiB', 2**30)} / {display(system, 'memory_total_bytes', ' GiB', 2**30)}",
                     f"디스크 여유 {display(system, 'disk_free_bytes', ' GiB', 2**30)}",
                     f"가동 {display(system, 'uptime_seconds', ' h', 3600)}"]
            warnings = []
            for key, limit, text in [('cpu_percent', 90, 'CPU 부하 높음'),
                                     ('temperature_c', 80, '온도 높음'),
                                     ('memory_used_percent', 90, '메모리 여유 부족')]:
                value = numeric(system, key)
                if value is not None and value >= limit:
                    warnings.append(text)
            self.system.setText(' · '.join(parts) + (' | ' + ', '.join(warnings) if warnings else ''))
            self.system.setStyleSheet('color:#b45309;' if warnings else '')
        self.ups.setStyleSheet('')
        if not ups.get('available'):
            self.ups.setText('UPS: 정보 없음 — UPS 모니터 서비스/설정 확인')
            self.ups.setStyleSheet('color:#9a6700;')
        elif ups.get('stale'):
            self.ups.setText(f"UPS: 갱신 지연 — 현재 잔량 확인 불가 (자료 경과 {display(ups, 'age_seconds', ' s')})")
            self.ups.setStyleSheet('color:#b45309;')
        else:
            text = (f"UPS 잔량(전압 기반 추정) {display(ups, 'percent', '%')}"
                    f" · {display(ups, 'bus_voltage_v', ' V')}"
                    f" · {display(ups, 'current_ma', ' mA')}"
                    f" · {display(ups, 'power_w', ' W')}"
                    f" · 자료 경과 {display(ups, 'age_seconds', ' s')}")
            percent = numeric(ups, 'percent')
            hold = numeric(ups, 'low_battery_hold_seconds')
            if percent is not None and percent <= 20:
                text += ' · 배터리 부족'
                self.ups.setStyleSheet('color:#b45309;')
            if hold is not None and hold > 0:
                text += f' · 저전압 지속 {hold:.0f} s (자동 종료 조건 확인)'
            self.ups.setText(text)
