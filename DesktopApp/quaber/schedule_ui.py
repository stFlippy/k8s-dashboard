"""One bounded local calendar worker and one tooltip clock for the window."""
from .tooltips import show_tooltip
from collections import OrderedDict
from dataclasses import replace
from datetime import datetime,timezone
import time
from PyQt5.QtCore import QObject,QTimer,pyqtSignal
from PyQt5.QtWidgets import QToolTip
from .matrix_presentation import PodVisual
from .schedule import ScheduleInput,ScheduleEngine,ScheduleResult


class Schedules(QObject):
    changed = pyqtSignal()
    MAX_ENTRIES = 4096

    def __init__(self,window):
        super().__init__(window)
        self.window = window
        self.cache,self.pending = OrderedDict(),OrderedDict()
        self.inflight,self.seen = set(),None
        self.generation,self.busy = 0,False
        self.tooltip = None
        self._wall,self._mono = time.time(),time.monotonic()
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def invalidate(self):
        self.generation += 1
        self.cache.clear()
        self.pending.clear()
        self.changed.emit()

    def begin(self): self.seen = set()

    def end(self):
        if self.seen is None: return
        removed = False
        for key in tuple(self.cache):
            if key not in self.seen:
                self.cache.pop(key)
                removed = True
        for key in tuple(self.pending):
            if key not in self.seen: self.pending.pop(key)
        self.seen = None
        if removed: self.changed.emit()

    def visual(self,cid,namespace,resource,scope):
        spec,status,metadata = resource.get('spec',{}),resource.get('status',{}),resource.get('metadata',{})
        from .cron_presentation import cron_state
        from .widgets import COLORS
        store = getattr(self.window, 'store', None)
        connection = next((c for c in store.clusters() if c['id'] == cid), {}) if store else {}
        manual_zone = connection.get('controllerTimeZone', self.window.preferences.get('controller_timezones',{}).get(cid))
        value = ScheduleInput(spec.get('schedule',''),manual_zone,bool(spec.get('suspend')),
                              spec.get('concurrencyPolicy','Allow'),spec.get('startingDeadlineSeconds'),bool(status.get('active')),
                              'connection setting')
        identity = (cid,'cronjob',namespace,metadata.get('name',''),metadata.get('uid'))
        key = (identity,value,status.get('lastScheduleTime'))
        if self.seen is not None: self.seen.add(key)
        cached = self.cache.get(key)
        if cached is None:
            if key in self.pending or key in self.inflight or len(self.cache)+len(self.pending)+len(self.inflight)<self.MAX_ENTRIES:
                if key not in self.inflight: self.pending[key] = value
                result = ScheduleResult(None,value.utc_offset,'unknown','Calculating','Calculating schedule…')
            else:
                result = ScheduleResult(None,value.utc_offset,'unknown','Unavailable','Next run unavailable: schedule limit reached; select fewer namespaces.')
        else:
            self.cache.move_to_end(key)
            result = cached
        confirmed = False
        try:
            seen = datetime.fromisoformat(scope.get('_last_success','').replace('Z','+00:00'))
            confirmed = result.next_due_utc is not None and seen >= result.next_due_utc
        except (TypeError,ValueError): pass
        stale = bool(scope.get('_stale')) or 'cronjob' in scope.get('_type_errors',{})
        state, summary = cron_state(resource, scope)
        result = replace(result, reason=(result.reason+'\n' if result.reason else '')+summary)
        return PodVisual(identity,state,COLORS[state],False,stale,summary,schedule=result,
                         schedule_confirmed=confirmed)

    def flush(self):
        if self.busy or not self.pending or self.window._closing: return
        generation = self.generation
        batch = tuple(list(self.pending.items())[:64])
        self.inflight = {key for key,_ in batch}
        for key,_ in batch: self.pending.pop(key,None)
        self.busy = True
        now = datetime.now(timezone.utc)
        def work():
            engine = ScheduleEngine()
            return tuple((key,engine.calculate(value,now)) for key,value in batch)
        def loaded(values):
            if generation != self.generation or self.window._closing: return
            for key,value in values: self.cache[key] = value
            self.changed.emit()
        def failed(message):
            if generation != self.generation: return
            for key,value in batch:
                self.cache[key] = ScheduleResult(None,value.utc_offset,'unknown',
                                                'Unavailable','Next run unavailable: calculation failed')
            self.changed.emit()
        def finalized(_):
            self.busy = False
            self.inflight.clear()
            if generation != self.generation: self.changed.emit()
            if self.pending and not self.window._closing: QTimer.singleShot(0,self.flush)
        if not self.window.background.submit(work,loaded,failed,key='schedule-calculation',monitor=False,finalized=finalized):
            self.busy = False

    def watch_tooltip(self,visual,position,view):
        self.tooltip = visual,position,view
        show_tooltip(position,visual.schedule.tooltip(datetime.now(timezone.utc),
                          confirmed_snapshot=visual.schedule_confirmed,stale=visual.stale),view)

    def tick(self):
        if self.window._closing: return
        wall,mono = time.time(),time.monotonic()
        if abs((wall-self._wall)-(mono-self._mono))>5: self.invalidate()
        self._wall,self._mono = wall,mono
        panel = getattr(self.window,'_resource_panel',None)
        if panel is not None and hasattr(panel,'refresh_cron_summary') and panel.isVisible():
            panel.refresh_cron_summary()
        if self.tooltip and QToolTip.isVisible():
            visual,position,view = self.tooltip
            matrix = self.window.matrix
            index = matrix.indexAt(matrix.viewport().mapFromGlobal(position))
            visual = next((v for v in matrix.cell_visuals(index.row(),index.column()) if v.identity==visual.identity and v.schedule is not None),None) if index.isValid() else None
            if visual is not None and view.isVisible():
                self.tooltip = visual,position,view
                show_tooltip(position,visual.schedule.tooltip(datetime.now(timezone.utc),
                                  confirmed_snapshot=visual.schedule_confirmed,stale=visual.stale),view)
            else:
                self.tooltip = None
                QToolTip.hideText()
        elif self.tooltip: self.tooltip = None
