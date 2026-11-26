"""Merged Kubernetes events; requests belong only to the visible Events page."""
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
import threading
import time
from PyQt5.QtCore import QAbstractTableModel, QEvent, QModelIndex, QSignalBlocker, Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout,
                            QLabel, QLineEdit, QPushButton, QTableView, QVBoxLayout, QWidget)
from .authorization import AuthorizationRequired
from .inventory_pages import ClusterPicker
from .loading_status import LoadingStatus
from .monitoring import MonitoringCancelled, cancellable_read
from .request_policy import credentials_revision, expected_credentials, manual_read
from .tooltips import wrap_tooltip
from .resource_list import ResourceHeader
from .wrapped_cells import WrappedCells


def event_time(event):
    values = (event.get('series', {}).get('lastObservedTime'), event.get('lastTimestamp'),
              event.get('eventTime'), event.get('metadata', {}).get('creationTimestamp'))
    dates = []
    for value in values:
        if value:
            try:
                dates.append(datetime.fromisoformat(value.replace('Z', '+00:00')))
            except (ValueError, TypeError):
                pass
    return max(dates, key=lambda d: d.timestamp()) if dates else None


@dataclass(frozen=True)
class EventRow:
    key: tuple
    cells: tuple
    time: float
    count: int
    target: tuple


def event_row(cluster, event):
    meta = event.get('metadata', {})
    target = event.get('involvedObject') or event.get('regarding') or {}
    namespace = meta.get('namespace', target.get('namespace', ''))
    date = event_time(event)
    count = event.get('series', {}).get('count', event.get('count', event.get('deprecatedCount', 1)))
    count = count if type(count) is int else 1
    message, reason = str(event.get('message', event.get('note', ''))), str(event.get('reason', ''))
    key = (cluster['id'], meta.get('uid') or (namespace, target.get('uid', ''), reason, message))
    cells = (date.astimezone().strftime('%d.%m.%Y %H:%M:%S') if date else '—', cluster['name'],
             namespace, str(event.get('type', 'Normal')), reason,
             '/'.join((target.get('kind', ''), target.get('name', ''))), message, str(count),
             str(event.get('reportingController') or event.get('source', {}).get('component', '')))
    return EventRow(key, cells, date.timestamp() if date else 0., count,
                    (cluster['id'], target.get('kind', '').lower(), namespace,
                     target.get('name', ''), target.get('uid')))


class EventModel(QAbstractTableModel):
    headers = ('Время', 'Кластер', 'Namespace', 'Тип', 'Причина', 'Ресурс', 'Сообщение', 'Количество', 'Источник')

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.sort_column, self.sort_order = 0, Qt.DescendingOrder

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        return self.headers[section] if orientation == Qt.Horizontal and role == Qt.DisplayRole else None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self.rows):
            return None
        row = self.rows[index.row()]
        if role == Qt.DisplayRole:
            return row.cells[index.column()]
        if role == Qt.ToolTipRole:
            return wrap_tooltip('\n'.join(f'{key}: {value}' for key, value in zip(self.headers, row.cells)))
        if role == Qt.ForegroundRole and index.column() == 3 and row.cells[3] == 'Warning':
            return QColor('#dcae54')
        return None

    def replace(self, rows):
        column = self.sort_column
        def key(row):
            return row.time if column == 0 else row.count if column == 7 else row.cells[column].casefold()
        rows = sorted(rows, key=key, reverse=self.sort_order == Qt.DescendingOrder)
        if rows == self.rows:
            return
        if [r.key for r in rows] == [r.key for r in self.rows]:
            changed = [i for i, (old, new) in enumerate(zip(self.rows, rows)) if old != new]
            self.rows = rows
            if changed:
                self.dataChanged.emit(self.index(min(changed), 0), self.index(max(changed), len(self.headers)-1))
        else:
            self.beginResetModel()
            self.rows = rows
            self.endResetModel()

    def sort(self, column, order=Qt.AscendingOrder):
        self.sort_column, self.sort_order = column, order
        self.replace(self.rows)


def collect_events(api, cluster, namespace=None):
    from .kubernetes import AggregateListForbidden, Kubernetes
    errors = []
    if namespace is None:
        try:
            return api.all_namespace_resources(cluster['id'], 'event'), errors
        except AggregateListForbidden:
            namespaces = api.namespaces(cluster['id'])
    else:
        namespaces = [namespace]
    events = []
    for name in namespaces:
        try:
            events.extend(api.snapshot_list(cluster['id'], Kubernetes.path('event', name)))
        except (MonitoringCancelled, AuthorizationRequired):
            raise
        except Exception as exc:
            errors.append(f'{name}: {exc}')
    return events, errors


class EventsPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.main_window = window
        self.snapshots, self.errors, self.operations = {}, {}, {}
        self.revisions, self.loaded_at = {}, {}
        self.request_scope = None
        self.demanded = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        heading = QLabel('Events')
        heading.setObjectName('sectionTitle')
        layout.addWidget(heading)
        controls = QHBoxLayout()
        self.clusters = ClusterPicker(self.available_clusters)
        controls.addWidget(self.clusters)
        self.namespace = QComboBox()
        self.namespace.addItem('Все namespace', None)
        controls.addWidget(self.namespace)
        self.event_type = QComboBox()
        for label, value in (('Все типы', None), ('Warning', 'Warning'), ('Normal', 'Normal')):
            self.event_type.addItem(label, value)
        controls.addWidget(self.event_type)
        self.search_field = QLineEdit()
        self.search_field.setPlaceholderText('Поиск события…')
        controls.addWidget(self.search_field)
        controls.addStretch()
        self.loading_status = LoadingStatus(window.background, window)
        controls.addWidget(self.loading_status)
        self.refresh_button = QPushButton('Повторить загрузку')
        controls.addWidget(self.refresh_button)
        layout.addLayout(controls)
        self.status = QLabel('Выберите кластеры.')
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        self.model = EventModel(self)
        self.table = QTableView()
        self.table.setObjectName('resourceList')
        self.table.setModel(self.model)
        self.table.verticalHeader().hide()
        self.table.setCornerButtonEnabled(False)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setMouseTracking(True)
        self.table.setHorizontalHeader(ResourceHeader(self.table))
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.table.horizontalHeader().setMinimumSectionSize(80)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.DescendingOrder)
        self.table.setWordWrap(True)
        self.table.setItemDelegate(WrappedCells(self.table))
        self.table.verticalHeader().setDefaultSectionSize(self.table.fontMetrics().lineSpacing()*2+18)
        for column, width in enumerate((155, 110, 160, 90, 150, 230, 350, 85, 160)):
            self.table.setColumnWidth(column, width)
        self.empty_label = QLabel('События не найдены.', self.table.viewport())
        self.empty_label.setObjectName('emptyState')
        self.empty_label.setTextFormat(Qt.PlainText)
        self.empty_label.setWordWrap(True)
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.table.viewport().installEventFilter(self)
        layout.addWidget(self.table, 1)
        self.clusters.changed.connect(lambda: self.refresh_data(force=True))
        self.namespace.currentIndexChanged.connect(lambda: self.refresh_data(force=True))
        self.event_type.currentIndexChanged.connect(self.render)
        self.search_field.textChanged.connect(self.render)
        self.refresh_button.clicked.connect(lambda: self.refresh_data(force=True))
        self.table.doubleClicked.connect(self.open_target)

    def available_clusters(self):
        return [c for c in self.main_window.store.clusters() if c.get('enabled')]

    def eventFilter(self, watched, event):
        if watched is self.table.viewport() and event.type() == QEvent.Resize:
            self._update_empty()
        return super().eventFilter(watched, event)

    def _update_empty(self):
        self.empty_label.setGeometry(10, 16, max(0, self.table.viewport().width()-20), 60)
        self.empty_label.setText('Загрузка событий…' if self.operations else 'События не найдены.')
        self.empty_label.setVisible(not self.model.rows)
        if not self.model.rows:
            self.empty_label.raise_()

    def active(self):
        window = self.main_window
        return (self.isVisible() and not window._closing and window.tabs.currentWidget() is self
                and getattr(window, '_resource_panel', None) is None
                and getattr(window, '_resource_opening_request', None) is None)

    def showEvent(self, event):
        super().showEvent(event)
        if not event.spontaneous(): self.refresh_data()

    def hideEvent(self, event):
        if not event.spontaneous(): self.set_demanded(False)
        super().hideEvent(event)

    def set_demanded(self, demanded):
        self.demanded = demanded
        if not demanded:
            for operation, cancel in self.operations.values():
                cancel.set()
            self.operations.clear()

    def reconcile(self):
        available = self.available_clusters()
        enabled = {c['id'] for c in available}
        for cluster in available:
            cid = cluster['id']
            if cid in self.revisions and self.revisions[cid] != credentials_revision(cluster):
                self.snapshots.pop(cid, None)
                self.loaded_at.pop(cid, None)
        for cid in list(self.operations):
            if cid not in enabled:
                self.operations.pop(cid)[1].set()
        self.snapshots = {cid: value for cid, value in self.snapshots.items() if cid in enabled}
        self.errors = {cid: value for cid, value in self.errors.items() if cid in enabled}
        self.clusters.update_text()
        self.render()

    def refresh_data(self, *, force=False):
        if not self.active():
            return
        from .polling import polling_allowed
        if not polling_allowed(self):
            return
        self.demanded = True
        self.reconcile()
        selected = self.clusters.ids()
        namespace = self.namespace.currentData()
        scope = (frozenset(selected), namespace,
                 tuple((c['id'], credentials_revision(c)) for c in self.available_clusters() if c['id'] in selected))
        if scope != self.request_scope:
            self.set_demanded(False)
            self.loaded_at.clear()
            self.demanded = True
            self.request_scope = scope
        for cid in list(self.operations):
            if cid not in selected:
                self.operations.pop(cid)[1].set()
        for cluster in self.available_clusters():
            cid = cluster['id']
            if cid not in selected:
                continue
            if time.monotonic() - self.loaded_at.get(cid, 0) < 1:
                continue
            if cid in self.operations:
                continue
            revision = credentials_revision(cluster)
            operation, canceled = object(), threading.Event()
            self.operations[cid] = (operation, canceled)
            def current(cid=cid, operation=operation, revision=revision):
                return (self.active() and self.demanded and self.operations.get(cid, (None,))[0] is operation
                        and any(c['id'] == cid and credentials_revision(c) == revision for c in self.available_clusters()))
            def done(value, cid=cid, current=current, revision=revision):
                if current():
                    self.snapshots[cid], errors = value
                    self.errors[cid] = '; '.join(errors)
                    self.revisions[cid] = revision
                    self.loaded_at[cid] = time.monotonic()
            def failed(message, cid=cid, current=current):
                if current():
                    self.errors[cid] = str(message)
            def finalized(outcome, cid=cid, operation=operation):
                if self.operations.get(cid, (None,))[0] is operation:
                    self.operations.pop(cid, None)
                    self.render()
            api = self.main_window.api
            def load(cluster=cluster, revision=revision, namespace=namespace, api=api):
                with expected_credentials({cluster['id']: revision}):
                    return collect_events(api, cluster, namespace)
            with manual_read() if force else nullcontext():
                admitted = self.main_window.background.submit(
                    cancellable_read(load, canceled, self.main_window.monitoring.token()), done, failed,
                    key=('events', cid), finalized=finalized, label='События кластеров')
            if admitted is False:
                failed('Чтение отложено; повторите обновление.')
                finalized('CANCELLED')
        self.render()

    def render(self, *_):
        if not self.active():
            return
        clusters = self.available_clusters()
        selected = self.clusters.ids(clusters)
        rows = [event_row(c, event) for c in clusters if c['id'] in selected
                for event in self.snapshots.get(c['id'], [])]
        namespace = self.namespace.currentData()
        namespaces = sorted({r.cells[2] for r in rows} | ({namespace} if namespace else set()))
        if [self.namespace.itemData(i) for i in range(1, self.namespace.count())] != namespaces:
            blocker = QSignalBlocker(self.namespace)
            self.namespace.clear()
            self.namespace.addItem('Все namespace', None)
            for name in namespaces:
                self.namespace.addItem(name, name)
            self.namespace.setCurrentIndex(max(0, self.namespace.findData(namespace)))
            del blocker
        kind, text = self.event_type.currentData(), self.search_field.text().casefold().strip()
        rows = [r for r in rows if (namespace is None or r.cells[2] == namespace)
                and (kind is None or r.cells[3] == kind)
                and (not text or text in '\n'.join(r.cells).casefold())]
        current = self.table.currentIndex().row()
        old_key = self.model.rows[current].key if 0 <= current < len(self.model.rows) else None
        scroll = self.table.verticalScrollBar().value()
        self.model.replace(rows)
        self._update_empty()
        if old_key is not None:
            row = next((i for i, value in enumerate(self.model.rows) if value.key == old_key), None)
            if row is not None and self.table.currentIndex().row() != row:
                self.table.setCurrentIndex(self.model.index(row, 0))
        self.table.verticalScrollBar().setValue(scroll)
        errors = [f'{c["name"]}: {self.errors[c["id"]]}' for c in clusters
                  if c['id'] in selected and self.errors.get(c['id'])]
        self.status.setText(f'Кластеров: {len(selected)} · событий: {len(rows)}'
                            + (' · загрузка…' if self.operations else '')
                            + (f' · ошибок: {len(errors)}; часть данных может быть устаревшей.' if errors else ''))
        self.status.setToolTip(wrap_tooltip('\n'.join(errors)))

    def open_target(self, index):
        if index.isValid():
            cid, kind, namespace, name, uid = self.model.rows[index.row()].target
            if kind in ('pod', 'deployment', 'statefulset', 'daemonset', 'job', 'cronjob', 'configmap') and name:
                self.main_window.open_resource(cid, kind, namespace, name, expected_uid=uid)
