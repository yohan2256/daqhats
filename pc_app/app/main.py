"""Floor impact sound measurement — main window.

    python run_gui.py            # real hardware
    python run_gui.py --demo     # built-in simulator, no hardware needed
"""

from __future__ import annotations

from pislm.standards.interrupted import interrupted_spectrum

import logging
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import numpy as np
from PySide6 import QtCore, QtWidgets

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import audio  # noqa: E402
from pislm.standards.impact import fetch_heavy_levels, prepare_heavy_capture
from app.live import LiveState  # noqa: E402
from app.options import (  # noqa: E402
    DeviceOptions,
    OptionsDialog,
    apply_options,
    recording_options,
)
from app.widgets import LevelBars, SpectrumGrid, StatusLight, channel_label  # noqa: E402
from app.worker import CommandWorker  # noqa: E402
from app.health import HealthPanel  # noqa: E402
from pislm import CommandError, Handshake, PiSLM  # noqa: E402
from pislm.excitation import (  # noqa: E402
    ExcitationSettings,
    Signal,
    generate,
    impulse_response,
)
from pislm.recorder import (  # noqa: E402
    RecordingOptions,
    WavRecorder,
    format_size,
    write_dump_wav,
    write_wav,
)
from pislm.session import ImpactSource, Measurement, Room, Session  # noqa: E402
from pislm.standards import (  # noqa: E402
    FILTER_ORDER,
    HEAVY_THIRD_OCTAVE_BANDS,
    LIGHT_THIRD_OCTAVE_BANDS,
    InverseACurve,
    Spectrum,
    a_weighted_single_number,
    average_reverberation,
    nominal_center,
    reverberation_spectrum,
)

log = logging.getLogger("app.main")

MAX_CHANNELS = 6

#: Every band either impact source is rated over, 50-3150 Hz. Used for the one
#: shared background measurement.
COMBINED_BANDS = tuple(
    sorted(set(HEAVY_THIRD_OCTAVE_BANDS) | set(LIGHT_THIRD_OCTAVE_BANDS))
)


class _TrimmedDump:
    """A RAW_DUMP sliced so sample 0 is where the excitation began.

    Wrapping rather than mutating keeps the original dump intact for the other
    microphones, and keeps `channel()` returning arrays that all start at the
    same instant.
    """

    def __init__(self, dump, offset: int) -> None:
        self._dump = dump
        self._offset = max(0, offset)
        self.sample_rate = dump.sample_rate
        self.start_index = dump.start_index

    def channel(self, index: int):
        return self._dump.channel(index)[self._offset:]


def _trimmed(dump, start_index: int):
    """Slice a dump to the excitation start, or hand it back untouched."""
    if getattr(dump, "start_index", -1) < 0:
        return dump
    try:
        return _TrimmedDump(dump, dump.offset_of(start_index))
    except (ValueError, AttributeError):
        return dump  # fall back to the whole window


class MainWindow(QtWidgets.QMainWindow):
    #: Emitted from the stream reader thread when the server reports output
    #: state; a queued signal is the safe way back onto the GUI thread.
    _output_event = QtCore.Signal(str, object)

    def __init__(self, host: str = "127.0.0.1", control: int = 5000, stream: int = 5001):
        super().__init__()
        self.setWindowTitle("Floor Impact Sound Meter")
        self.resize(1180, 800)

        self.pi: PiSLM | None = None
        self.live = LiveState()
        # Heavy and light are separate measurements with different band sets,
        # different per-band quantities and different ratings — but they are
        # usually walked position by position as one job, so both are held
        # open and `session` points at whichever is active.
        self._sessions: dict[str, Session] = {
            "heavy": Session(title="New measurement", source=ImpactSource.RUBBER_BALL),
            "light": Session(title="New measurement", source=ImpactSource.TAPPING),
        }
        self._active = "heavy"
        self.curve = InverseACurve.generated()
        self._reverb_runs: list[dict] = []
        self._closing = False
        #: None while a pass is in flight, then the `completed` flag from the
        #: `output_finished` event (PROTOCOL.md §10).
        self._output_completed: bool | None = None
        #: Was the raw stream already on before recording turned it on?
        self._raw_was_on = False
        #: External-speaker mode: the operator brackets the sound by hand.
        self._external_recording = False
        self._external_started = 0.0
        #: The sweep that was written out, so the analysis matches what played.
        self._external_settings: ExcitationSettings | None = None
        #: Microphones that produced nothing usable on the last run.
        self._reverb_failures: list[str] = []
        #: Plays the excitation through this machine's own sound card.
        self._player = audio.Player()
        #: WAV written for the most recent reverberation pass, if any.
        self._reverb_wav: Path | None = None
        self.recording = RecordingOptions()
        self.recorder = WavRecorder(self.recording)

        self.worker = CommandWorker(self)
        self.worker.finished_job.connect(self._on_job_done)
        self.worker.failed_job.connect(self._on_job_failed)
        self.worker.progress.connect(lambda text: self.status.set_state("busy", text))
        self._output_event.connect(self._on_output_event)
        self.worker.start()

        self._build_ui(host, control, stream)

        # Separate worker: a capture can occupy the command worker for minutes.
        # Only one health request may be queued/in flight at a time.
        self._health_pending = False
        self.health_worker = CommandWorker(self)
        self.health_worker.finished_job.connect(self._on_health_done)
        self.health_worker.failed_job.connect(self._on_health_failed)
        self.health_worker.start()
        self.health_timer = QtCore.QTimer(self)
        self.health_timer.timeout.connect(self._poll_health)
        self.health_timer.start(2000)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._refresh_live)
        self.timer.start(60)

    # ── The active session ──
    @property
    def session(self) -> Session:
        return self._sessions[self._active]

    @session.setter
    def session(self, value: Session) -> None:
        self._sessions[self._active] = value

    @property
    def other_session(self) -> Session:
        return self._sessions["light" if self._active == "heavy" else "heavy"]

    def _active_key_follows_source(self) -> None:
        """Keep the slot and the loaded session's own source in agreement."""
        key = "heavy" if self.session.source.is_heavy else "light"
        if key != self._active:
            self._sessions[key] = self._sessions.pop(self._active)
            self._sessions.setdefault(
                self._active,
                Session(source=ImpactSource.TAPPING if self._active == "light"
                        else ImpactSource.RUBBER_BALL),
            )
            self._active = key

    #: Everything that describes the *room and the rig* rather than the impact
    #: source. Measuring any of it twice would be wasted effort, and worse,
    #: two slightly different answers for one room.
    SHARED_FIELDS = ("title", "site", "operator", "source_positions",
                     "channels", "background", "calibration")

    def _share_to_other_session(self) -> None:
        other = self.other_session
        for name in self.SHARED_FIELDS:
            value = getattr(self.session, name)
            setattr(other, name, list(value) if isinstance(value, list)
                    else dict(value) if isinstance(value, dict) else value)
        other.room.name = self.session.room.name
        other.room.volume = self.session.room.volume
        other.room.reverberation = dict(self.session.room.reverberation)

    def _open_acoustic_modes(self):
        if self.live.capturing or self.recorder.recording or self.worker.busy:
            self._warn("Finish the current operation first")
            return
        from app.acoustic_modes import AcousticModes
        dialog = AcousticModes(self)
        dialog.exec()

    def _open_rt_sequence(self):
        if self.pi is None or self.live.capturing or self.recorder.recording or self.worker.busy:
            self._warn("Connect and finish the current operation first")
            return
        channels = self._reverb_selected_channels()
        if not channels:
            self._warn("Select microphones for reverberation")
            return
        from app.rt_sequence import RTSequence
        dialog = RTSequence(self, self.pi, channels, self.session.bands, self.session.fraction)
        if dialog.exec() and dialog.accepted_results is not None:
            self.session.reverberation_records.append(dialog.report())
            self._store_reverberation(dialog.accepted_results)

    # ── UI construction ──
    def _build_ui(self, host: str, control: int, stream: int) -> None:
        modes = self.menuBar().addMenu("&Measurement modes")
        modes.addAction("Sound level / airborne / facade…", self._open_acoustic_modes)
        modes.addAction("Reverberation — XL2 procedure / comparison…", self._open_rt_sequence)
        menu = self.menuBar().addMenu("&Settings")
        self.options_action = menu.addAction("Instrument options…")
        self.options_action.setShortcut("Ctrl+,")
        self.options_action.triggered.connect(self._open_options)
        menu.addSeparator()
        menu.addAction("Save settings to Pi", self._save_pi_config)
        menu.addAction("Reset peak hold", lambda: self.level_bars.reset_peaks())

        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)

        # Connection bar
        bar = QtWidgets.QHBoxLayout()
        self.host_edit = QtWidgets.QLineEdit(host)
        self.host_edit.setFixedWidth(150)
        self.control_spin = QtWidgets.QSpinBox()
        self.control_spin.setRange(1, 65535)
        self.control_spin.setValue(control)
        self.stream_spin = QtWidgets.QSpinBox()
        self.stream_spin.setRange(1, 65535)
        self.stream_spin.setValue(stream)
        self.connect_btn = QtWidgets.QPushButton("Connect")
        self.connect_btn.clicked.connect(self._toggle_connection)
        self.scan_btn = QtWidgets.QPushButton("Start scan")
        self.scan_btn.setEnabled(False)
        self.scan_btn.clicked.connect(self._toggle_scan)
        self.status = StatusLight()

        for widget in (
            QtWidgets.QLabel("Pi address"), self.host_edit,
            QtWidgets.QLabel("Control"), self.control_spin,
            QtWidgets.QLabel("Stream"), self.stream_spin,
            self.connect_btn, self.scan_btn,
        ):
            bar.addWidget(widget)
        bar.addStretch(1)
        self.record_label = QtWidgets.QLabel("WAV: off")
        self.record_label.setStyleSheet("color:#6b7280;")
        bar.addWidget(self.record_label)
        bar.addWidget(self.status)
        layout.addLayout(bar)

        self.health_panel = HealthPanel()
        layout.addWidget(self.health_panel)

        # Live meters
        meters = QtWidgets.QHBoxLayout()
        self.level_bars = LevelBars()
        self.spectrum_bars = SpectrumGrid()
        meters.addWidget(self.level_bars, 2)
        meters.addWidget(self.spectrum_bars, 3)
        layout.addLayout(meters, 1)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._tab_session(), "1. Session")
        self.tabs.addTab(self._tab_measure(), "2. Measure")
        self.tabs.addTab(self._tab_reverb(), "3. Reverberation")
        self.tabs.addTab(self._tab_evaluate(), "4. Rating")
        self.tabs.addTab(self._tab_device(), "Device / calibration")
        layout.addWidget(self.tabs, 2)

        self.setCentralWidget(central)
        self.statusBar().showMessage("Not connected")

    # ── Tab 1: session ──
    def _tab_session(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)

        self.title_edit = QtWidgets.QLineEdit(self.session.title)
        self.site_edit = QtWidgets.QLineEdit()
        self.operator_edit = QtWidgets.QLineEdit()
        self.source_combo = QtWidgets.QComboBox()
        for source in ImpactSource:
            self.source_combo.addItem(source.label, source)
        self.source_combo.setCurrentIndex(
            list(ImpactSource).index(ImpactSource.RUBBER_BALL)
        )
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)

        self.src_pos_spin = QtWidgets.QSpinBox()
        self.src_pos_spin.setRange(1, 12)
        self.src_pos_spin.setValue(5)
        self.volume_spin = QtWidgets.QDoubleSpinBox()
        self.volume_spin.setRange(0.0, 500.0)
        self.volume_spin.setSuffix(" m³")
        self.volume_spin.setValue(40.0)

        # A receiver position is a channel. One excitation records every
        # ticked channel at the same time.
        channel_box = QtWidgets.QWidget()
        channel_layout = QtWidgets.QHBoxLayout(channel_box)
        channel_layout.setContentsMargins(0, 0, 0, 0)
        self.channel_checks: dict[int, QtWidgets.QCheckBox] = {}
        for channel in range(MAX_CHANNELS):
            check = QtWidgets.QCheckBox(channel_label(channel))
            check.setChecked(channel < 5)
            check.toggled.connect(self._on_channels_changed)
            self.channel_checks[channel] = check
            channel_layout.addWidget(check)
        channel_layout.addStretch(1)


        form.addRow("Title", self.title_edit)
        form.addRow("Site", self.site_edit)
        form.addRow("Operator", self.operator_edit)
        form.addRow("Impact source", self.source_combo)
        form.addRow("Source positions", self.src_pos_spin)
        form.addRow("Receiver channels", channel_box)
        form.addRow("Receiving room volume", self.volume_spin)

        note = QtWidgets.QLabel(
            "Analysis is always 1/3 octave with a 6th-order Butterworth filter.\n"
            "You move the impact source between source positions yourself; every\n"
            "ticked channel is recorded in the same excitation, so excitation\n"
            "variation cannot leak between receiver positions.\n"
            "Detailed instrument settings live under Settings \u2192 Instrument options."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#9aa0a6;")
        form.addRow(note)

        buttons = QtWidgets.QHBoxLayout()
        apply_btn = QtWidgets.QPushButton("Apply session + configure Pi")
        apply_btn.clicked.connect(self._apply_session)
        save_btn = QtWidgets.QPushButton("Save session")
        save_btn.clicked.connect(self._save_session)
        load_btn = QtWidgets.QPushButton("Load session")
        load_btn.clicked.connect(self._load_session)
        for b in (apply_btn, save_btn, load_btn):
            buttons.addWidget(b)
        buttons.addStretch(1)
        form.addRow(buttons)
        return page

    # ── Tab 2: measurement ──
    def _tab_measure(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)

        controls = QtWidgets.QHBoxLayout()
        self.src_pos_current = QtWidgets.QSpinBox()
        self.src_pos_current.setRange(1, 12)
        self.duration_spin = QtWidgets.QDoubleSpinBox()
        self.duration_spin.setRange(1.0, 60.0)
        self.duration_spin.setValue(10.0)
        self.duration_spin.setSuffix(" s")

        self.capture_btn = QtWidgets.QPushButton("Capture this source position (all channels)")
        self.capture_btn.clicked.connect(self._capture_measurement)
        self.background_btn = QtWidgets.QPushButton("Measure background")
        self.background_btn.clicked.connect(self._capture_background)
        self.swap_btn = QtWidgets.QPushButton("Switch source")
        self.swap_btn.setToolTip(
            "Move between the heavy and light halves of the set.\n"
            "The Pi is reconfigured, so the scan stops and restarts."
        )
        self.swap_btn.clicked.connect(self._switch_impact_source)
        self.set_label = QtWidgets.QLabel()
        self.reset_btn = QtWidgets.QPushButton("Reset…")
        self.reset_btn.setToolTip("Discard measurements and start over")
        self.reset_btn.clicked.connect(self._reset_measurements)

        for widget in (
            QtWidgets.QLabel("Source position"), self.src_pos_current,
            QtWidgets.QLabel("Duration"), self.duration_spin,
            self.capture_btn, self.background_btn,
            self.swap_btn, self.set_label, self.reset_btn,
        ):
            controls.addWidget(widget)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.progress_label = QtWidgets.QLabel("Progress: 0 / 0")
        layout.addWidget(self.progress_label)
        self.snq_label = QtWidgets.QLabel()
        self.snq_label.setWordWrap(True)
        layout.addWidget(self.snq_label)

        # One column per 1/3-octave band. The broadband figure used to sit
        # here, but it is derived from these and tells you nothing the bands
        # do not — whereas a single bad band is invisible without them.
        self.measure_table = QtWidgets.QTableWidget(0, 4)
        self.measure_table.horizontalHeader().setStretchLastSection(True)
        self.measure_table.horizontalHeader().setDefaultSectionSize(58)
        self._measure_columns: tuple[float, ...] = ()
        layout.addWidget(self.measure_table, 1)
        return page

    # ── Tab 3: reverberation ──
    def _tab_reverb(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)

        # Where the excitation comes from. The Pi's own DAC is only one option
        # — plenty of rigs drive a proper amp and dodecahedron, and sometimes
        # the reverberation has already been measured on other equipment.
        source_row = QtWidgets.QHBoxLayout()
        self.reverb_source = QtWidgets.QComboBox()
        self.reverb_source.addItem("Pi analog output (DT9837A)", "internal")
        self.reverb_source.addItem("External speaker", "external")
        self.reverb_source.addItem("Manual entry", "manual")
        self.reverb_source.currentIndexChanged.connect(self._on_reverb_source_changed)

        # A single microphone position is one sample of a very non-uniform
        # field; ISO 3382-2 wants several and their arithmetic mean.
        self.reverb_channels: dict[int, QtWidgets.QCheckBox] = {}
        channel_row = QtWidgets.QWidget()
        channel_layout = QtWidgets.QHBoxLayout(channel_row)
        channel_layout.setContentsMargins(0, 0, 0, 0)
        for channel in range(MAX_CHANNELS):
            check = QtWidgets.QCheckBox(channel_label(channel))
            check.setChecked(channel == 0)
            self.reverb_channels[channel] = check
            channel_layout.addWidget(check)

        source_row.addWidget(QtWidgets.QLabel("Excitation source"))
        source_row.addWidget(self.reverb_source)
        source_row.addWidget(QtWidgets.QLabel("   Microphones"))
        source_row.addWidget(channel_row)
        source_row.addStretch(1)
        layout.addLayout(source_row)

        # Excitation — what the analog output plays
        excite = QtWidgets.QGroupBox("Excitation (DT9837A analog output)")
        self.excite_box = excite
        excite_row = QtWidgets.QHBoxLayout(excite)
        self.signal_combo = QtWidgets.QComboBox()
        for signal in Signal:
            self.signal_combo.addItem(signal.label, signal.value)
        self.signal_combo.setCurrentIndex(list(Signal).index(Signal.PINK))
        self.signal_combo.currentIndexChanged.connect(self._on_signal_changed)

        self.signal_seconds = QtWidgets.QDoubleSpinBox()
        self.signal_seconds.setRange(0.5, 30.0)
        self.signal_seconds.setValue(3.0)
        self.signal_seconds.setSuffix(" s")
        self.signal_level = QtWidgets.QDoubleSpinBox()
        self.signal_level.setRange(-60.0, 0.0)
        self.signal_level.setValue(-20.0)
        self.signal_level.setSuffix(" dBFS")
        self.mls_order = QtWidgets.QSpinBox()
        self.mls_order.setRange(8, 18)
        self.mls_order.setValue(16)
        self.mls_order.setPrefix("order ")

        self.output_btn = QtWidgets.QPushButton("▶ Output")
        self.output_btn.setToolTip("Play the excitation from the analog output")
        self.output_btn.clicked.connect(self._toggle_output)
        self.output_btn.setEnabled(False)

        for widget in (
            QtWidgets.QLabel("Signal"), self.signal_combo,
            QtWidgets.QLabel("Length"), self.signal_seconds,
            QtWidgets.QLabel("Level"), self.signal_level,
            self.mls_order, self.output_btn,
        ):
            excite_row.addWidget(widget)
        excite_row.addStretch(1)
        self.output_state = QtWidgets.QLabel("output idle")
        self.output_state.setStyleSheet("color:#6b7280;")
        excite_row.addWidget(self.output_state)
        layout.addWidget(excite)

        # External speaker. The program cannot start or stop the sound, so the
        # operator brackets it by hand; the sweep can still be deconvolved as
        # long as the exact file that was played is the one used as reference.
        self.external_box = QtWidgets.QGroupBox("External speaker")
        external_row = QtWidgets.QHBoxLayout(self.external_box)
        self.external_method = QtWidgets.QComboBox()
        self.external_method.addItem("Play from this computer", "soundcard")
        self.external_method.addItem("Interrupted noise — XL2 procedure / 3 cycles", "noise")
        self.external_method.addItem("Sine sweep file — deconvolve", "sweep")
        self.external_method.currentIndexChanged.connect(self._on_reverb_source_changed)

        # Playing from here beats handing over a WAV: the program knows which
        # signal went out and when, so the operator cannot get the file and
        # the analysis settings out of step.
        self.audio_device = QtWidgets.QComboBox()
        self.audio_device.setMinimumWidth(260)
        self.play_btn = QtWidgets.QPushButton("▶ Play and measure")
        self.play_btn.setToolTip(
            "Play the excitation through this computer's sound card while the "
            "Pi records, then analyse the same pass."
        )
        self.play_btn.clicked.connect(self._play_and_measure)
        self.refresh_audio_btn = QtWidgets.QPushButton("↻")
        self.refresh_audio_btn.setFixedWidth(30)
        self.refresh_audio_btn.setToolTip("Re-scan the sound cards")
        self.refresh_audio_btn.clicked.connect(self._refresh_audio_devices)

        self.save_sweep_btn = QtWidgets.QPushButton("Save sweep WAV…")
        self.save_sweep_btn.setToolTip(
            "Write the sweep to a file to play through the external system.\n"
            "The analysis uses the same parameters, so it must be this file."
        )
        self.save_sweep_btn.clicked.connect(self._save_sweep_file)
        self.external_record_btn = QtWidgets.QPushButton("● Start recording")
        self.external_record_btn.clicked.connect(self._toggle_external_recording)
        self.external_state = QtWidgets.QLabel("idle")
        self.external_state.setStyleSheet("color:#6b7280;")
        for widget in (
            QtWidgets.QLabel("Method"), self.external_method,
            self.audio_device, self.refresh_audio_btn, self.play_btn,
            self.save_sweep_btn, self.external_record_btn, self.external_state,
        ):
            external_row.addWidget(widget)
        external_row.addStretch(1)
        layout.addWidget(self.external_box)
        self._refresh_audio_devices()

        # Manual entry — reverberation measured on other equipment.
        self.manual_box = QtWidgets.QGroupBox("Manual entry — one T per band")
        manual_layout = QtWidgets.QVBoxLayout(self.manual_box)
        manual_row = QtWidgets.QHBoxLayout()
        self.manual_fill = QtWidgets.QDoubleSpinBox()
        self.manual_fill.setRange(0.05, 10.0)
        self.manual_fill.setValue(0.5)
        self.manual_fill.setSuffix(" s")
        self.manual_fill.setDecimals(2)
        fill_btn = QtWidgets.QPushButton("Fill all bands")
        fill_btn.clicked.connect(self._fill_manual_reverberation)
        apply_btn = QtWidgets.QPushButton("Apply to session")
        apply_btn.clicked.connect(self._apply_manual_reverberation)
        for widget in (
            QtWidgets.QLabel("Value"), self.manual_fill, fill_btn, apply_btn,
        ):
            manual_row.addWidget(widget)
        manual_row.addStretch(1)
        manual_layout.addLayout(manual_row)
        self.manual_table = QtWidgets.QTableWidget(0, 2)
        self.manual_table.setHorizontalHeaderLabels(["Band (Hz)", "T (s)"])
        self.manual_table.horizontalHeader().setStretchLastSection(True)
        manual_layout.addWidget(self.manual_table)
        layout.addWidget(self.manual_box)

        # Measurement
        controls = QtWidgets.QHBoxLayout()
        self.reverb_seconds = QtWidgets.QDoubleSpinBox()
        self.reverb_seconds.setRange(1.0, 30.0)
        self.reverb_seconds.setValue(5.0)
        self.reverb_seconds.setSuffix(" s")
        self.reverb_method = QtWidgets.QComboBox()
        self.reverb_method.addItems(["T20", "T30"])
        sequence_btn = QtWidgets.QPushButton("SET / 3 cycles — XL2")
        sequence_btn.clicked.connect(self._open_rt_sequence)
        measure_btn = QtWidgets.QPushButton("Capture decay \u2192 compute T")
        measure_btn.clicked.connect(self._measure_reverberation)
        auto_btn = QtWidgets.QPushButton("Output + measure")
        auto_btn.setToolTip("Play the excitation and analyse it in one go")
        auto_btn.clicked.connect(self._output_and_measure)
        clear_btn = QtWidgets.QPushButton("Clear")
        clear_btn.clicked.connect(self._clear_reverberation)

        for widget in (
            QtWidgets.QLabel("Window"), self.reverb_seconds,
            QtWidgets.QLabel("Method"), self.reverb_method,
            sequence_btn, auto_btn, measure_btn, clear_btn,
        ):
            controls.addWidget(widget)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.reverb_hint = QtWidgets.QLabel()
        self.reverb_hint.setWordWrap(True)
        self.reverb_hint.setStyleSheet("color:#9aa0a6;")
        layout.addWidget(self.reverb_hint)
        self._on_signal_changed()
        self._on_reverb_source_changed()

        self.reverb_table = QtWidgets.QTableWidget(0, 5)
        self.reverb_table.setHorizontalHeaderLabels(
            ["Band (Hz)", "T (s)", "Correlation", "Curvature (%)", "Verdict"]
        )
        self.reverb_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.reverb_table, 1)
        return page

    def _excitation(self) -> ExcitationSettings:
        bands = self.session.bands
        return ExcitationSettings(
            signal=Signal(self.signal_combo.currentData()),
            seconds=self.signal_seconds.value(),
            level_dbfs=self.signal_level.value(),
            f_min=min(bands) / 2,
            f_max=max(bands) * 2,
            mls_order=self.mls_order.value(),
            channel=0,
            tail_seconds=2.0,
            # For MLS this is the number of whole periods played. Two is the
            # minimum that can be deconvolved at all (the first is build-up);
            # a third leaves margin so that correcting the start_index error
            # cannot walk the analysis window out of the excitation.
            repeats=3 if Signal(self.signal_combo.currentData()) is Signal.MLS else 1,
        )

    def _on_signal_changed(self) -> None:
        signal = Signal(self.signal_combo.currentData())
        self.mls_order.setVisible(signal is Signal.MLS)
        self.signal_seconds.setEnabled(signal is not Signal.MLS)
        if signal.is_deterministic:
            self.reverb_hint.setText(
                f"{signal.label}: the impulse response is deconvolved from the "
                "recording (ISO 18233). Better signal-to-noise than interrupted "
                "noise and repeatable rather than stochastic. Use "
                "[Output + measure] so the software knows exactly what was played."
            )
        else:
            self.reverb_hint.setText(
                f"{signal.label}: interrupted-noise method. Excite the room, stop "
                "the source, and the decay after switch-off is analysed. Each run "
                "is one stochastic realisation, so average several."
            )

    # ── Analog output ──
    def _toggle_output(self) -> None:
        if self.pi is None:
            self._warn("Connect to the Pi first")
            return
        if self._output_running:
            self._submit("output_stop", lambda: self.pi.output_stop(), "Stopping output")
            return
        settings = self._excitation()
        self._submit(
            "output_start",
            lambda: (self.pi.set_output(settings), self.pi.output_start())[1],
            f"Playing {settings.signal.label}\u2026",
        )

    def _output_and_measure(self) -> None:
        """Play the excitation and analyse the same pass.

        For sweep and MLS this is the only sensible route: the analysis needs
        to know exactly which signal was played and where it started.
        """
        if self.pi is None or not self.pi.config.running:
            self._warn("The scan must be running before you can measure")
            return
        if not self.pi.output_available:
            self._warn("This hardware reports no analog output")
            return

        channels = self._reverb_selected_channels()
        if not channels:
            self._warn("Tick at least one microphone for the reverberation")
            return
        settings = self._excitation()
        method = self.reverb_method.currentText()
        window = self.reverb_seconds.value()

        def run():
            import time as _time

            was_raw = self.pi.config.stream_raw
            if not was_raw:
                self.pi.set_options(stream_raw=True)
            try:
                self._output_completed = None
                self.pi.set_output(settings)
                started = self.pi.output_start()
                total = float(started.get("total_seconds", settings.seconds))

                # Wait for the pass to end on its own rather than cutting it
                # off after a fixed sleep. §10 says a truncated sweep or MLS
                # must not be deconvolved, and only the server can say whether
                # the pass completed — `output_stop` would itself make it
                # incomplete.
                deadline = _time.monotonic() + total + 10.0
                while self._output_completed is None:
                    if _time.monotonic() > deadline:
                        self.pi.output_stop()
                        raise RuntimeError(
                            "The output never reported finishing; the pass was "
                            "cut short and cannot be analysed"
                        )
                    _time.sleep(0.1)
                if not self._output_completed and settings.signal.is_deterministic:
                    raise RuntimeError(
                        f"The {settings.signal.label} pass stopped early. A truncated "
                        "excitation cannot be deconvolved — measure again"
                    )
                # Let the room decay land in the buffer as well
                _time.sleep(window)

                dumps = self._fetch_window(total + window, timeout=45.0)

                # Line every microphone up with the excitation using the
                # start_index the server reported on the DATA grid. Trimming
                # the dumps rather than each channel keeps all of them on the
                # same time base.
                start = started.get("start_index")
                if settings.signal.is_deterministic and start is not None:
                    dumps = {
                        device: _trimmed(dump, int(start))
                        for device, dump in dumps.items()
                    }
                return self._analyse_dumps(
                    dumps, channels, method=method,
                    settings=settings if settings.signal.is_deterministic else None,
                )
            finally:
                if not was_raw:
                    self.pi.set_options(stream_raw=False)

        self._submit("reverb", run, f"{settings.signal.label} \u2192 T{method[1:]}\u2026")

    @property
    def _output_running(self) -> bool:
        return bool((self.config_output or {}).get("running", False))

    @property
    def config_output(self) -> dict:
        return (self.pi.config.raw.get("output") or {}) if self.pi else {}

    def _set_output_state(self, running: bool, text: str = "") -> None:
        self.output_btn.setText("■ Stop output" if running else "▶ Output")
        self.output_state.setText(text or ("playing" if running else "output idle"))
        self.output_state.setStyleSheet(
            "color:#4ade80;" if running else "color:#6b7280;"
        )

    # ── Tab 4: rating ──
    def _tab_evaluate(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)

        controls = QtWidgets.QHBoxLayout()
        evaluate_btn = QtWidgets.QPushButton("Evaluate")
        evaluate_btn.clicked.connect(self._evaluate)
        curve_btn = QtWidgets.QPushButton("Load inverse-A curve…")
        self.curve_load_btn = curve_btn
        curve_btn.clicked.connect(self._load_curve)
        export_btn = QtWidgets.QPushButton("Export result (text)")
        export_btn.clicked.connect(self._export_report)
        for b in (evaluate_btn, curve_btn, export_btn):
            controls.addWidget(b)
        controls.addStretch(1)
        self.curve_label = QtWidgets.QLabel()
        controls.addWidget(self.curve_label)
        layout.addLayout(controls)

        self.result_text = QtWidgets.QPlainTextEdit()
        self.result_text.setReadOnly(True)
        self.result_text.setStyleSheet("font-family: Consolas, monospace;")
        layout.addWidget(self.result_text, 1)
        self._update_curve_label()
        return page

    # ── Tab 5: device / calibration ──
    def _tab_device(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)

        controls = QtWidgets.QHBoxLayout()
        self.cal_channel = QtWidgets.QSpinBox()
        self.cal_channel.setRange(1, MAX_CHANNELS)
        self.cal_channel.setPrefix("Ch")
        self.cal_level = QtWidgets.QDoubleSpinBox()
        self.cal_level.setRange(80.0, 130.0)
        self.cal_level.setValue(94.0)
        self.cal_level.setSuffix(" dB")
        cal_btn = QtWidgets.QPushButton("Calibrate")
        cal_btn.clicked.connect(self._calibrate)
        save_cal_btn = QtWidgets.QPushButton("Save to Pi")
        save_cal_btn.clicked.connect(
            lambda: self._submit("save_config", lambda: self.pi.save_config(), "Saving settings")
        )
        for widget in (
            QtWidgets.QLabel("Channel"), self.cal_channel,
            QtWidgets.QLabel("Calibrator level"), self.cal_level,
            cal_btn, save_cal_btn,
        ):
            controls.addWidget(widget)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.device_text = QtWidgets.QPlainTextEdit()
        self.device_text.setReadOnly(True)
        self.device_text.setStyleSheet("font-family: Consolas, monospace;")
        layout.addWidget(self.device_text, 1)
        return page

    def _poll_health(self) -> None:
        if self._closing or self.pi is None or self._health_pending:
            return
        pi = self.pi
        self._health_pending = True

        def query():
            try:
                return pi, pi.health(), None
            except Exception as exc:
                return pi, None, str(exc)

        self.health_worker.submit("health", query)

    def _on_health_done(self, _name, result) -> None:
        self._health_pending = False
        pi, payload, error = result
        # Replies from a disconnected/replaced client must not overwrite the UI.
        if self._closing or pi is not self.pi:
            return
        if error is not None or not isinstance(payload, dict):
            self.health_panel.unavailable("조회 실패 — 연결 상태 확인")
        else:
            self.health_panel.update_snapshot(payload)

    def _on_health_failed(self, _name, _message, _traceback) -> None:
        self._health_pending = False
        if not self._closing and self.pi is not None:
            self.health_panel.unavailable("조회 실패")

    # ── Job submission ──
    def _submit(self, name: str, func, message: str = "") -> None:
        if self.pi is None and name != "connect":
            self._warn("Connect to the Pi first")
            return
        self.worker.submit(name, func, message)

    def _warn(self, text: str) -> None:
        QtWidgets.QMessageBox.warning(self, "Check this", text)

    # ── Connection ──
    def _toggle_connection(self) -> None:
        if self.pi is not None:
            pi, self.pi = self.pi, None
            self.health_panel.unavailable("연결 안 됨")
            self.worker.submit("disconnect", pi.close, "Disconnecting")
            self.connect_btn.setText("Connect")
            self.scan_btn.setEnabled(False)
            self.status.set_state("idle", "Not connected")
            return

        host = self.host_edit.text().strip()
        control = self.control_spin.value()
        stream = self.stream_spin.value()

        def connect():
            # No frame queue: everything here is read through `on_frame`,
            # and an undrained queue just counts overflows for ever.
            client = PiSLM(host, control, stream, connect_timeout=5.0,
                           stream_queue_size=0)
            client.connect()
            client.stream.on_frame(self.live.feed)
            client.stream.on_frame(self.recorder.feed)
            # The band table only appears while band output is active, so watch
            # the stream port and pick it up whenever it shows.
            client.stream.on_message(self._absorb_band_table)
            client.refresh()
            return client

        self._submit("connect", connect, f"Connecting to {host}")

    def _toggle_scan(self) -> None:
        if self.pi is None:
            return
        if self.pi.config.running:
            self._submit("stop", self.pi.stop, "Stopping scan")
        else:
            self._submit("start", self.pi.start, "Starting scan")

    # ── Session ──
    def _current_source(self) -> ImpactSource:
        """Read the impact source back from the combo box.

        `ImpactSource` subclasses `str`, so a round trip through QVariant hands
        back a plain string. Feed it through the constructor to restore the enum.
        """
        return ImpactSource(self.source_combo.currentData())

    def _on_source_changed(self) -> None:
        """The selector picks a source *and* therefore a half of the set.

        Assigning straight onto `self.session` was wrong once the set existed:
        choosing the tapping machine while the heavy half was active gave the
        heavy session a light source, and with it light bands and a light
        rating for data measured with a ball.
        """
        source = self._current_source()
        if (source.is_heavy and self._sessions["heavy"].measurements
                and self._sessions["heavy"].source != source):
            blocked = self.source_combo.blockSignals(True)
            self.source_combo.setCurrentIndex(list(ImpactSource).index(self.session.source))
            self.source_combo.blockSignals(blocked)
            self._warn("Save and reset the heavy measurements before changing between ball and bang sources")
            return
        key = "heavy" if source.is_heavy else "light"
        if key != self._active:
            self._share_to_other_session()
            self._active = key
        self.session.source = source
        self.spectrum_bars.set_series({})
        self.spectrum_bars.set_captured({})
        self._update_quantity_labels()
        self._update_set_label()
        self._refresh_measure_table()

    def _apply_session(self) -> None:
        # The source selector decides which half of the set is active, so
        # picking a heavy source while the light half is up switches over
        # rather than overwriting the light session's source.
        source = self._current_source()
        key = "heavy" if source.is_heavy else "light"
        if key != self._active:
            self._share_to_other_session()
            self._active = key

        self.session.title = self.title_edit.text()
        self.session.site = self.site_edit.text()
        self.session.operator = self.operator_edit.text()
        self.session.source = source
        self.session.source_positions = self.src_pos_spin.value()
        self.session.channels = self._selected_channels()
        self.session.room.volume = self.volume_spin.value()
        self._share_to_other_session()
        self.src_pos_current.setMaximum(self.session.source_positions)
        self._update_quantity_labels()
        self._refresh_measure_table()

        if not self.session.channels:
            self._warn("Tick at least one receiver channel")
            return

        if self.pi is None:
            self.statusBar().showMessage("Session applied (not connected — band setup deferred)")
            return

        source = self.session.source
        bands = source.bands
        f_min, f_max = min(bands), max(bands)
        frequency = source.frequency_weighting
        time_weighting = source.time_weighting

        # Put the scan back the way it was found. `send_stopped()` restarts a
        # scan it had to stop itself, but the explicit `stop()` below means the
        # config commands no longer *need* stopping, so nothing restarts it —
        # the scan was left stopped while the button still said "Stop scan".
        # Switch source hits this every time: reconfiguring the bands is the
        # whole point of the button, and the operator had to press Start scan
        # again for a scan the window claimed was already running.
        was_running = bool(self.pi.config.raw.get("running"))

        def configure():
            self.pi.stop()
            # Match the analysis bandwidth to the selected source profile
            self.pi.set_bands(
                enabled=True, output="level", fraction=source.fraction, order=FILTER_ORDER,
                f_min=f_min, f_max=f_max,
            )
            self.pi.set_weighting(frequency=frequency, time_weighting=time_weighting)
            self.pi.set_level(enabled=True, output_rate=20.0)
            if was_running:
                self.pi.start()
            return self.pi.refresh()

        self._submit(
            "configure",
            configure,
            f"Configuring 1/{source.fraction} octave {f_min:g}\u2013{f_max:g} Hz, {frequency}/{time_weighting}",
        )

    def _absorb_band_table(self, body: dict) -> None:
        """Refresh the band index -> frequency map from any handshake or event.

        Called on the stream reader thread; LiveState is lock-protected.
        """
        if body.get("type") == "handshake" or body.get("event") == "started":
            self.live.configure_bands(Handshake(raw=body))
        event = body.get("event")
        if event in ("output_started", "output_finished"):
            self._output_event.emit(event, dict(body))

    def _update_quantity_labels(self) -> None:
        if hasattr(self, "curve_load_btn"):
            self._update_curve_label()
        source = self.session.source
        self.spectrum_bars.set_quantity(
            f"Lp,{source.quantity}", source.frequency_weighting
        )

    # ── Options ──
    def _open_options(self) -> None:
        if self.pi is None:
            self._warn("Connect to the Pi first — the current settings must be read")
            return
        current = DeviceOptions.from_config(self.pi.config, self.recording)
        dialog = OptionsDialog(current, self)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        options = dialog.values()
        if not options.changed:
            self.statusBar().showMessage("Nothing changed")
            return

        # Recorder settings belong to this program, so apply them immediately
        if any(name.startswith("record_") for name in options.changed):
            if options.record_enabled and not options.record_directory:
                self._warn("Choose a folder for the WAV files")
                return
            self.recording = recording_options(options)
            self.recorder.options = self.recording
            self._update_recording_label()

        device_changes = {n for n in options.changed if not n.startswith("record_")}
        if not device_changes:
            self.statusBar().showMessage("Recording settings applied")
            return
        self._submit(
            "options",
            lambda: apply_options(self.pi, options),
            f"Applying {len(device_changes)} setting(s)\u2026",
        )

    def _on_output_event(self, event: str, body: dict) -> None:
        """Server-reported output state, delivered on the GUI thread."""
        if event == "output_started":
            self._output_completed = None
            self._set_output_state(True, f"playing {body.get('signal', '')}")
        else:
            completed = bool(body.get("completed", True))
            # PROTOCOL.md §10: a truncated sweep or MLS run must not be
            # deconvolved. Record the verdict for the measurement worker.
            self._output_completed = completed
            self._set_output_state(
                False, "output finished" if completed else "output stopped early"
            )

    def _update_recording_label(self) -> None:
        if self.recording.enabled:
            self.record_label.setText(
                f"WAV: per source position · {self.recording.fmt} "
                f"→ {self.recording.directory}"
            )
            self.record_label.setStyleSheet("color:#4ade80;")
        else:
            self.record_label.setText("WAV: off")
            self.record_label.setStyleSheet("color:#6b7280;")

    def _start_recording(self, label: str, meta: dict | None = None) -> None:
        """Begin a recording, turning on the raw stream it feeds off.

        The recorder only consumes DATA frames, and the Pi only sends those
        when `stream_raw` is on. Nothing here used to switch it on, so unless
        the operator happened to have ticked "stream raw waveform" in the
        options, recording silently produced no files at all — the recorder
        reported itself as running the whole time.
        """
        if not self.recording.enabled or self.pi is None:
            return
        self._raw_was_on = bool(self.pi.config.stream_raw)
        if not self._raw_was_on:
            try:
                self.pi.set_options(stream_raw=True)
            except Exception as exc:  # noqa: BLE001
                self._warn(f"Could not start the raw stream, so nothing can be recorded: {exc}")
                return
        # One file per source position; one column per receiver position.
        self.recorder.start(
            self.pi.config,
            label=label,
            channels=self._selected_channels(),
            meta=meta or {},
        )

    def _stop_recording(self) -> None:
        if not self.recorder.recording:
            return
        written = self.recorder.stop()
        # Put the raw stream back the way it was — it is the heaviest thing on
        # the link, and leaving it on would eat bandwidth for the rest of the
        # session (§6: DATA is best-effort and crowds out everything else).
        if self.pi is not None and not self._raw_was_on:
            try:
                self.pi.set_options(stream_raw=False)
            except Exception:  # noqa: BLE001
                pass  # a failure here costs bandwidth, not data
        self._raw_was_on = False
        if written:
            total = sum(p.stat().st_size for p in written if p.exists())
            self.statusBar().showMessage(
                f"Wrote {len(written)} WAV file(s) ({format_size(total)}): "
                + ", ".join(p.name for p in written)
            )
        else:
            self._warn(
                "Recording was on but no audio arrived, so no file was written. "
                "The raw stream may have been refused by the Pi."
            )

    def _save_pi_config(self) -> None:
        self._submit("save_config", lambda: self.pi.save_config(), "Saving settings to Pi")

    def _save_session(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save session", f"{self.session.title or 'session'}.json", "JSON (*.json)"
        )
        if path:
            self.session.save(path)
            self.statusBar().showMessage(f"Saved: {path}")

    def _load_session(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Load session", "", "JSON (*.json)"
        )
        if not path:
            return
        try:
            self._sessions[self._active] = Session.load(path)
            self._active_key_follows_source()
        except Exception as exc:  # noqa: BLE001
            self._warn(f"Could not load: {exc}")
            return
        self.title_edit.setText(self.session.title)
        self.site_edit.setText(self.session.site)
        self.operator_edit.setText(self.session.operator)
        self.source_combo.setCurrentIndex(list(ImpactSource).index(self.session.source))
        self.src_pos_spin.setValue(self.session.source_positions)
        for channel, box in self.channel_checks.items():
            box.setChecked(channel in self.session.channels)
        self.volume_spin.setValue(self.session.room.volume)
        self._refresh_measure_table()
        self._refresh_reverb_table()
        self.statusBar().showMessage(f"Loaded: {path}")

    # ── Capture ──
    def _collect(self, seconds: float, channels: list[int], *, source=None, background=False) -> dict:
        """The capture itself — runs on the worker thread.

        Heavy: full-rate Fast maxima recomputed from buffered raw waveform.
        Light: per-band Leq that the Pi computes from the raw buffer as an RMS.
               Fast time weighting is only for the on-screen meter; it plays no
               part in the Leq figure.
        """
        import time

        source = source or self.session.source
        bands = tuple(source.bands)
        fraction = 3 if background else source.fraction
        heavy = source.is_heavy and not background
        if heavy:
            pi = self.pi
            self.worker.progress.emit("Preparing raw history — wait before striking")
            prepare_heavy_capture(
                pi, seconds, channels,
                cancelled=lambda: self._closing or self.pi is not pi)
            self.worker.progress.emit(f"Capture now — {seconds:g} s")
        self.live.start_capture()
        try:
            time.sleep(seconds)
        finally:
            self.live.finish_capture(channels)

        per_channel: dict[int, dict[float, float]] = {}

        analysis_note = ""
        if heavy:
            per_channel, analysis_note = fetch_heavy_levels(
                self.pi, seconds, channels, bands, fraction=fraction,
                order=FILTER_ORDER)
        else:
            metrics = self.pi.get_metrics(
                seconds=seconds, channels=channels, include_bands=True
            )
            for channel in channels:
                entry = metrics["channels"].get(str(channel), {})
                levels: dict[float, float] = {}
                for band in entry.get("bands", []):
                    try:
                        center = nominal_center(float(band["center"]), fraction)
                    except (KeyError, ValueError):
                        continue
                    levels[center] = float(band["Leq"])
                per_channel[channel] = levels

        # The A-weighted single number comes from adding per-band weightings to
        # the Z spectrum (ISO 717-2). No A filter runs on the device, so the
        # weighting is applied arithmetically here.
        broadband: dict[int, float] = {}
        for channel, levels in per_channel.items():
            if not levels:
                continue
            spectrum = Spectrum.from_mapping(
                levels, fraction=fraction, weighting="Z"
            )
            broadband[channel] = a_weighted_single_number(spectrum)

        return {
            "levels": per_channel,
            "broadband": broadband,
            "seconds": seconds,
            "analysis_note": analysis_note,
            "impact_source": source.value,
            "valid": self.pi.measurement_valid[0],
            "reasons": self.pi.measurement_valid[1],
        }

    def _capture_measurement(self) -> None:
        if self.pi is None or not self.pi.config.running:
            self._warn("The scan must be running before you can capture")
            return
        channels = self._selected_channels()
        if not channels:
            self._warn("Tick at least one receiver channel")
            return
        source_pos = self.src_pos_current.value()
        source = self.session.source
        seconds = self.duration_spin.value()

        self.capture_btn.setEnabled(False)
        if self.recording.enabled:
            # The half of the set goes in the name too, otherwise a heavy and
            # a light pass at the same position are told apart only by their
            # timestamp.
            self._start_recording(
                f"{self._active}_src{source_pos}",
                meta={
                    "source_position": source_pos,
                    "impact_source": self.session.source.value,
                    "title": self.session.title,
                    "site": self.session.site,
                },
            )

        def capture():
            data = self._collect(seconds, channels, source=source)
            data["source_position"] = source_pos
            return data

        self._submit(
            "capture",
            capture,
            ("Preparing — wait for Capture now before striking" if source.is_heavy else
             f"Capturing source position {source_pos}, {len(channels)} channels, {seconds:.0f} s\u2026"),
        )

    def _capture_background(self) -> None:
        if self.pi is None or not self.pi.config.running:
            self._warn("The scan must be running before you can capture")
            return
        channels = self._selected_channels()
        if not channels:
            self._warn("Tick at least one receiver channel")
            return
        seconds = self.duration_spin.value()
        source = self.session.source
        wide = COMBINED_BANDS

        def measure():
            # One background sweep over 50-3150 Hz serves both halves of the
            # set — it is the same room noise either way, and measuring it
            # twice invites two slightly different answers for one room. The
            # analysis trims it to whichever bands each source is rated over.
            self.pi.stop()
            self.pi.set_bands(
                enabled=True, output="level", fraction=3, order=FILTER_ORDER,
                f_min=min(wide), f_max=max(wide),
            )
            self.pi.start()
            try:
                # The band table only appears once the scan is running, and
                # BAND_LEVEL frames whose index is not in the map are dropped
                # — so refresh and wait for it before starting the capture,
                # or the whole window is silently discarded.
                if not self._await_band_table(len(wide)):
                    raise RuntimeError(
                        "The Pi never reported a band table for 50-3150 Hz"
                    )
                return self._collect(seconds, channels, source=source, background=True)
            finally:
                # Put the instrument back on the source's own band set,
                # otherwise the next capture silently measures the wrong range.
                self.pi.stop()
                self.pi.set_bands(
                    enabled=True, output="level", fraction=source.fraction, order=FILTER_ORDER,
                    f_min=min(source.bands), f_max=max(source.bands),
                )
                self.pi.start()
                self._await_band_table(len(source.bands))

        self._submit(
            "background",
            measure,
            f"Measuring background 50-3150 Hz for {seconds:.0f} s\u2026",
        )

    def _await_band_table(self, expected: int, timeout: float = 5.0) -> bool:
        """Wait until the live mapping matches the band set just configured.

        Called from the worker thread, so it blocks that thread only.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pi.refresh()
            self.live.configure_bands(self.pi.config)
            # Count, not merely "a table exists" — right after a reconfigure
            # the previous table is still what `get_config` reports, and
            # accepting it means the capture is filed under the wrong bands.
            if len(self.live.band_centers) == expected:
                return True
            time.sleep(0.1)
        return False

    def _selected_channels(self) -> list[int]:
        return [c for c, box in self.channel_checks.items() if box.isChecked()]

    def _on_channels_changed(self) -> None:
        channels = self._selected_channels()
        self.session.channels = channels
        self.spectrum_bars.set_channels(channels)
        self.level_bars.set_channels(channels)
        self._refresh_measure_table()

    # ── Reverberation ──
    def _reverb_selected_channels(self) -> list[int]:
        return [c for c, box in self.reverb_channels.items() if box.isChecked()]

    def _save_reverb_wav(self, dumps: dict, channels: list[int], *,
                         method: str, settings: ExcitationSettings | None) -> None:
        """Keep the waveform a reverberation time was derived from.

        A T20 is a fitted number; without the decay it came from there is no
        way to check it later, or to re-run it with T30. The dump is already
        in hand, so this costs nothing on the wire.
        """
        if not self.recording.enabled:
            return
        signal = settings.signal.value if settings is not None else "decay"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{stamp}_{self._active}_reverb_{method}_{signal}.wav"
        try:
            path = write_dump_wav(
                Path(self.recording.directory or ".") / name,
                dumps, channels, config=self.pi.config,
                fmt=self.recording.fmt, full_scale=self.recording.full_scale,
                meta={
                    "purpose": "reverberation",
                    "method": method,
                    "excitation": signal,
                    "source": (
                        "raw dump (get_raw) — the waveform the "
                        f"{method} fit ran on"
                    ),
                    "impact_source": self.session.source.value,
                    "room": self.session.room.name,
                    "title": self.session.title,
                    "site": self.session.site,
                },
            )
        except Exception as exc:  # noqa: BLE001
            # Losing the recording must not lose the measurement.
            log.warning("could not write the reverberation WAV: %s", exc)
            return
        if path is not None:
            self._reverb_wav = path

    def _analyse_dumps(self, dumps: dict, channels: list[int], *, method: str,
                       settings: ExcitationSettings | None = None) -> dict:
        """Compute T per band for each microphone, then average across them.

        [SPEC] ISO 3382-2 \u2014 the mean over microphone positions is
        **arithmetic**. T is a time, not a level, so energy averaging would be
        wrong. A position whose decay cannot be fitted in some band is left out
        of that band\u2019s mean rather than dragging it toward zero.
        """
        # Save before analysing: if the fit fails, the waveform that failed is
        # exactly the thing worth having.
        self._save_reverb_wav(dumps, channels, method=method, settings=settings)

        bands = self.session.bands
        fraction = self.session.fraction
        per_channel: list[dict] = []
        failures: list[str] = []

        for channel in channels:
            device = self.pi.config.device_of(channel)
            dump = dumps.get(device)
            if dump is None:
                failures.append(f"{channel_label(channel)}: no dump for device {device}")
                continue
            signal = dump.channel(channel)
            if settings is not None and settings.signal.is_deterministic:
                signal = impulse_response(signal, settings, dump.sample_rate)
            try:
                analyse = (reverberation_spectrum if settings is not None and
                           settings.signal.is_deterministic else interrupted_spectrum)
                _, results = analyse(
                    signal, dump.sample_rate, bands, method=method, fraction=fraction
                )
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{channel_label(channel)}: {exc}")
                continue
            rejected = [b for b in bands if b not in results or not results[b].reliable]
            if rejected:
                failures.append(f"{channel_label(channel)}: unreliable RT bands {rejected}")
            else:
                per_channel.append(results)

        if failures or not per_channel:
            raise RuntimeError(
                "Reverberation not stored: incomplete or unreliable measurement. " + "; ".join(failures)
            )
        self._reverb_failures = failures
        return self._merge_channels(per_channel)

    def _fetch_window(self, seconds: float, *, timeout: float = 45.0) -> dict:
        """Pull the most recent `seconds` of waveform from the Pi's ring buffer.

        Two things go wrong here and they need telling apart:

        * **The buffer is shorter than the window.** `get_raw` refuses with
          "not enough data buffered". Only `set_storage` can grow it and that
          needs a stop, which empties the buffer — so after the sound has
          already been made there is nothing to be done except analyse what
          there is. The decay sits at the *end* of the recording and `get_raw`
          returns the most recent samples, so trimming the request keeps the
          part that matters.
        * **The buffer has not filled yet.** Same error, but waiting fixes it.
          A scan that just restarted holds nothing.

        Note there is deliberately no `stream_raw` here. RAW_DUMP is an
        on-demand pull from the ring buffer (§4) and works whether or not the
        Pi is pushing DATA frames; switching raw streaming on for a dump just
        floods the link with waveform nobody reads, and DATA crowds out other
        frames (§6).
        """
        import time as _time

        capacity = float(self.pi.config.buffer_seconds or 0.0)
        wanted = float(seconds)
        if capacity and wanted > capacity:
            wanted = max(1.0, capacity - 0.2)
            self.worker.progress.emit(
                f"Buffer holds {capacity:g} s; analysing the last {wanted:.1f} s"
            )

        # One wall-clock budget for the whole thing, per attempt *and* across
        # retries. Handing the full timeout to every attempt is what made this
        # look like a hang: three retries of a 90 s fetch is four and a half
        # minutes of a frozen-looking window.
        started = _time.monotonic()
        ends_at = started + float(timeout)
        attempt = 0

        while True:
            attempt += 1
            left = ends_at - _time.monotonic()
            if left <= 0:
                raise RuntimeError(
                    f"The Pi did not return {wanted:.1f} s of waveform within "
                    f"{timeout:.0f} s. The scan may have stopped, or the stream "
                    "port may be blocked — check Device / calibration."
                )
            if attempt > 1:
                self.worker.progress.emit(
                    f"Waiting for {wanted:.1f} s of waveform "
                    f"({left:.0f} s left)…"
                )
            try:
                # Never let a single attempt eat the whole budget: leave room
                # for at least one retry at a shorter window.
                return self.pi.fetch_raw(seconds=wanted, timeout=min(left, 30.0))
            except CommandError as exc:
                if "buffered" not in str(exc):
                    raise
                # Either the buffer is still filling or it is simply too small.
                # Waiting fixes the first; only a shorter window fixes the
                # second, and there is no way to tell them apart from here.
                if _time.monotonic() - started < 10.0:
                    _time.sleep(0.5)
                    continue
                if wanted > 2.0:
                    wanted = max(1.0, wanted / 2)
                    continue
                raise RuntimeError(
                    f"The Pi has less than {wanted:.1f} s of waveform buffered "
                    f"(ring buffer {capacity:g} s). Raise 'Raw buffer' under "
                    "Settings → Instrument options → Sampling before "
                    "measuring — changing it restarts the scan and empties "
                    "the buffer, so it cannot be done afterwards."
                ) from exc

    @staticmethod
    def _merge_channels(per_channel: list[dict]) -> dict:
        """Arithmetic mean per band, carried on one of the DecayResults.

        The averaged time is what gets used, but the table also shows the
        correlation, so keep the *worst* position\u2019s rather than an average
        of them \u2014 one bad microphone should stay visible instead of being
        smoothed away.
        """
        if len(per_channel) == 1:
            return per_channel[0]
        merged: dict = {}
        for band in sorted({b for entry in per_channel for b in entry}):
            found = [entry[band] for entry in per_channel if band in entry]
            if not found:
                continue
            worst = min(found, key=lambda r: r.correlation)
            # Curvature is None when T20 and T30 could not both be computed,
            # which means "unknown" rather than "straight" — so average only
            # the positions that actually have it.
            curvatures = [
                r.curvature_percent for r in found if r.curvature_percent is not None
            ]
            merged[band] = replace(
                worst,
                t60=float(np.mean([r.t60 for r in found])),
                curvature_percent=(
                    float(np.mean(curvatures)) if curvatures else None
                ),
            )
        return merged

    def _measure_reverberation(self) -> None:
        if self.pi is None:
            self._warn("Connect to the Pi first")
            return
        channels = self._reverb_selected_channels()
        if not channels:
            self._warn("Tick at least one microphone for the reverberation")
            return
        seconds = self.reverb_seconds.value()
        method = self.reverb_method.currentText()

        def measure():
            dumps = self._fetch_window(seconds, timeout=45.0)
            return self._analyse_dumps(dumps, channels, method=method)

        self._submit(
            "reverb", measure,
            f"Capturing decay on {len(channels)} microphone(s)\u2026",
        )

    # ── External speaker ──
    def _refresh_audio_devices(self) -> None:
        """List the machine's own outputs, remembering the current pick."""
        previous = self.audio_device.currentData()
        self.audio_device.clear()
        if not audio.available():
            self.audio_device.addItem("sounddevice not installed", None)
            self.audio_device.setEnabled(False)
            self.audio_device.setToolTip(audio.INSTALL_HINT)
            return
        devices = audio.output_devices()
        if not devices:
            self.audio_device.addItem("no output device found", None)
            self.audio_device.setEnabled(False)
            return
        self.audio_device.setEnabled(True)
        self.audio_device.setToolTip("Sound card the excitation is played through")
        for device in devices:
            self.audio_device.addItem(device.label, device.index)
        if previous is not None:
            index = self.audio_device.findData(previous)
            if index >= 0:
                self.audio_device.setCurrentIndex(index)

    def _on_reverb_source_changed(self) -> None:
        source = self.reverb_source.currentData()
        method = self.external_method.currentData()
        through_pc = source == "external" and method == "soundcard"

        # The signal picker applies to the Pi's DAC, this machine's sound card
        # and the exported sweep alike, so it stays visible for all of them —
        # only manual entry has no signal to choose.
        self.excite_box.setVisible(source != "manual")
        self.excite_box.setTitle(
            "Excitation (DT9837A analog output)" if source == "internal"
            else "Excitation signal"
        )
        # ...but the Output button drives the Pi's DAC specifically.
        self.output_btn.setVisible(source == "internal")
        self.output_state.setVisible(source == "internal")

        self.external_box.setVisible(source == "external")
        self.manual_box.setVisible(source == "manual")
        if source == "external":
            self.save_sweep_btn.setVisible(method == "sweep")
            # Playing from here replaces the manual start/stop bracketing.
            self.audio_device.setVisible(through_pc)
            self.refresh_audio_btn.setVisible(through_pc)
            self.play_btn.setVisible(through_pc)
            self.external_record_btn.setVisible(not through_pc)
        if source == "manual" and self.manual_table.rowCount() == 0:
            self._rebuild_manual_table()

    def _save_sweep_file(self) -> None:
        """Write the sweep out so it can be played through the external system.

        Deconvolution needs the reference to match what was played sample for
        sample, so the file is generated from the same settings the analysis
        will use rather than from any file the operator happens to have.
        """
        settings = replace(self._excitation(), signal=Signal.SWEEP)
        rate = float(self.pi.config.sample_rate) if self.pi is not None else 48000.0
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save sweep",
            f"sweep_{settings.f_min:.0f}-{settings.f_max:.0f}Hz.wav", "WAV (*.wav)"
        )
        if not path:
            return
        waveform = generate(settings, rate)
        write_wav(Path(path), waveform, rate)
        self._external_settings = settings
        self.statusBar().showMessage(
            f"Sweep written: {Path(path).name} "
            f"({waveform.size / rate:.1f} s at {rate / 1000:.1f} kHz). "
            "Play this exact file \u2014 the analysis assumes it."
        )

    def _play_and_measure(self) -> None:
        """Play through this computer's sound card and analyse the same pass.

        The Pi is already buffering, so nothing has to be started on it — the
        excitation is played here and the window is pulled afterwards. Timing
        is a host timestamp rather than a hardware trigger, which is fine: a
        sweep's inverse filter finds its own peak, and the MLS path recovers
        the residual offset itself.
        """
        if self.pi is None or not self.pi.config.running:
            self._warn("The scan must be running before you can measure")
            return
        channels = self._reverb_selected_channels()
        if not channels:
            self._warn("Tick at least one microphone for the reverberation")
            return
        device = self.audio_device.currentData()
        if device is None:
            self._warn(audio.INSTALL_HINT if not audio.available()
                       else "No sound card to play through")
            return

        settings = self._excitation()
        method = self.reverb_method.currentText()
        window = self.reverb_seconds.value()
        # Play at the Pi's own rate so a recorded MLS is the sequence the
        # deconvolver expects, sample for sample.
        rate = float(self.pi.config.sample_rate)
        waveform = generate(settings, rate)
        total = waveform.size / rate

        capacity = float(self.pi.config.buffer_seconds or 0.0)
        if capacity and total + window > capacity:
            self._warn(
                f"This pass needs {total + window:.1f} s but the Pi only buffers "
                f"{capacity:g} s. Shorten the signal, or raise 'Raw buffer' under "
                "Settings → Instrument options → Sampling first."
            )
            return

        def run():
            import time as _time

            self._player.play(waveform, rate, device=device,
                              channels=1 if settings.channel == 0 else 2)
            if not self._player.wait(total + 15.0):
                self._player.stop()
                raise RuntimeError("Playback did not finish; the pass is unusable")
            if self._player.error:
                raise RuntimeError(f"Playback failed: {self._player.error}")
            _time.sleep(window)          # let the decay land in the buffer

            dumps = self._fetch_window(total + window, timeout=45.0)
            return self._analyse_dumps(
                dumps, channels, method=method,
                settings=settings if settings.signal.is_deterministic else None,
            )

        self._submit(
            "reverb", run,
            f"Playing {settings.signal.label} ({total:.1f} s) through the sound card…",
        )

    def _toggle_external_recording(self) -> None:
        if self.external_method.currentData() == "noise":
            self._open_rt_sequence()
            return
        if self.pi is None or not self.pi.config.running:
            self._warn("The scan must be running before you can record")
            return
        channels = self._reverb_selected_channels()
        if not channels:
            self._warn("Tick at least one microphone for the reverberation")
            return

        if not self._external_recording:
            # Nothing to start on the Pi \u2014 the ring buffer is already
            # filling. This only marks when the operator began.
            self._external_recording = True
            self._external_started = time.monotonic()
            self.external_record_btn.setText("\u25a0 Stop and analyse")
            self.external_state.setText("recording \u2014 play the signal now")
            self.external_state.setStyleSheet("color:#f59e0b;")
            return

        elapsed = time.monotonic() - self._external_started
        self._external_recording = False
        self.external_record_btn.setText("\u25cf Start recording")
        self.external_state.setText(f"analysing {elapsed:.1f} s")
        self.external_state.setStyleSheet("color:#6b7280;")

        method = self.reverb_method.currentText()
        wants_sweep = self.external_method.currentData() == "sweep"
        settings = self._external_settings if wants_sweep else None
        if wants_sweep and settings is None:
            self._warn(
                "Save the sweep file first \u2014 the deconvolution has to know "
                "exactly which signal was played"
            )
            return
        seconds = min(max(elapsed, 1.0), 60.0)

        def measure():
            dumps = self._fetch_window(seconds, timeout=45.0)
            return self._analyse_dumps(dumps, channels, method=method, settings=settings)

        self._submit("reverb", measure, f"Analysing {seconds:.1f} s\u2026")

    # ── Manual entry ──
    def _rebuild_manual_table(self) -> None:
        table = self.manual_table
        existing = self.session.room.reverberation
        bands = self.session.bands
        table.setRowCount(len(bands))
        for row, band in enumerate(bands):
            label = QtWidgets.QTableWidgetItem(f"{band:g}")
            label.setFlags(QtCore.Qt.ItemIsEnabled)
            table.setItem(row, 0, label)
            value = existing.get(band)
            table.setItem(
                row, 1, QtWidgets.QTableWidgetItem(f"{value:.2f}" if value else "")
            )

    def _fill_manual_reverberation(self) -> None:
        if self.manual_table.rowCount() == 0:
            self._rebuild_manual_table()
        value = self.manual_fill.value()
        for row in range(self.manual_table.rowCount()):
            self.manual_table.setItem(row, 1, QtWidgets.QTableWidgetItem(f"{value:.2f}"))

    def _apply_manual_reverberation(self) -> None:
        values: dict[float, float] = {}
        bad: list[str] = []
        for row in range(self.manual_table.rowCount()):
            band_item = self.manual_table.item(row, 0)
            value_item = self.manual_table.item(row, 1)
            if band_item is None or value_item is None:
                continue
            text = value_item.text().strip()
            if not text:
                continue
            try:
                seconds = float(text)
            except ValueError:
                bad.append(band_item.text())
                continue
            if seconds <= 0:
                bad.append(band_item.text())
                continue
            values[float(band_item.text())] = seconds

        if bad:
            self._warn(f"Not a reverberation time: {', '.join(bad)} Hz")
            return
        if not values:
            self._warn("Enter a value for at least one band")
            return
        # Hand-entered values replace the measured ones outright. Averaging
        # them together would make a reported T impossible to trace back to
        # either its measurement or its source.
        self._reverb_runs.clear()
        self.session.room.reverberation = values
        self._refresh_reverb_table()
        self.statusBar().showMessage(
            f"Applied {len(values)} manually entered band(s)"
        )

    def _clear_reverberation(self) -> None:
        self._reverb_runs.clear()
        self.session.room.reverberation.clear()
        self._refresh_reverb_table()

    # ── Calibration ──
    def _calibrate(self) -> None:
        channel = self.cal_channel.value() - 1   # display is 1-based, wire is 0-based
        level = self.cal_level.value()
        def calibrate():
            result = self.pi.calibrate(channel, level_db=level, apply=True)
            # Re-read the handshake so the channel listing shows the new
            # sensitivity. Without this the table keeps reporting the old
            # value and the calibration looks as though it did not take.
            self.pi.refresh()
            return result

        self._submit(
            "calibrate", calibrate, f"Calibrating {channel_label(channel)}\u2026"
        )

    # ── Rating ──
    def _load_curve(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Inverse-A reference curve", "", "JSON (*.json)"
        )
        if not path:
            return
        try:
            self.curve = InverseACurve.load(path)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"Could not load curve: {exc}")
            return
        self._update_curve_label()

    def _update_curve_label(self) -> None:
        curve = InverseACurve.legacy_heavy() if self.session.source is ImpactSource.BANG else self.curve
        self.curve_load_btn.setEnabled(self.session.source is not ImpactSource.BANG)
        mark = "verified" if curve.verified else "unverified"
        self.curve_label.setText(f"Inverse-A curve: {curve.source} [{mark}]")
        self.curve_label.setStyleSheet(
            "color:#4ade80;" if curve.verified else "color:#fbbf24;"
        )

    def _evaluate(self) -> None:
        try:
            result = self.session.evaluate(curve=self.curve)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"Evaluation failed: {exc}")
            return

        lines = [
            f"\u25a0 {self.session.title or '(untitled)'}",
            f"  Site {self.session.site or '-'} / Operator {self.session.operator or '-'}",
            f"  Source {self.session.source.label} \u00b7 per band {self.session.source.quantity}",
            f"  Rated as {self.session.source.single_number_symbol} "
            f"({self.session.source.single_number_method})",
            f"  {self.session.progress[0]}/{self.session.progress[1]} combinations measured",
            f"  Room {self.session.room.volume:g} m\u00b3, "
            f"mean T {self.session.room.mean_reverberation():.2f} s"
            + ("" if self.session.source.requires_standardisation
               else "  (not applied \u2014 heavy impact is not standardised)"),
            "",
            result.summary(),
            "",
        ]
        if result.iso_717_2 is not None:
            lines.append(result.iso_717_2.summary())
        if result.inverse_a is not None:
            lines.append("")
            lines.append(result.inverse_a.summary())
        self.result_text.setPlainText("\n".join(lines))
        self.tabs.setCurrentIndex(3)

    def _export_report(self) -> None:
        text = self.result_text.toPlainText()
        if not text.strip():
            self._warn("Run the evaluation first")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export result", f"{self.session.title or 'result'}.txt", "Text (*.txt)"
        )
        if path:
            Path(path).write_text(text, encoding="utf-8")
            self.statusBar().showMessage(f"Saved: {path}")

    # ── Job results ──
    def _on_job_done(self, name: str, result) -> None:
        if name == "connect":
            self.pi = result
            self.health_panel.unavailable("조회 중")
            self._poll_health()
            self.connect_btn.setText("Disconnect")
            self.scan_btn.setEnabled(True)
            self.live.configure_bands(self.pi.config)
            self._show_device_info()
            available = self.pi.output_available
            self.output_btn.setEnabled(available)
            if not available:
                self.output_state.setText("no analog output on this hardware")
            self.status.set_state("ok", f"Connected \u00b7 {self.pi.config.protocol}")
        elif name in ("start", "configure"):
            if self.pi:
                self.live.configure_bands(self.pi.config)
            # Read the state back rather than assuming it. Configuring does not
            # always leave the scan running — applying a session while stopped
            # legitimately keeps it stopped — and a button that says
            # "Stop scan" over a stopped scan sends the next click to `stop`,
            # so the operator has to press it twice to get any data.
            running = bool(self.pi and self.pi.config.raw.get("running"))
            self.scan_btn.setText("Stop scan" if running else "Start scan")
            self.status.set_state("ok", "Scanning" if running else "Configured")
            self._show_device_info()
        elif name == "stop":
            self._stop_recording()
            self.scan_btn.setText("Start scan")
            self.status.set_state("ok", "Stopped")
        elif name == "output_start":
            self._set_output_state(True, f"playing {result.get('signal', '')}")
        elif name == "output_stop":
            self._set_output_state(False)
        elif name == "capture":
            self._stop_recording()
            self._store_measurement(result)
        elif name == "background":
            # The background is stored as a single channel-averaged spectrum
            tables = [t for t in result["levels"].values() if t]
            if tables:
                bands = sorted(set().union(*[set(t) for t in tables]))
                self.session.background = {
                    band: float(
                        10.0 * np.log10(
                            np.mean([10 ** (t[band] / 10) for t in tables if band in t])
                        )
                    )
                    for band in bands
                }
                # Both halves of the set get it; each trims to its own bands.
                self.other_session.background = dict(self.session.background)
                self.spectrum_bars.set_captured(result["levels"])
                self.statusBar().showMessage(
                    f"Background stored for both sources: {len(bands)} bands "
                    f"averaged over {len(tables)} channels"
                )
            else:
                self._warn("No background band levels were received")
        elif name == "options":
            self.live.configure_bands(self.pi.config)
            self._show_device_info()
            self.statusBar().showMessage("Instrument options applied")
        elif name == "reverb":
            self._store_reverberation(result)
        elif name == "calibrate":
            self.session.calibration[str(result["channel"])] = result["new_sensitivity"]
            # `result["channel"]` is the wire number, which counts from 0.
            # Printing it raw said "ch1" for a calibration of Ch2 \u2014 the value
            # was right, the label was not.
            self._show_device_info()          # redraw with the new sensitivity
            self.device_text.appendPlainText(
                f"Calibrated {channel_label(result['channel'])}: "
                f"{result['old_sensitivity']:.3f} \u2192 "
                f"{result['new_sensitivity']:.3f} mV/Pa ({result['change_db']:+.2f} dB)"
            )
            self.status.set_state("ok", "Calibrated")
        elif name == "save_config":
            self.status.set_state("ok", "Settings saved to Pi")

        self.capture_btn.setEnabled(True)
        if name not in ("connect", "start", "stop", "configure"):
            self.status.set_state("ok", "Idle")

    def _on_job_failed(self, name: str, message: str, detail: str) -> None:
        self.capture_btn.setEnabled(True)
        # Even on failure the open WAV files must be closed
        if name in ("capture", "background"):
            self._stop_recording()
        if self._closing:
            # No dialog while shutting down — a modal on a closing window
            # cannot be answered.
            return
        self.status.set_state("error", f"{name} failed")
        self.statusBar().showMessage(f"{name} failed: {message}")
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Critical)
        box.setWindowTitle(f"{name} failed")
        box.setText(message)
        box.setDetailedText(detail)
        box.exec()

    def _store_measurement(self, result: dict) -> None:
        if result.get("impact_source", self.session.source.value) != self.session.source.value:
            self._warn("Impact source changed during capture; select the original source and recapture")
            return
        per_channel = result["levels"]
        filled = {ch: table for ch, table in per_channel.items() if table}
        if not filled:
            self._warn("No band levels received — check that band output is enabled")
            return

        note = "; ".join(filter(None, [result.get("analysis_note", ""),
            "" if result["valid"] else "; ".join(result["reasons"])]))
        source = self.session.source
        added = self.session.add_all(
            Measurement(
                source_position=result["source_position"],
                channel=channel,
                levels=levels,
                broadband=result["broadband"].get(channel),
                quantity=source.quantity,
                weighting=source.frequency_weighting,
                valid=result["valid"],
                note=note,
                fraction=self.session.fraction,
            )
            for channel, levels in sorted(filled.items())
        )
        self.spectrum_bars.set_captured(filled)
        self._refresh_measure_table()
        self.statusBar().showMessage(
            f"Source position {result['source_position']} \u2014 {added} channel(s) stored"
        )
        self._advance_position()

    def _advance_position(self) -> None:
        src = self.src_pos_current.value()
        if src < self.session.source_positions:
            self.src_pos_current.setValue(src + 1)

    def _store_reverberation(self, results: dict) -> None:
        self._reverb_runs.append(results)
        averaged = average_reverberation(self._reverb_runs)
        self.session.room.reverberation = averaged
        self._refresh_reverb_table(results)
        if self._reverb_wav is not None:
            self.statusBar().showMessage(
                f"{len(results)} band(s) from {len(self._reverb_runs)} run(s) · "
                f"waveform saved: {self._reverb_wav.name}"
            )
            self._reverb_wav = None

    # ── Heavy / light set ──
    def _switch_impact_source(self) -> None:
        """Move to the other half of the set.

        Heavy and light need different bands (50-630 vs 100-3150) *and*
        different per-band quantities (Fmax vs Leq), so the Pi has to be
        reconfigured — there is no way to record both from one scan. The room
        data travels across so it is only ever measured once.
        """
        self._share_to_other_session()
        self._active = "light" if self._active == "heavy" else "heavy"

        # Point the source selector at whatever the newly active half uses, so
        # the two never disagree about which source is being measured.
        index = list(ImpactSource).index(self.session.source)
        blocked = self.source_combo.blockSignals(True)
        self.source_combo.setCurrentIndex(index)
        self.source_combo.blockSignals(blocked)

        self._sync_ui_from_session()
        self._apply_session()

    def _sync_ui_from_session(self) -> None:
        """Push the active session back into the widgets."""
        session = self.session
        self.title_edit.setText(session.title)
        self.site_edit.setText(session.site)
        self.operator_edit.setText(session.operator)
        self.src_pos_spin.setValue(session.source_positions)
        self.volume_spin.setValue(session.room.volume)
        for channel, box in self.channel_checks.items():
            blocked = box.blockSignals(True)
            box.setChecked(channel in session.channels)
            box.blockSignals(blocked)
        self._reverb_runs.clear()
        self._refresh_reverb_table()
        self._refresh_measure_table()
        self._update_set_label()

    def _update_set_label(self) -> None:
        """Show both halves at once so neither is forgotten."""
        parts = []
        for key in ("heavy", "light"):
            session = self._sessions[key]
            done, need = session.progress
            mark = "\u25b6 " if key == self._active else "   "
            parts.append(f"{mark}{key}: {done}/{need}")
        self.set_label.setText("   ".join(parts))
        self.set_label.setStyleSheet("color:#9aa0a6;")

    # ── Reset ──
    def _reset_measurements(self) -> None:
        """Discard data, letting the operator pick how much.

        A single "clear everything" button is the wrong shape for this: the
        common case by far is re-doing one source position after a bad run,
        and losing the background and the reverberation with it would mean
        re-measuring the whole room. So the scope is chosen explicitly, and
        the button says what it is about to destroy.
        """
        position = self.src_pos_current.value()
        scopes = [
            (f"Source position {position} only",
             f"{len(self._measurements_at(position))} measurement(s)"),
            ("All measurements",
             f"{len(self.session.measurements)} measurement(s), keeping "
             "background and reverberation"),
            ("Background only",
             f"{len(self.session.background)} band(s)"),
            ("Reverberation only",
             f"{len(self.session.room.reverberation)} band(s) "
             f"from {len(self._reverb_runs)} run(s)"),
            ("Everything (new session)",
             "measurements, background, reverberation and room data"),
        ]
        labels = [f"{name}  —  {detail}" for name, detail in scopes]
        choice, ok = QtWidgets.QInputDialog.getItem(
            self, "Reset", "What should be discarded?", labels, 0, False
        )
        if not ok:
            return
        index = labels.index(choice)

        if index == 0:
            keep = [m for m in self.session.measurements if m.source_position != position]
            removed = len(self.session.measurements) - len(keep)
            self.session.measurements = keep
            message = f"Cleared {removed} measurement(s) at source position {position}"
        elif index == 1:
            removed = len(self.session.measurements)
            self.session.measurements = []
            message = f"Cleared {removed} measurement(s)"
        elif index == 2:
            self.session.background = {}
            self.spectrum_bars.set_reference({})
            message = "Cleared the background"
        elif index == 3:
            self._reverb_runs = []
            self.session.room.reverberation = {}
            self._refresh_reverb_table()
            message = "Cleared the reverberation"
        else:
            self.session.measurements = []
            self.session.background = {}
            self.session.room.reverberation = {}
            self.session.room.volume = 0.0
            self._reverb_runs = []
            self.spectrum_bars.set_reference({})
            self._refresh_reverb_table()
            message = "Session cleared"

        # Whatever was cleared, the next thing measured is position 1 again.
        self.src_pos_current.setValue(1)
        self._refresh_measure_table()
        self.statusBar().showMessage(message)

    def _measurements_at(self, position: int) -> list:
        return [m for m in self.session.measurements if m.source_position == position]

    # ── Table refresh ──
    def _refresh_measure_table(self) -> None:
        table = self.measure_table
        bands = self.session.bands
        if bands != self._measure_columns:
            # The band set changes with the impact source, so the columns are
            # rebuilt rather than reused — otherwise heavy readings would be
            # filed under light band headings after a switch.
            self._measure_columns = bands
            table.setColumnCount(3 + len(bands) + 1)
            table.setHorizontalHeaderLabels(
                ["Source", "Channel", "Time"]
                + [f"{b:g}" for b in bands]
                + ["Note"]
            )
        quantity = self.session.source.quantity
        table.setRowCount(len(self.session.measurements))
        for row, m in enumerate(self.session.measurements):
            values = [
                str(m.source_position),
                channel_label(m.channel),
                m.timestamp[11:19] if len(m.timestamp) > 11 else m.timestamp,
            ]
            values += [
                f"{m.levels[band]:.1f}" if band in m.levels else "-"
                for band in bands
            ]
            values.append(m.note or ("ok" if m.valid else "invalid"))
            for col, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                if 3 <= col < 3 + len(bands):
                    item.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                    item.setToolTip(f"{bands[col - 3]:g} Hz  {quantity}")
                table.setItem(row, col, item)
        self._update_set_label()
        done, need = self.session.progress
        finished = sorted(self.session.completed_source_positions())
        self.progress_label.setText(
            f"Progress: {done} / {need}   source positions done: {finished or 'none'}"
            + ("   \u2714 complete" if self.session.complete else "")
        )
        self._update_snq_estimate()

    def _update_snq_estimate(self) -> None:
        """Project the single number from whatever has been measured so far.

        Useful on site: if the running figure is nowhere near the 49 dB limit
        you want to know at position 2, not after packing up. It is explicitly
        marked provisional and lists what is still missing, because an
        estimate taken from half the positions and no reverberation can move
        by several decibels before the end — reading it as the answer would
        be worse than not showing it.
        """
        if not self.session.measurements:
            self.snq_label.setText("")
            return

        session = self.session
        try:
            result = session.evaluate(curve=None)
        except Exception as exc:  # noqa: BLE001
            self.snq_label.setText(f"Estimate unavailable: {exc}")
            self.snq_label.setStyleSheet("color:#9aa0a6;")
            return

        if session.source is ImpactSource.BANG:
            if result.inverse_a is None:
                self.snq_label.setText("Legacy inverse-A unavailable: " + "; ".join(result.warnings))
            else:
                self.snq_label.setText(f"구법 L'i,Fmax,AW = {result.inverse_a.value:.0f} dB "
                    f"(상회값 합 {result.inverse_a.deviation_sum:.1f}/8 dB; 참고 분석)")
            self.snq_label.setStyleSheet("color:#fbbf24;")
            return
        symbol = result.post_verification_symbol
        if result.post_verification is None:
            reason = result.warnings[0] if result.warnings else "not enough data yet"
            self.snq_label.setText(f"{symbol}: not computable yet \u2014 {reason}")
            self.snq_label.setStyleSheet("color:#9aa0a6;")
            return

        value = result.post_verification
        done, need = session.progress
        caveats = []
        if done < need:
            caveats.append(f"{done}/{need} measured")
        # Heavy impact is never standardised (L'iA,Fmax is the level as
        # measured), so a missing volume/T is not something the operator has to
        # go and fix — listing it would keep a finished heavy set looking
        # provisional for ever.
        if session.source.requires_standardisation and not session.room.configured:
            caveats.append("not standardised (no volume/reverberation)")
        if not session.background:
            caveats.append("no background correction")

        verdict = "within" if value <= result.limit_db else "over"
        colour = "#4ade80" if value <= result.limit_db else "#f87171"
        note = f"  \u00b7  provisional: {', '.join(caveats)}" if caveats else "  \u00b7  final"
        self.snq_label.setText(
            f"Estimated {symbol} \u2248 {value:.1f} dB  "
            f"({verdict} the {result.limit_db:.0f} dB limit){note}"
        )
        self.snq_label.setStyleSheet(f"color:{colour};")

    def _refresh_reverb_table(self, latest: dict | None = None) -> None:
        table = self.reverb_table
        averaged = self.session.room.reverberation
        bands = sorted(averaged)
        table.setRowCount(len(bands))
        for row, band in enumerate(bands):
            result = (latest or {}).get(band)
            values = [
                f"{band:g}",
                f"{averaged[band]:.3f}",
                f"{result.correlation:.4f}" if result else "-",
                f"{result.curvature_percent:+.1f}" if result and result.curvature_percent is not None else "-",
                ("good" if result.reliable else "; ".join(result.warnings())) if result else "-",
            ]
            for col, value in enumerate(values):
                table.setItem(row, col, QtWidgets.QTableWidgetItem(value))

    def _show_device_info(self) -> None:
        if self.pi is None:
            return
        config = self.pi.config
        lines = [
            f"Protocol    : {config.protocol}"
            + ("  (frame indices present)" if config.has_sample_index else "  \u26a0 no frame index"),
            f"Sample rate : {config.sample_rate:g} Hz",
        ]
        if config.resample_active:
            lines.append(f"Resample    : active \u2192 {config.resample.get('output_rate'):g} Hz")
        elif config.resample.get("enabled"):
            lines.append("Resample    : \u26a0 enabled but not active \u2014 drift remains")
        else:
            lines.append("Resample    : off")
        ppm = config.clock_ppm
        if ppm:
            detail = "  ".join(f"dev{d}: {p:+.2f} ppm" for d, p in sorted(ppm.items()))
            lines.append(
                f"Clock       : {detail} "
                + ("(settled)" if config.clock_settled else "\u26a0 not settled \u2014 warm-up needed")
            )
        lines.append(f"Weighting   : {config.weighting.get('frequency')}/{config.weighting.get('time')}")
        lines.append(f"Bands       : {config.bands}")
        lines.append("")
        for info in config.all_channels():
            mark = "calibrated" if info.calibrated else "uncalibrated (V)"
            lines.append(
                f"  {channel_label(info.global_index):<5} dev{info.device} {info.device_type:<8} "
                f"{info.sensitivity_mv_per_unit:>8.3f} mV/{info.units}  {mark}"
            )
        self.device_text.setPlainText("\n".join(lines))

    # ── Live refresh ──
    def _refresh_live(self) -> None:
        levels = self.live.snapshot_levels()
        channels = self._selected_channels() or sorted(levels)
        # Both meters show only the channels in use. An unused input still
        # streams, and drawing it squeezes the ones that matter.
        self.level_bars.set_channels(channels)
        self.level_bars.set_levels(levels)
        self.spectrum_bars.set_channels(channels)
        spectra = {channel: self.live.snapshot_spectrum(channel) for channel in channels}
        self.spectrum_bars.set_series(spectra)
        # If the band table went missing (e.g. reconnect), pick it back up.
        if self.pi is not None and not self.live.bands_known:
            self.live.configure_bands(self.pi.config)

        if self.live.capturing:
            remaining = self.duration_spin.value() - self.live.capture_elapsed
            self.statusBar().showMessage(f"Capturing\u2026 {max(0.0, remaining):.1f} s left")
        elif (self.pi is not None and not self.pi.overloads
              and self.pi.config.running and channels
              and self.live.bands_known
              and any(len(spectra[ch]) < len(self.session.bands) for ch in channels)):
            self.statusBar().showMessage(
                "Waiting for current band levels — filter settling or stream interruption")
        elif self.pi is not None and self.pi.overloads:
            channels = sorted({o.get("channel") for o in self.pi.overloads})
            self.statusBar().showMessage(
                "\u26a0 Overload on " + ", ".join(channel_label(c) for c in channels)
            )
        elif self.statusBar().currentMessage().startswith("Waiting for current band levels"):
            self.statusBar().showMessage("Scanning" if self.pi and self.pi.config.running else "Stopped")

    # ── Shutdown ──
    def closeEvent(self, event) -> None:  # noqa: N802
        """Shutdown order matters.

        Closing the socket while the worker still has a command in flight breaks
        the pending request with "connection lost" and pops a pointless error on
        a window that is already closing. Drain the worker first, then close.
        """
        self._closing = True
        self._player.stop()          # do not leave a tone playing after close
        self.timer.stop()
        self.health_timer.stop()
        self.health_worker.stop()
        self.worker.stop()
        self._stop_recording()  # finalise open WAV headers
        if self.pi is not None:
            try:
                self.pi.close()
            except Exception:  # noqa: BLE001
                pass
            self.pi = None
        super().closeEvent(event)


def build_app(argv: list[str] | None = None):
    """Build the QApplication (also used by the tests)."""
    argv = list(argv if argv is not None else sys.argv)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(argv)
    app.setApplicationName("Floor Impact Sound Meter")
    app.setStyle("Fusion")
    return app


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Floor impact sound meter")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--control", type=int, default=5000)
    parser.add_argument("--stream", type=int, default=5001)
    parser.add_argument("--demo", action="store_true", help="run with the built-in simulator")
    args = parser.parse_args(argv)

    host, control, stream = args.host, args.control, args.stream
    if args.demo:
        import pislm_sim

        control, stream = 15100, 15101
        host = "127.0.0.1"
        pislm_sim.STATE.protocol_version = 4
        pislm_sim.serve(control, stream)

    app = build_app([])
    window = MainWindow(host, control, stream)
    window.show()
    if args.demo:
        window.statusBar().showMessage("Demo mode \u2014 press Connect to reach the built-in simulator")
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
