"""Instrument options dialog — sampling, bands, weighting, recording.

The dialog is split into tabs. A single long column did not fit on smaller
laptop screens and the buttons fell below the bottom edge, which is exactly
the screen you use in the field.

Device settings (§4) only take effect while the scan is stopped, so applying
them is delegated to the worker thread via `PiSLM.send_stopped()`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6 import QtWidgets

from pislm.recorder import RecordingOptions, estimate_size, format_size
from pislm.standards import FILTER_ORDER

#: Names that belong to the recorder rather than to the Pi.
RECORDING_FIELDS = (
    "record_enabled",
    "record_directory",
    "record_format",
    "record_full_scale",
)

DEVICE_FIELDS = (
    "sample_rate", "resample_enabled", "resample_rate", "fraction", "order",
    "f_min", "f_max", "frequency_weighting", "time_weighting",
    "level_rate", "buffer_seconds", "stream_raw",
)


@dataclass
class DeviceOptions:
    """Everything the dialog can change, in one bundle."""

    sample_rate: float = 51200.0
    resample_enabled: bool = True
    resample_rate: float = 48000.0
    fraction: int = 3
    order: int = FILTER_ORDER
    f_min: float = 50.0
    f_max: float = 630.0
    frequency_weighting: str = "Z"
    time_weighting: str = "Fast"
    level_rate: float = 20.0
    buffer_seconds: float = 60.0
    stream_raw: bool = False
    # Recorder settings — handled by this program, not by the Pi.
    record_enabled: bool = False
    record_directory: str = ""
    record_format: str = "float32"
    record_full_scale: float = 10.0
    changed: set[str] = field(default_factory=set)

    @classmethod
    def from_config(cls, config, recording=None) -> DeviceOptions:
        bands = config.bands or {}
        resample = config.resample or {}
        weighting = config.weighting or {}
        return cls(
            record_enabled=bool(getattr(recording, "enabled", False)),
            record_directory=str(getattr(recording, "directory", "")),
            record_format=str(getattr(recording, "fmt", "float32")),
            record_full_scale=float(getattr(recording, "full_scale", 10.0)),
            sample_rate=config.sample_rate or 51200.0,
            resample_enabled=bool(resample.get("enabled", False)),
            resample_rate=float(resample.get("output_rate", 48000.0)),
            fraction=int(bands.get("fraction", 3)),
            order=int(bands.get("order", FILTER_ORDER)),
            f_min=float(bands.get("f_min", 50.0)),
            f_max=float(bands.get("f_max", 630.0)),
            frequency_weighting=str(weighting.get("frequency", "Z")),
            time_weighting=str(weighting.get("time", "Fast")),
            level_rate=config.level_output_rate or 20.0,
            buffer_seconds=config.buffer_seconds or 60.0,
            stream_raw=bool(config.stream_raw),
        )

    @property
    def device_changes(self) -> set[str]:
        return {name for name in self.changed if name in DEVICE_FIELDS}

    @property
    def recording_changes(self) -> set[str]:
        return {name for name in self.changed if name in RECORDING_FIELDS}


def _hint(text: str) -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color:#9aa0a6;")
    return label


class OptionsDialog(QtWidgets.QDialog):
    """Tabbed settings so the dialog stays short enough for any screen."""

    def __init__(self, options: DeviceOptions, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Instrument options")
        self.setMinimumWidth(520)
        self._initial = options

        layout = QtWidgets.QVBoxLayout(self)
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._tab_sampling(options), "Sampling")
        self.tabs.addTab(self._tab_bands(options), "Bands")
        self.tabs.addTab(self._tab_output(options), "Weighting && output")
        self.tabs.addTab(self._tab_recording(options), "Recording")
        layout.addWidget(self.tabs)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        for widget in (self.record_format, self.sample_rate):
            widget.currentIndexChanged.connect(self._update_estimate)
        self.resample_on.toggled.connect(self._update_estimate)
        self.resample_rate.valueChanged.connect(self._update_estimate)
        self._update_estimate()

    # ── Tabs ───────────────────────────────────────────────────
    def _tab_sampling(self, options: DeviceOptions) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)

        self.sample_rate = QtWidgets.QComboBox()
        for rate in (51200, 25600, 12800, 48000, 44100):
            self.sample_rate.addItem(f"{rate:,} Hz", float(rate))
        self._select(self.sample_rate, options.sample_rate)

        self.resample_on = QtWidgets.QCheckBox("Resample onto a common grid")
        self.resample_on.setChecked(options.resample_enabled)
        self.resample_rate = QtWidgets.QDoubleSpinBox()
        self.resample_rate.setRange(8000.0, 51200.0)
        self.resample_rate.setDecimals(0)
        self.resample_rate.setSuffix(" Hz")
        self.resample_rate.setValue(options.resample_rate)
        self.resample_on.toggled.connect(self.resample_rate.setEnabled)
        self.resample_rate.setEnabled(options.resample_enabled)

        form.addRow("Sample rate", self.sample_rate)
        form.addRow(self.resample_on)
        form.addRow("Common output rate", self.resample_rate)
        form.addRow(
            _hint(
                "The two ADCs run on independent clocks and drift up to 100 µs "
                "per second. Only resampling actually removes that drift. The "
                "rate estimate needs time to settle (6 ppm at 30 s, 0.15 ppm at "
                "300 s), so let the scan warm up before the real measurement."
            )
        )
        return page

    def _tab_bands(self, options: DeviceOptions) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)

        self.fraction = QtWidgets.QComboBox()
        self.fraction.addItem("1/3 octave", 3)
        self.fraction.addItem("1/1 octave", 1)
        self._select(self.fraction, options.fraction)
        self.order = QtWidgets.QSpinBox()
        self.order.setRange(2, 10)
        self.order.setValue(options.order)
        self.f_min = QtWidgets.QDoubleSpinBox()
        self.f_min.setRange(10.0, 20000.0)
        self.f_min.setDecimals(0)
        self.f_min.setSuffix(" Hz")
        self.f_min.setValue(options.f_min)
        self.f_max = QtWidgets.QDoubleSpinBox()
        self.f_max.setRange(20.0, 20000.0)
        self.f_max.setDecimals(0)
        self.f_max.setSuffix(" Hz")
        self.f_max.setValue(options.f_max)

        form.addRow("Resolution", self.fraction)
        form.addRow("Butterworth order", self.order)
        form.addRow("Lowest band", self.f_min)
        form.addRow("Highest band", self.f_max)
        form.addRow(
            _hint(
                "Band edges come from the IEC 61260-1 exact midband frequencies "
                "(nominal 3150 Hz is really 3162.28 Hz). Order 6 is the usual "
                "choice for Class 1."
            )
        )
        return page

    def _tab_output(self, options: DeviceOptions) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)

        self.frequency = QtWidgets.QComboBox()
        self.frequency.addItems(["Z", "A", "C"])
        self.frequency.setCurrentText(options.frequency_weighting)
        self.time = QtWidgets.QComboBox()
        self.time.addItems(["Fast", "Slow", "Impulse"])
        self.time.setCurrentText(options.time_weighting)
        self.level_rate = QtWidgets.QDoubleSpinBox()
        self.level_rate.setRange(1.0, 100.0)
        self.level_rate.setSuffix(" Hz")
        self.level_rate.setValue(options.level_rate)
        self.buffer_seconds = QtWidgets.QDoubleSpinBox()
        self.buffer_seconds.setRange(5.0, 600.0)
        self.buffer_seconds.setSuffix(" s")
        self.buffer_seconds.setValue(options.buffer_seconds)
        self.stream_raw = QtWidgets.QCheckBox("Stream raw waveform continuously")
        self.stream_raw.setChecked(options.stream_raw)

        form.addRow("Frequency weighting", self.frequency)
        form.addRow("Time weighting", self.time)
        form.addRow("Level output rate", self.level_rate)
        form.addRow("Ring buffer length", self.buffer_seconds)
        form.addRow(self.stream_raw)
        form.addRow(
            _hint(
                "Keep frequency weighting at Z. ISO 717-2 applies A-weighting as "
                "per-band values added to the 1/3-octave spectrum, not as a "
                "filter on the signal — the program does that arithmetically.\n"
                "Raw streaming is about 19.7 Mbps for 6 channels."
            )
        )
        return page

    def _tab_recording(self, options: DeviceOptions) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)

        self.record_on = QtWidgets.QCheckBox("Save raw waveform as WAV")
        self.record_on.setChecked(options.record_enabled)

        folder_row = QtWidgets.QWidget()
        folder_layout = QtWidgets.QHBoxLayout(folder_row)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        self.record_dir = QtWidgets.QLineEdit(options.record_directory)
        browse = QtWidgets.QPushButton("Browse…")
        browse.clicked.connect(self._pick_folder)
        folder_layout.addWidget(self.record_dir, 1)
        folder_layout.addWidget(browse)

        self.record_format = QtWidgets.QComboBox()
        self.record_format.addItem("32-bit float (lossless, physical units)", "float32")
        self.record_format.addItem("24-bit PCM", "int24")
        self.record_format.addItem("16-bit PCM", "int16")
        self._select(self.record_format, options.record_format)

        self.record_full_scale = QtWidgets.QDoubleSpinBox()
        self.record_full_scale.setRange(0.001, 10000.0)
        self.record_full_scale.setDecimals(3)
        self.record_full_scale.setValue(options.record_full_scale)
        self.record_full_scale.setToolTip(
            "Physical value (Pa or V) that maps to full scale when writing PCM"
        )

        self.record_estimate = QtWidgets.QLabel()
        self.record_estimate.setStyleSheet("color:#9aa0a6;")

        form.addRow(self.record_on)
        form.addRow("Folder", folder_row)
        form.addRow("Format", self.record_format)
        form.addRow("PCM full scale", self.record_full_scale)
        form.addRow(self.record_estimate)
        form.addRow(
            _hint(
                "One file per source position, one column per ticked receiver "
                "position. Raw streaming is switched on for you while "
                "recording. float32 stores Pa/V values as they are, with no "
                "scaling. PCM clips above full scale, so leave headroom.\n"
                "Lost frames are filled with silence so the time axis stays "
                "correct; gap positions go into the sidecar JSON."
            )
        )
        return page

    # ── Helpers ────────────────────────────────────────────────
    def _pick_folder(self) -> None:
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "WAV output folder", self.record_dir.text()
        )
        if folder:
            self.record_dir.setText(folder)

    def _update_estimate(self) -> None:
        """Show the expected size before the user commits to a long recording."""
        rate = (
            self.resample_rate.value()
            if self.resample_on.isChecked()
            else (self.sample_rate.currentData() or 51200.0)
        )
        size = estimate_size(6, rate, 600.0, self.record_format.currentData())
        self.record_estimate.setText(
            f"About {format_size(size)} per 10 minutes at 6 channels, {rate:g} Hz"
        )

    @staticmethod
    def _select(combo: QtWidgets.QComboBox, value) -> None:
        for index in range(combo.count()):
            if combo.itemData(index) == value:
                combo.setCurrentIndex(index)
                return

    def values(self) -> DeviceOptions:
        """Read the dialog and record which fields actually changed.

        Only changed settings are sent to the Pi. Every config command stops
        and restarts the scan, which empties the ring buffers, so resending
        unchanged values costs real measurement time.
        """
        result = DeviceOptions(
            sample_rate=self.sample_rate.currentData(),
            resample_enabled=self.resample_on.isChecked(),
            resample_rate=self.resample_rate.value(),
            fraction=self.fraction.currentData(),
            order=self.order.value(),
            f_min=self.f_min.value(),
            f_max=self.f_max.value(),
            frequency_weighting=self.frequency.currentText(),
            time_weighting=self.time.currentText(),
            level_rate=self.level_rate.value(),
            buffer_seconds=self.buffer_seconds.value(),
            stream_raw=self.stream_raw.isChecked(),
            record_enabled=self.record_on.isChecked(),
            record_directory=self.record_dir.text().strip(),
            record_format=self.record_format.currentData(),
            record_full_scale=self.record_full_scale.value(),
        )
        for name in DEVICE_FIELDS + RECORDING_FIELDS:
            if getattr(result, name) != getattr(self._initial, name):
                result.changed.add(name)
        return result


def recording_options(options: DeviceOptions) -> RecordingOptions:
    """Extract just the recorder part of the settings."""
    return RecordingOptions(
        enabled=options.record_enabled,
        directory=options.record_directory,
        fmt=options.record_format,
        full_scale=options.record_full_scale,
    )


def apply_options(pi, options: DeviceOptions):
    """Send only the changed settings to the Pi. Call from the worker thread."""
    changed = options.changed
    if "sample_rate" in changed:
        pi.set_sample_rate(options.sample_rate)
    if {"resample_enabled", "resample_rate"} & changed:
        pi.set_resample(
            enabled=options.resample_enabled, output_rate=options.resample_rate
        )
    if {"fraction", "order", "f_min", "f_max"} & changed:
        pi.set_bands(
            enabled=True,
            output="level",
            fraction=options.fraction,
            order=options.order,
            f_min=options.f_min,
            f_max=options.f_max,
        )
    if {"frequency_weighting", "time_weighting"} & changed:
        pi.set_weighting(
            frequency=options.frequency_weighting,
            time_weighting=options.time_weighting,
        )
    if "level_rate" in changed:
        pi.set_level(enabled=True, output_rate=options.level_rate)
    if "buffer_seconds" in changed:
        pi.set_storage(buffer_seconds=options.buffer_seconds)
    if "stream_raw" in changed:
        pi.set_options(stream_raw=options.stream_raw)
    return pi.refresh()
