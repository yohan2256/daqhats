"""Headless regression tests for raw Fmax and per-stream display freshness."""
import sys
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.live import LiveState
from pislm import BandLevelFrame, LevelFrame, PiSLM, Handshake
from pislm.stream import RawDump
from pislm.standards.impact import raw_band_fmax, fetch_heavy_levels


def test_full_rate_peak_does_not_depend_on_display_phase():
    fs = 51200
    values = []
    for shift in range(0, 2560, 256):
        x = np.zeros(5*fs)
        offset = 3*fs+5120+shift
        x[offset:offset+512] = np.sin(2*np.pi*500*np.arange(512)/fs)
        values.append(raw_band_fmax(x, fs, 2, [500])[500])
    assert max(values)-min(values) < 1e-9
    assert 70 < values[0] < 90


def test_steady_calibrated_tone_absolute_level():
    fs = 51200
    x = np.sin(2*np.pi*1000*np.arange(5*fs)/fs)
    assert raw_band_fmax(x, fs, 2, [1000])[1000] == pytest.approx(90.9691, abs=.01)


@pytest.mark.parametrize('bad', ['short','nan','infinity','nyquist','empty'])
def test_raw_analysis_rejects_invalid_input(bad):
    x = np.zeros(5*8000)
    bands = [500]
    if bad == 'short': x = x[:200]
    if bad == 'nan': x[-3] = np.nan
    if bad == 'infinity': x[-3] = np.inf
    if bad == 'nyquist': bands = [4000]
    if bad == 'empty': bands = []
    with pytest.raises(ValueError):
        raw_band_fmax(x, 8000, 2, bands)


def fake_pi(**kwargs):
    dump = RawDump(1, 0, np.zeros((40000,1)), channels=[0], sample_rate=8000,
                   start_index=10000)
    cfg = SimpleNamespace(buffer_seconds=30, channel_info=lambda ch: SimpleNamespace(calibrated=True))
    return SimpleNamespace(config=cfg, fetch_raw=lambda **kw: {0:dump}, **kwargs)


def test_raw_fetch_records_sample_window():
    pi = fake_pi()
    levels, note = fetch_heavy_levels(pi, 2, [0], [500])
    assert set(levels[0]) == {500}
    assert '[34000, 50000)' in note


@pytest.mark.parametrize('bad', ['uncalibrated','missing','capacity','index'])
def test_raw_fetch_fails_closed(bad):
    pi = fake_pi()
    if bad == 'uncalibrated': pi.config.channel_info = lambda ch: SimpleNamespace(calibrated=False)
    if bad == 'missing': pi.fetch_raw = lambda **kw: {}
    if bad == 'capacity': pi.config.buffer_seconds = 4
    if bad == 'index':
        dumps = pi.fetch_raw()
        dumps[0].start_index = -1
        pi.fetch_raw = lambda **kw: dumps
    with pytest.raises(ValueError): fetch_heavy_levels(pi, 2, [0], [500])


def test_fresh_channels_do_not_keep_stale_channels_alive(monkeypatch):
    now = [10.]
    monkeypatch.setattr('app.live.time.monotonic', lambda: now[0])
    live = LiveState()
    live.band_centers = {0:500}
    live.feed(LevelFrame(0, np.array([80.,60.])))
    live.feed(BandLevelFrame(0,0,np.array([80.,60.])))
    now[0] += .1
    assert live.snapshot_levels()[0] == 80
    assert live.snapshot_spectrum(0)[500] == 80
    now[0] += 1
    live.feed(LevelFrame(1,np.array([50.])))
    assert live.snapshot_levels() == {1:50}
    assert live.snapshot_spectrum(0) == {}


def test_nonfinite_live_values_cannot_poison_bar():
    live = LiveState()
    live.feed(LevelFrame(0,np.array([np.nan,70,np.inf])))
    assert live.snapshot_levels() == {0:70}


def test_overrun_invalidates_raw_even_without_stream_frames():
    pi = PiSLM('localhost')
    pi._absorb({'type':'handshake','protocol':'pislm/4','running':True})
    pi._absorb({'type':'event','event':'overrun','device':0})
    assert pi.measurement_status[0] == 'invalid'
    assert not pi.config.running
    pi._absorb({'type':'event','event':'started','protocol':'pislm/4','running':True})
    assert not pi.acquisition_errors


def test_fractional_device_rate_matches_server_frame_rounding():
    fs = 51200.123
    x = np.zeros(int(5*fs))
    assert 500 in raw_band_fmax(x, fs, 2, [500])


def test_raw_collector_cannot_accept_another_dump_before_response():
    pi = PiSLM('localhost')
    handlers = []
    pi.stream = SimpleNamespace(on_dump=handlers.append, _dump_handlers=handlers)
    wrong = RawDump(90, 0, np.ones((10,1)), channels=[0], sample_rate=10)
    right = RawDump(91, 0, np.zeros((10,1)), channels=[0], sample_rate=10)
    def request(**kwargs):
        handlers[0](right)
        handlers[0](wrong)
        return {'dump_id':91,'devices':[{'device':0}]}
    pi.get_raw = request
    assert pi.fetch_raw(seconds=1, timeout=.01)[0] is right
    assert handlers == []


def test_capture_routes_heavy_to_raw_and_background_to_leq(monkeypatch):
    # Check production GUI-job wiring without importing unavailable OpenGL.
    import ast
    import time
    from pislm.session import ImpactSource
    from pislm.standards import Spectrum, a_weighted_single_number, nominal_center
    path = Path(__file__).resolve().parents[1]/'app/main.py'
    tree = ast.parse(path.read_text())
    method = next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_collect')
    ns = dict(Spectrum=Spectrum, a_weighted_single_number=a_weighted_single_number,
              nominal_center=nominal_center, FILTER_ORDER=6)
    calls = []
    ns['prepare_heavy_capture'] = lambda *args, **kw: calls.append('prepare')
    ns['fetch_heavy_levels'] = lambda *args, **kw: (calls.append('raw') or {0:{500:80}}, 'raw proof')
    exec(compile(ast.Module(body=[method],type_ignores=[]),str(path),'exec'),ns)
    def metrics(**kwargs):
        calls.append('leq')
        return {'channels':{'0':{'bands':[{'center':500,'Leq':40}]}}}
    owner = SimpleNamespace(worker=SimpleNamespace(progress=SimpleNamespace(emit=lambda text: None)),
                            _closing=False, live=LiveState(), session=SimpleNamespace(source=ImpactSource.RUBBER_BALL,fraction=3),
                            pi=SimpleNamespace(get_metrics=metrics,measurement_valid=(True,[])))
    monkeypatch.setattr(time,'sleep',lambda seconds: None)
    heavy = ns['_collect'](owner,2,[0])
    background = ns['_collect'](owner,2,[0],background=True)
    assert calls == ['prepare','raw','leq']
    assert heavy['levels'][0][500] == 80 and heavy['analysis_note'] == 'raw proof'
    assert background['levels'][0][500] == 40
    assert not owner.live.capturing


def test_prepare_waits_for_real_history_on_every_channel(monkeypatch):
    from pislm.standards import impact
    now = [0.0]
    monkeypatch.setattr(impact.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(impact.time, 'sleep', lambda dt: now.__setitem__(0, now[0] + dt))
    calls = []
    pi = fake_pi()
    def fetch(**kwargs):
        calls.append(kwargs['seconds'])
        n = 100 if len(calls) == 1 else int(kwargs['seconds'] * 8000)
        return {0: RawDump(1, 0, np.zeros((26000, 1)), channels=[0], sample_rate=8000, start_index=0),
                1: RawDump(1, 1, np.zeros((n, 1)), channels=[1], sample_rate=8000, start_index=0)}
    pi.fetch_raw = fetch
    impact.prepare_heavy_capture(pi, 1, [0, 1])
    assert calls == [3.25, 3.25]
    assert now[0] == 1


def test_prepare_timeout_and_cancellation_do_not_accept_short_data(monkeypatch):
    from pislm.standards import impact
    now = [0.0]
    monkeypatch.setattr(impact.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(impact.time, 'sleep', lambda dt: now.__setitem__(0, now[0] + dt))
    pi = fake_pi()
    pi.fetch_raw = lambda **kwargs: {}
    with pytest.raises(TimeoutError, match='not ready'):
        impact.prepare_heavy_capture(pi, 1, [0], timeout=2)
    assert now[0] == 2
    with pytest.raises(RuntimeError, match='cancelled'):
        impact.prepare_heavy_capture(pi, 1, [0], cancelled=lambda: True)


def test_prepare_rejects_configuration_before_waiting():
    from pislm.standards.impact import prepare_heavy_capture
    pi = fake_pi()
    pi.config.buffer_seconds = 3
    with pytest.raises(ValueError, match='at least 4'):
        prepare_heavy_capture(pi, 1, [0])


def test_prepare_preserves_uncalibrated_channel_guard():
    from pislm.standards.impact import prepare_heavy_capture
    pi = fake_pi()
    pi.config.channel_info = lambda ch: SimpleNamespace(calibrated=False)
    pi.fetch_raw = lambda **kwargs: pytest.fail('must reject before network request')
    with pytest.raises(ValueError, match='not calibrated'):
        prepare_heavy_capture(pi, 1, [0])
