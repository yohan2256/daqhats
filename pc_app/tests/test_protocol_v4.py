"""pislm/4 amendment verification — PROTOCOL_v4_AMENDMENT.md.

Three questions:
  1. Does start_index catch loss down to the **exact sample count** (§1.4)?
  2. Does epoch convert a sample index to wall-clock time (§2)?
  3. Does it still work against a v3 server — backwards compatibility (§5)?
"""

from __future__ import annotations

import socket
import struct
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pislm_sim
from pislm import (
    LAYOUT_V3,
    LAYOUT_V4,
    NO_INDEX,
    BandLevelFrame,
    DataFrame,
    GapDetector,
    Handshake,
    LevelFrame,
    PiSLM,
    decode_frame,
    layout_of,
)
from pislm.framing import FRAME_BAND_LEVEL, FRAME_DATA, FRAME_LEVEL, FRAME_RAW_DUMP


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_sim(protocol: int = 4, **opts):
    """Start a simulator with the given options; returns ports and servers."""
    pislm_sim.reset_state()  # clear leftovers from an earlier test module
    pislm_sim.STATE.protocol_version = protocol
    for key, value in opts.items():
        setattr(pislm_sim.OPTS, key, value)
    ctl_port, stm_port = free_port(), free_port()
    ctl, stm = pislm_sim.serve(ctl_port, stm_port)
    time.sleep(0.2)
    return ctl_port, stm_port, ctl, stm


# ── 1. Layout detection and decoding ────────────────────────────────
class TestLayout:
    def test_layout_detection(self):
        assert layout_of({"protocol": "pislm/3"}) == LAYOUT_V3
        assert layout_of({"protocol": "pislm/4"}) == LAYOUT_V4
        assert layout_of({"frame_layout": 4}) == LAYOUT_V4
        assert layout_of({"frame_layout": 3, "protocol": "pislm/4"}) == LAYOUT_V3
        assert layout_of(None) == LAYOUT_V3  # no information -> assume v3

    def test_v4_level_frame_carries_index(self):
        levels = np.array([61.2, 62.5], dtype="<f8")
        payload = struct.pack("<I", 3) + struct.pack("<Q", 48000) + levels.tobytes()
        frame = decode_frame(FRAME_LEVEL, payload, LAYOUT_V4)
        assert frame.channel == 3
        assert frame.start_index == 48000
        np.testing.assert_allclose(frame.levels_db, levels)

    def test_v3_frame_reports_no_index(self):
        levels = np.array([61.2], dtype="<f8")
        frame = decode_frame(FRAME_LEVEL, struct.pack("<I", 3) + levels.tobytes(), LAYOUT_V3)
        assert frame.start_index == NO_INDEX

    def test_v4_data_frame(self):
        raw = np.array([1.0, 10.0, 2.0, 20.0], dtype="<f8")
        payload = struct.pack("<I", 1) + struct.pack("<Q", 96000) + raw.tobytes()
        frame = decode_frame(FRAME_DATA, payload, LAYOUT_V4)
        assert frame.device == 1 and frame.start_index == 96000
        assert frame.count_for(2) == 2
        np.testing.assert_allclose(frame.reshape(2)[:, 0], [1.0, 2.0])

    def test_v4_band_level_frame(self):
        levels = np.array([44.0], dtype="<f8")
        payload = struct.pack("<II", 12, 5) + struct.pack("<Q", 100) + levels.tobytes()
        frame = decode_frame(FRAME_BAND_LEVEL, payload, LAYOUT_V4)
        assert (frame.band_index, frame.channel, frame.start_index) == (12, 5, 100)

    def test_v4_raw_dump_chunk(self):
        samples = np.zeros(4, dtype="<f8")
        payload = struct.pack("<IIII", 7, 1, 0, 1) + struct.pack("<Q", 1234) + samples.tobytes()
        frame = decode_frame(FRAME_RAW_DUMP, payload, LAYOUT_V4)
        assert frame.dump_id == 7 and frame.start_index == 1234 and frame.is_last

    def test_u64_survives_long_sessions(self):
        """u32 would overflow after about 23 hours at 51.2 kHz (§1.1)."""
        index = int(51200 * 3600 * 24 * 30)  # 30 days
        assert index > 2**32
        payload = struct.pack("<I", 0) + struct.pack("<Q", index) + np.zeros(1, "<f8").tobytes()
        assert decode_frame(FRAME_LEVEL, payload, LAYOUT_V4).start_index == index

    def test_truncated_v4_payload_raises(self):
        from pislm import ProtocolError

        # Valid as v3, but eight bytes short of the v4 header
        with pytest.raises(ProtocolError):
            decode_frame(FRAME_LEVEL, struct.pack("<I", 0) + b"\x00" * 4, LAYOUT_V4)


# ── 2. Gap detector (no sockets) ────────────────────────────────────
class TestGapDetectorUnit:
    def test_clean_stream_has_no_gaps(self):
        det = GapDetector()
        for i in range(0, 100, 10):
            assert det.feed(LevelFrame(0, np.zeros(10), start_index=i)) is None
        assert det.clean and det.total_missing == 0

    def test_gap_is_located_exactly(self):
        det = GapDetector()
        det.feed(LevelFrame(0, np.zeros(10), start_index=0))
        det.feed(LevelFrame(0, np.zeros(10), start_index=10))
        gap = det.feed(LevelFrame(0, np.zeros(10), start_index=55))  # 20..55 lost
        assert gap is not None
        assert gap.expected_index == 20 and gap.got_index == 55
        assert gap.missing == 35
        assert det.total_missing == 35 and not det.clean

    def test_streams_are_tracked_independently(self):
        det = GapDetector()
        det.feed(LevelFrame(0, np.zeros(10), start_index=0))
        det.feed(LevelFrame(1, np.zeros(10), start_index=0))
        det.feed(LevelFrame(0, np.zeros(10), start_index=30))  # only ch0 loses
        det.feed(LevelFrame(1, np.zeros(10), start_index=10))  # ch1 is fine
        tracks = det.tracks
        assert tracks[("level", 0)].missing == 20
        assert tracks[("level", 1)].missing == 0

    def test_band_streams_keyed_by_band_and_channel(self):
        det = GapDetector()
        det.feed(BandLevelFrame(2, 0, np.zeros(5), start_index=0))
        det.feed(BandLevelFrame(3, 0, np.zeros(5), start_index=0))  # other band, other grid
        assert det.total_missing == 0
        assert len(det.tracks) == 2

    def test_data_frame_needs_channel_count(self):
        det = GapDetector()
        # Without the channel count it cannot count, so it skips quietly
        assert det.feed(DataFrame(0, np.zeros(8), start_index=0)) is None
        assert not det.tracks

        det.device_channels = {0: 2}
        det.feed(DataFrame(0, np.zeros(8), start_index=0))   # 4 samples per channel
        gap = det.feed(DataFrame(0, np.zeros(8), start_index=10))
        assert gap.missing == 6   # should have ended at 4 but started at 10

    def test_out_of_order_does_not_rewind_the_clock(self):
        det = GapDetector()
        det.feed(LevelFrame(0, np.zeros(10), start_index=0))
        det.feed(LevelFrame(0, np.zeros(10), start_index=10))
        det.feed(LevelFrame(0, np.zeros(10), start_index=5))   # protocol violation
        assert det.tracks[("level", 0)].out_of_order == 1
        assert det.tracks[("level", 0)].next_index == 20       # never rewound
        assert not det.clean

    def test_v3_frames_leave_detector_inactive(self):
        det = GapDetector()
        for _ in range(5):
            det.feed(LevelFrame(0, np.zeros(10)))  # no start_index
        assert not det.active
        assert not det.clean  # "cannot tell" is not "clean"
        assert "pislm/3" in det.report()

    def test_reset_clears_state(self):
        det = GapDetector()
        det.feed(LevelFrame(0, np.zeros(10), start_index=0))
        det.feed(LevelFrame(0, np.zeros(10), start_index=50))
        assert det.total_missing == 40
        det.reset()
        assert det.total_missing == 0 and not det.tracks

    def test_loss_ratio(self):
        det = GapDetector()
        det.feed(LevelFrame(0, np.zeros(100), start_index=0))
        det.feed(LevelFrame(0, np.zeros(100), start_index=200))  # 100 lost
        track = det.tracks[("level", 0)]
        assert track.span == 300 and track.missing == 100
        assert abs(track.loss_ratio - 1 / 3) < 1e-9


# ── 3. Against the simulator: inject loss, detect it ────────────────
@pytest.fixture(scope="module")
def lossy():
    """A v4 simulator that deliberately drops 20 % of DATA frames."""
    ports = make_sim(fragment=False, shuffle=False, drop=0.20, overload=0.0)
    yield ports[0], ports[1]
    pislm_sim.stop_scan()
    pislm_sim.OPTS.drop = 0.0
    ports[2].shutdown()
    ports[3].shutdown()


@pytest.fixture(scope="module")
def clean_sim():
    """A v4 simulator with no loss (fragmenting and shuffling stay on)."""
    ports = make_sim(fragment=True, shuffle=True, drop=0.0, overload=0.0)
    yield ports[0], ports[1]
    pislm_sim.stop_scan()
    ports[2].shutdown()
    ports[3].shutdown()


@pytest.fixture(scope="module")
def v3_sim():
    """Old layout server — for the backwards-compatibility checks."""
    ports = make_sim(protocol=3, fragment=True, shuffle=False, drop=0.0, overload=0.0)
    yield ports[0], ports[1]
    pislm_sim.stop_scan()
    ports[2].shutdown()
    ports[3].shutdown()


class TestGapDetectionAgainstSim:
    def test_detected_loss_matches_injected_loss(self, lossy):
        """The most important test — does detected loss match what was dropped?"""
        ctl, stm = lossy
        pi = PiSLM("127.0.0.1", ctl, stm)
        pi.connect()
        try:
            assert pi.config.has_sample_index, "the simulator must emit v4"
            pi.stop()
            pi.set_options(stream_raw=True)
            pi.set_bands(enabled=False)
            before = pi.refresh().stream_frames_dropped
            pi.start()
            time.sleep(2.0)
            pi.stop()
            time.sleep(0.3)
            after = pi.refresh().stream_frames_dropped

            injected_frames = after - before
            assert injected_frames > 0, "no loss was injected"

            det = pi.gaps
            assert det.active
            assert not det.clean, "there was loss but it reported clean"

            # Gaps counted on the DATA streams versus frames dropped
            # (consecutive drops merge into one gap, so gaps <= frames)
            data_tracks = {k: t for k, t in det.tracks.items() if k[0] == "data"}
            assert data_tracks, "the DATA streams were not tracked"

            # Consecutive drops merge into one gap, so gaps <= frames dropped
            total_gaps = sum(len(t.gaps) for t in data_tracks.values())
            assert 0 < total_gaps <= injected_frames

            # The simulator block is a fixed 0.1 s, so every gap must be an
            # integer number of blocks — that is what "counts exactly" means.
            block = int(pi.config.device_rate(0) * 0.1)
            for track in data_tracks.values():
                for gap in track.gaps:
                    assert gap.missing % block == 0, (
                        f"gap of {gap.missing} is not a multiple of the {block} block"
                    )

            # Detected <= injected. They cannot be equal: frames dropped **after**
            # the last received frame have nothing following them, so they are
            # undetectable in principle. Drops just before the scan stops qualify.
            detected = sum(t.missing for t in data_tracks.values())
            assert detected % block == 0
            assert 0 < detected // block <= injected_frames, (
                f"detected {detected // block} frames, injected {injected_frames}"
            )

            # Level frames are not droppable, so they must show no gaps (§4.2)
            level_tracks = {k: t for k, t in det.tracks.items() if k[0] == "level"}
            assert level_tracks
            assert all(t.missing == 0 for t in level_tracks.values()), \
                "level frames were dropped — violates §4.2 of the amendment"
        finally:
            pi.set_options(stream_raw=False)
            pi.close()

    def test_dropped_by_type_counter(self, lossy):
        ctl, stm = lossy
        with PiSLM("127.0.0.1", ctl, stm) as pi:
            pi.stop()
            pi.set_options(stream_raw=True)
            pi.start()
            time.sleep(1.0)
            pi.stop()
            by_type = pi.refresh().dropped_by_type
            pi.set_options(stream_raw=False)
        assert by_type.get("DATA", 0) > 0
        assert "LEVEL" not in by_type  # level was never droppable

    def test_level_stream_survives_data_loss(self, lossy):
        """The level stream must survive even while 20 % of DATA is dropped (§6)."""
        ctl, stm = lossy
        with PiSLM("127.0.0.1", ctl, stm) as pi:
            pi.stop()
            pi.set_options(stream_raw=True)
            pi.start()
            time.sleep(1.2)
            pi.stop()
            det = pi.gaps
            best, reliable = det.best_effort_missing, det.reliable_missing
            intact = det.levels_intact
            pi.set_options(stream_raw=False)
        assert best > 0, "no DATA loss was injected"
        assert reliable == 0, "level frames were dropped — violates §6"
        assert intact


# ── 4. Clean stream / epoch / overload ──────────────────────────────
class TestCleanV4Stream:
    def test_no_gaps_on_a_healthy_link(self, clean_sim):
        ctl, stm = clean_sim
        with PiSLM("127.0.0.1", ctl, stm) as pi:
            pi.stop()
            pi.set_options(stream_raw=True)
            pi.start()
            time.sleep(2.0)
            pi.stop()
            report = pi.gaps.report()
            valid, reasons = pi.measurement_valid
            pi.set_options(stream_raw=False)
        assert "No gaps" in report, report
        assert valid, reasons

    def test_epoch_converts_index_to_wall_clock(self, clean_sim):
        ctl, stm = clean_sim
        with PiSLM("127.0.0.1", ctl, stm) as pi:
            pi.stop()
            t_start = time.time()
            pi.start()
            time.sleep(1.0)

            frame = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and frame is None:
                candidate = pi.stream.get(timeout=0.5)
                if isinstance(candidate, LevelFrame) and candidate.start_index > 0:
                    frame = candidate
            pi.stop()

            assert frame is not None
            cfg = pi.config
            assert cfg.epoch, "the started event carried no epoch"

            when = cfg.time_of(frame.start_index, cfg.level_output_rate)
            assert when is not None
            # Must fall between the scan start and now
            assert t_start - 1 <= when <= time.time() + 1, (t_start, when, time.time())

    def test_index_resets_on_each_start(self, clean_sim):
        ctl, stm = clean_sim
        with PiSLM("127.0.0.1", ctl, stm) as pi:
            def first_index() -> int:
                pi.start()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    frame = pi.stream.get(timeout=0.5)
                    if isinstance(frame, LevelFrame):
                        pi.stop()
                        return frame.start_index
                pi.stop()
                raise AssertionError("no level frame arrived")

            pi.stop()
            pi.stream.drain()
            assert first_index() < 5
            time.sleep(0.5)
            pi.stream.drain()
            assert first_index() < 5, "the index did not reset on the second start"

    def test_raw_dump_aligns_with_live_stream(self, clean_sim):
        """RAW_DUMP must share the DATA grid (§1.2)."""
        ctl, stm = clean_sim
        with PiSLM("127.0.0.1", ctl, stm) as pi:
            pi.stop()
            pi.set_options(stream_raw=True)
            pi.start()
            time.sleep(1.0)

            live_index = 0
            for frame in pi.stream.drain():
                if isinstance(frame, DataFrame):
                    live_index = max(live_index, frame.start_index)

            result = pi.get_raw(seconds=0.5)
            dumps = {}

            deadline = time.monotonic() + 20
            pi.stream.on_dump(lambda d: dumps.setdefault(d.device, d))
            while time.monotonic() < deadline and len(dumps) < 2:
                time.sleep(0.1)
            pi.stop()
            pi.set_options(stream_raw=False)

        for dev_meta in result["devices"]:
            start = dev_meta["start_index"]
            end = start + dev_meta["samples_per_channel"]
            # The dump window should sit near where we were watching live
            assert start >= 0
            assert end <= live_index + dev_meta["sample_rate"] * 2, (start, end, live_index)

    def test_overload_events_are_collected(self):
        ports = make_sim(fragment=False, shuffle=False, drop=0.0, overload=0.9)
        ctl, stm = ports[0], ports[1]
        try:
            with PiSLM("127.0.0.1", ctl, stm) as pi:
                pi.start()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not pi.overloads:
                    time.sleep(0.1)
                pi.stop()
                valid, reasons = pi.measurement_valid
                counts = pi.refresh().overload_counts

            assert pi.overloads, "no overload event arrived"
            event = pi.overloads[0]
            assert {"device", "channel", "start_index", "samples", "peak"} <= set(event)
            assert not valid
            assert any("overload" in r for r in reasons), reasons
            assert sum(counts.values()) > 0
        finally:
            pislm_sim.stop_scan()
            pislm_sim.OPTS.overload = 0.0
            ports[2].shutdown()
            ports[3].shutdown()


# ── 5. Backwards compatibility — v3 server ──────────────────────────
class TestBackwardCompatibility:
    def test_v3_server_still_works(self, v3_sim):
        ctl, stm = v3_sim
        with PiSLM("127.0.0.1", ctl, stm) as pi:
            assert pi.config.protocol == "pislm/3"
            assert not pi.config.has_sample_index
            assert pi.stream.layout == LAYOUT_V3

            pi.stop()
            pi.set_options(stream_raw=True)
            pi.start()

            seen = 0
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and seen < 3:
                frame = pi.stream.get(timeout=0.5)
                if isinstance(frame, (LevelFrame, DataFrame)):
                    assert frame.start_index == NO_INDEX
                    seen += 1
            pi.stop()
            pi.set_options(stream_raw=False)
        assert seen >= 3

    def test_v3_reports_inability_to_verify(self, v3_sim):
        """On v3 the answer must be "cannot verify", not "no loss"."""
        ctl, stm = v3_sim
        with PiSLM("127.0.0.1", ctl, stm) as pi:
            pi.start()
            time.sleep(0.7)
            pi.stop()
            valid, reasons = pi.measurement_valid
            assert not pi.gaps.active
            assert not valid
            assert any("pislm/3" in r for r in reasons), reasons

    def test_v3_dump_still_reassembles(self, v3_sim):
        ctl, stm = v3_sim
        with PiSLM("127.0.0.1", ctl, stm) as pi:
            pi.start()
            time.sleep(0.8)
            dumps = pi.fetch_raw(seconds=0.5, timeout=30.0)
            pi.stop()
        assert set(dumps) == {0, 1}
        assert dumps[1].samples.shape[1] == 4
        assert all(d.samples.size > 0 for d in dumps.values())


# ── 6. §9.13 — arrival-order race between response and frames ───────
class TestDumpOrderingRace:
    """Chunks arriving **before** the get_raw response must still reassemble.

    The two ports are independent connections, so order is not guaranteed.
    Reshaping without the metadata (the channel count) silently yields a
    single-channel array — that is what must be prevented.
    """

    @staticmethod
    def _chunks(dump_id: int, device: int, nch: int, n_per_ch: int, parts: int):
        """Build RAW_DUMP chunks split into a given number of pieces."""
        from pislm import RawDumpChunk

        # Channel c is filled with c*1000 + t so it can be checked exactly later
        block = np.array(
            [[c * 1000 + t for c in range(nch)] for t in range(n_per_ch)], dtype="<f8"
        )
        flat = block.ravel()
        size = int(np.ceil(flat.size / parts))
        out = []
        for i in range(parts):
            piece = flat[i * size : (i + 1) * size]
            out.append(
                RawDumpChunk(
                    dump_id=dump_id, device=device, chunk_index=i,
                    is_last=(i == parts - 1), interleaved=piece,
                    start_index=1000 + (i * size) // nch,
                )
            )
        return out, block

    @staticmethod
    def _meta(dump_id: int, device: int, nch: int, n_per_ch: int) -> dict:
        return {
            "dump_id": dump_id, "chunk_samples": 4096, "units": "Pa",
            "devices": [{
                "device": device, "channels": list(range(2, 2 + nch)),
                "num_channels": nch, "sample_rate": 48000.0,
                "samples_per_channel": n_per_ch, "seconds": n_per_ch / 48000.0,
                "total_chunks": 3, "start_index": 1000,
            }],
        }

    def test_chunks_before_response_still_reshape_correctly(self):
        """This test is the heart of the §9.13 race."""
        from pislm.stream import _DumpAssembler

        asm = _DumpAssembler()
        chunks, expected = self._chunks(dump_id=1, device=1, nch=4, n_per_ch=50, parts=3)

        # Every chunk arrives before the response — must not complete yet
        for chunk in chunks:
            assert asm.add(chunk) is None, "completed a dump without metadata"

        # Now the response lands and the held dump should come out
        completed = asm.register(1, self._meta(1, 1, 4, 50))
        assert len(completed) == 1
        dump = completed[0]
        assert dump.samples.shape == (50, 4), f"wrong reshape: {dump.samples.shape}"
        np.testing.assert_allclose(dump.samples, expected)
        assert dump.channels == [2, 3, 4, 5]
        assert dump.sample_rate == 48000.0
        assert dump.start_index == 1000

    def test_normal_order_still_works(self):
        from pislm.stream import _DumpAssembler

        asm = _DumpAssembler()
        chunks, expected = self._chunks(dump_id=2, device=1, nch=4, n_per_ch=50, parts=3)

        assert asm.register(2, self._meta(2, 1, 4, 50)) == []
        results = [asm.add(c) for c in chunks]
        assert results[0] is None and results[1] is None
        dump = results[2]
        assert dump is not None
        assert dump.samples.shape == (50, 4)
        np.testing.assert_allclose(dump.samples, expected)

    def test_interleaved_arrival(self):
        """Some chunks -> response -> the rest."""
        from pislm.stream import _DumpAssembler

        asm = _DumpAssembler()
        chunks, expected = self._chunks(dump_id=3, device=1, nch=4, n_per_ch=50, parts=3)

        assert asm.add(chunks[0]) is None
        assert asm.register(3, self._meta(3, 1, 4, 50)) == []
        assert asm.add(chunks[1]) is None
        dump = asm.add(chunks[2])
        assert dump is not None and dump.samples.shape == (50, 4)
        np.testing.assert_allclose(dump.samples, expected)

    def test_out_of_order_chunk_indexes(self):
        """Chunks arriving out of order must reassemble by chunk_index."""
        from pislm.stream import _DumpAssembler

        asm = _DumpAssembler()
        chunks, expected = self._chunks(dump_id=4, device=1, nch=4, n_per_ch=50, parts=3)
        asm.register(4, self._meta(4, 1, 4, 50))

        assert asm.add(chunks[2]) is None   # is_last came first
        assert asm.add(chunks[0]) is None
        dump = asm.add(chunks[1])
        assert dump is not None
        np.testing.assert_allclose(dump.samples, expected)

    def test_offset_of_locates_a_live_event_in_the_dump(self):
        """Convert a live DATA index into a position inside the dump (§2.5)."""
        from pislm.stream import _DumpAssembler

        asm = _DumpAssembler()
        chunks, _ = self._chunks(dump_id=5, device=1, nch=4, n_per_ch=50, parts=3)
        asm.register(5, self._meta(5, 1, 4, 50))
        for chunk in chunks:
            dump = asm.add(chunk)

        assert dump.start_index == 1000 and dump.end_index == 1050
        assert dump.offset_of(1000) == 0
        assert dump.offset_of(1025) == 25
        with pytest.raises(ValueError):
            dump.offset_of(999)
        with pytest.raises(ValueError):
            dump.offset_of(1050)


# ── 7. Loss severity (§6, §9.12) ────────────────────────────────────
class TestLossSeverity:
    def test_data_loss_is_tolerated_level_loss_is_not(self):
        det = GapDetector(device_channels={0: 2})

        det.feed(DataFrame(0, np.zeros(20), start_index=0))     # 10 per channel
        det.feed(DataFrame(0, np.zeros(20), start_index=50))    # 40 lost (allowed)
        det.feed(LevelFrame(0, np.zeros(10), start_index=0))
        det.feed(LevelFrame(0, np.zeros(10), start_index=10))   # fine

        assert det.best_effort_missing == 40
        assert det.reliable_missing == 0
        assert det.levels_intact, "DATA loss contaminated the level verdict"
        assert not det.clean       # strictly speaking, not clean

    def test_level_loss_breaks_levels_intact(self):
        det = GapDetector()
        det.feed(LevelFrame(0, np.zeros(10), start_index=0))
        det.feed(LevelFrame(0, np.zeros(10), start_index=30))   # 20 lost
        assert det.reliable_missing == 20
        assert not det.levels_intact

    def test_band_waveform_counts_as_best_effort(self):
        from pislm import BandFrame

        det = GapDetector()
        det.feed(BandFrame(0, 0, np.zeros(10), start_index=0))
        det.feed(BandFrame(0, 0, np.zeros(10), start_index=30))
        assert det.best_effort_missing == 20
        assert det.levels_intact  # BAND waveform loss is unrelated to levels

    def test_band_level_counts_as_reliable(self):
        det = GapDetector()
        det.feed(BandLevelFrame(0, 0, np.zeros(10), start_index=0))
        det.feed(BandLevelFrame(0, 0, np.zeros(10), start_index=30))
        assert det.reliable_missing == 20
        assert not det.levels_intact

    def test_report_labels_severity(self):
        det = GapDetector(device_channels={0: 2})
        det.feed(DataFrame(0, np.zeros(20), start_index=0))
        det.feed(DataFrame(0, np.zeros(20), start_index=50))
        report = det.report()
        assert "allowed" in report, report
        assert "get_raw" in report

    def test_data_loss_alone_does_not_invalidate_measurement(self):
        """§9.12 — DATA loss is designed behaviour and must not void a measurement.

        The simulator has a single global STATE that earlier fixtures rebind, so
        this test starts its own server.
        """
        ports = make_sim(fragment=False, shuffle=False, drop=0.25, overload=0.0)
        ctl, stm = ports[0], ports[1]
        try:
            with PiSLM("127.0.0.1", ctl, stm) as pi:
                pi.stop()
                pi.set_options(stream_raw=True)
                pi.start()
                time.sleep(1.5)
                pi.stop()

                valid, reasons = pi.measurement_valid
                warnings = pi.measurement_warnings
                best = pi.gaps.best_effort_missing
                reliable = pi.gaps.reliable_missing

            assert best > 0, "no DATA loss was injected"
            assert reliable == 0
            assert valid, f"invalidated by DATA loss alone: {reasons}"
            assert any("get_raw" in w for w in warnings), warnings
        finally:
            pislm_sim.stop_scan()
            pislm_sim.OPTS.drop = 0.0
            ports[2].shutdown()
            ports[3].shutdown()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
