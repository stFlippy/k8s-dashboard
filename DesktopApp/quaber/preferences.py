"""Coalesce preferences and perform durable writes outside the GUI thread."""
import copy
import threading
import time
from PyQt6.QtCore import QObject, pyqtSignal


class PreferenceWriter(QObject):
    failed = pyqtSignal(str)

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.condition = threading.Condition()
        self.pending = None
        self.closed = False
        self.last = store.preferences()
        self.requested = copy.deepcopy(self.last)
        self.thread = threading.Thread(target=self._run, name="preferences-writer", daemon=True)
        self.thread.start()

    def save(self, value):
        value = copy.deepcopy(value)
        with self.condition:
            if value != self.requested and not self.closed:
                self.requested = value
                self.pending = value
                self.condition.notify()

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify()

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.pending is not None or self.closed)
                if self.pending is None and self.closed:
                    return
                if not self.closed:
                    self.condition.wait(timeout=.15)
                value, self.pending = self.pending, None
            try:
                # Windows may briefly deny atomic replacement while another
                # reader (or antivirus) holds the destination open. Retry only
                # this local idempotent write, outside the GUI thread.
                for attempt in range(5):
                    try:
                        self.store.save_preferences(value)
                        break
                    except PermissionError:
                        if attempt == 4:
                            raise
                        time.sleep(.03 * 2**attempt)
                self.last = value
            except Exception as exc:
                with self.condition:
                    self.requested = None
                try:
                    self.failed.emit(f"Настройки не сохранены: {type(exc).__name__}. Проверьте доступ и свободное место.")
                except RuntimeError:
                    return
