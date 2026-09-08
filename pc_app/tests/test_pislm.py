"""Integration tests for the pislm client against the simulator.

    python -m pytest tests -v

The simulator runs in fragment/shuffle mode so the protocol traps (§9) are real.
"""

from __future__ import annotations

import socket
import struct
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pislm_sim
from pislm import (
    BandLevelFrame,
    CommandError,
    DataFrame,
    Handshake,
    LevelFrame,
    LineBuffer,
    PiSLM,
    ProtocolError,
    decode_frame,
    recv_exact,
)
from pislm.framing import FRAME_BAND_LEVEL, FRAME_DATA, FRAME_LEVEL, FRAME_MSG, FRAME_RAW_DUMP


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def sim():
    """Simulator with fragment + shuffle on, forcing reassembly and id matching."""
    pislm_sim.reset_state()
    pislm_sim.OPTS.fragment = True
    pislm_sim.OPTS.shuffle = True
    pislm_sim.OPTS.drop = 0.0
    pislm_sim.OPTS.overload = 0.0
    ctl_port, stm_port = free_port(), free_port()
    ctl, stm = pislm_sim.serve(ctl_port, stm_port)
    time.sleep(0.2)
    yield ctl_port, stm_port
    pislm_sim.stop_scan()
    ctl.shutdown()
    stm.shutdown()


@pytest.fixture
def pi(sim):
    ctl_port, stm_port = sim
    client = PiSLM("127.0.0.1", ctl_port, stm_port, connect_timeout=5.0)
    client.connect()
    yield client
    try:
        client.stop()
    except Exception:
        pass
    client.close()


# ── Pure unit tests (no sockets) ────────────────────────────────────
class TestFraming:
    def test_line_buffer_splits_multiple_lines_in_one_feed(self):
        buf = LineBuffer()
        out = list(buf.feed(b'{"a":1}\n{"b":2}\n'))
        assert out == [{"a": 1}, {"b": 2}]

    def test_line_buffer_holds_partial_line(self):
        buf = LineBuffer()
        assert list(buf.feed(b'{"a":')) == []
        assert list(buf.feed(b"1}\n")) == [{"a": 1}]

    def test_line_buffer_handles_byte_at_a_time(self):
        buf = LineBuffer()
        blob = b'{"type":"event","event":"stopped"}\n'
        got = [m for byte in blob for m in buf.feed(bytes([byte]))]
        assert got == [{"type": "event", "event": "stopped"}]

    def test_line_buffer_rejects_bad_json(self):
        with pytest.raises(ProtocolError):
            list(LineBuffer().feed(b"not json\n"))

    def test_recv_exact_reassembles_split_sends(self):
        a, b = socket.socketpair()
        payload = bytes(range(256)) * 40

        def writer():
            for i in range(0, len(payload), 7):
                b.sendall(payload[i : i + 7])
                time.sleep(0)

        threading.Thread(target=writer, daemon=True).start()
        assert recv_exact(a, len(payload)) == payload
        a.close()
        b.close()

    def test_recv_exact_raises_on_close(self):
        from pislm import PeerClosed

        a, b = socket.socketpair()
        b.sendall(b"abc")
        b.close()
        with pytest.raises(PeerClosed):
            recv_exact(a, 10)
        a.close()


class TestDecode:
    def test_level_frame(self):
        levels = np.array([61.2, 62.5], dtype="<f8")
        frame = decode_frame(FRAME_LEVEL, struct.pack("<I", 3) + levels.tobytes())
        assert isinstance(frame, LevelFrame)
        assert frame.channel == 3
        np.testing.assert_allclose(frame.levels_db, levels)

    def test_band_level_frame(self):
        levels = np.array([44.0], dtype="<f8")
        payload = struct.pack("<II", 12, 5) + levels.tobytes()
        frame = decode_frame(FRAME_BAND_LEVEL, payload)
        assert isinstance(frame, BandLevelFrame)
        assert (frame.band_index, frame.channel) == (12, 5)

    def test_data_frame_reshape_is_channel_fastest(self):
        # chA[n], chB[n], chA[n+1], chB[n+1] ...
        raw = np.array([1.0, 10.0, 2.0, 20.0, 3.0, 30.0], dtype="<f8")
        frame = decode_frame(FRAME_DATA, struct.pack("<I", 1) + raw.tobytes())
        assert isinstance(frame, DataFrame)
        block = frame.reshape(2)
        np.testing.assert_allclose(block[:, 0], [1.0, 2.0, 3.0])
        np.testing.assert_allclose(block[:, 1], [10.0, 20.0, 30.0])

    def test_unknown_type_is_returned_not_raised(self):
        from pislm import UnknownFrame

        frame = decode_frame(0x7F, b"\x00\x01")
        assert isinstance(frame, UnknownFrame) and frame.frame_type == 0x7F

    def test_truncated_payload_raises(self):
        with pytest.raises(ProtocolError):
            decode_frame(FRAME_LEVEL, b"\x01\x02")

    def test_msg_frame(self):
        frame = decode_frame(FRAME_MSG, b'{"type":"event","event":"stopped"}')
        assert frame.payload["event"] == "stopped"


class TestHandshakeModel:
    @pytest.fixture
    def hs(self):
        """Build the snapshot from a **fresh** SimState.

        Using the global `pislm_sim.STATE` lets band settings from an earlier
        module (the GUI tests) leak in, and this test then fails depending on
        file execution order. It actually broke that way.
        """
        state = pislm_sim.SimState()
        return Handshake(raw=state.snapshot(include_band_table=True))

    def test_channel_lookup(self, hs):
        info = hs.channel_info(3)
        assert info.device == 1 and info.device_type == "dt9837a" and info.local == 1
        assert info.units == "Pa" and info.calibrated

    def test_uncalibrated_channel_reports_volts(self, hs):
        assert hs.channel_info(1).units == "V"
        assert not hs.channel_info(1).calibrated

    def test_device_rate_follows_resample_when_active(self, hs):
        assert hs.resample_active
        assert hs.device_rate(0) == hs.resample["output_rate"]

    def test_band_lookup_via_channel(self, hs):
        band = hs.band_info_for_channel(4, 0)
        assert band.f_lo < band.center < band.f_hi

    def test_nominal_centers_snap_to_standard_values(self, hs):
        centers = {b.nominal_center for b in hs.bands_of_device(0)}
        for expected in (63, 125, 250, 500, 1000):
            assert expected in centers, f"{expected} Hz missing: {sorted(centers)}"

    def test_status_style_snapshot_has_no_network_block(self):
        assert Handshake(raw={"running": True}).stream_frames_dropped is None


# ── Integration tests (against the simulator) ───────────────────────
class TestConnection:
    def test_handshake_on_both_ports(self, pi):
        assert pi.config.protocol in ("pislm/3", "pislm/4")
        assert pi.config.num_channels == 6
        assert pi.stream.handshake is not None

    def test_ping(self, pi):
        assert pi.ping()

    def test_idle_socket_does_not_look_like_a_disconnect(self, pi):
        """§9.3 — a long silence must not look like a disconnect."""
        time.sleep(2.0)
        assert pi.connected and pi.ping()

    def test_pipelined_commands_match_by_id_not_order(self, pi):
        """Shuffled response order must not misroute results (§9.5)."""
        results: dict[int, float] = {}
        errors: list[BaseException] = []

        def query(ch: int):
            try:
                results[ch] = pi.get_sensitivity(ch)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=query, args=(c,)) for c in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

        assert not errors, errors
        assert results[1] == 1000.0  # the one uncalibrated channel
        assert all(results[c] == 50.0 for c in (0, 2, 3, 4, 5))


class TestCommands:
    def test_set_sensitivity_changes_units(self, pi):
        pi.stop()
        result = pi.set_sensitivity(1, 50.0)
        assert result["units"] == "Pa"
        pi.set_sensitivity(1, 1000.0)
        assert pi.refresh().channel_info(1).units == "V"

    def test_config_command_rejected_while_running(self, pi):
        pi.start()
        with pytest.raises(CommandError) as exc:
            pi.control.send("set_sample_rate", sample_rate=25600)
        assert exc.value.needs_stop
        pi.stop()

    def test_send_stopped_recovers_automatically(self, pi):
        """§9.8 — recover by itself with stop -> retry -> start."""
        pi.start()
        assert pi.running
        pi.set_weighting(frequency="C", time_weighting="Slow")
        assert pi.running  # must have come back up
        assert pi.refresh().weighting == {"frequency": "C", "time": "Slow"}
        pi.set_weighting(frequency="A", time_weighting="Fast")
        pi.stop()

    def test_start_stop_events_are_broadcast(self, pi):
        seen: list[str] = []
        pi.on_event(lambda msg: seen.append(msg.get("event")))
        pi.start()
        pi.stop()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and "stopped" not in seen:
            time.sleep(0.05)
        assert "started" in seen and "stopped" in seen

    def test_started_event_carries_band_table(self, pi):
        pi.start()
        assert pi.config.has_band_table
        pi.stop()

    def test_get_metrics(self, pi):
        pi.start()
        time.sleep(0.5)
        result = pi.get_metrics(seconds=2, channels=[0, 3], include_bands=True)
        assert set(result["channels"]) == {"0", "3"}
        assert "Leq" in result["channels"]["0"]
        assert result["channels"]["0"]["bands"]
        pi.stop()

    def test_one_third_octave_setup_for_light_impact(self, pi):
        """Light impact: 1/3 octave, 100-3150 Hz."""
        pi.stop()
        result = pi.set_bands(enabled=True, output="level", fraction=3, f_min=100, f_max=3150)
        centers = [b["center"] for b in result["band_table"][0]["bands"]]
        assert len(centers) >= 15
        pi.set_bands(fraction=3, f_min=20, f_max=20000)

    def test_one_octave_setup_for_heavy_impact(self, pi):
        """Heavy impact: 1/1 octave, 63-500 Hz — four bands."""
        pi.stop()
        result = pi.set_bands(enabled=True, output="level", fraction=1, f_min=63, f_max=500)
        hs = Handshake(raw={"band_table": result["band_table"], "channel_map": [], "channels": []})
        nominals = sorted(b.nominal_center for b in hs.bands_of_device(0))
        assert nominals == [63, 125, 250, 500], nominals
        pi.set_bands(fraction=3, f_min=20, f_max=20000)

    def test_calibrate(self, pi):
        pi.stop()
        result = pi.calibrate(0, level_db=94.0, apply=True)
        assert result["applied"]
        assert abs(result["change_db"] - (result["measured_level_db"] - 94.0)) < 1e-6
        pi.set_sensitivity(0, 50.0)


class TestStreaming:
    def test_level_frames_arrive(self, pi):
        pi.stop()
        pi.set_level(enabled=True, output_rate=10.0)
        pi.start()
        seen: set[int] = set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(seen) < 6:
            frame = pi.stream.get(timeout=0.5)
            if isinstance(frame, LevelFrame):
                seen.add(frame.channel)
        pi.stop()
        assert seen == set(range(6)), f"channels with levels: {sorted(seen)}"

    def test_band_level_frames_resolve_against_band_table(self, pi):
        pi.stop()
        pi.set_bands(enabled=True, output="level", fraction=1, f_min=63, f_max=500)
        pi.start()
        resolved = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and resolved is None:
            frame = pi.stream.get(timeout=0.5)
            if isinstance(frame, BandLevelFrame):
                resolved = pi.config.band_info_for_channel(frame.channel, frame.band_index)
        pi.stop()
        pi.set_bands(fraction=3, f_min=20, f_max=20000)
        assert resolved is not None
        assert resolved.nominal_center in (63, 125, 250, 500)

    def test_raw_data_only_when_enabled(self, pi):
        pi.stop()
        pi.set_options(stream_raw=False)
        pi.start()
        time.sleep(0.7)
        assert not any(isinstance(f, DataFrame) for f in pi.stream.drain())
        pi.stop()

        pi.set_options(stream_raw=True)
        pi.start()
        found = False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not found:
            frame = pi.stream.get(timeout=0.5)
            if isinstance(frame, DataFrame):
                block = frame.reshape(len(pi.config.device_channels(frame.device)))
                assert block.shape[1] == len(pi.config.device_channels(frame.device))
                found = True
        pi.stop()
        pi.set_options(stream_raw=False)
        assert found

    def test_fetch_raw_reassembles_chunks(self, pi):
        pi.start()
        time.sleep(0.9)
        dumps = pi.fetch_raw(seconds=1.0, timeout=30.0)
        pi.stop()

        assert set(dumps) == {0, 1}
        assert dumps[0].samples.shape[1] == 2   # mcc172
        assert dumps[1].samples.shape[1] == 4   # dt9837a
        for dump in dumps.values():
            assert dump.sample_rate > 0
            assert 0.5 < dump.seconds <= 1.5
            assert np.isfinite(dump.samples).all()
        # Channel extraction must agree with the channel map
        assert dumps[1].channel(3).shape[0] == dumps[1].samples.shape[0]

    def test_get_raw_requires_stream_client(self, sim):
        ctl_port, stm_port = sim
        client = PiSLM("127.0.0.1", ctl_port, stm_port, open_stream=False)
        client.connect()
        try:
            with pytest.raises(RuntimeError):
                client.get_raw(seconds=1)
        finally:
            client.close()

    def test_stats_and_bench(self, pi):
        pi.stop()
        pi.set_options(stream_raw=True)
        pi.start()
        report = pi.bench(seconds=2.0)
        pi.stop()
        pi.set_options(stream_raw=False)

        assert report["mbps"] > 0
        assert report["frames_per_s"] > 0
        assert report["control_rtt_ms"] >= 0
        # No DSP-side loss — the simulator always produces blocks on time
        assert report["dropped_blocks_delta"] == 0

        # Network drops cannot be guaranteed to be zero: the simulator queues
        # per §6 and discards on overflow, which really happens on a loopback
        # under pytest load. The counter is also server-global, so drops from
        # connections left by other tests mix in. The exact agreement between
        # server report and client detection is checked in test_protocol_v4.
        dropped = report["stream_frames_dropped_delta"]
        assert isinstance(dropped, int) and dropped >= 0


class TestReconnect:
    def test_reconnect_gets_fresh_handshake(self, sim):
        """§6 — reconnecting at any time re-reads the current state."""
        ctl_port, stm_port = sim
        first = PiSLM("127.0.0.1", ctl_port, stm_port)
        first.connect()
        first.stop()
        first.set_sample_rate(25600)
        first.close()

        second = PiSLM("127.0.0.1", ctl_port, stm_port)
        second.connect()
        try:
            assert second.config.sample_rate == 25600
        finally:
            second.stop()
            second.set_sample_rate(51200)
            second.close()

    def test_pending_request_fails_fast_on_disconnect(self, sim):
        from pislm import PeerClosed

        ctl_port, stm_port = sim
        client = PiSLM("127.0.0.1", ctl_port, stm_port, open_stream=False)
        client.connect()
        client.control._sock.close()  # noqa: SLF001 — force the cut
        with pytest.raises((PeerClosed, OSError)):
            client.send("status")
        assert not client.ping()  # ping returns False rather than raising
        client.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


class TestIntegrityVerdict:
    """The state the verdict used to mislabel.

    `GapDetector` only becomes active once a frame carrying a `start_index`
    has arrived, so before the scan starts — and any time it is stopped —
    there is nothing to judge. That was reported as "server is pislm/3 —
    frame loss cannot be verified": a red failure, on a healthy pislm/4
    server, naming the wrong cause.
    """

    def test_the_protocol_version_is_read_from_the_handshake(self, pi):
        assert pi.config.protocol == "pislm/4"
        assert pi.protocol_version == 4

    def test_nothing_received_yet_is_unknown_not_invalid(self, pi):
        assert not pi.gaps.active
        status, reasons = pi.measurement_status
        assert status == "unknown", (status, reasons)
        assert not any("pislm/3" in r for r in reasons)
        # Still not something to put in a report, though.
        assert pi.measurement_valid[0] is False

    def test_a_running_v4_scan_is_ok(self, pi):
        pi.start()
        try:
            time.sleep(1.0)
            status, reasons = pi.measurement_status
            assert status in ("ok", "warning"), (status, reasons)
            assert not any("pislm/3" in r for r in reasons)
        finally:
            pi.stop()


class TestLossDiagnosis:
    """Telling a per-frame counting error from a real dropout.

    They are identical in the totals and completely different in the detail,
    so `diagnosis()` reads the detail: a counting error gaps on every frame by
    the same amount, a stall gaps once when the link stalled.
    """

    @staticmethod
    def _detector(step, gap_at, gap_size, frames=60, channels=6, bands=16):
        import numpy as np

        from pislm.frames import BandLevelFrame, LevelFrame
        from pislm.gaps import GapDetector

        det = GapDetector()
        det.level_output_rate = 20.0
        for channel in range(channels):
            makers = [lambda i, c=channel: LevelFrame(c, np.zeros(1), i)]
            makers += [
                lambda i, c=channel, b=band: BandLevelFrame(b, c, np.zeros(1), i)
                for band in range(bands)
            ]
            for make in makers:
                index = 0
                for n in range(frames):
                    det.feed(make(index))
                    index += step
                    if gap_at is not None and n == gap_at:
                        index += gap_size
        return det

    def test_a_grid_mismatch_is_named_as_such(self):
        text = self._detector(2400, None, 0).diagnosis()
        assert "index-grid mismatch" in text
        assert "genuine stall" not in text

    def test_a_real_dropout_is_named_as_such(self):
        text = self._detector(1, 30, 30).diagnosis()
        assert "genuine stall" in text
        assert "index-grid mismatch" not in text

    def test_the_summary_gives_seconds_not_just_a_sample_count(self):
        summary = self._detector(1, 30, 30).reliable_summary()
        assert "3,060 samples" in summary
        assert "≈1.5 s per affected stream" in summary

    def test_no_loss_says_so(self):
        assert "No loss" in self._detector(1, None, 0).diagnosis()


class TestStreamQueue:
    """The queue nobody drains.

    Both GUIs read frames through `on_frame` callbacks. The queue is a second,
    independent path that only `next_frames()` consumes — so left enabled it
    fills within seconds and then counts every subsequent frame as an
    overflow. Nothing is lost (callbacks and the gap detector run before the
    queue), but `measurement_warnings` reports "the consumer is slower than
    the stream" for ever, on a perfectly healthy link.
    """

    def test_zero_disables_the_queue(self, sim):
        ctl_port, stm_port = sim
        pi = PiSLM("127.0.0.1", ctl_port, stm_port, stream_queue_size=0)
        try:
            pi.connect()
            assert pi.stream._queue is None
            with pytest.raises(RuntimeError, match="queue is disabled"):
                pi.stream.get(timeout=0.01)
        finally:
            pi.close()

    def test_callbacks_still_get_every_frame_without_a_queue(self, sim):
        ctl_port, stm_port = sim
        pi = PiSLM("127.0.0.1", ctl_port, stm_port, stream_queue_size=0)
        seen = []
        try:
            pi.connect()
            pi.on_frame(seen.append)
            pi.start()
            time.sleep(1.0)
            assert len(seen) > 50
            assert pi.stream.stats.queue_overflows == 0
            assert not any("queue overflow" in w for w in pi.measurement_warnings)
        finally:
            try:
                pi.stop()
            except Exception:
                pass
            pi.close()

    def test_the_gui_asks_for_no_queue(self):
        import inspect

        from app.main import MainWindow

        assert "stream_queue_size=0" in inspect.getsource(MainWindow._toggle_connection)
