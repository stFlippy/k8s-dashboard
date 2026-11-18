"""Bounded GUI task delivery and panel-scoped callback lifecycle."""
import logging
import time
from PyQt5.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, pyqtSignal, pyqtSlot
from PyQt5.QtWidgets import QMessageBox
logger = logging.getLogger("quaber.background")


class Signals(QObject):
    done = pyqtSignal(object, object)
    failed = pyqtSignal(object, str)


class Task(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function = function
        self.operation = getattr(function, "__qualname__", type(function).__name__).split(".<locals>.")[0]
        # The runnable owns its signals until run() completes, even if the
        # receiver window is closed while a subprocess is still stopping.
        self.signals = Signals()
        self.started = False

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
            logger.error("Ошибка: %s · %s", self.operation, type(exc).__name__)
            self.signals.failed.emit(self, str(exc) or type(exc).__name__)
        finally:
            self.function = None


class Background(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(4)
        self.stopped = False
        self.running: set[Task] = set()
        self._callbacks = {}
        self._keys = {}
        self._pending = {}

    def submit(self, function, done=None, failed=None, *, key=None, priority=False):
        if self.stopped:
            return False
        if key is not None and key in self._keys.values():
            self._pending[key] = (function, done, failed, priority)
            return True
        if len(self.running) >= 48:
            if failed:
                QTimer.singleShot(0, lambda: failed("Очередь фоновых операций заполнена. Дождитесь завершения текущих запросов."))
            return False
        task = Task(function)
        self.running.add(task)
        self._callbacks[task] = (done, failed)
        if key is not None:
            self._keys[task] = key
        task.signals.done.connect(self._done, Qt.ConnectionType.QueuedConnection)
        task.signals.failed.connect(self._failed, Qt.ConnectionType.QueuedConnection)
        self.pool.start(task, 10 if priority else 0)
        return True

    def stop(self):
        self.stopped = True
        self.pool.clear()
        self._callbacks.clear()
        self._pending.clear()
        self._keys.clear()
        for task in list(self.running):
            task.function = None
            if not task.started:
                self.running.discard(task)
                task.signals.deleteLater()

    def _finish(self, task, value, index):
        callback = self._callbacks.pop(task, (None, None))[index]
        key = self._keys.pop(task, None)
        pending = self._pending.pop(key, None) if key is not None else None
        self.running.discard(task)
        task.signals.deleteLater()
        if callback and not self.stopped:
            try:
                callback(value)
            except Exception as exc:
                # A user can close an embedded resource panel before its read
                # finishes. Qt has already destroyed the target widgets.
                if "has been deleted" not in str(exc):
                    QMessageBox.warning(None, "Quaber", str(exc))
        if pending and not self.stopped:
            function, done, failed, priority = pending
            self.submit(function, done, failed, key=key, priority=priority)

    @pyqtSlot(object, object)
    def _done(self, task, value):
        self._finish(task, value, 0)

    @pyqtSlot(object, str)
    def _failed(self, task, message):
        self._finish(task, message, 1)


class PanelBackground:
    """Finish reads normally but deliver results only while the panel is alive."""
    def __init__(self, background, owner):
        self.background, self.owner = background, owner
        self.active = True

    def submit(self, function, done=None, failed=None, *, key=None, priority=False):
        def guarded(callback):
            def deliver(value):
                from PyQt5 import sip
                if self.active and not sip.isdeleted(self.owner) and callback:
                    callback(value)
            return deliver
        return self.background.submit(function, guarded(done), guarded(failed), key=(id(self.owner), key) if key is not None else None, priority=priority)


