"""Bounded GUI task delivery and panel-scoped callback lifecycle."""
import logging
import time
import threading
from functools import partial
from dataclasses import dataclass
from PyQt5.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, QEvent, pyqtSignal, pyqtSlot
logger = logging.getLogger("quaber.background")
from .request_policy import LIMITS


def finish_callbacks(callbacks):
    """Complete all owned cleanup before propagating the first Python error."""
    first = None
    for callback in callbacks:
        try:
            callback()
        except Exception as exc:
            if first is None: first = exc
            else: logger.error('Additional terminal cleanup failure: %s', type(exc).__name__)
    if first is not None: raise first


@dataclass(frozen=True)
class ReadJobPlan:
    function: object
    done: object
    failed: object
    priority: bool
    monitor: bool
    demand: str
    cancelled: object
    finalized: object
    valid: object
    identity: object
    label: str
    intent: object


def cancelled_callbacks(plan):
    return ([plan.cancelled] if plan.cancelled else []) + ([partial(plan.finalized, 'CANCELLED')] if plan.finalized else [])


class Signals(QObject):
    done = pyqtSignal(object, object)
    failed = pyqtSignal(object, str)
    cancelled = pyqtSignal(object)


class Task:
    def __init__(self, function):
        self.function = function
        self.operation = getattr(function, "__qualname__", type(function).__name__).split(".<locals>.")[0]
        # The runnable owns its signals until run() completes, even if the
        # receiver window is closed while a subprocess is still stopping.
        self.signals = Signals()
        self.started = False
        self.returned = threading.Event()

    def run(self):
        self.started = True
        if self.function is None:
            return
        function, self.function = self.function, None
        started = time.monotonic()
        logger.info("Начало: %s", self.operation)
        try:
            value = function()
            logger.info("Завершено: %s (%.2f с)", self.operation, time.monotonic()-started)
            self.signals.done.emit(self, value)
        except Exception as exc:
            from .monitoring import MonitoringCancelled
            if isinstance(exc, MonitoringCancelled):
                logger.debug('Cancelled read: %s', self.operation)
                self.signals.cancelled.emit(self)
                return
            from .app_logging import log_problem
            log_problem(self.operation, f'{type(exc).__name__}: {exc}')
            self.signals.failed.emit(self, str(exc) or type(exc).__name__)
        finally:
            self.function = None


class TaskRunner(QRunnable):
    """Own the job until its complete run method (including wrappers) returns."""
    def __init__(self, task):
        super().__init__()
        self.task = task

    def run(self):
        try:
            self.task.run()
        finally:
            self.task.returned.set()


class Background(QObject):
    activity_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(32)
        self.stopped = False
        self.running: set[Task] = set()
        self._callbacks = {}
        self._keys = {}
        self._pending = {}
        self._terminal = {}
        self._retired = set()
        self._retire_timer = QTimer(self)
        self._retire_timer.setSingleShot(True)
        self._retire_timer.setInterval(50)
        self._retire_timer.timeout.connect(self._collect_retired)

    def _retire(self,task):
        # A queued result can reach GUI before emit()/run() has returned in
        # the pool thread. Never destroy its sender during that interval.
        self._retired.add(task)
        self._collect_retired()

    def _collect_retired(self):
        ready = {task for task in self._retired if task.returned.is_set()}
        self._retired.difference_update(ready)
        for task in ready:
            task.signals.deleteLater()
        if self._retired:
            self._retire_timer.start()
        else:
            self._retire_timer.stop()

    def activities(self):
        return (tuple(task.activity_label for task in self.running),
                tuple(plan.label for plan in self._pending.values()))

    def submit(self, function, done=None, failed=None, *, key=None, priority=False, monitor=True, demand='card', cancelled=None, finalized=None, plan_valid=None, plan_identity=None, label=None):
        # Retain the unwrapped plan for coalescing. Re-admission must capture
        # a new read token, never nest the previous epoch's read_scope.
        from .request_policy import request_intent
        intent = request_intent.get()
        priority = priority or intent.priority
        from .loading_status import task_label
        label = label or task_label(function, key)
        pending_plan = ReadJobPlan(function, done, failed, priority, monitor, demand, cancelled, finalized, plan_valid, plan_identity, label, intent)
        if plan_valid is not None and not plan_valid():
            finish_callbacks(cancelled_callbacks(pending_plan))
            return False
        intent_function = function
        def function():
            reset = request_intent.set(intent)
            try:
                return intent_function()
            finally:
                request_intent.reset(reset)
        priority = priority or intent.priority
        controller = getattr(self, 'monitoring', None)
        if monitor and controller is not None:
            token = controller.token()
            if not controller.valid(token):
                return False
            original = function
            def function():
                with controller.read_scope(token):
                    return original()
            def guarded(callback, success):
                def deliver(value):
                    if controller.valid(token) and callback:
                        callback(value)
                        if hasattr(controller, 'observe_read'):
                            controller.observe_read(token)
                return deliver
            done, failed = guarded(done, True), guarded(failed, False)
        if self.stopped:
            return False
        if key is not None and key in self._keys.values():
            if key not in self._pending and len(self.running)+len(self._pending) >= LIMITS.queue:
                if failed:
                    QTimer.singleShot(0, lambda: failed('Background queue is full.'))
                return False
            previous = self._pending.get(key)
            retired = previous is not None and previous.valid is not None and not previous.valid()
            changed = previous is not None and plan_identity is not None and previous.identity != plan_identity
            # A poll cannot downgrade the pending manual refresh of this key.
            if previous is None or retired or changed or intent.priority or not previous.intent.priority:
                self._pending[key] = pending_plan
                if previous is not None: finish_callbacks(cancelled_callbacks(previous))
                self.activity_changed.emit()
            else:
                finish_callbacks(cancelled_callbacks(pending_plan))
            return True
        if len(self.running)+len(self._pending) >= LIMITS.queue:
            if failed:
                QTimer.singleShot(0, lambda: failed("Очередь фоновых операций заполнена. Дождитесь завершения текущих запросов."))
            return False
        task = Task(function)
        task.monitor = monitor
        task.activity_label = label
        self.running.add(task)
        self._callbacks[task] = (done, failed)
        self._terminal[task] = (cancelled, finalized)
        if key is not None:
            self._keys[task] = key
        task.signals.done.connect(self._done, Qt.ConnectionType.QueuedConnection)
        task.signals.failed.connect(self._failed, Qt.ConnectionType.QueuedConnection)
        task.signals.cancelled.connect(self._cancelled, Qt.ConnectionType.QueuedConnection)
        self.activity_changed.emit()
        self.pool.start(TaskRunner(task), 10 if priority else 0)
        return True

    def stop(self):
        self.stopped = True
        self.pool.clear()
        callbacks = [callback for plan in self._pending.values() for callback in cancelled_callbacks(plan)]
        for cancel, finalize in self._terminal.values():
            if cancel: callbacks.append(cancel)
            if finalize: callbacks.append(partial(finalize,'CANCELLED'))
        self._terminal.clear()
        self._callbacks.clear()
        self._pending.clear()
        self._keys.clear()
        for task in list(self.running):
            task.function = None
            if not task.started:
                self.running.discard(task)
                task.returned.set()
                self._retire(task)
        self.activity_changed.emit()
        finish_callbacks(callbacks)

    def cancel_monitoring(self):
        # Retire coalescing ownership too. A canceled socket read may still be
        # returning; the next epoch must not queue behind that old transport.
        # Keep its runnable/callbacks alive until it settles, but without a key
        # that could consume the replacement epoch's pending plan in _finish.
        callbacks = []
        for key, plan in list(self._pending.items()):
            if plan.monitor:
                self._pending.pop(key)
                callbacks.extend(cancelled_callbacks(plan))
        for task in list(self._keys):
            if task.monitor:
                self._keys.pop(task)
        self.activity_changed.emit()
        finish_callbacks(callbacks)

    def _finish(self, task, value, index):
        callback = (self._callbacks.pop(task, (None, None)) + (None,))[index]
        cancel, finalize = self._terminal.pop(task, (None, None))
        key = self._keys.pop(task, None)
        pending = self._pending.pop(key, None) if key is not None else None
        self.running.discard(task)
        self._retire(task)
        self.activity_changed.emit()
        actions = []
        if not self.stopped:
            if index == 2 and cancel: actions.append(cancel)
            if callback: actions.append(partial(callback,value))
        if finalize: actions.append(partial(finalize,('ACCEPTED','FAILED','CANCELLED')[index]))
        try:
            finish_callbacks(actions)
        except Exception:
            if pending:
                try: finish_callbacks(cancelled_callbacks(pending))
                except Exception as exc: logger.error('Pending cleanup failure: %s',type(exc).__name__)
            raise
        if pending and not self.stopped:
            from .request_policy import request_intent
            reset = request_intent.set(pending.intent)
            try:
                self.submit(pending.function, pending.done, pending.failed, key=key,
                            priority=pending.priority, monitor=pending.monitor, demand=pending.demand,
                            cancelled=pending.cancelled, finalized=pending.finalized, plan_valid=pending.valid,
                            plan_identity=pending.identity, label=pending.label)
            finally:
                request_intent.reset(reset)

    @pyqtSlot(object, object)
    def _done(self, task, value):
        self._finish(task, value, 0)

    @pyqtSlot(object, str)
    def _failed(self, task, message):
        self._finish(task, message, 1)

    @pyqtSlot(object)
    def _cancelled(self, task):
        self._finish(task, None, 2)


class PanelBackground(QObject):
    """Finish reads normally but deliver results only while the panel is alive."""
    def __init__(self, background, owner):
        super().__init__(owner)
        self.background, self.owner = background, owner
        self.active = True
        self._read_cancel = threading.Event()
        self._tab_cancel = threading.Event()
        owner.installEventFilter(self)
        owner.destroyed.connect(self._closed)

    def _closed(self):
        self.active = False
        self.cancel_reads()

    def eventFilter(self, watched, event):
        if watched is self.owner and event.type() == QEvent.Hide and not event.spontaneous():
            self.cancel_reads()
        return False

    def cancel_reads(self, *, tab_only=False):
        self._tab_cancel.set()
        self._tab_cancel = threading.Event()
        if not tab_only:
            self._read_cancel.set()
            self._read_cancel = threading.Event()

    def submit(self, function, done=None, failed=None, *, key=None, priority=False, monitor=True,
               demand='card', cancelled=None, finalized=None, _renewal_budget=0, label=None):
        from .loading_status import task_label
        label = label or task_label(function, key)
        from PyQt5 import sip
        from .monitoring import cancellable_read, ReadToken, _read_token
        from .request_policy import expected_credentials, credentials_revision
        local = self._tab_cancel if demand == 'tab' else self._read_cancel
        original = function
        revision = getattr(self.owner, '_opened_credentials', None) if monitor else None
        cluster_id = getattr(self.owner, 'cluster_id', None)
        try:
            connection = self.owner.api.cluster(cluster_id) if revision is not None else {}
            subject = (connection.get('server'), connection.get('username'))
        except Exception:
            subject = None
        renewals = []
        def execute():
            from dataclasses import replace
            parent = _read_token.get()
            reset = _read_token.set(replace(parent, allow_renewal=False)) if _renewal_budget and parent else None
            try:
                if revision is not None:
                    with expected_credentials({cluster_id: revision}):
                        return original()
                return original()
            finally:
                token = _read_token.get()
                if token is not None: renewals.extend(token.renewals)
                if reset is not None: _read_token.reset(reset)
        function = cancellable_read(execute, local, ReadToken(0, threading.Event())) if monitor else execute
        def alive():
            return self.active and not sip.isdeleted(self.owner) and not (monitor and local.is_set())
        def guarded(callback, success):
            def deliver(value):
                if not alive() or not callback: return
                if revision is not None:
                    # A sibling read from before a renewal cannot adopt after rebind.
                    if self.owner._opened_credentials != revision: return
                    if success:
                        try: current = credentials_revision(self.owner.api.cluster(cluster_id))
                        except Exception: current = None
                        if current != revision:
                            controller = getattr(self.owner, 'controller', None)
                            if hasattr(controller, 'set_read_only'): controller.set_read_only(True)
                            if failed: failed('Connection settings changed. Reopen this resource to continue.')
                            return
                callback(value)
            return deliver
        def complete(outcome):
            if not self.active or sip.isdeleted(self.owner): return
            rebind = False
            if monitor and revision is not None and _renewal_budget == 0 and alive():
                try:
                    current = self.owner.api.cluster(cluster_id)
                    after = credentials_revision(current)
                    rebind = (current.get('enabled') and subject == (current.get('server'), current.get('username'))
                              and (cluster_id, revision, after) in renewals and self.owner._opened_credentials == revision)
                except Exception: rebind = False
            controller = getattr(self.owner, 'controller', None)
            monitoring = getattr(controller, 'monitoring', None)
            if rebind and (monitoring is None or monitoring.policy.allows()):
                # Replan a read, never rebase a write/draft or replay a mutation.
                self.owner._opened_credentials = after
                if hasattr(controller, 'set_read_only'):
                    controller.set_read_only(True, preserve_drafts=True)
                if monitoring is not None and hasattr(monitoring, 'accept_renewals'):
                    monitoring.accept_renewals(tuple(renewals))
                if self.submit(original, done, failed, key=key, priority=priority, monitor=monitor,
                               demand=demand, cancelled=cancelled, finalized=finalized, _renewal_budget=1, label=label):
                    return
                outcome = 'CANCELLED'
            if outcome == 'CANCELLED' and cancelled: cancelled()
            if finalized: finalized(outcome)
        return self.background.submit(function, guarded(done, True), guarded(failed, False),
            key=(id(self.owner), key) if key is not None else None, priority=priority, monitor=monitor,
            demand=demand, finalized=complete, label=label)
