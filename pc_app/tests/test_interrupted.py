import unittest
import numpy as np
from pislm.standards.interrupted import interrupted_spectrum, switch_off


def recording(on=2., tail=3., rt=.6, seed=12):
    fs=16000
    rng=np.random.default_rng(seed)
    t=np.arange(round(tail*fs))/fs
    decay=rng.normal(size=len(t))*np.exp(-np.log(1000)*t/rt)
    decay+=rng.normal(size=len(t))*1e-4
    return fs,np.concatenate([rng.normal(size=round(on*fs)),decay])


class InterruptedTests(unittest.TestCase):
    def test_source_on_duration(self):
        estimates=[]
        for on in [2.,20.]:
            fs,x=recording(on=on)
            self.assertLess(abs(switch_off(x,fs)/fs-on),.1)
            _,r=interrupted_spectrum(x,fs,[1000])
            self.assertIn(1000,r)
            self.assertAlmostEqual(r[1000].t60,.6,delta=.15)
            estimates.append(r[1000].t60)
        self.assertLess(abs(estimates[0]-estimates[1]),.15)

    def test_invalid_records(self):
        for kind in ['silence','noise','nan','short']:
            with self.subTest(kind=kind):
                fs,x=recording()
                if kind=='silence': x[:]=0
                if kind=='noise': x=np.random.default_rng(1).normal(size=len(x))
                if kind=='nan': x[4]=np.nan
                if kind=='short': x=x[:100]
                with self.assertRaises(ValueError): switch_off(x,fs)

    def test_background_range(self):
        fs,x=recording()
        x+=np.random.default_rng(9).normal(size=len(x))*.2
        with self.assertRaises(ValueError): interrupted_spectrum(x,fs,[1000],method='T30')

    def test_tail_length(self):
        fs,x=recording()
        _,a=interrupted_spectrum(x,fs,[1000])
        extra=np.random.default_rng(77).normal(size=fs*5)*1e-4
        _,b=interrupted_spectrum(np.concatenate([x,extra]),fs,[1000])
        self.assertAlmostEqual(a[1000].t60,b[1000].t60,delta=.01)
