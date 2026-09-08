"""Hardware-free regression: python -m pytest tests/test_recovery.py."""
import importlib.util
import sys
from pathlib import Path
from queue import Queue
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dsp_pool import _WorkerState, DspPool
from slm import RecoveryGate


def config():
    return dict(channels=[0], rate=51200., level_enabled=True,
                band_output='level', refs={0: 20e-6}, time_weighting='Fast',
                freq_weighting='Z', highpass_hz=20, highpass_order=2,
                level_rate=20, bands=dict(f_min=50, f_max=1000, fraction=3,
                                         order=6, margin=1))


def feed(state, start, n_blocks, gap=0):
    out = []
    for i in range(n_blocks):
        t = (np.arange(1024) + start + i * 1024) / state.rate
        x = np.sin(2*np.pi*1000*t + .7)
        out.extend(state.process(x[:, None], gap if i == 0 else 0))
    return out


def test_gap_does_not_turn_a_tone_into_low_band_impacts():
    state = _WorkerState(config())
    feed(state, 0, 200)
    out = feed(state, 201*1024, 200, gap=1024)
    assert any(kind == 'band_level_gap' for kind, _, _ in out)
    maxima = {}
    for kind, ( *key, ), payload in out:
        if kind == 'band_level':
            maxima[key[0]] = max(maxima.get(key[0], -np.inf),
                                 np.frombuffer(payload, '<f8').max())
    assert len(maxima) == len(state.bank.bands), 'all bands must recover'
    for band in state.bank.bands:
        if band['center'] <= 250:
            # Before the fix a 50 Hz band jumped to ~36 dB on a pure 1 kHz tone.
            assert maxima[band['index']] < 0
        if band['center'] == 1000:
            assert maxima[band['index']] == pytest.approx(90.97, abs=.1)


def test_warmup_keeps_exact_output_grid_across_blocks_and_gap():
    state = _WorkerState(config())
    out = feed(state, 0, 7) + feed(state, 9*1024, 250, gap=2*1024)
    counts = {}
    for kind, key, payload in out:
        base = kind.removesuffix('_gap')
        n = payload if kind.endswith('_gap') else len(payload)//8
        counts[(base, key)] = counts.get((base, key), 0) + n
    total = 259*1024
    assert counts[('level', (0,))] == (total-1)//2560+1
    for band in state.bank.bands:
        decimated = (total-1)//band['decimation']+1
        step = state.band_level[(band['index'], 0)]._step
        assert counts[('band_level', (band['index'], 0))] == (decimated-1)//step+1


def test_truncated_tail_gap_belongs_to_following_task():
    pool = DspPool.__new__(DspPool)
    worker = dict(spec=dict(device=0, columns=[0]), free=[1, 0],
                  dropped=0, dropped_frames=3, per_slot=4, n_cols=1,
                  flat=np.zeros(8), task_q=Queue(), pending=0)
    pool.workers = [worker]
    pool.submit(0, np.arange(6.)[:, None])
    assert worker['task_q'].get() == (0, 4, 3)
    assert worker['dropped_frames'] == 2
    pool.submit(0, np.arange(2.)[:, None])
    assert worker['task_q'].get() == (1, 2, 2)
    assert worker['dropped_frames'] == 0


def test_raw_snapshot_index_survives_concurrent_append(monkeypatch):
    spec = importlib.util.spec_from_file_location('server_main', ROOT/'pislm.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ring = mod.RawRingBuffer(1, 2, 10)
    ring.append(np.arange(12.))
    ring.append(np.arange(12., 24.))
    concatenate = np.concatenate
    def append_during_copy(arrays):
        ring.append(np.arange(24., 32.))
        return concatenate(arrays)
    monkeypatch.setattr(np, 'concatenate', append_during_copy)
    data, start = ring.snapshot_recent(1, 10)
    np.testing.assert_array_equal(data, np.arange(4., 24.))
    assert start == 2


def test_inline_startup_uses_recovery_gate():
    spec = importlib.util.spec_from_file_location('server_inline', ROOT/'pislm.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ctl = mod.Controller.__new__(mod.Controller)
    ctl._recovery_gates = {}
    ctl.time_weighting = 'Fast'
    from slm import ExpLevel
    detector = ExpLevel(1000, (.125,.125), 20)
    result, skipped = ctl._settled_levels(('level',0), np.ones(2), None, 1000,
                                           (1000, detector))
    assert result.size == 0 and skipped == 2
