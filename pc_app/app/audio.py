"""Playback through the laptop's own sound card.

The Pi's DT9837A has an analog output, but plenty of rigs drive a proper
amplifier and dodecahedron from the measuring laptop instead. Playing from
here has one real advantage over handing the operator a WAV file: the program
knows exactly which signal went out and when it started, so a sweep or MLS can
be deconvolved without the operator having to keep the file and the settings
in step by hand.

`sounddevice` (PortAudio) is an optional dependency. Everything here degrades
to a clear message rather than an import error when it is missing, because the
Pi-output and manual-entry routes must keep working on a machine without it.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np

try:  # pragma: no cover - depends on the machine
    import sounddevice as _sd
except Exception:  # noqa: BLE001  - ImportError, or PortAudio missing
    _sd = None


INSTALL_HINT = (
    "Playback through this computer needs the 'sounddevice' package:\n"
    "    pip install sounddevice\n"
    "The Pi's own analog output and manual entry work without it."
)


@dataclass(frozen=True)
class OutputDevice:
    """One playable output, as PortAudio reports it."""

    index: int
    name: str
    channels: int
    default_rate: float
    host_api: str = ""
    is_default: bool = False

    @property
    def label(self) -> str:
        mark = " (default)" if self.is_default else ""
        api = f" · {self.host_api}" if self.host_api else ""
        return f"{self.name}{api} — {self.channels}ch{mark}"


def available() -> bool:
    return _sd is not None


def output_devices() -> list[OutputDevice]:
    """Every device that can play audio, best guess at the default first.

    Returns an empty list rather than raising when sounddevice is missing, so
    the caller can populate a combo box unconditionally.
    """
    if _sd is None:
        return []
    try:
        infos = _sd.query_devices()
        apis = _sd.query_hostapis()
        default = _sd.default.device
        default_out = default[1] if isinstance(default, (list, tuple)) else default
    except Exception:  # noqa: BLE001 - PortAudio can fail at runtime too
        return []

    devices: list[OutputDevice] = []
    for index, info in enumerate(infos):
        channels = int(info.get("max_output_channels", 0))
        if channels <= 0:
            continue          # an input-only device
        api_index = info.get("hostapi")
        try:
            api = apis[api_index]["name"]
        except (IndexError, KeyError, TypeError):
            api = ""
        devices.append(OutputDevice(
            index=index,
            name=str(info.get("name", f"device {index}")).strip(),
            channels=channels,
            default_rate=float(info.get("default_samplerate", 48000.0)),
            host_api=api,
            is_default=(index == default_out),
        ))
    devices.sort(key=lambda d: (not d.is_default, d.name.lower()))
    return devices


def default_device() -> OutputDevice | None:
    for device in output_devices():
        if device.is_default:
            return device
    return None


class Player:
    """Plays one waveform at a time and remembers when it started.

    The start time is the whole point: it is what lets the analysis find the
    excitation inside the Pi's recording. It is a host timestamp taken as the
    stream begins, so it carries the same caveat as the Pi's own `start_index`
    (PROTOCOL.md §10) — good to a few milliseconds, not to the sample. That is
    fine for a sweep, whose inverse filter finds its own peak, and the MLS
    path corrects the residual offset itself.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stream = None
        self._done = threading.Event()
        self._done.set()
        self.started_at: float | None = None
        self.error: str | None = None

    @property
    def playing(self) -> bool:
        return not self._done.is_set()

    def play(self, waveform: np.ndarray, rate: float, device: int | None = None,
             channels: int = 1) -> None:
        """Start playback and return immediately.

        Raises RuntimeError when playback is impossible, rather than failing
        silently — a measurement taken against a signal that never sounded is
        far worse than an error.
        """
        if _sd is None:
            raise RuntimeError(INSTALL_HINT)
        with self._lock:
            if self.playing:
                raise RuntimeError("already playing; stop it first")
            data = np.asarray(waveform, dtype=np.float32).reshape(-1, 1)
            if channels > 1:
                # The same signal to every channel: one loudspeaker fed from
                # a stereo output must not come out 6 dB quiet on one side.
                data = np.repeat(data, channels, axis=1)
            self.error = None
            self._done.clear()
            try:
                self._stream = _sd.OutputStream(
                    samplerate=float(rate),
                    device=device,
                    channels=data.shape[1],
                    dtype="float32",
                )
                self._stream.start()
            except Exception as exc:  # noqa: BLE001
                self._done.set()
                self._stream = None
                raise RuntimeError(f"could not open the output: {exc}") from exc

        import time as _time

        self.started_at = _time.monotonic()

        def pump() -> None:
            try:
                # Write in blocks so stop() can interrupt between them.
                block = max(1024, int(rate * 0.05))
                for start in range(0, len(data), block):
                    if self._done.is_set():
                        break
                    self._stream.write(data[start:start + block])
            except Exception as exc:  # noqa: BLE001
                self.error = str(exc)
            finally:
                self.stop()

        threading.Thread(target=pump, name="audio-out", daemon=True).start()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until playback finishes. True if it finished in time."""
        return self._done.wait(timeout)

    def stop(self) -> None:
        with self._lock:
            self._done.set()
            stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass
