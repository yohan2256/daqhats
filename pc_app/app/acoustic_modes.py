"""Separate acoustic workflows sharing the existing acquisition connection."""
import csv
import json
import threading
from pathlib import Path
from datetime import datetime, timezone
import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets as W
from app.worker import CommandWorker
from pislm.acoustic_capture import capture
from pislm.standards import airborne
from pislm.standards.bands import THIRD_OCTAVE_NOMINAL, OCTAVE_NOMINAL
from pislm.recorder import write_dump_wav

MODES = [('General sound level', 'SLM'), ('Room-to-room airborne', 'rooms'),
         ('Whole facade — loudspeaker, 2 m', 'facade_ls'),
         ('Whole facade — road traffic, 2 m', 'facade_traffic'),
         ('Window / element — 45° loudspeaker, surface mic', 'element_45')]


class History(W.QWidget):
    def __init__(self):
        super().__init__(); self.values=[]; self.setMinimumHeight(100)
    def paintEvent(self,event):
        p=QtGui.QPainter(self); p.fillRect(self.rect(),QtGui.QColor('#1c1e22'))
        p.setPen(QtGui.QColor('white')); p.drawText(8,18,'A-weighted time history (first selected channel)')
        if not self.values: return
        a=np.array(self.values); lo,hi=float(a.min())-1,float(a.max())+1
        stride=max(1,len(a)//max(1,self.width()))
        pts=[QtCore.QPointF(8+i/(max(1,len(a)-1))*(self.width()-16),
             30+(hi-a[i])/(hi-lo)*(self.height()-40)) for i in range(0,len(a),stride)]
        p.setPen(QtGui.QColor('#56b4e9')); p.drawPolyline(QtGui.QPolygonF(pts))


class AcousticModes(W.QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner=owner; self.pi=owner.pi; self.records=[]; self.last_result=None
        self._active_mode=None; self._t_by_mode={}
        self.last_dumps=None; self.busy=False; self.cancel=threading.Event()
        self.setWindowTitle('Sound level / airborne / facade'); self.resize(1150,850)
        self.setWindowModality(QtCore.Qt.ApplicationModal)
        self.worker=CommandWorker(self)
        self.worker.finished_job.connect(self._done); self.worker.failed_job.connect(self._failed)
        self.worker.progress.connect(self._progress); self.worker.start()
        layout=W.QVBoxLayout(self)
        self.controls=W.QWidget(); form=W.QGridLayout(self.controls)
        self.mode=W.QComboBox()
        for name,key in MODES: self.mode.addItem(name,key)
        self.role=W.QComboBox()
        for name in ('SLM','L1','L2','B2','PAIR','T'): self.role.addItem(name)
        self.channels=W.QLineEdit('1,3'); self.channels.setToolTip('Display channel numbers, e.g. 1,3 (wire 0,2)')
        self.outside=W.QSpinBox(); self.outside.setRange(1,64); self.outside.setValue(1)
        self.source=W.QSpinBox(); self.source.setRange(1,99)
        self.position=W.QLineEdit('P1')
        self.seconds=W.QDoubleSpinBox(); self.seconds.setRange(1,240); self.seconds.setValue(10)
        self.time_weight=W.QComboBox(); self.time_weight.addItems(['Fast','Slow'])
        self.fraction=W.QComboBox(); self.fraction.addItems(['1/3 octave','1/1 octave'])
        self.volume=W.QDoubleSpinBox(); self.volume.setRange(0,100000); self.volume.setSuffix(' m³')
        self.area=W.QDoubleSpinBox(); self.area.setRange(0,100000); self.area.setSuffix(' m²')
        self.t0=W.QDoubleSpinBox(); self.t0.setRange(.01,10); self.t0.setValue(.5); self.t0.setSuffix(' s')
        fields=[('Mode',self.mode),('Phase',self.role),('Channels',self.channels),
                ('Outside / L1 channel (PAIR)',self.outside),('Source / event ID',self.source),
                ('Microphone position',self.position),('Duration (s)',self.seconds),
                ('Time weighting',self.time_weight),('General band resolution',self.fraction),
                ('Receiving volume',self.volume),('Test area',self.area),('Reference T0',self.t0)]
        for i,(name,widget) in enumerate(fields):
            row,col=divmod(i,3); form.addWidget(W.QLabel(name),row,col*2); form.addWidget(widget,row,col*2+1)
        layout.addWidget(self.controls)
        self.guide=W.QLabel(); self.guide.setWordWrap(True); layout.addWidget(self.guide)
        self.notice=W.QLabel('Connect and start acquisition in the main window.'); self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        actions=W.QHBoxLayout(); layout.addLayout(actions)
        self.start=W.QPushButton('Capture'); self.start.clicked.connect(self._capture); actions.addWidget(self.start)
        self.stop=W.QPushButton('Cancel'); self.stop.clicked.connect(self.cancel.set); actions.addWidget(self.stop)
        self.manual=W.QPushButton('Add entered spectrum'); self.manual.clicked.connect(self._manual); actions.addWidget(self.manual)
        self.evaluate_btn=W.QPushButton('Calculate insulation'); self.evaluate_btn.clicked.connect(self._evaluate); actions.addWidget(self.evaluate_btn)
        self.remove=W.QPushButton('Remove selected record'); self.remove.clicked.connect(self._remove); actions.addWidget(self.remove)
        for title,slot in [('Save session',self._save),('Load session',self._load),('Export CSV',self._csv),('Save last WAV',self._wav)]:
            btn=W.QPushButton(title); btn.clicked.connect(slot); actions.addWidget(btn)
        split=W.QSplitter(); layout.addWidget(split,1)
        self.table=W.QTableWidget(len(airborne.BANDS),3)
        self.table.setHorizontalHeaderLabels(['Hz','Entered Leq (dB) / T (s)','Receiving T60 (s)'])
        for i,b in enumerate(airborne.BANDS):
            cell=W.QTableWidgetItem(str(b)); cell.setFlags(cell.flags() & ~QtCore.Qt.ItemIsEditable); self.table.setItem(i,0,cell)
        self.table.horizontalHeader().setSectionResizeMode(W.QHeaderView.Stretch); split.addWidget(self.table)
        right=W.QWidget(); rl=W.QVBoxLayout(right); split.addWidget(right)
        self.record_list=W.QListWidget(); self.record_list.itemClicked.connect(self._inspect); rl.addWidget(self.record_list)
        self.result=W.QPlainTextEdit(); self.result.setReadOnly(True); rl.addWidget(self.result,2)
        self.history=History(); layout.addWidget(self.history)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.table.itemChanged.connect(self._invalidate)
        for field in (self.volume,self.area,self.t0): field.valueChanged.connect(self._invalidate)
        self._mode_changed()

    def _invalidate(self,*args):
        self.last_result=None
        if hasattr(self,'result'): self.result.clear()

    def _mode_changed(self):
        key=self.mode.currentData()
        if self._active_mode is not None:
            self._t_by_mode[self._active_mode]=[self.table.item(i,2).text() if self.table.item(i,2) else '' for i in range(len(airborne.BANDS))]
        self._active_mode=key
        for i,v in enumerate(self._t_by_mode.get(key,['']*len(airborne.BANDS))):
            self.table.setItem(i,2,W.QTableWidgetItem(v))
        self._invalidate(); self.last_dumps=None
        self.history.values=[]; self.history.update()
        general=key=='SLM'
        self.table.setVisible(not general)
        self.manual.setEnabled(not general); self.evaluate_btn.setEnabled(not general)
        for widget in (self.volume,self.area,self.t0,self.source,self.position,self.role): widget.setEnabled(not general)
        self.fraction.setEnabled(general); self.time_weight.setEnabled(general)
        self.role.setCurrentText('SLM' if general else ('PAIR' if key=='facade_traffic' else 'L1'))
        self.guide.setText({
            'SLM':'Calibrated A/C/Z levels, full-rate Fast/Slow maxima and peak; 10 ms LN/history. General spectrum 31.5–8000 Hz.',
            'rooms':'L1: source room; L2: receiving room; B2: source OFF; T: SET + 3 gated-noise cycles (XL2 procedure). Keep each source ID separate. Use multiple spatial positions.',
            'facade_ls':'Whole facade: L1 microphone 2 m outside facade; L2/B2/T indoors. External loudspeaker must remain stable between phases.',
            'facade_traffic':'Road traffic: PAIR captures the outside 2 m reference and indoor channels in the SAME acquisition device/window. Take B2 separately with the test source absent.',
            'element_45':'Window/element: L1 is a SURFACE microphone; loudspeaker incidence 45°. Enter specimen area, receiving volume and T. R′45° includes flanking transmission; it is not laboratory Rw.'}[key])
        self._render_records()

    def _settings(self):
        channels=[int(s.strip())-1 for s in self.channels.text().split(',') if s.strip()]
        if not channels or min(channels)<0 or len(set(channels))!=len(channels): raise ValueError('Select distinct channels numbered from 1')
        return dict(mode=self.mode.currentData(),role=self.role.currentText(),channels=channels,
                    source=str(self.source.value()),position=self.position.text().strip(),seconds=self.seconds.value(),
                    outside=self.outside.value()-1,time_weighting=self.time_weight.currentText(),
                    fraction=3 if self.fraction.currentIndex()==0 else 1)

    def _capture(self):
        if self.busy: return
        try:
            settings=self._settings()
            if self.pi is None: raise ValueError('Connect to the Pi first')
            if settings['mode']=='SLM': settings['role']='SLM'
            elif settings['role']=='SLM': raise ValueError('Choose L1, L2, B2, PAIR or T')
            if settings['mode']=='facade_traffic' and settings['role'] in ('L1','L2'):
                raise ValueError('Use simultaneous PAIR for road traffic')
            if settings['role']=='PAIR':
                if settings['outside'] not in settings['channels'] or len(settings['channels'])<2:
                    raise ValueError('PAIR needs the outside/L1 channel and at least one receiving channel')
                if len({self.pi.config.device_of(ch) for ch in settings['channels']}) != 1:
                    raise ValueError('PAIR must use one acquisition device to guarantee a common sample window')
            if not settings['position']: raise ValueError('Enter a microphone position')
        except Exception as exc: self.notice.setText(str(exc)); return
        if settings['role']=='T':
            from app.rt_sequence import RTSequence
            dialog=RTSequence(self,self.pi,settings['channels'],airborne.BANDS,3)
            if dialog.exec() and dialog.accepted_results is not None:
                report=dialog.report()
                for ch,entries in report['summary'].items():
                    self.records.append(dict(settings,channel=ch,position=f"{settings['position']}/Ch{ch+1}",
                        origin='rt_sequence',captured_at=datetime.now(timezone.utc).isoformat(),
                        data=dict(bands={b:e['mean_s'] for b,e in entries.items()},rt_sequence=report)))
                self.last_dumps=None; self._invalidate(); self._render_records(); self._update_t()
                self.notice.setText('Repeated RT stored with cycle diagnostics and comparison data')
            return
        self.busy=True; self.cancel.clear(); self.controls.setEnabled(False); self.start.setEnabled(False)
        def run():
            frac=settings['fraction'] if settings['mode']=='SLM' else 3
            bands=tuple(b for b in (THIRD_OCTAVE_NOMINAL if frac==3 else OCTAVE_NOMINAL) if 31.5<=b<=8000) if settings['mode']=='SLM' else airborne.BANDS
            data,dumps=capture(self.pi,settings['seconds'],settings['channels'],role=settings['role'],
                time_weighting=settings['time_weighting'],fraction=frac,bands=bands,
                cancelled=self.cancel.is_set,progress=self.worker.progress.emit)
            return settings,data,dumps
        self.worker.submit('acoustics',run)

    def _progress(self,text): self.notice.setText(text)
    def _idle(self):
        self.busy=False; self.controls.setEnabled(True); self.start.setEnabled(True)
    def _failed(self,name,message,detail): self._idle(); self.notice.setText(message)

    def _done(self,name,value):
        self._idle()
        if self.cancel.is_set(): self.notice.setText('Cancelled — no result stored'); return
        settings,data,dumps=value; self.last_dumps=(dumps,settings['channels'])
        batch=[]
        for ch,entry in data.items():
            role=settings['role']
            if role=='PAIR': role='L1' if ch==settings['outside'] else 'L2'
            batch.append(dict(settings,role=role,channel=ch,position=f"{settings['position']}/Ch{ch+1}",
                              data=entry,origin='raw',captured_at=datetime.now(timezone.utc).isoformat()))
        self.records.extend(batch); self._invalidate(); self._render_records()
        if settings['mode']=='SLM':
            lines=[]
            for ch,entry in data.items():
                for w,m in entry['weightings'].items():
                    lines.append(f"Ch{ch+1} {w}/{entry['time_weighting']}: Leq {m['Leq']:.1f}, max {m['Lmax']:.1f}, min {m['Lmin']:.1f}, peak {m['Lpeak']:.1f}, SEL {m['SEL']:.1f} dB; {m['LN']}")
            lines += ['Z-weighted band Leq:'] + [f'Ch{ch+1}: ' + ', '.join(f'{b:g} Hz={v:.1f}' for b,v in entry['bands'].items()) for ch,entry in data.items()]
            self.result.setPlainText('\n'.join(lines))
            self.history.values=next(iter(data.values()))['weightings']['A']['history_db']; self.history.update()
        elif settings['role']=='T':
            self._update_t()
        self.notice.setText(f"Stored {len(batch)} channel record(s); raw snapshot window documented in session")

    def _entered(self,column):
        return {b:float(self.table.item(i,column).text()) for i,b in enumerate(airborne.BANDS)}

    def _manual(self):
        if self.busy: return
        try:
            s=self._settings()
            if s['mode']=='SLM' or s['role'] not in ('L1','L2','B2','T'): raise ValueError('Manual entry supports L1/L2/B2/T spectra')
            if s['mode']=='facade_traffic' and s['role'] in ('L1','L2'): raise ValueError('Traffic reference and receive data require a paired raw capture')
            values=self._entered(1); airborne.vector(values)
            if s['role']=='T' and min(values.values())<=0: raise ValueError('T must be positive')
            if not s['position']: raise ValueError('Enter a position')
            self.records.append(dict(s,channel=None,data={'bands':values},origin='manual',captured_at=datetime.now(timezone.utc).isoformat()))
            self._invalidate(); self._render_records()
            if s['role']=='T': self._update_t()
        except Exception as exc: self.notice.setText(f'Entry rejected: {exc}')

    def _update_t(self):
        rows=[r['data']['bands'] for r in self.records if r['mode']==self.mode.currentData() and r['role']=='T']
        if not rows:
            for i in range(len(airborne.BANDS)): self.table.setItem(i,2,W.QTableWidgetItem(""))
        if rows:
            avg=np.mean([airborne.vector(r) for r in rows],axis=0)
            for i,v in enumerate(avg): self.table.setItem(i,2,W.QTableWidgetItem(f'{v:.6g}'))

    def _render_records(self):
        self.record_list.clear()
        for i,r in enumerate(self.records):
            if r['mode']!=self.mode.currentData(): continue
            item=W.QListWidgetItem(f"Source {r['source']} · {r['role']} · {r['position']} · {r['origin']}")
            item.setData(QtCore.Qt.UserRole,i); self.record_list.addItem(item)

    def _inspect(self,item):
        r=self.records[item.data(QtCore.Qt.UserRole)]
        data=r['data']
        lines=[f"Source {r['source']} · {r['role']} · {r['position']} · {r['origin']}"]
        lines += [f"{b} Hz: {v:.3f} {'s' if r['role']=='T' else 'dB'}" for b,v in data.get('bands',{}).items()]
        for w,m in data.get('weightings',{}).items():
            lines.append(f"{w}/{data['time_weighting']}: Leq {m['Leq']:.1f}, Lmax {m['Lmax']:.1f}, Lpeak {m['Lpeak']:.1f} dB")
        self.result.setPlainText('\n'.join(lines))
        self.history.values=data.get('weightings',{}).get('A',{}).get('history_db',[]); self.history.update()

    def _remove(self):
        if self.busy: return
        item=self.record_list.currentItem()
        if item: del self.records[item.data(QtCore.Qt.UserRole)]; self._invalidate(); self._render_records(); self._update_t()

    def _evaluate(self):
        if self.busy: return
        try:
            groups={}; seen=set()
            for r in self.records:
                if r['mode']!=self.mode.currentData() or r['role'] not in ('L1','L2','B2'): continue
                key=(r['source'],r['role'],r['position'])
                if key in seen: raise ValueError('Duplicate position: remove the old record or use a new position ID')
                seen.add(key)
                groups.setdefault(r['source'],{}).setdefault(r['role'],[]).append(r['data']['bands'])
            if self.mode.currentData()=='facade_traffic':
                for source in groups:
                    pair=[r for r in self.records if r['mode']=='facade_traffic' and r['source']==source and r['role'] in ('L1','L2')]
                    if sum(r['role']=='L1' for r in pair)!=1 or any(r['origin']!='raw' for r in pair):
                        raise ValueError('Use a separate event ID for each simultaneous traffic pair')
                    windows={(r['data']['start_index'],r['data']['end_index'],r['data']['sample_rate']) for r in pair}
                    if len(windows)!=1: raise ValueError('Traffic pair sample windows do not match')
            self.last_result=airborne.evaluate(groups,self._entered(2),method=self.mode.currentData(),
                                volume=self.volume.value(),area=self.area.value(),t0=self.t0.value())
            r=self.last_result
            lines=['LOWER-BOUND RESULT — background noise limits the measurement' if r['lower_bound'] else 'Calculated result — field geometry/sampling must be verified']
            for name,entry in r['ratings'].items():
                lines.append(f"{name},w = {entry['value']} ({entry['C']}; {entry['Ctr']}) dB")
            lines += r['notes']
            lines += ['Hz  ' + '  '.join(r['spectra'])]
            for b in airborne.BANDS: lines.append(f"{b:g}  " + '  '.join(f"{v[b]:.1f}" for v in r['spectra'].values()))
            self.result.setPlainText('\n'.join(lines))
        except Exception as exc: self.last_result=None; self.result.setPlainText(f'Cannot evaluate: {exc}')

    def _save(self):
        if self.busy: return
        path,_=W.QFileDialog.getSaveFileName(self,'Save acoustic session','','JSON (*.json)')
        if not path: return
        try:
            t=[self.table.item(i,2).text() if self.table.item(i,2) else '' for i in range(len(airborne.BANDS))]
            self._t_by_mode[self.mode.currentData()]=t
            payload=dict(schema='pislm-acoustics/1',records=self.records,mode=self.mode.currentData(),
                         volume=self.volume.value(),area=self.area.value(),t0=self.t0.value(),T=t,T_by_mode=self._t_by_mode,result=self.last_result)
            target=Path(path); tmp=target.with_suffix(target.suffix+'.tmp')
            tmp.write_text(json.dumps(payload,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8'); tmp.replace(target)
            self.notice.setText(f'Saved {path}')
        except Exception as exc: self.notice.setText(str(exc))

    def _load(self):
        if self.busy: return
        path,_=W.QFileDialog.getOpenFileName(self,'Load acoustic session','','JSON (*.json)')
        if not path: return
        try:
            payload=json.loads(Path(path).read_text(encoding='utf-8'))
            if payload.get('schema')!='pislm-acoustics/1': raise ValueError('Unknown session schema')
            records=payload['records']
            if not isinstance(records,list): raise ValueError('Invalid records')
            for r in records:
                if r['mode'] not in dict((key,name) for name,key in MODES): raise ValueError('Unknown mode')
                if r['role'] not in ('SLM','L1','L2','B2','T'): raise ValueError('Unknown phase')
                if r['role']!='SLM': airborne.vector(r['data']['bands'])
                for key in ('position','source','origin'): str(r[key])
            mode=payload['mode']
            if mode not in dict((key,name) for name,key in MODES): raise ValueError('Unknown mode')
            geometry=[float(payload[k]) for k in ('volume','area','t0')]
            if not all(np.isfinite(x) and x>=0 for x in geometry) or geometry[2]<=0: raise ValueError('Invalid geometry/reference time')
            tmap=payload.get('T_by_mode',{mode:payload['T']})
            for vals in tmap.values():
                if len(vals)!=len(airborne.BANDS): raise ValueError('Invalid T band count')
                for v in vals:
                    if v and (not np.isfinite(float(v)) or float(v)<=0): raise ValueError('Invalid T value')
            self.records=records; self._active_mode=None; self._t_by_mode=tmap
            self.mode.setCurrentIndex(self.mode.findData(mode)); self._mode_changed()
            self.volume.setValue(geometry[0]); self.area.setValue(geometry[1]); self.t0.setValue(geometry[2])
            self.last_dumps=None; self.history.values=[]; self.history.update()
            self._invalidate(); self._render_records(); self.notice.setText('Loaded — calculate to refresh results')
        except Exception as exc: self.notice.setText(f'Load rejected: {exc}')

    def _csv(self):
        if self.busy: return
        path,_=W.QFileDialog.getSaveFileName(self,'Export records','','CSV (*.csv)')
        if not path: return
        try:
            with open(path,'w',newline='',encoding='utf-8-sig') as f:
                writer=csv.writer(f); writer.writerow(['mode','source','phase','position','channel','quantity','frequency_or_time','value'])
                for r in self.records:
                    prefix=[r['mode'],r['source'],r['role'],r['position'],r.get('channel')]
                    for b,v in r['data'].get('bands',{}).items(): writer.writerow(prefix+['T60' if r['role']=='T' else 'Z_Leq',b,v])
                    for w,m in r['data'].get('weightings',{}).items():
                        for key in ('Leq','Lmax','Lmin','Lpeak','SEL'): writer.writerow(prefix+[w+'_'+key,'',m[key]])
                        for i,v in enumerate(m['history_db']): writer.writerow(prefix+[w+'_history',i*m['history_step_seconds'],v])
                if self.last_result:
                    writer.writerow(['result_status', 'lower_bound' if self.last_result['lower_bound'] else 'calculated_field_verification_required'])
                    for name,rating in self.last_result['ratings'].items():
                        writer.writerow([name+',w',rating['value'],'C',rating['C'],'Ctr',rating['Ctr']])
                    for name,values in self.last_result['spectra'].items():
                        for b,v in values.items(): writer.writerow([self.mode.currentData(),'all','result','', '',name,b,v])
            self.notice.setText(f'Exported {path}')
        except Exception as exc: self.notice.setText(str(exc))

    def _wav(self):
        if self.busy or self.last_dumps is None: return
        path,_=W.QFileDialog.getSaveFileName(self,'Save raw waveform with prehistory','','WAV (*.wav)')
        if path:
            try:
                dumps,channels=self.last_dumps
                write_dump_wav(path,dumps,channels,config=self.pi.config,meta={'purpose':'acoustic modes','prehistory_seconds':10})
                self.notice.setText(f'Saved {path}')
            except Exception as exc: self.notice.setText(str(exc))

    def reject(self):
        if self.busy:
            self.cancel.set(); self.notice.setText('Cancelling — close again after capture stops'); return
        self.worker.stop(); super().reject()
    def closeEvent(self,event):
        if self.busy: self.cancel.set(); event.ignore(); return
        self.worker.stop(); event.accept()
