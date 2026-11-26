"""One tooltip width policy for widgets, item views and dynamic hints."""
import html
import re

from PyQt5.QtCore import QObject, QEvent, Qt
from PyQt5.QtGui import QFontMetrics, QTextDocument
from PyQt5.QtWidgets import QApplication, QWidget, QAbstractItemView, QHeaderView, QMenu, QToolTip

MAX_WIDTH = 440  # logical pixels: Qt handles high-DPI scaling


def tooltip_text(text):
    text = str(text)
    if re.match(r'\s*<(?:qt|html|p|b|div|table|span)\b', text, re.I):
        document = QTextDocument()
        document.setHtml(text)
        return document.toPlainText()
    return text


def wrap_tooltip(text):
    """Wrap even long names/URLs; keep short hints and explicit line breaks."""
    metrics = QFontMetrics(QToolTip.font())
    lines = []
    for paragraph in tooltip_text(text).split('\n'):
        line = ''
        for word in paragraph.split(' '):
            candidate = (line + ' ' + word) if line else word
            if metrics.horizontalAdvance(candidate) <= MAX_WIDTH:
                line = candidate
                continue
            if line:
                lines.append(line)
            line = ''
            for character in word:
                if line and metrics.horizontalAdvance(line + character) > MAX_WIDTH:
                    lines.append(line)
                    line = ''
                line += character
        lines.append(line)
    # QLabel otherwise chooses a narrow, almost square rich-text layout and
    # wraps our already bounded lines again. Keep short hints naturally small.
    width = max(1, max((metrics.horizontalAdvance(line) for line in lines), default=0))
    body = '<br>'.join(html.escape(line) for line in lines)
    return f'<qt><table width="{width}" cellpadding="0" cellspacing="0"><tr><td>{body}</td></tr></table></qt>'


def show_tooltip(position, text, widget=None, *args):
    if text:
        QToolTip.showText(position, wrap_tooltip(text), widget, *args)
    else:
        QToolTip.hideText()


class TooltipPolicy(QObject):
    def eventFilter(self, watched, event):
        if event.type() != QEvent.ToolTip or not isinstance(watched, QWidget):
            return False
        view = watched if isinstance(watched, QAbstractItemView) else watched.parentWidget()
        if isinstance(view, QHeaderView):
            section = view.logicalIndexAt(event.pos())
            tip = view.model().headerData(section, view.orientation(), Qt.ToolTipRole) if section >= 0 else ''
        elif isinstance(view, QAbstractItemView):
            # Matrix delegates resolve the hovered Pod, not just the table cell.
            if hasattr(view, 'schedules'):
                return False
            index = view.indexAt(event.pos())
            if not index.isValid():
                QToolTip.hideText()
                return True
            tip = index.data(Qt.ToolTipRole) or view.toolTip()
        elif isinstance(watched, QMenu):
            action = watched.actionAt(event.pos())
            if not watched.toolTipsVisible() or action is None:
                return False
            tip = action.toolTip()
        else:
            tip = watched.toolTip()
        if not tip:
            return False
        show_tooltip(event.globalPos(), tip, watched)
        return True


def install_tooltips():
    app = QApplication.instance()
    if app is not None and not hasattr(app, '_quaber_tooltip_policy'):
        app._quaber_tooltip_policy = TooltipPolicy(app)
        app.installEventFilter(app._quaber_tooltip_policy)
