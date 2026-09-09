"""GUI state gates; runnable with the project's PySide6 dependency installed."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import unittest
try:
    from PySide6 import QtWidgets
    from app.rt_sequence import RTSequence
except ImportError:
    QtWidgets=None


@unittest.skipIf(QtWidgets is None,'PySide6 is not installed')
class SequenceGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self): self.dialog=RTSequence(None,None,[0],[1000],3)
    def tearDown(self): self.dialog.worker.stop(); self.dialog.close()
    def cycle(self):
        return {0:{1000:dict(t60=.6,correlation=.97,range_db=45,curvature_percent=None,warnings=['review curve'])}}
    def test_apply_requires_three_cycles_and_review(self):
        d=self.dialog
        d._done('set',({'powers':{0:{1000:1e-8}},'rates':{0:48000}},[]))
        self.assertFalse(d.config.isEnabled())
        for _ in range(2): d._done('cycle',self.cycle())
        d.review.setChecked(True); self.assertFalse(d.apply.isEnabled())
        d._done('cycle',self.cycle()); d.review.setChecked(True)
        self.assertTrue(d.apply.isEnabled())
        d._apply(); self.assertAlmostEqual(d.accepted_results[1000].t60,.6)
    def test_exclude_keeps_audit_and_blocks_incomplete_average(self):
        d=self.dialog
        for _ in range(3): d._done('cycle',self.cycle())
        d.cycle.setCurrentIndex(1); d._remove()
        self.assertEqual(len(d.cycles),2); self.assertEqual(len(d.report()['excluded']),1)
        self.assertFalse(d.apply.isEnabled())
