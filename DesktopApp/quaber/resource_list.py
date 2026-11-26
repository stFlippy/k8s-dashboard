"""Flat resource lists with native row selection and existing cell metadata."""
from PyQt5.QtCore import QRect, Qt, pyqtSignal
from PyQt5.QtGui import QKeySequence, QColor, QPainter
from PyQt5.QtWidgets import (QTreeWidget, QTreeWidgetItem, QHeaderView, QAbstractItemView,
    QLabel, QStyle, QStyleOptionViewItem, QStyledItemDelegate, QLineEdit, QMenu, QApplication)


class ResourceHeader(QHeaderView):
    manuallyResized = pyqtSignal(int)

    def __init__(self, parent):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self._edge = self._drag_column = -1

    def _resizable(self, column):
        return (self.sectionResizeMode(column) == QHeaderView.ResizeMode.Interactive
                and not (self.stretchLastSection() and self.visualIndex(column) == self.count()-1))

    def _edge_at(self, position):
        return next((column for column in range(self.count()) if not self.isSectionHidden(column) and self._resizable(column)
                     and abs(position.x()-(self.sectionViewportPosition(column)+self.sectionSize(column))) <= 4), -1)

    def paintSection(self, painter, rect, column):
        super().paintSection(painter, rect, column)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self.viewport())
        for column in range(self.count()):
            if self.isSectionHidden(column) or not self._resizable(column):
                continue
            active = column in (self._edge, self._drag_column)
            color = QColor("#005FB8" if self.palette().base().color().lightness() > 128 else "#65ABFF") if active else self.palette().dark().color()
            width = 2 if active else 1
            right = self.sectionViewportPosition(column) + self.sectionSize(column) - 1
            painter.fillRect(QRect(right-width+1, 6, width, max(0, self.height()-12)), color)

    def mouseMoveEvent(self, event):
        edge = self._edge_at(event.localPos())
        if edge != self._edge:
            self._edge = edge
            self.viewport().update()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        self._drag_column = self._edge_at(event.localPos()) if event.button() == Qt.MouseButton.LeftButton else -1
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        column, self._drag_column = self._drag_column, -1
        super().mouseReleaseEvent(event)
        if column >= 0:
            self.manuallyResized.emit(column)
        self.viewport().update()

    def leaveEvent(self, event):
        self._edge = -1
        self.viewport().update()
        super().leaveEvent(event)


class CopyTextDelegate(QStyledItemDelegate):
    def __init__(self, columns, parent):
        super().__init__(parent)
        self.columns = columns

    def createEditor(self, parent, option, index):
        if index.column() not in self.columns:
            return None
        editor = QLineEdit(parent)
        editor.setObjectName("copyField")
        editor.setReadOnly(True)
        editor.setFrame(False)
        return editor

    def setEditorData(self, editor, index):
        editor.setText(str(index.data() or ""))
        editor.selectAll()

    def setModelData(self, editor, model, index):
        pass  # A copying field can never change connection settings.


class ResourceCell:
    def __init__(self, entry, column):
        self.entry, self.column = entry, column

    def text(self):
        return self.entry.text(self.column)

    def setText(self, text):
        self.entry.setText(self.column, text)

    def data(self, role):
        return self.entry.data(self.column, role)

    def setData(self, role, value):
        self.entry.setData(self.column, role, value)

    def setToolTip(self, text):
        self.entry.setToolTip(self.column, text)


class ResourceList(QTreeWidget):
    cellDoubleClicked = pyqtSignal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("resourceList")
        self._batch_depth = 0
        self._batch_dirty = False
        for signal in (self.model().dataChanged, self.model().headerDataChanged,
                       self.model().rowsAboutToBeInserted, self.model().rowsAboutToBeRemoved,
                       self.model().modelAboutToBeReset, self.model().layoutAboutToBeChanged):
            signal.connect(self._mark_batch_changed)
        self.setHeader(ResourceHeader(self))
        self._manual_widths = {}
        self.header().manuallyResized.connect(self._remember_width)
        self.setMouseTracking(True)
        self._hovered_item = None
        self.setRootIsDecorated(False)
        self.setItemsExpandable(False)
        self.setIndentation(0)
        self.setAlternatingRowColors(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.header().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.header().setMinimumSectionSize(80)
        self.header().setStretchLastSection(True)
        self.itemDoubleClicked.connect(lambda entry, column: self.cellDoubleClicked.emit(self.indexOfTopLevelItem(entry), column))
        self.empty_text = "Нет данных."
        self.empty_label = QLabel(self.empty_text, self.viewport())
        self.empty_label.setObjectName("emptyState")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setTextFormat(Qt.TextFormat.PlainText)
        self.empty_label.setWordWrap(True)
        self.empty_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._update_empty()

    def _mark_batch_changed(self, *args):
        if self._batch_depth and not self._batch_dirty:
            self._batch_dirty = True
            self.setUpdatesEnabled(False)

    def begin_batch(self):
        if not self._batch_depth:
            self._batch_dirty = False
        self._batch_depth += 1

    def end_batch(self):
        self._batch_depth -= 1

    def enable_text_copy(self, columns):
        """Allow read-only text selection for named columns, preserving row actions."""
        self._copy_columns = dict(columns)
        self.setItemDelegate(CopyTextDelegate(self._copy_columns, self))
        self.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._copy_menu)
        for row in range(self.rowCount()):
            entry = self.topLevelItem(row)
            entry.setFlags(entry.flags() | Qt.ItemFlag.ItemIsEditable)

    def _copy_menu(self, position):
        index = self.indexAt(position)
        if not index.isValid():
            return
        entry = self.itemFromIndex(index)
        menu = QMenu(self)
        for column, label in self._copy_columns.items():
            action = menu.addAction("Копировать "+label)
            action.triggered.connect(lambda checked=False, column=column: QApplication.clipboard().setText(entry.text(column)))
        try:
            menu.exec_(self.viewport().mapToGlobal(position))
        finally:
            menu.deleteLater()

    def keyPressEvent(self, event):
        if (getattr(self, "_copy_columns", None) and event.matches(QKeySequence.StandardKey.Copy)
                and self.currentItem() is not None and self.currentColumn() in self._copy_columns):
            QApplication.clipboard().setText(self.currentItem().text(self.currentColumn()))
            event.accept()
            return
        super().keyPressEvent(event)

    def _remember_width(self, column):
        self._manual_widths[self.headerItem().text(column)] = self.columnWidth(column)

    def set_empty_text(self, text):
        self.empty_text = text
        self._update_empty()

    def _update_empty(self):
        if not hasattr(self, "empty_label"):
            return
        empty = not any(not self.topLevelItem(row).isHidden() for row in range(self.topLevelItemCount()))
        revision = (empty, bool(self.topLevelItemCount()), self.empty_text, self.viewport().size())
        if revision == getattr(self, '_empty_revision', None):
            return
        self._empty_revision = revision
        self.empty_label.setText("Нет совпадений." if self.topLevelItemCount() else self.empty_text)
        self.empty_label.setGeometry(10, 16, max(0, self.viewport().width()-20), 60)
        self.empty_label.setVisible(empty)
        if empty:
            self.empty_label.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_empty()

    def mousePressEvent(self, event):
        if not self.indexAt(event.pos()).isValid():
            self.clearSelection()
            self.setCurrentItem(None)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        hovered = self.itemAt(event.pos())
        if hovered is not self._hovered_item:
            if self._hovered_item is not None:
                self.viewport().update(self.visualItemRect(self._hovered_item))
            self._hovered_item = hovered
            if hovered is not None:
                self.viewport().update(self.visualItemRect(hovered))
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        if self._hovered_item is not None:
            self.viewport().update(self.visualItemRect(self._hovered_item))
            self._hovered_item = None
        super().leaveEvent(event)

    def drawRow(self, painter, option, index):
        row_option = QStyleOptionViewItem(option)
        if self.itemFromIndex(index) is self._hovered_item:
            row_option.state |= QStyle.StateFlag.State_MouseOver
        super().drawRow(painter, row_option, index)

    def horizontalHeader(self):
        return self.header()

    def setHorizontalHeaderLabels(self, labels):
        self.setHeaderLabels(labels)

    def rowCount(self):
        return self.topLevelItemCount()

    def setRowCount(self, count):
        while self.topLevelItemCount() > count:
            if self.topLevelItem(self.topLevelItemCount()-1) is self._hovered_item:
                self._hovered_item = None
            self.takeTopLevelItem(self.topLevelItemCount()-1)
        while self.topLevelItemCount() < count:
            entry = QTreeWidgetItem()
            if getattr(self, "_copy_columns", None):
                entry.setFlags(entry.flags() | Qt.ItemFlag.ItemIsEditable)
            self.addTopLevelItem(entry)
        self._update_empty()

    def clear(self):
        self._hovered_item = None
        self._fitted_schema = None
        super().clear()
        self._update_empty()

    def setItem(self, row, column, cell):
        entry = self.topLevelItem(row)
        # Copy presentation and identity; later edits go directly to the tree item.
        for role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole,
                     Qt.ItemDataRole.UserRole, Qt.ItemDataRole.ForegroundRole,
                     Qt.ItemDataRole.BackgroundRole, Qt.ItemDataRole.TextAlignmentRole):
            value = cell.data(role)
            if entry.data(column, role) != value:
                entry.setData(column, role, value)

    def item(self, row, column):
        entry = self.topLevelItem(row)
        return ResourceCell(entry, column) if entry is not None else None

    def currentRow(self):
        return self.indexOfTopLevelItem(self.currentItem())

    def set_filter(self, text):
        self.filter_text = text.strip().casefold()
        self.apply_filter()

    def apply_filter(self):
        needle = getattr(self, "filter_text", "")
        for row in range(self.topLevelItemCount()):
            entry = self.topLevelItem(row)
            entry.setHidden(bool(needle) and not any(needle in entry.text(col).casefold() for col in range(self.columnCount())))
            if entry.isHidden() and entry.isSelected():
                self.clearSelection()
                self.setCurrentItem(None)
        self._update_empty()

    def selectRow(self, row):
        self.setCurrentItem(self.topLevelItem(row))

    def resizeColumnsToContents(self):
        schema = (getattr(self, 'resource_scope', None), tuple(self.headerItem().text(c) for c in range(self.columnCount())))
        if schema == getattr(self, '_fitted_schema', None):
            return
        if not self.rowCount():
            return
        self._fitted_schema = schema
        for column in range(self.columnCount()):
            if self.header().sectionResizeMode(column) != QHeaderView.ResizeMode.Stretch:
                manual = self._manual_widths.get(self.headerItem().text(column))
                if manual is not None:
                    self.setColumnWidth(column, manual)
                else:
                    self.resizeColumnToContents(column)
                    self.setColumnWidth(column, min(520, max(100, self.columnWidth(column))))
