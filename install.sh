#!/bin/bash
# Install only the native DAQ library and headless MCC172 tools.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ $EUID -ne 0 ]]; then
    echo "Run: sudo ./install.sh" >&2
    exit 1
fi
apt-get install -y build-essential pkg-config gpiod libgpiod-dev
make -C lib all
make -C lib install
make -C tools all
make -C tools install
if command -v raspi-config >/dev/null; then
    raspi-config nonint do_spi 0
fi
daqhats_read_eeproms
echo "MCC native library installed. Continue with README.md to install Python dependencies."
