import unittest
from unittest.mock import patch
from types import SimpleNamespace
import numpy as np
from pislm.frames import ChannelInfo
from pislm.rt_sequence import background, analyse_cycle, summarise, capture_raw


class Dump:
    sample_rate=16000
    start_index=0
    channels=[0]
    def __init__(self,x): self.x=x
    def channel(self,ch): return self.x


def noise_record(rt=.6,seed=1):
    rng=np.random.default_rng(seed); fs=16000
    t=np.arange(fs*3)/fs
    return np.concatenate([rng.normal(size=fs*3),
        rng.normal(size=len(t))*np.exp(-np.log(1000)*t/rt)])+rng.normal(size=fs*6)*1e-4


class SequenceTests(unittest.TestCase):
    def setUp(self):
        self.bg=background({0:Dump(np.random.default_rng(44).normal(size=48000)*1e-4)},[0],[1000],3)

    def cycle(self,seed): return analyse_cycle({0:Dump(noise_record(seed=seed))},[0],[1000],3,'T20',self.bg)

    def test_three_real_cycles_and_sd(self):
        cycles=[self.cycle(i) for i in [1,2,3]]
        e=summarise(cycles,[0],[1000])[0][1000]
        self.assertTrue(e['complete']); self.assertEqual(e['n'],3)
        vals=[c[0][1000]['t60'] for c in cycles]
        self.assertAlmostEqual(e['mean_s'],np.mean(vals)); self.assertAlmostEqual(e['sd_s'],np.std(vals,ddof=1))
        self.assertAlmostEqual(e['mean_s'],.6,delta=.15)
        self.assertFalse(summarise(cycles[:2],[0],[1000])[0][1000]['complete'])

    def test_missing_band_never_silently_passes(self):
        cycles=[self.cycle(i) for i in [1,2,3]]
        cycles[1][0][1000]['t60']=None
        self.assertFalse(summarise(cycles,[0],[1000])[0][1000]['complete'])

    def test_background_headroom(self):
        self.bg['powers'][0][1000]=.1
        self.assertIsNone(self.cycle(1)[0][1000]['t60'])

    def test_rate_change(self):
        self.bg['rates'][0]=48000
        with self.assertRaises(ValueError): self.cycle(1)

    def test_diagnostics_fit_bounds(self):
        entry=self.cycle(1)[0][1000]
        a,b=entry['fit_start_s'],entry['fit_end_s']
        self.assertGreater(b,a); self.assertLess(entry['noise_db'],-35)
        self.assertGreater(len(entry['time_s']),10)
        self.assertEqual(len(entry['time_s']),len(entry['level_db']))

    def test_tail_only_not_a_cycle(self):
        c=analyse_cycle({0:Dump(np.random.default_rng(2).normal(size=48000)*1e-4)},[0],[1000],3,'T20',self.bg)
        self.assertIsNone(c[0][1000]['t60'])

    def fake_pi(self,x):
        info=ChannelInfo(0,0,'sim',0,'Pa',50,1)
        config=SimpleNamespace(running=True,buffer_seconds=30,channel_info=lambda ch:info)
        return SimpleNamespace(config=config,refresh=lambda:None,measurement_valid=(True,[]),
            fetch_raw=lambda **kw:{0:Dump(x)})

    def test_cancel_does_not_fetch(self):
        pi=self.fake_pi(np.ones(48000)); pi.fetch_raw=lambda **kw:self.fail('fetch after cancellation')
        with self.assertRaises(RuntimeError): capture_raw(pi,[0],cancelled=lambda:True)

    def test_short_dump_is_rejected_no_shorter_retry(self):
        pi=self.fake_pi(np.ones(100))
        with patch('pislm.rt_sequence._wait'):
            with self.assertRaises(ValueError): capture_raw(pi,[0])

    def test_integrity_failure(self):
        pi=self.fake_pi(np.ones(48000)); pi.measurement_valid=(False,['gap'])
        with patch('pislm.rt_sequence._wait'):
            with self.assertRaisesRegex(ValueError,'gap'): capture_raw(pi,[0])

    def test_session_preserves_cycle_audit(self):
        import tempfile
        from pathlib import Path
        from pislm.session import Session
        s=Session(); s.reverberation_records.append({'cycles':[self.cycle(1)],'method':'T20'})
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'session.json'; s.save(path); restored=Session.load(path)
        self.assertEqual(restored.reverberation_records[0]['method'],'T20')
        self.assertIn('1000',restored.reverberation_records[0]['cycles'][0]['0'])
