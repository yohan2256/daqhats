# PiSLM — 라즈베리파이 이식용 핵심 구성

MCC172 / DT9837A 바닥충격음 측정 서버와 PC 프로그램입니다.

| 경로 | 역할 |
|---|---|
| `pi_server/` | 측정 서버, config.ini, 통신·DSP, 장치 진단, 서비스 예시 |
| `deploy/` | 실제 사용자·설치 경로를 반영하는 서비스 설치 도구 |
| `daqhats/`, `lib/`, `include/` | MCC 드라이버와 네이티브 라이브러리 소스 |
| `tools/` | EEPROM 읽기·장치 확인·MCC172 펌웨어 도구 |
| `pc_app/` | PC 측정 GUI, 구법/신법 분석, 시뮬레이터·회귀 시험 |

PC 프로그램은 라즈베리파이에 설치할 필요가 없습니다. 서버 설치는
PC 폴더를 사용하지 않으며, GTK나 예제 프로그램도 빌드하지 않습니다.

## 새 라즈베리파이에 설치

Raspberry Pi OS와 측정용 사용자를 준비한 뒤 일반 사용자로 실행합니다.

```bash
git clone https://github.com/yohan2256/daqhats.git
cd daqhats
sudo apt-get update
sudo ./install.sh
sudo apt-get install -y python3-venv python3-numpy python3-scipy python3-libgpiod
python3 -m venv --system-site-packages "$HOME/pislm-venv"
"$HOME/pislm-venv/bin/pip" install .
```

**DT9837A를 사용하는 경우** [설치 문서 §7](pi_server/INSTALL.md#7-install-uldaq-dt9837a)의
libuldaq 설치를 먼저 수행하고, `"$HOME/pislm-venv/bin/pip" install uldaq`를 실행합니다.
Python 패키지만 설치해서는 USB 장치 드라이버가 설치되지 않습니다.

1. 기존 장비의 `config.ini`를 백업하고 `pi_server/config.ini`에 반영합니다. 마이크 감도, 채널 순서, 장치 종류와 네트워크 주소를 확인합니다. 새 장비에서도 교정음을 확인합니다.
2. 먼저 전면 실행하여 장치 초기화를 확인합니다.

```bash
"$HOME/pislm-venv/bin/python" pi_server/pislm.py
```

3. 종료한 뒤 자동 시작 서비스를 설치합니다. 다음 명령은 **sudo 없이** 실행하며, 필요한 단계에서만 sudo를 사용합니다.

```bash
./deploy/install-service.sh
journalctl -u pislm -f
```

설치 도구는 현재 사용자와 저장소 경로로 서비스를 생성합니다. 설정을 외부에
보관하려면 `./deploy/install-service.sh --config /absolute/path/config.ini`를 사용합니다.
기존 장비를 업데이트할 때도 이 도구를 다시 실행하여 예전 서버 경로를 교체합니다.
GPIO 종료 버튼을 별도로 사용했다면 [설치 문서 §14](pi_server/INSTALL.md#14-physical-shutdown-button-optional)의
서비스 경로도 함께 갱신합니다. 설정 파일과 시스템의 기존 교정값은 정리 작업으로 삭제하지 않습니다.

## PC 프로그램

[pc_app/README.md](pc_app/README.md)를 따릅니다. Windows에서는 해당 폴더의
`run.bat --host <Pi-IP>`로 실행합니다. [구법 뱅머신·역A 분석](pc_app/LEGACY_BANG.md)도 포함됩니다.

## 정리 범위

일반 DAQ C/Python 예제, 생성된 HTML 문서와 Sphinx 원본, 데스크톱 제어판,
MCC118/128 펌웨어 도구를 제거했습니다. MCC172 공통 드라이버는 다른 모듈을 참조하므로
호환성을 위해 함께 보존했습니다. 서버 경로는 `pi_server/`로 변경했습니다.
회귀 시험, 프로토콜, 실제 장비 설치 설명, 라이선스는 유지했습니다.

정리 전 코드는 Git 이력에서 복구할 수 있습니다. 이 저장소는
[Measurement Computing DAQ HAT 라이브러리](https://github.com/mccdaq/daqhats)를 바탕으로 하며
원본 저작권·[라이선스](LICENSE)를 유지합니다.
