import sys
from pathlib import Path
import math
import numpy as np
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pislm.standards import airborne as a
from pislm.standards.sound_level import levels


def flat(x): return {b:x for b in a.BANDS}


def test_spatial_and_source_averages_are_different():
    assert a.energy_mean([flat(40),flat(60)])[0] == pytest.approx(57.0329,abs=1e-4)
    assert a.transmission_mean([flat(40),flat(60)])[0] == pytest.approx(42.9671,abs=1e-4)


def test_rating_flat_spectrum_and_shift_boundary():
    r=a.rating(flat(50))
    assert (r['value'],r['C'],r['Ctr'])==(50,0,0)
    assert r['deviation_sum']<=32
    assert sum(max(v+1-50,0) for v in r['reference'].values())>32
    assert a.rating(flat(60))['value']==60


def test_room_and_facade_geometry():
    groups={'1':dict(L1=[flat(80)],L2=[flat(40)],B2=[flat(20)])}
    room=a.evaluate(groups,flat(1),method='rooms',volume=50,area=10)
    assert room['spectra']['DnT'][100]==pytest.approx(40+10*math.log10(2))
    assert room['spectra']['R′'][100]==pytest.approx(40+10*math.log10(10/8))
    facade=a.evaluate(groups,flat(1),method='facade_ls')
    assert facade['spectra']['Dls,2m,nT'][100]==pytest.approx(room['spectra']['DnT'][100])
    element=a.evaluate(groups,flat(1),method='element_45',volume=50,area=10)
    assert element['spectra']['R′45°'][100]==pytest.approx(room['spectra']['R′'][100]-1.5)


def test_background_limit_and_no_silent_missing_band():
    groups={'1':dict(L1=[flat(80)],L2=[flat(40)],B2=[flat(35)])}
    r=a.evaluate(groups,flat(.5),method='facade_traffic')
    assert r['lower_bound'] and r['spectra']['Dtr,2m,nT'][100]==pytest.approx(41.3)
    del groups['1']['B2'][0][100]
    with pytest.raises(ValueError,match='Missing bands'): a.evaluate(groups,flat(.5),method='facade_ls')


@pytest.mark.parametrize('bad',[float('nan'),float('inf'),-1,0])
def test_invalid_reverberation(bad):
    with pytest.raises(ValueError): a.evaluate({'1':dict(L1=[flat(80)],L2=[flat(40)],B2=[flat(10)])},flat(bad),method='facade_ls')


def test_full_rate_sound_level_absolute_and_time_weighting():
    fs=12000
    x=np.sin(2*np.pi*1000*np.arange(12*fs)/fs)
    for tw in ('Fast','Slow'):
        r=levels(x,fs,2,time_weighting=tw,bands=[1000])
        assert r['weightings']['Z']['Leq']==pytest.approx(90.9691,abs=.001)
        assert r['weightings']['Z']['Lpeak']==pytest.approx(93.9794,abs=.001)
        assert r['weightings']['Z']['SEL']==pytest.approx(93.9794,abs=.001)
        assert r['bands'][1000]==pytest.approx(90.9691,abs=.02)
        assert len(r['weightings']['Z']['history_db'])==200


def test_peak_at_unsampled_history_position_is_not_lost():
    fs=12000
    x=np.zeros(12*fs); x[11*fs+7]=1
    r=levels(x,fs,2)
    assert r['weightings']['Z']['Lpeak']==pytest.approx(20*math.log10(1/20e-6))
    assert r['weightings']['Z']['Lmax']>max(r['weightings']['Z']['history_db'])


def test_no_short_or_nonfinite_sound_capture():
    with pytest.raises(ValueError): levels(np.ones(12000),12000,1)
    x=np.ones(12*12000);x[-1]=np.nan
    with pytest.raises(ValueError): levels(x,12000,2)
