"""Native Qt desktop shell for Quaber."""

from __future__ import annotations

import copy
import difflib
import json
import random
import time
import re
import uuid
import html
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

import yaml
from PyQt6.QtCore import QObject, QPointF, QRunnable, QThreadPool, QTimer, Qt, QEvent, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap, QPolygonF, QKeySequence, QAction
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QDialog,
    QDialogButtonBox, QFileDialog, QFormLayout, QGridLayout, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QSpinBox, QSplitter, QTabBar, QTabWidget,
    QTableWidgetItem, QSizePolicy, QToolTip, QTextEdit, QVBoxLayout, QWidget, QScrollArea, QTextBrowser)

from .kubectl import ProxyManager, locate_kubectl, login, test_connection
from .kubernetes import Kubernetes, ResourceNotFoundError
from .storage import UserStore, validate_server
from .views import MatrixViews
from .widgets import FullCheckBox, AppComboBox as QComboBox, CodeEditor, NamespaceHeader, PodDelegate, PodMatrix, stylesheet, pod_status, pod_at, resource_age, resource_percent, StatusDelegate
from PyQt6.QtWidgets import QMenu, QButtonGroup, QToolButton
from concurrent.futures import ThreadPoolExecutor, as_completed
from .app_logging import LogSession, register_secret
from .log_panel import LogPanel
from .resource_list import ResourceList
from .quota_card import QuotaCard
from .view_tabs import ViewTabs, VIEW_COLORS, blended_color, color_icon
from .themes import theme as theme_colors, apply_palette
from .safety import check_identity, resource_changes, scale_summary
from .preferences import PreferenceWriter
from .background import Background, PanelBackground
from .resource_index import index_scope
from PyQt6.QtWidgets import QPlainTextEdit
from PyQt6.QtGui import QTextCursor

logger = logging.getLogger("quaber.ui")


def run_dialog(dialog):
    """Dispose temporary modal widgets after their caller has read the result."""
    try:
        return dialog.exec()
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
    QMessageBox.warning(parent, "Quaber", message)


def confirm(parent, title: str, text: str) -> bool:
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


class ClusterDialog(QDialog):
    def __init__(self, parent, store: UserStore, app_dir: Path, cluster: dict | None = None):
        super().__init__(parent)
        self.store, self.app_dir, self.cluster = store, app_dir, cluster
        self.setWindowTitle("Кластер — Quaber")
        self.resize(620, 410)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(cluster["name"] if cluster else "")
        self.server = QLineEdit(cluster["server"] if cluster else "")
        self.username = QLineEdit(cluster["username"] if cluster else "")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.remember_password = QCheckBox("Сохранить пароль")
        self.remember_password.setChecked(bool(cluster and cluster.get("encryptedPassword")))
        password_error = ""
        if cluster:
            try:
                self.password.setText(store.saved_password(cluster))
            except Exception:
                password_error = "Сохранённый пароль повреждён. Введите его заново."
        self.kubectl_path = QLineEdit(store.preferences().get("kubectl_path", ""))
        self.kubectl_path.setPlaceholderText("Автоматически из PATH")
        client_row = QHBoxLayout()
        client_row.addWidget(self.kubectl_path)
        browse = QPushButton("Обзор…")
        browse.clicked.connect(self.choose_kubectl)
        client_row.addWidget(browse)
        self.enabled = QCheckBox("Кластер активен")
        self.enabled.setChecked(cluster["enabled"] if cluster else True)
        form.addRow("Имя", self.name)
        form.addRow("API server", self.server)
        form.addRow("Пользователь", self.username)
        form.addRow("Пароль", self.password)
        form.addRow("", self.remember_password)
        form.addRow("", self.enabled)
        self.form_widget = QWidget()
        self.form_widget.setLayout(form)
        layout.addWidget(self.form_widget)
        hint = QLabel("По галочке пароль сохраняется зашифрованным в UserData.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.status = QLabel(password_error)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.test_button = buttons.addButton("Проверить", QDialogButtonBox.ButtonRole.ActionRole)
        self.test_button.clicked.connect(lambda: self.connect_cluster(False))
        buttons.accepted.connect(lambda: self.connect_cluster(True))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.buttons = buttons
        self.busy = False
        self.server.textEdited.connect(self._identity_changed)
        self.username.textEdited.connect(self._identity_changed)

    def _identity_changed(self):
        self.password.clear()
        self.remember_password.setChecked(False)

    def choose_kubectl(self):
        path, _ = QFileDialog.getOpenFileName(self, "Выберите kubectl", self.kubectl_path.text() or str(self.app_dir),
                                             "Исполняемые файлы (*.exe);;Все файлы (*)")
        if path:
            self.kubectl_path.setText(path)
        return bool(path)

    def connect_cluster(self, save: bool):
        if self.busy:
            return
        name, server, username, password = self.name.text(), self.server.text(), self.username.text(), self.password.text()
        register_secret(password)
        existing = self.cluster
        enabled = self.enabled.isChecked()
        remember_password = self.remember_password.isChecked()
        try:
            server = validate_server(server.strip())
            try:
                executable = locate_kubectl(self.app_dir, self.kubectl_path.text())
            except FileNotFoundError as exc:
                self.status.setText(str(exc))
                if not self.choose_kubectl():
                    return
                executable = locate_kubectl(self.app_dir, self.kubectl_path.text())
        except (ValueError, OSError) as exc:
            self.status.setText(str(exc))
            return
        configured_path = str(executable) if self.kubectl_path.text().strip() else ""
        preferences = self.store.preferences()
        preferences["kubectl_path"] = configured_path
        self.store.save_preferences(preferences)
        if hasattr(self.parent(), "preferences"):
            self.parent().preferences["kubectl_path"] = configured_path
        if not password and (not existing or server.strip() != existing["server"] or username.strip() != existing["username"]):
            error(self, "Для нового кластера или смены API server/пользователя нужен пароль.")
            return
        self.status.setText("Выполняется kubectl login…" if password else "Проверка сохранённого подключения…")
        self.buttons.setEnabled(False)
        self.form_widget.setEnabled(False)
        self.busy = True
        def work():
            config = login(executable, server.strip(), username.strip(), password) if password else None
            if save:
                record = self.store.put_cluster(name, server, username, config, enabled, existing["id"] if existing else None,
                                               password=password, remember_password=remember_password, expected=existing)
                return record
            if config:
                count = test_connection(executable, self.store, server.strip(), config)
                return f"Подключение установлено, namespaces: {count}. Настройки ещё не сохранены."
            config = self.store.decrypt(existing["encryptedKubeconfig"])
            count = test_connection(executable, self.store, server, config)
            return f"Подключение установлено, namespaces: {count}."
        def done(value):
            self.busy = False
            self.buttons.setEnabled(True)
            self.form_widget.setEnabled(True)
            if save:
                self.password.clear()
                self.accept()
            else:
                self.status.setText(str(value))
        def failed(message):
            self.busy = False
            self.buttons.setEnabled(True)
            self.form_widget.setEnabled(True)
            self.password.clear()
            self.status.setText(message)
        self.parent().background.submit(work, done, failed)

    def closeEvent(self, event):
        if self.busy:
            event.ignore()
        else:
            for field in ([self.password] if hasattr(self, "password") else self.passwords):
                field.clear()
            super().closeEvent(event)

    def reject(self):
        if not self.busy:
            for field in ([self.password] if hasattr(self, "password") else self.passwords):
                field.clear()
            super().reject()


class LoginGroupsDialog(QDialog):
    def __init__(self, parent, groups: list[dict]):
        super().__init__(parent)
        self.groups = groups
        self.setWindowTitle("Требуется вход в кластеры")
        self.resize(620, 380)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Срок действия подключения истёк. Сохранённые пароли подставляются автоматически."))
        self.passwords = []
        for group in groups:
            layout.addWidget(QLabel(f"{group['username']}: {', '.join(cluster['name'] for cluster in group['clusters'])}"))
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
            layout.addWidget(password)
            self.passwords.append(password)
        self.status = QLabel("")
        layout.addWidget(self.status)
        layout.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Войти")
        buttons.accepted.connect(self.submit)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.buttons = buttons
        self.busy = False

    def submit(self):
        passwords = [widget.text() for widget in self.passwords]
        for password in passwords:
            register_secret(password)
        if any(not value for value in passwords):
            error(self, "Введите пароль для каждого пользователя.")
            return
        self.buttons.setEnabled(False)
        self.busy = True
        self.status.setText("Выполняется kubectl login…")
        parent = self.parent()
        app_dir, store = parent.app_dir, parent.store
        def work():
            for group, password in zip(self.groups, passwords):
                updates = {cluster["id"]: login(locate_kubectl(app_dir, store.preferences().get("kubectl_path", "")),
                                               cluster["server"], group["username"], password)
                           for cluster in group["clusters"]}
                store.update_logins(updates, password=password, expected={c["id"]: c for c in group["clusters"]})
            return True
        def done(_):
            self.busy = False
            for widget in self.passwords:
                widget.clear()
            self.accept()
        def failed(message):
            self.busy = False
            for widget in self.passwords:
                widget.clear()
            self.status.setText(message)
            self.buttons.setEnabled(True)
        self.parent().background.submit(work, done, failed)

    def closeEvent(self, event):
        if self.busy:
            event.ignore()
        else:
            for field in ([self.password] if hasattr(self, "password") else self.passwords):
                field.clear()
            super().closeEvent(event)

    def reject(self):
        if not self.busy:
            for field in ([self.password] if hasattr(self, "password") else self.passwords):
                field.clear()
            super().reject()


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

    def set_data(self, values: list[float | None], request: float | None = None, limit: float | None = None):
        self.values, self.request, self.limit = values, request, limit
        self.update()

    def maximum(self):
        if self.limit is not None and self.limit > 0:
            return self.limit
        return max([v for v in [*self.values, self.request] if v is not None] + [1.0]) * 1.1

    def mouseMoveEvent(self, event):
        left, right, top, bottom = 64, self.width() - 16, 34, self.height() - 24
        point = event.position()
        if self.values and right > left and left <= point.x() <= right and top <= point.y() <= bottom:
            self.hover_index = min(len(self.values)-1, max(0, round((point.x()-left)/(right-left)*(len(self.values)-1))))
            value = self.values[self.hover_index]
            stamp = self.timestamps[self.hover_index].strftime("%H:%M:%S") if self.hover_index < len(self.timestamps) else f"#{self.hover_index+1}"
            text = f"{stamp}  {value:.2f} {self.unit}" if value is not None else f"{stamp}  No data"
            QToolTip.showText(event.globalPosition().toPoint(), text, self)
        else:
            self.hover_index = None
            QToolTip.hideText()
        self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.hover_index = None
        QToolTip.hideText()
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
                segment.append(QPointF(left + (right-left)*index/max(1, len(self.values)-1), y_value(value)))
            if len(segment) > 1:
                painter.drawPolyline(segment)
            elif len(segment) == 1:
                painter.drawEllipse(segment[0], 2, 2)
        if self.hover_index is not None and self.hover_index < len(self.values):
            x = left + (right-left)*self.hover_index/max(1, len(self.values)-1)
            painter.setPen(QPen(color, 1, Qt.PenStyle.DotLine))
            painter.drawLine(QPointF(x, top), QPointF(x, bottom))
            value = self.values[self.hover_index]
            if value is not None:
                painter.setBrush(QColor("#51a7f9"))
                painter.drawEllipse(QPointF(x, y_value(value)), 4, 4)
        painter.setPen(color)
        painter.drawText(8, bottom, "0")
        painter.drawText(8, top + 7, f"{maximum:.1f}")


class MetricsWidget(QWidget):
    def __init__(self, parent, api: Kubernetes, background: Background, cluster_id: str, namespace: str, pod: str):
        super().__init__(parent)
        self.api, self.background, self.cluster_id, self.namespace, self.pod = api, background, cluster_id, namespace, pod
        self.history: dict[str, dict[str, list[float | None]]] = {}
        self.timestamps = {}
        self.current: dict = {}
        self.loading = False
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.status = QLabel("Загрузка метрик…")
        row.addWidget(self.status, 1)
        self.container = QComboBox()
        self.container.currentIndexChanged.connect(self.render)
        row.addWidget(self.container)
        layout.addLayout(row)
        self.mode = QComboBox()
        self.mode.addItems(["%", "Фактические"])
        self.mode.setCurrentIndex(1)
        self.mode.currentIndexChanged.connect(self.render)
        row.addWidget(self.mode)
        self.cpu = MetricChart("CPU", "m")
        self.memory = MetricChart("RAM", "MiB")
        self.throttling = MetricChart("CPU throttling", "%")
        for chart in (self.cpu, self.memory, self.throttling):
            layout.addWidget(chart)
        layout.addStretch()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.load)
        self.timer.start(5000)
        QTimer.singleShot(0, self.load)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.load)

    def load(self):
        if self.loading or not self.isVisible():
            return
        self.loading = True
        def done(value):
            self.loading = False
            self.current = value
            self.status.setText(f"CPU/RAM: {value.get('source') or 'нет данных'} · Throttling: {'cAdvisor' if value.get('throttlingAvailable') else 'нет данных'}" +
                                (f" · {value.get('error')}" if not value.get("available") else ""))
            for container in value.get("containers", []):
                name = container["name"]
                history = self.history.setdefault(name, {"cpu": [], "memory": [], "throttle": []})
                times = self.timestamps.setdefault(name, [])
                times.append(datetime.now())
                del times[:-120]
                history["cpu"].append(container.get("cpu", {}).get("usageCores"))
                history["memory"].append(container.get("memory", {}).get("usageBytes"))
                history["throttle"].append(container.get("cpuThrottlingPercent"))
                for samples in history.values():
                    del samples[:-120]
                if self.container.findText(name) < 0:
                    self.container.addItem(name)
            self.render()
        def failed(message):
            self.loading = False
            self.status.setText("Метрики устарели / недоступны: " + message)
            for chart in (self.cpu, self.memory, self.throttling):
                chart.set_data([None], None, None)
        self.background.submit(lambda: self.api.metrics(self.cluster_id, self.namespace, self.pod), done, failed)

    def render(self):
        name = self.container.currentText()
        container = next((item for item in self.current.get("containers", []) if item["name"] == name), None)
        if not container:
            return
        history = self.history.get(name, {})
        for chart in (self.cpu, self.memory, self.throttling):
            chart.timestamps = self.timestamps.get(name, [])
        cpu = container.get("cpu", {})
        memory = container.get("memory", {})
        self.cpu.set_data([v * 1000 if v is not None else None for v in history.get("cpu", [])],
                          cpu.get("requestCores") * 1000 if cpu.get("requestCores") is not None else None,
                          cpu.get("limitCores") * 1000 if cpu.get("limitCores") is not None else None)
        self.memory.set_data([v / 1048576 if v is not None else None for v in history.get("memory", [])],
                             memory.get("requestBytes") / 1048576 if memory.get("requestBytes") is not None else None,
                             memory.get("limitBytes") / 1048576 if memory.get("limitBytes") is not None else None)
        if self.mode.currentIndex() == 0:
            self.cpu.unit = self.memory.unit = "%"
            for chart, samples, limit in ((self.cpu, history.get("cpu", []), cpu.get("limitCores")),
                                          (self.memory, history.get("memory", []), memory.get("limitBytes"))):
                request = cpu.get("requestCores") if chart is self.cpu else memory.get("requestBytes")
                chart.set_data([100*v/limit if v is not None and limit else None for v in samples], 100*request/limit if request is not None and limit else None, 100 if limit else None)
        else:
            self.cpu.unit, self.memory.unit = "m", "MiB"
        self.throttling.set_data(history.get("throttle", []), limit=100)


class ResourceDialog(QDialog):
    def __init__(self, parent, api: Kubernetes, background: Background, cluster_id: str, kind: str, namespace: str, name: str, resource: dict):
        embedded = hasattr(parent, "workspace")
        super().__init__(parent.workspace if embedded else parent)
        if embedded:
            # QDialog adds the Dialog flag in its constructor even when Widget (0) is supplied.
            self.setWindowFlags(Qt.WindowType.Widget)
        self.controller = parent
        self.api = api
        self.background = PanelBackground(background, self)
        self.cluster_id, self.kind, self.namespace, self.name = cluster_id, kind, namespace, name
        self.resource = resource
        self.version = resource.get("metadata", {}).get("resourceVersion", "")
        self.pending_operation = False
        self.setWindowTitle(f"{kind.title()}: {name} — {namespace}")
        self.resize(1000, 720)
        self.setObjectName("resourcePanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        header_widget = QWidget()
        header_widget.setObjectName("resourceHeader")
        header = QHBoxLayout(header_widget)
        header.setContentsMargins(18, 12, 18, 12)
        title = QLabel(name)
        title.setObjectName("resourceTitle")
        header.addWidget(title, 1)
        layout.addWidget(header_widget)
        context_widget = QWidget()
        context_widget.setObjectName("resourceContext")
        context = QHBoxLayout(context_widget)
        context.setContentsMargins(18, 8, 18, 12)
        self.context_values = {}
        context_entries = [("Кластер", self.api.cluster(cluster_id)["name"]), ("Namespace", namespace)]
        if kind == "deployment":
            context_entries.append(("Pods", f"{resource.get('status', {}).get('availableReplicas', 0)}/{resource.get('spec', {}).get('replicas', 1)}"))
        elif kind == "pod":
            context_entries.append(("Статус", pod_status(resource)))
        for label, value in context_entries:
            box = QVBoxLayout()
            box.addWidget(QLabel(label))
            value_label = QLabel(str(value))
            value_label.setObjectName("contextValue")
            self.context_values[label] = value_label
            box.addWidget(value_label)
            context.addLayout(box, 1)
        layout.addWidget(context_widget)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        summary = QTextBrowser()
        self.details = summary
        summary.setHtml(self._summary(resource))
        self.tabs.addTab(summary, "Details")
        self.yaml_editor = CodeEditor()
        self.yaml_editor.setPlainText(yaml.safe_dump(resource, allow_unicode=True, sort_keys=False))
        self.yaml_editor.editor.document().setModified(False)
        self._allow_discard = False
        self.yaml_editor.editor.textChanged.connect(self._yaml_changed)
        self.tabs.addTab(self.yaml_editor, "YAML")
        if kind == "pod":
            self._pod_tabs()
            for label in ("Details", "Логи", "Консоль Pod", "YAML", "Метрики", "События", "Контейнеры"):
                index = next(i for i in range(self.tabs.count()) if self.tabs.tabText(i) == label)
                widget = self.tabs.widget(index)
                self.tabs.removeTab(index)
                self.tabs.addTab(widget, label)
            self.tabs.setCurrentIndex(0)
        elif kind == "deployment":
            self._deployment_tabs()
            self.tabs.setCurrentIndex(0)
        controls = QHBoxLayout()
        refresh_object = QPushButton("Обновить объект")
        refresh_object.clicked.connect(self.refresh_object)
        controls.addWidget(refresh_object)
        save = QPushButton("Сохранить YAML")
        save.clicked.connect(self.save_yaml)
        controls.addWidget(save)
        self.tabs.currentChanged.connect(lambda i: save.setVisible(self.tabs.tabText(i) == "YAML"))
        save.setVisible(self.tabs.tabText(self.tabs.currentIndex()) == "YAML")
        if kind == "deployment":
            scale = QPushButton("Масштабировать")
            scale.clicked.connect(self.scale)
            controls.addWidget(scale)
            resources = QPushButton("CPU / RAM")
            resources.clicked.connect(self.edit_resources)
            controls.addWidget(resources)
            restart = QPushButton("Reload pods")
            restart.clicked.connect(self.restart)
            controls.addWidget(restart)
        if kind == "pod":
            remove = QPushButton("Evict Pod (PDB)" if self._controlled() else "Удалить Pod")
            remove.clicked.connect(self.delete_pod)
            controls.addWidget(remove)
        controls.addStretch()
        close = QPushButton("Назад")
        close.clicked.connect(self._go_back)
        controls.addWidget(close)
        header.addLayout(controls)
        if kind in ("pod", "deployment") and hasattr(self.controller, "store"):
            self._setup_navigation(layout)

    def _setup_navigation(self, layout):
        self.nav_cluster, self.nav_namespace, self.nav_deployment, self.nav_pod = (QComboBox() for _ in range(4))
        bar = QWidget()
        grid = QGridLayout(bar)
        grid.setContentsMargins(18, 4, 18, 8)
        for column, (label, combo) in enumerate((("Cluster", self.nav_cluster), ("Namespace", self.nav_namespace), ("Deployment", self.nav_deployment), ("Pod", self.nav_pod))):
            grid.addWidget(QLabel(label), 0, column)
            combo.setMinimumWidth(80)
            combo.setMaximumWidth(280)
            grid.addWidget(combo, 1, column)
        previous_context = self.findChild(QWidget, "resourceContext")
        if previous_context:
            previous_context.show()
        status_key = "Статус" if self.kind == "pod" else "Pods"
        if status_key in self.context_values:
            grid.addWidget(QLabel(status_key), 0, 4)
            grid.addWidget(QLabel(self.context_values[status_key].text()), 1, 4)
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
        self.background.submit(lambda: self.api.namespaces(cluster), done, lambda message: self._navigation_failed(request, message))

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
                entries.append(("Without Deployment", ""))
            self._fill_navigation(self.nav_deployment, entries, selected)
            self._choose_navigation_deployment(navigate)
        self.background.submit(load, done, lambda message: self._navigation_failed(request, message))

    def _navigation_failed(self, request, message):
        if request == self._navigation_request:
            self._restore_navigation()
            self.nav_cluster.setToolTip(message)

    def _restore_navigation(self):
        self._navigation_pending = False
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
            elif self.nav_pod.currentData():
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
        self.controller.open_resource(cluster, kind, namespace, name, initial_tab=self.tabs.tabText(self.tabs.currentIndex()))

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

    def _summary(self, resource: dict) -> str:
        metadata, status = resource.get("metadata", {}), resource.get("status", {})
        values = {"Имя": metadata.get("name"), "Namespace": metadata.get("namespace"),
                  "Кластер": self.api.cluster(self.cluster_id).get("name"), "Создан": metadata.get("creationTimestamp"),
                  "Статус": status.get("phase"), "IP": status.get("podIP"), "Реплики": status.get("replicas"),
                  "Доступно": status.get("availableReplicas")}
        if self.kind == "deployment":
            desired = resource.get("spec", {}).get("replicas", 1)
            values.update({"Готово / нужно": f"{status.get('readyReplicas', 0)} / {desired}", "Обновлено": status.get("updatedReplicas", 0), "Недоступно": status.get("unavailableReplicas", 0), "Стратегия": resource.get("spec", {}).get("strategy", {}).get("type", "RollingUpdate")})
        elif self.kind == "pod":
            values.update({"Узел": resource.get("spec", {}).get("nodeName"), "Причина": status.get("reason"), "Сообщение": status.get("message")})
        text = "<h2>" + html.escape(self.name) + "</h2><table cellspacing='12'>"
        text += "".join(f"<tr><td>{label}</td><td><b>{html.escape(str(value))}</b></td></tr>" for label, value in values.items() if value is not None)
        text += "</table><h3>Labels</h3>"
        labels = metadata.get("labels", {})
        text += "<table cellspacing='8'>" + "".join(f"<tr><td>{html.escape(str(key))}</td><td><b>{html.escape(str(value))}</b></td></tr>" for key, value in sorted(labels.items())) + "</table>" if labels else "<p>No labels</p>"
        text += "<h3>Состояния</h3><table cellspacing='8'><tr><th align='left'>Проверка</th><th align='left'>Статус</th><th align='left'>Причина / сообщение</th><th align='left'>Изменено</th></tr>"
        for condition in status.get("conditions", []):
            title = {"Available": "Доступность", "Progressing": "Ход обновления", "Ready": "Готов к трафику", "ContainersReady": "Контейнеры готовы", "PodScheduled": "Назначен узел", "ReplicaFailure": "Ошибка создания реплик"}.get(condition.get("type"), condition.get("type", ""))
            state = {"True": "Да", "False": "Нет", "Unknown": "Unknown"}.get(str(condition.get("status")), str(condition.get("status", "")))
            text += "<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in (title, state, " / ".join(filter(None, (condition.get("reason"), condition.get("message")))), condition.get("lastTransitionTime", ""))) + "</tr>"
        text += "</table>"
        return text

    def _controlled(self) -> bool:
        return any(owner.get("controller") for owner in self.resource.get("metadata", {}).get("ownerReferences", []))

    def _pod_tabs(self):
        container_names = [c.get("name", "") for key in ("containers", "initContainers", "ephemeralContainers") for c in self.resource.get("spec", {}).get(key, [])]
        panel = QWidget()
        layout = QVBoxLayout(panel)
        row = QGridLayout()
        self.container = QComboBox()
        self.container.addItems(container_names)
        row.addWidget(self.container, 0, 0)
        self.previous = FullCheckBox("Логи до перезапуска")
        row.addWidget(self.previous, 0, 1)
        export = QPushButton("Экспорт")
        export.clicked.connect(self.export_logs)
        row.addWidget(export, 0, 2)
        self.log_tail = QSpinBox()
        self.log_tail.setRange(1, 5000)
        self.log_tail.setValue(1000)
        row.addWidget(QLabel("Строк"), 1, 0)
        row.addWidget(self.log_tail, 1, 1)
        self.all_logs = FullCheckBox("Последние доступные логи (до 4 МиБ)")
        row.addWidget(self.all_logs, 1, 2)
        self.auto_logs = FullCheckBox("Автообновление")
        self.auto_logs.setChecked(True)
        row.addWidget(self.auto_logs, 2, 0, 1, 3)
        layout.addLayout(row)
        self._updating_logs = False
        self._logs_first_load = True
        self._logs_opened = False
        self._log_request = 0
        self.log_search = QLineEdit()
        self.log_search.setPlaceholderText("Поиск в логах")
        layout.addWidget(self.log_search)
        self.logs = QTextEdit()
        self.logs.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.logs.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.logs.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.logs.setObjectName("podLogs")
        self.logs.setReadOnly(True)
        self.logs.viewport().installEventFilter(self)
        self.logs.installEventFilter(self)
        self.logs.verticalScrollBar().actionTriggered.connect(lambda *_: self.auto_logs.setChecked(False))
        self.logs.verticalScrollBar().sliderPressed.connect(lambda: self.auto_logs.setChecked(False))
        self.log_search.textEdited.connect(lambda *_: self.auto_logs.setChecked(False))
        self.log_search.textChanged.connect(lambda text: self.logs.find(text) if text else self.logs.moveCursor(self.logs.textCursor().MoveOperation.Start))
        layout.addWidget(self.logs)
        self.tabs.addTab(panel, "Логи")
        self.log_timer = QTimer(self)
        self.log_timer.timeout.connect(lambda: self.load_logs() if self.auto_logs.isChecked() and self.tabs.currentWidget() == panel else None)
        self.log_timer.start(5000)
        self.container.currentTextChanged.connect(self.load_logs)
        self.previous.toggled.connect(self.load_logs)
        self.log_tail.valueChanged.connect(self.load_logs)
        self.all_logs.toggled.connect(lambda enabled: (self.log_tail.setEnabled(not enabled), self.load_logs()))
        self.auto_logs.toggled.connect(lambda enabled: self.load_logs() if enabled else None)
        QTimer.singleShot(0, self.load_logs)
        terminal = QWidget()
        terminal_layout = QVBoxLayout(terminal)
        console_container = QComboBox()
        console_container.addItems(container_names)
        console_container.currentTextChanged.connect(self.container.setCurrentText)
        self.container.currentTextChanged.connect(console_container.setCurrentText)
        terminal_layout.addWidget(console_container)
        self.console_output = BoundedConsole()
        self.console_output.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.console_output.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.console_output.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.console_output.setObjectName("podConsole")
        self.container.currentTextChanged.connect(self.lock_console)
        self.console_output.setReadOnly(True)
        terminal_layout.addWidget(self.console_output)
        console_row = QHBoxLayout()
        self.console_input = QLineEdit()
        self.console_input.setPlaceholderText("/bin/sh -lc команда")
        self.console_input.returnPressed.connect(self.run_console)
        console_row.addWidget(self.console_input)
        execute = QPushButton("Выполнить")
        execute.clicked.connect(self.run_console)
        console_row.addWidget(execute)
        self.console_lock = QPushButton("🔒")
        self.console_lock.setToolTip("Заблокировать консоль")
        self.console_lock.clicked.connect(self.lock_console)
        console_row.addWidget(self.console_lock)
        terminal_layout.addLayout(console_row)
        self.tabs.addTab(terminal, "Консоль Pod")
        self.console_unlocked_until = 0.0
        self._info_tab("События", lambda: self.api.events(self.cluster_id, self.namespace, "Pod", self.name, self.resource.get("metadata", {}).get("uid")))
        self.tabs.addTab(MetricsWidget(self, self.api, self.background, self.cluster_id, self.namespace, self.name), "Метрики")
        self._info_tab("Контейнеры", lambda: self.resource.get("spec", {}).get("containers", []))
        self.tabs.setCurrentIndex(next(i for i in range(self.tabs.count()) if self.tabs.tabText(i) == "Логи"))

    def lock_console(self):
        self.console_unlocked_until = 0.0
        self.console_lock.setText("🔒")

    def run_console(self):
        command = self.console_input.text().strip()
        container = self.container.currentText()
        if not command or not container:
            return
        if time.monotonic() >= self.console_unlocked_until:
            if not confirm(self, "Разблокировка консоли Pod", f"Выполнить первую команду в {self.target_label()} ({container})?\nПосле подтверждения консоль откроется на 5 минут простоя.\n\n{command[:500]}"):
                return
        self.console_unlocked_until = time.monotonic() + 300
        self.console_lock.setText("🔓")
        self.console_input.clear()
        self.console_output.append(f"{container}@{self.name}:~$ {command}")
        def execute():
            cluster = self.api.cluster(self.cluster_id)
            if not cluster.get("enabled"):
                raise PermissionError("Кластер отключён.")
            current = self.api.resource(self.cluster_id, "pod", self.namespace, self.name)
            # The REST read may renew an expired token; use that committed login.
            cluster = self.api.cluster(self.cluster_id)
            if not cluster.get("enabled"):
                raise PermissionError("Кластер отключён.")
            uid = self.resource.get("metadata", {}).get("uid")
            check_identity(current, uid)
            with self.api.store.operation("exec", self.cluster_id, self.namespace, self.name, uid=uid, container=container):
                return self.api.proxy.execute(cluster, self.namespace, self.name, container, command)
        self._mutate(execute,
                     lambda result: self.console_output.append("\n".join(part for part in result if part) or "(нет вывода)"),
                     lambda message: self.console_output.append(f"Ошибка: {message}\nИсход удалённой команды может быть неизвестен. Проверьте состояние ресурса и процессов перед повтором."))

    def _deployment_tabs(self):
        from PyQt6.QtWidgets import QHeaderView
        self.pod_table = ResourceList()
        table = self.pod_table
        table.setColumnCount(7)
        table.setHorizontalHeaderLabels(["Статус", "Имя", "Возраст", "IP", "Рестарты", "CPU", "RAM"])
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.setItemDelegateForColumn(0, StatusDelegate(table))
        table.itemClicked.connect(lambda entry, column: self._open_deployment_pod(table.indexOfTopLevelItem(entry), column))
        self.tabs.addTab(table, "Pods")
        self.deployment_notice = QLabel("")
        self.deployment_notice.setWordWrap(True)
        self.deployment_notice.setObjectName("deploymentNotice")
        self.deployment_notice.hide()
        table.setToolTip("Нажмите, чтобы открыть Pod")
        self.layout().insertWidget(2, self.deployment_notice)
        self._pods_loading = False
        self._pod_metrics_loading = False
        self._pod_metrics = {}
        self._displayed_pods = []
        self.deployment_timer = QTimer(self)
        self.deployment_timer.timeout.connect(lambda: self.reload_deployment_pods() if self.isVisible() else None)
        self.deployment_timer.start(5000)
        self.reload_deployment_pods()
        self._info_tab("События", lambda: self.api.events(self.cluster_id, self.namespace, "Deployment", self.name, self.resource.get("metadata", {}).get("uid")))
        self._info_tab("Метрики", lambda: {"pods": []})

    def _open_deployment_pod(self, row, column):
        cell = self.pod_table.item(row, 1)
        if cell is not None:
            name, uid = cell.data(Qt.ItemDataRole.UserRole)
            self.controller.open_resource(self.cluster_id, "pod", self.namespace, name, expected_uid=uid)

    def reload_deployment_pods(self):
        if self._pods_loading or self.pending_operation:
            return
        self._pods_loading = True
        deployment_uid = self.resource.get("metadata", {}).get("uid")
        def load():
            current = self.api.resource(self.cluster_id, "deployment", self.namespace, self.name)
            if current.get("metadata", {}).get("uid") != deployment_uid:
                raise ResourceNotFoundError("Deployment был заменён. Откройте его заново из матрицы.")
            pods = self.api.pods(self.cluster_id, self.namespace)
            sets = self.api.replica_sets(self.cluster_id, self.namespace)
            owned = {rs.get("metadata", {}).get("uid") for rs in sets if any(o.get("uid") == deployment_uid and o.get("controller") for o in rs.get("metadata", {}).get("ownerReferences", []))}
            owned.discard(None)
            pods = [p for p in pods if deployment_uid and any(o.get("controller") and (o.get("uid") in owned or o.get("uid") == deployment_uid) for o in p.get("metadata", {}).get("ownerReferences", []))]
            return current, pods
        def done(result):
            self._pods_loading = False
            current, pods = result
            self._adopt_live(current)
            self.context_values["Pods"].setText(f"{current.get('status', {}).get('availableReplicas', 0)}/{current.get('spec', {}).get('replicas', 1)}")
            self._displayed_pods = pods
            if not pods:
                self._pod_metrics = {}
                if hasattr(self, "_deployment_metrics_display"):
                    self._deployment_metrics_display({"pods": []})
            table = self.pod_table
            selected = table.item(table.currentRow(), 1)
            selected_identity = selected.data(Qt.ItemDataRole.UserRole) if selected else None
            table.setRowCount(len(pods))
            for row, pod in enumerate(pods):
                meta, status = pod.get("metadata", {}), pod.get("status", {})
                identity = (meta.get("name", ""), meta.get("uid", ""))
                values = (pod_status(pod), identity[0], resource_age(meta.get("creationTimestamp")), status.get("podIP", "—"),
                          sum(c.get("restartCount", 0) for c in status.get("containerStatuses", [])), "—", "—")
                for col, value in enumerate(values):
                    table.setItem(row, col, item(value))
                table.item(row, 1).setData(Qt.ItemDataRole.UserRole, identity)
                table.item(row, 2).setToolTip(meta.get("creationTimestamp", ""))
                if identity == selected_identity:
                    table.selectRow(row)
            table.resizeColumnsToContents()
            table.setColumnWidth(0, max(92, table.columnWidth(0)))
            self._apply_pod_metrics()
            if not self._pod_metrics_loading and pods:
                self._pod_metrics_loading = True
                def metrics():
                    def sample(pod):
                        meta = pod["metadata"]
                        identity = (meta["name"], meta.get("uid", ""))
                        try:
                            return identity, self.api.metrics(self.cluster_id, self.namespace, meta["name"])
                        except Exception as exc:
                            return identity, {"available": False, "error": str(exc)}
                    with ThreadPoolExecutor(max_workers=4) as executor:
                        return dict(executor.map(sample, pods))
                def show_metrics(samples):
                    self._pod_metrics_loading = False
                    self._pod_metrics = samples
                    self._apply_pod_metrics()
                    if hasattr(self, "_deployment_metrics_display"):
                        self._deployment_metrics_display({"pods": [dict(value, pod=identity[0]) for identity, value in samples.items()]})
                def metrics_failed(message):
                    self._pod_metrics_loading = False
                self.background.submit(metrics, show_metrics, metrics_failed)
        def failed(message):
            self._pods_loading = False
            self.deployment_notice.setText(message)
            self.deployment_notice.show()
        self.background.submit(load, done, failed)

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
        table.setWordWrap(True)
        self.tabs.addTab(table, label)
        def display(value):
            if label == "События":
                headings = ["Тип", "Причина", "Сообщение", "Источник", "Дата", "Кол-во"]
                rows = [[e.get("type"), e.get("reason"), e.get("message"), (e.get("source") or {}).get("component"), e.get("lastTimestamp") or e.get("metadata", {}).get("creationTimestamp"), e.get("count")] for e in value]
            elif label == "Контейнеры":
                headings = ["Имя", "Образ", "Порты", "CPU request / limit", "RAM request / limit"]
                rows = []
                for c in value:
                    r = c.get("resources", {})
                    rows.append([c.get("name"), c.get("image"), ", ".join(str(p.get("containerPort")) for p in c.get("ports", [])),
                                 " / ".join(str(r.get(k, {}).get("cpu", "—")) for k in ("requests", "limits")),
                                 " / ".join(str(r.get(k, {}).get("memory", "—")) for k in ("requests", "limits"))])
            else:
                headings = ["Pod", "Контейнер", "CPU (cores)", "RAM (bytes)", "Источник"]
                rows = [[m.get("pod", ""), c.get("name"), c.get("cpu", {}).get("usageCores"), c.get("memory", {}).get("usageBytes"), m.get("source")] for m in value.get("pods", []) for c in m.get("containers", [])]
            table.setColumnCount(len(headings))
            table.setHorizontalHeaderLabels(headings)
            table.setRowCount(len(rows))
            for row, values in enumerate(rows):
                for column, value in enumerate(values):
                    table.setItem(row, column, item(value))
            table.resizeColumnsToContents()
        if label == "Метрики" and self.kind == "deployment":
            self._deployment_metrics_display = display
            display({"pods": [dict(value, pod=identity[0]) for identity, value in self._pod_metrics.items()]})
        else:
            def refresh_info():
                if getattr(table, "_loading", False):
                    return
                table._loading = True
                def done(value):
                    table._loading = False
                    display(value)
                def failed(message):
                    table._loading = False
                    self._info_failure(table, message)
                self.background.submit(loader, done, failed)
            self.tabs.currentChanged.connect(lambda _: refresh_info() if self.tabs.currentWidget() is table else None)
            refresh_info()
            timer = QTimer(table)
            timer.timeout.connect(lambda: refresh_info() if table.isVisible() else None)
            timer.start(10000)

    def _info_failure(self, table, message):
        table.setColumnCount(1)
        table.setHeaderLabels(["Диагностика недоступна"])
        table.setRowCount(1)
        table.setItem(0, 0, item(message))

    def eventFilter(self, watched, event):
        if hasattr(self, "logs"):
            if watched is self.logs.viewport() and event.type() in (QEvent.Type.Wheel, QEvent.Type.TouchBegin):
                self.auto_logs.setChecked(False)
            elif watched is self.logs:
                if event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown, Qt.Key.Key_Home, Qt.Key.Key_End):
                    self.auto_logs.setChecked(False)
                elif event.type() == QEvent.Type.Show:
                    QTimer.singleShot(0, self._follow_pod_logs)
        return super().eventFilter(watched, event)

    def _follow_pod_logs(self):
        if self.auto_logs.isChecked() or not self._logs_opened:
            self.logs.verticalScrollBar().setValue(self.logs.verticalScrollBar().maximum())
        self._logs_opened = True

    def load_logs(self, *_):
        if getattr(self, "_logs_loading", False):
            self._logs_again = True
            self._log_request += 1
            return
        self._logs_loading = True
        self._logs_again = False
        container, previous = self.container.currentText(), self.previous.isChecked()
        tail = None if self.all_logs.isChecked() else self.log_tail.value()
        self._log_request += 1
        request = self._log_request
        def display(text):
            self._logs_loading = False
            if self._logs_again:
                QTimer.singleShot(0, self.load_logs)
            if request != self._log_request:
                return
            if hasattr(self, "log_error"):
                self.log_error.clear()
            scroll = self.logs.verticalScrollBar()
            position = scroll.value()
            self._updating_logs = True
            self.logs.setPlainText(text)
            scroll.setValue(scroll.maximum() if self.auto_logs.isChecked() or self._logs_first_load else position)
            self._logs_first_load = False
            self._updating_logs = False
        def failed(message):
            self._logs_loading = False
            self.logs.setToolTip("Ошибка чтения: " + message)
            error_label = getattr(self, "log_error", None)
            if error_label is None:
                self.log_error = QLabel(self.logs.parentWidget())
                self.logs.parentWidget().layout().insertWidget(0, self.log_error)
            self.log_error.setText("Логи недоступны: " + message)
            if self._logs_again:
                QTimer.singleShot(0, self.load_logs)
        self.background.submit(lambda: self.api.logs(self.cluster_id, self.namespace, self.name, container, previous, tail), display, failed)

    def export_logs(self):
        path, _ = QFileDialog.getSaveFileName(self, "Экспорт логов", f"{self.name}.log", "Text (*.log *.txt)")
        if path:
            try:
                Path(path).write_text(self.logs.toPlainText(), encoding="utf-8")
            except OSError as exc:
                error(self, str(exc))

    def target_label(self):
        return f"{self.api.cluster(self.cluster_id)['name']} / {self.namespace} / {self.name}"

    def save_yaml(self):
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
        before = yaml.safe_dump(self.resource, allow_unicode=True, sort_keys=False).splitlines(keepends=True)
        after = yaml.safe_dump(edited, allow_unicode=True, sort_keys=False).splitlines(keepends=True)
        diff = "".join(difflib.unified_diff(before, after, fromfile="Текущий объект", tofile="После изменения"))
        if not diff:
            QMessageBox.information(self, "Quaber", "Изменений нет.")
            return
        run_dialog(TextDialog(self, "Предварительный просмотр изменений", diff))
        if edited.get("spec") != self.resource.get("spec") or self.kind == "configmap":
            typed, accepted = QInputDialog.getText(self, "Изменение спецификации", f"{self.target_label()}\nИзменения spec могут затронуть реплики, image, command, volumes, serviceAccount, securityContext и доступность.\nВведите точную цель: {self.target_label()}")
            if not accepted or typed != self.target_label():
                return
        if not confirm(self, "Сохранить изменения", f"Изменить {self.kind} {self.target_label()}?\nБудет проверен resourceVersion объекта."):
            return
        self._mutate(lambda: self.api.replace(self.cluster_id, self.kind, self.namespace, self.name, edited, self.version), self._updated)

    def _mutate(self, function, done, failed=None):
        if self.pending_operation or getattr(self, "_navigation_pending", False):
            return
        if hasattr(self.controller, "store") and getattr(self.controller, "preferences", {}).get("read_only", True):
            error(self, "Включён режим только чтения. Разрешите изменения явно в настройках.")
            return
        self.pending_operation = True
        def finish(value):
            self.pending_operation = False
            done(value)
        def on_error(message):
            self.pending_operation = False
            (failed or (lambda value: error(self, value)))(message)
        def guarded_operation():
            if hasattr(self.controller, "store") and getattr(self.controller, "preferences", {}).get("read_only", True):
                raise PermissionError("Режим только чтения включён до начала операции.")
            return function()
        self.background.submit(guarded_operation, finish, on_error, priority=True)

    def _yaml_changed(self):
        self._allow_discard = False

    def can_leave(self):
        if self.pending_operation:
            return False
        if self.yaml_editor.editor.document().isModified() and not self._allow_discard:
            answer = QMessageBox.warning(self, "Несохранённый YAML", "В YAML есть несохранённые изменения. Отмените переход и сохраните YAML либо отбросьте правки.", QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Discard:
                return False
            self._allow_discard = True
        return True

    def closeEvent(self, event):
        if not self.can_leave():
            event.ignore()
            return
        self._stop_panel()
        super().closeEvent(event)

    def _stop_panel(self):
        self.background.active = False
        for timer in self.findChildren(QTimer):
            timer.stop()

    def reject(self):
        if self.can_leave():
            self._stop_panel()
            super().reject()

    def refresh_object(self):
        if self.pending_operation or getattr(self, "_object_loading", False):
            return
        self._object_loading = True
        def done(resource):
            self._object_loading = False
            try:
                check_identity(resource, self.resource.get("metadata", {}).get("uid"))
                self._adopt_live(resource)
            except RuntimeError as exc:
                error(self, str(exc))
        def failed(message):
            self._object_loading = False
            error(self, message)
        self.background.submit(lambda: self.api.resource(self.cluster_id, self.kind, self.namespace, self.name), done, failed)

    def _adopt_live(self, resource):
        self.live_resource = resource
        self.details.setHtml(self._summary(resource))
        if self.yaml_editor.editor.document().isModified():
            if resource.get("metadata", {}).get("resourceVersion") != self.version:
                self.setToolTip("Серверный объект обновился. YAML содержит локальные правки; основание сохранено, перед записью потребуется согласование версии.")
            return
        self.resource = resource
        self.version = resource.get("metadata", {}).get("resourceVersion", "")
        self.yaml_editor.setPlainText(yaml.safe_dump(resource, allow_unicode=True, sort_keys=False))
        self.yaml_editor.editor.document().setModified(False)

    def _updated(self, resource):
        self.api.cache.invalidate() if hasattr(self.api, "cache") else None
        self.details.setHtml(self._summary(resource))
        self.resource = resource
        self.version = resource.get("metadata", {}).get("resourceVersion", "")
        self.yaml_editor.setPlainText(yaml.safe_dump(resource, allow_unicode=True, sort_keys=False))
        self.yaml_editor.editor.document().setModified(False)
        QMessageBox.information(self, "Quaber", "API принял изменение. Это не подтверждение готовности workload; проверьте состояние, Pod и события.")

    def scale(self):
        self.background.submit(lambda: self.api.scaling_info(self.cluster_id, self.namespace, self.name), self._scale_dialog)

    def _scale_dialog(self, hpa_info):
        current = self.resource.get("spec", {}).get("replicas", 0)
        dialog = QInputDialog(self)
        dialog.setWindowTitle("Реплики Deployment")
        dialog.setLabelText(f"{self.target_label()}\n{hpa_info}\nЧисло реплик")
        dialog.setInputMode(QInputDialog.InputMode.IntInput)
        dialog.setIntRange(0, 10000)
        dialog.setIntValue(current)
        spin = dialog.findChild(QSpinBox)
        if spin:
            spin.setProperty("replicaCount", True)
            spin.style().unpolish(spin)
            spin.style().polish(spin)
        accepted = run_dialog(dialog) == QDialog.DialogCode.Accepted
        value = dialog.intValue()
        if accepted and (value < current or value > max(current*2, current+10) or self.namespace.startswith("kube-")):
            typed, ok = QInputDialog.getText(self, "Опасное изменение реплик", f"{self.target_label()}\n{scale_summary(self.resource, value)}\nВведите точную цель: {self.target_label()}")
            if not ok or typed != self.target_label():
                return
        if accepted and confirm(self, "Масштабировать", f"{self.target_label()}\n{hpa_info}\n{scale_summary(self.resource, value)}"):
            self._mutate(lambda: self.api.scale(self.cluster_id, self.namespace, self.name, value, self.resource.get("metadata", {}).get("uid"), self.version), self._updated)

    def edit_resources(self):
        containers = self.resource.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        names = [container.get("name", "") for container in containers]
        if not names:
            error(self, "У Deployment нет контейнеров.")
            return
        name, accepted = QInputDialog.getItem(self, "Ресурсы контейнера", "Контейнер", names, 0, False)
        if not accepted:
            return
        source = next(container for container in containers if container.get("name") == name)
        original = source.get("resources", {})
        dialog = QDialog(self)
        dialog.setWindowTitle(f"CPU / RAM — {name}")
        form = QFormLayout(dialog)
        fields = {}
        for field, category, resource in (("CPU request", "requests", "cpu"), ("CPU limit", "limits", "cpu"),
                                           ("RAM request", "requests", "memory"), ("RAM limit", "limits", "memory")):
            edit = QLineEdit(original.get(category, {}).get(resource, ""))
            form.addRow(field, edit)
            fields[(category, resource)] = edit
        remove_limits = QCheckBox("Снять пустые лимиты (явное удаление ограничений)")
        form.addRow(remove_limits)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if run_dialog(dialog) != QDialog.DialogCode.Accepted:
            return
        edited = copy.deepcopy(self.resource)
        target = next(container for container in edited["spec"]["template"]["spec"]["containers"] if container.get("name") == name)
        try:
            resources = resource_changes(original, {key: edit.text() for key, edit in fields.items()}, remove_limits.isChecked())
            target["resources"] = resources
        except ValueError as exc:
            error(self, str(exc))
            return
        before = yaml.safe_dump(original, allow_unicode=True, sort_keys=False).splitlines(keepends=True)
        after = yaml.safe_dump(resources, allow_unicode=True, sort_keys=False).splitlines(keepends=True)
        diff = "".join(difflib.unified_diff(before, after, fromfile="До", tofile="После"))
        if not diff:
            return
        run_dialog(TextDialog(self, "Предварительный просмотр CPU / RAM", diff + "\nИзменение шаблона Pod запустит rollout Deployment; возможно изменение QoS и доступности."))
        if not confirm(self, "Изменение ресурсов", f"{self.target_label()}\nПрименить новые ресурсы контейнера {name} и запустить rollout?\nresourceVersion будет проверен."):
            return
        self._mutate(lambda: self.api.replace(self.cluster_id, "deployment", self.namespace, self.name, edited, self.version), self._updated)

    def restart(self):
        if confirm(self, "Restart Deployment", f"Запустить rollout Deployment {self.target_label()}?\nСтратегия: {self.resource.get('spec', {}).get('strategy', {})}\nГотово: {self.resource.get('status', {}).get('readyReplicas', 0)} / {self.resource.get('spec', {}).get('replicas', 1)}\nRecreate или единственная реплика могут привести к полному перерыву. API dry-run не проверяет будущую доступность."):
            self._mutate(lambda: self.api.restart(self.cluster_id, self.namespace, self.name, self.resource.get("metadata", {}).get("uid"), self.version), self._updated)

    def delete_pod(self):
        mode = "restart" if self._controlled() else "delete"
        if confirm(self, "Подтверждение", f"{('Выселить с проверкой PDB' if mode == 'restart' else 'Окончательно удалить')} Pod {self.target_label()}?\nВладелец: {next((o.get('kind') for o in self.resource.get('metadata', {}).get('ownerReferences', []) if o.get('controller')), 'нет')}. Восстановление не гарантируется.\nUID будет проверен перед действием."):
            uid = self.resource.get("metadata", {}).get("uid", "")
            self._mutate(lambda: self.api.delete_pod(self.cluster_id, self.namespace, self.name, uid, mode), lambda _: self.accept())


class MainWindow(QMainWindow):
    authorization_needed = pyqtSignal(str)

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
        try:
            executable = locate_kubectl(app_dir, store.preferences().get("kubectl_path", ""))
        except FileNotFoundError:
            executable = None
        # executable above controls only the missing-client hint. The manager
        # resolves the current global setting when starting each client.
        self.proxy = ProxyManager(None, store)
        self.api = Kubernetes(store, self.proxy)
        self.api.authorization_required = self.authorization_needed.emit
        self.authorization_needed.connect(self._on_authorization_needed, Qt.ConnectionType.QueuedConnection)
        self._unauthorized_clusters = set()
        self._auth_dismissed = {}
        self._auth_busy = False
        self.preferences = store.preferences()
        self.preferences["read_only"] = True
        self.preference_writer = PreferenceWriter(store, self)
        self.preference_writer.failed.connect(lambda message: self.statusBar().showMessage(message), Qt.ConnectionType.QueuedConnection)
        self.views = MatrixViews(self.preferences)
        self.setWindowTitle("Quaber")
        screen = QApplication.primaryScreen()
        if screen:
            available = screen.availableGeometry()
            self.resize(min(1450, max(700, available.width() - 80)), min(900, max(550, available.height() - 80)))
        else:
            self.resize(1450, 900)
        self._closing = False
        root = QWidget()
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.sidebar = QWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setMinimumWidth(160)
        self.sidebar.setMaximumWidth(280)
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(0, 6, 0, 6)
        side.setSpacing(2)
        brand = QLabel("Quaber")
        brand.setObjectName("brand")
        side.addWidget(brand)
        self.nav_group = QButtonGroup(self)
        self.nav_buttons = []
        for index, label in enumerate(("Deployment и их Pod", "Квоты ресурсов", "ConfigMaps")):
            button = QPushButton(label)
            button.setObjectName("sideNav")
            button.setCheckable(True)
            button.setChecked(index == 0)
            button.clicked.connect(lambda checked=False, i=index: self.tabs.setCurrentIndex(i))
            self.nav_group.addButton(button)
            self.nav_buttons.append(button)
            side.addWidget(button)
        side.addStretch(1)
        settings = QPushButton("Настройки")
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
        self.tabs.addTab(self.matrix_page, "Deployment и их Pod")
        self._setup_matrix()
        self.quotas_page = self._resource_page("quota", "Квоты ресурсов")
        self.configmaps_page = self._resource_page("configmap", "ConfigMaps")
        self.allpods_page = self._resource_page("pod", "Все Pod")
        allpods_button = QPushButton("Все Pod")
        allpods_button.setObjectName("sideNav")
        allpods_button.setCheckable(True)
        allpods_button.clicked.connect(lambda: self.tabs.setCurrentWidget(self.allpods_page))
        self.nav_group.addButton(allpods_button)
        self.nav_buttons.append(allpods_button)
        side.insertWidget(side.count()-2, allpods_button)
        self.tabs.tabBar().hide()
        self._active_workspace_index = 0
        self.tabs.currentChanged.connect(lambda index: self.nav_buttons[index].setChecked(True))
        self.tabs.currentChanged.connect(self._navigation_changed)
        self.shell_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.shell_splitter.setObjectName("shellSplitter")
        self.shell_splitter.setChildrenCollapsible(False)
        self.shell_splitter.setHandleWidth(3)
        self.shell_splitter.addWidget(self.sidebar)
        self.shell_splitter.addWidget(self.workspace)
        self.shell_splitter.setStretchFactor(0, 0)
        self.shell_splitter.setStretchFactor(1, 1)
        self.shell_splitter.setSizes([self.preferences.get("sidebar_width", 190), 1200])
        layout.addWidget(self.shell_splitter)
        self.nav_resize_timer = QTimer(self)
        self.nav_resize_timer.setSingleShot(True)
        self.nav_resize_timer.setInterval(250)
        self.nav_resize_timer.timeout.connect(self._remember_sidebar_width)
        self.shell_splitter.splitterMoved.connect(lambda *_: (self.nav_resize_timer.start(), self._position_resource_panel()))
        self._setup_log_panel()
        self.statusBar().showMessage("Готово")
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        interval = self.preferences.get("refresh_ms", 10000)
        if interval:
            self.timer.start(interval)
        self._apply_theme()
        QTimer.singleShot(0, self.refresh)
        QTimer.singleShot(1000, self._prompt_expired_login)
        self.auth_timer = QTimer(self)
        self.auth_timer.timeout.connect(self._prompt_expired_login)
        self.auth_timer.start(15000)

    def _setup_log_panel(self):
        self.log_panel = LogPanel(self)
        button = QPushButton("Журнал")
        button.setObjectName("sideNav")
        button.setCheckable(True)
        self.nav_group.addButton(button)
        self.nav_buttons.append(button)
        self.tabs.addTab(self.log_panel, "Журнал")
        button.clicked.connect(lambda: self.tabs.setCurrentWidget(self.log_panel))
        button.setToolTip("Ctrl+J")
        self.sidebar.layout().insertWidget(self.sidebar.layout().count()-2, button)
        self.log_action = QAction("Журнал", self)
        self.log_action.setShortcut(QKeySequence("Ctrl+J"))
        self.log_action.triggered.connect(lambda: self.tabs.setCurrentWidget(self.log_panel))
        self.addAction(self.log_action)
        self.log_session = LogSession(self.store.root, self, console=bool(getattr(QApplication.instance(), "quaber_console_logging", False)))
        self.log_session.bridge.message.connect(self.log_panel.append, Qt.ConnectionType.QueuedConnection)
        self.log_session.logger.info("Quaber запущен")
        if self.log_session.file_error:
            self.log_panel.append("ERROR  Не удалось открыть файл журнала; сообщения доступны в этой панели.")

    def _remember_log_visibility(self, visible):
        if self.isVisible() and not self._closing:
            self.preferences["log_visible"] = visible
            self._save_preferences()

    def _remember_sidebar_width(self):
        self.preferences["sidebar_width"] = self.shell_splitter.sizes()[0]
        self._save_preferences()

    def _select_theme(self, value):
        self.preferences["theme"] = value
        self._save_preferences()
        self._apply_theme()
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

    def _navigation_changed(self, index):
        if not self._close_resource_stack():
            blocked = self.tabs.blockSignals(True)
            self.tabs.setCurrentIndex(self._active_workspace_index)
            self.tabs.blockSignals(blocked)
            self.nav_buttons[self._active_workspace_index].setChecked(True)
            return
        self._resource_request = getattr(self, "_resource_request", 0) + 1
        self._active_workspace_index = index
        logger.info("Раздел: %s", self.tabs.tabText(index))
        self.refresh()

    @pyqtSlot(str)
    def _on_authorization_needed(self, identifier):
        if self._closing:
            return
        self._unauthorized_clusters.add(identifier)
        if not self._auth_busy:
            QTimer.singleShot(0, self._prompt_expired_login)

    def _prompt_expired_login(self):
        if self._closing or self._auth_busy:
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
            for cluster in clusters:
                if cluster["id"] not in unresolved:
                    self._unauthorized_clusters.discard(cluster["id"])
            remaining = [c for c in self.store.clusters() if c["id"] in unresolved and c["enabled"]]
            if remaining:
                groups = {}
                for cluster in remaining:
                    key = (cluster["server"], cluster["username"])
                    groups.setdefault(key, {"username": cluster["username"], "clusters": []})["clusters"].append(cluster)
                dialog = LoginGroupsDialog(self, list(groups.values()))
                accepted = run_dialog(dialog) == QDialog.DialogCode.Accepted
                for cluster in remaining:
                    if accepted:
                        self._unauthorized_clusters.discard(cluster["id"])
                        self._auth_dismissed.pop(cluster["id"], None)
                    else:
                        self._auth_dismissed[cluster["id"]] = self.api.authorization.signature(cluster)
            self._auth_busy = False
            self.statusBar().showMessage("Готово")
            self.refresh()
        def failed(message):
            self._auth_busy = False
            self.statusBar().showMessage(message)
        self.background.submit(renew, done, failed, priority=True)

    @staticmethod
    def _scroll_controls(widget):
        scroll = QScrollArea()
        scroll.setWidget(widget)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFixedHeight(widget.sizeHint().height()+16)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        return scroll

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
        for label, handler in (("Экспорт", self._export_views), ("Импорт", self._import_views)):
            button = QPushButton(label)
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
        self.recent = FullCheckBox("Рестарты < 1 ч")
        self.recent.setObjectName("recentFilter")
        self.recent.setToolTip("Добавлять Pod с рестартами за последний час, включая скрытые статусы")
        self.recent.setChecked(self.preferences.get("recent", True))
        self.recent.stateChanged.connect(self.render_matrix)
        for status in ("Running", "Pending", "Failed", "Unknown"):
            checkbox = FullCheckBox("Error" if status == "Failed" else status)
            checkbox.setObjectName("statusFilter")
            checkbox.setProperty("status", status)
            checkbox.setChecked(self.preferences.get("filters", {}).get(status, True))
            checkbox.stateChanged.connect(self.render_matrix)
            controls.addWidget(checkbox)
            self.status_filters[status] = checkbox
            if status == "Running":
                controls.addWidget(self.recent)
        self.problem_only = FullCheckBox("Только проблемы")
        self.problem_only.stateChanged.connect(self.render_matrix)
        controls.addWidget(self.problem_only)
        self.hide_empty = FullCheckBox("Скрыть пустые")
        self.hide_empty.stateChanged.connect(self.render_matrix)
        controls.addWidget(self.hide_empty)
        self.summary_label = QLabel("0 Total")
        self.summary_label.setObjectName("totalCounter")
        controls.addWidget(self.summary_label)
        controls.addStretch(1)
        layout.addWidget(self._scroll_controls(status_strip))
        refresh_strip = QWidget()
        refresh_strip.setObjectName("refreshStrip")
        refresh_row = QHBoxLayout(refresh_strip)
        refresh_row.setContentsMargins(12, 3, 12, 3)
        refresh_row.addStretch(1)
        self.last_updated = QLabel("Последнее обновление: —")
        self.last_updated.setObjectName("lastUpdated")
        refresh_row.addWidget(self.last_updated)
        refresh = QPushButton("Обновить")
        refresh.setProperty("primary", True)
        refresh.clicked.connect(self.refresh)
        refresh_row.addWidget(refresh)
        refresh_row.addWidget(QLabel("Автообновление"))
        self.refresh_interval = QComboBox()
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
        for title, key in (("Deployment", "selectedDeployments"), ("Namespace", "selectedNamespaces"), ("Кластер", "selectedClusterIds")):
            scope = QPushButton(title + ": Все ▾")
            scope.setObjectName("scopeFilter")
            scope.clicked.connect(lambda checked=False, title=title, key=key: self._scope_menu(title, key))
            toolbar_row.addWidget(scope)
            self.scope_buttons[key] = (title, scope)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Найти deployment…")
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
        self.matrix.verticalHeader().sectionResized.connect(self._row_resized)
        layout.addWidget(self.matrix, 1)
        layout.addWidget(refresh_strip)
        self.matrix_data: dict[tuple[int, int], tuple] = {}
        self.snapshot: dict = {}
        self._row_names = []
        self._columns = []
        self._load_view_controls()

    def _save_view_controls(self):
        if getattr(self, "_loading_view", False):
            return
        view = self.views.active
        view["filters"] = {key: box.isChecked() for key, box in self.status_filters.items()}
        view["recentRestartFilter"] = self.recent.isChecked()
        view["problemOnly"] = self.problem_only.isChecked()
        view["hideFilteredNamespaces"] = self.hide_empty.isChecked()
        self.views.persist()
        self._save_preferences()

    def _load_view_controls(self):
        view = self.views.active
        self._loading_view = True
        for status, box in self.status_filters.items():
            box.setChecked(view.get("filters", {}).get(status, True))
        self.recent.setChecked(view.get("recentRestartFilter", True))
        self.problem_only.setChecked(view.get("problemOnly", False))
        self.hide_empty.setChecked(view.get("hideFilteredNamespaces", True))
        self._loading_view = False
        color = view.get("color", "transparent")
        self._apply_view_color(color)
        self.render_matrix()

    def _apply_view_color(self, color):
        light = self.preferences.get("theme") == "light"
        colors = theme_colors(light)
        background = blended_color(colors["editor"], color)
        self.matrix_page.setStyleSheet(
            f"QWidget#matrixPage, QWidget#statusStrip, QWidget#filterStrip, QWidget#filterStrip QPushButton, QWidget#statusStrip QCheckBox, QTableWidget#podMatrix, "
            f"QWidget#matrixCanvas, QTableWidget#podMatrix QHeaderView, "
            f"QTableWidget#podMatrix QHeaderView QWidget {{background:{background};}}"
            f"QTableWidget#podMatrix QHeaderView::section, QTableCornerButton::section "
            f"{{background:{background};border:1px solid {colors['border']};}}")
        self.matrix_page.setStyleSheet(self.matrix_page.styleSheet() +
            f"QWidget#statusStrip QCheckBox:hover, QWidget#filterStrip QPushButton:hover {{background:{blended_color(background, colors['accent'], .15)};}}")
        self.matrix.setProperty("viewColor", color)
        self.matrix.frozen.setStyleSheet(f"QTableView#frozenDeployment {{background:{background};}}")
        palette = self.matrix.palette()
        from PyQt6.QtGui import QPalette
        palette.setColor(QPalette.ColorRole.Base, QColor(background))
        self.matrix.setPalette(palette)
        self.view_bar.colors = [view.get("color", "transparent") for view in self.views.views]
        self.view_bar.light = light
        self.view_bar.update()
        self.matrix.viewport().update()
        self.matrix.horizontalHeader().viewport().update()

    def _set_view_color(self, color):
        self.views.active["color"] = color
        self.views.persist()
        self._save_preferences()
        self._apply_view_color(color)

    def _switch_view(self, index):
        if 0 <= index < len(self.views.views):
            self.views.active_id = self.views.views[index]["id"]
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
        menu.exec(self.view_bar.mapToGlobal(point))

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
        from PyQt6.QtGui import QCursor
        if key == "selectedDeployments":
            values = sorted({d.get("metadata", {}).get("name", "") for c in self.snapshot.values() for r in c["namespaces"].values() for d in r["deployments"]})
            choices = [(v, v) for v in values]
        elif key == "selectedNamespaces":
            choices = [(v, v) for v in sorted({ns for c in self.snapshot.values() for ns in c["namespaces"]})]
        else:
            choices = [(identifier, c["name"]) for identifier, c in self.snapshot.items()]
        menu = QMenu(self)
        def apply(value):
            self.views.active[key] = value
            self.views.persist()
            self._save_preferences()
            self.render_matrix()
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
        menu.exec(QCursor.pos())

    def _select_scope(self):
        view = self.views.active
        clusters = self.store.clusters()
        options = [("cluster", cluster["id"], cluster["name"]) for cluster in clusters]
        options += [("namespace", namespace, namespace) for namespace in sorted({namespace
                    for cluster in self.snapshot.values() for namespace in cluster["namespaces"]})]
        options += [("deployment", name, name) for name in sorted({d.get("metadata", {}).get("name", "")
                    for cluster in self.snapshot.values() for ns in cluster["namespaces"].values() for d in ns["deployments"]})]
        dialog = QDialog(self)
        dialog.setWindowTitle("Фильтры ресурсов")
        dialog.resize(520, 620)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Выберите ресурсы для этой вкладки. Все отметки означают «все»; пустой выбор скрывает ресурсы."))
        lists = {}
        for kind, label, key in (("cluster", "Кластеры", "selectedClusterIds"), ("namespace", "Namespaces", "selectedNamespaces"),
                                 ("deployment", "Deployments", "selectedDeployments")):
            layout.addWidget(QLabel(label))
            widget = QListWidget()
            selected = view.get(key)
            for _, value, display in (option for option in options if option[0] == kind):
                entry = QListWidgetItem(display)
                entry.setData(Qt.ItemDataRole.UserRole, value)
                entry.setFlags(entry.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                entry.setCheckState(Qt.CheckState.Checked if selected is None or value in selected else Qt.CheckState.Unchecked)
                widget.addItem(entry)
            layout.addWidget(widget)
            lists[key] = widget
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if run_dialog(dialog) == QDialog.DialogCode.Accepted:
            for key, widget in lists.items():
                selected = [widget.item(i).data(Qt.ItemDataRole.UserRole) for i in range(widget.count())
                            if widget.item(i).checkState() == Qt.CheckState.Checked]
                view[key] = None if len(selected) == widget.count() else selected
            self.views.persist()
            self._save_preferences()
            self.render_matrix()

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
        if getattr(self, "_rendering_matrix", False) or not self._columns:
            return
        if logical == 0:
            self.preferences["deployment_width"] = new_size
        else:
            cluster_id, _, namespace = self._columns[logical - 1]
            self.views.active.setdefault("columnWidths", {})[f"{cluster_id}/{namespace}"] = new_size
        self.views.persist()
        self._save_preferences()

    def _row_resized(self, logical, old_size, new_size):
        if getattr(self, "_rendering_matrix", False) or logical >= len(self._row_names):
            return
        self.views.active.setdefault("rowHeights", {})[self._row_names[logical]] = new_size
        self.views.persist()
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
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            self.quota_content = QWidget()
            self.quota_layout = QVBoxLayout(self.quota_content)
            self.quota_layout.addWidget(QLabel("Квоты ресурсов · ResourceQuota.status"))
            scroll.setWidget(self.quota_content)
            self.tabs.addTab(scroll, title)
            return scroll
        page = QWidget()
        self.tabs.addTab(page, title)
        layout = QVBoxLayout(page)
        controls = QHBoxLayout()
        cluster_box, namespace_box = QComboBox(), QComboBox()
        cluster_box.setMaximumWidth(280)
        namespace_box.setMaximumWidth(280)
        controls.addWidget(cluster_box)
        controls.addWidget(namespace_box)
        button = QPushButton("Обновить")
        controls.addWidget(button)
        controls.addStretch(1)
        layout.addLayout(controls)
        table = ResourceList()
        table.setColumnCount(5 if kind == "pod" else 3)
        table.setHorizontalHeaderLabels(["Кластер", "Namespace", "Имя", "Владелец", "Состояние"] if kind == "pod" else ["Кластер", "Namespace", "Имя"])
        layout.addWidget(table)
        setattr(self, f"{kind}_controls", (cluster_box, namespace_box, table))
        cluster_box.currentIndexChanged.connect(lambda: self._load_namespaces_for(kind))
        namespace_box.currentIndexChanged.connect(lambda: self.load_resource_page(kind))
        button.clicked.connect(lambda: self.load_resource_page(kind))
        table.cellDoubleClicked.connect(lambda row, col: self.open_resource_row(kind, row))
        return page

    def load_quotas(self):
        if getattr(self, "_loading_quotas", False):
            return
        self._loading_quotas = True
        clusters = [c for c in self.store.clusters() if c["enabled"]]
        def load():
            result = []
            for cluster in clusters:
                try:
                    for namespace in self.api.namespaces(cluster["id"]):
                        try:
                            result.append((cluster["name"], namespace, self.api.list_resources(cluster["id"], "quota", namespace), ""))
                        except Exception as exc:
                            result.append((cluster["name"], namespace, [], str(exc)))
                except Exception as exc:
                    result.append((cluster["name"], "", [], str(exc)))
            return result
        def display(groups):
            self._loading_quotas = False
            while self.quota_layout.count():
                child = self.quota_layout.takeAt(0)
                if child.widget(): child.widget().deleteLater()
            self.quota_layout.addWidget(QLabel("Квоты ресурсов · использовано / лимит"))
            for cluster, namespace, quotas, failure in groups:
                if not quotas and not failure:
                    continue
                if failure:
                    self.quota_layout.addWidget(QLabel(failure))
                for quota in quotas:
                    self.quota_layout.addWidget(QuotaCard(cluster, namespace, quota))
            self.quota_layout.addStretch()
        def failed(message):
            self._loading_quotas = False
            self.statusBar().showMessage(message)
        self.background.submit(load, display, failed)

    def _invalidate_resource_list(self, kind):
        _, _, table = getattr(self, f"{kind}_controls")
        table.setRowCount(0)
        table.setEnabled(False)
        token = getattr(self, f"_{kind}_generation", 0)+1
        setattr(self, f"_{kind}_generation", token)
        return token

    def _load_namespaces_for(self, kind):
        cluster_box, namespace_box, _ = getattr(self, f"{kind}_controls")
        token = self._invalidate_resource_list(kind)
        cluster_id = cluster_box.currentData()
        namespace_box.clear()
        if not cluster_id:
            return
        def done(values):
            if getattr(self, f"_{kind}_generation") == token:
                namespace_box.addItems(values)
        self.background.submit(lambda: self.api.namespaces(cluster_id), done,
                               lambda message: self.statusBar().showMessage(message), key=("namespaces", kind))

    def load_resource_page(self, kind):
        cluster_box, namespace_box, _ = getattr(self, f"{kind}_controls")
        token = self._invalidate_resource_list(kind)
        cluster_id, namespace = cluster_box.currentData(), namespace_box.currentText()
        if not cluster_id or not namespace:
            return
        scope = (cluster_id, namespace, cluster_box.currentText())
        def done(values):
            if getattr(self, f"_{kind}_generation") == token:
                self._render_resources(kind, values, scope)
        self.background.submit(lambda: self.api.list_resources(cluster_id, kind, namespace), done,
                               lambda message: self.statusBar().showMessage(message), key=("resources", kind))

    def _render_resources(self, kind, values, scope=None):
        cluster_box, namespace_box, table = getattr(self, f"{kind}_controls")
        cluster_id, namespace, label = scope or (cluster_box.currentData(), namespace_box.currentText(), cluster_box.currentText())
        table.setRowCount(len(values))
        for row, value in enumerate(values):
            metadata = value.get("metadata", {})
            for col, text in enumerate((label, namespace, metadata.get("name", ""))):
                table.setItem(row, col, item(text))
            table.item(row, 2).setData(Qt.ItemDataRole.UserRole, (cluster_id, kind, namespace, metadata.get("name", ""), metadata.get("uid")))
            if kind == "pod":
                owner = next((o for o in metadata.get("ownerReferences", []) if o.get("controller")), {})
                table.setItem(row, 3, item(f"{owner.get('kind', 'Standalone / mirror')} {owner.get('name', '')}"))
                table.setItem(row, 4, item(pod_status(value)))
        table.resizeColumnsToContents()
        table.setEnabled(True)

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
        self._save_preferences()

    def _save_preferences(self):
        self.preference_writer.save(self.preferences)

    def _apply_theme(self):
        light = self.preferences.get("theme") == "light"
        apply_palette(self, light)
        self.setStyleSheet(stylesheet(light))
        if hasattr(self, "matrix_page"):
            self._apply_view_color(self.views.active.get("color", "transparent"))
            self.matrix.viewport().update()
            self.matrix.horizontalHeader().viewport().update()

    def clusters_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Настройки")
        dialog.resize(1100, 640)
        layout = QVBoxLayout(dialog)
        client_row = QHBoxLayout()
        client_row.addWidget(QLabel("kubectl path"))
        client = QLineEdit(self.store.preferences().get("kubectl_path", ""))
        client.setPlaceholderText("Автоматически из PATH")
        client_row.addWidget(client, 1)
        def set_client():
            try:
                if client.text().strip():
                    locate_kubectl(self.app_dir, client.text())
                self.preferences["kubectl_path"] = client.text().strip()
                self._save_preferences()
            except (OSError, ValueError) as exc:
                error(dialog, str(exc))
        def browse_client():
            path, _ = QFileDialog.getOpenFileName(dialog, "Общий kubectl", str(self.app_dir), "Программы (*.exe);;Все файлы (*)")
            if path:
                client.setText(path)
                set_client()
        choose = QPushButton("Обзор…")
        choose.clicked.connect(browse_client)
        client.editingFinished.connect(set_client)
        client_row.addWidget(choose)
        layout.addLayout(client_row)
        table = ResourceList()
        table.setColumnCount(6)
        table.setHorizontalHeaderLabels(["Имя", "API server", "Пользователь", "Активен", "Вход", "Пароль сохранён"])
        layout.addWidget(table)
        def populate():
            clusters = self.store.clusters()
            table.setRowCount(len(clusters))
            for row, cluster in enumerate(clusters):
                for col, value in enumerate((cluster["name"], cluster["server"], cluster["username"], "Да" if cluster["enabled"] else "Нет", "Требуется вход" if self.store.login_required(cluster) else "Активен",
                                             "Да" if cluster.get("encryptedPassword") else "Нет")):
                    table.setItem(row, col, item(value))
            table.resizeColumnsToContents()
            table.clearSelection()
            table.setCurrentItem(None)
            return clusters
        clusters = populate()
        buttons = QHBoxLayout()
        def selected():
            row = table.currentRow()
            return clusters[row] if 0 <= row < len(clusters) else None
        def edit(cluster=None):
            nonlocal clusters
            if run_dialog(ClusterDialog(self, self.store, self.app_dir, cluster)) == QDialog.DialogCode.Accepted:
                clusters = populate()
                self.refresh()
        add = QPushButton("Добавить")
        add.clicked.connect(lambda: edit())
        buttons.addWidget(add)
        change = QPushButton("Изменить / войти")
        change.clicked.connect(lambda: edit(selected()) if selected() else None)
        buttons.addWidget(change)
        test = QPushButton("Проверить")
        def test_selected():
            cluster = selected()
            if cluster:
                self.background.submit(lambda: len(self.api.namespaces(cluster["id"])),
                                       lambda count: QMessageBox.information(dialog, "Quaber", f"Подключение работает. Namespaces: {count}"),
                                       lambda message: error(dialog, message))
        test.clicked.connect(test_selected)
        buttons.addWidget(test)
        remove = QPushButton("Удалить")
        def remove_selected():
            nonlocal clusters
            cluster = selected()
            if cluster and confirm(dialog, "Удаление кластера", f"Удалить конфигурацию {cluster['name']} и её сохранённый kubeconfig?"):
                self.background.submit(lambda: self.proxy.close(cluster["id"]))
                self.store.delete_cluster(cluster["id"])
                clusters = populate()
                self.refresh()
        remove.clicked.connect(remove_selected)
        buttons.addWidget(remove)
        def update_selection():
            enabled = selected() is not None and bool(table.selectedItems())
            for action in (change, test, remove):
                action.setEnabled(enabled)
        table.itemSelectionChanged.connect(update_selection)
        update_selection()
        journal = QPushButton("Журнал действий")
        def show_journal():
            def display(records):
                content = "\n".join(f"{record.get('time', '')} · {record.get('phase', '')} · {record.get('action', '')} · {record.get('clusterId', '')} · {record.get('namespace', '')}/{record.get('name', '')} · {record.get('operationId', '')}"
                                    for record in reversed(records)) or "Действий пока нет."
                run_dialog(TextDialog(self, "Журнал действий", content))
            self.background.submit(self.store.recent_audit, display, lambda message: error(self, message))
        journal.clicked.connect(show_journal)
        buttons.addWidget(journal)
        buttons.addStretch()
        layout.addLayout(buttons)
        read_only = QCheckBox("Только чтение (изменения ресурсов и exec запрещены)")
        read_only.setChecked(self.preferences.get("read_only", True))
        read_only.toggled.connect(lambda checked: (self.preferences.__setitem__("read_only", checked), self._save_preferences()))
        layout.addWidget(read_only)
        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel("Цветовая тема"))
        theme = QComboBox()
        theme.setObjectName("settingsTheme")
        theme.addItem("Dark Modern", "dark")
        theme.addItem("Light Modern", "light")
        theme.setCurrentIndex(1 if self.preferences.get("theme") == "light" else 0)
        theme.currentIndexChanged.connect(lambda index: self._select_theme(theme.itemData(index)))
        theme_row.addWidget(theme)
        theme_row.addStretch()
        layout.addLayout(theme_row)
        run_dialog(dialog)

    def refresh(self):
        if self.tabs.currentIndex() == 0:
            self.refresh_matrix()
        elif self.tabs.currentIndex() == 1:
            self._refresh_resource_controls("quota")
        elif self.tabs.currentIndex() == 2:
            self._refresh_resource_controls("configmap")
        elif self.tabs.currentWidget() is self.allpods_page:
            self._refresh_resource_controls("pod")

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
        if cluster_box.currentData() and not namespace_box.count():
            self._load_namespaces_for(kind)
        elif namespace_box.currentText():
            self.load_resource_page(kind)

    def refresh_matrix(self):
        if getattr(self, "loading_matrix", False):
            return
        clusters = [item for item in self.store.clusters() if item["enabled"]]
        if not clusters:
            self.matrix.setRowCount(0)
            self.matrix.setColumnCount(0)
            self.summary_label.setText("Total: 0")
            self.statusBar().showMessage("Добавьте кластер в настройках.")
            return
        self.loading_matrix = True
        self.statusBar().showMessage("Обновление матрицы…")
        previous = self.snapshot
        def load():
            snapshot = {}
            failures = []
            def cluster_data(cluster):
                identifier = cluster["id"]
                data = copy.deepcopy(previous.get(identifier, {}).get("namespaces", {}))
                for scope in data.values():
                    scope["_stale"] = True
                    scope["_error"] = "Обновление не завершилось успешно"
                problems = []
                try:
                    namespaces = self.api.namespaces(identifier)
                except Exception as exc:
                    return identifier, {"name": cluster["name"], "namespaces": data}, [f"{cluster['name']}: {exc}"]
                def namespace_data(namespace):
                    return {"deployments": self.api.deployments(identifier, namespace),
                            "pods": self.api.pods(identifier, namespace),
                            "replica_sets": self.api.replica_sets(identifier, namespace),
                            "_stale": False, "_last_success": datetime.now().isoformat()}
                with ThreadPoolExecutor(max_workers=4) as executor:
                    pending = {executor.submit(namespace_data, ns): ns for ns in namespaces}
                    for future in as_completed(pending):
                        ns = pending[future]
                        try:
                            data[ns] = future.result()
                        except Exception as exc:
                            problems.append(f"{cluster['name']}/{ns}: {exc}")
                data = {ns: data[ns] for ns in namespaces if ns in data}
                return identifier, {"name": cluster["name"], "namespaces": data}, problems
            with ThreadPoolExecutor(max_workers=4) as executor:
                for identifier, data, problems in executor.map(cluster_data, clusters):
                    snapshot[identifier] = data
                    failures.extend(problems)
            return snapshot, failures
        def done(result):
            self.loading_matrix = False
            self.snapshot, failures = result
            self.last_updated.setText(("Частичное обновление: есть устаревшие данные · " if failures else "Последнее обновление: ") + datetime.now().strftime("%H:%M:%S"))
            self.render_matrix()
            self.statusBar().showMessage("; ".join(failures[:3]) if failures else "Матрица обновлена")
        self.background.submit(load, done, lambda message: (setattr(self, "loading_matrix", False), error(self, message)))

    def render_matrix(self):
        if not hasattr(self, "matrix"):
            return
        self.matrix.setUpdatesEnabled(False)
        try:
            self._render_matrix_content()
        finally:
            self.matrix.setUpdatesEnabled(True)
            self._rendering_matrix = False

    def _render_matrix_content(self):
        if not hasattr(self, "snapshot"):
            return
        self._rendering_matrix = True
        self._save_view_controls()
        view = self.views.active
        columns = [(cluster_id, cluster["name"], namespace) for cluster_id, cluster in self.snapshot.items()
                   for namespace in cluster["namespaces"] if (view.get("selectedClusterIds") is None or cluster_id in view["selectedClusterIds"])
                   and (view.get("selectedNamespaces") is None or namespace in view["selectedNamespaces"])]
        order = view.get("namespaceOrder") or []
        positions = {key: position for position, key in enumerate(order)}
        columns.sort(key=lambda col: positions.get(f"{col[0]}/{col[2]}", len(order)))
        deployments = sorted({deployment.get("metadata", {}).get("name", "") for cluster_id, _, namespace in columns
                              for deployment in self.snapshot[cluster_id]["namespaces"][namespace]["deployments"]})
        if view.get("selectedDeployments") is not None:
            deployments = [name for name in deployments if name in view["selectedDeployments"]]
        deployment_order = view.get("deploymentOrder") or []
        positions = {key: position for position, key in enumerate(deployment_order)}
        deployments.sort(key=lambda name: positions.get(name, len(deployment_order)))
        term = self.search.text().casefold().strip()
        if term:
            deployments = [name for name in deployments if term in name.casefold() or any(
                term in namespace.casefold() or any(term in pod.get("metadata", {}).get("name", "").casefold()
                                                   for pod in self.snapshot[cluster_id]["namespaces"][namespace]["pods"]
                                                   if pod.get("metadata", {}).get("name", "").startswith(name + "-"))
                for cluster_id, _, namespace in columns)]
        self._row_names = deployments
        self._columns = columns
        self.matrix.clear()
        self.matrix.setColumnCount(len(columns) + 1)
        self.matrix.setRowCount(len(deployments))
        for header in (self.matrix.horizontalHeader(), self.matrix.verticalHeader()):
            header.blockSignals(True)
            for logical in range(header.count()):
                visual = header.visualIndex(logical)
                if visual != logical:
                    header.moveSection(visual, logical)
            header.blockSignals(False)
        self.matrix.setHorizontalHeaderLabels(["Deployment"] + [f"{label}\n{ns}" for _, label, ns in columns])
        for col, (cluster_id, label, namespace) in enumerate(columns, 1):
            boundary = col == len(columns) or columns[col][0] != cluster_id
            self.matrix.model().setHeaderData(col, Qt.Orientation.Horizontal, boundary, Qt.ItemDataRole.UserRole)
            scope = self.snapshot[cluster_id]["namespaces"][namespace]
            stale = scope.get("_stale", False)
            if stale:
                self.matrix.horizontalHeaderItem(col).setText(f"{label}\n⚠ {namespace}")
            self.matrix.horizontalHeaderItem(col).setToolTip(f"{label} / {namespace}\nПоследний успех: {scope.get('_last_success', 'неизвестно')}" + ("\nУСТАРЕЛО: " + scope.get("_error", "ошибка обновления") if stale else ""))
        self.matrix_data.clear()
        scope_indexes = {(cid, ns): index_scope(self.snapshot[cid]["namespaces"][ns]) for cid, _, ns in columns}
        visible_pod_count = 0
        for row, name in enumerate(deployments):
            self.matrix.setItem(row, 0, item(name))
            for col, (cluster_id, _, namespace) in enumerate(columns, 1):
                resources = self.snapshot[cluster_id]["namespaces"][namespace]
                deployments_index, pods_index = scope_indexes[(cluster_id, namespace)]
                deployment = deployments_index.get(name)
                if not deployment:
                    continue
                deployment_uid = deployment.get("metadata", {}).get("uid")
                pods = pods_index.get(deployment_uid, [])
                filtered = []
                recent = False
                for pod in pods:
                    status = pod_status(pod)
                    if status == "Succeeded":
                        status = "Completed"
                    restarted = self._recent_restart(pod)
                    recent |= restarted
                    allowed = self.status_filters[status].isChecked() if status in self.status_filters else True
                    if (self.problem_only.isChecked() and (status in ("Pending", "Failed", "Unknown") or restarted)) or (not self.problem_only.isChecked() and (allowed or (self.recent.isChecked() and restarted))):
                        filtered.append(pod)
                visible_pod_count += len(filtered)
                available = deployment.get("status", {}).get("availableReplicas", 0)
                target = deployment.get("spec", {}).get("replicas", 1)
                color = "#286047" if available >= target and pods else "#70512a" if pods else "#5d3641"
                if recent:
                    color = "#755e2b"
                for pod in filtered:
                    pod["_recent"] = self._recent_restart(pod)
                    pod["_stale"] = bool(resources.get("_stale"))
                missing = target > 0 and not pods
                text = "0 (scale=0)" if target == 0 else f"0/{target}\nНет Pod" if missing else "Скрыто фильтром" if pods and not filtered else ""
                cell = item(text)
                if missing:
                    cell.setForeground(QColor("#E56B6E"))
                cell.setData(Qt.ItemDataRole.UserRole, filtered)
                cell.setToolTip("\n".join(str(c.get("reason", ""))+": "+str(c.get("message", "")) for c in deployment.get("status", {}).get("conditions", [])) or text or "Наведите на Pod для сведений")
                self.matrix.setItem(row, col, cell)
                self.matrix_data[(row, col)] = (cluster_id, namespace, name, deployment, filtered)
                self.matrix.setRowHeight(row, max(self.matrix.rowHeight(row), 70 if missing else 30, 14 + ((len(filtered)+1)//2)*19))
        if self.hide_empty.isChecked():
            for col in range(self.matrix.columnCount() - 1, 0, -1):
                if not any(self.matrix.item(row, col) and (self.matrix_data.get((row, col), (None, None, None, {}, []))[4] or self.matrix_data[(row, col)][3].get("spec", {}).get("replicas", 1) >= 0) for row in range(self.matrix.rowCount())):
                    self.matrix.setColumnHidden(col, True)
                else:
                    self.matrix.setColumnHidden(col, False)
        else:
            for col in range(1, self.matrix.columnCount()):
                self.matrix.setColumnHidden(col, False)
        self.matrix.resizeColumnToContents(0)
        self.matrix.horizontalHeader().setMinimumSectionSize(36)
        totals = {status: 0 for status in self.status_filters}
        total_pods = 0
        for ref in self.matrix_data.values():
            for pod in ref[4]:
                total_pods += 1
                status = pod_status(pod)
                if status in totals:
                    totals[status] += 1
        for status, checkbox in self.status_filters.items():
            checkbox.setText(f"{'Error' if status == 'Failed' else status}  {totals[status]}")
            checkbox.setEnabled(not self.problem_only.isChecked())
        self.recent.setEnabled(not self.problem_only.isChecked())
        self.summary_label.setText(f"{total_pods}  Total")
        for key, (title, button) in self.scope_buttons.items():
            selected = view.get(key)
            button.setText(f"{title}: {'Все' if selected is None else len(selected)} ▾")
        self.matrix.setColumnWidth(0, self.preferences.get("deployment_width", 150))
        self.matrix.horizontalHeader().setFixedHeight(self.preferences.get("header_height", 160))
        for col, (cluster_id, _, namespace) in enumerate(columns, 1):
            width = view.get("columnWidths", {}).get(f"{cluster_id}/{namespace}")
            self.matrix.setColumnWidth(col, width or 48)
        for row, name in enumerate(deployments):
            height = view.get("rowHeights", {}).get(name)
            if height:
                self.matrix.setRowHeight(row, height)
        self.matrix.sync_frozen()
        self._rendering_matrix = False

    @staticmethod
    def _recent_restart(pod):
        for status in pod.get("status", {}).get("containerStatuses", []):
            if not status.get("restartCount"):
                continue
            values = [status.get("state", {}).get("running", {}).get("startedAt"),
                      status.get("lastState", {}).get("terminated", {}).get("finishedAt"),
                      status.get("lastState", {}).get("terminated", {}).get("startedAt")]
            for value in values:
                if value:
                    try:
                        age = (datetime.now(timezone.utc) - datetime.fromisoformat(value.replace("Z", "+00:00"))).total_seconds()
                        if 0 <= age <= 3600:
                            return True
                    except ValueError:
                        pass
        return False

    def _matrix_click(self, row, col, point):
        ref = self.matrix_data.get((row, col))
        if col == 0:
            choices = [key for key in self.matrix_data if key[0] == row]
            if len(choices) == 1:
                target = self.matrix_data[choices[0]]
                self.open_resource(target[0], "deployment", target[1], target[2])
            elif choices:
                labels = [f"{self.snapshot[self.matrix_data[key][0]]['name']} / {self.matrix_data[key][1]}" for key in choices]
                selected, accepted = QInputDialog.getItem(self, "Deployment", "Выберите расположение", labels, 0, False)
                if accepted:
                    target = self.matrix_data[choices[labels.index(selected)]]
                    self.open_resource(target[0], "deployment", target[1], target[2])
            return
        if not ref:
            return
        rect = self.matrix.visualItemRect(self.matrix.item(row, col))
        i = pod_at(rect, point, len(ref[4]))
        if i is not None:
            self.open_resource(ref[0], "pod", ref[1], ref[4][i]["metadata"]["name"], expected_uid=ref[4][i]["metadata"].get("uid"))
        else:
            self.open_resource(ref[0], "deployment", ref[1], ref[2])

    def _pod_menu(self, point):
        index = self.matrix.indexAt(point)
        ref = self.matrix_data.get((index.row(), index.column()))
        if not ref:
            return
        rect = self.matrix.visualRect(index)
        i = pod_at(rect, point, len(ref[4]))
        if i is None:
            return
        pod = ref[4][i]["metadata"]["name"]
        menu = QMenu(self)
        for label, tab in (("Открыть карточку Pod", "Details"), ("Открыть логи", "Логи"), ("Открыть консоль Pod", "Консоль Pod")):
            menu.addAction(label, lambda checked=False, tab=tab: self.open_resource(ref[0], "pod", ref[1], pod, tab, expected_uid=ref[4][i]["metadata"].get("uid")))
        menu.exec(self.matrix.viewport().mapToGlobal(point))

    def open_resource(self, cluster_id, kind, namespace, name, initial_tab=None, expected_uid=None):
        origin = getattr(self, "_resource_panel", None)
        if origin is not None:
            origin._navigation_pending = True
        self._resource_request = getattr(self, "_resource_request", 0) + 1
        request_id = self._resource_request
        def load():
            try:
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
            if resource is None:
                message = f"Pod {namespace}/{name} больше не существует или был заменён. Список обновлён." if kind == "pod" else f"Ресурс {namespace}/{name} больше не существует."
                self.statusBar().showMessage(message)
                previous = getattr(self, "_resource_panel", None)
                if previous is not None:
                    from PyQt6 import sip
                    if not sip.isdeleted(previous) and previous.kind == "deployment":
                        previous.deployment_notice.setText(message)
                        previous.deployment_notice.show()
                        previous.reload_deployment_pods()
                self.refresh()
                return
            if kind == "quota":
                run_dialog(TextDialog(self, f"ResourceQuota {name}", yaml.safe_dump(resource, allow_unicode=True)))
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
                panel = ResourceDialog(self, self.api, self.background, cluster_id, kind, namespace, name, resource)
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
                self._position_resource_panel()
                panel.show()
                panel.raise_()
        def failed(message):
            if request_id != self._resource_request:
                return
            if origin is not None:
                origin._navigation_pending = False
                if hasattr(origin, "nav_cluster"):
                    origin._restore_navigation()
            error(self, message)
        self.background.submit(load, done, failed)

    def _dismiss_resource_event(self, event):
        panel = getattr(self, "_resource_panel", None)
        if panel is not None:
            panel.close()

    def _resource_finished(self, panel):
        self._resource_request = getattr(self, "_resource_request", 0) + 1
        stack = getattr(self, "_resource_stack", [])
        if panel in stack:
            stack.remove(panel)
        self._resource_panel = stack[-1] if stack else None
        if self._resource_panel is not None:
            self._resource_panel._navigation_pending = False
            self._resource_panel.show()
        panel.deleteLater()
        if not stack:
            overlay = getattr(self, "_resource_overlay", None)
            if overlay is not None:
                overlay.hide()
                overlay.deleteLater()
                self._resource_overlay = None
        self._position_resource_panel()

    def _position_resource_panel(self):
        if not hasattr(self, "workspace"):
            return
        from PyQt6 import sip
        overlay = getattr(self, "_resource_overlay", None)
        if overlay is not None and not sip.isdeleted(overlay):
            overlay.setGeometry(self.workspace.rect())
        stack = getattr(self, "_resource_stack", [])
        width = self.workspace.width()
        for depth, panel in enumerate(stack):
            if sip.isdeleted(panel):
                continue
            root_width = min(width, max(900, round(width*.94)))
            panel_width = root_width
            panel.setGeometry(width-panel_width, 0, panel_width, self.workspace.height())
            panel.raise_()
        strip = getattr(self, "_resource_return_strip", None)
        if strip is not None and not sip.isdeleted(strip):
            strip.hide()
    def eventFilter(self, watched, event):
        if watched is getattr(self, "workspace", None) and event.type() == QEvent.Type.Resize:
            self._position_resource_panel()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_resource_panel()

    def closeEvent(self, event):
        if not self._close_resource_stack():
            event.ignore()
            return
        self._closing = True
        self.timer.stop()
        self.auth_timer.stop()
        self.api.stopped = True
        self.background.stop()
        try:
            self._save_preferences()
        finally:
            self.preference_writer.close()
            logger.info("Quaber закрывается")
            # No new producers survive; wait for already started reads before
            # closing proxies so they cannot recreate them after cleanup.
            pool, proxy, writer, logs = self.background.pool, self.proxy, self.preference_writer, self.log_session
            def cleanup():
                try:
                    try:
                        pool.waitForDone()
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
