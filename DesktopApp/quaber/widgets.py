"""Native widgets matching the web matrix and resource editor."""
import re
from pathlib import Path
from datetime import datetime, timezone
from PyQt5.QtCore import Qt, QSize, QRect, QRectF, QPointF, QEvent, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen, QFont, QTextCursor, QFontDatabase
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QLabel,
    QPushButton, QPlainTextEdit, QHeaderView, QStyledItemDelegate, QMenu, QTableWidget, QTableView, QAbstractItemView, QToolTip, QCheckBox, QFrame, QComboBox, QListView, QApplication, QTabWidget)

COLORS = {"Running": "#43B694", "Pending": "#DCAE54", "Failed": "#E56B6E",
          "Unknown": "#70819d", "Completed": "#729fc7", "Terminating": "#8d94a0", "Restart": "#51A8EA"}

WORKLOAD_HELP = {
    "Deployment": "Deployment управляет числом реплик приложения и их обновлением через ReplicaSet.",
    "StatefulSet": "StatefulSet управляет Pod со стабильными именами и хранилищами.",
    "DaemonSet": "DaemonSet запускает Pod на подходящих узлах кластера.",
    "ReplicaSet": "ReplicaSet поддерживает заданное число реплик Pod.",
    "ReplicationController": "ReplicationController поддерживает заданное число реплик Pod.",
    "Job": "Job запускает задачу до завершения.",
    "CronJob": "CronJob создаёт Job по расписанию.",
    "Pod": "Отдельные Pod, включая статические Pod, не сгруппированные под контроллером нагрузки.",
}


class DisabledHintButton(QPushButton):
    """A disabled operation stays inert, but explains a click immediately."""
    def event(self, event):
        if event.type() == QEvent.Type.MouseButtonPress and not self.isEnabled():
            if self.toolTip():
                QToolTip.showText(event.globalPos(), self.toolTip(), self)
            return True
        return super().event(event)


class ElidedButton(QPushButton):
    """Keep the complete target in a tooltip without expanding the dialog."""
    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.full_text = text
        self.setMinimumWidth(0)
        from PyQt5.QtWidgets import QSizePolicy
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setToolTip(text)
        self.setAccessibleName(text)

    def resizeEvent(self, event):
        self.setText(self.fontMetrics().elidedText(self.full_text, Qt.TextElideMode.ElideMiddle, max(20, self.width()-28)))
        super().resizeEvent(event)


class PodDonut(QWidget):
    """Observed Pod states, independent of the desired replica count."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(180, 180)
        self.setMaximumSize(230, 230)
        self.counts = None
        self.desired = None
        self.requesting = False
        self.setToolTip("Загрузка состояний Pod этого Deployment…")

    def set_pods(self, pods, desired=None, *, requesting=False):
        self.desired = desired
        self.requesting = requesting
        self.counts = {}
        for pod in pods:
            status = pod_status(pod)
            self.counts[status] = self.counts.get(status, 0) + 1
        summary = f"Готово: {self.counts.get('Running', 0)}. Всего Pod: {sum(self.counts.values())}."
        if desired is not None:
            summary += f" Цель: {desired}."
        self.setAccessibleName(summary)
        self.setToolTip(summary + "\n" + ("\n".join(f"{status}: {count}" for status, count in self.counts.items()) or "Pod отсутствуют."))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = QRectF(self.rect()).adjusted(18, 18, -18, -18)
        side = min(area.width(), area.height())
        area = QRectF(area.center().x()-side/2, area.center().y()-side/2, side, side)
        total = sum(self.counts.values()) if self.counts is not None else None
        painter.setPen(QPen(self.palette().mid().color(), 15))
        painter.drawEllipse(area)
        start = 90 * 16
        for status, count in (self.counts or {}).items():
            span = round(count / total * 360 * 16) if total else 0
            painter.setPen(QPen(QColor(COLORS.get(status, COLORS["Unknown"])), 15))
            painter.drawArc(area, start, -span)
            start -= span
        font = painter.font()
        font.setPixelSize(30)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(self.palette().text().color())
        current = total
        painter.drawText(area.adjusted(0, -12, 0, -12), Qt.AlignmentFlag.AlignCenter, str(current) if current is not None else "—")
        font.setPixelSize(13)
        font.setBold(False)
        painter.setFont(font)
        subtitle = f"Запрос: {self.desired}" if self.requesting else f"Переход к {self.desired}" if self.desired is not None and current != self.desired else "Pods"
        painter.drawText(area.adjusted(0, 42, 0, 42), Qt.AlignmentFlag.AlignCenter, subtitle)


class FullCheckBox(QCheckBox):
    def paintEvent(self, event):
        super().paintEvent(event)
        color = COLORS.get(self.property("status"))
        if self.objectName() == "recentFilter":
            color = COLORS["Restart"]
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


class MultiSelectMenu(QMenu):
    """Checkbox choices stay open for both mouse and keyboard selection."""
    def __init__(self, parent=None):
        super().__init__(parent)
        font = self.font()
        font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        self.setFont(font)
        self.setObjectName("multiSelectMenu")

    def mouseReleaseEvent(self, event):
        action = self.actionAt(event.pos())
        if event.button() == Qt.MouseButton.LeftButton and action and action.isEnabled() and action.isCheckable():
            action.trigger()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        action = self.activeAction()
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter) and action and action.isEnabled() and action.isCheckable():
            action.trigger()
            event.accept()
            return
        super().keyPressEvent(event)


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
        from PyQt5.QtWidgets import QStyle
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        from PyQt5.QtWidgets import QStyleOptionViewItem
        background_option = QStyleOptionViewItem(option)
        self.initStyleOption(background_option, index)
        background_option.text = ""
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, background_option, painter, option.widget)
        painter.save()
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        color = QColor("#FFFFFF" if selected else COLORS.get(status, COLORS["Unknown"]))
        area = option.rect.adjusted(6, 5, -6, -5)
        background = QColor(color)
        background.setAlpha(35)
        painter.setBrush(background)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(area, 5, 5)
        painter.setPen(color)
        painter.drawText(area, Qt.AlignmentFlag.AlignCenter, status)
        painter.restore()


class ResourceTabs(QTabWidget):
    def paintEvent(self, event):
        super().paintEvent(event)
        if self.property("writeEnabled"):
            from .themes import write_background
            bar = self.tabBar().geometry()
            painter = QPainter(self)
            painter.fillRect(QRect(bar.right()+1, bar.top(), max(0, self.width()-bar.right()-1), bar.height()),
                             QColor(write_background(self.palette().text().color().lightness() < 128)))
            painter.end()


class MatrixHeader(QHeaderView):
    """Only the bottom edge changes the shared matrix header height."""
    def __init__(self, orientation, parent=None, matrix=None):
        super().__init__(orientation, parent)
        self.matrix = matrix if matrix is not None else parent
        self._height_drag = None
        self.setMouseTracking(True)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and event.localPos().y() >= self.height()-4:
            self._height_drag = (event.screenPos().y(), self.height())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._height_drag is not None:
            origin, height = self._height_drag
            height = max(54, min(360, height+round(event.screenPos().y()-origin)))
            self.matrix.horizontalHeader().setFixedHeight(height)
            self.matrix.sync_frozen()
            self.matrix.headerHeightChanged.emit(height)
            return
        if event.localPos().y() >= self.height()-4:
            self.viewport().setCursor(Qt.CursorShape.SplitVCursor)
        else:
            self.viewport().unsetCursor()
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._height_drag is not None:
            self._height_drag = None
            event.accept()
            return
        super().mouseReleaseEvent(event)


class MatrixGroupHeaders(QWidget):
    def __init__(self, matrix):
        super().__init__(matrix)
        self.matrix = matrix
        self.setObjectName("matrixGroupHeaders")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setStyleSheet("background:transparent;")

    def paintEvent(self, event):
        from .themes import theme
        matrix = self.matrix
        colors = theme(matrix.palette().text().color().lightness() < 128)
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        font = matrix.font()
        font.setBold(True)
        painter.setFont(font)
        for row in getattr(matrix, "group_rows", set()):
            rect = QRect(0, matrix.rowViewportPosition(row), matrix.group_width(), matrix.GROUP_HEIGHT)
            if not rect.intersects(event.rect()):
                continue
            painter.fillRect(rect, QColor(colors["sidebar"]))
            painter.setPen(QPen(QColor(colors["muted"]), 2))
            painter.drawLine(rect.left(), rect.top()+1, rect.right(), rect.top()+1)
            painter.setPen(QColor(colors["foreground"]))
            painter.drawText(rect.adjusted(4, 2, -4, 0), Qt.AlignmentFlag.AlignCenter, matrix.row_kinds[row])


class PodMatrix(QTableWidget):
    activatedAt = pyqtSignal(int, int, object)
    headerHeightChanged = pyqtSignal(int)
    GROUP_HEIGHT = 26

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setShowGrid(False)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.verticalHeader().hide()
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.hovered = (-1, -1)
        self.hovered_pod = None
        self._resize_drag = None
        self._row_drag = None
        self.frozen = QTableView(self)
        self.frozen.setObjectName("frozenDeployment")
        self.frozen.setShowGrid(False)
        self.frozen.setFrameShape(QFrame.Shape.NoFrame)
        self.frozen.setHorizontalHeader(MatrixHeader(Qt.Orientation.Horizontal, self.frozen, self))
        self.frozen.horizontalHeader().setMinimumSectionSize(0)
        self.frozen.setMouseTracking(True)
        self.frozen.viewport().installEventFilter(self)
        self.viewport().installEventFilter(self)
        self.frozen.setModel(self.model())
        self.frozen.setSelectionModel(self.selectionModel())
        self.frozen.setItemDelegate(PodDelegate(self))
        self.frozen.horizontalHeader().setStretchLastSection(False)
        self.frozen.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.frozen.horizontalHeader().sectionResized.connect(self._resize_name_column)
        self.frozen.horizontalHeader().setSectionsMovable(False)
        self.frozen.horizontalHeader().show()
        self.frozen.verticalHeader().hide()
        self.group_headers = MatrixGroupHeaders(self)
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
        self.verticalScrollBar().valueChanged.connect(self.group_headers.update)
        self.horizontalScrollBar().valueChanged.connect(self.group_headers.update)
        self.horizontalHeader().sectionMoved.connect(self._pin_deployment_column)
        self.verticalHeader().sectionMoved.connect(self.sync_frozen)

    def setHorizontalHeader(self, header):
        super().setHorizontalHeader(header)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        header.sectionMoved.connect(self._pin_deployment_column)
        header.sectionResized.connect(self.sync_frozen)

    def _resize_name_column(self, column, old, width):
        if column == 0 and not getattr(self, "_syncing_frozen", False):
            self.setColumnWidth(0, max(100, min(500, width)))

    def _pin_deployment_column(self, *_):
        header = self.horizontalHeader()
        if header.count() and header.visualIndex(0) != 0:
            blocked = header.blockSignals(True)
            header.moveSection(header.visualIndex(0), 0)
            header.blockSignals(blocked)
        self.sync_frozen()

    def sync_frozen(self, *_):
        if getattr(self, "_syncing_frozen", False):
            return
        self._syncing_frozen = True
        kinds = getattr(self, "row_kinds", [])
        self.group_rows = set()
        for visual in range(min(len(kinds), self.rowCount())):
            row = self.verticalHeader().logicalIndex(visual)
            previous = self.verticalHeader().logicalIndex(visual-1) if visual else None
            if row < len(kinds) and (previous is None or kinds[row] != kinds[previous]):
                self.group_rows.add(row)
        previous_groups = getattr(self, "_height_groups", set())
        if self.group_rows != previous_groups:
            header = self.verticalHeader()
            blocked = header.blockSignals(True)
            for row in self.group_rows | previous_groups:
                if row < self.rowCount():
                    delta = (int(row in self.group_rows)-int(row in previous_groups))*self.GROUP_HEIGHT
                    self.setRowHeight(row, max(30+self.group_offset(row), self.rowHeight(row)+delta))
            header.blockSignals(blocked)
            self._height_groups = set(self.group_rows)
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
        self.group_headers.setGeometry(self.viewport().geometry())
        self.group_headers.raise_()
        self.group_headers.update()
        self._syncing_frozen = False

    def group_offset(self, row):
        return self.GROUP_HEIGHT if row in getattr(self, "group_rows", set()) else 0

    def content_rect(self, rect, row):
        return rect.adjusted(0, self.group_offset(row), 0, 0)

    def group_width(self):
        return min(self.viewport().width(), sum(self.columnWidth(c) for c in range(self.columnCount()) if not self.isColumnHidden(c)))

    def group_at(self, point):
        if 0 <= point.x() < self.group_width():
            for row in getattr(self, "group_rows", set()):
                if self.rowViewportPosition(row) <= point.y() < self.rowViewportPosition(row)+self.GROUP_HEIGHT:
                    return row
        return None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "frozen"):
            self.sync_frozen()

    def showEvent(self, event):
        super().showEvent(event)
        self.sync_frozen()

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        index = self.indexAt(event.pos())
        if event.button() == Qt.MouseButton.LeftButton and index.isValid() and self.content_rect(self.visualRect(index), index.row()).contains(event.pos()):
            self.activatedAt.emit(index.row(), index.column(), event.pos())

    def _set_hover(self, cell, pod=None):
        if cell == self.hovered and pod == self.hovered_pod:
            return
        self.hovered = cell
        self.hovered_pod = pod
        self.viewport().update()
        self.frozen.viewport().update()
        self.horizontalHeader().viewport().update()

    def _resize_edge(self, point):
        if self.columnCount() and abs(point.x()-self.columnWidth(0)) <= 3:
            return ("column", 0, self.columnWidth(0))
        return None

    def eventFilter(self, watched, event):
        if watched in (self.viewport(), self.frozen.viewport()):
            kind = event.type()
            if kind in (QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                        QEvent.Type.MouseButtonDblClick, QEvent.Type.ToolTip):
                local = event.pos()
                point = local if watched is self.viewport() else self.viewport().mapFromGlobal(watched.mapToGlobal(local))
                group = self.group_at(point)
                if group is not None and not self._resize_drag and not self._row_drag and not self._resize_edge(point):
                    self._set_hover((-1, -1))
                    watched.setCursor(Qt.CursorShape.ArrowCursor)
                    if kind == QEvent.Type.ToolTip:
                        name = self.row_kinds[group]
                        QToolTip.showText(event.globalPos(), WORKLOAD_HELP.get(name, name), watched)
                    return True
            if kind in (QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
                point = (event.pos() if watched is self.viewport() else
                         self.viewport().mapFromGlobal(watched.mapToGlobal(event.pos())))
                edge = self._resize_edge(point)
                if watched is self.frozen.viewport() and abs(point.x()-self.frozen.width()) <= 3:
                    edge = ("column", 0, self.columnWidth(0))
                if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton and edge:
                    self._resize_drag = (*edge, event.screenPos())
                    return True
                if self._resize_drag:
                    axis, section, size, origin = self._resize_drag
                    if kind == QEvent.Type.MouseMove:
                        delta = event.screenPos() - origin
                        if axis == "column":
                            self.setColumnWidth(section, max(100, min(500, size + round(delta.x()))))
                        else:
                            self.setRowHeight(section, max(30, size + round(delta.y())))
                        self.sync_frozen()
                    elif kind == QEvent.Type.MouseButtonRelease:
                        self._resize_drag = None
                    return True
                if watched is self.frozen.viewport():
                    index = self.frozen.indexAt(event.pos())
                    if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton and index.isValid():
                        if self.content_rect(self.frozen.visualRect(index), index.row()).contains(event.pos()):
                            self._row_drag = (index.row(), event.pos(), False)
                    if kind == QEvent.Type.MouseMove and self._row_drag and event.buttons() & Qt.MouseButton.LeftButton:
                        row, origin, active = self._row_drag
                        active = active or (event.pos()-origin).manhattanLength() >= QApplication.startDragDistance()
                        self._row_drag = (row, origin, active)
                        if active:
                            watched.setCursor(Qt.CursorShape.ClosedHandCursor)
                            return True
                    if kind == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                        drag, self._row_drag = self._row_drag, None
                        if drag and drag[2]:
                            kinds = getattr(self, "row_kinds", [])
                            if index.isValid() and drag[0] < len(kinds) and kinds[drag[0]] == kinds[index.row()]:
                                header = self.verticalHeader()
                                header.moveSection(header.visualIndex(drag[0]), header.visualIndex(index.row()))
                            watched.setCursor(Qt.CursorShape.ArrowCursor)
                            return True
                watched.setCursor(Qt.CursorShape.SplitHCursor if edge and edge[0] == "column" else Qt.CursorShape.SplitVCursor if edge else Qt.CursorShape.ArrowCursor)
                if watched is self.frozen.viewport() and kind == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                    index = self.frozen.indexAt(event.pos())
                    if index.isValid() and self.content_rect(self.frozen.visualRect(index), index.row()).contains(event.pos()):
                        self.activatedAt.emit(index.row(), 0, None)
                    return True
        if watched is self.frozen.viewport() and event.type() == QEvent.Type.MouseMove:
            index = self.frozen.indexAt(event.pos())
            self._set_hover((index.row(), 0) if index.isValid() else (-1, -1))
        elif watched is self.viewport() and event.type() == QEvent.Type.Leave:
            self._set_hover((-1, -1))
        return super().eventFilter(watched, event)

    def mouseMoveEvent(self, event):
        index = self.indexAt(event.pos())
        pods = index.data(Qt.ItemDataRole.UserRole) if index.isValid() else None
        hit = pod_at(self.content_rect(self.visualRect(index), index.row()), event.pos(), len(pods)) if isinstance(pods, list) else None
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


class NamespaceHeader(MatrixHeader):
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
        lines = [painter.fontMetrics().elidedText(part, Qt.TextElideMode.ElideRight, rect.height()-12)
                 for part in label.split("\n")]
        painter.drawText(QRect(6, 0, rect.height()-12, rect.width()), Qt.AlignmentFlag.AlignVCenter, "\n".join(lines))
        painter.restore()
        if self.model().headerData(logical, Qt.Orientation.Horizontal, Qt.ItemDataRole.UserRole):
            painter.save()
            painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
            painter.setPen(QColor(colors["input_border"]))
            painter.drawLine(rect.right()-1, rect.top(), rect.right()-1, rect.bottom())
            painter.restore()


def pod_marker_style(pod, deploy_mode=False):
    """Keep filtering activity separate from its temporary visual suppression."""
    status = pod_status(pod)
    if pod.get("_stale"):
        return COLORS["Unknown"], False
    recent = bool(pod.get("_recent")) and status == "Running" and not deploy_mode
    return COLORS["Restart"] if recent else COLORS.get(status, COLORS["Unknown"]), recent


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
        in_cross = index.row() == hovered[0] or (hovered[1] > 0 and index.column() == hovered[1])
        if in_cross:
            hover = QColor("#8576B2")
            hover.setAlpha(85 if active else 38)
            painter.fillRect(option.rect, hover)
        if index.row() in getattr(matrix, "group_rows", set()):
            banner = QRect(option.rect.left(), option.rect.top(), option.rect.width(), matrix.GROUP_HEIGHT)
            painter.fillRect(banner, QColor(colors["sidebar"]))
            painter.setPen(QPen(QColor(colors["muted"]), 3))
            painter.drawLine(option.rect.left(), option.rect.top()+1, option.rect.right(), option.rect.top()+1)
        content = matrix.content_rect(option.rect, index.row()) if isinstance(matrix, PodMatrix) else option.rect
        painter.setPen(QColor(colors["input_border"]))
        painter.drawLine(QPointF(option.rect.left(), option.rect.bottom()+0.5), QPointF(option.rect.right()+1, option.rect.bottom()+0.5))
        painter.drawLine(QPointF(option.rect.right()+0.5, option.rect.top()), QPointF(option.rect.right()+0.5, option.rect.bottom()+1))
        if index.column() == 0:
            painter.setPen(QColor(colors["foreground"]))
            font = painter.font()
            font.setWeight(QFont.Weight.Bold)
            painter.setFont(font)
            painter.drawText(content.adjusted(10, 3, -8, -3), Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap, str(index.data() or ""))
            painter.restore()
            return
        if (index.row(), index.column()) == hovered:
            painter.setPen(QColor(colors["accent"]))
            painter.drawRect(content.adjusted(0, 0, -1, -1))
        if not pods:
            painter.setPen(QColor(colors["muted"]))
            painter.drawText(content.adjusted(3, 3, -3, -3), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWrapAnywhere, str(index.data() or "—"))
        for number, (area, pod) in enumerate(zip(pod_rects(content, len(pods)), pods)):
            color, recent = pod_marker_style(pod, bool(matrix.property("deployMode")))
            painter.setBrush(QColor(color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(area, 4, 4)
            if recent:
                painter.setPen(QPen(QColor("#FFFFFF"), 1.2))
                painter.drawArc(QRectF(area).adjusted(3, 3, -3, -3), 35*16, 285*16)
                x, y = area.right()-3, area.top()+3
                painter.drawLine(QPointF(x-3, y), QPointF(x, y))
                painter.drawLine(QPointF(x, y), QPointF(x, y+3))
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
            matrix = self.parent()
            area = matrix.content_rect(option.rect, index.row()) if isinstance(matrix, PodMatrix) else option.rect
            hit = pod_at(area, event.pos(), len(pods))
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
        numbers = getattr(self.editor, "line_numbers", None)
        return "\n".join(str(n) if n is not None else "" for n in numbers) if numbers is not None else "\n".join(map(str, range(1, self.editor.blockCount()+1)))

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
                numbers = getattr(self.editor, "line_numbers", None)
                number = numbers[block.blockNumber()] if numbers is not None and block.blockNumber() < len(numbers) else block.blockNumber()+1
                painter.drawText(0, round(top), self.width()-8, self.editor.fontMetrics().height(), Qt.AlignmentFlag.AlignRight, str(number) if number is not None else "")
            block = block.next()


class NumberedEditor(QPlainTextEdit):
    def __init__(self):
        super().__init__()
        self.gutter = LineNumberArea(self)
        self.blockCountChanged.connect(self.update_gutter)
        self.updateRequest.connect(lambda *_: self.gutter.update())
        self.update_gutter()

    def update_gutter(self, *_):
        numbers = getattr(self, "line_numbers", None)
        largest = max((n or 0 for n in numbers), default=1) if numbers is not None else self.blockCount()
        width = 16 + self.fontMetrics().horizontalAdvance("9")*len(str(max(1, largest)))
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
        self.toolbar = row
        row.setContentsMargins(6, 2, 6, 2)
        row.setSpacing(4)
        row.addStretch()
        self.search_button = QPushButton("Поиск")
        self.search_button.setToolTip("Поиск в YAML · Ctrl+F")
        self.search_button.clicked.connect(self.show_search)
        row.addWidget(self.search_button)
        layout.addLayout(row)
        body = QHBoxLayout()
        self.editor = NumberedEditor()
        self.editor.installEventFilter(self)
        self.search_panel = QFrame(self.editor)
        self.search_panel.setObjectName("editorFind")
        find_row = QHBoxLayout(self.search_panel)
        find_row.setContentsMargins(6, 6, 6, 6)
        self.search = QLineEdit()
        self.search.setMinimumWidth(0)
        self.search.setClearButtonEnabled(True)
        self.search.setPlaceholderText("Поиск…")
        self.search.installEventFilter(self)
        find_row.addWidget(self.search, 1)
        self.count = QLabel("0/0")
        find_row.addWidget(self.count)
        for label, backwards in (("↑", True), ("↓", False)):
            button = QPushButton(label)
            button.clicked.connect(lambda _, back=backwards: self.find_match(back))
            find_row.addWidget(button)
        close = QPushButton("×")
        close.setToolTip("Закрыть поиск · Esc")
        close.clicked.connect(self.hide_search)
        find_row.addWidget(close)
        self.search_panel.hide()
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

    def show_search(self):
        self.search_panel.show()
        self._position_search()
        self.search_panel.raise_()
        self.search.setFocus()
        self.search.selectAll()

    def hide_search(self):
        self.search_panel.hide()
        self.editor.setFocus()

    def _position_search(self):
        width = min(410, max(180, self.editor.viewport().width()-12))
        self.search_panel.setGeometry(max(0, self.editor.width()-width-16), 4, width, self.search_panel.sizeHint().height())

    def eventFilter(self, watched, event):
        if watched is self.editor and event.type() == QEvent.Type.Resize:
            self._position_search()
        if event.type() in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress):
            find = event.key() == Qt.Key.Key_F and event.modifiers() & Qt.KeyboardModifier.ControlModifier
            close = event.key() == Qt.Key.Key_Escape and self.search_panel.isVisible()
            if find or close:
                event.accept()
                if event.type() == QEvent.Type.KeyPress:
                    self.show_search() if find else self.hide_search()
                return True
        return super().eventFilter(watched, event)

    def update_lines(self):
        self.editor.update_gutter()
        self.update_matches()

    def update_matches(self):
        needle = self.search.text()
        total = self.toPlainText().casefold().count(needle.casefold()) if needle else 0
        self.count.setText(f"0/{total}")

    def find_match(self, backwards=False):
        from PyQt5.QtGui import QTextDocument
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
