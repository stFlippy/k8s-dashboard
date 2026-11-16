"""Live resource details that keep copied text and scroll position stable."""
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QTextBrowser


class ResourceDetails(QTextBrowser):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._rendered_summary = None
        self._pending_summary = None
        self._selecting = False
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.timeout.connect(self._apply_pending)
        self.selectionChanged.connect(lambda: self._update_timer.start(0))

    def set_summary(self, text):
        # resourceVersion, metrics and other invisible changes need no document reset.
        self._pending_summary = text if text != self._rendered_summary else None
        self._apply_pending()

    def _apply_pending(self):
        if self._pending_summary is None or self._selecting or self.textCursor().hasSelection():
            return
        text, self._pending_summary = self._pending_summary, None
        self._rendered_summary = text
        vertical, horizontal = self.verticalScrollBar().value(), self.horizontalScrollBar().value()
        self.setHtml(text)
        self.verticalScrollBar().setValue(vertical)
        self.horizontalScrollBar().setValue(horizontal)

    def mousePressEvent(self, event):
        self._selecting = True
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self._selecting = False
        self._update_timer.start(0)
