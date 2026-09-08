import sys
from pathlib import Path
import numpy as np
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pislm.session import Session, Measurement, ImpactSource
from pislm.standards import Spectrum, InverseACurve
from pislm.standards.korea import rate_with_curve
from pislm.standards.impact import raw_band_fmax

BANDS=(63,125,250,500)

def session():
    s=Session(source=ImpactSource.BANG,source_positions=1,channels=[0])
    s.add(Measurement(1,0,levels={63:83,125:73,250:66,500:60},fraction=1))
    return s

def test_legacy_curve_budget_and_anchor():
    curve=InverseACurve.legacy_heavy()
    result=rate_with_curve(Spectrum.from_mapping(curve.values,fraction=1),curve)
    assert result.value == 58
    assert result.deviation_sum == 8
    assert result.limit == 8
    assert '/8 dB' in result.summary()
    assert np.maximum(0,result.measured.array()-(result.reference.array()-1)).sum()>8

@pytest.mark.parametrize('excess,expected',[(8,60),(8.1,61),(9,61)])
def test_single_band_controls_shift(excess,expected):
    curve=InverseACurve.legacy_heavy()
    levels={63:83+excess,125:0,250:0,500:0}
    assert rate_with_curve(Spectrum.from_mapping(levels,fraction=1),curve).value==expected

def test_bang_profile_is_octave_inverse_a_not_new_law():
    s=session()
    assert s.bands==BANDS and s.fraction==1
    r=s.evaluate(curve=InverseACurve.generated())
    assert r.legacy and r.inverse_a.value==58
    assert r.post_verification is None and r.passes is None
    assert "L'i,Fmax,AW" in r.summary()
    assert 'PASS' not in r.summary()

def test_legacy_data_is_not_silently_reinterpreted():
    s=session()
    s.measurements[0].fraction=3
    result=s.evaluate()
    assert result.inverse_a is None
    assert 'recapture' in ' '.join(result.warnings)

def test_octave_missing_band_rejected():
    s=session()
    del s.measurements[0].levels[125]
    assert s.evaluate().inverse_a is None

def test_save_load_preserves_bandwidth_and_legacy_source(tmp_path):
    s=session()
    loaded=Session.load(s.save(tmp_path/'legacy.json'))
    assert loaded.source is ImpactSource.BANG
    assert loaded.measurements[0].fraction==1
    assert loaded.evaluate().inverse_a.value==58

def test_legacy_averages_source_levels_arithmetically():
    s=Session(source=ImpactSource.BANG,source_positions=2,channels=[0,1])
    for source,ch,value in [(1,0,50),(1,1,60),(2,0,70),(2,1,70)]:
        s.add(Measurement(source,ch,{b:value for b in BANDS},fraction=1))
    expected=(10*np.log10((10**5+10**6)/2)+70)/2
    assert s.averaged_spectrum().as_dict()[63]==pytest.approx(expected)

def test_background_thirds_combine_as_leq_not_selected_centres():
    from pislm.standards.korea import HEAVY_THIRD_OCTAVE_BANDS
    s=session()
    s.background={b:30 for b in HEAVY_THIRD_OCTAVE_BANDS}
    assert s.background_spectrum().as_dict()[63]==pytest.approx(30+10*np.log10(3))

def test_octave_fmax_runs_directly_on_raw():
    fs=8000
    x=np.sin(2*np.pi*125*np.arange(fs*5)/fs)
    levels=raw_band_fmax(x,fs,2,BANDS,fraction=1)
    assert tuple(levels)==BANDS
    assert levels[125]==pytest.approx(90.97,abs=.06)

@pytest.mark.parametrize('value',[np.nan,np.inf])
def test_nonfinite_curve_rejected(value):
    curve=InverseACurve.legacy_heavy()
    curve.values[63]=value
    with pytest.raises(ValueError):
        rate_with_curve(Spectrum.from_mapping({b:50 for b in BANDS},fraction=1),curve)
