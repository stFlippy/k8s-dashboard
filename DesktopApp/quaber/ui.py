"""Native Qt desktop shell for Quaber."""

from __future__ import annotations
from .tooltips import show_tooltip

import copy
import difflib
import json
import random
import time
import html
import logging
import threading
import math
from datetime import datetime, timezone
from pathlib import Path

import yaml
from .capabilities import ActionTarget, AccessOutcome, review_action
from PyQt5.QtCore import QPointF, QRect, QSize, QTimer, Qt, QEvent, QSignalBlocker, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QColor, QPainter, QPen, QPixmap, QPolygonF, QKeySequence, QIcon, QFontMetrics
from PyQt5.QtWidgets import QAbstractItemView, QApplication, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QSpinBox, QSplitter, QTabWidget, QTableWidgetItem, QSizePolicy, QToolTip, QTextEdit, QVBoxLayout, QWidget, QScrollArea, QFrame, QAction

from .kubectl import ProxyManager, locate_kubectl, login, test_connection
from .kubernetes import Kubernetes, ResourceNotFoundError
from .storage import UserStore, validate_server
from .views import MatrixViews
from .widgets import FullCheckBox, AppComboBox as QComboBox, CodeEditor, NamespaceHeader, PodDelegate, PodMatrix, stylesheet, pod_status, pod_at, resource_age, resource_percent, StatusDelegate, DisabledHintButton, PodDonut
from PyQt5.QtWidgets import QMenu, QButtonGroup
from concurrent.futures import ThreadPoolExecutor
from .monitoring import bind_read
from .app_logging import LogSession, register_secret
from .log_panel import LogPanel
from .resource_list import ResourceList
from .resource_details import ResourceDetails
from .app_identity import application_icon
from .polling import PollingState, polling_allowed, mark_poll_timer, restart_poll_timer, poll_interval
from .view_tabs import ViewTabs, VIEW_COLORS, blended_color, color_icon
from .themes import theme as theme_colors, apply_palette, install_button_translations
from .safety import check_identity, resource_changes
from .confirmations import confirm_change
from .navigation import navigation_icon
from .widgets import MultiSelectMenu, ResourceTabs
from .pod_summary import deployment_rings
from .pod_recency import DeploymentMode, recent_activity_deadline
from .container_selection import preferred_container
from .metric_history import MetricHistory
from .access import server_forbidden
from .admin_access import AdminAccess
from .inventory import InventoryService
from .inventory_pages import DumpsPage, VersionsPage
from .preferences import PreferenceWriter
from .background import Background, PanelBackground
from .secret_dialog import SecretDialog
from .resource_index import index_scope, empty_workload, WORKLOAD_ORDER
from PyQt5.QtWidgets import QPlainTextEdit
from PyQt5.QtGui import QTextCursor

logger = logging.getLogger("quaber.ui")


def run_dialog(dialog):
    """Dispose temporary modal widgets after their caller has read the result."""
    try:
        return dialog.exec_()
    finally:
        dialog.deleteLater()


class BoundedConsole(QPlainTextEdit):
    def __init__(self):
        super().__init__()
        self.setMaximumBlockCount(2000)

    def append(self, value):
        # Plain text and a character cap also bound a single enormous line.
        value = str(value)[-250000:]
        self.moveCursor(QTextCursor.MoveOperation.End)
        self.insertPlainText(("\n" if self.toPlainText() else "") + value)
        overflow = self.document().characterCount()-500000
        if overflow > 0:
            cursor = QTextCursor(self.document())
            cursor.setPosition(0)
            cursor.setPosition(overflow, QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()


def error(parent, message: str):
    logger.error('%s', message)
    from PyQt5 import sip
    if parent is not None and sip.isdeleted(parent):
        return
    QMessageBox.warning(parent, "Quaber", message)


def confirm(parent, title: str, text: str, *, arithmetic=False, target=None, details=None) -> bool:
    if target is not None and not arithmetic:
        return confirm_change(parent, title, target, text, details)
    if not arithmetic:
        return QMessageBox.question(parent, title, text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel) == QMessageBox.StandardButton.Yes
    first, second = random.randint(10, 99), random.randint(10, 99)
    answer, accepted = QInputDialog.getInt(parent, title, f"{text}\n\nДля подтверждения решите пример: {first} + {second} = ?", 0, 0, 198)
    if accepted and answer != first + second:
        error(parent, "Неверный ответ. Действие не выполнено.")
    return accepted and answer == first + second


def item(text: object, color: str | None = None) -> QTableWidgetItem:
    cell = QTableWidgetItem(str(text if text is not None else ""))
    cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
    if color:
        cell.setBackground(QColor(color))
        cell.setForeground(QColor("#f4f7fb"))
    return cell


from .accounts_ui import ClusterDialog

class LoginGroupsDialog(SecretDialog):
    def __init__(self, parent, groups: list[dict]):
        super().__init__(parent)
        self.groups = groups = sorted(groups, key=lambda g: g["username"].casefold())
        self.completed_groups = set()
        self.succeeded_cluster_ids = set()
        from .connection_presentation import login_color
        self.setWindowTitle("Требуется вход в кластеры")
        self.resize(440, min(560, 115+76*len(groups)))
        layout = QVBoxLayout(self)
        hint = QLabel('Пароль учётной записи применяется к связанным подключениям. Вход выполняется последовательно.')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.passwords = []
        self.remember_passwords = []
        self.group_statuses = []
        fields_widget = QWidget()
        fields = QVBoxLayout(fields_widget)
        fields.setContentsMargins(0, 0, 0, 0)
        for group in groups:
            group_label = QLabel(f"{group['username']}: {', '.join(cluster['name'] for cluster in group['clusters'])}")
            group_label.setTextFormat(Qt.TextFormat.PlainText)
            group_label.setWordWrap(True)
            group_label.setStyleSheet(f"border-left: 4px solid {login_color(group['username'])}; padding: 6px 10px; font-weight: 600;")
            fields.addWidget(group_label)
            password = QLineEdit()
            password.setEchoMode(QLineEdit.EchoMode.Password)
            password.setPlaceholderText("Пароль")
            saved = set()
            for cluster in group["clusters"]:
                try:
                    saved.add(parent.store.saved_password(cluster))
                except Exception:
                    saved.add("")
            if len(saved) == 1:
                password.setText(next(iter(saved)))
            fields.addWidget(password)
            self.passwords.append(password)
            remember = QCheckBox("Сохранить пароль")
            remember.setChecked(False)
            fields.addWidget(remember)
            self.remember_passwords.append(remember)
            group_status = QLabel()
            group_status.setTextFormat(Qt.TextFormat.PlainText)
            group_status.setWordWrap(True)
            group_status.hide()
            fields.addWidget(group_status)
            self.group_statuses.append(group_status)
        fields.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll = QScrollArea()
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setWidget(fields_widget)
        self.fields_widget, self.groups_scroll = fields_widget, scroll
        layout.addWidget(scroll, 1)
        if len(groups) <= 2:
            scroll.setFixedHeight(fields_widget.sizeHint().height()+4)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.hide()
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Войти")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.submit)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.buttons = buttons
        self.busy = False

    def _update_login_fields(self):
        for index, (password, remember) in enumerate(zip(self.passwords, self.remember_passwords)):
            editable = not self.busy and index not in self.completed_groups
            password.setEnabled(editable)
            password.setVisible(index not in self.completed_groups)
            remember.setEnabled(editable)
            remember.setVisible(index not in self.completed_groups)
        if len(self.groups) <= 2:
            self.groups_scroll.setFixedHeight(min(360, self.fields_widget.sizeHint().height()+4))

    @staticmethod
    def _login_subject(cluster):
        return (cluster.get('id'), cluster.get('server'), cluster.get('username'),
                cluster.get('authMode', 'login'), bool(cluster.get('enabled')),
                cluster.get('accountId'))

    def submit(self):
        if self.busy:
            return
        pending = [index for index in range(len(self.groups)) if index not in self.completed_groups]
        if not pending:
            self.accept()
            return
        passwords = {index: self.passwords[index].text() for index in pending}
        remember = {index: self.remember_passwords[index].isChecked() for index in pending}
        for password in passwords.values():
            register_secret(password)
        if any(not value for value in passwords.values()):
            error(self, "Введите пароль только для групп, где вход ещё не выполнен.")
            return
        parent = self.parent()
        app_dir, store = parent.app_dir, parent.store
        try:
            current = {c['id']: c for c in store.clusters()}
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))
            self.status.show()
            return
        attempts, identity_errors = [], []
        for index in pending:
            group = self.groups[index]
            snapshots = [current.get(c['id']) for c in group['clusters']]
            if any(c is None or self._login_subject(c) != self._login_subject(original)
                   for c, original in zip(snapshots, group['clusters'])):
                identity_errors.append((index, (), 'Подключение изменено, удалено или отключено. Закройте форму и откройте вход заново.'))
            else:
                # A new explicit click uses fresh credentials; changes DURING
                # this attempt are still rejected by store.update_logins.
                attempts.append((index, group['username'], snapshots, passwords[index], remember[index]))
        self.buttons.setEnabled(False)
        self.busy = True
        self.status.setText("Выполняется kubectl login…")
        self.status.show()
        self._update_login_fields()
        def work():
            from .kubectl import _login_message
            results = list(identity_errors)
            for index, username, snapshots, password, save_password in attempts:
                succeeded, problems = [], []
                try:
                    executable = locate_kubectl(app_dir, store.preferences().get('kubectl_path', ''))
                    from .auth_queue import clear_rejection, serialized_login
                    clear_rejection(store, snapshots[0])
                    for c in snapshots:
                        if c['id'] in self.succeeded_cluster_ids:
                            continue
                        try:
                            config = serialized_login(store, c, password,
                                lambda c=c: login(executable, c['server'], username, password))
                            store.update_logins({c['id']:config}, expected={c['id']:c})
                            succeeded.append(c['id'])
                        except Exception as exc:
                            problems.append(c['name']+': '+_login_message(str(exc),password))
                    if succeeded and snapshots[0].get('accountId'):
                        account = next(a for a in store.accounts() if a['id'] == snapshots[0]['accountId'])
                        store.put_account(account['name'], username, password, account_id=account['id'],
                                          remember_password=save_password)
                    results.append((index, tuple(succeeded), '\n'.join(problems)))
                except Exception as exc:
                    results.append((index, (), _login_message(str(exc), password)))
            return results
        def done(results):
            self.busy = False
            for index, identifiers, message in results:
                self.passwords[index].clear()
                self.succeeded_cluster_ids.update(identifiers)
                if not message and all(c['id'] in self.succeeded_cluster_ids for c in self.groups[index]['clusters']):
                    self.completed_groups.add(index)
                    self.passwords[index].setPlaceholderText('Вход выполнен')
                    message = '✓ Вход выполнен. Повторный пароль не требуется.'
                self.group_statuses[index].setText(message)
                self.group_statuses[index].show()
            self._update_login_fields()
            if len(self.completed_groups) == len(self.groups):
                self.accept()
                return
            self.status.setText(f'Вход выполнен для {len(self.completed_groups)} из {len(self.groups)} групп. Повторите вход только для оставшихся групп.')
            self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Повторить вход')
            self.buttons.setEnabled(True)
        def failed(message):
            self.busy = False
            for index in pending:
                self.passwords[index].clear()
            self.status.setText(message)
            self.status.show()
            self.buttons.setEnabled(True)
            self._update_login_fields()
        if parent.background.submit(work, done, failed, monitor=False) is False:
            failed('Не удалось начать вход. Повторите попытку.')

class TextDialog(QDialog):
    def __init__(self, parent, title: str, content: str, editable: bool = False):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(950, 690)
        layout = QVBoxLayout(self)
        self.editor = QTextEdit()
        self.editor.setPlainText(content)
        self.editor.setReadOnly(not editable)
        self.editor.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.editor)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel if editable else QDialogButtonBox.StandardButton.Close)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class MetricChart(QWidget):
    def __init__(self, title: str, unit: str):
        super().__init__()
        self.title, self.unit = title, unit
        self.values: list[float | None] = []
        self.request: float | None = None
        self.limit: float | None = None
        self.setMinimumHeight(140)
        self.setMouseTracking(True)
        self.hover_index = None
        self.timestamps = []
        self.window_seconds = 60
        self.window_end = None

    def set_data(self, values: list[float | None], request: float | None = None, limit: float | None = None):
        revision = (tuple(values), request, limit, self.title, self.unit,
                    tuple(self.timestamps), self.window_seconds, self.window_end)
        if revision == getattr(self, '_chart_revision', None):
            return
        self._chart_revision = revision
        self.values, self.request, self.limit = values, request, limit
        self.hover_index = None
        self.update()

    def maximum(self):
        if self.limit is not None and self.limit > 0:
            return self.limit
        return max([v for v in [*self.values, self.request] if v is not None] + [1.0]) * 1.1

    def sample_x(self, index):
        left, right = 64, self.width()-16
        if self.window_end is not None and index < len(self.timestamps):
            age = self.window_end-self.timestamps[index].timestamp()
            return right-(right-left)*age/self.window_seconds
        capacity = max(120, len(self.values))
        return right - (len(self.values)-1-index) * (right-left) / max(1, capacity-1)

    def mouseMoveEvent(self, event):
        previous_hover = self.hover_index
        left, right, top, bottom = 64, self.width() - 16, 34, self.height() - 24
        point = event.localPos()
        nearest = min((index for index, value in enumerate(self.values) if value is not None),
                      key=lambda index: abs(self.sample_x(index)-point.x()), default=None)
        if nearest is not None and right > left and left <= point.x() <= right and top <= point.y() <= bottom:
            self.hover_index = nearest
            value = self.values[self.hover_index]
            stamp = self.timestamps[self.hover_index].strftime("%H:%M:%S") if self.hover_index < len(self.timestamps) else f"#{self.hover_index+1}"
            text = f"{stamp}  {value:.2f} {self.unit}" if value is not None else f"{stamp}  No data"
            show_tooltip(event.globalPos(), text, self)
        else:
            self.hover_index = None
            QToolTip.hideText()
        if previous_hover != self.hover_index:
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        changed = self.hover_index is not None
        self.hover_index = None
        QToolTip.hideText()
        if changed:
            self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        color = self.palette().text().color()
        painter.setPen(color)
        current = next((value for value in reversed(self.values) if value is not None), None)
        label = f"{self.title}: {current:.2f} {self.unit}" if current is not None else f"{self.title}: —"
        painter.drawText(8, 18, label)
        left, right, top, bottom = 64, self.width() - 16, 34, self.height() - 24
        if right <= left or bottom <= top:
            return
        values = [value for value in self.values if value is not None]
        maximum = self.maximum()
        painter.setPen(QPen(self.palette().mid().color(), 1))
        for step in (0, 0.5, 1):
            y = bottom - (bottom - top) * step
            painter.drawLine(QPointF(left, y), QPointF(right, y))
        def y_value(value):
            return bottom - max(0.0, min(maximum, value)) / maximum * (bottom - top)
        for value, color_code in ((self.request, "#e0b15a"), (self.limit, "#db7777")):
            if value is not None:
                painter.setPen(QPen(QColor(color_code), 1, Qt.PenStyle.DashLine if value == self.request else Qt.PenStyle.SolidLine))
                y = y_value(value)
                painter.drawLine(QPointF(left, y), QPointF(right, y))
                painter.drawText(QPointF(right - 160, max(top + 12, y - 4)), f"{'request' if value == self.request else 'limit'}: {value:.2f} {self.unit}")
        if values:
            painter.setPen(QPen(QColor("#51a7f9"), 2))
            segment = QPolygonF()
            for index, value in enumerate(self.values):
                if value is None:
                    if len(segment) > 1:
                        painter.drawPolyline(segment)
                    elif len(segment) == 1:
                        painter.drawEllipse(segment[0], 2, 2)
                    segment = QPolygonF()
                    continue
                segment.append(QPointF(self.sample_x(index), y_value(value)))
            if len(segment) > 1:
                painter.drawPolyline(segment)
            elif len(segment) == 1:
                painter.drawEllipse(segment[0], 2, 2)
        if self.hover_index is not None and self.hover_index < len(self.values):
            x = self.sample_x(self.hover_index)
            painter.setPen(QPen(color, 1, Qt.PenStyle.DotLine))
            painter.drawLine(QPointF(x, top), QPointF(x, bottom))
            value = self.values[self.hover_index]
            if value is not None:
                painter.setBrush(QColor("#51a7f9"))
                painter.drawEllipse(QPointF(x, y_value(value)), 4, 4)
        painter.setPen(color)
        painter.drawText(8, bottom, "0")
        painter.drawText(8, top + 7, f"{maximum:.1f}")
        if self.window_end is not None:
            painter.drawText(left, self.height()-5, f"−{self.window_seconds // 60} мин")
            painter.drawText(max(left, right-46), self.height()-5, "сейчас")


class MetricsWidget(QWidget):
    def __init__(self, parent, api: Kubernetes, background: Background, cluster_id: str, namespace: str, pod: str):
        super().__init__(parent)
        self.api, self.background, self.cluster_id, self.namespace, self.pod = api, background, cluster_id, namespace, pod
        self._metric_operation = None
        self._history_operation = None
        self._history_next = 0.
        from .read_lifecycle import register_read_owner
        register_read_owner(parent, self)
        controller = getattr(parent, "controller", None)
        shared_history = getattr(controller, "metric_history", None)
        self.history_store = shared_history if isinstance(shared_history, MetricHistory) else MetricHistory()
        self.history_identity = (cluster_id, namespace, pod, getattr(parent, "resource", {}).get("metadata", {}).get("uid"))
        self.current: dict = {}
        self.loading = False
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.status = QLabel("Загрузка метрик…")
        self.refresh_period = QLabel("Автообновление: 5 сек")
        row.addWidget(self.refresh_period)
        row.addWidget(self.status, 1)
        row.addWidget(QLabel("Период"))
        self.period = QComboBox()
        for minutes in (1, 5, 10, 30):
            self.period.addItem(f"{minutes} мин", minutes*60)
        self.period.setToolTip("Показывать накопленные измерения. Приложение сохраняет до 30 минут в памяти; Metrics API не отдаёт архив.")
        self.period.currentIndexChanged.connect(self.render)
        row.addWidget(self.period)
        self.container = QComboBox()
        self.container.setToolTip("Контейнер, для которого показана нагрузка.")
        self.container.currentIndexChanged.connect(self.render)
        row.addWidget(self.container)
        layout.addLayout(row)
        self.mode = QComboBox()
        self.mode.addItems(["От лимита, %", "Значения"])
        self.mode.setToolTip("Процент считается от лимита контейнера. Если лимит не задан, выберите значения.")
        self.mode.setCurrentIndex(1)
        self.mode.currentIndexChanged.connect(self.render)
        row.addWidget(self.mode)
        self.history_status = QLabel('История: загрузка из мониторинга кластера…')
        self.history_status.setWordWrap(True)
        self.history_status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.history_status)
        self.cpu = MetricChart("CPU", "m")
        self.memory = MetricChart("RAM", "MiB")
        self.throttling = MetricChart("CPU throttling", "%")
        self.throttling.setToolTip("Доля периодов, в которых контейнеру ограничивали CPU. Это не процент занятости процессора.")
        self.refresh_data = self.load
        for chart in (self.cpu, self.memory, self.throttling):
            layout.addWidget(chart)
        layout.addStretch()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.load)
        self.timer.start(5000)
        mark_poll_timer(self.timer)
        QTimer.singleShot(0, self.load)

    def showEvent(self, event):
        super().showEvent(event)
        if event.spontaneous(): return
        QTimer.singleShot(0, self.load)

    def suspend_reads(self):
        self.loading = False
        self._metric_operation = None
        self._history_operation = None
        self._history_next = 0.

    def load(self):
        if self.loading or not self.isVisible() or not polling_allowed(self):
            return
        self.loading = True
        operation = self._metric_operation = object()
        self.refresh_period.setText(f"Автообновление: {poll_interval(self.timer)/1000:g} сек")
        def done(value):
            if self._metric_operation is not operation:
                return
            self.loading = False
            self.current = value
            self.status.setText(f"CPU/RAM: {value.get('source') or 'нет данных'} · Throttling: {'cAdvisor' if value.get('throttlingAvailable') else 'нет данных'}" +
                                (f" · {value.get('error')}" if not value.get("available") else ""))
            containers = value.get("containers", [])
            self.history_store.append(self.history_identity, containers)
            first_selection = self.container.count() == 0
            for container in containers:
                name = container["name"]
                if self.container.findText(name) < 0:
                    self.container.addItem(name)
            if first_selection:
                self.container.setCurrentText(preferred_container([c["name"] for c in containers]))
            self.render()
            self.load_history()
        def failed(message):
            if self._metric_operation is not operation:
                return
            self.loading = False
            self.status.setText("Метрики устарели / недоступны: " + message)
            self.history_store.append(self.history_identity, [{"name": c["name"]} for c in self.current.get("containers", [])])
            self.render()
            self.load_history()
        def finalized(_):
            if self._metric_operation is operation:
                self.loading = False
                self._metric_operation = None
        if self.background.submit(lambda: self.api.metrics(self.cluster_id, self.namespace, self.pod,
            expected_uid=self.history_identity[3]), done, failed, demand='tab', finalized=finalized) is False:
            finalized('CANCELLED')

    def load_history(self):
        if (self._history_operation is not None or time.monotonic() < self._history_next
                or not self.isVisible() or not polling_allowed(self) or not hasattr(self.api, 'metrics_history')):
            return
        operation = self._history_operation = object()
        def done(value):
            if self._history_operation is not operation:
                return
            if not isinstance(value, dict) or not isinstance(value.get('series'), dict):
                failed('Сервер не вернул историю метрик.')
                return
            self.history_store.merge(self.history_identity, value['series'])
            self.history_status.setText('История: ' + value.get('source', 'мониторинг кластера')
                + (' · за последние 30 минут' if any(value['series'].values()) else ' · за этот период данных нет'))
            self._history_next = time.monotonic() + 60
            self.render()
        def failed(message):
            if self._history_operation is operation:
                self.history_status.setText('Серверная история недоступна: ' + str(message)
                    + ' Показаны накопленные в приложении точки.')
                self._history_next = time.monotonic() + 60
        def finalized(_):
            if self._history_operation is operation:
                self._history_operation = None
        if self.background.submit(lambda: self.api.metrics_history(self.cluster_id, self.namespace, self.pod,
            expected_uid=self.history_identity[3]), done, failed, demand='tab', finalized=finalized,
            label='История метрик') is False:
            finalized('CANCELLED')

    def render(self):
        name = self.container.currentText()
        container = next((item for item in self.current.get("containers", []) if item["name"] == name), None)
        if not container:
            return
        now = time.time()
        seconds = self.period.currentData() or 60
        rows = self.history_store.window(self.history_identity, name, seconds, now)
        history = {key: [row[index] for row in rows] for index, key in enumerate(("time", "cpu", "memory", "throttle"))}
        for chart in (self.cpu, self.memory, self.throttling):
            chart.timestamps = [datetime.fromtimestamp(row[0]) for row in rows]
            chart.window_seconds, chart.window_end = seconds, now
        cpu = container.get("cpu", {})
        memory = container.get("memory", {})
        if self.mode.currentIndex() == 0:
            self.cpu.unit = self.memory.unit = "%"
            for chart, samples, limit in ((self.cpu, history.get("cpu", []), cpu.get("limitCores")),
                                          (self.memory, history.get("memory", []), memory.get("limitBytes"))):
                request = cpu.get("requestCores") if chart is self.cpu else memory.get("requestBytes")
                chart.set_data([100*v/limit if v is not None and limit else None for v in samples], 100*request/limit if request is not None and limit else None, 100 if limit else None)
        else:
            self.cpu.unit, self.memory.unit = "m", "MiB"
            self.cpu.set_data([v * 1000 if v is not None else None for v in history.get("cpu", [])],
                              cpu.get("requestCores") * 1000 if cpu.get("requestCores") is not None else None,
                              cpu.get("limitCores") * 1000 if cpu.get("limitCores") is not None else None)
            self.memory.set_data([v / 1048576 if v is not None else None for v in history.get("memory", [])],
                                 memory.get("requestBytes") / 1048576 if memory.get("requestBytes") is not None else None,
                                 memory.get("limitBytes") / 1048576 if memory.get("limitBytes") is not None else None)
        self.throttling.set_data(history.get("throttle", []), limit=100)


from .yaml_codec import dump_resource
from .table_batch import batch_table
from .write_guard import WriteGuard, current_permit, operation_permit
from .request_policy import credentials_revision


class ResourceDialog(QDialog):
    def __init__(self, parent, api: Kubernetes, background: Background, cluster_id: str, kind: str, namespace: str, name: str, resource: dict, *, source_credentials=None):
        embedded = hasattr(parent, "workspace")
        super().__init__(parent.workspace if embedded else parent)
        if embedded:
            # QDialog adds the Dialog flag in its constructor even when Widget (0) is supplied.
            self.setWindowFlags(Qt.WindowType.Widget)
        self.controller = parent
        self.api = api
        self.background = PanelBackground(background, self)
        self.cluster_id, self.kind, self.namespace, self.name = cluster_id, kind, namespace, name
        self._opened_identity = (cluster_id, kind, namespace, name, resource.get("metadata", {}).get("uid"))
        self._opened_credentials = source_credentials or credentials_revision(api.cluster(cluster_id))
        self.resource = resource
        self.yaml_base_resource = copy.deepcopy(resource)
        self.version = resource.get("metadata", {}).get("resourceVersion", "")
        self._yaml_ready = False
        self._yaml_loading = False
        self._yaml_generation = 0
        self.pending_operation = False
        self._resource_epoch = 0
        self._access_context_generation = 0
        self._rollout_in_progress = False
        self._scaling_target = None
        self._scale_snapshot = None
        self.scale_delay_timer = QTimer(self)
        self.scale_delay_timer.setSingleShot(True)
        self.scale_delay_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.scale_countdown_timer = QTimer(self)
        self.scale_countdown_timer.setInterval(100)
        self.scale_countdown_timer.timeout.connect(self._update_scale_notice)
        self._hpa_controllers = []
        self._displayed_sets = []
        self.write_controls = []
        self._write_tooltips = {}
        self.setWindowTitle(f"{kind.title()}: {name} — {namespace}")
        self.resize(1000, 720)
        self.setObjectName("resourcePanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        header_widget = QWidget()
        self.header_widget = header_widget
        header_widget.setObjectName("resourceHeader")
        header = QHBoxLayout(header_widget)
        header.setContentsMargins(18, 12, 18, 12)
        title = QLabel(name)
        title.setObjectName("resourceTitle")
        title.setMinimumWidth(100)
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        title.setToolTip(name)
        title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        header.addWidget(title, 1)
        layout.addWidget(header_widget)
        context_widget = QWidget()
        context_widget.setObjectName("resourceContext")
        context = QHBoxLayout(context_widget)
        context.setContentsMargins(18, 8, 18, 12)
        self.context_values = {}
        context_entries = [("Кластер", self.api.cluster(cluster_id)["name"]), ("Namespace", namespace)]
        if kind == "deployment":
            context_entries.append(("Pods", f"{resource.get('status', {}).get('replicas', resource.get('status', {}).get('availableReplicas', 0))}/{resource.get('spec', {}).get('replicas', 1)}"))
        elif kind == "pod":
            context_entries.append(("Статус", pod_status(resource)))
        for label, value in context_entries:
            context_box = QWidget()
            context_box.setObjectName("resourceContextField")
            context_box.setMaximumWidth(360)
            box = QVBoxLayout(context_box)
            box.setContentsMargins(0, 0, 0, 0)
            box.addWidget(QLabel(label))
            value_label = QLabel(str(value))
            value_label.setObjectName("contextValue")
            self.context_values[label] = value_label
            box.addWidget(value_label)
            context.addWidget(context_box, 1)
        context.addStretch(1)
        layout.addWidget(context_widget)
        self.tabs = ResourceTabs()
        self.tabs.blockSignals(True)
        self.tabs.currentChanged.connect(self._resource_tab_changed)
        self.tabs.setObjectName("resourceTabs")
        layout.addWidget(self.tabs)
        if kind == 'cronjob':
            self.cron_schedule = QLabel()
            self.cron_schedule.setWordWrap(True)
            self.cron_schedule.setTextFormat(Qt.TextFormat.RichText)
            self.cron_schedule.linkActivated.connect(self._cron_link)
            layout.insertWidget(layout.indexOf(self.tabs),self.cron_schedule)
            self.refresh_cron_summary()
        summary = ResourceDetails()
        summary.setObjectName("resourceDetails")
        summary.setFrameShape(QFrame.Shape.NoFrame)
        summary.document().setDocumentMargin(0)
        self.details = summary
        summary.set_summary(self._summary(resource))
        if kind == "deployment":
            overview = QWidget()
            overview_row = QVBoxLayout(overview)
            overview_row.setContentsMargins(18, 12, 18, 12)
            overview_row.setSpacing(8)
            overview_row.addWidget(QLabel("Deployment details"))
            summary.setMinimumWidth(0)
            pod_overview = QWidget()
            pod_column = QVBoxLayout(pod_overview)
            pod_column.setContentsMargins(0, 0, 0, 0)
            pod_column.setAlignment(Qt.AlignmentFlag.AlignTop)
            ring_row = QHBoxLayout()
            ring_row.setSpacing(4)
            self.previous_ring = PodDonut()
            self.previous_ring.setFixedSize(180, 180)
            self.previous_ring.hide()
            self.rollout_arrow = QLabel("→")
            self.rollout_arrow.hide()
            ring_row.addWidget(self.previous_ring)
            ring_row.addWidget(self.rollout_arrow)
            self.pod_donut = PodDonut()
            self.pod_donut.setFixedSize(180, 180)
            ring_row.addWidget(self.pod_donut)
            self.replica_up = DisabledHintButton("↑")
            self.replica_down = DisabledHintButton("↓")
            self.replica_up.setToolTip("Увеличить запланированное число реплик на 1")
            self.replica_down.setToolTip("Уменьшить запланированное число реплик на 1")
            for button, delta in ((self.replica_up, 1), (self.replica_down, -1)):
                button.setProperty('operationAction', 'scale')
                self.write_controls.append(button)
                button.clicked.connect(lambda checked=False, delta=delta: self.scale_by(delta))
            replica_column = QVBoxLayout()
            replica_column.setSpacing(2)
            replica_column.addStretch()
            replica_column.addWidget(self.replica_up)
            replica_column.addWidget(self.replica_down)
            replica_column.addStretch()
            ring_row.addLayout(replica_column)
            ring_row.addStretch(1)
            pod_column.addLayout(ring_row)
            self.scale_notice = QWidget()
            self.scale_notice.setObjectName("replicaDraftNotice")
            scale_actions = QHBoxLayout(self.scale_notice)
            scale_actions.setContentsMargins(0, 0, 0, 0)
            self.scale_apply = QPushButton("Apply")
            self.scale_cancel = QPushButton("Cancel")
            self.scale_apply.clicked.connect(self._commit_scale_draft)
            self.scale_cancel.clicked.connect(lambda: self._cancel_scale_draft(lock=True))
            scale_actions.addWidget(self.scale_apply)
            scale_actions.addWidget(self.scale_cancel)
            scale_actions.addStretch()
            self.scale_notice.hide()
            pod_column.addWidget(self.scale_notice)
            pod_column.addStretch()
            overview_row.addWidget(pod_overview)
            overview_row.addWidget(summary, 1)
            self.tabs.addTab(overview, "Обзор")
            self._update_replica_target(resource)
        else:
            overview = QWidget()
            overview_layout = QVBoxLayout(overview)
            overview_layout.setContentsMargins(18, 12, 18, 12)
            overview_layout.addWidget(summary)
            self.tabs.addTab(overview, "Обзор")
        self.yaml_editor = CodeEditor()
        self.yaml_editor.setObjectName("yamlPage")
        self.yaml_editor.setPlainText("")
        self.yaml_editor.editor.document().setModified(False)
        self._allow_discard = False
        self.yaml_editor.editor.textChanged.connect(self._yaml_changed)
        self.yaml_editor.editor.document().modificationChanged.connect(self._yaml_changed)
        self.yaml_notice = QWidget()
        self.yaml_notice.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        notice_row = QHBoxLayout(self.yaml_notice)
        notice_row.setContentsMargins(0, 0, 0, 0)
        self.yaml_notice_text = QLabel("YAML на сервере изменён.")
        notice_row.addWidget(self.yaml_notice_text)
        self.yaml_reload = QPushButton("Обновить")
        self.yaml_reload.setToolTip("Загрузить серверную версию YAML")
        self.yaml_reload.clicked.connect(self.reload_yaml)
        self.yaml_editor.toolbar.insertWidget(0, self.yaml_reload)
        self.yaml_editor.toolbar.insertWidget(1, self.yaml_notice)
        self.yaml_reload.setEnabled(False)
        self.yaml_notice.hide()
        self.tabs.addTab(self.yaml_editor, "YAML")
        if kind == "pod":
            self._pod_tabs()
            for label in ("Обзор", "Логи", "Terminal", "YAML", "Метрики", "События", "Контейнеры"):
                index = next(i for i in range(self.tabs.count()) if self.tabs.tabText(i) == label)
                widget = self.tabs.widget(index)
                self.tabs.removeTab(index)
                self.tabs.addTab(widget, label)
            self.tabs.setCurrentIndex(0)
        elif kind == "deployment":
            self._deployment_tabs()
            self.tabs.setCurrentIndex(0)
        elif kind == 'cronjob':
            from .cron_resources import CronJobResources
            self.cron_pods = CronJobResources(self, 'pod')
            self.cron_jobs = CronJobResources(self, 'job')
            self.tabs.addTab(self.cron_pods, 'Pods')
            self.tabs.addTab(self.cron_jobs, 'Jobs')
        if kind != "deployment":
            self.live_timer = QTimer(self)
            self.live_timer.setInterval(5000)
            self.live_timer.timeout.connect(lambda: self.refresh_object(notify_errors=False) if self.isVisible() else None)
            self.live_timer.start()
            mark_poll_timer(self.live_timer)
        controls = QHBoxLayout()
        save = DisabledHintButton("Сохранить YAML")
        self.yaml_save = save
        save.setProperty("primary", True)
        save.setToolTip("Показать изменения и отправить их в кластер после подтверждения · Ctrl+S")
        save.setProperty('operationAction', 'yaml')
        self.write_controls.append(save)
        save.clicked.connect(self.save_yaml)
        self.yaml_editor.toolbar.addWidget(save)
        self.yaml_draft_state = QLabel("")
        self.yaml_draft_state.setObjectName("yamlDraftState")
        self.yaml_editor.toolbar.insertWidget(2, self.yaml_draft_state)
        if kind == "deployment":
            resources = DisabledHintButton("Ресурсы…")
            self.resources_button = resources
            resources.setToolTip("Изменить запросы и лимиты CPU / памяти контейнера. Это запустит обновление Pod.")
            resources.setProperty('operationAction', 'resources')
            self.write_controls.append(resources)
            resources.clicked.connect(self.edit_resources)
            controls.addWidget(resources)
            restart = DisabledHintButton("Restart rollout")
            self.restart_button = restart
            restart.setToolTip("Запустить новую ревизию Deployment через обновление шаблона Pod, как Restart rollout в OpenShift.")
            restart.setProperty('operationAction', 'restart')
            self.write_controls.append(restart)
            restart.clicked.connect(self.restart)
            controls.addWidget(restart)
        if kind == "pod":
            remove = DisabledHintButton("Удалить Pod…")
            remove.setToolTip("Удалить выбранный экземпляр. Для управляемого Pod учитывается бюджет доступности (PDB).")
            remove.setProperty('operationAction', 'delete')
            self.write_controls.append(remove)
            remove.clicked.connect(self.delete_pod)
            controls.addWidget(remove)
        controls.addStretch()
        close = QPushButton("Назад")
        close.setToolTip("Вернуться к предыдущему экрану · Alt+← или боковая кнопка мыши")
        close.clicked.connect(self._go_back)
        controls.addWidget(close)
        header.addLayout(controls)
        if kind in ("pod", "deployment") and hasattr(self.controller, "store"):
            self._setup_navigation(layout)
        self._active_resource_tab = self.tabs.currentWidget()
        self.tabs.blockSignals(False)
        self.update_write_controls()

    def _resource_tab_changed(self, _):
        self._access_context_generation += 1
        previous = self._active_resource_tab
        self._active_resource_tab = self.tabs.currentWidget()
        if previous is not self._active_resource_tab:
            self.background.cancel_reads(tab_only=True)
            self._pod_metrics_loading = False
            from .read_lifecycle import suspend_children
            suspend_children(self, previous)
            self._cancel_scale_draft()
        if self._active_resource_tab is self.yaml_editor:
            self._ensure_yaml_ready()
        if self._active_resource_tab is getattr(self, 'log_view', None):
            self.log_view.loading = False
            self.log_view.load()
        if self.kind == 'deployment' and self.tabs.tabText(self.tabs.currentIndex()) in ('Pods', 'Метрики'):
            self._rollout_cadence.request_fast()
            self.reload_deployment_pods()
        if previous is getattr(self, "terminal_page", None) and previous is not self._active_resource_tab:
            self._lock_terminal_access()
        self.update_write_controls()
        if hasattr(self.controller, "admin_access"):
            self.controller.admin_access.update()
        if hasattr(self.controller, 'shell_actions'):
            self.controller.shell_actions.sync_admin_timer()

    def showEvent(self, event):
        super().showEvent(event)
        logs = getattr(self, 'log_view', None)
        if logs is not None and self.tabs.currentWidget() is logs and logs._source_text is None:
            logs.load()
        if self.tabs.currentWidget() is self.yaml_editor and getattr(self, '_yaml_reset_pending', False):
            self._ensure_yaml_ready()

    def hideEvent(self, event):
        if not event.spontaneous(): self.suspend_reads()
        super().hideEvent(event)

    def suspend_reads(self):
        from .resource_read_controller import ResourceReadController
        ResourceReadController(self).suspend()
        if hasattr(self, "terminal_page") and self.tabs.currentWidget() is self.terminal_page:
            self._lock_terminal_access()

    def _setup_navigation(self, layout):
        self.nav_cluster, self.nav_namespace, self.nav_deployment, self.nav_pod = (QComboBox() for _ in range(4))
        bar = QWidget()
        self.navigation_bar = bar
        bar.setObjectName("resourceNavigation")
        grid = QGridLayout(bar)
        grid.setContentsMargins(18, 4, 18, 8)
        grid.setHorizontalSpacing(12)
        for column, (label, combo) in enumerate((("Кластер", self.nav_cluster), ("Namespace", self.nav_namespace), ("Deployment", self.nav_deployment), ("Pod", self.nav_pod))):
            if label == "Pod" and self.kind == "deployment":
                combo.setParent(bar)
                combo.hide()
                continue
            grid.addWidget(QLabel(label), 0, column)
            combo.setMinimumWidth(80)
            combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            grid.setColumnStretch(column, 1)
            grid.addWidget(combo, 1, column)
        for combo, tip in ((self.nav_cluster, "Окружение, в котором находится объект."),
                (self.nav_namespace, "Область ресурсов внутри выбранного кластера."),
                (self.nav_deployment, "Приложение: шаблон и нужное число копий."),
                (self.nav_pod, "Конкретный экземпляр приложения с контейнерами.")):
            combo.setToolTip(tip)
        previous_context = self.findChild(QWidget, "resourceContext")
        if previous_context:
            previous_context.hide()
        status_key = "Статус" if self.kind == "pod" else "Pods"
        if self.kind == "pod" and status_key in self.context_values:
            grid.addWidget(QLabel("Реплики факт./запланировано" if self.kind == "deployment" else status_key), 0, 4)
            grid.addWidget(self.context_values[status_key], 1, 4)
        layout.insertWidget(2, bar)
        clusters = [(c["name"], c["id"]) for c in self.controller.store.clusters() if c.get("enabled") or c["id"] == self.cluster_id]
        self._fill_navigation(self.nav_cluster, clusters, self.cluster_id)
        self.nav_cluster.activated.connect(lambda *_: self._load_navigation_namespaces(True))
        self.nav_namespace.activated.connect(lambda *_: self._load_navigation_scope(True))
        self.nav_deployment.activated.connect(lambda *_: self._choose_navigation_deployment(True))
        self.nav_pod.activated.connect(lambda *_: self._navigate_to("pod", self.nav_pod.currentData()))
        self._navigation_pending = False
        self._navigation_request = 0
        self._load_navigation_namespaces(False)

    @staticmethod
    def _fill_navigation(combo, entries, current):
        blocked = combo.blockSignals(True)
        entries = list(entries)
        if [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())] != entries:
            combo.clear()
            for label, value in entries:
                combo.addItem(label, value)
        found = combo.findData(current)
        combo.setCurrentIndex(found if found >= 0 else 0)
        combo.blockSignals(blocked)
        combo.setEnabled(bool(entries))

    def _load_navigation_namespaces(self, navigate):
        if navigate and self.pending_operation:
            self._restore_navigation()
            return
        self._navigation_pending = bool(navigate)
        cluster = self.nav_cluster.currentData()
        if not cluster:
            return
        self._navigation_request += 1
        request = self._navigation_request
        self.nav_namespace.setEnabled(False)
        self.nav_deployment.setEnabled(False)
        self.nav_pod.setEnabled(False)
        def done(namespaces):
            if request != self._navigation_request:
                return
            self._fill_navigation(self.nav_namespace, [(n, n) for n in namespaces], self.namespace)
            self._load_navigation_scope(navigate)
        if self.background.submit(lambda: self.api.namespaces(cluster), done,
                lambda message: self._navigation_failed(request, message),
                cancelled=lambda: self._navigation_failed(request, 'Переход отменён.')) is False:
            self._navigation_failed(request, 'Переход отложен: очередь занята.')

    def _load_navigation_scope(self, navigate):
        if navigate and self.pending_operation:
            self._restore_navigation()
            return
        self._navigation_pending = bool(navigate)
        cluster, namespace = self.nav_cluster.currentData(), self.nav_namespace.currentData()
        if not cluster or not namespace:
            return
        self._navigation_request += 1
        request = self._navigation_request
        self.nav_deployment.setEnabled(False)
        self.nav_pod.setEnabled(False)
        def load():
            if self.kind == "deployment":
                return self.api.deployments(cluster, namespace), [], []
            return self.api.deployments(cluster, namespace), self.api.replica_sets(cluster, namespace), self.api.pods(cluster, namespace)
        def done(result):
            if request != self._navigation_request:
                return
            deployments, sets, self._navigation_pods = result
            self._navigation_deployments = {d.get("metadata", {}).get("uid"): d.get("metadata", {}).get("name") for d in deployments}
            self._navigation_sets = {r.get("metadata", {}).get("uid"): next((o.get("uid") for o in r.get("metadata", {}).get("ownerReferences", []) if o.get("controller") and o.get("kind") == "Deployment"), None) for r in sets}
            selected = self.name if self.kind == "deployment" else self._pod_deployment(self.resource)
            entries = [(d.get("metadata", {}).get("name", ""), d.get("metadata", {}).get("name", "")) for d in deployments]
            if any(not self._pod_deployment(p) for p in self._navigation_pods):
                entries.append(("Без Deployment", ""))
            self._fill_navigation(self.nav_deployment, entries, selected)
            self._choose_navigation_deployment(navigate)
        if self.background.submit(load, done, lambda message: self._navigation_failed(request, message),
                cancelled=lambda: self._navigation_failed(request, 'Переход отменён.')) is False:
            self._navigation_failed(request, 'Переход отложен: очередь занята.')

    def _navigation_failed(self, request, message):
        if request == self._navigation_request:
            self._restore_navigation()
            self.nav_cluster.setToolTip(message)

    def _restore_navigation(self):
        from PyQt5.QtCore import QSignalBlocker
        blockers = [QSignalBlocker(combo) for combo in
                    (self.nav_cluster, self.nav_namespace, self.nav_deployment, self.nav_pod)]
        self._navigation_pending = False
        self.update_write_controls()
        self._navigation_request += 1
        self.nav_cluster.setCurrentIndex(self.nav_cluster.findData(self.cluster_id))
        self._fill_navigation(self.nav_namespace, [(self.namespace, self.namespace)], self.namespace)
        self.nav_deployment.setCurrentIndex(self.nav_deployment.findData(self.name if self.kind == "deployment" else self._pod_deployment(self.resource)))
        self.nav_pod.setCurrentIndex(self.nav_pod.findData(self.name))

    def _pod_deployment(self, pod):
        owner = next((o for o in pod.get("metadata", {}).get("ownerReferences", []) if o.get("controller")), {})
        uid = owner.get("uid") if owner.get("kind") == "Deployment" else self._navigation_sets.get(owner.get("uid"))
        return self._navigation_deployments.get(uid, "")

    def _choose_navigation_deployment(self, navigate):
        deployment = self.nav_deployment.currentData()
        pods = [p for p in self._navigation_pods if self._pod_deployment(p) == deployment]
        self._fill_navigation(self.nav_pod, [(p["metadata"]["name"], p["metadata"]["name"]) for p in pods], self.name)
        if navigate:
            if deployment:
                self._navigate_to("deployment", deployment)
            elif self.kind == "pod" and self.nav_pod.currentData():
                self._navigate_to("pod", self.nav_pod.currentData())

    def _navigate_to(self, kind, name):
        if self.pending_operation or not name:
            self._restore_navigation()
            return
        cluster, namespace = self.nav_cluster.currentData(), self.nav_namespace.currentData()
        if (cluster, kind, namespace, name) == (self.cluster_id, self.kind, self.namespace, self.name):
            self._navigation_pending = False
            return
        retaining_parent = getattr(self, "managed_panel", False) and self.kind == "deployment" and kind == "pod"
        if not retaining_parent and not self.can_leave():
            self.nav_cluster.setCurrentIndex(self.nav_cluster.findData(self.cluster_id))
            self._load_navigation_namespaces(False)
            return
        self._restore_navigation()
        uid = None
        if kind == "pod":
            uid = next((p.get("metadata", {}).get("uid") for p in self._navigation_pods if p.get("metadata", {}).get("name") == name), None)
        elif kind == "deployment":
            uid = next((key for key, value in self._navigation_deployments.items() if value == name), None)
        self.controller.open_resource(cluster, kind, namespace, name, initial_tab=self.tabs.tabText(self.tabs.currentIndex()), expected_uid=uid)

    def _go_back(self):
        if self.pending_operation:
            return
        if getattr(self, "managed_panel", False):
            self.close()
            return
        if not self.can_leave():
            return
        target = getattr(self, "back_target", None)
        if hasattr(self.controller, "_resource_request"):
            self.controller._resource_request += 1
        self.close()
        if target:
            self.controller.open_resource(*target)

    def refresh_cron_summary(self):
        if self.kind != 'cronjob' or not hasattr(self.controller,'schedules'): return
        from .schedule import last_run
        scope = self.controller.snapshot.get(self.cluster_id,{}).get('namespaces',{}).get(self.namespace,{})
        visual = self.controller.schedules.visual(self.cluster_id,self.namespace,self.resource,scope)
        text = html.escape(visual.schedule.tooltip(datetime.now(timezone.utc),
                           confirmed_snapshot=visual.schedule_confirmed,stale=visual.stale)).replace('\n','<br>')
        latest = None if 'job' in scope.get('_type_errors',{}) else last_run(self.resource.get('metadata',{}).get('uid'),scope.get('jobs',[]))
        if latest:
            job,result,started,source = latest
            text += f'<br>Last run: <b>{html.escape(result)}</b> · {html.escape(started)} ({html.escape(source)})'
            uid = job.get('metadata',{}).get('uid','')
            text += f' · <a href="job/{html.escape(uid,quote=True)}">Open Job</a>'
            for pod in scope.get('pods',[]):
                if any(o.get('controller') and o.get('kind')=='Job' and o.get('uid')==uid for o in pod.get('metadata',{}).get('ownerReferences',[])):
                    meta = pod.get('metadata',{})
                    text += f' · <a href="pod/{html.escape(meta.get("uid",""),quote=True)}">{html.escape(meta.get("name","Pod"))}</a>'
        else:
            text += '<br>Last run: Unknown — history unavailable or removed.'
        success = self.resource.get('status',{}).get('lastSuccessfulTime')
        if success: text += '<br>Last success: '+html.escape(success)
        if self.cron_schedule.text()!=text: self.cron_schedule.setText(text)
        self.controller.schedules.flush()

    def _cron_link(self,link):
        kind,_,uid = link.partition('/')
        if kind not in ('job','pod') or not uid: return
        scope = self.controller.snapshot.get(self.cluster_id,{}).get('namespaces',{}).get(self.namespace,{})
        for value in scope.get('jobs' if kind=='job' else 'pods',[]):
            meta = value.get('metadata',{})
            if meta.get('uid')==uid:
                self.controller.open_resource(self.cluster_id,kind,self.namespace,meta['name'],expected_uid=uid)
                return

    def _summary(self, resource: dict) -> str:
        metadata, status = resource.get("metadata", {}), resource.get("status", {})
        values = {"Имя": metadata.get("name"), "Namespace": metadata.get("namespace"),
                  "Кластер": self.api.cluster(self.cluster_id).get("name"), "Создан": metadata.get("creationTimestamp"),
                  "Статус": status.get("phase"), "IP": status.get("podIP"), "Реплики": status.get("replicas"),
                  "Доступно": status.get("availableReplicas")}
        if self.kind == "deployment":
            desired = resource.get("spec", {}).get("replicas", 1)
            values.update({"Реплики факт./запланировано": f"{status.get('replicas', status.get('readyReplicas', 0))} / {desired}", "Обновлено": status.get("updatedReplicas", 0), "Недоступно": status.get("unavailableReplicas", 0), "Стратегия": resource.get("spec", {}).get("strategy", {}).get("type", "RollingUpdate")})
        elif self.kind == "pod":
            values.update({"Узел": resource.get("spec", {}).get("nodeName"), "Причина": status.get("reason"), "Сообщение": status.get("message")})
        elif self.kind == 'cronjob':
            spec = resource.get('spec', {})
            job = spec.get('jobTemplate', {}).get('spec', {})
            values.update({'Расписание': spec.get('schedule', '—'), 'Приостановлена': 'Да' if spec.get('suspend') else 'Нет',
                           'Политика одновременных запусков': spec.get('concurrencyPolicy', 'Allow'),
                           'Допустимое опоздание запуска, с': spec.get('startingDeadlineSeconds', '—'),
                           'Последний запуск': status.get('lastScheduleTime', '—'),
                           'Последний успешный запуск': status.get('lastSuccessfulTime', '—'),
                           'Хранить успешных Job': spec.get('successfulJobsHistoryLimit', 3),
                           'Хранить неудачных Job': spec.get('failedJobsHistoryLimit', 1),
                           'Job · успешных завершений': job.get('completions', 1),
                           'Job · параллелизм': job.get('parallelism', 1),
                           'Job · предельное время, с': job.get('activeDeadlineSeconds', '—'),
                           'Job · число повторов': job.get('backoffLimit', 6)})
        text = ("" if self.kind == "deployment" else "<p>" + html.escape(self.kind.title()) + " details</p>") + "<table cellspacing='10'>"
        text += "".join(f"<tr><td>{label}</td><td><b>{html.escape(str(value))}</b></td></tr>" for label, value in values.items() if value is not None)
        text += "</table><p>Labels</p>"
        labels = metadata.get("labels", {})
        text += "<table cellspacing='8'>" + "".join(f"<tr><td>{html.escape(str(key))}</td><td><b>{html.escape(str(value))}</b></td></tr>" for key, value in sorted(labels.items())) + "</table>" if labels else "<p>No labels</p>"
        text += "<p>Состояния</p><table cellspacing='8'><tr><th align='left'>Проверка</th><th align='left'>Статус</th><th align='left'>Причина / сообщение</th><th align='left'>Изменено</th></tr>"
        for condition in status.get("conditions", []):
            title = {"Available": "Доступность", "Progressing": "Ход обновления", "Ready": "Готов к трафику", "ContainersReady": "Контейнеры готовы", "PodScheduled": "Назначен узел", "ReplicaFailure": "Ошибка создания реплик"}.get(condition.get("type"), condition.get("type", ""))
            state = {"True": "Да", "False": "Нет", "Unknown": "Unknown"}.get(str(condition.get("status")), str(condition.get("status", "")))
            text += "<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in (title, state, " / ".join(filter(None, (condition.get("reason"), condition.get("message")))), condition.get("lastTransitionTime", ""))) + "</tr>"
        if not status.get("conditions"):
            text += "<tr><td colspan='4'>Данных о состояниях нет.</td></tr>"
        text += "</table>"
        return text


    def _pod_tabs(self):
        container_names = [c.get("name", "") for key in ("containers", "initContainers", "ephemeralContainers") for c in self.resource.get("spec", {}).get(key, [])]
        from .pod_logs import PodLogs
        self.log_view = PodLogs(self, container_names, defer_initial=True)
        self.container = self.log_view.container
        self.previous = self.log_view.previous
        self.all_logs = self.log_view.expanded
        self.follow_logs = self.log_view.follow
        self.log_search = self.log_view.search
        self.logs = self.log_view.output
        self.log_timer = self.log_view.timer
        self.tabs.addTab(self.log_view, "Логи")
        terminal = QWidget()
        terminal_layout = QVBoxLayout(terminal)
        console_container = QComboBox()
        console_container.addItems(container_names)
        console_container.setCurrentText(preferred_container([c.get("name", "") for c in self.resource.get("spec", {}).get("containers", [])]) or console_container.currentText())
        self.console_container = console_container
        console_container.setToolTip("В каком контейнере выполнить команду. Выбор контейнера для логов независим.")
        terminal_layout.addWidget(console_container)
        self.console_output = BoundedConsole()
        self.console_output.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.console_output.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.console_output.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.console_output.setObjectName("podConsole")
        console_container.currentTextChanged.connect(self.lock_console)
        self.console_output.setReadOnly(True)
        terminal_layout.addWidget(self.console_output)
        console_row = QHBoxLayout()
        self.console_input = QLineEdit()
        self.console_input.setPlaceholderText("/bin/sh -lc команда")
        self.console_input.setToolTip("Команда внутри контейнера. Она может изменить приложение или его данные.")
        self.console_input.returnPressed.connect(self.run_console)
        console_row.addWidget(self.console_input)
        execute = DisabledHintButton("Выполнить")
        execute.setToolTip("Выполнить команду в выбранном контейнере · Enter. Требуется разрешение изменений.")
        execute.setProperty('operationAction', 'exec')
        self.write_controls.append(execute)
        execute.clicked.connect(self.run_console)
        console_row.addWidget(execute)
        self.console_lock = QPushButton("🔒")
        self.console_lock.setToolTip("Выключить режим администратора")
        self.console_lock.clicked.connect(self._lock_terminal_access)
        console_row.addWidget(self.console_lock)
        terminal_layout.addLayout(console_row)
        self.tabs.addTab(terminal, "Terminal")
        self.terminal_page = terminal
        self.console_unlocked_until = 0.0
        self.terminal_timer = QTimer(self)
        self.terminal_timer.setSingleShot(True)
        self.terminal_timer.setInterval(60000)
        self.terminal_timer.timeout.connect(self._lock_terminal_access)
        self._info_tab("События", lambda: self.api.events(self.cluster_id, self.namespace, "Pod", self.name, self.resource.get("metadata", {}).get("uid")))
        self.tabs.addTab(MetricsWidget(self, self.api, self.background, self.cluster_id, self.namespace, self.name), "Метрики")
        self._info_tab("Контейнеры", lambda: [dict(c, containerType=label) for key, label in (
            ("containers", "Основной"), ("initContainers", "Инициализация"), ("ephemeralContainers", "Отладка"))
            for c in self.resource.get("spec", {}).get(key, [])])
        self.tabs.setCurrentIndex(next(i for i in range(self.tabs.count()) if self.tabs.tabText(i) == "Логи"))

    def lock_console(self):
        self.console_unlocked_until = 0.0
        self.console_lock.setText("🔒")
        self.terminal_timer.stop()

    def _lock_terminal_access(self):
        self.lock_console()
        if hasattr(self.controller, "set_read_only"):
            self.controller.set_read_only(True)

    def run_console(self):
        if self.tabs.currentWidget() is not self.terminal_page:
            return
        command = self.console_input.text().strip()
        container = self.console_container.currentText()
        if not command or not container:
            return
        if not self._ensure_write():
            return
        cid, _, namespace, name, uid = self._opened_identity
        if not self._ensure_write():
            return
        remaining = self.terminal_timer.remainingTime()
        if remaining <= 0:
            self._lock_terminal_access()
            return
        from .container_instance import capture_instance
        try:
            approved_instance = capture_instance(self.resource,container)
        except PermissionError as exc:
            self.console_output.append(str(exc))
            return
        self.console_unlocked_until = time.monotonic() + remaining / 1000
        self.console_lock.setText("🔓")
        self.console_output.append(f"{container}@{self.name}:~$ {command}")
        def execute():
            try:
                return self.api.execute_pod(cid, namespace, name, uid, container, command,
                                            expected_instance=approved_instance,permit=current_permit())
            except Exception as exc:
                if server_forbidden(exc) and hasattr(self.controller, 'access_denied'):
                    self.controller.access_denied.emit(cid, 'POST', f'/api/v1/namespaces/{namespace}/pods/{name}/exec')
                    raise PermissionError('Нет доступа к Terminal этого Pod. Режим администратора выключен.') from exc
                raise
        def terminal_failed(message):
            if message.startswith(("Режим администратора недоступен", "Не удалось подтвердить права администратора")):
                self.controller.set_read_only(True)
                self.console_output.append(message)
                return
            if server_forbidden(message) or message.startswith(("Нет доступа к Terminal", "Kubernetes запретил доступ (403)")):
                if hasattr(self.controller, "set_read_only"):
                    self.controller.set_read_only(True)
                self.console_output.append("Нет доступа к Terminal этого Pod. Режим администратора выключен.")
            else:
                self.console_output.append(f"Ошибка: {message}")
        def terminal_done(result):
            if self.console_input.text() == command:
                self.console_input.clear()
            self.console_output.append("\n".join(part for part in result if part) or "(нет вывода)")
        self._mutate(execute,
                     terminal_done,
                     terminal_failed,
                     terminal=True, parameters={'container': container, 'command': command,
                                                'instance':approved_instance.parameters()})

    def _deployment_tabs(self):
        from PyQt5.QtWidgets import QHeaderView
        self.pod_table = ResourceList()
        self.pod_table.set_empty_text("Загрузка Pod…")
        table = self.pod_table
        table.setColumnCount(7)
        table.setHorizontalHeaderLabels(["Статус", "Имя", "Возраст", "IP", "Рестарты", "CPU", "RAM"])
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.setItemDelegateForColumn(0, StatusDelegate(table))
        table.cellDoubleClicked.connect(self._open_deployment_pod)
        page = QWidget()
        page_layout = QVBoxLayout(page)
        controls = QHBoxLayout()
        search = QLineEdit()
        search.setPlaceholderText("Найти Pod, IP или состояние…")
        search.setClearButtonEnabled(True)
        search.setToolTip("Поиск по загруженным экземплярам Deployment · Ctrl+F")
        search.textChanged.connect(table.set_filter)
        page.search_field = search
        controls.addWidget(search, 1)
        if hasattr(self.controller, "resource_actions"):
            self.controller.resource_actions.bind_table(table,
                lambda entry: (self.cluster_id, "pod", self.namespace, *entry.data(1, Qt.ItemDataRole.UserRole)), controls)
        page_layout.addLayout(controls)
        page_layout.addWidget(table)
        self.tabs.addTab(page, "Pods")
        self.deployment_notice = QLabel("")
        self.deployment_notice.setWordWrap(True)
        self.deployment_notice.setObjectName("deploymentNotice")
        self.deployment_notice.hide()
        table.setToolTip("Двойной клик или Enter — открыть. ПКМ или Shift+F10 — логи, события и удаление Pod.")
        self.layout().insertWidget(2, self.deployment_notice)
        self._pods_loading = False
        self._pod_metrics_loading = False
        self._pod_metrics = {}
        self._displayed_pods = []
        from .rollout import RolloutCadence
        self._rollout_cadence = RolloutCadence()
        self.deployment_timer = QTimer(self)
        self.deployment_timer.timeout.connect(lambda: self.reload_deployment_pods() if self.isVisible() else None)
        self.deployment_timer.start(5000)
        mark_poll_timer(self.deployment_timer)
        self.deployment_period = QLabel("Автообновление: 5 сек")
        self.deployment_period.setObjectName("resourceRefresh")
        self.deployment_period.setContentsMargins(18, 4, 18, 4)
        self.layout().insertWidget(2, self.deployment_period)
        self.reload_deployment_pods()
        self._info_tab("События", lambda: self.api.events(self.cluster_id, self.namespace, "Deployment", self.name, self.resource.get("metadata", {}).get("uid")))
        self._info_tab("Метрики", lambda: {"pods": []})

    def _open_deployment_pod(self, row, column):
        cell = self.pod_table.item(row, 1)
        if cell is not None:
            name, uid = cell.data(Qt.ItemDataRole.UserRole)
            self.controller.open_resource(self.cluster_id, "pod", self.namespace, name, expected_uid=uid)

    def reload_deployment_pods(self):
        from .resource_read_controller import ResourceReadController
        ResourceReadController(self).reload_deployment_pods()

    def _apply_pod_metrics(self):
        for row in range(self.pod_table.rowCount()):
            name = self.pod_table.item(row, 1)
            if name is None:
                continue
            sample = self._pod_metrics.get(tuple(name.data(Qt.ItemDataRole.UserRole)), {})
            for column, resource in ((5, "cpu"), (6, "memory")):
                percent = resource_percent(sample, resource)
                cell = item("—" if percent is None else f"{percent:.0f}%")
                cell.setToolTip(sample.get("error") or "Limit не задан или метрики недоступны" if percent is None else "Использовано от limit")
                if percent is not None:
                    cell.setForeground(QColor("#ff5b5b" if percent >= 90 else "#f3b63f" if percent >= 70 else "#22c796"))
                self.pod_table.setItem(row, column, cell)

    def _info_tab(self, label: str, loader):
        table = ResourceList()
        table.set_empty_text("Загрузка…")
        table.setWordWrap(True)
        self.tabs.addTab(table, label)
        if label == "Метрики" and self.kind == "deployment":
            self.deployment_metrics_table = table
            index = self.tabs.indexOf(table)
            self.tabs.removeTab(index)
            metrics_page = QWidget()
            metrics_layout = QVBoxLayout(metrics_page)
            table.setMaximumHeight(180)
            table.setMinimumHeight(100)
            metrics_layout.addWidget(table)
            table.show()
            self.metrics_pod = QComboBox()
            metrics_layout.addWidget(self.metrics_pod)
            self.metrics_graph_layout = QVBoxLayout()
            metrics_layout.addLayout(self.metrics_graph_layout, 1)
            self.tabs.insertTab(index, metrics_page, label)
            metrics_page.refresh_data = self.reload_deployment_pods
            self.metrics_pod.currentTextChanged.connect(self._select_deployment_graph)
        @batch_table(lambda *_: table)
        def display(value):
            metric_pods = value.get("pods", []) if label == "Метрики" else []
            if label == "События":
                headings = ["Тип", "Причина", "Сообщение", "Источник", "Дата", "Кол-во"]
                rows = [[e.get("type"), e.get("reason"), e.get("message"), (e.get("source") or {}).get("component"), e.get("lastTimestamp") or e.get("metadata", {}).get("creationTimestamp"), e.get("count")] for e in value]
            elif label == "Контейнеры":
                headings = ["Имя", "Назначение", "Образ", "Порты", "CPU request / limit", "RAM request / limit"]
                rows = []
                for c in value:
                    r = c.get("resources", {})
                    rows.append([c.get("name"), c.get("containerType", "Основной"), c.get("image"), ", ".join(str(p.get("containerPort")) for p in c.get("ports", [])),
                                 " / ".join(str(r.get(k, {}).get("cpu", "—")) for k in ("requests", "limits")),
                                 " / ".join(str(r.get(k, {}).get("memory", "—")) for k in ("requests", "limits"))])
            else:
                headings = ["Pod", "Контейнер", "CPU (cores)", "RAM (MiB)", "Источник"]
                rows = [[m.get("pod", ""), c.get("name"), c.get("cpu", {}).get("usageCores"),
                         f"{c['memory']['usageBytes']/1048576:.2f}" if c.get("memory", {}).get("usageBytes") is not None else "—",
                         m.get("source")] for m in value.get("pods", []) for c in m.get("containers", [])]
            table.setColumnCount(len(headings))
            table.setHorizontalHeaderLabels(headings)
            table.setRowCount(len(rows))
            table.set_empty_text({"События": "Событий нет.", "Контейнеры": "Контейнеры не найдены.", "Метрики": "Метрики пока недоступны."}.get(label, "Нет данных."))
            for row, values in enumerate(rows):
                for column, value in enumerate(values):
                    table.setItem(row, column, item(value))
            table.resizeColumnsToContents()
            if label == "Метрики" and self.kind == "deployment":
                names = [pod.get("pod", "") for pod in metric_pods if pod.get("pod")]
                selected = self.metrics_pod.currentText()
                if names != [self.metrics_pod.itemText(i) for i in range(self.metrics_pod.count())]:
                    blocked = self.metrics_pod.blockSignals(True)
                    self.metrics_pod.clear()
                    self.metrics_pod.addItems(names)
                    if selected in names:
                        self.metrics_pod.setCurrentText(selected)
                    self.metrics_pod.blockSignals(blocked)
                self._select_deployment_graph(self.metrics_pod.currentText())
        if label == "Метрики" and self.kind == "deployment":
            table.refresh_data = self.reload_deployment_pods
            self._deployment_metrics_display = display
            display({"pods": [dict(value, pod=identity[0]) for identity, value in self._pod_metrics.items()]})
        else:
            def refresh_info():
                if getattr(table, "_loading", False) or not polling_allowed(self):
                    return
                table._loading = True
                def done(value):
                    table._loading = False
                    display(value)
                def failed(message):
                    table._loading = False
                    self._info_failure(table, message)
                self.background.submit(loader, done, failed, demand='tab')
            table.refresh_data = refresh_info
            self.tabs.currentChanged.connect(lambda _: refresh_info() if self.tabs.currentWidget() is table else None)
            if self.tabs.currentWidget() is table:
                refresh_info()
            timer = QTimer(table)
            timer.timeout.connect(lambda: refresh_info() if table.isVisible() else None)
            timer.start(10000)
            mark_poll_timer(timer)

    def _select_deployment_graph(self, name):
        pod = next((p for p in self._displayed_pods if p.get("metadata", {}).get("name") == name), {})
        identity = (name, pod.get("metadata", {}).get("uid"))
        if getattr(self, "_deployment_graph_identity", None) == identity:
            return
        self._deployment_graph_identity = identity
        previous = getattr(self, "deployment_graph", None)
        if previous is not None:
            previous.timer.stop()
            previous.hide()
            self.metrics_graph_layout.removeWidget(previous)
            previous.deleteLater()
        self.deployment_graph = None
        if name:
            graph = MetricsWidget(self, self.api, self.background, self.cluster_id, self.namespace, name)
            graph.background = PanelBackground(self.background, graph)
            graph.history_identity = (self.cluster_id, self.namespace, name, identity[1])
            self.metrics_graph_layout.addWidget(graph)
            self.deployment_graph = graph

    def _info_failure(self, table, message):
        table.setColumnCount(1)
        table.setHeaderLabels(["Диагностика недоступна"])
        table.setRowCount(1)
        table.setItem(0, 0, item(message))

    def load_logs(self, *_):
        self.log_view.load()

    def export_logs(self):
        self.log_view.export()

    def target_label(self):
        cid, _, namespace, name, _ = self._opened_identity
        return f"{self.api.cluster(cid)['name']} / {namespace} / {name}"

    def operation_label(self):
        kind = {"pod": "Pod", "deployment": "Deployment", "configmap": "ConfigMap"}.get(self._opened_identity[1], self._opened_identity[1])
        return f"{kind} · {self.target_label()}"

    def _matches_opened_target(self):
        return self._opened_identity == (self.cluster_id, self.kind, self.namespace, self.name,
                                         self.resource.get("metadata", {}).get("uid"))

    def _accept_yaml_text(self, text):
        self.yaml_editor.setPlainText(text)
        self.yaml_editor.editor.document().setModified(False)
        self._yaml_loading = False
        self._yaml_ready = True
        self._yaml_changed()
        self.update_write_controls()

    def _ensure_yaml_ready(self):
        if getattr(self, '_yaml_reset_pending', False):
            self.reload_yaml(discard=True)
            return
        if self._yaml_ready or self._yaml_loading or not self.background.active:
            return
        self._yaml_loading = True
        self._yaml_generation += 1
        token = (self._opened_identity, self._resource_epoch, self._yaml_generation)
        base = self.yaml_base_resource
        self.yaml_draft_state.setText("Подготовка YAML…")
        self.update_write_controls()
        def valid():
            return token == (self._opened_identity, self._resource_epoch, self._yaml_generation)
        def done(text):
            if not valid():
                return
            self._yaml_loading = False
            if self.yaml_editor.editor.document().isModified():
                return
            self._accept_yaml_text(text)
        def failed(message):
            if valid():
                self._yaml_loading = False
                self.yaml_draft_state.setText("Не удалось подготовить YAML: " + message)
                self.update_write_controls()
        self.background.submit(lambda: dump_resource(base), done, failed, monitor=False,
                               finalized=lambda outcome: setattr(self, '_yaml_loading', False) if valid() else None)

    def save_yaml(self):
        if not self._yaml_ready:
            return
        if not self._ensure_write(draft_action=True):
            return
        try:
            raw = self.yaml_editor.toPlainText()
            if len(raw) > 500000:
                raise ValueError("YAML превышает поддерживаемые 500000 символов.")
            edited = yaml.safe_load(raw)
            from .safety import validate_document
            validate_document(edited)
            if not isinstance(edited, dict):
                raise ValueError("YAML должен содержать объект Kubernetes.")
        except Exception as exc:
            error(self, str(exc))
            return
        before = dump_resource(self.yaml_base_resource, allow_unicode=True, sort_keys=False).splitlines(keepends=True)
        after = dump_resource(edited, allow_unicode=True, sort_keys=False).splitlines(keepends=True)
        diff = "".join(difflib.unified_diff(before, after, fromfile="Текущий объект", tofile="После изменения"))
        if not diff:
            QMessageBox.information(self, "Quaber", "Изменений нет.")
            return
        cid, kind, namespace, name, _ = self._opened_identity
        version = self.version
        revision = self.yaml_editor.editor.document().revision()
        guard = getattr(self.controller, 'write_guard', None)
        generation = guard.generation if guard is not None else None
        if not confirm(self, "Сохранить YAML", "Применить изменения YAML?", target=self.operation_label(), details=diff):
            if hasattr(self.controller, 'set_read_only'):
                self.controller.set_read_only(True)
            return
        if (self.yaml_editor.editor.document().revision() != revision or self.version != version
                or getattr(self, '_yaml_reset_pending', False)
                or (guard is not None and guard.generation != generation)):
            if hasattr(self.controller, 'set_read_only'):
                self.controller.set_read_only(True)
            if hasattr(self.controller, 'statusBar'):
                self.controller.statusBar().showMessage('YAML или права изменились во время подтверждения. Проверьте YAML и сохраните заново.', 10000)
            return
        self._mutate(lambda: self.api.replace(cid, kind, namespace, name, edited, version, permit=current_permit()), self._updated,
                     parameters=edited)

    def update_write_controls(self):
        readonly = (getattr(self.controller, "preferences", {}).get("read_only", True) if hasattr(self.controller, "store") else False) or self.kind not in ("pod", "deployment", "configmap")
        operation_busy = self.pending_operation or getattr(self, "_navigation_pending", False) or getattr(self.controller, "_write_operation", None) is not None
        draft_owner = getattr(self.controller, "_replica_draft_owner", None)
        busy = operation_busy or draft_owner is not None
        for button in self.write_controls:
            original = self._write_tooltips.setdefault(button, button.toolTip())
            button.setEnabled(not readonly and not busy)
            button.setToolTip("Ресурс доступен только для просмотра." if self.kind not in ("pod", "deployment", "configmap") else
                              "Включите режим администратора." if readonly else
                              "Дождитесь завершения текущей операции." if busy else original)
        if hasattr(self, "resources_button") and self.tabs.tabText(self.tabs.currentIndex()) != "Метрики":
            self.resources_button.setEnabled(False)
            self.resources_button.setToolTip("Настройка ресурсов доступна на вкладке «Метрики».")
        self.yaml_editor.editor.setReadOnly(readonly or busy or not self._yaml_ready)
        if not self._yaml_ready:
            self.yaml_save.setEnabled(False)
        self._update_access_style(not readonly)
        if self.kind == "deployment" and not readonly and not operation_busy and draft_owner in (None, self):
            for button in (self.replica_up, self.replica_down):
                button.setEnabled(True)
                button.setToolTip(self._write_tooltips[button])
            if self._hpa_controllers:
                for button in (self.replica_up, self.replica_down):
                    button.setEnabled(False)
                    button.setToolTip("Числом реплик управляет HPA: " + ", ".join(self._hpa_controllers))
            elif (self._scaling_target if self._scaling_target is not None else self.resource.get("spec", {}).get("replicas", 1)) <= 0:
                self.replica_down.setEnabled(False)
                self.replica_down.setToolTip("Число реплик уже равно нулю.")
        if hasattr(self, "terminal_timer"):
            if readonly or self.tabs.currentWidget() is not self.terminal_page:
                self.lock_console()
            elif not self.terminal_timer.isActive():
                self.terminal_timer.start()
        access = getattr(self.controller, 'admin_access', None)
        if access is not None:
            for button in self.write_controls:
                action = button.property('operationAction')
                if action and not access.allows(action):
                    button.setEnabled(False)
                    button.setToolTip('Действие не разрешено или права ещё не проверены для этой цели.')
            if not access.may_edit_yaml():
                self.yaml_editor.editor.setReadOnly(True)

    def _ensure_write(self, draft_action=False, *, replica_step=False):
        if self.kind not in ("pod", "deployment", "configmap"):
            error(self, "Ресурс доступен только для просмотра.")
            return False
        if (self.pending_operation or getattr(self, "_navigation_pending", False)
                or getattr(self.controller, "_pod_operations", None) or getattr(self.controller, "_write_operation", None) is not None):
            if hasattr(self.controller, "statusBar"):
                self.controller.statusBar().showMessage("Дождитесь завершения текущей операции или перехода.", 5000)
            return False
        if hasattr(self.controller, "store") and getattr(self.controller, "preferences", {}).get("read_only", True):
            error(self, "Включите режим администратора.")
            return False
        draft_owner = getattr(self.controller, "_replica_draft_owner", None)
        if draft_owner is not None and not (replica_step and draft_owner is self):
            self.controller.statusBar().showMessage("Сейчас доступны только стрелки реплик этой карточки.", 5000)
            return False
        if not self._matches_opened_target():
            error(self, "Ресурс изменился. Откройте его заново.")
            return False
        if not draft_action and self.yaml_editor.editor.document().isModified():
            answer = QMessageBox.warning(self, "Есть несохранённый YAML",
                "Сохранить YAML или отбросить правки?",
                QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Discard:
                return False
            self.yaml_editor.setPlainText(dump_resource(self.yaml_base_resource, allow_unicode=True, sort_keys=False))
            self.yaml_editor.editor.document().setModified(False)
        return True

    def _update_access_style(self, writable):
        if self.property("writeEnabled") == writable:
            return
        self.setProperty("writeEnabled", writable)
        self.tabs.setProperty("writeEnabled", writable)
        self.tabs.tabBar().setProperty("writeEnabled", writable)
        names = {"resourceHeader", "resourceContext", "resourceNavigation", "resourceRefresh", "deploymentNotice"}
        widgets = [self, self.tabs, self.tabs.tabBar()]
        for widget in self.findChildren(QWidget):
            if widget.objectName() in names:
                widgets.extend([widget, *widget.findChildren(QWidget)])
        for widget in widgets:
            widget.style().unpolish(widget)
            widget.style().polish(widget)
            widget.update()

    def _mutate(self, function, done, failed=None, *, terminal=False, action='yaml', parameters=None):
        if (self.pending_operation or getattr(self, "_navigation_pending", False)
                or getattr(self.controller, "_pod_operations", None) or getattr(self.controller, "_write_operation", None) is not None):
            return
        if hasattr(self.controller, "store") and getattr(self.controller, "preferences", {}).get("read_only", True):
            error(self, "Включите режим администратора.")
            return
        if not self._matches_opened_target():
            error(self, "Ресурс изменился. Откройте его заново.")
            return
        try:
            expected_credentials = credentials_revision(self.api.cluster(self.cluster_id))
            if expected_credentials != self._opened_credentials:
                raise PermissionError('Подключение изменилось после открытия карточки. Откройте ресурс заново.')
            root_guard = getattr(self.controller, 'write_guard', None)
            origin = root_guard.context() if root_guard is not None else None
        except Exception as exc:
            if hasattr(self.controller, 'set_read_only'):
                self.controller.set_read_only(True)
            error(self, str(exc))
            return
        lease = None
        if hasattr(self.controller, "begin_write_operation"):
            lease = self.controller.begin_write_operation()
            if lease is None:
                return
        origin = root_guard.context() if root_guard is not None else None
        confirmed_parameters = copy.deepcopy(parameters)
        confirmed_identity = tuple(self._opened_identity)
        confirmed_generation = root_guard.generation if root_guard is not None else None
        now = root_guard.clock() if root_guard is not None else time.monotonic()
        confirmed_deadline = now + (min(60., max(0., self.terminal_timer.remainingTime()/1000)) if terminal else 60.)
        self.pending_operation = True
        self.update_write_controls()
        completed = False
        permits = []
        def release(accepted=False):
            self.pending_operation = False
            if lease is not None:
                self.controller.finish_write_operation(lease, terminal=terminal, accepted=accepted)
            self.update_write_controls()
            if not accepted and getattr(self, '_yaml_reset_pending', False):
                self.reload_yaml(discard=True)
        def finish(value):
            nonlocal completed
            if completed:
                return
            completed = True
            controller = getattr(self.controller, 'monitoring', None)
            display_allowed = (controller is None or controller.policy.state == 'ACTIVE') and self._matches_opened_target()
            try:
                display_allowed &= credentials_revision(self.api.cluster(self.cluster_id)) == self._opened_credentials
            except Exception:
                display_allowed = False
            release(accepted=True)
            if display_allowed:
                done(value)
            elif hasattr(self.controller, 'statusBar'):
                self.controller.statusBar().showMessage('Изменение принято; обновление данных приостановлено.', 10000)
                if self.yaml_editor.editor.document().isModified():
                    self.reset_yaml_after_lock()
        def on_error(message):
            nonlocal completed
            if completed:
                return
            completed = True
            if terminal and hasattr(self.controller, 'set_read_only'):
                self.controller.set_read_only(True)
            release()
            if permits and permits[0].state == 'accepted':
                if action == 'scale':
                    self._cancel_scale_draft(lock=False)
                self._scaling_target = None
                if hasattr(self.controller, 'statusBar'):
                    self.controller.statusBar().showMessage('Изменение принято; обновление данных приостановлено. '+str(message), 10000)
                return
            if permits:
                outcome = {'commit-dispatched': 'Результат неизвестен; проверьте состояние перед повтором.',
                           'rejected': 'Изменение отклонено сервером.',
                           'canceled-before-dispatch': 'Отменено до отправки изменения.',
                           'issued': 'Отменено до отправки изменения.'}.get(permits[0].state, '')
                message = str(message)+'\n'+outcome
            (failed or (lambda value: error(self, value)))(message)
        def guarded_operation():
            if credentials_revision(self.api.cluster(self.cluster_id)) != expected_credentials:
                raise PermissionError('Credentials changed after confirmation.')
            if hasattr(self.controller, "store") and getattr(self.controller, "preferences", {}).get("read_only", True):
                raise PermissionError("Режим только чтения включён до начала операции.")
            if not self._matches_opened_target():
                raise RuntimeError("Ресурс изменился. Откройте его заново.")
            guard = getattr(self.controller, 'write_guard', None)
            if guard is None or not hasattr(self.controller, 'validate_admin_access'):
                raise PermissionError('An active application write guard is required.')
            if guard.context() != origin or credentials_revision(self.api.cluster(self.cluster_id)) != expected_credentials:
                raise PermissionError('Operation context changed after confirmation.')
            permit = guard.authorize(confirmed_identity, expected_credentials,
                                     'exec' if terminal else action, parameters=confirmed_parameters,
                                   origin=origin, generation=confirmed_generation,
                                   validator=lambda cid: self.controller.validate_operation_access(
                                       ActionTarget(cid, confirmed_identity[1], confirmed_identity[2], confirmed_identity[3],
                                                    'exec' if terminal else action,
                                                    action == 'delete' and confirmed_parameters.get('mode') == 'restart')),
                                     deadline=confirmed_deadline)
            permits.append(permit)
            with operation_permit(permit):
                return function()
        if self.background.submit(guarded_operation, finish, on_error, priority=True, monitor=False) is False:
            on_error("Не удалось поставить операцию в очередь.")

    def _yaml_changed(self):
        self._allow_discard = False
        if hasattr(self, "yaml_draft_state"):
            self.yaml_draft_state.setText("Не сохранено · Ctrl+S" if self.yaml_editor.editor.document().isModified() else "")
        if hasattr(self.controller, 'shell_actions'):
            self.controller.shell_actions.sync_admin_timer()

    def can_leave(self):
        if self.pending_operation:
            return False
        if self.yaml_editor.editor.document().isModified() and not self._allow_discard:
            answer = QMessageBox.warning(self, "Несохранённый YAML", "Отбросить изменения YAML?", QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Discard:
                return False
            self._allow_discard = True
            if hasattr(self.controller, 'set_read_only'):
                self.controller.set_read_only(True)
        return True

    def closeEvent(self, event):
        if not self.can_leave():
            event.ignore()
            return
        self._stop_panel()
        super().closeEvent(event)

    def _stop_panel(self):
        self._cancel_scale_draft()
        self.background.active = False
        for timer in self.findChildren(QTimer):
            timer.stop()

    def reject(self):
        if self.can_leave():
            self._stop_panel()
            super().reject()

    def refresh_object(self, *, notify_errors=True):
        from .resource_read_controller import ResourceReadController
        ResourceReadController(self).refresh_object(notify_errors=notify_errors)

    def _read_finalizer(self, flag, epoch):
        def finish(outcome):
            if self._resource_epoch == epoch:
                setattr(self, flag, False)
        return finish

    def _adopt_live(self, resource):
        changed = resource != getattr(self, 'live_resource', self.resource)
        self.live_resource = resource
        self.resource = resource
        if hasattr(self,'log_view'):
            self.log_view.update_resource(resource)
        self.details.set_summary(self._summary(resource))
        self._update_context_status(resource)
        self.yaml_notice.setToolTip("")
        self.yaml_notice_text.setText("YAML на сервере изменён.")
        self.yaml_notice.setVisible(resource != self.yaml_base_resource)
        self._highlight_yaml_reload(resource != self.yaml_base_resource)
        if changed:
            self._card_refreshed()
        return changed

    def _highlight_yaml_reload(self, changed):
        self.yaml_reload.setEnabled(bool(changed) and not self.pending_operation and not getattr(self, "_navigation_pending", False))
        if bool(self.yaml_reload.property("primary")) != bool(changed):
            self.yaml_reload.setProperty("primary", bool(changed))
            self.yaml_reload.style().unpolish(self.yaml_reload)
            self.yaml_reload.style().polish(self.yaml_reload)

    def _card_refreshed(self):
        if self.isVisible() and hasattr(self.controller, "statusBar"):
            self.controller.statusBar().showMessage(f"{self.name} · карточка обновлена {datetime.now():%H:%M:%S}", 5000)

    def reload_yaml(self, *, discard=False):
        from .yaml_controller import YamlReloadController
        if not hasattr(self, '_yaml_reload_controller'):
            self._yaml_reload_controller = YamlReloadController(self)
        self._yaml_reload_controller.reload(discard=discard)

    def reset_yaml_after_lock(self):
        from .yaml_controller import YamlReloadController
        if not hasattr(self, '_yaml_reload_controller'):
            self._yaml_reload_controller = YamlReloadController(self)
        self._yaml_reload_controller.discard()

    def _updated(self, resource, *, notify=True):
        self._resource_epoch += 1
        self.api.invalidate_resource(self.cluster_id, self.kind, self.namespace, self.name)
        self.details.set_summary(self._summary(resource))
        self._update_context_status(resource)
        self.resource = resource
        self.live_resource = resource
        self.yaml_base_resource = copy.deepcopy(resource)
        self.version = resource.get("metadata", {}).get("resourceVersion", "")
        self._yaml_generation += 1
        self._yaml_loading = False
        self._yaml_ready = True
        self._yaml_reset_pending = False
        self._accept_yaml_text(dump_resource(resource))
        self.yaml_notice.hide()
        self._highlight_yaml_reload(False)
        if notify:
            QMessageBox.information(self, "Quaber", "Изменение принято.")
        else:
            self._card_refreshed()

    def _update_context_status(self, resource):
        if self.kind == "pod":
            self.context_values["Статус"].setText(pod_status(resource))
        elif self.kind == "deployment":
            self.context_values["Pods"].setText(f"{resource.get('status', {}).get('replicas', resource.get('status', {}).get('availableReplicas', 0))}/{resource.get('spec', {}).get('replicas', 1)}")
            self._update_replica_target(resource)

    def _update_replica_target(self, resource):
        if not hasattr(self, "_displayed_pods"):
            return
        groups = deployment_rings(resource, self._displayed_pods, self._displayed_sets)
        self._rollout_in_progress = groups["rolling"]
        self.previous_ring.setVisible(groups["rolling"])
        self.rollout_arrow.setVisible(groups["rolling"])
        if groups["rolling"]:
            self.previous_ring.set_pods(groups["previous"], groups["previous_target"], label='Предыдущая версия')
        desired = self._scaling_target if self._scaling_target is not None else groups["current_target"]
        self.pod_donut.set_pods(groups["current"], desired, requesting=self._scaling_target is not None,
                                label='Новая версия' if groups['rolling'] else '')

    def scale_by(self, delta):
        if not self._ensure_write(replica_step=True):
            return
        if self._hpa_controllers:
            return
        current = self._scaling_target if self._scale_snapshot is not None else self.resource.get("spec", {}).get("replicas", 1)
        value = max(0, min(10000, current+delta))
        if value == current:
            return
        if self._scale_snapshot is None:
            self._scale_snapshot = copy.deepcopy(self.resource)
            self.controller._replica_draft_owner = self
        self._scaling_target = value
        self._update_scale_notice()
        self._update_replica_target(self.resource)
        self.controller.refresh_write_controls() if hasattr(self.controller, "refresh_write_controls") else self.update_write_controls()

    def _apply_scale(self, snapshot, value, *, revalidate=False):
        if not self._ensure_write():
            return
        if self._hpa_controllers:
            return
        current = snapshot.get("spec", {}).get("replicas", 1)
        if value == current:
            return
        cid, _, namespace, name, uid = self._opened_identity
        version = snapshot.get("metadata", {}).get("resourceVersion")
        self._scaling_target = value
        self._resource_epoch += 1
        self._update_replica_target(snapshot)
        def done(resource):
            self._cancel_scale_draft(lock=False)
            self._scaling_target = None
            self.scale_notice.hide()
            self._updated(resource, notify=False)
            self._rollout_cadence.request_fast()
            restart_poll_timer(self.deployment_timer, self._rollout_cadence.interval(True))
            self.reload_deployment_pods()
        def failed(message):
            self._scale_snapshot = copy.deepcopy(snapshot)
            self._scaling_target = value
            self.controller._replica_draft_owner = self
            self._update_scale_notice()
            restart_poll_timer(self.deployment_timer, self._rollout_cadence.interval(True))
            self.reload_deployment_pods()
            error(self, message)
        def execute():
            actual_version = version
            if revalidate:
                current_resource = self.api.resource(cid, "deployment", namespace, name)
                check_identity(current_resource, uid, version)
                if current_resource.get("spec", {}).get("replicas", 1) != current:
                    raise RuntimeError("Число реплик изменено на сервере. Повторите выбор по свежим данным.")
            return self.api.scale(cid, namespace, name, value, uid, actual_version, permit=current_permit())
        self._mutate(execute, done, failed, action='scale', parameters={'replicas': value, 'resourceVersion': version})

    def _update_scale_notice(self):
        if self._scale_snapshot is None:
            return
        before = self._scale_snapshot.get("spec", {}).get("replicas", 1)
        self.scale_apply.setText(f"Apply · Replicas: {before} → {self._scaling_target}")
        self.scale_notice.show()

    def _commit_scale_draft(self):
        if self._scale_snapshot is None:
            return
        snapshot, value = copy.deepcopy(self._scale_snapshot), self._scaling_target
        if value == 0 and not confirm(self, 'Scale to zero',
                'Остановить все экземпляры Deployment?', target=self.operation_label()):
            return
        if (value == snapshot.get("spec", {}).get("replicas", 1) or self._hpa_controllers
                or not self._matches_opened_target() or getattr(self, "_navigation_pending", False)):
            if hasattr(self.controller, "set_read_only"):
                self.controller.set_read_only(True)
            return
        # Transfer the local draft lease to the write operation without losing
        # the baseline/target. Terminal success is the point that clears it.
        if getattr(self.controller, '_replica_draft_owner', None) is self:
            self.controller._replica_draft_owner = None
        self._apply_scale(snapshot, value, revalidate=True)
        if not self.pending_operation and self._scale_snapshot is not None:
            self.controller._replica_draft_owner = self

    def _cancel_scale_draft(self, *, lock=True):
        self.scale_delay_timer.stop()
        self.scale_countdown_timer.stop()
        self._scale_snapshot = None
        if getattr(self.controller, "_replica_draft_owner", None) is self:
            self.controller._replica_draft_owner = None
            self._scaling_target = None
            self.scale_notice.hide()
            self._update_replica_target(self.resource)
            if lock and hasattr(self.controller, "set_read_only"):
                self.controller.set_read_only(True)

    def edit_resources(self):
        if self.tabs.tabText(self.tabs.currentIndex()) != "Метрики":
            return
        if not self._ensure_write():
            return
        snapshot = copy.deepcopy(self.resource)
        containers = snapshot.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        names = [container.get("name", "") for container in containers]
        if not names:
            error(self, "У Deployment нет контейнеров.")
            return
        graph = getattr(self, "deployment_graph", None)
        selected_container = graph.container.currentText() if graph else ""
        name = selected_container if selected_container in names else preferred_container(names) or names[0]
        source = next(container for container in containers if container.get("name") == name)
        original = source.get("resources", {})
        dialog = QDialog(self)
        dialog.setWindowTitle(f"CPU / RAM — {name}")
        form = QFormLayout(dialog)
        form.addRow(QLabel(self.operation_label()))
        container_selector = QComboBox()
        container_selector.addItems(names)
        container_selector.setCurrentText(name)
        if len(names) > 1:
            form.addRow("Контейнер", container_selector)
        fields = {}
        for field, category, resource in (("CPU request", "requests", "cpu"), ("CPU limit", "limits", "cpu"),
                                           ("RAM request", "requests", "memory"), ("RAM limit", "limits", "memory")):
            edit = QLineEdit(original.get(category, {}).get(resource, ""))
            edit.setToolTip({("requests", "cpu"): "CPU для планирования. 1000m = 1 ядро; например 100m.",
                ("limits", "cpu"): "Максимум CPU. При достижении выполнение замедляется; например 1 или 500m.",
                ("requests", "memory"): "Память для планирования; например 256Mi или 1Gi.",
                ("limits", "memory"): "Максимум памяти. При превышении контейнер может завершиться; например 512Mi."}[(category, resource)])
            form.addRow(field, edit)
            fields[(category, resource)] = edit
        def load_container(selected):
            values = next(c for c in containers if c.get("name") == selected).get("resources", {})
            for (category, resource), field in fields.items():
                field.setText(values.get(category, {}).get(resource, ""))
        container_selector.currentTextChanged.connect(load_container)
        remove_limits = QCheckBox("Снять пустые лимиты (явное удаление ограничений)")
        remove_limits.setToolTip("Удалить из конфигурации ограничения с пустыми полями. Без флажка пустое поле сохраняет прежнее значение.")
        form.addRow(remove_limits)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if run_dialog(dialog) != QDialog.DialogCode.Accepted:
            return
        name = container_selector.currentText()
        original = next(c for c in containers if c.get("name") == name).get("resources", {})
        edited = copy.deepcopy(snapshot)
        target = next(container for container in edited["spec"]["template"]["spec"]["containers"] if container.get("name") == name)
        try:
            resources = resource_changes(original, {key: edit.text() for key, edit in fields.items()}, remove_limits.isChecked())
            target["resources"] = resources
        except ValueError as exc:
            error(self, str(exc))
            return
        before = dump_resource(original, allow_unicode=True, sort_keys=False).splitlines(keepends=True)
        after = dump_resource(resources, allow_unicode=True, sort_keys=False).splitlines(keepends=True)
        diff = "".join(difflib.unified_diff(before, after, fromfile="До", tofile="После"))
        if not diff:
            return
        if not confirm(self, "Изменение ресурсов", f"Применить ресурсы контейнера {name}?\nИзменение запустит rollout; возможен простой.", target=self.operation_label(), details=diff):
            return
        cid, _, namespace, resource_name, _ = self._opened_identity
        self._mutate(lambda: self.api.replace(cid, "deployment", namespace, resource_name, edited, snapshot.get("metadata", {}).get("resourceVersion"), permit=current_permit()), self._updated, action='resources', parameters=edited)

    def restart(self):
        if not self._ensure_write():
            return
        snapshot = copy.deepcopy(self.resource)
        cid, _, namespace, name, uid = self._opened_identity
        if confirm(self, "Restart rollout", "Запустить новую ревизию Deployment?", target=self.operation_label()):
            def done(resource):
                self._updated(resource, notify=False)
                self._rollout_cadence.request_fast()
                restart_poll_timer(self.deployment_timer, self._rollout_cadence.interval(True))
                self.reload_deployment_pods()
            self._mutate(lambda: self.api.restart(cid, namespace, name, uid, snapshot.get("metadata", {}).get("resourceVersion"), permit=current_permit()), done, action='restart')

    def delete_pod(self):
        if not self._ensure_write():
            return
        from .resource_actions import confirm_pod_removal
        cid, _, namespace, name, uid = self._opened_identity
        approved = copy.deepcopy(self.resource)
        approved_version = approved.get('metadata',{}).get('resourceVersion')
        if not isinstance(approved_version,str) or not approved_version:
            error(self,'Версия Pod неизвестна. Обновите карточку перед удалением.')
            return
        mode = confirm_pod_removal(self, self.target_label(), approved)
        if mode:
            self._mutate(lambda: self.api.delete_pod(cid, namespace, name, uid, mode,
                         expected_version=approved_version,permit=current_permit()), lambda _: self.accept(),
                         action='delete', parameters={'mode': mode,'resourceVersion':approved_version})


class MainWindow(QMainWindow):
    authorization_needed = pyqtSignal(str)
    access_denied = pyqtSignal(str, str, str)
    matrix_scope_ready = pyqtSignal(object)

    def __init__(self, store: UserStore, app_dir: Path):
        super().__init__()
        self.store, self.app_dir = store, app_dir
        preferences = store.preferences()
        # Legacy per-cluster paths are read only for one-time migration.
        if "kubectl_path" not in preferences:
            previous = {c["kubectlPath"] for c in store.clusters() if c.get("kubectlPath")}
            if len(previous) == 1:
                preferences["kubectl_path"] = previous.pop()
                store.save_preferences(preferences)
        self.background = Background(self)
        from .tooltips import install_tooltips
        install_tooltips()
        from .loading_status import LoadingStatus
        self.loading_status = LoadingStatus(self.background, self)
        try:
            executable = locate_kubectl(app_dir, store.preferences().get("kubectl_path", ""))
        except FileNotFoundError:
            executable = None
        # executable above controls only the missing-client hint. The manager
        # resolves the current global setting when starting each client.
        self.proxy = ProxyManager(None, store)
        self.api = Kubernetes(store, self.proxy)
        from .cluster_workers import configure_cluster_threads
        self.api.authorization_required = self.authorization_needed.emit
        self.api.access_denied = self.access_denied.emit
        self.access_denied.connect(self._on_access_denied, Qt.ConnectionType.QueuedConnection)
        self.authorization_needed.connect(self._on_authorization_needed, Qt.ConnectionType.QueuedConnection)
        self._unauthorized_clusters = set()
        self._auth_dismissed = {}
        self._auth_busy = False
        self.preferences = store.preferences()
        configure_cluster_threads(self.api, self.preferences.get('cluster_io_threads', 1))
        self.api.set_transport_mode(self.preferences.get('api_transport', 'proxy'))
        self.preferences["read_only"] = True
        self.write_guard = WriteGuard(
            lambda: not self.preferences.get('read_only', True)
                    and not self._closing and self.monitoring.policy.state == 'ACTIVE'
                    and self._write_operation is not None,
            lambda cid: credentials_revision(self.api.cluster(cid)),
            context=lambda: (getattr(self, '_resource_request', 0),
                             getattr(getattr(self, '_resource_panel', None), '_resource_epoch', 0),
                             getattr(getattr(self, '_resource_panel', None), '_access_context_generation', 0),
                             getattr(self, '_write_operation', None)))
        self.metric_history = MetricHistory()
        self._write_operation = None
        self._replica_draft_owner = None
        self.preference_writer = PreferenceWriter(store, self)
        self.preference_writer.failed.connect(lambda message: self.statusBar().showMessage(message), Qt.ConnectionType.QueuedConnection)
        self.views = MatrixViews(self.preferences)
        self._displayed_view_id = self.views.active_id
        self.setWindowTitle("Quaber")
        screen = QApplication.primaryScreen()
        if screen:
            available = screen.availableGeometry()
            self.resize(min(1450, max(700, available.width() - 80)), min(900, max(550, available.height() - 80)))
        else:
            self.resize(1450, 900)
        self._closing = False
        self._page_history = []
        self._history_move = False
        self._pod_operations = set()
        self._matrix_generation = 0
        self.matrix_scope_ready.connect(self._matrix_scope_loaded, Qt.ConnectionType.QueuedConnection)
        self.matrix_paint_timer = QTimer(self)
        self.matrix_paint_timer.setSingleShot(True)
        self.matrix_paint_timer.setInterval(80)
        self.matrix_paint_timer.timeout.connect(self.render_matrix)
        self.recent_age_timer = QTimer(self)
        self.recent_age_timer.setSingleShot(True)
        self.recent_age_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.recent_age_timer.timeout.connect(self.render_matrix)
        self.matrix_return_timer = QTimer(self)
        self.matrix_return_timer.setSingleShot(True)
        self.matrix_return_timer.timeout.connect(self._refresh_after_return)
        self.deployment_mode = DeploymentMode(self, self.preferences.get("deploy_mode_until"))
        if not self.deployment_mode.active:
            self.preferences.pop("deploy_mode_until", None)
        from .resource_actions import ResourceActions
        self.resource_actions = ResourceActions(self)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.sidebar = QWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(240)
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(0, 6, 0, 6)
        side.setSpacing(2)
        self.sidebar_toggle = QPushButton("<<")
        self.sidebar_toggle.setObjectName("sidebarToggle")
        self.sidebar_toggle.setFixedSize(32, 24)
        self.sidebar_toggle.clicked.connect(lambda: self._set_sidebar_collapsed(not self.preferences.get("sidebar_collapsed", False)))
        toggle_row = QHBoxLayout()
        toggle_row.setContentsMargins(8, 0, 8, 0)
        toggle_row.addStretch()
        toggle_row.addWidget(self.sidebar_toggle)
        side.addLayout(toggle_row)
        brand_area = QWidget()
        self.brand_area = brand_area
        brand_area.setObjectName("brandArea")
        brand_row = QHBoxLayout(brand_area)
        self.brand_row = brand_row
        brand_row.setContentsMargins(18, 12, 14, 12)
        brand_row.setSpacing(12)
        brand = QLabel()
        self.brand_logo = brand
        brand.setObjectName("brand")
        brand.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        public = Path(__file__).resolve().parent.parent / "public"
        self._brand_source = QPixmap(str(public / "logo.png")).scaled(60, 60, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        brand.setPixmap(self._brand_source)
        brand.setToolTip("Quaber")
        brand.setAccessibleName("Quaber")
        self.setWindowIcon(application_icon())
        QApplication.instance().setWindowIcon(self.windowIcon())
        brand_row.addWidget(brand)
        brand_name = QLabel("Quaber")
        self.brand_name = brand_name
        brand_name.setObjectName("brandName")
        brand_row.addWidget(brand_name)
        brand_row.addStretch()
        side.addWidget(brand_area)
        install_button_translations(QApplication.instance())
        self.nav_group = QButtonGroup(self)
        self.nav_buttons = []
        for index, label in enumerate(("Нагрузки и Pod", "Квоты ресурсов", "ConfigMaps")):
            button = QPushButton(label)
            button.setToolTip(("Приложения по кластерам и namespace. Нажмите экземпляр Pod, чтобы открыть его.",
                "Использование и ограничения ресурсов namespace.",
                "ConfigMap: настройки приложений. Изменения могут повлиять на их работу.")[index])
            button.setObjectName("sideNav")
            button.setCheckable(True)
            button.setChecked(index == 0)
            button.clicked.connect(lambda checked=False, i=index: self.navigate_workspace(i))
            self.nav_group.addButton(button)
            self.nav_buttons.append(button)
            side.addWidget(button)
        side.addStretch(1)
        settings = QPushButton("Настройки")
        self.settings_button = settings
        settings.setToolTip("Подключения к кластерам, проверка доступа, тема и журнал изменений.")
        settings.setObjectName("sideNav")
        settings.clicked.connect(self.clusters_dialog)
        side.addWidget(settings)
        self.workspace = QWidget()
        self.workspace.setObjectName("workspace")
        self.workspace.installEventFilter(self)
        work_layout = QVBoxLayout(self.workspace)
        work_layout.setContentsMargins(0, 0, 0, 0)
        work_layout.setSpacing(0)
        if executable is None:
            self.missing_client = QLabel("kubectl не найден. Укажите общий путь в настройках приложения.")
            self.missing_client.setWordWrap(True)
            work_layout.addWidget(self.missing_client)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("workspacePages")
        work_layout.addWidget(self.tabs)
        self.matrix_page = QWidget()
        self.tabs.addTab(self.matrix_page, "Нагрузки и Pod")
        self._setup_matrix()
        self.quotas_page = self._resource_page("quota", "Квоты ресурсов")
        self.configmaps_page = self._resource_page("configmap", "ConfigMaps")
        self.tabs.tabBar().hide()
        self._active_workspace_index = 0
        self.tabs.currentChanged.connect(lambda index: self.nav_buttons[index].setChecked(True))
        self.tabs.currentChanged.connect(self._navigation_changed)
        self.shell_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.shell_splitter.setObjectName("shellSplitter")
        self.shell_splitter.setChildrenCollapsible(False)
        self.shell_splitter.setHandleWidth(1)
        self.shell_splitter.addWidget(self.sidebar)
        self.shell_splitter.addWidget(self.workspace)
        self.shell_splitter.setStretchFactor(0, 0)
        self.shell_splitter.setStretchFactor(1, 1)
        self.shell_splitter.setSizes([240, 1200])
        self.shell_splitter.handle(1).setEnabled(False)
        layout.addWidget(self.shell_splitter)
        self.nav_resize_timer = QTimer(self)
        self.nav_resize_timer.setSingleShot(True)
        self.nav_resize_timer.setInterval(250)
        self.nav_resize_timer.timeout.connect(self._remember_sidebar_width)
        self.shell_splitter.splitterMoved.connect(lambda *_: (self.nav_resize_timer.start(), self._position_resource_panel()))
        self._setup_log_panel()
        self._setup_inventory_pages()
        self.admin_access = AdminAccess(self)
        from .ux import ShellActions
        self.shell_actions = ShellActions(self)
        self.admin_access.update()
        self.statusBar().showMessage("Готово")
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        interval = self.preferences.get("refresh_ms", 10000)
        if interval:
            self.timer.start(interval)
        self._apply_theme()
        QTimer.singleShot(0, self._load_initial_matrix)
        QTimer.singleShot(1000, self._prompt_expired_login)
        self.auth_timer = QTimer(self)
        self.auth_timer.timeout.connect(self._prompt_expired_login)
        self.auth_timer.start(15000)
        self.polling_state = PollingState(self, self._resume_cluster_polling)
        self.monitoring = self.polling_state
        self.api.monitoring = self.monitoring
        self.background.monitoring = self.monitoring
        from .schedule_ui import Schedules
        self.schedules = Schedules(self)
        self.matrix.schedules = self.schedules
        self.schedules.changed.connect(lambda: self.matrix_paint_timer.start() if not self._closing else None)
        for timer in self.findChildren(QTimer):
            if timer is self.timer or timer is self.auth_timer or timer is self.admin_access.timer:
                mark_poll_timer(timer)

    def _resume_cluster_polling(self):
        from .request_policy import manual_read
        with manual_read():
            self._resume_demanded()

    def _resume_demanded(self):
        if self._closing or not polling_allowed(self):
            return
        if getattr(self, "_matrix_visual_dirty", False):
            self.render_matrix()
        panel = getattr(self, "_resource_panel", None)
        if panel:
            panel.refresh_object(notify_errors=False)
            current = panel.tabs.currentWidget()
            if current is panel.yaml_editor:
                panel._ensure_yaml_ready()
            if current is getattr(panel, "log_view", None):
                panel.log_view.load()
            for metrics in panel.findChildren(MetricsWidget):
                if metrics.isVisible():
                    metrics.load()
        else:
            page = self.tabs.currentWidget()
            if page in (self.dumps_page, self.versions_page, self.events_page):
                page.refresh_data(force=True)
            else:
                self.refresh()
                if not any(c.get('enabled') for c in self.store.clusters()):
                    self.monitoring.no_demand()
        self._prompt_expired_login()

    def set_read_only(self, readonly, *, preserve_drafts=False, discard_yaml=True):
        monitoring = getattr(self, 'monitoring', None)
        if not readonly and monitoring is not None and monitoring.policy.state != 'ACTIVE':
            return
        if not readonly and self._write_operation is not None:
            self.statusBar().showMessage("Дождитесь завершения операции.", 5000)
            return
        self.preferences["read_only"] = bool(readonly)
        if readonly:
            self.write_guard.invalidate()
            if discard_yaml:
                for panel in getattr(self, '_resource_stack', []):
                    if panel.yaml_editor.editor.document().isModified():
                        panel.reset_yaml_after_lock()
        if readonly and self._replica_draft_owner is not None and not preserve_drafts:
            self._replica_draft_owner._cancel_scale_draft(lock=False)
        self._save_preferences()
        self.shell_actions.update_mode()
        if readonly:
            for panel in getattr(self, "_resource_stack", []):
                if hasattr(panel, "console_lock"):
                    panel.lock_console()
        self.statusBar().showMessage("Изменения заблокированы." if readonly else "Разрешено одно изменение; Terminal — на одну минуту.", 5000)

    def refresh_write_controls(self):
        for panel in getattr(self, "_resource_stack", []):
            panel.update_write_controls()
        if hasattr(self, "admin_access"):
            self.admin_access.update()

    def validate_admin_access(self, cluster_id):
        try:
            if self.api.is_cluster_administrator(cluster_id) is True:
                return True
            message = "Режим администратора недоступен из-за ограничения прав Kubernetes."
        except Exception as exc:
            message = f"Не удалось подтвердить права администратора: {exc}"
        self.access_denied.emit(cluster_id, "CHECK", "administrator")
        raise PermissionError(message)

    def validate_operation_access(self, target):
        decision = review_action(self.api, target)
        if decision.outcome is AccessOutcome.ALLOWED:
            return True
        self.access_denied.emit(target.cluster_id, 'CHECK', target.action)
        raise PermissionError(decision.reason)

    def begin_write_operation(self):
        if self.preferences.get("read_only", True) or self._write_operation is not None or self._replica_draft_owner is not None:
            return None
        token = object()
        self._write_operation = token
        self.refresh_write_controls()
        return token

    def finish_write_operation(self, token, *, terminal=False, accepted=False):
        if self._write_operation is not token:
            return
        self._write_operation = None
        if not terminal:
            self.set_read_only(True, discard_yaml=not accepted)
        else:
            self.refresh_write_controls()

    def go_back(self):
        panel = getattr(self, "_resource_panel", None)
        if panel is not None:
            if not panel.close():
                self.statusBar().showMessage("Переход отменён: сохраните YAML или дождитесь операции.", 5000)
        elif self._page_history:
            index = self._page_history.pop()
            self._history_move = True
            try:
                self.tabs.setCurrentIndex(index)
            finally:
                self._history_move = False

    def _setup_log_panel(self):
        self.log_panel = LogPanel(self)
        button = QPushButton("Журнал")
        button.setObjectName("sideNav")
        button.setCheckable(True)
        self.nav_group.addButton(button)
        self.nav_buttons.append(button)
        self.tabs.addTab(self.log_panel, "Журнал")
        button.clicked.connect(lambda: self.navigate_workspace(self.tabs.indexOf(self.log_panel)))
        button.setToolTip("Диагностика самого приложения: обновления и ошибки · Ctrl+J")
        self.log_nav_button = button
        self.sidebar.layout().insertWidget(self.sidebar.layout().indexOf(self.settings_button), button)
        self.log_action = QAction("Журнал", self)
        self.log_action.setShortcut(QKeySequence("Ctrl+J"))
        self.log_action.triggered.connect(lambda: self.navigate_workspace(self.tabs.indexOf(self.log_panel)))
        self.addAction(self.log_action)
        self.log_session = LogSession(self.store.root, self, console=bool(getattr(QApplication.instance(), "quaber_console_logging", False)))
        self.log_session.bridge.message.connect(self.log_panel.append, Qt.ConnectionType.QueuedConnection)
        self.log_session.logger.info("Quaber запущен")
        if self.log_session.file_error:
            self.log_panel.append("ERROR  Не удалось открыть файл журнала; сообщения доступны в этой панели.")


    def _setup_inventory_pages(self):
        self.inventory = InventoryService(self)
        self.dumps_page = DumpsPage(self, self.inventory)
        self.versions_page = VersionsPage(self, self.inventory)
        from .events_page import EventsPage
        self.events_page = EventsPage(self)
        for title, page in (("Dumps", self.dumps_page), ("Сверка версий", self.versions_page), ("Events", self.events_page)):
            button = QPushButton(title)
            button.setObjectName("sideNav")
            button.setCheckable(True)
            self.nav_group.addButton(button)
            self.nav_buttons.append(button)
            self.tabs.addTab(page, title)
            button.clicked.connect(lambda checked=False, page=page: self.navigate_workspace(self.tabs.indexOf(page)))
            side = self.sidebar.layout()
            side.insertWidget(side.indexOf(self.log_nav_button)-1, button)

    def _remember_sidebar_width(self):
        self.preferences["sidebar_width"] = self.shell_splitter.sizes()[0]
        self._save_preferences()

    def _select_theme(self, value):
        self.preferences["theme"] = value
        self._save_preferences()
        self._apply_theme()
        self.render_matrix()
        logger.info("Цветовая тема: %s", value)

    def _close_resource_stack(self):
        stack = getattr(self, "_resource_stack", [])[:]
        if any(not panel.can_leave() for panel in reversed(stack)):
            for panel in stack:
                panel._allow_discard = False
            return False
        for panel in reversed(stack):
            panel.close()
        return True

    def navigate_workspace(self, index):
        if index == self.tabs.currentIndex():
            self._navigation_changed(index)
        else:
            self.tabs.setCurrentIndex(index)

    def _navigation_changed(self, index):
        had_card = bool(getattr(self, "_resource_stack", []))
        if not self._close_resource_stack():
            blocked = self.tabs.blockSignals(True)
            self.tabs.setCurrentIndex(self._active_workspace_index)
            self.tabs.blockSignals(blocked)
            self.nav_buttons[self._active_workspace_index].setChecked(True)
            return
        self._resource_request = getattr(self, "_resource_request", 0) + 1
        self._resource_opening_request = None
        self._cancel_workspace_reads()
        if getattr(self, '_resource_open_cancel', None) is not None:
            self._resource_open_cancel.set()
        if index != 0 and getattr(self, '_matrix_cancel', None) is not None:
            self._matrix_cancel.set()
            self._matrix_generation += 1
            self.loading_matrix = False
        if index != self._active_workspace_index and not self._history_move:
            self._page_history.append(self._active_workspace_index)
            self._page_history = self._page_history[-50:]
        if index != self._active_workspace_index or had_card:
            self.set_read_only(True)
        self._active_workspace_index = index
        if getattr(self, "_matrix_visual_dirty", False):
            self.render_matrix()
        logger.info("Раздел: %s", self.tabs.tabText(index))
        self.refresh()

    @pyqtSlot(str, str, str)
    def _on_access_denied(self, cluster_id, method, path):
        if self._closing:
            return
        self.set_read_only(True)
        self.admin_access.invalidate(cluster_id)
        message = ("Права администратора не подтверждены. Режим администратора выключен." if method == "CHECK" else
                   "Кластер запретил изменение: недостаточно прав или ограничение политики. Режим администратора выключен.")
        self.statusBar().showMessage(message, 15000)

    @pyqtSlot(str)
    def _on_authorization_needed(self, identifier):
        if self._closing:
            return
        self._unauthorized_clusters.add(identifier)
        if not self._auth_busy:
            QTimer.singleShot(0, self._prompt_expired_login)

    def _prompt_expired_login(self):
        if self._closing or self._auth_busy or not polling_allowed(self):
            return
        clusters = [c for c in self.store.clusters() if c["enabled"] and
                    (c["id"] in self._unauthorized_clusters or self.store.login_required(c)) and
                    self._auth_dismissed.get(c["id"]) != self.api.authorization.signature(c)]
        if not clusters:
            return
        self._auth_busy = True
        self.statusBar().showMessage("Восстановление авторизации…")
        def renew():
            unresolved = []
            for cluster in clusters:
                try:
                    self.api.authorization.renew(cluster["id"], cluster["encryptedKubeconfig"])
                except Exception:
                    unresolved.append(cluster["id"])
            return unresolved
        def done(unresolved):
            if not polling_allowed(self):
                self._auth_busy = False
                return
            for cluster in clusters:
                if cluster["id"] not in unresolved:
                    self._unauthorized_clusters.discard(cluster["id"])
            remaining = [c for c in self.store.clusters() if c["id"] in unresolved and c["enabled"]]
            imported = [c for c in remaining if c.get("authMode") == "kubeconfig"]
            if remaining:
                from .connection_presentation import password_groups
                password_connections = []
                for cluster in remaining:
                    if cluster.get("authMode") == "kubeconfig":
                        self._auth_dismissed[cluster["id"]] = self.api.authorization.signature(cluster)
                        self.statusBar().showMessage(f"{cluster['name']}: обновите kubeconfig в настройках.", 15000)
                        continue
                    password_connections.append(cluster)
                groups = password_groups(password_connections)
                dialog = LoginGroupsDialog(self, groups) if groups else None
                accepted = run_dialog(dialog) == QDialog.DialogCode.Accepted if dialog else False
                succeeded = set(getattr(dialog, 'succeeded_cluster_ids', ())) if dialog else set()
                for cluster in remaining:
                    if cluster.get("authMode") == "kubeconfig":
                        continue
                    if accepted or cluster['id'] in succeeded:
                        self._unauthorized_clusters.discard(cluster["id"])
                        self._auth_dismissed.pop(cluster["id"], None)
                    else:
                        self._auth_dismissed[cluster["id"]] = self.api.authorization.signature(cluster)
            self._auth_busy = False
            self.refresh()
            self.statusBar().showMessage("Обновите kubeconfig в настройках: " + ", ".join(c['name'] for c in imported) if imported else "Готово", 15000)
        def failed(message):
            self._auth_busy = False
            self.statusBar().showMessage(message)
        self.background.submit(renew, done, failed, priority=True)

    @staticmethod
    def _scroll_controls(widget):
        from .controls_strip import ControlsStrip
        return ControlsStrip(widget)

    def _setup_matrix(self):
        self.matrix_page.setObjectName("matrixPage")
        layout = QVBoxLayout(self.matrix_page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        view_strip = QWidget()
        view_strip.setObjectName("viewStrip")
        view_strip.setFixedHeight(30)
        view_row = QHBoxLayout(view_strip)
        view_row.setContentsMargins(16, 0, 16, 0)
        view_row.setSpacing(3)
        self.view_bar = ViewTabs()
        self.view_bar.setObjectName("matrixViewTabs")
        self.view_bar.setDrawBase(False)
        self.view_bar.setMovable(True)
        self.view_bar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view_bar.customContextMenuRequested.connect(self._view_menu)
        self.view_bar.setExpanding(False)
        self.view_bar.setElideMode(Qt.TextElideMode.ElideNone)
        self.view_bar.setUsesScrollButtons(True)
        self.view_bar.setDocumentMode(True)
        for view in self.views.views:
            self.view_bar.addTab(view["name"])
        self.view_bar.setCurrentIndex(next(i for i, view in enumerate(self.views.views) if view["id"] == self.views.active_id))
        self.view_bar.currentChanged.connect(self._switch_view)
        self.view_bar.tabMoved.connect(self._move_view)
        view_row.addWidget(self.view_bar, 0)
        self.view_bar.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        add = QPushButton("+")
        add.setObjectName("addView")
        add.setFixedWidth(28)
        add.setToolTip("Добавить представление")
        add.setAccessibleName("Добавить представление")
        add.clicked.connect(self._add_view)
        view_row.addWidget(add)
        view_row.addStretch()
        for label, handler in (("Экспорт видов…", self._export_views), ("Импорт видов…", self._import_views)):
            button = QPushButton(label)
            button.setToolTip("Перенос фильтров и расположения матрицы. Подключения и пароли в файл не входят.")
            button.setObjectName("viewAction")
            button.clicked.connect(handler)
            view_row.addWidget(button)
        layout.addWidget(view_strip)
        status_strip = QWidget()
        status_strip.setObjectName("statusStrip")
        controls = QHBoxLayout(status_strip)
        controls.setContentsMargins(16, 6, 16, 6)
        controls.setSpacing(8)
        self.status_filters = {}
        self.recent_label = QLabel('● Recently started 0')
        self.recent_label.setObjectName('recentLegend')
        self.recent_label.setStyleSheet('color: #5BA8E5;')
        self.recent_label.setToolTip('Running Pods created or restarted in the last 30 minutes.')
        self.deploy_mode = FullCheckBox("Режим деплоя")
        self.deploy_mode.setObjectName("deployMode")
        self.deploy_mode.setToolTip("На 30 минут убрать голубую подсветку работающих Pod. Pod остаются в таблице; затем подсветка возвращается автоматически.")
        self.deploy_mode.setChecked(self.deployment_mode.active)
        self.deploy_mode.toggled.connect(self.deployment_mode.set_active)
        self.deployment_mode.changed.connect(self._deployment_mode_changed)
        self.deployment_mode.countdown.connect(self._deployment_mode_countdown)
        self._deployment_mode_countdown(self.deployment_mode.remaining_seconds)
        for status in ("Running", "Pending", "Failed", "Unknown"):
            checkbox = FullCheckBox("Error" if status == "Failed" else status)
            checkbox.setObjectName("statusFilter")
            checkbox.setProperty("status", status)
            checkbox.setToolTip({"Running": "Pod работает и готов принимать трафик.",
                "Pending": "Pod ожидает запуска или ещё не готов принимать трафик.",
                "Failed": "Pod или контейнер завершился с ошибкой, не запускается либо перезапускается.",
                "Unknown": "Состояние Pod неизвестно. Откройте события для диагностики."}[status])
            checkbox.setChecked(self.preferences.get("filters", {}).get(status, True))
            checkbox.stateChanged.connect(self.render_matrix)
            controls.addWidget(checkbox)
            self.status_filters[status] = checkbox
            if status == "Running":
                controls.addWidget(self.recent_label)
        self.summary_label = QLabel("0 Total Pods")
        self.summary_label.setObjectName("totalCounter")
        controls.addWidget(self.summary_label)
        self.extra_counters = QLabel()
        controls.addWidget(self.extra_counters)
        controls.addStretch(1)
        layout.addWidget(self._scroll_controls(status_strip))
        self.workload_counters = QLabel()
        self.workload_counters.setObjectName("workloadCounter")
        self.workload_counters.setTextFormat(Qt.TextFormat.RichText)
        self.workload_counters.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.workload_counters.hide()
        controls.insertWidget(controls.indexOf(self.summary_label) + 1, self.workload_counters)
        refresh_strip = QWidget()
        refresh_strip.setObjectName("refreshStrip")
        refresh_row = QHBoxLayout(refresh_strip)
        refresh_row.setContentsMargins(12, 3, 12, 3)
        refresh_row.addWidget(self.deploy_mode)
        refresh_row.addStretch(1)
        self.last_updated = QLabel("Последнее обновление: —")
        self.last_updated.setObjectName("lastUpdated")
        refresh_row.addWidget(self.last_updated)
        refresh = QPushButton("Обновить")
        self.matrix_refresh_button = refresh
        refresh.setToolTip("Получить свежие состояния из кластеров · F5")
        refresh.setProperty("primary", True)
        refresh.clicked.connect(self._manual_refresh)
        refresh_row.addWidget(self.loading_status)
        refresh_row.addWidget(refresh)
        refresh_row.addWidget(QLabel("Автообновление"))
        self.refresh_interval = QComboBox()
        self.refresh_interval.setToolTip("Как часто обновлять текущий раздел. Это не влияет на отдельный интервал логов Pod.")
        for label, ms in (("Выкл", 0), ("5 сек", 5000), ("10 сек", 10000), ("30 сек", 30000), ("1 мин", 60000), ("5 мин", 300000)):
            self.refresh_interval.addItem(label, ms)
        self.refresh_interval.setCurrentIndex(max(0, self.refresh_interval.findData(self.preferences.get("refresh_ms", 10000))))
        self.refresh_interval.currentIndexChanged.connect(self._set_refresh_interval)
        refresh_row.addWidget(self.refresh_interval)

        toolbar = QWidget()
        toolbar.setObjectName("filterStrip")
        toolbar_row = QHBoxLayout(toolbar)
        toolbar_row.setContentsMargins(16, 6, 16, 6)
        self.scope_buttons = {}
        for title, key in (("Кластер", "selectedClusterIds"), ("Namespace", "selectedNamespaces"), ("Deployment", "selectedDeployments")):
            scope = QPushButton(title + ": Все ▾")
            scope.setToolTip("Выберите, какие объекты показывать в текущем виде. Ресурсы кластера не изменяются.")
            scope.setObjectName("scopeFilter")
            scope.clicked.connect(lambda checked=False, title=title, key=key: self._scope_menu(title, key))
            toolbar_row.addWidget(scope)
            self.scope_buttons[key] = (title, scope)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Имя Deployment…")
        self.search.setToolTip("Поиск по имени нагрузки · Ctrl+F")
        self.search.setMinimumWidth(160)
        self.search.setClearButtonEnabled(True)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(200)
        self.search_timer.timeout.connect(self.render_matrix)
        self.search.textChanged.connect(lambda: self.search_timer.start())
        toolbar_row.addWidget(self.search)
        toolbar_row.addStretch(1)
        layout.addWidget(self._scroll_controls(toolbar))
        self.deployment_width = QSpinBox(self)
        self.deployment_width.setRange(100, 500)
        self.deployment_width.setValue(self.preferences.get("deployment_width", 150))
        self.deployment_width.valueChanged.connect(self._set_matrix_dimensions)
        self.deployment_width.hide()
        self.header_height = QSpinBox(self)
        self.header_height.setRange(54, 360)
        self.header_height.setValue(self.preferences.get("header_height", 160))
        self.header_height.valueChanged.connect(self._set_matrix_dimensions)
        self.header_height.hide()
        self.matrix = PodMatrix()
        self.matrix.setProperty("deployMode", self.deployment_mode.active)
        self.matrix.setObjectName("podMatrix")
        self.matrix.viewport().setObjectName("matrixCanvas")
        self.matrix.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)

        self.matrix.setHorizontalHeader(NamespaceHeader(Qt.Orientation.Horizontal, self.matrix))
        self.matrix.setItemDelegate(PodDelegate(self.matrix))
        self.matrix.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.matrix.customContextMenuRequested.connect(self._pod_menu)
        self.matrix.activatedAt.connect(self._matrix_click)
        self.matrix.horizontalHeader().setSectionsMovable(True)
        self.matrix.verticalHeader().setSectionsMovable(True)
        self.matrix.horizontalHeader().sectionMoved.connect(self._column_moved)
        self.matrix.verticalHeader().sectionMoved.connect(self._row_moved)
        self.matrix.horizontalHeader().sectionResized.connect(self._column_resized)
        self.matrix.horizontalHeader().sectionResized.connect(self.matrix.sync_frozen)
        self.matrix.headerHeightChanged.connect(self.header_height.setValue)
        layout.addWidget(self.matrix, 1)
        layout.addWidget(refresh_strip)
        self.matrix_data: dict[tuple[int, int], tuple] = {}
        self.snapshot: dict = {}
        self._row_names = []
        self._columns = []
        self._load_view_controls()

    def _deployment_mode_changed(self, active):
        self.deploy_mode.blockSignals(True)
        self.deploy_mode.setChecked(active)
        self.deploy_mode.blockSignals(False)
        if active:
            self.preferences["deploy_mode_until"] = self.deployment_mode.expires_at
        else:
            self.preferences.pop("deploy_mode_until", None)
        self._save_preferences()
        self.render_matrix()

    def _deployment_mode_countdown(self, remaining):
        self.deploy_mode.setText(f"Режим деплоя · {remaining // 60:02}:{remaining % 60:02}" if remaining else "Режим деплоя")

    def _save_view_controls(self):
        if getattr(self, "_loading_view", False):
            return
        view = self.views.active
        view["filters"] = {key: box.isChecked() for key, box in self.status_filters.items()}
        view.pop('recentRestartFilter', None)
        view["problemOnly"] = False
        self.views.persist()
        self._save_preferences()


    def _load_view_controls(self):
        view = self.views.active
        self._loading_view = True
        for status, box in self.status_filters.items():
            box.setChecked(view.get("filters", {}).get(status, True))
        self._loading_view = False
        color = view.get("color", "transparent")
        self._apply_view_color(color)
        self.render_matrix()
        if (hasattr(self, '_matrix_requested_scope')
                and self._matrix_requested_scope != self._view_network_scope()):
            self._matrix_scope_changed()

    def _view_network_scope(self):
        clusters = self.views.active.get('selectedClusterIds')
        return (None if clusters is None else tuple(sorted(clusters)),
                self.views.active.get('selectedNamespaces') == [])

    def _matrix_scope_changed(self):
        canceled = getattr(self, '_matrix_cancel', None)
        if canceled is not None: canceled.set()
        self._matrix_generation += 1
        self.loading_matrix = False
        self.render_matrix()
        self.matrix_return_timer.start(0)

    def _apply_view_color(self, color):
        light = self.preferences.get("theme") == "light"
        colors = theme_colors(light)
        background = blended_color(colors["editor"], color)
        style = (
            f"QWidget#matrixPage, QWidget#statusStrip, QWidget#filterStrip, QWidget#filterStrip QPushButton, QWidget#statusStrip QCheckBox, QTableWidget#podMatrix, "
            f"QWidget#matrixCanvas, QTableWidget#podMatrix QHeaderView, "
            f"QTableWidget#podMatrix QHeaderView QWidget {{background:{background};}}"
            f"QTableWidget#podMatrix QHeaderView::section, QTableCornerButton::section "
            f"{{background:{background};border:1px solid {colors['border']};}}"
            f"QWidget#statusStrip QCheckBox:hover, QWidget#filterStrip QPushButton:hover {{background:{blended_color(background, colors['accent'], .15)};}}")
        if style != self.matrix_page.styleSheet():
            self.matrix_page.setStyleSheet(style)
        self.matrix.setProperty("viewColor", color)
        frozen_style = f"QTableView#frozenDeployment {{background:{background};}}"
        if frozen_style != self.matrix.frozen.styleSheet():
            self.matrix.frozen.setStyleSheet(frozen_style)
        palette = self.matrix.palette()
        from PyQt5.QtGui import QPalette
        if palette.color(QPalette.ColorRole.Base) != QColor(background):
            palette.setColor(QPalette.ColorRole.Base, QColor(background))
            self.matrix.setPalette(palette)
        tab_colors = [view.get("color", "transparent") for view in self.views.views]
        if self.view_bar.colors != tab_colors or self.view_bar.light != light:
            self.view_bar.colors = tab_colors
            self.view_bar.light = light
            self.view_bar.update()

    def _set_view_color(self, color):
        self.views.active["color"] = color
        self.views.persist()
        self._save_preferences()
        self._apply_view_color(color)

    def _switch_view(self, index):
        if 0 <= index < len(self.views.views):
            if self._displayed_view_id != self.views.views[index]["id"]:
                self.set_read_only(True)
            self.views.active_id = self.views.views[index]["id"]
            self._displayed_view_id = self.views.active_id
            self.views.persist()
            self._save_preferences()
            self._load_view_controls()

    def _view_menu(self, point):
        index = self.view_bar.tabAt(point)
        if index < 0:
            return
        self.view_bar.setCurrentIndex(index)
        menu = QMenu(self)
        menu.addAction("Переименовать", self._rename_view)
        menu.addAction("Экспортировать все вкладки", self._export_views)
        colors = menu.addMenu("Цвет вкладки и фона")
        for color, name in VIEW_COLORS:
            action = colors.addAction(color_icon(color, theme_colors(self.preferences.get("theme") == "light")["editor"]), name)
            action.setCheckable(True)
            action.setChecked(self.views.active.get("color", "transparent") == color)
            action.triggered.connect(lambda checked=False, color=color: self._set_view_color(color))
        action = menu.addAction("Удалить", self._remove_view)
        action.setEnabled(len(self.views.views) > 1)
        menu.exec_(self.view_bar.mapToGlobal(point))

    def _move_view(self, source, target):
        view = self.views.views.pop(source)
        self.views.views.insert(target, view)
        self.views.persist()
        self._save_preferences()
        self._apply_view_color(self.views.active.get("color", "transparent"))

    def _add_view(self):
        name, accepted = QInputDialog.getText(self, "Новая вкладка", "Название")
        if accepted:
            try:
                view = self.views.add(name)
                index = self.view_bar.addTab(view["name"])
                self.view_bar.setCurrentIndex(index)
                self._save_preferences()
            except ValueError as exc:
                error(self, str(exc))

    def _rename_view(self):
        index = self.view_bar.currentIndex()
        if index < 0:
            return
        view = self.views.views[index]
        name, accepted = QInputDialog.getText(self, "Переименовать вкладку", "Название", text=view["name"])
        if accepted:
            try:
                self.views.rename(view["id"], name)
                self.view_bar.setTabText(index, name.strip())
                self._save_preferences()
            except ValueError as exc:
                error(self, str(exc))

    def _remove_view(self):
        index = self.view_bar.currentIndex()
        if index >= 0 and confirm(self, "Удаление вкладки", f"Удалить вкладку {self.views.views[index]['name']}?"):
            try:
                self.views.remove(self.views.views[index]["id"])
                self.view_bar.removeTab(index)
                self.view_bar.setCurrentIndex(0)
                self._save_preferences()
            except ValueError as exc:
                error(self, str(exc))

    def _export_views(self):
        path, _ = QFileDialog.getSaveFileName(self, "Экспорт вкладок", "quaber-views.json", "JSON (*.json)")
        if path:
            try:
                Path(path).write_text(json.dumps(self.views.export(self.store.clusters()), ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError as exc:
                error(self, str(exc))

    def _import_views(self):
        path, _ = QFileDialog.getOpenFileName(self, "Импорт вкладок", "", "JSON (*.json)")
        if path:
            try:
                if Path(path).stat().st_size > 1_000_000:
                    raise ValueError("Файл вкладок превышает 1 МБ.")
                count = self.views.import_data(json.loads(Path(path).read_text(encoding="utf-8")), self.store.clusters())
                for view in self.views.views[-count:]:
                    self.view_bar.addTab(view["name"])
                self.view_bar.setCurrentIndex(len(self.views.views) - count)
                self._save_preferences()
            except (OSError, ValueError, KeyError, TypeError) as exc:
                error(self, str(exc))

    def _scope_menu(self, title, key):
        from PyQt5.QtGui import QCursor
        if key == "selectedDeployments":
            values = sorted({name for c in self.snapshot.values() for r in c["namespaces"].values() for name in index_scope(r)[0]})
            choices = [(v, v) for v in values]
        elif key == "selectedNamespaces":
            choices = [(v, v) for v in sorted({ns for c in self.snapshot.values() for ns in c['namespaces']})]
        else:
            choices = [(c['id'], c['name']) for c in self.store.clusters() if c.get('enabled')]
        menu = MultiSelectMenu(self)
        def apply(value):
            self.views.active[key] = value
            self.views.persist()
            self._save_preferences()
            self.render_matrix()
            if key in ('selectedClusterIds', 'selectedNamespaces') and self._view_network_scope() != getattr(self, '_matrix_requested_scope', None):
                self._matrix_scope_changed()
        menu.addAction("Выбрать все", lambda: apply(None))
        menu.addAction("Снять все", lambda: apply([]))
        menu.addSeparator()
        selected = self.views.active.get(key)
        for value, label in choices:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(selected is None or value in selected)
            def toggle(checked, value=value):
                current = self.views.active.get(key)
                current = [v for v, _ in choices] if current is None else list(current)
                apply(list(dict.fromkeys(current + [value])) if checked else [v for v in current if v != value])
            action.toggled.connect(toggle)
        menu.exec_(QCursor.pos())


    def _column_moved(self, logical, old_visual, new_visual):
        if getattr(self, "_rendering_matrix", False) or not self._columns:
            return
        header = self.matrix.horizontalHeader()
        self.views.active["namespaceOrder"] = [f"{self._columns[index - 1][0]}/{self._columns[index - 1][2]}"
                                               for visual in range(1, header.count())
                                               if (index := header.logicalIndex(visual)) > 0]
        self.views.persist()
        self._save_preferences()

    def _row_moved(self, logical, old_visual, new_visual):
        if getattr(self, "_rendering_matrix", False) or not self._row_names:
            return
        header = self.matrix.verticalHeader()
        self.views.active["deploymentOrder"] = [self._row_names[header.logicalIndex(visual)] for visual in range(header.count())]
        self.views.persist()
        self._save_preferences()

    def _column_resized(self, logical, old_size, new_size):
        if getattr(self, "_rendering_matrix", False) or logical != 0:
            return
        self.preferences["deployment_width"] = new_size
        self.deployment_width.blockSignals(True)
        self.deployment_width.setValue(new_size)
        self.deployment_width.blockSignals(False)
        self._save_preferences()

    def _set_matrix_dimensions(self):
        if not hasattr(self, "matrix"):
            return
        self.preferences["deployment_width"] = self.deployment_width.value()
        self.preferences["header_height"] = self.header_height.value()
        self.matrix.setColumnWidth(0, self.deployment_width.value())
        self.matrix.horizontalHeader().setFixedHeight(self.header_height.value())
        self._save_preferences()

    def _resource_page(self, kind: str, title: str) -> QWidget:
        if kind == "quota":
            from .inventory_pages import ClusterPicker
            page = QWidget()
            layout = QVBoxLayout(page)
            heading = QHBoxLayout()
            section_title = QLabel("Квоты ресурсов · использовано / лимит")
            section_title.setObjectName("sectionTitle")
            heading.addWidget(section_title)
            heading.addStretch()
            self.quota_clusters = ClusterPicker(lambda: [c for c in self.store.clusters() if c["enabled"]])
            self.quota_clusters.changed.connect(self._quota_filter_changed)
            heading.addWidget(self.quota_clusters)
            from .loading_status import LoadingStatus
            self.quota_loading_status = LoadingStatus(self.background, self)
            heading.addWidget(self.quota_loading_status)
            self.quota_refresh_button = QPushButton('Обновить')
            self.quota_refresh_button.setToolTip('Получить актуальные квоты ресурсов · F5')
            self.quota_refresh_button.clicked.connect(self._manual_refresh)
            heading.addWidget(self.quota_refresh_button)
            layout.addLayout(heading)
            self.quota_status = QLabel('Выберите кластеры для чтения квот.')
            self.quota_status.setWordWrap(True)
            self.quota_status.setTextFormat(Qt.PlainText)
            layout.addWidget(self.quota_status)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            self.quota_content = QWidget()
            self.quota_layout = QVBoxLayout(self.quota_content)
            self.quota_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
            scroll.setWidget(self.quota_content)
            layout.addWidget(scroll, 1)
            self.tabs.addTab(page, title)
            return page
        page = QWidget()
        self.tabs.addTab(page, title)
        layout = QVBoxLayout(page)
        controls = QHBoxLayout()
        cluster_box, namespace_box = QComboBox(), QComboBox()
        cluster_box.setMaximumWidth(280)
        namespace_box.setMaximumWidth(280)
        cluster_box.setToolTip("Кластер: окружение, в котором искать объекты.")
        namespace_box.setToolTip("Namespace: область ресурсов внутри кластера.")
        controls.addWidget(QLabel("Кластер"))
        controls.addWidget(cluster_box)
        controls.addWidget(QLabel("Namespace"))
        controls.addWidget(namespace_box)
        button = QPushButton("Обновить")
        button.setToolTip("Получить актуальный список в выбранном namespace · F5")
        from .loading_status import LoadingStatus
        controls.addWidget(LoadingStatus(self.background, self))
        controls.addWidget(button)
        controls.addStretch(1)
        layout.addLayout(controls)
        table = ResourceList()
        table.setColumnCount(3)
        table.setHorizontalHeaderLabels(["Кластер", "Namespace", "Имя"])
        search_controls = QHBoxLayout()
        page.search_field = QLineEdit()
        page.search_field.setPlaceholderText("Найти ConfigMap…")
        page.search_field.setClearButtonEnabled(True)
        page.search_field.setToolTip("Поиск по текущему списку · Ctrl+F")
        page.search_field.textChanged.connect(table.set_filter)
        search_controls.addWidget(page.search_field, 1)
        self.resource_actions.bind_table(table, lambda entry: entry.data(2, Qt.ItemDataRole.UserRole), search_controls)
        layout.addLayout(search_controls)
        table.state_label = QLabel("Выберите кластер и namespace.")
        table.state_label.setTextFormat(Qt.TextFormat.PlainText)
        table.state_label.setWordWrap(True)
        layout.addWidget(table.state_label)
        layout.addWidget(table)
        setattr(self, f"{kind}_controls", (cluster_box, namespace_box, table))
        cluster_box.currentIndexChanged.connect(lambda: self._load_namespaces_for(kind))
        namespace_box.currentIndexChanged.connect(lambda: self.load_resource_page(kind))
        button.clicked.connect(lambda: self.load_resource_page(kind))
        table.cellDoubleClicked.connect(lambda row, col: self.open_resource_row(kind, row))
        return page

    def _clear_quota_cards(self):
        self._quota_render_revision = None
        while self.quota_layout.count():
            child = self.quota_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

    def _quota_filter_changed(self):
        self._quota_generation = getattr(self, "_quota_generation", 0) + 1
        self.quota_clusters.update_text()
        self._clear_quota_cards()
        self.load_quotas()

    def load_quotas(self):
        from .quota_controller import QuotaController
        if not hasattr(self, '_quota_controller'):
            self._quota_controller = QuotaController(self)
        self._quota_controller.load()

    def _workspace_read_active(self, kind):
        page = getattr(self, 'quotas_page' if kind == 'quota' else 'configmaps_page', None)
        return (not self._closing and self.tabs.currentWidget() is page
                and getattr(self, '_resource_panel', None) is None
                and getattr(self, '_resource_opening_request', None) is None)

    def _cancel_workspace_reads(self):
        previous = getattr(self, '_workspace_cancel', None)
        if previous is not None:
            previous.set()
        self._workspace_cancel = threading.Event()
        self._configmap_generation = getattr(self, '_configmap_generation', 0) + 1
        self._quota_generation = getattr(self, '_quota_generation', 0) + 1
        if hasattr(self, '_quota_controller'):
            self._quota_controller.operation = None
            self._loading_quotas = False

    def _workspace_read(self, function):
        from .monitoring import cancellable_read
        if not hasattr(self, '_workspace_cancel'):
            self._workspace_cancel = threading.Event()
        return cancellable_read(function, self._workspace_cancel, self.monitoring.token())

    def _invalidate_resource_list(self, kind, clear=True):
        _, _, table = getattr(self, f"{kind}_controls")
        if clear:
            table.setRowCount(0)
            table.setEnabled(False)
        table.state_label.setText("Загрузка…")
        table.set_empty_text("Загрузка…")
        token = getattr(self, f"_{kind}_generation", 0)+1
        setattr(self, f"_{kind}_generation", token)
        return token

    def _load_namespaces_for(self, kind):
        if not self._workspace_read_active(kind):
            return
        cluster_box, namespace_box, _ = getattr(self, f"{kind}_controls")
        token = self._invalidate_resource_list(kind)
        cluster_id = cluster_box.currentData()
        previous = namespace_box.currentText()
        blocker = QSignalBlocker(namespace_box)
        namespace_box.clear()
        del blocker
        if not cluster_id:
            return
        def done(values):
            if self._workspace_read_active(kind) and getattr(self, f"_{kind}_generation") == token and cluster_box.currentData() == cluster_id:
                blocker = QSignalBlocker(namespace_box)
                namespace_box.addItems(values)
                if previous in values:
                    namespace_box.setCurrentText(previous)
                del blocker
                self.load_resource_page(kind)
        self.background.submit(self._workspace_read(lambda: self.api.namespaces(cluster_id)), done,
                               lambda message: self.statusBar().showMessage(message), key=("namespaces", kind))

    def load_resource_page(self, kind):
        if not self._workspace_read_active(kind):
            return
        cluster_box, namespace_box, table = getattr(self, f"{kind}_controls")
        cluster_id, namespace = cluster_box.currentData(), namespace_box.currentText()
        token = self._invalidate_resource_list(kind, getattr(table, "resource_scope", None) != (cluster_id, namespace))
        if not cluster_id or not namespace:
            table.state_label.setText("Выберите кластер и namespace.")
            return
        scope = (cluster_id, namespace, cluster_box.currentText())
        table.resource_scope = (cluster_id, namespace)
        def done(values):
            if self._workspace_read_active(kind) and getattr(self, f"_{kind}_generation") == token:
                self._render_resources(kind, values, scope)
        def failed(message):
            if self._workspace_read_active(kind) and getattr(self, f"_{kind}_generation") == token:
                table.setEnabled(True)
                suffix = " Показан последний загруженный список." if table.rowCount() else ""
                table.state_label.setText(f"Не удалось обновить: {message}.{suffix} Нажмите «Обновить» для повтора.")
        self.background.submit(self._workspace_read(lambda: self.api.list_resources(cluster_id, kind, namespace)), done,
                               failed, key=("resources", kind))

    @batch_table(lambda self, kind, *args, **kwargs: getattr(self, f'{kind}_controls')[2], identity_column=2)
    def _render_resources(self, kind, values, scope=None):
        cluster_box, namespace_box, table = getattr(self, f"{kind}_controls")
        cluster_id, namespace, label = scope or (cluster_box.currentData(), namespace_box.currentText(), cluster_box.currentText())
        selected = table.item(table.currentRow(), 2)
        selected_identity = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        scroll_position = table.verticalScrollBar().value()
        table.setRowCount(len(values))
        table.set_empty_text("ConfigMap не найдены.")
        for row, value in enumerate(values):
            metadata = value.get("metadata", {})
            for col, text in enumerate((label, namespace, metadata.get("name", ""))):
                cell = item(text)
                if col == 2:
                    cell.setData(Qt.ItemDataRole.UserRole, (cluster_id, kind, namespace, metadata.get("name", ""), metadata.get("uid")))
                table.setItem(row, col, cell)
            if table.item(row, 2).data(Qt.ItemDataRole.UserRole) == selected_identity:
                table.selectRow(row)
        table.resizeColumnsToContents()
        table.setEnabled(True)
        table.apply_filter()
        table.verticalScrollBar().setValue(scroll_position)
        table.state_label.setText(f"Объектов: {len(values)} · обновлено {datetime.now():%H:%M:%S}" if values else
            "В этом namespace объектов нет. Выберите другой namespace или обновите список.")

    def open_resource_row(self, kind, row):
        _, _, table = getattr(self, f"{kind}_controls")
        cell = table.item(row, 2)
        identity = cell.data(Qt.ItemDataRole.UserRole) if cell is not None and table.isEnabled() else None
        if identity:
            cluster, resource_kind, namespace, name, uid = identity
            self.open_resource(cluster, resource_kind, namespace, name, expected_uid=uid)

    def _set_refresh_interval(self):
        ms = self.refresh_interval.currentData()
        self.timer.stop()
        if ms:
            self.timer.start(ms)
        self.preferences["refresh_ms"] = ms
        mark_poll_timer(self.timer)
        for timer in self.findChildren(QTimer):
            if timer is not self.timer and timer.property('clusterPoll'):
                if ms:
                    timer.start()
                    mark_poll_timer(timer)
                else:
                    timer.stop()
                    self.monitoring.timers.pop(timer, None)
        self._save_preferences()

    def _save_preferences(self):
        self.preference_writer.save(self.preferences)

    def _apply_theme(self):
        light = self.preferences.get("theme") == "light"
        apply_palette(self, light)
        self.setStyleSheet(stylesheet(light))
        if hasattr(self, 'log_panel'):
            self.log_panel.apply_theme(light)
        self._update_brand()
        self._apply_sidebar()
        if hasattr(self, "matrix_page"):
            self._apply_view_color(self.views.active.get("color", "transparent"))
            self.matrix.viewport().update()
            self.matrix.horizontalHeader().viewport().update()
        if hasattr(self, "versions_page"):
            self.versions_page.render()

    def _update_brand(self):
        color = "#FFFFFF" if self.preferences.get("theme") != "light" or not self.preferences.get("read_only", True) else "#20334B"
        size = 32 if self.preferences.get("sidebar_collapsed", False) else 60
        revision = (self._brand_source.cacheKey(), color, size)
        if revision == getattr(self, '_brand_revision', None):
            return
        self._brand_revision = revision
        image = QPixmap(self._brand_source)
        painter = QPainter(image)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(image.rect(), QColor(color))
        painter.end()
        self.brand_logo.setPixmap(image.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def _set_sidebar_collapsed(self, collapsed):
        self.preferences["sidebar_collapsed"] = bool(collapsed)
        self._save_preferences()
        self._update_brand()
        self._apply_sidebar()
        self._position_resource_panel()

    def _apply_sidebar(self):
        if not hasattr(self, "shell_actions"):
            return
        revision = (self.preferences.get('sidebar_collapsed', False), self.preferences.get('read_only', True),
                    self.preferences.get('theme'), tuple(id(button) for button in self.nav_buttons))
        if revision == getattr(self, '_sidebar_revision', None):
            return
        self._sidebar_revision = revision
        collapsed = self.preferences.get("sidebar_collapsed", False)
        self.sidebar.setFixedWidth(56 if collapsed else 240)
        self.sidebar.setProperty("collapsed", collapsed)
        self.sidebar_toggle.setText(">>" if collapsed else "<<")
        self.sidebar_toggle.setToolTip("Развернуть меню" if collapsed else "Свернуть меню")
        self.sidebar_toggle.setAccessibleName(self.sidebar_toggle.toolTip())
        self.brand_name.setVisible(not collapsed)
        self.brand_row.setContentsMargins(12 if collapsed else 18, 8, 12 if collapsed else 14, 8)
        self.brand_row.setSpacing(0 if collapsed else 12)
        colors = theme_colors(self.preferences.get("theme") == "light")
        from .themes import write_background
        from .window_frame import apply_caption_color
        apply_caption_color(self, write_background(self.preferences.get("theme") == "light") if not self.preferences.get("read_only", True) else None)
        color = colors["foreground"] if self.preferences.get("read_only", True) else "#FFFFFF"
        specs = (("Нагрузки и Pod", "workloads"), ("Квоты ресурсов", "quota"), ("ConfigMaps", "config"),
                 ("Журнал", "logs"), ("Dumps", "dumps"), ("Сверка версий", "versions"), ("Events", "events"))
        pairs = [(button, label, icon) for button, (label, icon) in zip(self.nav_buttons, specs)]
        pairs.extend(((self.settings_button, "Настройки", "settings"), (self.shell_actions.mode, "Режим администратора", "admin"),
                      (self.shell_actions.help_button, "help", "help")))
        for button, label, icon in pairs:
            button.setText("" if collapsed else label)
            if icon == "admin":
                asset = "readonly.svg" if self.preferences.get("read_only", True) else "write-enabled.svg"
                button.setIcon(QIcon(str(Path(__file__).resolve().parent.parent / "assets" / asset)))
            else:
                button.setIcon(navigation_icon(icon, color, colors["sidebar"] if self.preferences.get("read_only", True) else write_background(self.preferences.get("theme") == "light")))
            button.setIconSize(QSize(22, 22))
            button.setFixedHeight(44)
            button.setAccessibleName(label)
            if not button.toolTip():
                button.setToolTip(label)
        for widget in [self.sidebar, *self.sidebar.findChildren(QWidget)]:
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self.sidebar.update()
        self.sidebar.updateGeometry()
        if hasattr(self, "shell_splitter"):
            width = 56 if collapsed else 240
            self.shell_splitter.setSizes([width, max(1, self.shell_splitter.width()-width)])

    def clusters_dialog(self):
        from .connection_settings import ConnectionSettings
        ConnectionSettings(self).show()

    def _manual_refresh(self):
        from .request_policy import manual_read
        with manual_read():
            self.refresh()

    def _refresh_after_return(self):
        if getattr(self, '_matrix_manual_pending', False):
            self._matrix_manual_pending = False
            self._manual_refresh()
        else:
            self.refresh()

    def refresh(self):
        from .connection_state import ConnectionState
        ConnectionState(self).reconcile()
        if not polling_allowed(self):
            return
        if (getattr(self, '_resource_panel', None) is not None
                or getattr(self, '_resource_opening_request', None) is not None):
            return
        if self.tabs.currentIndex() == 0:
            self.refresh_matrix()
        elif self.tabs.currentIndex() == 1:
            self._refresh_resource_controls("quota")
        elif self.tabs.currentIndex() == 2:
            self._refresh_resource_controls("configmap")
        elif self.tabs.currentWidget() in (self.dumps_page, self.versions_page, self.events_page):
            self.tabs.currentWidget().refresh_data()

    def _load_initial_matrix(self):
        if self._closing:
            return
        self.refresh()

    @pyqtSlot(object)
    def _matrix_scope_loaded(self, progress):
        plan, target = progress.plan, progress.cluster
        if self._closing or plan.epoch != self._matrix_generation or not self.loading_matrix:
            return
        from .fetch_plan import FetchPlan
        from .monitoring import MonitoringCancelled
        try:
            self.monitoring.check(progress.token)
            current = FetchPlan.capture(self.store.clusters(), self.views.active, plan.epoch)
            if current != plan or target not in current.clusters:
                return
            if current.namespaces is not None and progress.namespace not in current.namespaces:
                return
        except (MonitoringCancelled, ValueError):
            return
        cluster = self.snapshot.setdefault(target.identifier, {"name": target.label, "namespaces": {}})
        cluster["namespaces"][progress.namespace] = progress.scope
        if not self.matrix_paint_timer.isActive():
            self.matrix_paint_timer.start()

    def _refresh_resource_controls(self, kind):
        if kind == "quota":
            self.load_quotas()
            return
        cluster_box, namespace_box, table = getattr(self, f"{kind}_controls")
        selected = cluster_box.currentData()
        clusters = [item for item in self.store.clusters() if item["enabled"]]
        cluster_box.blockSignals(True)
        cluster_box.clear()
        for cluster in clusters:
            cluster_box.addItem(cluster["name"], cluster["id"])
        cluster_box.setCurrentIndex(max(0, cluster_box.findData(selected)))
        cluster_box.blockSignals(False)
        current = cluster_box.currentData()
        if current and (current != selected or not namespace_box.count()):
            self._load_namespaces_for(kind)
        elif current and namespace_box.currentText():
            self.load_resource_page(kind)
        elif not current:
            blocker = QSignalBlocker(namespace_box)
            namespace_box.clear()
            del blocker
            self._invalidate_resource_list(kind)
            table.state_label.setText('Нет активных подключений. Добавьте кластер в настройках.')
            table.set_empty_text('Нет активных подключений.')

    @staticmethod
    def _restore_header_identity(header):
        blocked = header.blockSignals(True)
        try:
            for logical in range(header.count()):
                visual = header.visualIndex(logical)
                if visual != logical:
                    header.moveSection(visual, logical)
        finally:
            header.blockSignals(blocked)

    def _reset_matrix_state(self):
        """Invalidate pending reads and all presentation layers; keep user views."""
        self._matrix_generation += 1
        canceled = getattr(self, '_matrix_cancel', None)
        if canceled is not None: canceled.set()
        self.loading_matrix = False
        self.snapshot = {}
        self._matrix_failures = ()
        self.matrix_return_timer.stop()
        self._scope_index_cache = {}
        self._row_names, self._columns = [], []
        self.matrix_data.clear()
        self.matrix._cell_keys.clear()
        self.matrix._cell_visuals.clear()
        self._matrix_structure = None
        self._matrix_visual_dirty = False
        self.matrix_paint_timer.stop()
        self.recent_age_timer.stop()
        self.matrix._set_hover((-1, -1))
        self.matrix.clear()
        self.matrix.setRowCount(0)
        # The empty renderer contract retains the service/name column.
        self.matrix.setColumnCount(1)
        self.matrix.setHorizontalHeaderLabels([''])
        self.matrix.row_kinds = []
        self.matrix.group_rows = set()
        self.matrix._height_groups = set()
        self.matrix.sync_frozen()
        self.summary_label.setText("0  Total Pods")
        self.recent_label.setText('● Recently started 0')
        self.extra_counters.clear()
        self.workload_counters.clear()
        self.workload_counters.hide()
        for status, box in self.status_filters.items():
            box.setText(f"{'Error' if status == 'Failed' else status}  0")
        self.last_updated.setText("")
        self.last_updated.setToolTip("")

    def refresh_matrix(self):
        from .matrix_controller import MatrixController
        if not hasattr(self, '_matrix_controller'):
            self._matrix_controller = MatrixController(self)
        self._matrix_controller.refresh()

    def render_matrix(self):
        if not hasattr(self, "matrix"):
            return
        if getattr(self, "_closing", False):
            return
        if (getattr(self, "_resource_panel", None) is not None
                or getattr(self, '_resource_opening_request', None) is not None or self.isMinimized()
                or self.tabs.currentWidget() is not self.matrix_page):
            self._matrix_visual_dirty = True
            return
        self._matrix_visual_dirty = False
        updates = self.matrix.updatesEnabled()
        syncing = getattr(self.matrix, "_syncing_frozen", False)
        geometry = self.matrix.frozen_layout_signature()
        self.matrix._syncing_frozen = True
        try:
            self._render_matrix_content()
        except Exception:
            self._matrix_structure = None
            raise
        finally:
            self.matrix._syncing_frozen = syncing
            if not syncing and geometry != self.matrix.frozen_layout_signature():
                self.matrix.sync_frozen()
            if self.matrix.updatesEnabled() != updates:
                self.matrix.setUpdatesEnabled(updates)
            self._rendering_matrix = False

    def _render_matrix_content(self):
        from .matrix_presenter import MatrixPresenter
        if not hasattr(self, '_matrix_presenter'):
            self._matrix_presenter = MatrixPresenter(self)
        self._matrix_presenter.render()


    def _open_workload(self, ref):
        resource = ref[3]
        kind = resource.get("kind", "Deployment").lower()
        if kind not in ("deployment", "pod", "statefulset", "daemonset", "replicaset", "replicationcontroller", "job", "cronjob"):
            self.statusBar().showMessage("Откройте Pod этой нагрузки.", 4000)
            return
        self.open_resource(ref[0], kind, ref[1], resource.get("metadata", {}).get("name", ref[2]), expected_uid=resource.get("metadata", {}).get("uid"))

    def _matrix_click(self, row, col, point):
        ref = self.matrix_data.get((row, col))
        if col == 0:
            choices = [key for key in self.matrix_data if key[0] == row]
            if len(choices) == 1:
                target = self.matrix_data[choices[0]]
                self._open_workload(target)
            elif choices:
                from .resource_actions import choose_location
                entries = [(f"{self.snapshot[ref[0]]['name']} / {ref[1]}" if self.preferences.get("show_cluster_names", True) else ref[1],
                            (ref[0], ref[3].get("kind", "Deployment").lower(), ref[1], ref[3].get("metadata", {}).get("name", ref[2]), ref[3].get("metadata", {}).get("uid")))
                           for key in choices for ref in [self.matrix_data[key]]]
                target = choose_location(self, entries)
                if target:
                    self.open_resource(*target[:4], expected_uid=target[4])
            return
        if not ref:
            return
        rect = self.matrix.visualItemRect(self.matrix.item(row, col))
        i = pod_at(self.matrix.content_rect(rect, row), point, len(self.matrix.cell_visuals(row,col)))
        if ref[3].get('kind') == 'CronJob':
            if i is not None:
                pod = self._latest_cron_pod(ref)
                if pod:
                    meta = pod['metadata']
                    self.open_resource(ref[0], 'pod', ref[1], meta['name'], expected_uid=meta.get('uid'))
                else:
                    self.statusBar().showMessage('У CronJob нет доступных Pod.', 4000)
                return
            self._open_workload(ref)
            return
        if i is not None and i < len(ref[4]):
            self.open_resource(ref[0], "pod", ref[1], ref[4][i]["metadata"]["name"], expected_uid=ref[4][i]["metadata"].get("uid"))
        else:
            self._open_workload(ref)

    def _pod_menu(self, point):
        index = self.matrix.indexAt(point)
        ref = self.matrix_data.get((index.row(), index.column()))
        if not ref:
            return
        rect = self.matrix.visualRect(index)
        if not self.matrix.content_rect(rect, index.row()).contains(point):
            return
        i = pod_at(self.matrix.content_rect(rect, index.row()), point, len(self.matrix.cell_visuals(index.row(),index.column())))
        if ref[3].get('kind') == 'CronJob' and i is not None:
            pod = self._latest_cron_pod(ref)
            if pod:
                meta = pod['metadata']
                identity = (ref[0], 'pod', ref[1], meta['name'], meta.get('uid'))
            else:
                identity = (ref[0], 'cronjob', ref[1], ref[3]['metadata']['name'], ref[3]['metadata'].get('uid'))
        elif i is None or i >= len(ref[4]):
            identity = (ref[0], ref[3].get("kind", "Deployment").lower(), ref[1], ref[3].get("metadata", {}).get("name", ref[2]), ref[3].get("metadata", {}).get("uid"))
        else:
            metadata = ref[4][i]["metadata"]
            identity = (ref[0], "pod", ref[1], metadata["name"], metadata.get("uid"))
        self.resource_actions.show(identity, self.matrix.viewport().mapToGlobal(point), allow_delete=False)

    def _latest_cron_pod(self, ref):
        from .cron_history import latest_available_pod
        scope = self.snapshot.get(ref[0], {}).get('namespaces', {}).get(ref[1], {})
        return latest_available_pod(ref[3].get('metadata', {}).get('uid'), scope.get('jobs', []), scope.get('pods', []))

    def open_resource(self, cluster_id, kind, namespace, name, initial_tab=None, expected_uid=None):
        try:
            source_credentials = credentials_revision(self.api.cluster(cluster_id))
        except Exception:
            self.statusBar().showMessage('Подключение недоступно; ресурс не открыт.', 5000)
            return
        origin = getattr(self, "_resource_panel", None)
        if origin is not None:
            origin._navigation_pending = True
            origin.update_write_controls()
        self._resource_request = getattr(self, "_resource_request", 0) + 1
        request_id = self._resource_request
        self._resource_opening_request = request_id
        self._cancel_workspace_reads()
        self.events_page.set_demanded(False)
        matrix_cancel = getattr(self, '_matrix_cancel', None)
        if matrix_cancel is not None:
            matrix_cancel.set()
        self._matrix_generation += 1
        self.loading_matrix = False
        self._matrix_manual_pending = False
        self.matrix_paint_timer.stop()
        self.matrix_return_timer.stop()
        canceled = getattr(self, '_resource_open_cancel', None)
        if canceled is not None: canceled.set()
        self._resource_open_cancel = open_cancel = threading.Event()
        def load():
            try:
                from .request_policy import expected_credentials
                with expected_credentials({cluster_id: source_credentials}):
                    resource = self.api.resource(cluster_id, kind, namespace, name)
                if expected_uid and resource.get("metadata", {}).get("uid") != expected_uid:
                    return None
                return resource
            except ResourceNotFoundError:
                return None
        def done(resource):
            if self._closing or request_id != self._resource_request:
                return
            if origin is not None:
                origin._navigation_pending = False
                origin.update_write_controls()
            try:
                current_credentials = credentials_revision(self.api.cluster(cluster_id))
            except Exception:
                current_credentials = None
            if current_credentials != source_credentials:
                self.set_read_only(True)
                self.statusBar().showMessage('Подключение изменилось; откройте ресурс заново.', 5000)
                return
            if resource is None:
                message = f"Pod {namespace}/{name} больше не существует или был заменён. Список обновлён." if kind == "pod" else f"Ресурс {namespace}/{name} больше не существует."
                self.statusBar().showMessage(message)
                previous = getattr(self, "_resource_panel", None)
                if previous is not None:
                    from PyQt5 import sip
                    if not sip.isdeleted(previous) and previous.kind == "deployment":
                        previous.deployment_notice.setText(message)
                        previous.deployment_notice.show()
                        previous.reload_deployment_pods()
                return
            if kind == "quota":
                run_dialog(TextDialog(self, f"ResourceQuota {name}", dump_resource(resource, allow_unicode=True)))
            else:
                stack = getattr(self, "_resource_stack", [])
                previous = stack[-1] if stack else None
                if previous is not None and previous.pending_operation:
                    return
                keep_parent = previous is not None and previous.kind == "deployment" and kind == "pod"
                # Returning to an existing Deployment keeps its tab, selection and edits.
                ancestor = next((p for p in stack[:-1] if (p.cluster_id, p.kind, p.namespace, p.name) == (cluster_id, kind, namespace, name)), None)
                if ancestor:
                    while self._resource_stack[-1] is not ancestor:
                        if not self._resource_stack[-1].close():
                            return
                    return
                replace = [] if keep_parent else (stack[-1:] if previous is not None and previous.kind == "pod" and kind == "pod" else stack[:])
                if any(not p.can_leave() for p in reversed(replace)):
                    for p in replace:
                        p._allow_discard = False
                    return
                if keep_parent and previous.yaml_editor.editor.document().isModified():
                    self.statusBar().showMessage("В Deployment остался несохранённый YAML. Правки сохранены в открытой карточке.")
                for previous_panel in reversed(replace):
                    previous_panel.close()
                if not getattr(self, "_resource_stack", []):
                    overlay = QWidget(self.workspace)
                    overlay.setObjectName("drawerOverlay")
                    overlay.setStyleSheet("QWidget#drawerOverlay { background:rgba(0,0,0,100); }")
                    overlay.setGeometry(self.workspace.rect())
                    overlay.mousePressEvent = self._dismiss_resource_event
                    overlay.show()
                    self._resource_overlay = overlay
                    self._resource_stack = []
                panel = ResourceDialog(self, self.api, self.background, cluster_id, kind, namespace, name, resource,
                                       source_credentials=source_credentials)
                panel.managed_panel = True
                panel.finished.connect(lambda result, panel=panel: self._resource_finished(panel))
                if initial_tab:
                    for i in range(panel.tabs.count()):
                        if panel.tabs.tabText(i) == initial_tab:
                            panel.tabs.setCurrentIndex(i)
                for ancestor_panel in self._resource_stack:
                    ancestor_panel.hide()
                self._resource_stack.append(panel)
                self._resource_panel = panel
                self.inventory.set_demanded(False)
                if getattr(self, 'loading_matrix', False):
                    canceled = getattr(self, '_matrix_cancel', None)
                    if canceled is not None: canceled.set()
                    self._matrix_generation += 1
                    self.loading_matrix = False
                    self._matrix_visual_dirty = True
                self._position_resource_panel()
                panel.show()
                panel.raise_()
                self.refresh_write_controls()
        def failed(message):
            if request_id != self._resource_request:
                return
            if origin is not None:
                origin._navigation_pending = False
                origin.update_write_controls()
                if hasattr(origin, "nav_cluster"):
                    origin._restore_navigation()
            error(self, message)
        from .monitoring import cancellable_read
        def finalized(_):
            if getattr(self, '_resource_opening_request', None) == request_id:
                self._resource_opening_request = None
                if getattr(self, '_resource_panel', None) is None and not self._closing:
                    self.refresh()
        if self.background.submit(cancellable_read(load, open_cancel, self.monitoring.token()), done, failed,
                                  key='resource-open', priority=True, finalized=finalized) is False:
            finalized('CANCELLED')
            if origin is not None:
                origin._navigation_pending = False
                origin.update_write_controls()
            self.statusBar().showMessage('Чтение ресурса отложено; мониторинг остановлен или очередь занята.', 5000)

    def _dismiss_resource_event(self, event):
        panel = getattr(self, "_resource_panel", None)
        if panel is not None:
            panel.close()

    def _resource_finished(self, panel):
        self._resource_request = getattr(self, "_resource_request", 0) + 1
        self._resource_opening_request = None
        canceled = getattr(self, '_resource_open_cancel', None)
        if canceled is not None: canceled.set()
        stack = getattr(self, "_resource_stack", [])
        if panel in stack:
            stack.remove(panel)
        self._resource_panel = stack[-1] if stack else None
        if self._resource_panel is not None:
            self._resource_panel._navigation_pending = False
            self._resource_panel.update_write_controls()
            self._resource_panel.show()
        panel.deleteLater()
        if not stack:
            page = self.tabs.currentWidget()
            self.inventory.set_demanded(page in (self.dumps_page, self.versions_page),owner=page)
            overlay = getattr(self, "_resource_overlay", None)
            if overlay is not None:
                overlay.hide()
                overlay.deleteLater()
                self._resource_overlay = None
            if getattr(self, "_matrix_visual_dirty", False):
                self.render_matrix()
            if not self._closing:
                self.matrix_return_timer.start(0)
        self._position_resource_panel()
        self.refresh_write_controls()

    def _position_resource_panel(self):
        if not hasattr(self, "workspace"):
            return
        from PyQt5 import sip
        overlay = getattr(self, "_resource_overlay", None)
        if overlay is not None and not sip.isdeleted(overlay):
            overlay.setGeometry(self.workspace.rect())
        stack = getattr(self, "_resource_stack", [])
        width = self.workspace.width()
        for depth, panel in enumerate(stack):
            if sip.isdeleted(panel):
                continue
            panel_width = width
            panel.setGeometry(width-panel_width, 0, panel_width, self.workspace.height())
            panel.raise_()
        strip = getattr(self, "_resource_return_strip", None)
        if strip is not None and not sip.isdeleted(strip):
            strip.hide()
        monitoring = getattr(self, 'monitoring', None)
        if monitoring is not None and monitoring.policy.state in ('SLEEPING', 'RECONNECTING'):
            monitoring.overlay.setGeometry(self.workspace.rect())
            monitoring.overlay.raise_()
    def eventFilter(self, watched, event):
        if watched is getattr(self, "workspace", None) and event.type() == QEvent.Type.Resize:
            self._position_resource_panel()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_resource_panel()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange and not self.isMinimized() and getattr(self, '_matrix_visual_dirty', False):
            QTimer.singleShot(0, self.render_matrix)

    def closeEvent(self, event):
        if self._closing:
            event.accept()
            return
        fatal = getattr(self,'_fatal_shutdown',False)
        if self._pod_operations and not fatal:
            self.statusBar().showMessage("Дождитесь завершения операции с Pod перед закрытием приложения.", 5000)
            event.ignore()
            return
        if not fatal and not self._close_resource_stack():
            event.ignore()
            return
        if fatal:
            for panel in getattr(self,'_resource_stack',[]): panel._stop_panel()
        self._closing = True
        self.monitoring.close()
        QApplication.instance().removeEventFilter(self.shell_actions)
        self.shell_actions.idle_timer.stop()
        self.matrix_paint_timer.stop()
        self.recent_age_timer.stop()
        self.deployment_mode.timer.stop()
        self.matrix_return_timer.stop()
        self.timer.stop()
        self.auth_timer.stop()
        self.admin_access.timer.stop()
        self.api.stopped = True
        try:
            self.background.stop()
            self._save_preferences()
        finally:
            self.preference_writer.close()
            logger.info("Quaber закрывается")
            # No new producers survive; wait for already started reads before
            # closing proxies so they cannot recreate them after cleanup.
            pool, proxy, writer, logs, api = self.background.pool, self.proxy, self.preference_writer, self.log_session, self.api
            def cleanup():
                try:
                    try:
                        pool.waitForDone()
                        api.close()
                    finally:
                        proxy.close()
                except Exception:
                    logger.error("Ошибка финализации фоновых процессов")
                finally:
                    writer.thread.join(timeout=5)
                    logs.close()
            self._cleanup_thread = threading.Thread(target=cleanup, name="quaber-shutdown")
            self._cleanup_thread.start()
        super().closeEvent(event)
