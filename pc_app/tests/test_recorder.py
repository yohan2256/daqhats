"""WAV recording tests.

The property that matters most: **is the time axis preserved through loss?**
Without filling gaps with silence the file gets shorter and every impact after
that point shifts earlier — an error invisible in the file itself.
"""

from __future__ import annotations

import json
import struct
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pislm.frames import DataFrame  # noqa: E402
from pislm.recorder import (  # noqa: E402
    FORMATS,
    RecordingOptions,
    WavRecorder,
    WavWriter,
    estimate_size,
    format_size,
)
from pislm.standards import Spectrum  # noqa: E402


def read_wav_header(path: Path) -> dict:
    raw = path.read_bytes()
    assert raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"
    riff_size = struct.unpack("<I", raw[4:8])[0]
    fmt_code, channels, rate, byte_rate, block_align, bits = struct.unpack(
        "<HHIIHH", raw[20:36]
    )
    data_size = struct.unpack("<I", raw[40:44])[0]
    return {
        "riff_size": riff_size, "declared_total": len(raw) - 8,
        "format": fmt_code, "channels": channels, "rate": rate,
        "block_align": block_align, "bits": bits, "data_size": data_size,
        "payload": raw[44:],
    }


class TestWavWriter:
    @pytest.mark.parametrize("fmt", list(FORMATS))
    def test_header_sizes_are_patched_on_close(self, tmp_path, fmt):
        """Without patched header sizes most programs cannot read the file."""
        path = tmp_path / f"{fmt}.wav"
        with WavWriter(path, channels=2, sample_rate=48000, fmt=fmt) as writer:
            writer.write(np.zeros((1000, 2)))
        header = read_wav_header(path)
        assert header["riff_size"] == header["declared_total"]
        assert header["data_size"] == 1000 * header["block_align"]
        assert header["channels"] == 2 and header["rate"] == 48000

    def test_pcm_files_open_with_standard_wave_module(self, tmp_path):
        for fmt, bits in (("int16", 16), ("int24", 24)):
            path = tmp_path / f"{fmt}.wav"
            with WavWriter(path, 2, 48000, fmt) as writer:
                writer.write(np.zeros((500, 2)))
            with wave.open(str(path)) as handle:
                assert handle.getnchannels() == 2
                assert handle.getsampwidth() * 8 == bits
                assert handle.getnframes() == 500

    def test_float32_is_lossless_within_precision(self, tmp_path):
        """float32 stores Pa values as they are — no scaling."""
        data = np.random.default_rng(0).standard_normal((500, 2)) * 3.0
        path = tmp_path / "f.wav"
        with WavWriter(path, 2, 48000, "float32") as writer:
            writer.write(data)
        payload = read_wav_header(path)["payload"]
        restored = np.frombuffer(payload, dtype="<f4").reshape(-1, 2)
        np.testing.assert_allclose(restored, data, rtol=1e-6)

    def test_pcm_scales_by_full_scale(self, tmp_path):
        path = tmp_path / "p.wav"
        with WavWriter(path, 1, 48000, "int16", full_scale=2.0) as writer:
            writer.write(np.array([[2.0], [-2.0], [1.0], [0.0]]))
        payload = read_wav_header(path)["payload"]
        values = np.frombuffer(payload, dtype="<i2")
        assert values[0] == 32767 and values[1] == -32767
        assert abs(values[2] - 16383) <= 1 and values[3] == 0

    def test_pcm_clipping_is_counted_not_silent(self, tmp_path):
        """Above full scale it clips — the user needs to know."""
        path = tmp_path / "c.wav"
        with WavWriter(path, 1, 48000, "int16", full_scale=1.0) as writer:
            writer.write(np.array([[5.0], [0.1]]))
            assert writer.clipped == 1

    def test_interleaved_input_is_accepted(self, tmp_path):
        path = tmp_path / "i.wav"
        with WavWriter(path, 2, 48000, "float32") as writer:
            writer.write(np.array([1.0, 10.0, 2.0, 20.0]))
            assert writer.frames_written == 2
        restored = np.frombuffer(read_wav_header(path)["payload"], dtype="<f4")
        np.testing.assert_allclose(restored, [1.0, 10.0, 2.0, 20.0])

    def test_bad_interleave_length_rejected(self, tmp_path):
        with WavWriter(tmp_path / "x.wav", 2, 48000) as writer:
            with pytest.raises(ValueError):
                writer.write(np.array([1.0, 2.0, 3.0]))

    def test_unknown_format_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="unsupported format"):
            WavWriter(tmp_path / "x.wav", 2, 48000, fmt="mp3")

    def test_odd_data_size_is_padded(self, tmp_path):
        """RIFF chunks must be even-sized; odd needs a pad byte."""
        path = tmp_path / "odd.wav"
        with WavWriter(path, 1, 8000, "int24") as writer:
            writer.write(np.zeros((3, 1)))  # 9 bytes
        assert len(path.read_bytes()) % 2 == 0


class _Config:
    """The minimum interface WavRecorder needs."""

    def __init__(self, rate=48000.0, resample_active=True):
        self._rate = rate
        self.epoch = {"unix": 1785638285.8, "index": 0}
        self.resample_active = resample_active
        self.devices = [{"index": 0}, {"index": 1}]

    def device_channels(self, device):
        return {0: [0, 1], 1: [2, 3, 4, 5]}[device]

    def device_rate(self, device):
        return self._rate

    def channel_info(self, channel):
        class Info:
            units = "Pa"
            sensitivity_mv_per_unit = 50.0

        return Info()


def data_frame(device: int, channels: int, samples: int, start_index: int, value=0.1):
    return DataFrame(
        device=device,
        interleaved=np.full(samples * channels, value, dtype=float),
        start_index=start_index,
    )


class TestWavRecorder:
    def _recorder(self, tmp_path, **kwargs):
        options = RecordingOptions(
            enabled=True, directory=str(tmp_path), fmt="float32", **kwargs
        )
        return WavRecorder(options)

    def test_one_file_holds_every_channel(self, tmp_path):
        """All six channels are one measurement, so they belong in one file."""
        recorder = self._recorder(tmp_path)
        recorder.start(_Config(), label="t")
        recorder.feed(data_frame(0, 2, 100, 0))
        recorder.feed(data_frame(1, 4, 100, 0))
        written = recorder.stop()

        assert len(written) == 1
        header = read_wav_header(written[0])
        assert header["channels"] == 6
        assert header["data_size"] // header["block_align"] == 100

    def test_columns_are_in_global_channel_order(self, tmp_path):
        """The file order must match what the operator sees on screen."""
        recorder = self._recorder(tmp_path)
        recorder.start(_Config(), label="order")
        recorder.feed(data_frame(0, 2, 50, 0, value=1.0))   # channels 0,1
        recorder.feed(data_frame(1, 4, 50, 0, value=2.0))   # channels 2..5
        written = recorder.stop()

        header = read_wav_header(written[0])
        samples = np.frombuffer(header["payload"], dtype="<f4").reshape(-1, 6)
        assert np.all(samples[:, :2] == 1.0)
        assert np.all(samples[:, 2:] == 2.0)
        info = json.loads(written[0].with_suffix(".json").read_text(encoding="utf-8"))
        assert info["column_channels"] == [0, 1, 2, 3, 4, 5]
        assert info["devices"] == {"0": [0, 1], "1": [2, 3, 4, 5]}

    def test_the_file_waits_for_the_slower_device(self, tmp_path):
        """Devices arrive at different times; writing early would stagger
        the columns against each other."""
        recorder = self._recorder(tmp_path)
        recorder.start(_Config(), label="wait")
        recorder.feed(data_frame(0, 2, 100, 0))
        recorder.feed(data_frame(1, 4, 40, 0))
        # Only the 40 frames both devices have may be written so far.
        assert recorder._combined.writer.frames_written == 40
        recorder.feed(data_frame(1, 4, 60, 40))
        assert recorder._combined.writer.frames_written == 100
        recorder.stop()

    def test_a_silent_device_does_not_lose_the_recording(self, tmp_path):
        """One stalled device must not take the whole file down with it."""
        recorder = self._recorder(tmp_path)
        recorder.start(_Config(), label="silent")
        recorder.feed(data_frame(0, 2, 100, 0, value=1.0))
        written = recorder.stop()

        header = read_wav_header(written[0])
        samples = np.frombuffer(header["payload"], dtype="<f4").reshape(-1, 6)
        assert np.all(samples[:, :2] == 1.0)
        assert np.all(samples[:, 2:] == 0.0), "absent device must be silence"
        info = json.loads(written[0].with_suffix(".json").read_text(encoding="utf-8"))
        assert info["silent_devices"] == [1], "and it must be named"

    def test_unsynchronised_devices_are_flagged_in_the_sidecar(self, tmp_path):
        """Without resampling the two run on independent clocks, so a single
        file asserts an alignment that does not exist."""
        recorder = self._recorder(tmp_path)
        recorder.start(_Config(resample_active=False), label="drift")
        recorder.feed(data_frame(0, 2, 50, 0))
        recorder.feed(data_frame(1, 4, 50, 0))
        written = recorder.stop()

        info = json.loads(written[0].with_suffix(".json").read_text(encoding="utf-8"))
        assert info["sample_locked"] is False
        assert "warning" in info and "clocks" in info["warning"]

    def test_sample_locked_when_resampling_is_on(self, tmp_path):
        recorder = self._recorder(tmp_path)
        recorder.start(_Config(resample_active=True), label="locked")
        recorder.feed(data_frame(0, 2, 50, 0))
        recorder.feed(data_frame(1, 4, 50, 0))
        written = recorder.stop()

        info = json.loads(written[0].with_suffix(".json").read_text(encoding="utf-8"))
        assert info["sample_locked"] is True
        assert "warning" not in info

    def test_gap_is_filled_with_silence_to_preserve_time(self, tmp_path):
        """The key test — the time axis must not shift after a loss."""
        recorder = self._recorder(tmp_path)
        recorder.start(_Config(), label="gap")

        recorder.feed(data_frame(0, 2, 100, 0, value=1.0))
        # Indices 100..300 went missing entirely (200 samples)
        recorder.feed(data_frame(0, 2, 100, 300, value=2.0))
        recorder.feed(data_frame(1, 4, 400, 0, value=3.0))
        written = recorder.stop()

        header = read_wav_header(written[0])
        frames = header["data_size"] // header["block_align"]
        assert frames == 400, f"silence was not inserted: {frames} != 400"

        samples = np.frombuffer(header["payload"], dtype="<f4").reshape(-1, 6)
        assert np.all(samples[:100, :2] == 1.0)
        assert np.all(samples[100:300, :2] == 0.0), "the lost span is not zero"
        assert np.all(samples[300:, :2] == 2.0), "post-gap data is in the wrong place"
        # The device that lost nothing must be unaffected — that is the point
        # of zero-filling rather than closing the gap up.
        assert np.all(samples[:, 2:] == 3.0)

    def test_gap_is_recorded_in_sidecar(self, tmp_path):
        recorder = self._recorder(tmp_path)
        recorder.start(_Config(), label="gap")
        recorder.feed(data_frame(0, 2, 100, 0))
        recorder.feed(data_frame(0, 2, 100, 300))
        written = recorder.stop()

        info = json.loads(written[0].with_suffix(".json").read_text(encoding="utf-8"))
        assert info["missing_samples"] == 200
        assert info["gaps"][0]["at_sample"] == 100
        assert info["gaps"][0]["missing_samples"] == 200
        assert info["gaps"][0]["seconds"] == pytest.approx(200 / 48000)

    def test_sidecar_carries_calibration(self, tmp_path):
        """A WAV cannot carry units or sensitivity — without them levels are lost."""
        recorder = self._recorder(tmp_path)
        recorder.start(_Config(), label="cal")
        recorder.feed(data_frame(0, 2, 50, 0))
        recorder.feed(data_frame(1, 4, 50, 0))
        written = recorder.stop()

        info = json.loads(written[0].with_suffix(".json").read_text(encoding="utf-8"))
        assert info["units"] == {str(c): "Pa" for c in range(6)}
        assert info["sensitivity_mv_per_unit"] == {str(c): 50.0 for c in range(6)}
        assert info["column_channels"] == [0, 1, 2, 3, 4, 5]
        assert info["sample_rate"] == 48000
        assert "epoch" in info

    def test_pcm_sidecar_records_full_scale_and_clipping(self, tmp_path):
        options = RecordingOptions(
            enabled=True, directory=str(tmp_path), fmt="int16", full_scale=1.0
        )
        recorder = WavRecorder(options)
        recorder.start(_Config(), label="pcm")
        recorder.feed(data_frame(0, 2, 10, 0, value=5.0))  # beyond full scale
        written = recorder.stop()

        info = json.loads(written[0].with_suffix(".json").read_text(encoding="utf-8"))
        assert info["full_scale"] == 1.0
        assert info["clipped_samples"] == 20

    def test_ignores_frames_when_not_recording(self, tmp_path):
        recorder = self._recorder(tmp_path)
        recorder.feed(data_frame(0, 2, 100, 0))  # before start()
        assert not list(tmp_path.glob("*.wav"))

    def test_ignores_non_data_frames(self, tmp_path):
        from pislm import LevelFrame

        recorder = self._recorder(tmp_path)
        recorder.start(_Config(), label="x")
        recorder.feed(LevelFrame(0, np.array([50.0])))
        assert recorder.stop() == []

    def test_out_of_order_frame_is_dropped_not_rewound(self, tmp_path):
        recorder = self._recorder(tmp_path)
        recorder.start(_Config(), label="ooo")
        recorder.feed(data_frame(0, 2, 100, 0))
        recorder.feed(data_frame(0, 2, 100, 100))
        recorder.feed(data_frame(0, 2, 100, 50))  # backwards — must be dropped
        written = recorder.stop()
        header = read_wav_header(written[0])
        assert header["data_size"] // header["block_align"] == 200

    def test_restart_closes_previous_files(self, tmp_path):
        recorder = self._recorder(tmp_path)
        recorder.start(_Config(), label="first")
        recorder.feed(data_frame(0, 2, 100, 0))
        recorder.start(_Config(), label="second")  # restart without an explicit stop
        recorder.feed(data_frame(0, 2, 100, 0))
        recorder.stop()

        files = sorted(tmp_path.glob("*.wav"))
        assert len(files) == 2
        for path in files:
            header = read_wav_header(path)
            assert header["riff_size"] == header["declared_total"], f"{path} header incomplete"

    def test_v3_frames_without_index_still_record(self, tmp_path):
        """A pislm/3 server must still record — only gap detection is lost."""
        recorder = self._recorder(tmp_path)
        recorder.start(_Config(), label="v3")
        recorder.feed(DataFrame(device=0, interleaved=np.zeros(200)))
        written = recorder.stop()
        header = read_wav_header(written[0])
        assert header["data_size"] // header["block_align"] == 100

    def test_a_frame_from_an_unknown_device_is_ignored(self, tmp_path):
        recorder = self._recorder(tmp_path)
        recorder.start(_Config(), label="x")
        recorder.feed(data_frame(9, 2, 100, 0))
        written = recorder.stop()
        # The file exists (the handshake promised two devices) but the stray
        # frame contributed nothing to it.
        assert len(written) == 1
        assert read_wav_header(written[0])["data_size"] == 0


class TestSizeEstimate:
    def test_estimate_matches_actual_file(self, tmp_path):
        seconds, rate, channels = 0.5, 48000, 2
        path = tmp_path / "e.wav"
        with WavWriter(path, channels, rate, "int24") as writer:
            writer.write(np.zeros((int(rate * seconds), channels)))
        predicted = estimate_size(channels, rate, seconds, "int24")
        assert read_wav_header(path)["data_size"] == predicted

    def test_format_size_is_readable(self):
        assert format_size(500) == "500.0 B"
        assert format_size(1536) == "1.5 KB"
        assert "MB" in format_size(5 * 1024**2)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
