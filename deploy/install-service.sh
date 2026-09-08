#!/bin/bash
# Run as the measurement user. Only the service installation uses sudo.
set -euo pipefail
if [[ $EUID -eq 0 ]]; then
    echo "Run as the login user: ./deploy/install-service.sh (without sudo)" >&2
    exit 1
fi
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
venv_dir=${PISLM_VENV:-"$HOME/pislm-venv"}
if [[ ! -x "$venv_dir/bin/python" ]]; then
    echo "Python environment not found: $venv_dir" >&2
    exit 1
fi
"$venv_dir/bin/python" -c 'import daqhats, numpy, scipy'
# A tempfile prevents a rendering error from truncating an installed unit.
unit_file=$(mktemp)
trap 'rm -f "$unit_file"' EXIT
python3 "$repo_dir/deploy/render_service.py" --venv "$venv_dir" "$@" > "$unit_file"
sudo install -m 644 "$unit_file" /etc/systemd/system/pislm.service
sudo systemctl daemon-reload
sudo systemctl enable pislm.service
sudo systemctl restart pislm.service
sudo systemctl --no-pager status pislm.service
