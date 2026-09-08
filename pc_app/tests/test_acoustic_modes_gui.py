import os,sys,json
from pathlib import Path
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.main import MainWindow,build_app
from app.acoustic_modes import AcousticModes
from PySide6 import QtWidgets as W
from pislm.standards.airborne import BANDS


def make():
    app=build_app([]); owner=MainWindow(); dialog=AcousticModes(owner)
    return app,owner,dialog


def enter(dialog,role,value):
    dialog.role.setCurrentText(role)
    for i,b in enumerate(BANDS): dialog.table.setItem(i,1,W.QTableWidgetItem(str(value)))
    dialog._manual()


def test_manual_facade_calculation_and_mode_isolation():
    app,owner,d=make()
    try:
        d.mode.setCurrentIndex(d.mode.findData('facade_ls'))
        enter(d,'L1',80);enter(d,'L2',40);enter(d,'B2',20);enter(d,'T',.5)
        assert len(d.records)==4
        d._evaluate()
        assert d.last_result['ratings']['Dls,2m,nT']['value']==40
        d.mode.setCurrentIndex(d.mode.findData('element_45'))
        assert d.last_result is None and d.record_list.count()==0
        assert not d.table.item(0,2).text()
        d.mode.setCurrentIndex(d.mode.findData('facade_ls'))
        assert d.table.item(0,2).text()=='0.5'
    finally: d.reject();owner.close()


def test_save_load_and_csv(monkeypatch,tmp_path):
    app,owner,d=make()
    try:
        d.mode.setCurrentIndex(d.mode.findData('facade_ls'))
        for role,value in [('L1',80),('L2',40),('B2',20),('T',.5)]: enter(d,role,value)
        target=tmp_path/'session.json'
        monkeypatch.setattr(W.QFileDialog,'getSaveFileName',lambda *a,**k:(str(target),''))
        d._save(); assert len(json.loads(target.read_text())['records'])==4
        d.records=[]
        monkeypatch.setattr(W.QFileDialog,'getOpenFileName',lambda *a,**k:(str(target),''))
        d._load();d._evaluate();assert d.last_result is not None
        csvfile=tmp_path/'result.csv'
        monkeypatch.setattr(W.QFileDialog,'getSaveFileName',lambda *a,**k:(str(csvfile),''))
        d._csv();assert 'Dls,2m,nT,w' in csvfile.read_text(encoding='utf-8-sig')
    finally: d.reject();owner.close()


def test_missing_data_and_duplicate_positions_block_evaluation():
    app,owner,d=make()
    try:
        d.mode.setCurrentIndex(d.mode.findData('facade_ls'))
        enter(d,'L1',80);d._evaluate();assert d.last_result is None
        enter(d,'L1',80);enter(d,'L2',40);enter(d,'B2',20);enter(d,'T',.5)
        d._evaluate();assert d.last_result is None and 'Duplicate' in d.result.toPlainText()
    finally:d.reject();owner.close()


def test_simulator_capture_reaches_general_panel():
    import socket,time
    import pislm_sim
    from pislm import PiSLM
    def port():
        with socket.socket() as s: s.bind(('127.0.0.1',0));return s.getsockname()[1]
    pislm_sim.reset_state();pislm_sim.OPTS.fragment=False;pislm_sim.OPTS.shuffle=False
    pislm_sim.OPTS.drop=0;pislm_sim.OPTS.overload=0
    cp,sp=port(),port();ctl,stm=pislm_sim.serve(cp,sp)
    app,owner,d=make();pi=PiSLM('127.0.0.1',cp,sp,stream_queue_size=0)
    try:
        pi.connect();pi.start();owner.pi=pi;d.pi=pi
        d.seconds.setValue(1);d.channels.setText('1,3');d._capture()
        deadline=time.monotonic()+25
        while d.busy and time.monotonic()<deadline:
            app.processEvents();time.sleep(.01)
        assert not d.busy,d.notice.text()
        assert len(d.records)==2,d.notice.text()
        assert 'Leq' in d.result.toPlainText()
        assert d.history.values
        d.mode.setCurrentIndex(d.mode.findData('facade_traffic'))
        d.channels.setText('3,4');d.outside.setValue(3);d._capture()
        deadline=time.monotonic()+10
        while d.busy and time.monotonic()<deadline:app.processEvents();time.sleep(.01)
        pairs=[r for r in d.records if r['mode']=='facade_traffic']
        assert {r['role'] for r in pairs}=={'L1','L2'},d.notice.text()
        assert len({(r['data']['start_index'],r['data']['end_index']) for r in pairs})==1
    finally:
        d.cancel.set()
        deadline=time.monotonic()+5
        while d.busy and time.monotonic()<deadline:app.processEvents();time.sleep(.01)
        d.reject();pi.stop();pi.close();owner.pi=None;owner.close()
        pislm_sim.stop_scan();ctl.shutdown();stm.shutdown()
