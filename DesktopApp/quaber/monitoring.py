"""One monitoring state/epoch shared by GUI producers and physical dispatch."""
import contextvars
import threading
import time
from dataclasses import dataclass, field
from contextlib import contextmanager
from PyQt5.QtCore import QObject, QEvent, QTimer, Qt, pyqtSignal
from PyQt5.QtWidgets import QApplication, QLabel, QPushButton, QVBoxLayout, QWidget
from .request_policy import LIMITS


class MonitoringCancelled(RuntimeError):
    pass


def idle_minutes(value):
    return value if type(value) is int and 10 <= value <= 120 else LIMITS.idle_minutes


@dataclass(frozen=True)
class ReadToken:
    epoch: int
    canceled: threading.Event
    fetched: threading.Event = field(default_factory=threading.Event)
    core_reads: list = field(default_factory=list)
    attempt_id: str | None = None
    renewals: list = field(default_factory=list)
    allow_renewal: bool = True


_read_token = contextvars.ContextVar('monitoring_read_token', default=None)
_optional_read = contextvars.ContextVar('optional_monitoring_read', default=False)


def optional_read(function):
    """Ancillary identity GETs are ancillary too, regardless of their URL."""
    from functools import wraps
    @wraps(function)
    def call(*args, **kwargs):
        reset = _optional_read.set(True)
        try:
            return function(*args, **kwargs)
        finally:
            _optional_read.reset(reset)
    return call


def bind_read(function):
    context = contextvars.copy_context()
    def run(*args, **kwargs):
        return context.copy().run(function, *args, **kwargs)
    return run


class _LinkedCancellation:
    def __init__(self, parent, local): self.parent, self.local = parent, local
    def is_set(self): return self.parent.is_set() or self.local.is_set()


def cancellable_read(function, local, fallback):
    def run():
        parent = _read_token.get() or fallback
        child = ReadToken(parent.epoch, _LinkedCancellation(parent.canceled, local), parent.fetched, parent.core_reads,
                          parent.attempt_id, parent.renewals, parent.allow_renewal)
        if child.canceled.is_set(): raise MonitoringCancelled('Superseded read plan.')
        token = _read_token.set(child)
        try:
            return function()
        finally:
            _read_token.reset(token)
    return run


class MonitoringState:
    """Thread-safe state only; workers never inspect widgets."""
    def __init__(self, minutes=30, clock=time.monotonic):
        self.clock, self.minutes = clock, idle_minutes(minutes)
        self.last_activity = clock()
        self.reasons = set()
        self.hard_reason = None
        self.state = 'ACTIVE'
        self.epoch = 0
        self._cancel = threading.Event()
        self.lock = threading.RLock()

    def token(self):
        with self.lock:
            return ReadToken(self.epoch, self._cancel)

    def allows(self, token=None):
        with self.lock:
            return (self.state in ('ACTIVE', 'RECONNECTING') and not self.reasons
                    and (token is None or (token.epoch == self.epoch and not token.canceled.is_set())))

    def check(self, token=None):
        if not self.allows(token if token is not None else _read_token.get()):
            raise MonitoringCancelled('Monitoring is paused; stale read canceled.')

    def _invalidate(self):
        self._cancel.set()
        self.epoch += 1
        self._cancel = threading.Event()

    def soft(self, reason, stopped):
        with self.lock:
            before = set(self.reasons)
            self.reasons.add(reason) if stopped else self.reasons.discard(reason)
            if before == self.reasons:
                return False
            self._invalidate()
            if self.state == 'RECONNECTING' and stopped:
                self.state = 'SLEEPING'
            if self.state not in ('SLEEPING', 'CLOSING', 'CLOSED'):
                self.state = 'SOFT_PAUSED' if self.reasons else 'ACTIVE'
            return True

    def sleep(self, reason):
        with self.lock:
            if self.state in ('CLOSING', 'CLOSED'):
                return False
            self._invalidate()
            self.state, self.hard_reason = 'SLEEPING', reason
            return True

    def activity(self):
        with self.lock:
            self.last_activity = self.clock()

    def tick(self):
        with self.lock:
            if self.state not in ('SLEEPING', 'CLOSING', 'CLOSED') and self.clock()-self.last_activity >= self.minutes*60:
                return self.sleep('inactivity')
            return False

    def set_idle_minutes(self, minutes):
        with self.lock:
            self.minutes = idle_minutes(minutes)
            return self.tick()

    def reconnect(self):
        with self.lock:
            if self.state != 'SLEEPING' or self.reasons:
                return False
            self._invalidate()
            self.state = 'RECONNECTING'
            self.last_activity = self.clock()
            return True

    def reconnected(self, success):
        with self.lock:
            if self.state == 'RECONNECTING':
                self.state = 'ACTIVE' if success else 'SLEEPING'

    def close(self):
        with self.lock:
            self._invalidate()
            self.state = 'CLOSING'

    @contextmanager
    def read_scope(self, token):
        reset = _read_token.set(token)
        try:
            self.check(token)
            yield
        finally:
            _read_token.reset(reset)


class MonitoringController(QObject):
    changed = pyqtSignal()
    physical_read = pyqtSignal(object)
    TEXT = {'inactivity': 'Monitoring paused due to inactivity.',
            'disconnect': 'Monitoring paused because the remote session was disconnected.',
            'lock': 'Monitoring paused because the Windows session was locked.',
            'suspend': 'Monitoring paused because Windows was suspended.'}

    def __init__(self, window, resume, *, clock=time.monotonic):
        super().__init__(window)
        self.window, self.resume = window, resume
        self.policy = MonitoringState(window.preferences.get('monitoring_idle_minutes'), clock)
        self.timers = {}
        self._foreign_since = None
        self._last_state = 'ACTIVE'
        self.attempt = None
        self.resume_results = {}
        self._resume_revisions = {}
        self.overlay = QWidget(window.workspace)
        self.overlay.setObjectName('monitoringSleep')
        self.overlay.setStyleSheet('QWidget#monitoringSleep { background: #202630; color: white; }')
        layout = QVBoxLayout(self.overlay)
        layout.addStretch()
        self.message = QLabel()
        self.message.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.message)
        guidance = QLabel('')
        self.guidance = guidance
        guidance.setAlignment(Qt.AlignCenter)
        layout.addWidget(guidance)
        self.button = QPushButton('Resume monitoring')
        self.button.setToolTip('Возобновить мониторинг включённых подключений.')
        self.button.setMinimumHeight(48)
        self.button.setMaximumWidth(360)
        self.button.setAutoDefault(True)
        self.button.setDefault(True)
        self.button.clicked.connect(self.reconnect)
        layout.addWidget(self.button, alignment=Qt.AlignCenter)
        layout.addStretch()
        self.physical_read.connect(self._observe_physical, Qt.ConnectionType.QueuedConnection)
        self.overlay.hide()
        self.watchdog = QTimer(self)
        self.watchdog.setInterval(1000)
        self.watchdog.timeout.connect(self.tick)
        self.watchdog.start()
        QApplication.instance().installEventFilter(self)
        self.changed.connect(self._apply)
        from .windows_session import WindowsSession
        self.session = WindowsSession(window, self)
        self._apply()

    @property
    def paused(self): return not self.policy.allows()
    def token(self):
        from dataclasses import replace
        token = self.policy.token()
        return replace(token, attempt_id=self.attempt.attempt_id
                       if self.attempt is not None and self.policy.state == 'RECONNECTING' else None)
    def check(self, token=None):
        self.policy.check(token)
        token = token if token is not None else _read_token.get()
        if token is not None and token.attempt_id is not None:
            if (self.attempt is None or token.attempt_id != self.attempt.attempt_id
                    or self.policy.clock() >= self.attempt.deadline):
                raise MonitoringCancelled('Resume attempt expired or retired.')
    def valid(self, token): return self.policy.allows(token)
    def read_scope(self, token): return self.policy.read_scope(token)

    def soft(self, reason, stopped):
        if self.policy.soft(reason, stopped): self.changed.emit()
    def sleep(self, reason):
        if self.policy.sleep(reason): self.changed.emit()
    def set_idle_minutes(self, value):
        if self.policy.set_idle_minutes(value): self.changed.emit()

    def suspend(self, timer):
        if timer.isActive():
            from .polling import poll_interval
            self.timers[timer] = poll_interval(timer)
            timer.stop()

    def tick(self):
        if self.attempt is not None:
            if not self.policy.allows() or self.attempt.epoch != self.policy.epoch:
                self.attempt = None
            elif self._capture_demand(self.window.store.clusters()) != self.attempt.scopes:
                self._finish_resume_wait()
            elif self.policy.clock() >= self.attempt.deadline:
                self._finish_resume_wait(expired=True)
        if self.policy.tick(): self.changed.emit()

    def reconnect(self):
        if self.attempt is not None and self.attempt.epoch != self.policy.epoch:
            self.attempt = None
        if self.attempt is not None:
            return
        if not self.session.connected or self.session.own_foreground() is not True:
            self.guidance.setText('Resume requires a connected session and the application in the foreground.')
            return
        if self.policy.reconnect():
            from .resume import ResumeAttempt
            from .request_policy import credentials_revision
            clusters = self.window.store.clusters()
            revisions = tuple((c['id'], credentials_revision(c)) for c in clusters if c.get('enabled'))
            scopes = self._capture_demand(clusters)
            self.attempt = ResumeAttempt.start(self.policy.epoch, revisions, self.policy.clock(), scopes)
            self._resume_revisions = dict(revisions)
            self.resume_results = {cid: 'Not requested' for cid, _ in revisions}
            self.guidance.setText('')
            self.changed.emit()
            self.resume()
            if not scopes:
                self.no_demand()

    def _finish_resume_wait(self, *, expired=False):
        # A failed connection is not an inactivity/session pause. Leave the
        # workspace usable and normal bounded polling enabled, without claiming
        # that an unobserved connection is Ready.
        if expired and self.attempt is not None:
            for scope in self.attempt.scopes:
                if self.resume_results.get(scope.cluster_id) == 'Not requested':
                    self.resume_results[scope.cluster_id] = 'Unavailable'
        self.attempt = None
        if self.policy.state == 'RECONNECTING':
            self.policy.reconnected(True)
            self.changed.emit()
            if expired:
                self.window.statusBar().showMessage(
                    'Подключения ещё не восстановлены. Проверьте доступ к кластерам; обновление продолжится в фоне.', 15000)

    def _capture_demand(self, clusters):
        from .resume import ReadScope
        from .kubernetes import GVR
        window = self.window
        panel = getattr(window, '_resource_panel', None)
        if panel is not None:
            if panel.kind not in GVR: return ()
            return (ReadScope(panel.cluster_id, (GVR[panel.kind][1],), (panel.namespace,), panel.name),)
        page = window.tabs.currentWidget()
        if page in (window.dumps_page, window.versions_page):
            selected = page.clusters.ids()
            resources, namespaces = ('namespaces','deployments','cronjobs'), None
        elif page is getattr(window, 'events_page', None):
            selected = page.clusters.ids()
            namespace = page.namespace.currentData()
            resources, namespaces = ('events',), (namespace,) if namespace else None
        elif page is window.matrix_page:
            from .fetch_plan import COLLECTIONS
            view = window.views.active
            selected = view.get('selectedClusterIds')
            selected = {c['id'] for c in clusters} if selected is None else set(selected)
            namespaces = view.get('selectedNamespaces')
            if namespaces == []: return ()
            namespaces = None if namespaces is None else tuple(namespaces)
            resources = tuple(GVR[kind][1] for _, kind in COLLECTIONS) + ('namespaces',)
        else:
            kind = 'quota' if page is window.quotas_page else 'configmap'
            controls = getattr(window, kind+'_controls', None)
            if kind == 'quota':
                selected = window.quota_clusters.ids()
                namespaces = None
            elif controls:
                selected = {controls[0].currentData()}
                namespaces = (controls[1].currentText(),)
                if not namespaces[0]: return ()
            else: return ()
            resources = (GVR[kind][1], 'namespaces')
        return tuple(ReadScope(c['id'], resources, namespaces) for c in clusters
                     if c.get('enabled') and c['id'] in selected)

    def record_read(self, cluster_id, path, status, revision):
        """Worker boundary: emit detached evidence, never inspect GUI state."""
        token = _read_token.get()
        if token is None or token.attempt_id is None: return
        from .resume import PhysicalRead
        self.physical_read.emit(PhysicalRead(token.attempt_id, token.epoch, cluster_id, revision,
                                            path, status, _optional_read.get(), tuple(token.renewals)))

    def accept_renewals(self, renewals):
        """GUI-only, attestations originate in the compare-and-update login path."""
        if self.attempt is None: return
        from .request_policy import credentials_revision
        current = {c['id']: credentials_revision(c) for c in self.window.store.clusters() if c.get('enabled')}
        for cid, before, after in renewals:
            if self._resume_revisions.get(cid) == before and current.get(cid) == after:
                self._resume_revisions[cid] = after

    def _observe_physical(self, evidence):
        attempt = self.attempt
        if (attempt is None or evidence.attempt_id != attempt.attempt_id or evidence.epoch != attempt.epoch
                or evidence.epoch != self.policy.epoch or not self.policy.allows()
                or self.policy.clock() >= attempt.deadline or evidence.optional): return
        if self._capture_demand(self.window.store.clusters()) != attempt.scopes:
            self._finish_resume_wait()
            return
        if not any(s.cluster_id == evidence.cluster_id and s.accepts(evidence.path) for s in attempt.scopes): return
        self.accept_renewals(evidence.renewals)
        from .request_policy import credentials_revision
        current = next((c for c in self.window.store.clusters() if c['id'] == evidence.cluster_id and c.get('enabled')), None)
        if (current is None or evidence.revision != credentials_revision(current)
                or self._resume_revisions.get(evidence.cluster_id) != evidence.revision): return
        if evidence.status == 200: outcome = 'Ready'
        elif evidence.status in (403,404): outcome = 'Access limited'
        elif evidence.status == 401: outcome = 'Sign-in required'
        else: outcome = 'Unavailable'
        previous = self.resume_results.get(evidence.cluster_id)
        if previous == outcome or previous == 'Ready': return
        self.resume_results[evidence.cluster_id] = outcome
        if outcome in ('Ready','Access limited'):
            self.policy.reconnected(True)
            self.changed.emit()

    def observe_read(self, token):
        attempt = self.attempt
        if (attempt is None or token.epoch != attempt.epoch
                or token.attempt_id != attempt.attempt_id or not self.valid(token)):
            return
        if self.policy.clock() >= attempt.deadline:
            self._finish_resume_wait(expired=True)
            return
        # Production attempts accept only queued, scoped physical evidence.
        if attempt.scopes:
            self.accept_renewals(token.renewals)
            return
        from .request_policy import credentials_revision
        revisions = dict(attempt.revisions)
        for cluster_id, before, after in token.renewals:
            if revisions.get(cluster_id) == before:
                revisions[cluster_id] = after
        for cluster_id in token.core_reads:
            cluster = next((c for c in self.window.store.clusters() if c['id'] == cluster_id), None)
            if cluster is not None and revisions.get(cluster_id) == credentials_revision(cluster):
                self.resume_results[cluster_id] = 'Ready'
                self.policy.reconnected(True)
                self.changed.emit()

    def no_demand(self):
        # Explicit empty demand is the only completion without physical evidence.
        if self.attempt is not None and (not self.attempt.revisions or not self.attempt.scopes):
            self.policy.reconnected(True)
            self.attempt = None
            self.changed.emit()

    def _apply(self):
        from PyQt5 import sip
        state = self.policy.state
        old, self._last_state = self._last_state, state
        stopped = state not in ('ACTIVE', 'RECONNECTING')
        if stopped:
            self.window.set_read_only(True)
            self.window._matrix_generation += 1
            self.window.loading_matrix = False
            self.window._matrix_manual_pending = False
            self.window.matrix_return_timer.stop()
            for timer in self.window.findChildren(QTimer):
                if timer.property('clusterPoll'): self.suspend(timer)
            self.window.background.cancel_monitoring()
            if self.window._write_operation is None:
                self.window._pod_operations.clear()
            self.window.admin_access.busy.clear()
            self.window.inventory.cancel_reads('PAUSED') if hasattr(self.window, 'inventory') else None
            for panel in getattr(self.window, '_resource_stack', []):
                panel.suspend_reads()
            self.window.statusBar().showMessage('Monitoring paused: ' + (self.policy.hard_reason or ', '.join(sorted(self.policy.reasons))))
        elif old == 'SOFT_PAUSED' and state == 'ACTIVE':
            for timer, interval in self.timers.items():
                if not sip.isdeleted(timer) and self.window.preferences.get('refresh_ms', 10000) != 0:
                    from .polling import restart_poll_timer
                    restart_poll_timer(timer, interval)
            self.timers.clear()
            QTimer.singleShot(0, self.resume)
        elif old == 'RECONNECTING' and state == 'ACTIVE':
            for timer, interval in self.timers.items():
                if not sip.isdeleted(timer) and self.window.preferences.get('refresh_ms', 10000) != 0:
                    from .polling import restart_poll_timer
                    restart_poll_timer(timer, interval)
            self.timers.clear()
        # Resume admits reads immediately. Connection readiness is established
        # asynchronously; a slow cluster must not cover the workspace meanwhile.
        if state == 'RECONNECTING':
            self.window.statusBar().showMessage('Мониторинг возобновлён. Восстановление данных…')
        sleeping = state == 'SLEEPING'
        self.overlay.setVisible(sleeping)
        if sleeping:
            self.overlay.setGeometry(self.window.workspace.rect())
            self.overlay.raise_()
            self.message.setText(self.TEXT.get(self.policy.hard_reason, 'Monitoring paused.'))
            self.button.setText('Resume monitoring')
            self.button.setEnabled(not self.policy.reasons
                                   and (not hasattr(self, 'session') or self.session.connected))
            self.button.setFocus()

    def close(self):
        self.policy.close()
        self.watchdog.stop()
        QApplication.instance().removeEventFilter(self)
        self.session.close()

    def eventFilter(self, watched, event):
        if watched is self.window:
            if event.type() == QEvent.Resize:
                self.overlay.setGeometry(self.window.workspace.rect())
            elif event.type() == QEvent.WinIdChange and hasattr(self, 'session') and hasattr(self.session, 'wts'):
                self.session.register()
        if event.type() in (QEvent.KeyPress, QEvent.MouseButtonPress, QEvent.Wheel, QEvent.TouchBegin):
            from PyQt5 import sip
            if isinstance(watched, QWidget) and not sip.isdeleted(watched) and QWidget.window(watched) in QApplication.topLevelWidgets():
                self.policy.activity()
        return False
