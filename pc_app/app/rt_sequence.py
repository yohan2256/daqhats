"""Guided external-source RT measurement, with XL2 comparison fields."""
import csv
import json
import threading
from pathlib import Path
from datetime import datetime, timezone
import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets as W
from app.worker import CommandWorker
from pislm.rt_sequence import capture_raw, background, analyse_cycle, summarise
from pislm.standards.reverberation import DecayResult
from pislm.standards.bands import OCTAVE_NOMINAL


class DecayPlot(W.QWidget):
    def __init__(self):
        super().__init__(); self.entry={}; self.setMinimumHeight(200)
    def paintEvent(self,event):
        p=QtGui.QPainter(self); p.fillRect(self.rect(),QtGui.QColor('#181c22'))
        p.setPen(QtGui.QColor('white'))
        p.drawText(10,20,'감쇠 / 회귀선 / 배경소음 — 시간축: 음원 중단 후 (s), 세로축: dB')
        e=self.entry
        if not e.get('time_s'):
            p.drawText(10,45,e.get('error','CYC와 대역을 선택하세요')); return
        end=min(e['time_s'][-1],max(e['fit_end_s']*1.8,.5))
        def pt(t,v):
            return QtCore.QPointF(45+t/end*(self.width()-60),40-min(5,max(-65,v))/70*(self.height()-60))
        p.setPen(QtGui.QColor('#56b4e9'))
        points=[pt(t,v) for t,v in zip(e['time_s'],e['level_db']) if t<=end]
        p.drawPolyline(QtGui.QPolygonF(points))
        p.setPen(QtGui.QColor('#f0bf55'))
        a,b=e['fit_start_s'],e['fit_end_s']
        p.drawLine(pt(a,e['slope']*a+e['intercept']),pt(b,e['slope']*b+e['intercept']))
        p.setPen(QtGui.QColor('#e08080')); p.drawLine(pt(0,e['noise_db']),pt(end,e['noise_db']))
        p.setPen(QtGui.QColor('white'))
        for v in [0,-5,-25,-35,-55]: p.drawText(2,int(pt(0,v).y()),str(v))
        p.drawText(self.width()-80,self.height()-3,f'{end:.2f} s')


class RTSequence(W.QDialog):
    def __init__(self, parent, pi, channels, bands, fraction=3):
        super().__init__(parent); self.pi=pi; self.channels=list(channels)
        self.original_bands=tuple(bands); self.original_fraction=fraction
        self.cycles=[]; self.excluded=[]; self.baseline=None; self.signature=None; self.busy=False
        self.cancel=threading.Event(); self.remaining=0; self.references={}; self.accepted_results=None
        self.setWindowTitle('반복 잔향 측정 — XL2 절차 / 비교'); self.resize(1150,820)
        self.setWindowModality(QtCore.Qt.ApplicationModal)
        self.worker=CommandWorker(self); self.worker.finished_job.connect(self._done)
        self.worker.failed_job.connect(self._failed); self.worker.progress.connect(self._progress); self.worker.start()
        layout=W.QVBoxLayout(self)
        info=W.QLabel('외부 핑크노이즈 음원을 화면 안내에 맞춰 조작하세요. SET 후 같은 위치에서 3회 측정합니다.\n'
                      'XL2와 비교할 때 방법·대역폭을 맞추세요. XL2 내부 알고리즘 및 불확도 계산을 복제한 기능은 아닙니다.')
        info.setWordWrap(True); layout.addWidget(info)
        self.config=W.QWidget(); row=W.QHBoxLayout(self.config)
        self.method=W.QComboBox(); self.method.addItems(['T20','T30'])
        self.resolution=W.QComboBox(); self.resolution.addItem('현재 측정 대역',fraction); self.resolution.addItem('XL2 비교: 1/1 octave 63–8000 Hz',1)
        self.on=W.QDoubleSpinBox(); self.on.setRange(3,30); self.on.setValue(5); self.on.setSuffix(' s ON')
        self.off=W.QDoubleSpinBox(); self.off.setRange(3,30); self.off.setValue(5); self.off.setSuffix(' s OFF')
        for w in [self.method,self.resolution,self.on,self.off]: row.addWidget(w)
        layout.addWidget(self.config)
        self.notice=W.QLabel('SET: 음원을 끄고 배경소음을 측정하세요.'); self.notice.setWordWrap(True); layout.addWidget(self.notice)
        actions=W.QHBoxLayout(); layout.addLayout(actions)
        self.set_btn=W.QPushButton('SET · 배경소음 3초'); self.set_btn.clicked.connect(self._set)
        self.start=W.QPushButton('START · 3회'); self.start.clicked.connect(lambda: self._start(3))
        self.add=W.QPushButton('1회 추가 / 재측정'); self.add.clicked.connect(lambda: self._start(1))
        self.stop=W.QPushButton('STOP'); self.stop.clicked.connect(self._cancel)
        self.reset=W.QPushButton('RESET'); self.reset.clicked.connect(self._reset)
        for w in [self.set_btn,self.start,self.add,self.stop,self.reset]: actions.addWidget(w)
        view=W.QHBoxLayout(); layout.addLayout(view)
        self.channel=W.QComboBox()
        for ch in self.channels: self.channel.addItem(f'Ch{ch+1}',ch)
        self.cycle=W.QComboBox(); self.cycle.addItem('AVRG',None)
        self.remove=W.QPushButton('선택 CYC 제외'); self.remove.clicked.connect(self._remove)
        for w in [self.channel,self.cycle,self.remove]: view.addWidget(w)
        self.table=W.QTableWidget(0,9)
        self.table.setHorizontalHeaderLabels(['Hz','SET dB','필요 ON dB','RT (s)','유효 횟수','반복 SD (s)','상태 / 상관','XL2 (s)','차이 (s)'])
        self.table.horizontalHeader().setSectionResizeMode(W.QHeaderView.ResizeToContents)
        layout.addWidget(self.table,1); self.plot=DecayPlot(); layout.addWidget(self.plot)
        self.review=W.QCheckBox('감쇠곡선과 품질 경고를 확인했습니다.'); layout.addWidget(self.review)
        foot=W.QHBoxLayout(); layout.addLayout(foot)
        for text,slot in [('JSON 저장',self._json),('비교 CSV 저장',self._csv)]:
            w=W.QPushButton(text); w.clicked.connect(slot); foot.addWidget(w)
        self.apply=W.QPushButton('평균을 잔향 보정값으로 적용'); self.apply.clicked.connect(self._apply); foot.addWidget(self.apply)
        self.channel.currentIndexChanged.connect(self._render); self.cycle.currentIndexChanged.connect(self._render)
        self.table.cellClicked.connect(self._plot); self.table.itemChanged.connect(self._reference)
        self.review.toggled.connect(self._buttons); self.resolution.currentIndexChanged.connect(self._render)
        self.method.currentIndexChanged.connect(self._render)
        self._render()

    @property
    def bands(self):
        return self.original_bands if self.resolution.currentIndex()==0 else tuple(b for b in OCTAVE_NOMINAL if 63<=b<=8000)
    @property
    def fraction(self): return self.resolution.currentData()

    def _buttons(self):
        idle=not self.busy
        self.set_btn.setEnabled(idle and not self.cycles)
        self.start.setEnabled(idle and self.baseline is not None)
        self.add.setEnabled(idle and self.baseline is not None)
        self.reset.setEnabled(idle); self.remove.setEnabled(idle and self.cycle.currentData() is not None)
        self.config.setEnabled(idle and self.baseline is None)
        complete=bool(self.cycles) and all(e['complete'] for v in self._summary().values() for e in v.values())
        self.apply.setEnabled(idle and complete and self.review.isChecked() and
            self.bands==self.original_bands and self.fraction==self.original_fraction)

    def _summary(self): return summarise(self.cycles,self.channels,self.bands)
    def _progress(self,text): self.notice.setText(f'{len(self.cycles)}회 완료 · {text}')
    def _job(self,name,func):
        self.busy=True; self.review.setChecked(False); self._buttons(); self.worker.submit(name,func)
    def _set(self):
        self.cancel.clear(); bands=self.bands; fraction=self.fraction
        def run():
            dumps,signature=capture_raw(self.pi,self.channels,cancelled=self.cancel.is_set,progress=self.worker.progress.emit)
            return background(dumps,self.channels,bands,fraction),signature
        self._job('set',run)
    def _start(self,count):
        if self.busy or self.baseline is None: return
        self.remaining=count; self.cancel.clear(); self._next()
    def _next(self):
        on,off=self.on.value(),self.off.value(); bands=self.bands; fraction=self.fraction; method=self.method.currentText()
        baseline=self.baseline; signature=self.signature
        def run():
            from pislm.rt_sequence import validate
            if validate(self.pi,self.channels,on+off)!=signature:
                raise ValueError('설정/교정이 변경되었습니다. RESET 후 SET을 반복하세요.')
            dumps,_=capture_raw(self.pi,self.channels,on_seconds=on,off_seconds=off,
                cancelled=self.cancel.is_set,progress=self.worker.progress.emit)
            return analyse_cycle(dumps,self.channels,bands,fraction,method,baseline)
        self._job('cycle',run)
    def _cancel(self): self.remaining=0; self.cancel.set()
    def _done(self,name,value):
        self.busy=False
        if self.cancel.is_set():
            self.notice.setText('중단됨 — 미완료 사이클은 저장하지 않았습니다.'); self._buttons(); return
        if name=='set':
            self.baseline,self.signature=value
            self.notice.setText('ARMED — START 후 ON/OFF 안내를 따르세요. XL2도 같은 음원으로 측정하세요.')
        else:
            self.cycles.append(value); self.remaining-=1
            self.cycle.addItem(f'CYC {len(self.cycles)}',len(self.cycles)-1)
            self.notice.setText(f'{len(self.cycles)}회 완료. AVRG / CYC에서 결과를 확인하세요.')
        self._render()
        if name=='cycle' and self.remaining>0: self._next()
    def _failed(self,name,message,detail):
        self.busy=False; self.remaining=0; self.notice.setText(message); self._buttons()
    def _reset(self):
        self.cycles=[]; self.excluded=[]; self.baseline=None; self.signature=None; self.references={}
        self.cycle.clear(); self.cycle.addItem('AVRG',None); self.review.setChecked(False)
        self.notice.setText('음원을 끄고 SET을 누르세요.'); self._render()
    def _remove(self):
        i=self.cycle.currentData()
        if i is None or self.busy: return
        self.excluded.append(dict(index=i+1,cycle=self.cycles.pop(i),reason="operator excluded")); self.cycle.blockSignals(True); self.cycle.clear(); self.cycle.addItem('AVRG',None)
        for j in range(len(self.cycles)): self.cycle.addItem(f'CYC {j+1}',j)
        self.cycle.blockSignals(False); self.review.setChecked(False); self._render()
    def _render(self,*args):
        ch=self.channel.currentData(); idx=self.cycle.currentData(); summary=self._summary()
        self.table.blockSignals(True); self.table.setRowCount(len(self.bands))
        for row,b in enumerate(self.bands):
            e=summary[ch][b]; rt=e['mean_s']; status='; '.join(e['warnings']) or ('완료' if e['complete'] else '3회 유효 결과 필요')
            if idx is not None and idx<len(self.cycles):
                c=self.cycles[idx][ch][b]; rt=c['t60']; status=c.get('error') or f"r={c['correlation']:.3f}; "+'; '.join(c['warnings'])
            bg=10*np.log10(self.baseline['powers'][ch][b]/(20e-6)**2) if self.baseline else None
            ref=self.references.get((ch,b)); diff=rt-ref if rt is not None and ref is not None else None
            vals=[b,bg,None if bg is None else bg+(35 if self.method.currentText()=='T20' else 45),rt,e['n'],e['sd_s'],status,ref,diff]
            for col,v in enumerate(vals):
                item=W.QTableWidgetItem('' if v is None else (f'{v:.4g}' if isinstance(v,(float,np.floating)) else str(v)))
                if col!=7: item.setFlags(item.flags() & ~QtCore.Qt.ItemIsEditable)
                self.table.setItem(row,col,item)
        self.table.blockSignals(False); self._plot(max(0,self.table.currentRow()),0); self._buttons()
    def _plot(self,row,col):
        idx=self.cycle.currentData(); ch=self.channel.currentData()
        self.plot.entry=self.cycles[idx][ch][self.bands[row]] if idx is not None and idx<len(self.cycles) and row<len(self.bands) else {}
        self.plot.update()
    def _reference(self,item):
        if item.column()!=7: return
        key=(self.channel.currentData(),self.bands[item.row()])
        try:
            v=float(item.text())
            if not np.isfinite(v) or v<=0: raise ValueError()
            self.references[key]=v
        except ValueError: self.references.pop(key,None)
        self._render()
    def report(self):
        return dict(schema='pislm-rt-sequence/1',created_at=datetime.now(timezone.utc).isoformat(),
            method=self.method.currentText(),fraction=self.fraction,bands=self.bands,channels=self.channels,
            on_seconds=self.on.value(),off_seconds=self.off.value(),background=self.baseline,
            signature=self.signature,cycles=self.cycles,excluded=self.excluded,summary=self._summary(),
            quality_reviewed=self.review.isChecked(),
            reference=[dict(channel=ch,band=b,t60=v) for (ch,b),v in self.references.items()],
            averaging='arithmetic mean of per-cycle RT; SD is sample SD, not XL2 uncertainty',
            algorithm='independent short-time power regression; not XL2 firmware')
    def _json(self):
        path,_=W.QFileDialog.getSaveFileName(self,'결과 JSON','rt_sequence.json','JSON (*.json)')
        if not path: return
        try:
            target=Path(path); tmp=target.with_suffix(target.suffix+'.tmp')
            tmp.write_text(json.dumps(self.report(),ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8'); tmp.replace(target)
        except Exception as exc: self.notice.setText(str(exc))
    def _csv(self):
        path,_=W.QFileDialog.getSaveFileName(self,'XL2 비교 CSV','rt_xl2_comparison.csv','CSV (*.csv)')
        if not path: return
        try:
            with open(path,'w',encoding='utf-8-sig',newline='') as f:
                w=csv.writer(f); w.writerow(['channel','band_hz','method','fraction','cycle','rt_s','correlation','n','sample_sd_s','xl2_s','difference_s','warnings'])
                for ch,entries in self._summary().items():
                    for b,e in entries.items():
                        ref=self.references.get((ch,b)); rt=e['mean_s']
                        w.writerow([ch+1,b,self.method.currentText(),self.fraction,'AVRG',rt,'',e['n'],e['sd_s'],ref,rt-ref if rt is not None and ref is not None else '', '; '.join(e['warnings'])])
                        for i,c in enumerate(self.cycles):
                            v=c[ch][b]; w.writerow([ch+1,b,self.method.currentText(),self.fraction,i+1,v['t60'],v['correlation'],'','','','',v.get('error') or '; '.join(v['warnings'])])
        except Exception as exc: self.notice.setText(str(exc))
    def _apply(self):
        self._buttons()
        if not self.apply.isEnabled(): return
        summary=self._summary(); results={}
        for b in self.bands:
            entries=[c[ch][b] for c in self.cycles for ch in self.channels]
            results[b]=DecayResult(float(np.mean([summary[ch][b]['mean_s'] for ch in self.channels])),
                self.method.currentText(),min(e['correlation'] for e in entries),min(e['range_db'] for e in entries),None,b)
        self.accepted_results=results; self.worker.stop(); self.accept()
    def reject(self):
        if self.busy: self._cancel(); self.notice.setText('중단 중입니다. 작업 종료 후 닫으세요.'); return
        self.worker.stop(); super().reject()
    def closeEvent(self,event):
        if self.busy: self._cancel(); event.ignore(); return
        self.worker.stop(); event.accept()
