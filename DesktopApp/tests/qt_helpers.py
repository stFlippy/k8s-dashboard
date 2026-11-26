"""Qt5 event helpers for deterministic offscreen drag tests."""

from PyQt5.QtCore import QEvent, QPointF, Qt
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtWidgets import QApplication


def drag_move(widget, point):
    """Send a Qt5 mouse move with explicit local, window and screen positions."""
    event = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(point),
        QPointF(widget.mapTo(widget.window(), point)),
        QPointF(widget.mapToGlobal(point)),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(widget, event)
    QApplication.processEvents()


def label_text(label):
    """Compare displayed text independently of rich-text number styling."""
    from PyQt5.QtGui import QTextDocument
    document = QTextDocument()
    document.setHtml(label.text())
    return document.toPlainText().replace("\u00a0", " ")
