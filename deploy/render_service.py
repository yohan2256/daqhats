#!/usr/bin/env python3
"""Render the PiSLM unit for this checkout, without changing the machine."""
import argparse
import os
from pathlib import Path
import pwd
import sys


def quoted(value):
    if any(c in str(value) for c in '\n\r\0'):
        raise ValueError('unit values cannot contain newlines or NUL')
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def render(root, user, venv, config):
    server = root / 'pi_server'
    quoted(server)  # reject control characters before writing any unit
    working_directory = str(server).replace('%', '%%')
    return '\n'.join([
        '[Unit]', 'Description=PiSLM measurement server',
        'After=network-online.target', 'Wants=network-online.target', '',
        '[Service]', 'Type=simple', 'User=' + user,
        'WorkingDirectory=' + working_directory,
        'ExecStart=' + quoted(venv / 'bin/python').replace('$', '$$') + ' ' + quoted(server / 'pislm.py').replace('$', '$$'),
        'Environment=' + quoted('PISLM_CONFIG=' + str(config)),
        'Restart=on-failure', 'RestartSec=3',
        'StandardOutput=journal', 'StandardError=journal', '',
        '[Install]', 'WantedBy=multi-user.target', '',
    ])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--venv', type=Path, default=Path.home()/'pislm-venv')
    parser.add_argument('--config', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    user = pwd.getpwuid(os.getuid()).pw_name
    config = (args.config or root/'pi_server/config.ini').expanduser().resolve()
    sys.stdout.write(render(root, user, args.venv.expanduser().resolve(), config))


if __name__ == '__main__':
    main()
