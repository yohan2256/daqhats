"""Command thread — keeps blocking calls off the GUI thread.

`PiSLM` commands wait for a response, up to command_timeout. Calling them
from the GUI thread freezes the window for that long.
"""

from __future__ import annotations

import queue
import traceback
from typing import Any, Callable

from PySide6 import QtCore


class CommandWorker(QtCore.QThread):
    """Runs submitted jobs in order and reports results through signals."""

    #: (job name, result)
    finished_job = QtCore.Signal(str, object)
    #: (job name, message, traceback)
    failed_job = QtCore.Signal(str, str, str)
    #: Progress message for the user
    progress = QtCore.Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._jobs: queue.Queue = queue.Queue()
        self._running = True

    def submit(self, name: str, func: Callable[[], Any], message: str = "") -> None:
        self._jobs.put((name, func, message))

    def stop(self) -> None:
        self._running = False
        self._jobs.put(None)
        self.wait(3000)

    @property
    def pending(self) -> int:
        return self._jobs.qsize()

    def run(self) -> None:
        while self._running:
            item = self._jobs.get()
            if item is None:
                break
            name, func, message = item
            if message:
                self.progress.emit(message)
            try:
                result = func()
            except Exception as exc:  # noqa: BLE001 — every failure reaches the GUI
                self.failed_job.emit(name, str(exc), traceback.format_exc())
            else:
                self.finished_job.emit(name, result)
