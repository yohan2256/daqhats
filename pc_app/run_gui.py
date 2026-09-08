#!/usr/bin/env python3
"""Floor impact sound meter launcher.

    python run_gui.py                    # real hardware (default 127.0.0.1:5000/5001)
    python run_gui.py --host 192.168.0.42
    python run_gui.py --demo             # built-in simulator, no hardware needed
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _check_dependencies() -> None:
    missing = []
    for module, package in (
        ("numpy", "numpy"),
        ("scipy", "scipy"),
        ("PySide6", "PySide6"),
    ):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        print("Missing packages:", ", ".join(missing), file=sys.stderr)
        print(f"  pip install {' '.join(missing)}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    _check_dependencies()
    from app.main import main

    raise SystemExit(main())
