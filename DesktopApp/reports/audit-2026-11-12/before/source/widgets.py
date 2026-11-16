"""Native widgets matching the web matrix and resource editor."""
import re
from pathlib import Path
from datetime import datetime, timezone
from PyQt6.QtCore import Qt, QSize, QRect, QRectF, QPointF, QEvent, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen, QFont, QTextCursor, QFontDatabase
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QLabel,
    QPushButton, QPlainTextEdit, QHeaderView, QStyledItemDelegate, QMenu, QTableWidget, QTableView, QAbstractItemView, QToolTip, QCheckBox, QFrame, QComboBox, QListView)

COLORS = {"Running": "#43B694", "Pending": "#DCAE54", "Failed": "#E56B6E",
          "Unknown": "#70819d", "Completed": "#729fc7", "Terminating": "#8d94a0"}


class FullCheckBox(QCheckBox):
    def paintEvent(self, event):
        super().paintEvent(event)
        color = COLORS.get(self.property("status"))
        if self.objectName() == "recentFilter":
            color = COLORS["Pending"]
        if color:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(QRectF(3, 5, 5, max(0, self.height()-10)), 2, 2)

    def hitButton(self, position):
        return self.rect().contains(position)


class AppComboBox(QComboBox):
    """Use the same item geometry for mouse and keyboard popup selection."""
    def __init__(self, parent=None):
        super().__init__(parent)
        view = QListView(self)
        view.setMouseTracking(True)
        view.setUniformItemSizes(True)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.setView(view)
        self.setItemDelegate(QStyledItemDelegate(view))


def pod_rects(rect, count):
    result = []
    for i in range(count):
        row_count = min(2, count - i // 2 * 2)
        left = rect.left() + (rect.width() - (row_count * 15 + (row_count-1)*4)) // 2
        result.append(QRect(left + i % 2 * 19, rect.top()+7+i//2*19, 15, 15))
    return result


def pod_at(rect, point, count):
    return next((i for i, area in enumerate(pod_rects(rect, count)) if area.contains(point)), None)


def resource_age(timestamp):
    try:
        elapsed = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(timestamp.replace("Z", "+00:00"))).total_seconds())
        minutes = int(elapsed // 60)
        return f"{minutes} мин" if minutes < 60 else f"{minutes // 60} ч" if minutes < 2880 else f"{minutes // 1440} д"
    except (ValueError, TypeError, AttributeError):
        return "—"


def resource_percent(metrics, resource):
    if not metrics.get("available") or not metrics.get("containers"):
        return None
    usage_key, limit_key = ("usageCores", "limitCores") if resource == "cpu" else ("usageBytes", "limitBytes")
    samples = [c.get(resource, {}) for c in metrics["containers"]]
    if any(c.get(usage_key) is None or not c.get(limit_key) for c in samples):
        return None
    return 100 * sum(c[usage_key] for c in samples) / sum(c[limit_key] for c in samples)


class StatusDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        status = str(index.data() or "Unknown")
        painter.save()
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        painter.fillRect(option.rect, option.palette.base())
        color = QColor(COLORS.get(status, COLORS["Unknown"]))
        area = option.rect.adjusted(6, 5, -6, -5)
        background = QColor(color)
        background.setAlpha(35)
        painter.setBrush(background)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(area, 5, 5)
        painter.setPen(color)
        painter.drawText(area, Qt.AlignmentFlag.AlignCenter, status)
        painter.restore()


class PodMatrix(QTableWidget):
    activatedAt = pyqtSignal(int, int, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setShowGrid(False)
        self.hovered = (-1, -1)
        self.hovered_pod = None
        self._resize_drag = None
        self.frozen = QTableView(self)
        self.frozen.setObjectName("frozenDeployment")
        self.frozen.setShowGrid(False)
        self.frozen.setFrameShape(QFrame.Shape.NoFrame)
        self.frozen.horizontalHeader().setMinimumSectionSize(0)
        self.frozen.setMouseTracking(True)
        self.frozen.viewport().installEventFilter(self)
        self.viewport().installEventFilter(self)
        self.frozen.setModel(self.model())
        self.frozen.setSelectionModel(self.selectionModel())
        self.frozen.setItemDelegate(PodDelegate(self))
        self.frozen.horizontalHeader().setStretchLastSection(False)
        self.frozen.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.frozen.horizontalHeader().setSectionsMovable(False)
        self.frozen.horizontalHeader().show()
        self.frozen.verticalHeader().hide()
        self.frozen.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.frozen.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.frozen.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.verticalScrollBar().valueChanged.connect(self.frozen.verticalScrollBar().setValue)
        self.frozen.verticalScrollBar().valueChanged.connect(self.verticalScrollBar().setValue)
        self.horizontalHeader().sectionResized.connect(self.sync_frozen)
        self.model().columnsInserted.connect(self.sync_frozen)
        self.verticalHeader().sectionResized.connect(lambda row, old, height: self.frozen.setRowHeight(row, height))
        self.verticalScrollBar().valueChanged.connect(lambda *_: self._set_hover((-1, -1)))
        self.horizontalScrollBar().valueChanged.connect(lambda *_: self._set_hover((-1, -1)))
        self.frozen.clicked.connect(lambda index: self.activatedAt.emit(index.row(), 0, None))
        self.horizontalHeader().sectionMoved.connect(self._pin_deployment_column)
        self.verticalHeader().sectionMoved.connect(self.sync_frozen)

    def setHorizontalHeader(self, header):
        super().setHorizontalHeader(header)
        header.sectionMoved.connect(self._pin_deployment_column)
        header.sectionResized.connect(self.sync_frozen)

    def _pin_deployment_column(self, *_):
        header = self.horizontalHeader()
        if header.count() and header.visualIndex(0) != 0:
            blocked = header.blockSignals(True)
            header.moveSection(header.visualIndex(0), 0)
            header.blockSignals(blocked)
        self.sync_frozen()

    def sync_frozen(self, *_):
        self.frozen.setVisible(self.columnCount() > 0)
        for column in range(1, self.columnCount()):
            self.frozen.setColumnHidden(column, True)
        if self.columnCount():
            self.frozen.setColumnHidden(0, False)
            self.frozen.setColumnWidth(0, self.columnWidth(0))
            frozen_header = self.frozen.verticalHeader()
            for visual in range(self.rowCount()):
                logical = self.verticalHeader().logicalIndex(visual)
                current = frozen_header.visualIndex(logical)
                if current != visual:
                    frozen_header.moveSection(current, visual)
            for row in range(self.rowCount()):
                self.frozen.setRowHeight(row, self.rowHeight(row))
                self.frozen.setRowHidden(row, self.isRowHidden(row))
            viewport = self.viewport().geometry()
            header_height = self.horizontalHeader().height() if self.horizontalHeader().isVisible() else 0
            self.frozen.horizontalHeader().setFixedHeight(header_height)
            self.frozen.setGeometry(viewport.x(), viewport.y()-header_height, self.columnWidth(0), header_height+min(viewport.height(), sum(self.rowHeight(row) for row in range(self.rowCount()) if not self.isRowHidden(row))))
            self.frozen.verticalScrollBar().setValue(self.verticalScrollBar().value())
            self.frozen.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "frozen"):
            self.sync_frozen()

    def showEvent(self, event):
        super().showEvent(event)
        self.sync_frozen()

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        index = self.indexAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and index.isValid():
            self.activatedAt.emit(index.row(), index.column(), event.position().toPoint())

    def _set_hover(self, cell, pod=None):
        if cell == self.hovered and pod == self.hovered_pod:
            return
        self.hovered = cell
        self.hovered_pod = pod
        self.viewport().update()
        self.frozen.viewport().update()
        self.horizontalHeader().viewport().update()

    def _resize_edge(self, point):
        header = self.horizontalHeader()
        for column in range(self.columnCount()):
            if not self.isColumnHidden(column):
                edge = header.sectionViewportPosition(column) + self.columnWidth(column)
                if abs(point.x() - edge) <= 3:
                    return ("column", column, self.columnWidth(column))
        for row in range(self.rowCount()):
            if not self.isRowHidden(row):
                edge = self.rowViewportPosition(row) + self.rowHeight(row)
                if abs(point.y() - edge) <= 3:
                    return ("row", row, self.rowHeight(row))
        return None

    def eventFilter(self, watched, event):
        if watched in (self.viewport(), self.frozen.viewport()):
            kind = event.type()
            if kind in (QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
                point = watched.mapTo(self.viewport(), event.position().toPoint())
                edge = self._resize_edge(point)
                if watched is self.frozen.viewport() and abs(point.x()-self.frozen.width()) <= 3:
                    edge = ("column", 0, self.columnWidth(0))
                if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton and edge:
                    self._resize_drag = (*edge, event.globalPosition())
                    return True
                if self._resize_drag:
                    axis, section, size, origin = self._resize_drag
                    if kind == QEvent.Type.MouseMove:
                        delta = event.globalPosition() - origin
                        if axis == "column":
                            self.setColumnWidth(section, max(36, size + round(delta.x())))
                        else:
                            self.setRowHeight(section, max(30, size + round(delta.y())))
                        self.sync_frozen()
                    elif kind == QEvent.Type.MouseButtonRelease:
                        self._resize_drag = None
                    return True
                watched.setCursor(Qt.CursorShape.SplitHCursor if edge and edge[0] == "column" else Qt.CursorShape.SplitVCursor if edge else Qt.CursorShape.ArrowCursor)
        if watched is self.frozen.viewport() and event.type() == QEvent.Type.MouseMove:
            index = self.frozen.indexAt(event.position().toPoint())
            self._set_hover((index.row(), 0) if index.isValid() else (-1, -1))
        elif watched is self.viewport() and event.type() == QEvent.Type.Leave:
            self._set_hover((-1, -1))
        return super().eventFilter(watched, event)

    def mouseMoveEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        pods = index.data(Qt.ItemDataRole.UserRole) if index.isValid() else None
        hit = pod_at(self.visualRect(index), event.position().toPoint(), len(pods)) if isinstance(pods, list) else None
        self._set_hover((index.row(), index.column()) if index.isValid() else (-1, -1), hit)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._set_hover((-1, -1))
        super().leaveEvent(event)


def pod_status(pod):
    if pod.get("metadata", {}).get("deletionTimestamp"):
        return "Terminating"
    phase = pod.get("status", {}).get("phase", "Unknown")
    status = pod.get("status", {})
    containers = status.get("containerStatuses", []) + status.get("initContainerStatuses", [])
    if any(c.get("state", {}).get("waiting", {}).get("reason") in
           ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError", "RunContainerError")
           or c.get("state", {}).get("terminated", {}).get("exitCode", 0) != 0 for c in containers):
        return "Failed"
    if phase == "Running" and (any(c.get("ready") is False for c in status.get("containerStatuses", [])) or
            any(c.get("type") == "Ready" and c.get("status") != "True" for c in status.get("conditions", []))):
        return "Pending"
    return "Completed" if phase == "Succeeded" else phase


class NamespaceHeader(QHeaderView):
    def paintSection(self, painter, rect, logical):
        if logical == 0:
            return super().paintSection(painter, rect, logical)
        painter.save()
        painter.setClipRect(rect, Qt.ClipOperation.IntersectClip)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        label = str(self.model().headerData(logical, Qt.Orientation.Horizontal) or "")
        from .themes import theme
        from .view_tabs import blended_color
        colors = theme(self.palette().text().color().lightness() < 128)
        painter.fillRect(rect, QColor(blended_color(colors["editor"], self.parent().property("viewColor"))))
        if logical == getattr(self.parent(), "hovered", (-1, -1))[1]:
            tint = QColor(colors["accent"])
            tint.setAlpha(40)
            painter.fillRect(rect, tint)
        painter.setPen(QColor(colors["input_border"]))
        painter.drawLine(QPointF(rect.left(), rect.bottom()+0.5), QPointF(rect.right()+1, rect.bottom()+0.5))
        painter.drawLine(QPointF(rect.right()+0.5, rect.top()), QPointF(rect.right()+0.5, rect.bottom()+1))
        painter.setPen(QColor(colors["foreground"]))
        font = painter.font()
        font.setWeight(QFont.Weight.Bold)
        painter.setFont(font)
        painter.translate(rect.left(), rect.bottom())
        painter.rotate(-90)
        painter.drawText(QRect(6, 0, rect.height()-12, rect.width()), Qt.AlignmentFlag.AlignVCenter, label.split("\n")[-1])
        painter.restore()
        if self.model().headerData(logical, Qt.Orientation.Horizontal, Qt.ItemDataRole.UserRole):
            painter.save()
            painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
            painter.setPen(QColor(colors["input_border"]))
            painter.drawLine(rect.right()-1, rect.top(), rect.right()-1, rect.bottom())
            painter.restore()


class PodDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        pods = index.data(Qt.ItemDataRole.UserRole)
        # Missing deployments still occupy a visual cell in the matrix.
        if pods is None:
            pods = []
        painter.save()
        painter.setClipRect(option.rect, Qt.ClipOperation.IntersectClip)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        from .themes import theme
        colors = theme(option.palette.text().color().lightness() < 128)
        painter.fillRect(option.rect, option.palette.base())
        matrix = self.parent() if isinstance(self.parent(), PodMatrix) else option.widget
        hovered = getattr(matrix, "hovered", (-1, -1))
        active = (index.row(), index.column()) == hovered
        in_cross = index.row() == hovered[0] or index.column() == hovered[1]
        if in_cross:
            hover = QColor("#8576B2")
            hover.setAlpha(85 if active else 38)
            painter.fillRect(option.rect, hover)
        painter.setPen(QColor(colors["input_border"]))
        painter.drawLine(QPointF(option.rect.left(), option.rect.bottom()+0.5), QPointF(option.rect.right()+1, option.rect.bottom()+0.5))
        painter.drawLine(QPointF(option.rect.right()+0.5, option.rect.top()), QPointF(option.rect.right()+0.5, option.rect.bottom()+1))
        if index.column() == 0:
            painter.setPen(QColor(colors["foreground"]))
            font = painter.font()
            font.setWeight(QFont.Weight.Bold)
            painter.setFont(font)
            painter.drawText(option.rect.adjusted(10, 3, -8, -3), Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap, str(index.data() or ""))
            painter.restore()
            return
        if (index.row(), index.column()) == hovered:
            painter.setPen(QColor(colors["accent"]))
            painter.drawRect(option.rect.adjusted(0, 0, -1, -1))
        if not pods:
            painter.setPen(QColor(colors["muted"]))
            painter.drawText(option.rect, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, str(index.data() or "—"))
        for number, (area, pod) in enumerate(zip(pod_rects(option.rect, len(pods)), pods)):
            painter.setBrush(QColor(COLORS["Unknown"] if pod.get("_stale") else COLORS.get(pod_status(pod), COLORS["Unknown"])))
            painter.setPen(option.palette.highlight().color() if pod.get("_recent") else Qt.PenStyle.NoPen)
            painter.drawRoundedRect(area, 4, 4)
            if (index.row(), index.column()) == hovered and number == getattr(matrix, "hovered_pod", None):
                painter.setBrush(Qt.BrushStyle.NoBrush)
                glow = QColor(colors["accent"])
                glow.setAlpha(65)
                painter.setPen(QPen(glow, 6))
                painter.drawRoundedRect(area.adjusted(-3, -3, 3, 3), 5, 5)
                painter.setPen(QPen(QColor(colors["foreground"]), 1))
                painter.drawRoundedRect(area.adjusted(-2, -2, 2, 2), 4, 4)
        if index.model().headerData(index.column(), Qt.Orientation.Horizontal, Qt.ItemDataRole.UserRole):
            painter.setPen(QColor(colors["input_border"]))
            painter.drawLine(option.rect.right()-1, option.rect.top(), option.rect.right()-1, option.rect.bottom())
        painter.restore()

    def helpEvent(self, event, view, option, index):
        pods = index.data(Qt.ItemDataRole.UserRole)
        if pods is not None:
            hit = pod_at(option.rect, event.pos(), len(pods))
            if hit is not None:
                pod = pods[hit]
                meta = pod.get("metadata", {})
                restarts = sum(c.get("restartCount", 0) for c in pod.get("status", {}).get("containerStatuses", []))
                QToolTip.showText(event.globalPos(), f"Name: {meta.get('name', '')}\nStatus: {pod_status(pod)}\nRestarts: {restarts}\nCreated: {meta.get('creationTimestamp', '—')}", view)
                return True
        return super().helpEvent(event, view, option, index)


class LineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def toPlainText(self):
        return "\n".join(map(str, range(1, self.editor.blockCount()+1)))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(event.rect(), self.editor.palette().base())
        from .themes import theme
        colors = theme(self.editor.palette().text().color().lightness() < 128)
        painter.setPen(QColor(colors["line_number"]))
        painter.setFont(self.editor.font())
        block = self.editor.firstVisibleBlock()
        while block.isValid():
            top = self.editor.blockBoundingGeometry(block).translated(self.editor.contentOffset()).top()
            height = self.editor.blockBoundingRect(block).height()
            if top > event.rect().bottom():
                break
            if block.isVisible() and top + height >= event.rect().top():
                painter.drawText(0, round(top), self.width()-8, self.editor.fontMetrics().height(), Qt.AlignmentFlag.AlignRight, str(block.blockNumber()+1))
            block = block.next()


class NumberedEditor(QPlainTextEdit):
    def __init__(self):
        super().__init__()
        self.gutter = LineNumberArea(self)
        self.blockCountChanged.connect(self.update_gutter)
        self.updateRequest.connect(lambda *_: self.gutter.update())
        self.update_gutter()

    def update_gutter(self, *_):
        width = 16 + self.fontMetrics().horizontalAdvance("9")*len(str(max(1, self.blockCount())))
        self.setViewportMargins(width, 0, 0, 0)
        viewport = self.viewport().geometry()
        self.gutter.setGeometry(self.frameWidth(), viewport.y(), width, viewport.height())
        self.gutter.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_gutter()


class CodeEditor(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.setContentsMargins(8, 4, 8, 4)
        row.addStretch()
        self.search = QLineEdit()
        self.search.setMaximumWidth(260)
        self.search.setClearButtonEnabled(True)
        self.search.setPlaceholderText("Поиск…")
        row.addWidget(self.search)
        self.count = QLabel("0/0")
        row.addWidget(self.count)
        for label, backwards in (("↑", True), ("↓", False)):
            button = QPushButton(label)
            button.clicked.connect(lambda _, back=backwards: self.find_match(back))
            row.addWidget(button)
        layout.addLayout(row)
        body = QHBoxLayout()
        self.editor = NumberedEditor()
        self.numbers = self.editor.gutter
        self.editor.setObjectName("codeEditorText")
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.editor.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.editor.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        from .themes import code_font
        self.editor.setFont(code_font())
        self.numbers.setFont(self.editor.font())
        body.addWidget(self.editor, 1)
        layout.addLayout(body)
        self.editor.textChanged.connect(self.update_lines)
        self.search.returnPressed.connect(self.find_match)
        self.search.textChanged.connect(self.update_matches)
        self.update_lines()

    def update_lines(self):
        self.editor.update_gutter()
        self.update_matches()

    def update_matches(self):
        needle = self.search.text()
        total = self.toPlainText().casefold().count(needle.casefold()) if needle else 0
        self.count.setText(f"0/{total}")

    def find_match(self, backwards=False):
        from PyQt6.QtGui import QTextDocument
        flags = QTextDocument.FindFlag.FindBackward if backwards else QTextDocument.FindFlag(0)
        if not self.editor.find(self.search.text(), flags):
            self.editor.moveCursor(QTextCursor.MoveOperation.End if backwards else QTextCursor.MoveOperation.Start)
            self.editor.find(self.search.text(), flags)
        needle = self.search.text()
        if needle:
            before = self.toPlainText()[:self.editor.textCursor().selectionEnd()]
            self.count.setText(f"{before.casefold().count(needle.casefold())}/{self.toPlainText().casefold().count(needle.casefold())}")

    def setPlainText(self, value):
        self.editor.setPlainText(value)

    def toPlainText(self):
        return self.editor.toPlainText()


def stylesheet(light=False):
    from .themes import stylesheet as make_stylesheet
    return make_stylesheet(light)
