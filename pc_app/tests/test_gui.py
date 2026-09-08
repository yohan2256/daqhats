"""GUI startup and full measurement flow (offscreen).

Widgets are built without showing a real window and driven against the
simulator: connect -> configure -> capture -> reverberation -> rating. This
checks the program actually runs, not merely that it imports — button handlers
are invoked and the results must land in the session.
"""

from __future__ import annotations

import os
import socket
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("PySide6", reason="PySide6 not installed — skipping GUI tests")

from PySide6 import QtWidgets  # noqa: E402

import pislm_sim  # noqa: E402
from app.live import LiveState  # noqa: E402
from app.main import MainWindow, build_app  # noqa: E402
from pislm import BandLevelFrame, LevelFrame  # noqa: E402
from pislm.excitation import Signal  # noqa: E402
from pislm.session import ImpactSource  # noqa: E402

import numpy as np  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def qapp():
    app = build_app([])
    yield app


@pytest.fixture(autouse=True)
def no_modal_dialogs(monkeypatch):
    """Block modal dialogs.

    Nobody can press OK offscreen, so `exec()` blocks forever. That actually
    hung the suite once and hid a shutdown-ordering bug behind it. Record the
    calls instead.
    """
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "exec", lambda self: calls.append(("exec", self.text()))
    )
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "warning",
        classmethod(lambda cls, *a, **k: calls.append(("warning", a[2] if len(a) > 2 else ""))),
    )
    return calls


@pytest.fixture(scope="module")
def sim():
    pislm_sim.reset_state()
    pislm_sim.OPTS.fragment = False
    pislm_sim.OPTS.shuffle = False
    pislm_sim.OPTS.drop = 0.0
    pislm_sim.OPTS.overload = 0.0
    pislm_sim.STATE.protocol_version = 4
    ctl_port, stm_port = free_port(), free_port()
    ctl, stm = pislm_sim.serve(ctl_port, stm_port)
    time.sleep(0.2)
    yield ctl_port, stm_port
    pislm_sim.stop_scan()
    ctl.shutdown()
    stm.shutdown()


def pump(app, seconds: float = 0.2) -> None:
    """Actually run the Qt event loop so worker signals get delivered."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


def ensure_scanning(app, window, running: bool = True) -> bool:
    """Put the scan into a known state.

    The simulator is shared across the module and some tests deliberately
    close a window with a stop command still queued, so the scan may already
    be running when the next test starts. Clicking Start blindly would then
    *stop* it, and the failure surfaces much later as "no data".
    """
    # Ask the Pi rather than trusting the cached handshake: a previous test
    # may have stopped the scan without this window hearing about it, and
    # acting on the stale value clicks the button the wrong way.
    window.pi.refresh()
    if bool(window.pi.config.running) != running:
        window.scan_btn.click()
    return wait_for(app, lambda: bool(window.pi.config.running) is running, 15.0)


def wait_for(app, predicate, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ── LiveState unit tests ────────────────────────────────────────────
class TestLiveState:
    def test_level_frames_update_current_value(self):
        live = LiveState()
        live.feed(LevelFrame(2, np.array([50.0, 55.0])))
        assert live.snapshot_levels()[2] == 55.0

    def test_capture_tracks_maximum_not_last(self):
        """Heavy impact needs the maximum, not the most recent value."""
        live = LiveState()
        live.start_capture()
        live.feed(LevelFrame(0, np.array([50.0, 80.0])))
        live.feed(LevelFrame(0, np.array([60.0, 61.0])))
        result = live.finish_capture([0])
        assert result.broadband_max[0] == 80.0

    @staticmethod
    def _config(bands, fraction=3):
        class Config:
            raw = {"band_table": [{"bands": bands}]}

        Config.bands = {"fraction": fraction}
        return Config()

    def test_band_capture_uses_nominal_centers(self):
        live = LiveState()
        live.configure_bands(
            self._config([{"index": 0, "center": 63.0957},
                          {"index": 1, "center": 125.8925}])
        )
        live.start_capture()
        live.feed(BandLevelFrame(0, 3, np.array([40.0, 44.0])))
        live.feed(BandLevelFrame(1, 3, np.array([38.0])))
        result = live.finish_capture([3])
        assert result.band_max[3] == {63: 44.0, 125: 38.0}

    def test_channels_are_captured_simultaneously_and_kept_apart(self):
        """One excitation records several channels at once, kept apart."""
        live = LiveState()
        live.configure_bands(self._config([{"index": 0, "center": 63.0957}]))
        live.start_capture()
        live.feed(BandLevelFrame(0, 1, np.array([55.0])))
        live.feed(BandLevelFrame(0, 3, np.array([44.0])))
        live.feed(LevelFrame(1, np.array([70.0])))
        live.feed(LevelFrame(3, np.array([62.0])))

        result = live.finish_capture([1, 3])
        assert result.band_max[1] == {63: 55.0}
        assert result.band_max[3] == {63: 44.0}
        assert result.broadband_max == {1: 70.0, 3: 62.0}

    def test_unselected_channels_are_excluded(self):
        live = LiveState()
        live.configure_bands(self._config([{"index": 0, "center": 63.0957}]))
        live.start_capture()
        live.feed(BandLevelFrame(0, 1, np.array([99.0])))
        live.feed(BandLevelFrame(0, 3, np.array([44.0])))
        result = live.finish_capture([3])
        assert set(result.band_max) == {3}
        assert result.band_max[3] == {63: 44.0}


# ── Window startup ──────────────────────────────────────────────────
class TestWindowStartup:
    def test_window_constructs_and_shows(self, qapp, sim):
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window.show()
            pump(qapp)
            assert window.isVisible()
            assert window.tabs.count() == 5
            assert window.windowTitle() == "Floor Impact Sound Meter"
        finally:
            window.close()
            pump(qapp)

    def test_all_tabs_render(self, qapp, sim):
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window.show()
            for index in range(window.tabs.count()):
                window.tabs.setCurrentIndex(index)
                pump(qapp, 0.05)
                assert window.tabs.currentWidget() is not None
        finally:
            window.close()
            pump(qapp)

    def test_widgets_paint_without_data(self, qapp, sim):
        """Painting with no data must not raise."""
        from PySide6 import QtGui

        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window.show()
            pump(qapp, 0.1)
            for widget in (window.level_bars, window.spectrum_bars):
                image = QtGui.QImage(widget.size(), QtGui.QImage.Format_ARGB32)
                widget.render(image)
        finally:
            window.close()
            pump(qapp)


# ── Full workflow ───────────────────────────────────────────────────
class TestFullWorkflow:
    def test_connect_configure_capture_evaluate(self, qapp, sim):
        """The real order of operations: connect through to rating."""
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        window.show()
        try:
            # 1) connect
            window.connect_btn.click()
            assert wait_for(qapp, lambda: window.pi is not None         ), "connection failed"
            assert window.pi.config.protocol == "pislm/4"
            assert window.scan_btn.isEnabled()

            # 2) session — rubber ball, 2 source positions x 3 channels at once
            window.source_combo.setCurrentIndex(
                list(ImpactSource).index(ImpactSource.RUBBER_BALL)
            )
            window.src_pos_spin.setValue(2)
            for channel, box in window.channel_checks.items():
                box.setChecked(channel in (0, 2, 3))
            window.volume_spin.setValue(38.0)
            # Two microphones, so the run also exercises the averaging path.
            for channel, box in window.reverb_channels.items():
                box.setChecked(channel in (0, 1))
            window.title_edit.setText("automated check")
            window._apply_session()
            assert wait_for(qapp, lambda: window.worker.pending == 0, 20.0)
            pump(qapp, 0.3)
            assert window.session.source is ImpactSource.RUBBER_BALL
            assert window.session.channels == [0, 2, 3]
            assert window.session.room.volume == 38.0
            assert window.session.required_count == 6  # 2 source positions x 3 channels
            # Analysis must be 1/3 octave, 6th order
            assert window.pi.config.bands["fraction"] == 3
            assert window.pi.config.bands["order"] == 6
            # A-weighting is applied as band values, not a filter, so device = Z
            assert window.pi.config.weighting["frequency"] == "Z"
            assert window.pi.config.weighting["time"] == "Fast"

            # 3) start the scan -> live frames arrive
            window.scan_btn.click()
            assert wait_for(qapp, lambda: window.pi.config.running, 15.0         ), "scan did not start"
            assert wait_for(
                qapp, lambda: bool(window.live.snapshot_levels()), 10.0
                     ), "no level frames"
            assert wait_for(
                qapp, lambda: bool(window.live.snapshot_spectrum(0)), 10.0
                     ), "no band frames"

            # 4) background
            window.duration_spin.setValue(1.0)
            window._capture_background()
            assert wait_for(qapp, lambda: bool(window.session.background), 20.0)

            # 5) two source positions; each capture must yield 3 channels
            for source_position in (1, 2):
                window.src_pos_current.setValue(source_position)
                before = len(window.session.measurements)
                window._capture_measurement()
                assert wait_for(
                    qapp,
                    lambda b=before: len(window.session.measurements) >= b + 3,
                    20.0,
                         ), f"capture failed at source position {source_position}"

            assert window.session.complete, window.session.missing()
            assert window.measure_table.rowCount() == 6
            assert window.session.completed_source_positions() == {1, 2}

            # 12 bands of 1/3 octave, 50-630 Hz, on every channel
            first = window.session.measurements[0]
            assert set(first.levels) == {50, 63, 80, 100, 125, 160,
                                         200, 250, 315, 400, 500, 630}
            assert first.broadband is not None
            assert first.quantity == "Fmax"
            assert first.weighting == "Z", "band levels must be stored unweighted"
            # separate measurements exist per channel
            assert {m.channel for m in window.session.measurements} == {0, 2, 3}

            # The broadband value must be the Z spectrum plus A-weighting values
            from pislm.standards import Spectrum, a_weighted_single_number

            expected = a_weighted_single_number(
                Spectrum.from_mapping(first.levels, fraction=3, weighting="Z")
            )
            assert first.broadband == pytest.approx(expected, abs=1e-9)

            # 6) reverberation
            window.reverb_seconds.setValue(3.0)
            # Watch for a failed job as well as a result. Without this the
            # wait just burns its whole timeout when the worker raises, and
            # the reason never surfaces — an AttributeError in the averaging
            # code hid behind a 60 s stall exactly once.
            failures: list[str] = []
            window.worker.failed_job.connect(
                lambda name, message, detail: failures.append(f"{name}: {message}")
            )
            window._measure_reverberation()
            assert wait_for(
                qapp,
                lambda: bool(window.session.room.reverberation) or failures,
                60.0,
            ), "reverberation calculation timed out"
            assert not failures, failures
            assert window.reverb_table.rowCount() > 0

            # 7) rating
            window._evaluate()
            pump(qapp, 0.2)
            text = window.result_text.toPlainText()
            assert "Post-construction" in text, text
            assert "L'iA,Fmax" in text, text
            assert "KS F 2863" in text, text
            assert window.tabs.currentIndex() == 3

            # 8) session save/load round trip
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "session.json"
                window.session.save(path)
                from pislm.session import Session

                restored = Session.load(path)
                assert restored.progress == window.session.progress
                assert restored.source is ImpactSource.RUBBER_BALL
                assert restored.room.reverberation

            window.scan_btn.click()
            wait_for(qapp, lambda: not window.pi.config.running, 10.0)
        finally:
            window.close()
            pump(qapp, 0.3)

    def test_light_impact_uses_get_metrics_leq(self, qapp, sim):
        """Light impact takes Leq from the Pi get_metrics, not a maximum."""
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        window.show()
        try:
            window.connect_btn.click()
            assert wait_for(qapp, lambda: window.pi is not None)

            window.source_combo.setCurrentIndex(
                list(ImpactSource).index(ImpactSource.TAPPING)
            )
            window.src_pos_spin.setValue(1)
            for channel, box in window.channel_checks.items():
                box.setChecked(channel in (0, 1))
            window._apply_session()
            assert wait_for(qapp, lambda: window.worker.pending == 0, 20.0)
            pump(qapp, 0.3)
            # Light impact is Z weighted; the Pi computes Leq as an RMS
            assert window.pi.config.weighting["frequency"] == "Z"

            window.scan_btn.click()
            assert wait_for(qapp, lambda: window.pi.config.running, 15.0)
            pump(qapp, 0.5)

            window.duration_spin.setValue(1.0)
            window._capture_measurement()
            assert wait_for(
                qapp, lambda: len(window.session.measurements) >= 2, 25.0
            )

            measurement = window.session.measurements[0]
            assert measurement.levels, "no Leq band values"
            assert measurement.quantity == "Leq"
            # 1/3 octave, 100-3150 Hz
            assert min(measurement.levels) == 100
            assert max(measurement.levels) == 3150
            assert {m.channel for m in window.session.measurements} == {0, 1}

            window.scan_btn.click()
            wait_for(qapp, lambda: not window.pi.config.running, 10.0)
        finally:
            window.close()
            pump(qapp, 0.3)


class TestSpectrumDisplay:
    """The live spectrum must keep working across configuration changes."""

    def test_band_table_survives_apply_while_running(self, qapp, sim):
        """Applying a session while scanning must not blank the spectrum.

        `band_table` only appears while band output is active (§3), so the
        `get_config` that follows a configure command legitimately omits it.
        Clearing the index->frequency map there dropped every incoming
        BAND_LEVEL frame and the display stayed empty until the scan was
        toggled by hand. That was a real bug.
        """
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        window.show()
        try:
            window.connect_btn.click()
            assert wait_for(qapp, lambda: window.pi is not None, 20.0)
            for channel, box in window.channel_checks.items():
                box.setChecked(channel in (0, 1))
            window._apply_session()
            assert wait_for(qapp, lambda: window.worker.pending == 0, 20.0)

            window.scan_btn.click()
            assert wait_for(qapp, lambda: window.pi.config.running, 15.0)

            # Wait for the full band set, not just the first band to show up —
            # sampling too early makes the comparison below meaningless.
            expected = len(ImpactSource.RUBBER_BALL.bands)
            assert wait_for(
                qapp,
                lambda: len(window.live.snapshot_spectrum(0)) == expected,
                20.0,
            ), f"only {len(window.live.snapshot_spectrum(0))}/{expected} bands arrived"

            # Re-apply while the scan is running — this used to wipe the table
            window._apply_session()
            assert wait_for(qapp, lambda: window.worker.pending == 0, 20.0)
            pump(qapp, 1.0)

            assert window.live.bands_known, "the band table was cleared"
            assert len(window.live.snapshot_spectrum(0)) == expected
            assert len(window.live.snapshot_spectrum(1)) == expected

            window.scan_btn.click()
            wait_for(qapp, lambda: not window.pi.config.running, 10.0)
        finally:
            window.close()
            pump(qapp, 0.3)

    def test_grid_shows_one_panel_per_selected_channel(self, qapp, sim):
        from PySide6 import QtGui

        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        window.show()
        try:
            for channel, box in window.channel_checks.items():
                box.setChecked(channel in (0, 2, 4))
            pump(qapp, 0.1)
            assert window.spectrum_bars._channels == [0, 2, 4]

            window.spectrum_bars.set_series(
                {ch: {63: 50.0, 125: 55.0, 250: 45.0} for ch in (0, 2, 4)}
            )
            image = QtGui.QImage(window.spectrum_bars.size(), QtGui.QImage.Format_ARGB32)
            window.spectrum_bars.render(image)  # must not raise
            assert image.width() > 0
        finally:
            window.close()
            pump(qapp)

    def test_empty_band_table_does_not_clear_a_good_one(self):
        """A config without band_table must leave the existing map alone."""
        live = LiveState()

        class WithTable:
            raw = {"band_table": [{"bands": [{"index": 0, "center": 63.0957}]}]}
            bands = {"fraction": 3}

        class WithoutTable:
            raw = {}
            bands = {"fraction": 3}

        assert live.configure_bands(WithTable()) is True
        live.feed(BandLevelFrame(0, 0, np.array([44.0])))
        assert live.snapshot_spectrum(0) == {63: 44.0}

        assert live.configure_bands(WithoutTable()) is False
        assert live.bands_known
        assert live.snapshot_spectrum(0) == {63: 44.0}, "readings were discarded"

    def test_unchanged_table_keeps_readings(self):
        """Reconfiguring with the same table must not drop what has arrived."""
        live = LiveState()

        class Config:
            raw = {"band_table": [{"bands": [{"index": 0, "center": 63.0957}]}]}
            bands = {"fraction": 3}

        live.configure_bands(Config())
        live.feed(BandLevelFrame(0, 0, np.array([44.0])))
        live.configure_bands(Config())
        assert live.snapshot_spectrum(0) == {63: 44.0}

    def test_changed_table_drops_stale_readings(self):
        """When indices are reassigned, old readings would be mislabelled."""
        live = LiveState()

        class First:
            raw = {"band_table": [{"bands": [{"index": 0, "center": 63.0957}]}]}
            bands = {"fraction": 3}

        class Second:
            raw = {"band_table": [{"bands": [{"index": 0, "center": 501.1872}]}]}
            bands = {"fraction": 3}

        live.configure_bands(First())
        live.feed(BandLevelFrame(0, 0, np.array([44.0])))
        live.configure_bands(Second())
        assert live.snapshot_spectrum(0) == {}


# ── Instrument options ──────────────────────────────────────────────
class TestOptions:
    def test_dialog_reports_only_what_changed(self, qapp, sim):
        """Each config command stops and restarts the scan; send only changes."""
        from app.options import DeviceOptions, OptionsDialog

        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window.connect_btn.click()
            assert wait_for(qapp, lambda: window.pi is not None, 20.0)

            current = DeviceOptions.from_config(window.pi.config)
            dialog = OptionsDialog(current)
            assert dialog.values().changed == set(), "unchanged dialog reported changes"

            dialog.order.setValue(current.order + 2)
            dialog.frequency.setCurrentText("C")
            changed = dialog.values().changed
            assert changed == {"order", "frequency_weighting"}, changed
        finally:
            window.close()
            pump(qapp)

    def test_apply_options_reaches_the_device(self, qapp, sim):
        from app.options import DeviceOptions, OptionsDialog, apply_options

        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window.connect_btn.click()
            assert wait_for(qapp, lambda: window.pi is not None, 20.0)

            dialog = OptionsDialog(DeviceOptions.from_config(window.pi.config))
            dialog.resample_on.setChecked(True)
            dialog.resample_rate.setValue(44100)
            dialog.order.setValue(8)
            apply_options(window.pi, dialog.values())

            config = window.pi.config
            assert config.resample["output_rate"] == 44100
            assert config.bands["order"] == 8
        finally:
            window.close()
            pump(qapp)

    def test_options_requires_connection(self, qapp, no_modal_dialogs):
        window = MainWindow("127.0.0.1", 1, 2)
        try:
            window._open_options()
            pump(qapp, 0.1)
            warnings = [t for k, t in no_modal_dialogs if k == "warning"]
            assert warnings and "Connect" in warnings[0]
        finally:
            window.close()
            pump(qapp)


# ── WAV recording ───────────────────────────────────────────────────
class TestRecording:
    def test_capture_writes_wav_files(self, qapp, sim, tmp_path):
        """Capture mode — files must actually appear and open."""
        import json
        import wave

        from pislm.recorder import RecordingOptions

        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        window.show()
        try:
            window.connect_btn.click()
            assert wait_for(qapp, lambda: window.pi is not None, 20.0)

            window.source_combo.setCurrentIndex(
                list(ImpactSource).index(ImpactSource.RUBBER_BALL)
            )
            window.src_pos_spin.setValue(1)
            for channel, box in window.channel_checks.items():
                box.setChecked(channel in (0, 1))
            window._apply_session()
            assert wait_for(qapp, lambda: window.worker.pending == 0, 20.0)

            # Enable recording; raw streaming is required too
            window.recording = RecordingOptions(
                enabled=True, directory=str(tmp_path), fmt="int24"
            )
            window.recorder.options = window.recording
            window._update_recording_label()
            assert "int24" in window.record_label.text()

            window._submit(
                "options",
                lambda: (window.pi.set_options(stream_raw=True), window.pi.refresh())[1],
            )
            assert wait_for(qapp, lambda: window.pi.config.stream_raw, 20.0)

            window.scan_btn.click()
            assert wait_for(qapp, lambda: window.pi.config.running, 15.0)

            window.duration_spin.setValue(1.0)
            window._capture_measurement()
            assert wait_for(qapp, lambda: bool(window.session.measurements), 25.0)
            pump(qapp, 0.4)

            files = sorted(tmp_path.glob("*.wav"))
            assert files, "no WAV files were written"
            assert not window.recorder.recording, "recording left open after capture"

            for path in files:
                with wave.open(str(path)) as handle:
                    assert handle.getnframes() > 0
                    assert handle.getsampwidth() == 3
                info = json.loads(
                    path.with_suffix(".json").read_text(encoding="utf-8")
                )
                assert info["column_channels"]
                assert info["units"]
                assert "src1" in info["label"]

            window.scan_btn.click()
            wait_for(qapp, lambda: not window.pi.config.running, 10.0)
        finally:
            # Restore the shared simulator or later tests are affected
            if window.pi is not None:
                try:
                    window.pi.set_options(stream_raw=False)
                    window.pi.refresh()
                except Exception:  # noqa: BLE001
                    pass
            window.close()
            pump(qapp, 0.3)

    def test_closing_mid_capture_finalises_the_wav(self, qapp, sim, tmp_path):
        """Closing must finalise the header, or the file is corrupt.

        The standard `wave` module cannot read float32 (format code 3), so the
        header is checked by hand.
        """
        import struct

        from pislm.recorder import RecordingOptions

        def check_header(path):
            raw = path.read_bytes()
            assert raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"
            riff_size = struct.unpack("<I", raw[4:8])[0]
            data_size = struct.unpack("<I", raw[40:44])[0]
            assert riff_size == len(raw) - 8, "RIFF size was never patched"
            assert data_size > 0, "data size is zero — header incomplete"
            return data_size

        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        window.show()
        window.connect_btn.click()
        assert wait_for(qapp, lambda: window.pi is not None, 20.0)

        window.recording = RecordingOptions(
            enabled=True, directory=str(tmp_path), fmt="float32"
        )
        window.recorder.options = window.recording
        assert ensure_scanning(qapp, window)

        # Start a long capture and pull the window out from under it.
        window.duration_spin.setValue(3.0)
        window.capture_btn.click()
        assert wait_for(qapp, lambda: window.recorder.recording, 15.0)
        pump(qapp, 0.6)

        window.close()          # close while still recording
        pump(qapp, 0.5)

        files = sorted(tmp_path.glob("*.wav"))
        assert files, "no recording file"
        for path in files:
            check_header(path)

    def test_options_dialog_round_trips_recording(self, qapp, tmp_path):
        """Supply the initial values explicitly, independent of device state.

        If an earlier test leaves stream_raw on, this reads as "no change" and
        fails for the wrong reason — which is exactly what happened once.
        """
        from app.options import DeviceOptions, OptionsDialog, recording_options

        initial = DeviceOptions(stream_raw=False, record_enabled=False)
        dialog = OptionsDialog(initial)
        assert dialog.values().changed == set()

        dialog.record_on.setChecked(True)
        dialog.record_dir.setText(str(tmp_path))
        values = dialog.values()

        assert {"record_enabled", "record_directory"} <= values.changed
        options = recording_options(values)
        assert options.enabled and options.directory == str(tmp_path)

    def test_recording_does_not_force_permanent_raw_streaming(self, qapp, tmp_path):
        """Raw is only needed during a capture, and it is the heaviest thing
        on the link — turning it on for the whole session is not the same
        thing and must not happen behind the operator's back."""
        from app.options import DeviceOptions, OptionsDialog

        dialog = OptionsDialog(DeviceOptions(stream_raw=False))
        dialog.record_on.setChecked(True)
        dialog.record_dir.setText(str(tmp_path))
        assert dialog.values().stream_raw is False

    def test_estimate_updates_with_format(self, qapp):
        """Show the size before committing — 6-channel raw grows fast."""
        from app.options import DeviceOptions, OptionsDialog

        dialog = OptionsDialog(DeviceOptions())
        dialog.record_format.setCurrentIndex(0)  # float32
        float_text = dialog.record_estimate.text()
        dialog.record_format.setCurrentIndex(2)  # int16
        int_text = dialog.record_estimate.text()
        assert "MB" in float_text or "GB" in float_text
        assert float_text != int_text


# ── Error handling ──────────────────────────────────────────────────
class TestErrorHandling:
    def test_failed_connection_reports_instead_of_crashing(self, qapp, no_modal_dialogs):
        window = MainWindow("127.0.0.1", 1, 2)  # closed ports
        try:
            window.show()
            window.connect_btn.click()
            assert wait_for(
                qapp, lambda: any(k == "exec" for k, _ in no_modal_dialogs), 20.0
                     ), "the error was never reported"
            assert window.pi is None
            assert window.isVisible(), "the window must survive an error"
        finally:
            window.close()
            pump(qapp)

    def test_capture_without_scan_warns(self, qapp, sim, no_modal_dialogs):
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window.show()
            window._capture_measurement()
            pump(qapp, 0.1)
            warnings = [text for kind, text in no_modal_dialogs if kind == "warning"]
            assert warnings and "scan" in warnings[0]
        finally:
            window.close()
            pump(qapp)

    def test_closing_mid_operation_does_not_error(self, qapp, sim, no_modal_dialogs):
        """Closing with jobs in flight must not produce "connection lost".

        Closing the socket before draining the worker breaks the pending command
        and pops an error over a closing window. This was a real bug.
        """
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        failures: list[str] = []
        window.worker.failed_job.connect(lambda n, m, d: failures.append(f"{n}: {m}"))
        window.show()
        window.connect_btn.click()
        assert wait_for(qapp, lambda: window.pi is not None, 20.0)

        window.scan_btn.click()
        assert wait_for(qapp, lambda: window.pi.config.running, 15.0)
        window.scan_btn.click()          # queue the stop command
        window.close()                   # and close immediately
        pump(qapp, 0.5)

        assert not any("connection lost" in f for f in failures), failures


# ── Recording, reset, and the reverberation sources ─────────────────
class TestRecordingIntegration:
    def test_capture_actually_writes_a_wav(self, qapp, sim, tmp_path):
        """The recorder consumes DATA frames, which only flow when raw is on.

        Nothing used to switch `stream_raw` on, so recording produced no files
        at all while still reporting itself as running — a silent no-op.
        """
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window.connect_btn.click()
            assert wait_for(qapp, lambda: window.pi is not None, 20.0)
            window.recording.enabled = True
            window.recording.directory = str(tmp_path)
            window.recorder.options = window.recording

            window._apply_session()
            assert wait_for(qapp, lambda: window.worker.pending == 0, 20.0)
            assert ensure_scanning(qapp, window)

            window.duration_spin.setValue(2.0)
            window.capture_btn.click()
            assert wait_for(qapp, lambda: window.worker.pending == 0, 40.0)
            assert wait_for(qapp, lambda: not window.recorder.recording, 15.0)

            wavs = sorted(tmp_path.glob("*.wav"))
            assert wavs, "recording was enabled but no WAV was written"
            assert all(w.stat().st_size > 1000 for w in wavs), \
                [(w.name, w.stat().st_size) for w in wavs]
            # A sidecar per file — a WAV header cannot carry channel order,
            # units or the gap list.
            assert len(sorted(tmp_path.glob("*.json"))) == len(wavs)

            ensure_scanning(qapp, window, running=False)
        finally:
            window.close()
            pump(qapp, 0.3)

    def test_the_raw_stream_is_put_back_afterwards(self, qapp, sim, tmp_path):
        """Raw is the heaviest thing on the link; leaving it on costs the
        rest of the session bandwidth (§6 — DATA crowds everything else)."""
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window.connect_btn.click()
            assert wait_for(qapp, lambda: window.pi is not None, 20.0)
            window.recording.enabled = True
            window.recording.directory = str(tmp_path)
            window.recorder.options = window.recording
            assert ensure_scanning(qapp, window)
            was_on = window.pi.config.stream_raw

            window._start_recording("test")
            assert window.pi.config.stream_raw, "recording must turn raw on"
            window._stop_recording()
            assert window.pi.config.stream_raw is was_on, "and put it back as it was"

            window.scan_btn.click()
            wait_for(qapp, lambda: not window.pi.config.running, 10.0)
        finally:
            window.close()
            pump(qapp, 0.3)


class TestReset:
    @staticmethod
    def _populate(window):
        from pislm.session import Measurement

        window.session.measurements = [
            Measurement(source_position=1, channel=0, levels={125: 50.0}, broadband=50.0),
            Measurement(source_position=1, channel=1, levels={125: 51.0}, broadband=51.0),
            Measurement(source_position=2, channel=0, levels={125: 52.0}, broadband=52.0),
        ]
        window.session.background = {125: 20.0}
        window.session.room.reverberation = {125: 0.5}
        window.session.room.volume = 40.0

    @staticmethod
    def _choose(monkeypatch, index):
        from PySide6 import QtWidgets

        monkeypatch.setattr(
            QtWidgets.QInputDialog, "getItem",
            classmethod(lambda cls, *a, **k: (a[3][index], True)),
        )

    def test_one_source_position_only(self, qapp, sim, monkeypatch):
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            self._populate(window)
            window.src_pos_current.setValue(1)
            self._choose(monkeypatch, 0)
            window._reset_measurements()
            left = {m.source_position for m in window.session.measurements}
            assert left == {2}
            # The expensive room data survives — that is the entire point.
            assert window.session.background == {125: 20.0}
            assert window.session.room.reverberation == {125: 0.5}
        finally:
            window.close()
            pump(qapp)

    def test_all_measurements_keeps_room_data(self, qapp, sim, monkeypatch):
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            self._populate(window)
            self._choose(monkeypatch, 1)
            window._reset_measurements()
            assert window.session.measurements == []
            assert window.session.background == {125: 20.0}
            assert window.session.room.reverberation == {125: 0.5}
        finally:
            window.close()
            pump(qapp)

    def test_everything(self, qapp, sim, monkeypatch):
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            self._populate(window)
            self._choose(monkeypatch, 4)
            window._reset_measurements()
            assert window.session.measurements == []
            assert window.session.background == {}
            assert window.session.room.reverberation == {}
            assert window.session.room.volume == 0.0
        finally:
            window.close()
            pump(qapp)

    def test_cancelling_changes_nothing(self, qapp, sim, monkeypatch):
        from PySide6 import QtWidgets

        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            self._populate(window)
            monkeypatch.setattr(
                QtWidgets.QInputDialog, "getItem",
                classmethod(lambda cls, *a, **k: ("", False)),
            )
            window._reset_measurements()
            assert len(window.session.measurements) == 3
        finally:
            window.close()
            pump(qapp)


class TestReverberationSources:
    def test_panels_follow_the_source(self, qapp, sim):
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        window.show()
        window.tabs.setCurrentIndex(2)   # the panels live on the reverb tab
        try:
            # The signal picker serves the Pi's DAC, the sound card and the
            # exported sweep, so it stays up for everything except manual
            # entry — only the Output button is specific to the Pi.
            for index, (visible, hidden) in enumerate([
                (window.excite_box, window.manual_box),
                (window.external_box, window.manual_box),
                (window.manual_box, window.external_box),
            ]):
                window.reverb_source.setCurrentIndex(index)
                pump(qapp, 0.05)
                assert visible.isVisible()
                assert not hidden.isVisible()
            window.reverb_source.setCurrentIndex(0)
            pump(qapp, 0.05)
            assert window.output_btn.isVisible()
            window.reverb_source.setCurrentIndex(1)
            pump(qapp, 0.05)
            assert not window.output_btn.isVisible()
        finally:
            window.close()
            pump(qapp)

    def test_manual_entry_reaches_the_session(self, qapp, sim):
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window.reverb_source.setCurrentIndex(2)
            window.manual_fill.setValue(0.62)
            window._fill_manual_reverberation()
            window._apply_manual_reverberation()

            bands = window.session.bands
            assert set(window.session.room.reverberation) == set(bands)
            assert all(v == 0.62 for v in window.session.room.reverberation.values())
        finally:
            window.close()
            pump(qapp)

    def test_manual_entry_rejects_nonsense(self, qapp, sim, no_modal_dialogs):
        from PySide6 import QtWidgets

        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window.reverb_source.setCurrentIndex(2)
            window._rebuild_manual_table()
            window.manual_table.setItem(0, 1, QtWidgets.QTableWidgetItem("soon"))
            window._apply_manual_reverberation()
            assert window.session.room.reverberation == {}
            assert no_modal_dialogs, "a bad value must be reported, not ignored"
        finally:
            window.close()
            pump(qapp)

    def test_manual_entry_replaces_measured_values(self, qapp, sim):
        """Mixing typed-in numbers into a measured average would make a
        reported T impossible to trace back to either."""
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window._reverb_runs.append({125: object()})
            window.reverb_source.setCurrentIndex(2)
            window.manual_fill.setValue(0.4)
            window._fill_manual_reverberation()
            window._apply_manual_reverberation()
            assert window._reverb_runs == []
        finally:
            window.close()
            pump(qapp)

    def test_sweep_file_is_written_and_remembered(self, qapp, sim, tmp_path, monkeypatch):
        """The deconvolution reference has to be the file that was played."""
        from PySide6 import QtWidgets

        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            target = tmp_path / "sweep.wav"
            monkeypatch.setattr(
                QtWidgets.QFileDialog, "getSaveFileName",
                classmethod(lambda cls, *a, **k: (str(target), "")),
            )
            window.reverb_source.setCurrentIndex(1)
            window.external_method.setCurrentIndex(
                window.external_method.findData("sweep")
            )
            window._save_sweep_file()

            assert target.exists() and target.stat().st_size > 1000
            assert window._external_settings is not None
            assert window._external_settings.signal is Signal.SWEEP
        finally:
            window.close()
            pump(qapp)

    def test_sweep_analysis_refuses_without_the_file(self, qapp, sim, no_modal_dialogs):
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window.connect_btn.click()
            assert wait_for(qapp, lambda: window.pi is not None, 20.0)
            assert ensure_scanning(qapp, window)

            window.reverb_source.setCurrentIndex(1)
            window.external_method.setCurrentIndex(
                window.external_method.findData("sweep")
            )
            window._external_settings = None
            window._toggle_external_recording()   # start
            assert window._external_recording
            window._toggle_external_recording()   # stop -> should refuse
            assert any("sweep file" in text for _, text in no_modal_dialogs), \
                no_modal_dialogs

            window.scan_btn.click()
            wait_for(qapp, lambda: not window.pi.config.running, 10.0)
        finally:
            window.close()
            pump(qapp, 0.3)


class TestReverberationAveraging:
    """[SPEC] ISO 3382-2 — the mean over microphone positions is arithmetic."""

    @staticmethod
    def _result(t60, correlation=0.99, curvature=None):
        from pislm.standards.reverberation import DecayResult

        return DecayResult(t60=t60, method="T20", correlation=correlation,
                           decay_range_db=30.0, curvature_percent=curvature)

    def test_one_channel_passes_straight_through(self):
        single = {125: self._result(0.5)}
        assert MainWindow._merge_channels([single]) is single

    def test_times_are_averaged_arithmetically(self):
        merged = MainWindow._merge_channels([
            {125: self._result(0.40)},
            {125: self._result(0.60)},
            {125: self._result(0.80)},
        ])
        assert merged[125].t60 == pytest.approx(0.60)

    def test_it_is_not_a_geometric_mean(self):
        """The plausible wrong choice, and it gives a visibly different answer.

        Levels elsewhere in this program are energy-averaged, so averaging T
        the same way is an easy mistake to make. T is a time, not a level.
        """
        import numpy as np

        times = [0.25, 1.0]
        merged = MainWindow._merge_channels([{125: self._result(t)} for t in times])
        assert merged[125].t60 == pytest.approx(0.625)
        assert merged[125].t60 != pytest.approx(float(np.exp(np.mean(np.log(times)))))

    def test_the_worst_correlation_survives(self):
        """One bad microphone should stay visible, not be smoothed away."""
        merged = MainWindow._merge_channels([
            {125: self._result(0.5, correlation=0.999)},
            {125: self._result(0.5, correlation=0.910)},
        ])
        assert merged[125].correlation == pytest.approx(0.910)

    def test_a_band_missing_from_one_channel_uses_the_rest(self):
        merged = MainWindow._merge_channels([
            {125: self._result(0.4), 250: self._result(0.6)},
            {125: self._result(0.6)},
        ])
        assert merged[125].t60 == pytest.approx(0.5)
        assert merged[250].t60 == pytest.approx(0.6)

    def test_unknown_curvature_stays_unknown(self):
        """None means 'could not be computed', not 'straight'."""
        merged = MainWindow._merge_channels([
            {125: self._result(0.5, curvature=None)},
            {125: self._result(0.5, curvature=None)},
        ])
        assert merged[125].curvature_percent is None

        mixed = MainWindow._merge_channels([
            {125: self._result(0.5, curvature=4.0)},
            {125: self._result(0.5, curvature=None)},
            {125: self._result(0.5, curvature=6.0)},
        ])
        assert mixed[125].curvature_percent == pytest.approx(5.0)


# ── Heavy and light measured as one set ─────────────────────────────
class TestImpactSourceSet:
    """Both halves are held open, because operators often walk position by
    position doing heavy then light rather than finishing one source first."""

    def test_both_halves_exist_and_keep_their_own_source(self, qapp, sim):
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            assert set(window._sessions) == {"heavy", "light"}
            assert window._sessions["heavy"].source.is_heavy
            assert not window._sessions["light"].source.is_heavy
            assert window.session is window._sessions["heavy"]
        finally:
            window.close()
            pump(qapp)

    def test_switching_keeps_each_half_separate(self, qapp, sim):
        from pislm.session import Measurement

        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window.session.measurements = [
                Measurement(source_position=1, channel=0, levels={125: 60.0})
            ]
            heavy_bands = window.session.bands
            window._switch_impact_source()

            assert window._active == "light"
            assert window.session.measurements == [], "light must start empty"
            assert window.session.bands != heavy_bands
            assert min(window.session.bands) == 100
            assert max(window.session.bands) == 3150

            window._switch_impact_source()
            assert window._active == "heavy"
            assert len(window.session.measurements) == 1, "heavy data must survive"
            assert window.session.bands == heavy_bands
        finally:
            window.close()
            pump(qapp)

    def test_switching_leaves_the_scan_running(self, qapp, sim):
        """Switch source reconfigures the Pi, so the scan stops and restarts.

        It used to stop and stay stopped: `configure()` calls `pi.stop()`
        first, which means the config commands no longer need stopping, so
        `send_stopped()` never restarts anything. The operator had to press
        Start scan again — for a scan the window claimed was already running.
        """
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window._toggle_connection()
            pump(qapp, 2.0)
            ensure_scanning(qapp, window, True)
            assert window.pi.config.raw.get("running")

            window._switch_impact_source()
            pump(qapp, 4.0)

            assert window.pi.refresh().raw.get("running"), \
                "Switch source must leave the scan running"
            assert window.scan_btn.text() == "Stop scan"
        finally:
            window.close()
            pump(qapp)

    def test_configuring_while_stopped_does_not_claim_to_be_scanning(self, qapp, sim):
        """The mirror of the above: applying a session with the scan stopped
        legitimately leaves it stopped, and the button must say so.

        A button reading "Stop scan" over a stopped scan sends the next click
        to `stop`, so the operator presses it twice to get any data.
        """
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window._toggle_connection()
            pump(qapp, 2.0)
            ensure_scanning(qapp, window, False)
            assert not window.pi.config.raw.get("running")

            window._apply_session()
            pump(qapp, 4.0)

            assert not window.pi.refresh().raw.get("running")
            assert window.scan_btn.text() == "Start scan"
        finally:
            window.close()
            pump(qapp)

    def test_room_data_is_shared_not_measured_twice(self, qapp, sim):
        """Same room either way — two answers for one room would be worse
        than useless."""
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window.session.room.volume = 42.0
            window.session.room.reverberation = {125: 0.55}
            window.session.calibration = {"0": 48.1}
            window.session.site = "Block 101"
            window._switch_impact_source()

            assert window.session.room.volume == 42.0
            assert window.session.room.reverberation == {125: 0.55}
            assert window.session.calibration == {"0": 48.1}
            assert window.session.site == "Block 101"
        finally:
            window.close()
            pump(qapp)

    def test_the_source_selector_switches_halves(self, qapp, sim):
        """Choosing a light source while the heavy half is active must move
        to the light half, not rewrite the heavy session's source."""
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            assert window._active == "heavy"
            window.source_combo.setCurrentIndex(
                list(ImpactSource).index(ImpactSource.TAPPING)
            )
            window._apply_session()
            pump(qapp, 0.1)
            assert window._active == "light"
            assert window._sessions["heavy"].source.is_heavy, \
                "the heavy half must keep a heavy source"
        finally:
            window.close()
            pump(qapp)

    def test_the_label_shows_both_halves(self, qapp, sim):
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window._update_set_label()
            text = window.set_label.text()
            assert "heavy" in text and "light" in text
            assert "\u25b6" in text, "the active half must be marked"
        finally:
            window.close()
            pump(qapp)


class TestSharedBackground:
    def test_one_background_serves_both_sources(self, qapp, sim):
        """50-3150 Hz once, trimmed per source at analysis time."""
        from app.main import COMBINED_BANDS

        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            wide = {float(b): 22.0 for b in COMBINED_BANDS}
            window.session.background = dict(wide)
            window.other_session.background = dict(wide)

            heavy = window._sessions["heavy"].background_spectrum()
            light = window._sessions["light"].background_spectrum()
            assert len(heavy.centers) == 12 and min(heavy.centers) == 50
            assert len(light.centers) == 16 and max(light.centers) == 3150
        finally:
            window.close()
            pump(qapp)

    def test_combined_range_covers_both(self):
        from app.main import COMBINED_BANDS

        assert min(COMBINED_BANDS) == 50 and max(COMBINED_BANDS) == 3150
        assert len(COMBINED_BANDS) == 19

    def test_a_background_missing_bands_counts_as_unmeasured(self, qapp, sim):
        """Half a background is more dangerous than none — a partial
        correction would be applied silently to the bands it happens to
        cover."""
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window.session.background = {50.0: 20.0, 63.0: 20.0}
            assert window.session.background_spectrum() is None
        finally:
            window.close()
            pump(qapp)

    def test_the_two_halves_rate_independently(self, qapp, sim):
        from app.main import COMBINED_BANDS
        from pislm.session import Measurement

        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            for key in ("heavy", "light"):
                session = window._sessions[key]
                session.source_positions = 1
                session.channels = [0]
                session.room.volume = 40.0
                session.room.reverberation = {float(b): 0.5 for b in COMBINED_BANDS}
                session.background = {float(b): 20.0 for b in COMBINED_BANDS}
                session.measurements = [
                    Measurement(source_position=1, channel=0,
                                levels={b: 55.0 for b in session.bands},
                                broadband=55.0)
                ]
            heavy = window._sessions["heavy"].evaluate()
            light = window._sessions["light"].evaluate()
            assert heavy.post_verification_symbol == "L'iA,Fmax"
            assert light.post_verification_symbol == "L'nT,w"
            assert heavy.post_verification != light.post_verification
        finally:
            window.close()
            pump(qapp)


# ── Measurement table and the running estimate ──────────────────────
class TestMeasurementTable:
    @staticmethod
    def _fill(window, level=58.0):
        from pislm.session import Measurement

        session = window.session
        session.source_positions = 2
        session.channels = [0, 1]
        for pos in (1, 2):
            for channel in (0, 1):
                session.add(Measurement(
                    source_position=pos, channel=channel,
                    levels={b: level for b in session.bands}, broadband=level,
                ))
        window._refresh_measure_table()

    def test_five_source_positions_by_default(self, qapp, sim):
        from pislm.session import Session

        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            assert Session().source_positions == 5
            assert window.src_pos_spin.value() == 5
        finally:
            window.close()
            pump(qapp)

    def test_every_band_gets_a_column(self, qapp, sim):
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            self._fill(window)
            table = window.measure_table
            headers = [table.horizontalHeaderItem(i).text()
                       for i in range(table.columnCount())]
            bands = [f"{b:g}" for b in window.session.bands]
            assert headers == ["Source", "Channel", "Time"] + bands + ["Note"]
            row = [table.item(0, i).text() for i in range(table.columnCount())]
            assert row[3:-1] == ["58.0"] * len(bands)
        finally:
            window.close()
            pump(qapp)

    def test_broadband_column_is_gone(self, qapp, sim):
        """It was derivable from the bands and hid a bad single band."""
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            self._fill(window)
            table = window.measure_table
            headers = [table.horizontalHeaderItem(i).text()
                       for i in range(table.columnCount())]
            assert not any("road" in h for h in headers), headers
        finally:
            window.close()
            pump(qapp)

    def test_columns_are_rebuilt_when_the_source_changes(self, qapp, sim):
        """Heavy readings under light headings would be quietly wrong."""
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            self._fill(window)
            heavy = window.measure_table.columnCount()
            window._switch_impact_source()
            light = window.measure_table.columnCount()
            assert heavy == 3 + 12 + 1
            assert light == 3 + 16 + 1
            headers = [window.measure_table.horizontalHeaderItem(i).text()
                       for i in range(window.measure_table.columnCount())]
            assert "3150" in headers and "50" not in headers
        finally:
            window.close()
            pump(qapp)

    def test_a_missing_band_shows_as_a_gap(self, qapp, sim):
        from pislm.session import Measurement

        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            bands = window.session.bands
            window.session.add(Measurement(
                source_position=1, channel=0,
                levels={b: 55.0 for b in bands[1:]},
            ))
            window._refresh_measure_table()
            assert window.measure_table.item(0, 3).text() == "-"
        finally:
            window.close()
            pump(qapp)


class TestSingleNumberEstimate:
    def test_nothing_shown_before_any_measurement(self, qapp, sim):
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window._refresh_measure_table()
            assert window.snq_label.text() == ""
        finally:
            window.close()
            pump(qapp)

    def test_it_names_the_quantity_and_says_it_is_provisional(self, qapp, sim):
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            TestMeasurementTable._fill(window)
            text = window.snq_label.text()
            assert window.session.source.single_number_symbol in text
            assert "provisional" in text
            # Say *what* is missing, so the number is not mistaken for final.
            assert "no background" in text
            # ...but only what actually is missing. This session is heavy, and
            # L'iA,Fmax is not standardised, so volume and T are not inputs at
            # all. Listing them would leave a finished heavy set looking
            # provisional for ever, and send the operator off to measure a
            # reverberation time that cannot change the answer.
            assert "not standardised" not in text
        finally:
            window.close()
            pump(qapp)

    def test_light_impact_does_say_it_is_not_standardised(self, qapp, sim):
        """The mirror image: L'nT,w is a standardised level, so T is required."""
        from pislm.session import ImpactSource

        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window.session.source = ImpactSource.TAPPING
            window.session.measurements.clear()
            TestMeasurementTable._fill(window)
            assert "not standardised" in window.snq_label.text()
        finally:
            window.close()
            pump(qapp)

    def test_it_drops_the_caveats_once_everything_is_there(self, qapp, sim):
        from app.main import COMBINED_BANDS

        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            TestMeasurementTable._fill(window)
            window.session.room.volume = 40.0
            window.session.room.reverberation = {float(b): 0.5 for b in COMBINED_BANDS}
            window.session.background = {float(b): 25.0 for b in COMBINED_BANDS}
            window._refresh_measure_table()
            text = window.snq_label.text()
            assert "final" in text and "provisional" not in text
        finally:
            window.close()
            pump(qapp)

    def test_it_reports_the_verdict_against_the_limit(self, qapp, sim):
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            TestMeasurementTable._fill(window, level=20.0)
            assert "within" in window.snq_label.text()
            window.session.measurements = []
            TestMeasurementTable._fill(window, level=85.0)
            assert "over" in window.snq_label.text()
        finally:
            window.close()
            pump(qapp)

    def test_each_half_of_the_set_estimates_its_own_quantity(self, qapp, sim):
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            TestMeasurementTable._fill(window)
            assert "L'iA,Fmax" in window.snq_label.text()
            window._switch_impact_source()
            TestMeasurementTable._fill(window)
            assert "L'nT,w" in window.snq_label.text()
        finally:
            window.close()
            pump(qapp)

    def test_a_reset_clears_the_estimate_and_returns_to_position_1(
        self, qapp, sim, monkeypatch
    ):
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            TestMeasurementTable._fill(window)
            window.src_pos_current.setValue(2)
            monkeypatch.setattr(
                QtWidgets.QInputDialog, "getItem",
                classmethod(lambda cls, *a, **k: (a[3][1], True)),  # all measurements
            )
            window._reset_measurements()
            assert window.src_pos_current.value() == 1
            assert window.snq_label.text() == ""
        finally:
            window.close()
            pump(qapp)


# ── Only the channels in use are drawn ──────────────────────────────
class TestVisibleChannels:
    def test_both_meters_follow_the_ticked_channels(self, qapp, sim):
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            for channel, box in window.channel_checks.items():
                box.setChecked(channel in (0, 3))
            pump(qapp, 0.05)
            assert window.level_bars._channels == (0, 3)
            assert tuple(window.spectrum_bars._channels) == (0, 3)
        finally:
            window.close()
            pump(qapp)

    def test_an_unticked_channel_is_not_drawn(self, qapp, sim):
        """It still streams — drawing it squeezes the ones that matter."""
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window.level_bars.set_channels([0, 1])
            window.level_bars.set_levels({c: 60.0 for c in range(6)})
            drawn = [c for c in sorted(window.level_bars._levels)
                     if c in window.level_bars._channels]
            assert drawn == [0, 1]
        finally:
            window.close()
            pump(qapp)

    def test_it_still_paints_with_nothing_selected(self, qapp, sim):
        """An empty selection must not raise mid-paint."""
        from PySide6 import QtGui

        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window.show()
            pump(qapp, 0.05)
            window.level_bars.set_channels([])
            window.level_bars.set_levels({0: 55.0})
            image = QtGui.QImage(window.level_bars.size(), QtGui.QImage.Format_ARGB32)
            window.level_bars.render(image)

            window.level_bars.set_channels([4])
            window.level_bars.set_levels({0: 55.0})   # nothing to draw
            window.level_bars.render(image)
        finally:
            window.close()
            pump(qapp)

    def test_the_recording_holds_exactly_the_ticked_receivers(self, qapp, sim, tmp_path):
        """One column per receiver position, and nothing else.

        An unticked input is not a receiver position, so recording it would
        only pad the file and blur what each column means.
        """
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window.connect_btn.click()
            assert wait_for(qapp, lambda: window.pi is not None, 20.0)
            for channel, box in window.channel_checks.items():
                box.setChecked(channel in (0, 2, 3))
            window.recording.enabled = True
            window.recording.directory = str(tmp_path)
            window.recorder.options = window.recording
            window._apply_session()
            assert wait_for(qapp, lambda: window.worker.pending == 0, 20.0)
            assert ensure_scanning(qapp, window)

            window.duration_spin.setValue(2.0)
            # Read it before the click: the program moves on to the next
            # source position once a capture lands.
            position = window.src_pos_current.value()
            window.capture_btn.click()
            assert wait_for(qapp, lambda: window.worker.pending == 0, 40.0)
            assert wait_for(qapp, lambda: not window.recorder.recording, 15.0)

            wavs = sorted(tmp_path.glob("*.wav"))
            assert len(wavs) == 1, [w.name for w in wavs]
            import json
            info = json.loads(
                wavs[0].with_suffix(".json").read_text(encoding="utf-8")
            )
            assert info["column_channels"] == [0, 2, 3]
            # Operators count receiver positions from 1; the wire counts from 0.
            assert info["receiver_positions"] == {"0": 1, "1": 3, "2": 4}
            assert info["sample_locked"] is True
            assert info["source_position"] == position
            assert f"src{position}" in wavs[0].name, wavs[0].name
            assert str(window._active) in info["label"]

            ensure_scanning(qapp, window, running=False)
        finally:
            window.close()
            pump(qapp, 0.3)


# ── Ring buffer limits and sound-card playback ──────────────────────
class TestRawWindow:
    """`get_raw` refuses a window longer than the Pi's ring buffer."""

    def _window(self, qapp, sim, buffer_seconds, ask):
        pislm_sim.STATE.storage["buffer_seconds"] = buffer_seconds
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window.connect_btn.click()
            assert wait_for(qapp, lambda: window.pi is not None, 20.0)
            assert ensure_scanning(qapp, window)
            pump(qapp, 2.5)          # let the ring buffer actually fill
            dumps = window._fetch_window(ask, timeout=30.0)
            device = sorted(dumps)[0]
            got = dumps[device].channel(
                window.pi.config.device_channels(device)[0]
            ).size / dumps[device].sample_rate
            return got
        finally:
            window.close()
            pump(qapp, 0.2)
            pislm_sim.STATE.storage["buffer_seconds"] = 60.0

    def test_a_window_longer_than_the_buffer_is_trimmed(self, qapp, sim):
        """It must not raise. The decay sits at the end of the recording and
        `get_raw` returns the most recent samples, so the trimmed window keeps
        the part that matters."""
        got = self._window(qapp, sim, buffer_seconds=4.0, ask=20.0)
        assert 0 < got <= 4.0, got      # trimmed, and never more than the buffer

    def test_a_window_inside_the_buffer_is_untouched(self, qapp, sim):
        got = self._window(qapp, sim, buffer_seconds=30.0, ask=2.0)
        assert got == pytest.approx(2.0, abs=0.2), got

    def test_it_gives_up_inside_its_budget_instead_of_hanging(self, qapp, sim):
        """Handing the full timeout to every retry made this look like a hang.

        Three retries of a 90 s fetch is four and a half minutes of a frozen
        window, and the operator has no way to tell that from a crash. The
        budget now covers the retries as well as each attempt.
        """
        pislm_sim.STATE.storage["buffer_seconds"] = 0.5   # nothing can satisfy it
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window.connect_btn.click()
            assert wait_for(qapp, lambda: window.pi is not None, 20.0)
            assert ensure_scanning(qapp, window)

            started = time.monotonic()
            with pytest.raises(RuntimeError, match="buffered|did not return"):
                window._fetch_window(30.0, timeout=12.0)
            elapsed = time.monotonic() - started
            assert elapsed < 25.0, f"took {elapsed:.1f} s — the budget is not held"
        finally:
            window.close()
            pump(qapp, 0.2)
            pislm_sim.STATE.storage["buffer_seconds"] = 60.0

    def test_the_dump_does_not_switch_on_raw_streaming(self, qapp, sim):
        """RAW_DUMP is an on-demand pull from the ring buffer (§4). Turning
        DATA on for it floods the link with waveform nobody reads, and DATA
        crowds out everything else (§6)."""
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window.connect_btn.click()
            assert wait_for(qapp, lambda: window.pi is not None, 20.0)
            assert ensure_scanning(qapp, window)
            window.pi.set_options(stream_raw=False)
            window._fetch_window(1.0, timeout=30.0)
            window.pi.refresh()
            assert not window.pi.config.stream_raw
        finally:
            window.close()
            pump(qapp, 0.2)


class TestSoundCardOutput:
    def test_the_device_list_survives_a_machine_without_sounddevice(self, qapp, sim):
        """The Pi output, sweep-file and manual routes must keep working on a
        machine that has no PortAudio."""
        from app import audio

        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            assert window.audio_device.count() >= 1
            if not audio.available():
                assert not window.audio_device.isEnabled()
                assert window.audio_device.currentData() is None
        finally:
            window.close()
            pump(qapp)

    def test_playing_without_a_device_reports_instead_of_measuring(
        self, qapp, sim, no_modal_dialogs
    ):
        """A measurement taken against a signal that never sounded is worse
        than an error."""
        from app import audio

        if audio.available() and audio.output_devices():
            pytest.skip("this machine has a real output device")

        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window.connect_btn.click()
            assert wait_for(qapp, lambda: window.pi is not None, 20.0)
            assert ensure_scanning(qapp, window)
            window.reverb_source.setCurrentIndex(1)      # external
            window.external_method.setCurrentIndex(
                window.external_method.findData("soundcard")
            )
            window._play_and_measure()
            pump(qapp, 0.1)
            assert no_modal_dialogs, "it must say why nothing happened"
        finally:
            window.close()
            pump(qapp, 0.2)

    def test_a_pass_longer_than_the_buffer_is_refused_before_playing(
        self, qapp, sim, no_modal_dialogs
    ):
        """Checked up front: once the sound has been made the buffer cannot be
        grown, because `set_storage` needs a stop and that empties it."""
        pislm_sim.STATE.storage["buffer_seconds"] = 2.0
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window.connect_btn.click()
            assert wait_for(qapp, lambda: window.pi is not None, 20.0)
            assert ensure_scanning(qapp, window)
            window.reverb_source.setCurrentIndex(1)
            window.external_method.setCurrentIndex(0)
            window.audio_device.addItem("fake", 0)
            window.audio_device.setCurrentIndex(window.audio_device.count() - 1)
            window.signal_seconds.setValue(10.0)
            window.reverb_seconds.setValue(5.0)

            window._play_and_measure()
            pump(qapp, 0.1)
            assert any("buffer" in text.lower() for _, text in no_modal_dialogs), \
                no_modal_dialogs
            assert not window._player.playing, "nothing should have been played"
        finally:
            window.close()
            pump(qapp, 0.2)
            pislm_sim.STATE.storage["buffer_seconds"] = 60.0

    def test_the_signal_picker_stays_available_for_external_output(self, qapp, sim):
        """It drives the sound card and the exported sweep too, so hiding it
        with the Pi's DAC controls would leave no way to choose."""
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        window.show()
        window.tabs.setCurrentIndex(2)
        try:
            window.reverb_source.setCurrentIndex(1)      # external
            pump(qapp, 0.05)
            assert window.excite_box.isVisible()
            assert window.signal_combo.isVisible()
            assert not window.output_btn.isVisible(), "that button is the Pi's DAC"

            window.reverb_source.setCurrentIndex(2)      # manual
            pump(qapp, 0.05)
            assert not window.excite_box.isVisible()
        finally:
            window.close()
            pump(qapp, 0.2)


# ── The reverberation waveform is kept too ──────────────────────────
class TestReverberationRecording:
    """A T20 is a fitted number; without the decay there is no checking it."""

    def _run(self, qapp, sim, tmp_path, enabled=True):
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        failures: list[str] = []
        window.worker.failed_job.connect(lambda n, m, d: failures.append(m))
        stored: dict = {}
        original = window._store_reverberation
        window._store_reverberation = lambda r: (stored.update(r=r), original(r))[1]
        try:
            window.connect_btn.click()
            assert wait_for(qapp, lambda: window.pi is not None, 20.0)
            window.recording.enabled = enabled
            window.recording.directory = str(tmp_path)
            window.recorder.options = window.recording
            window._apply_session()
            assert wait_for(qapp, lambda: window.worker.pending == 0, 20.0)
            assert ensure_scanning(qapp, window)
            for channel, box in window.reverb_channels.items():
                box.setChecked(channel in (0, 2))
            window.reverb_seconds.setValue(2.0)
            pump(qapp, 2.5)          # let the ring buffer fill

            window._measure_reverberation()
            assert wait_for(qapp, lambda: stored or failures, 40.0)
            assert not failures, failures
            return window, sorted(tmp_path.glob("*.wav"))
        finally:
            window.close()
            pump(qapp, 0.3)

    def test_the_analysed_waveform_is_written(self, qapp, sim, tmp_path):
        import json

        window, wavs = self._run(qapp, sim, tmp_path)
        assert len(wavs) == 1, [w.name for w in wavs]
        assert "reverb" in wavs[0].name and "T20" in wavs[0].name

        info = json.loads(wavs[0].with_suffix(".json").read_text(encoding="utf-8"))
        assert info["purpose"] == "reverberation"
        assert info["method"] == "T20"
        # Exactly the microphones that were analysed, no more.
        assert info["column_channels"] == [0, 2]
        assert info["receiver_positions"] == {"0": 1, "1": 3}
        assert info["frames"] > 0
        # It is the dump the fit ran on, not a second recording of the moment.
        assert "get_raw" in info["source"]

    def test_nothing_is_written_when_recording_is_off(self, qapp, sim, tmp_path):
        window, wavs = self._run(qapp, sim, tmp_path, enabled=False)
        assert wavs == []

    def test_a_failed_write_does_not_lose_the_measurement(self, qapp, sim, tmp_path):
        """The reverberation time matters more than the souvenir of it."""
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window.connect_btn.click()
            assert wait_for(qapp, lambda: window.pi is not None, 20.0)
            window.recording.enabled = True
            window.recording.directory = "/nonexistent/\0/bad"
            window.recorder.options = window.recording
            assert ensure_scanning(qapp, window)
            pump(qapp, 2.5)

            dumps = window._fetch_window(2.0, timeout=30.0)
            results = window._analyse_dumps(dumps, [0], method="T20")
            assert results, "the analysis must survive a write failure"
        finally:
            window.close()
            pump(qapp, 0.3)


class TestDumpWav:
    """`write_dump_wav` unit tests — no Pi, no GUI."""

    class _Dump:
        def __init__(self, data, rate=48000.0):
            self._data = data
            self.sample_rate = rate

        def channel(self, index):
            return self._data[index]

    class _Config:
        resample_active = True

        def device_of(self, channel):
            return 0 if channel < 2 else 1

        def channel_info(self, channel):
            class Info:
                units = "Pa"
                sensitivity_mv_per_unit = 50.0

            return Info()

    def test_columns_follow_the_requested_channels(self, tmp_path):
        import json

        from pislm.recorder import write_dump_wav

        dumps = {
            0: self._Dump({0: np.full(100, 1.0), 1: np.full(100, 2.0)}),
            1: self._Dump({2: np.full(100, 3.0), 3: np.full(100, 4.0)}),
        }
        path = write_dump_wav(tmp_path / "r.wav", dumps, [3, 0], config=self._Config())
        assert path is not None
        info = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        assert info["column_channels"] == [0, 3], "ascending, whatever order was asked"
        assert info["frames"] == 100

    def test_devices_are_trimmed_to_their_shortest(self, tmp_path):
        """A ragged write would put the columns out of step with each other."""
        import json

        from pislm.recorder import write_dump_wav

        dumps = {
            0: self._Dump({0: np.zeros(100), 1: np.zeros(100)}),
            1: self._Dump({2: np.zeros(60), 3: np.zeros(60)}),
        }
        path = write_dump_wav(tmp_path / "r.wav", dumps, [0, 2], config=self._Config())
        info = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        assert info["frames"] == 60

    def test_nothing_to_write_returns_none(self, tmp_path):
        from pislm.recorder import write_dump_wav

        assert write_dump_wav(tmp_path / "r.wav", {}, [0], config=self._Config()) is None
        assert not list(tmp_path.iterdir())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


# ── Analog output and excitation (PROTOCOL_OUTPUT.md) ───────────────
class TestOutput:
    def test_labels_count_from_one(self, qapp, sim):
        """Operators count receiver positions from 1; the wire stays 0-based."""
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            labels = [b.text() for b in window.channel_checks.values()]
            assert labels == ["Ch1", "Ch2", "Ch3", "Ch4", "Ch5", "Ch6"]
            assert set(window.reverb_channels) == set(range(6))
            assert window.cal_channel.minimum() == 1
            assert window.src_pos_current.minimum() == 1
        finally:
            window.close()
            pump(qapp)

    def test_light_impact_covers_100_to_3150(self, qapp, sim):
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window.source_combo.setCurrentIndex(
                list(ImpactSource).index(ImpactSource.TAPPING)
            )
            pump(qapp, 0.05)
            bands = window.session.bands
            assert min(bands) == 100 and max(bands) == 3150
            assert len(bands) == 16
        finally:
            window.close()
            pump(qapp)

    def test_all_four_signals_are_offered(self, qapp, sim):
        from pislm.excitation import Signal

        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            offered = {window.signal_combo.itemData(i)
                       for i in range(window.signal_combo.count())}
            assert offered == {s.value for s in Signal}
        finally:
            window.close()
            pump(qapp)

    def test_output_button_follows_hardware_availability(self, qapp, sim):
        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            assert not window.output_btn.isEnabled(), "must be off before connecting"
            window.connect_btn.click()
            assert wait_for(qapp, lambda: window.pi is not None, 20.0)
            assert wait_for(qapp, lambda: window.output_btn.isEnabled(), 10.0)
            assert window.pi.output_available
        finally:
            window.close()
            pump(qapp)

    def test_playing_reports_start_index_on_the_data_grid(self, qapp, sim):
        """That index is what lets the analysis line up with the recording."""
        from pislm.excitation import Signal

        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        window.show()
        try:
            window.connect_btn.click()
            assert wait_for(qapp, lambda: window.pi is not None, 20.0)
            window._apply_session()
            assert wait_for(qapp, lambda: window.worker.pending == 0, 20.0)
            assert ensure_scanning(qapp, window)

            window.signal_combo.setCurrentIndex(list(Signal).index(Signal.PINK))
            window.signal_seconds.setValue(1.0)
            window.output_btn.click()
            assert wait_for(
                qapp, lambda: "playing" in window.output_state.text(), 20.0
            ), window.output_state.text()

            status = window.pi.output_status()
            assert status["running"]
            assert status["start_index"] is not None

            assert wait_for(
                qapp, lambda: "finish" in window.output_state.text(), 25.0
            ), window.output_state.text()

            ensure_scanning(qapp, window, running=False)
        finally:
            window.close()
            pump(qapp, 0.3)

    def test_level_above_full_scale_is_refused(self, qapp, sim):
        from pislm import CommandError
        from pislm.excitation import ExcitationSettings

        ctl, stm = sim
        window = MainWindow("127.0.0.1", ctl, stm)
        try:
            window.connect_btn.click()
            assert wait_for(qapp, lambda: window.pi is not None, 20.0)
            with pytest.raises(CommandError, match="level_dbfs"):
                window.pi.set_output(ExcitationSettings(level_dbfs=6.0))
        finally:
            window.close()
            pump(qapp)


class TestLiveMeterBallistics:
    """The display must not alias against the excitation.

    A tapping machine runs at ten impacts a second. The level display refreshes
    at 20 Hz, so there are exactly two samples per impact — right at Nyquist.
    Taking one value per frame and discarding the rest lands on a peak or in a
    gap depending on drift, and the meter flickers over tens of decibels while
    the underlying level barely moves. A rubber ball at one impact every two
    seconds gets forty samples per impact and never aliases, which is why light
    impact flickered and heavy did not.

    The capture path always used the whole frame, so results were never
    affected — this is display only.
    """

    @staticmethod
    def _frame(channel, values):
        import numpy as np

        from pislm import LevelFrame

        return LevelFrame(channel=channel, levels_db=np.asarray(values, dtype=float))

    def test_the_display_keeps_the_interval_maximum(self):
        from app.live import LiveState

        live = LiveState()
        # One refresh interval carrying an impact peak and the gap after it.
        live.feed(self._frame(0, [92.0, 58.0]))
        assert live.snapshot_levels()[0] == 92.0, \
            "a frame's peak must not be thrown away for its last sample"

    def test_reading_clears_the_interval(self):
        from app.live import LiveState

        live = LiveState()
        live.feed(self._frame(0, [92.0]))
        assert live.snapshot_levels()[0] == 92.0
        live.feed(self._frame(0, [61.0]))
        assert live.snapshot_levels()[0] == 61.0, \
            "each refresh reports its own interval, not a running maximum"

    def test_an_interval_with_no_frame_holds_the_last_value(self):
        """The other half of the flicker: blanking a channel that simply had
        no frame this interval makes the meter blink."""
        from app.live import LiveState

        live = LiveState()
        live.feed(self._frame(0, [74.0]))
        assert live.snapshot_levels()[0] == 74.0
        assert live.snapshot_levels()[0] == 74.0

    def test_band_levels_get_the_same_treatment(self):
        import numpy as np

        from pislm import BandLevelFrame
        from pislm.frames import Handshake

        from app.live import LiveState

        live = LiveState()
        live.configure_bands(Handshake(raw={
            "bands": {"fraction": 3},
            "band_table": [{"device": 0, "bands": [{"index": 0, "center": 125.0}]}],
        }))
        live.feed(BandLevelFrame(band_index=0, channel=0,
                                 levels_db=np.array([88.0, 55.0])))
        assert live.snapshot_spectrum(0)[125.0] == 88.0
        assert live.snapshot_spectrum(0)[125.0] == 88.0   # held

    def test_capture_is_unchanged(self):
        """Recorded results always used the whole frame — that is why the
        final numbers were right while the screen was not."""
        from app.live import LiveState

        live = LiveState()
        live.start_capture()
        live.feed(self._frame(0, [92.0, 58.0]))
        live.feed(self._frame(0, [61.0, 60.0]))
        assert live.finish_capture([0]).broadband_max[0] == 92.0

    def test_a_band_table_change_clears_the_pending_maximum_too(self):
        """The interval accumulator holds readings under the *old* band
        indices, and would put them straight back under the new labels.

        Caught by the existing `test_changed_table_drops_stale_readings` when
        the accumulator was added — a 63 Hz reading reappeared as 500 Hz.
        """
        import numpy as np

        from pislm import BandLevelFrame
        from pislm.frames import Handshake

        from app.live import LiveState

        def table(center):
            return Handshake(raw={
                "bands": {"fraction": 3},
                "band_table": [{"device": 0, "bands": [{"index": 0, "center": center}]}],
            })

        live = LiveState()
        live.configure_bands(table(63.0957))
        live.feed(BandLevelFrame(0, 0, np.array([44.0])))
        live.configure_bands(table(501.1872))
        assert live.snapshot_spectrum(0) == {}
