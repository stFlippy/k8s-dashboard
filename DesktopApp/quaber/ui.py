"""Native Qt desktop shell for Quaber."""

from __future__ import annotations

import copy
import difflib
import json
import random
import time
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml
from PyQt6.QtCore import QObject, QPointF, QRunnable, QThreadPool, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFormLayout, QGridLayout, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QSpinBox, QSplitter, QTabBar, QTabWidget,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget)

from .kubectl import ProxyManager, locate_kubectl, login, test_connection
from .kubernetes import Kubernetes
from .storage import UserStore
from .views import MatrixViews


class Signals(QObject):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)


class Task(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function = function
        self.signals = Signals()

    def run(self):
        try:
            self.signals.done.emit(self.function())
        except Exception as exc:
            self.signals.failed.emit(str(exc) or type(exc).__name__)


class Background:
    def __init__(self):
        self.pool = QThreadPool.globalInstance()
        self.running: set[Task] = set()

    def submit(self, function, done=None, failed=None):
        task = Task(function)
        self.running.add(task)
        def cleanup(value):
            self.running.discard(task)
            if done:
                done(value)
        def error(message):
            self.running.discard(task)
            if failed:
                failed(message)
        task.signals.done.connect(cleanup)
        task.signals.failed.connect(error)
        self.pool.start(task)


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
        self.resize(520, 340)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(cluster["name"] if cluster else "")
        self.server = QLineEdit(cluster["server"] if cluster else "")
        self.username = QLineEdit(cluster["username"] if cluster else "")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.enabled = QCheckBox("Кластер активен")
        self.enabled.setChecked(cluster["enabled"] if cluster else True)
        form.addRow("Имя", self.name)
        form.addRow("API server", self.server)
        form.addRow("Пользователь", self.username)
        form.addRow("Пароль", self.password)
        form.addRow("", self.enabled)
        layout.addLayout(form)
        layout.addWidget(QLabel("Пароль используется только для kubectl login и не сохраняется."))
        self.status = QLabel("")
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.test_button = buttons.addButton("Проверить", QDialogButtonBox.ButtonRole.ActionRole)
        self.test_button.clicked.connect(lambda: self.connect_cluster(False))
        buttons.accepted.connect(lambda: self.connect_cluster(True))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.buttons = buttons
        self.busy = False

    def connect_cluster(self, save: bool):
        name, server, username, password = self.name.text(), self.server.text(), self.username.text(), self.password.text()
        existing = self.cluster
        enabled = self.enabled.isChecked()
        if not password and (not existing or server.strip() != existing["server"] or username.strip() != existing["username"]):
            error(self, "Для нового кластера или смены API server/пользователя нужен пароль.")
            return
        self.status.setText("Выполняется kubectl login…" if password else "Проверка сохранённого подключения…")
        self.buttons.setEnabled(False)
        self.busy = True
        def work():
            executable = locate_kubectl(self.app_dir)
            config = login(executable, server.strip(), username.strip(), password) if password else None
            if save:
                record = self.store.put_cluster(name, server, username, config, enabled, existing["id"] if existing else None)
                return record
            if config:
                count = test_connection(executable, self.store, server.strip(), config)
                return f"Подключение установлено, namespaces: {count}. Настройки ещё не сохранены."
            count = len(self.parent().api.namespaces(existing["id"]))
            return f"Подключение установлено, namespaces: {count}."
        def done(value):
            self.busy = False
            self.buttons.setEnabled(True)
            if save:
                self.password.clear()
                self.accept()
            else:
                self.status.setText(str(value))
        def failed(message):
            self.busy = False
            self.buttons.setEnabled(True)
            self.password.clear()
            self.status.setText(message)
        self.parent().background.submit(work, done, failed)

    def closeEvent(self, event):
        if self.busy:
            event.ignore()
        else:
            super().closeEvent(event)

    def reject(self):
        if not self.busy:
            super().reject()


class LoginGroupsDialog(QDialog):
    def __init__(self, parent, groups: list[dict]):
        super().__init__(parent)
        self.groups = groups
        self.setWindowTitle("Требуется вход в кластеры")
        self.resize(620, 380)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Срок действия сохранённого подключения истёк. Пароль не сохраняется."))
        self.passwords = []
        for group in groups:
            layout.addWidget(QLabel(f"{group['username']}: {', '.join(cluster['name'] for cluster in group['clusters'])}"))
            password = QLineEdit()
            password.setEchoMode(QLineEdit.EchoMode.Password)
            password.setPlaceholderText("Пароль")
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
        if any(not value for value in passwords):
            error(self, "Введите пароль для каждого пользователя.")
            return
        self.buttons.setEnabled(False)
        self.busy = True
        self.status.setText("Выполняется kubectl login…")
        parent = self.parent()
        app_dir, store = parent.app_dir, parent.store
        def work():
            executable = locate_kubectl(app_dir)
            for group, password in zip(self.groups, passwords):
                updates = {cluster["id"]: login(executable, cluster["server"], group["username"], password)
                           for cluster in group["clusters"]}
                store.update_logins(updates)
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
            super().closeEvent(event)

    def reject(self):
        if not self.busy:
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

    def set_data(self, values: list[float | None], request: float | None = None, limit: float | None = None):
        self.values, self.request, self.limit = values, request, limit
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = self.palette().text().color()
        painter.setPen(color)
        current = next((value for value in reversed(self.values) if value is not None), None)
        label = f"{self.title}: {current:.2f} {self.unit}" if current is not None else f"{self.title}: —"
        painter.drawText(8, 18, label)
        left, right, top, bottom = 50, self.width() - 12, 30, self.height() - 20
        if right <= left or bottom <= top:
            return
        values = [value for value in self.values if value is not None]
        maximum = max([*values, *(v for v in (self.request, self.limit) if v is not None), 0.001]) * 1.1
        painter.setPen(QPen(QColor("#526477"), 1))
        for step in (0, 0.5, 1):
            y = bottom - (bottom - top) * step
            painter.drawLine(QPointF(left, y), QPointF(right, y))
        def y_value(value):
            return bottom - max(0.0, min(maximum, value)) / maximum * (bottom - top)
        for value, color_code in ((self.request, "#e0b15a"), (self.limit, "#db7777")):
            if value is not None:
                painter.setPen(QPen(QColor(color_code), 1))
                y = y_value(value)
                painter.drawLine(QPointF(left, y), QPointF(right, y))
        if values:
            painter.setPen(QPen(QColor("#51a7f9"), 2))
            points = QPolygonF([QPointF(left + (right - left) * index / max(1, len(self.values) - 1), y_value(value))
                                for index, value in enumerate(self.values) if value is not None])
            if len(points) > 1:
                painter.drawPolyline(points)
            else:
                painter.drawEllipse(points[0], 2, 2)
        painter.setPen(color)
        painter.drawText(8, bottom, "0")
        painter.drawText(8, top + 7, f"{maximum:.1f}")


class MetricsWidget(QWidget):
    def __init__(self, parent, api: Kubernetes, background: Background, cluster_id: str, namespace: str, pod: str):
        super().__init__(parent)
        self.api, self.background, self.cluster_id, self.namespace, self.pod = api, background, cluster_id, namespace, pod
        self.history: dict[str, dict[str, list[float | None]]] = {}
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
            self.status.setText(message)
        self.background.submit(lambda: self.api.metrics(self.cluster_id, self.namespace, self.pod), done, failed)

    def render(self):
        name = self.container.currentText()
        container = next((item for item in self.current.get("containers", []) if item["name"] == name), None)
        if not container:
            return
        history = self.history.get(name, {})
        cpu = container.get("cpu", {})
        memory = container.get("memory", {})
        self.cpu.set_data([v * 1000 if v is not None else None for v in history.get("cpu", [])],
                          cpu.get("requestCores") * 1000 if cpu.get("requestCores") is not None else None,
                          cpu.get("limitCores") * 1000 if cpu.get("limitCores") is not None else None)
        self.memory.set_data([v / 1048576 if v is not None else None for v in history.get("memory", [])],
                             memory.get("requestBytes") / 1048576 if memory.get("requestBytes") is not None else None,
                             memory.get("limitBytes") / 1048576 if memory.get("limitBytes") is not None else None)
        self.throttling.set_data(history.get("throttle", []))


class ResourceDialog(QDialog):
    def __init__(self, parent, api: Kubernetes, background: Background, cluster_id: str, kind: str, namespace: str, name: str, resource: dict):
        super().__init__(parent)
        self.api, self.background = api, background
        self.cluster_id, self.kind, self.namespace, self.name = cluster_id, kind, namespace, name
        self.resource = resource
        self.version = resource.get("metadata", {}).get("resourceVersion", "")
        self.pending_operation = False
        self.setWindowTitle(f"{kind.title()}: {name} — {namespace}")
        self.resize(1000, 720)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        summary = QTextEdit()
        summary.setReadOnly(True)
        summary.setPlainText(self._summary(resource))
        self.tabs.addTab(summary, "Сведения")
        self.yaml_editor = QTextEdit()
        self.yaml_editor.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.yaml_editor.setPlainText(yaml.safe_dump(resource, allow_unicode=True, sort_keys=False))
        self.tabs.addTab(self.yaml_editor, "YAML")
        if kind == "pod":
            self._pod_tabs()
        elif kind == "deployment":
            self._deployment_tabs()
        controls = QHBoxLayout()
        save = QPushButton("Сохранить YAML")
        save.clicked.connect(self.save_yaml)
        controls.addWidget(save)
        if kind == "deployment":
            scale = QPushButton("Масштабировать")
            scale.clicked.connect(self.scale)
            controls.addWidget(scale)
            resources = QPushButton("CPU / RAM")
            resources.clicked.connect(self.edit_resources)
            controls.addWidget(resources)
            restart = QPushButton("Restart")
            restart.clicked.connect(self.restart)
            controls.addWidget(restart)
        if kind == "pod":
            remove = QPushButton("Restart Pod" if self._controlled() else "Удалить Pod")
            remove.clicked.connect(self.delete_pod)
            controls.addWidget(remove)
        controls.addStretch()
        close = QPushButton("Закрыть")
        close.clicked.connect(self.close)
        controls.addWidget(close)
        layout.addLayout(controls)

    def _summary(self, resource: dict) -> str:
        metadata, status = resource.get("metadata", {}), resource.get("status", {})
        return "\n".join([f"Name: {metadata.get('name')}", f"Namespace: {metadata.get('namespace')}",
                          f"UID: {metadata.get('uid')}", f"Created: {metadata.get('creationTimestamp')}",
                          f"Resource version: {metadata.get('resourceVersion')}", "", json.dumps(status, ensure_ascii=False, indent=2)])

    def _controlled(self) -> bool:
        return any(owner.get("controller") for owner in self.resource.get("metadata", {}).get("ownerReferences", []))

    def _pod_tabs(self):
        container_names = [c.get("name", "") for c in self.resource.get("spec", {}).get("containers", [])]
        panel = QWidget()
        layout = QVBoxLayout(panel)
        row = QHBoxLayout()
        self.container = QComboBox()
        self.container.addItems(container_names)
        row.addWidget(self.container)
        self.previous = QCheckBox("Предыдущие логи")
        row.addWidget(self.previous)
        load = QPushButton("Загрузить")
        load.clicked.connect(self.load_logs)
        row.addWidget(load)
        export = QPushButton("Экспорт")
        export.clicked.connect(self.export_logs)
        row.addWidget(export)
        layout.addLayout(row)
        self.log_search = QLineEdit()
        self.log_search.setPlaceholderText("Поиск в логах")
        layout.addWidget(self.log_search)
        self.logs = QTextEdit()
        self.logs.setReadOnly(True)
        self.log_search.textChanged.connect(lambda text: self.logs.find(text) if text else self.logs.moveCursor(self.logs.textCursor().MoveOperation.Start))
        layout.addWidget(self.logs)
        self.tabs.addTab(panel, "Логи")
        terminal = QWidget()
        terminal_layout = QVBoxLayout(terminal)
        self.console_output = QTextEdit()
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
        self._info_tab("События", lambda: self.api.events(self.cluster_id, self.namespace, "Pod", self.name))
        self.tabs.addTab(MetricsWidget(self, self.api, self.background, self.cluster_id, self.namespace, self.name), "Метрики")
        self._info_tab("Контейнеры", lambda: self.resource.get("spec", {}).get("containers", []))

    def lock_console(self):
        self.console_unlocked_until = 0.0
        self.console_lock.setText("🔒")

    def run_console(self):
        command = self.console_input.text().strip()
        container = self.container.currentText()
        if not command or not container:
            return
        if time.monotonic() >= self.console_unlocked_until:
            if not confirm(self, "Разблокировка консоли Pod", f"Выполнить первую команду в {self.namespace}/{self.name} ({container})?\nПосле подтверждения консоль откроется на 5 минут простоя.\n\n{command[:500]}"):
                return
        self.console_unlocked_until = time.monotonic() + 300
        self.console_lock.setText("🔓")
        self.console_input.clear()
        self.console_output.append(f"{container}@{self.name}:~$ {command}")
        cluster = self.api.cluster(self.cluster_id)
        self._mutate(lambda: self.api.proxy.execute(cluster, self.namespace, self.name, container, command),
                     lambda result: self.console_output.append("\n".join(part for part in result if part) or "(нет вывода)"),
                     lambda message: self.console_output.append(f"Ошибка: {message}"))

    def _deployment_tabs(self):
        pod_table = QTableWidget()
        pod_table.setColumnCount(3)
        pod_table.setHorizontalHeaderLabels(["Pod", "Статус", "Рестарты"])
        self.tabs.addTab(pod_table, "Pods")
        deployment_uid = self.resource.get("metadata", {}).get("uid")
        def load_pods():
            pods = self.api.pods(self.cluster_id, self.namespace)
            replica_sets = self.api.replica_sets(self.cluster_id, self.namespace)
            owned = {rs.get("metadata", {}).get("uid") for rs in replica_sets if any(owner.get("uid") == deployment_uid and owner.get("controller") for owner in rs.get("metadata", {}).get("ownerReferences", []))}
            return [pod for pod in pods if any(owner.get("uid") in owned or owner.get("uid") == deployment_uid
                                               for owner in pod.get("metadata", {}).get("ownerReferences", []) if owner.get("controller"))]
        def show_pods(pods):
            pod_table.setRowCount(len(pods))
            for row, pod in enumerate(pods):
                meta, status = pod.get("metadata", {}), pod.get("status", {})
                for col, value in enumerate((meta.get("name", ""), status.get("phase", "Unknown"),
                                             sum(item.get("restartCount", 0) for item in status.get("containerStatuses", [])))):
                    pod_table.setItem(row, col, item(value))
            pod_table.resizeColumnsToContents()
        self.background.submit(load_pods, show_pods, lambda message: error(self, message))
        pod_table.cellDoubleClicked.connect(lambda row, _: self.parent().open_resource(
            self.cluster_id, "pod", self.namespace, pod_table.item(row, 0).text()))
        self._info_tab("События", lambda: self.api.events(self.cluster_id, self.namespace, "Deployment", self.name))
        self._info_tab("Метрики", lambda: {"pods": [self.api.metrics(self.cluster_id, self.namespace, pod.get("metadata", {}).get("name", ""))
                                                     for pod in load_pods()]})

    def _info_tab(self, label: str, loader):
        view = QTextEdit()
        view.setReadOnly(True)
        view.setPlainText("Загрузка…")
        self.tabs.addTab(view, label)
        self.background.submit(loader, lambda value: view.setPlainText(json.dumps(value, ensure_ascii=False, indent=2)),
                               lambda message: view.setPlainText(message))

    def load_logs(self):
        self.logs.setPlainText("Загрузка…")
        self.background.submit(lambda: self.api.logs(self.cluster_id, self.namespace, self.name, self.container.currentText(), self.previous.isChecked()),
                               self.logs.setPlainText, lambda message: self.logs.setPlainText(message))

    def export_logs(self):
        path, _ = QFileDialog.getSaveFileName(self, "Экспорт логов", f"{self.name}.log", "Text (*.log *.txt)")
        if path:
            try:
                Path(path).write_text(self.logs.toPlainText(), encoding="utf-8")
            except OSError as exc:
                error(self, str(exc))

    def save_yaml(self):
        try:
            edited = yaml.safe_load(self.yaml_editor.toPlainText())
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
        TextDialog(self, "Предварительный просмотр изменений", diff).exec()
        if not confirm(self, "Сохранить изменения", f"Изменить {self.kind} {self.namespace}/{self.name}?\nБудет проверен resourceVersion объекта."):
            return
        self._mutate(lambda: self.api.replace(self.cluster_id, self.kind, self.namespace, self.name, edited, self.version), self._updated)

    def _mutate(self, function, done, failed=None):
        if self.pending_operation:
            return
        self.pending_operation = True
        def finish(value):
            self.pending_operation = False
            done(value)
        def on_error(message):
            self.pending_operation = False
            (failed or (lambda value: error(self, value)))(message)
        self.background.submit(function, finish, on_error)

    def closeEvent(self, event):
        if self.pending_operation:
            event.ignore()
        else:
            super().closeEvent(event)

    def reject(self):
        if not self.pending_operation:
            super().reject()

    def _updated(self, resource):
        self.resource = resource
        self.version = resource.get("metadata", {}).get("resourceVersion", "")
        self.yaml_editor.setPlainText(yaml.safe_dump(resource, allow_unicode=True, sort_keys=False))
        QMessageBox.information(self, "Quaber", "Изменение сохранено.")

    def scale(self):
        current = self.resource.get("spec", {}).get("replicas", 0)
        value, accepted = QInputDialog.getInt(self, "Реплики Deployment", "Число реплик", current, 0, 10000)
        if accepted and confirm(self, "Масштабировать", f"Установить {value} реплик для {self.name}?"):
            self._mutate(lambda: self.api.scale(self.cluster_id, self.namespace, self.name, value), self._updated)

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
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        edited = copy.deepcopy(self.resource)
        target = next(container for container in edited["spec"]["template"]["spec"]["containers"] if container.get("name") == name)
        resources = target.setdefault("resources", {})
        for (category, resource), edit in fields.items():
            value = edit.text().strip()
            if len(value) > 100 or (value and not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?(?:[a-zA-Z]+)?", value)):
                error(self, f"Некорректное значение {category}/{resource}.")
                return
            bucket = resources.setdefault(category, {})
            if value:
                bucket[resource] = value
            else:
                bucket.pop(resource, None)
            if not bucket:
                resources.pop(category, None)
        before = yaml.safe_dump(original, allow_unicode=True, sort_keys=False).splitlines(keepends=True)
        after = yaml.safe_dump(resources, allow_unicode=True, sort_keys=False).splitlines(keepends=True)
        diff = "".join(difflib.unified_diff(before, after, fromfile="До", tofile="После"))
        if not diff:
            return
        TextDialog(self, "Предварительный просмотр CPU / RAM", diff + "\nИзменение шаблона Pod запустит rollout Deployment.").exec()
        if not confirm(self, "Изменение ресурсов", f"Применить новые ресурсы контейнера {name} и запустить rollout?\nresourceVersion будет проверен."):
            return
        self._mutate(lambda: self.api.replace(self.cluster_id, "deployment", self.namespace, self.name, edited, self.version), self._updated)

    def restart(self):
        if confirm(self, "Restart Deployment", f"Запустить rollout Deployment {self.name}?"):
            self._mutate(lambda: self.api.restart(self.cluster_id, self.namespace, self.name), self._updated)

    def delete_pod(self):
        mode = "restart" if self._controlled() else "delete"
        if confirm(self, "Подтверждение", f"{('Перезапустить' if mode == 'restart' else 'Удалить')} Pod {self.name}?\nUID будет проверен перед удалением."):
            uid = self.resource.get("metadata", {}).get("uid", "")
            self._mutate(lambda: self.api.delete_pod(self.cluster_id, self.namespace, self.name, uid, mode), lambda _: self.accept())


class MainWindow(QMainWindow):
    def __init__(self, store: UserStore, app_dir: Path):
        super().__init__()
        self.store, self.app_dir = store, app_dir
        self.background = Background()
        try:
            executable = locate_kubectl(app_dir)
        except FileNotFoundError:
            executable = None
        self.proxy = ProxyManager(executable, store)
        self.api = Kubernetes(store, self.proxy)
        self.preferences = store.preferences()
        self.views = MatrixViews(self.preferences)
        self.setWindowTitle("Quaber")
        screen = QApplication.primaryScreen()
        if screen:
            available = screen.availableGeometry()
            self.resize(min(1450, max(700, available.width() - 80)), min(900, max(550, available.height() - 80)))
        else:
            self.resize(1450, 900)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        header = QHBoxLayout()
        logo_path = app_dir / "assets" / "logo.png"
        if logo_path.exists():
            icon = QLabel()
            icon.setPixmap(QPixmap(str(logo_path)).scaled(34, 34, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            header.addWidget(icon)
        brand = QLabel("Quaber")
        brand.setStyleSheet("font-size: 22px; font-weight: 700; color: #51a7f9")
        header.addWidget(brand)
        header.addStretch()
        clusters_button = QPushButton("Кластеры · Настройки")
        clusters_button.clicked.connect(self.clusters_dialog)
        header.addWidget(clusters_button)
        layout.addLayout(header)
        if executable is None:
            missing = QLabel("kubectl не найден. Поместите корпоративный клиент в addons рядом с приложением или добавьте его в PATH.")
            missing.setStyleSheet("color: #e6b860; padding: 8px")
            layout.addWidget(missing)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self.matrix_page = QWidget()
        self.tabs.addTab(self.matrix_page, "Pod list")
        self._setup_matrix()
        self.quotas_page = self._resource_page("quota", "Квоты ресурсов")
        self.configmaps_page = self._resource_page("configmap", "ConfigMaps")
        self.tabs.currentChanged.connect(self.refresh)
        self.statusBar().showMessage("Готово")
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        interval = self.preferences.get("refresh_ms", 10000)
        if interval:
            self.timer.start(interval)
        self._apply_theme()
        QTimer.singleShot(0, self.refresh)
        QTimer.singleShot(1000, self._prompt_expired_login)

    def _prompt_expired_login(self):
        expired = [cluster for cluster in self.store.clusters() if cluster["enabled"] and self.store.login_required(cluster)]
        if expired:
            groups: dict[str, dict] = {}
            for cluster in expired:
                key = cluster["username"].strip().casefold()
                group = groups.setdefault(key, {"username": cluster["username"], "clusters": []})
                group["clusters"].append(cluster)
            if LoginGroupsDialog(self, list(groups.values())).exec() == QDialog.DialogCode.Accepted:
                self.refresh()

    def _setup_matrix(self):
        layout = QVBoxLayout(self.matrix_page)
        view_row = QHBoxLayout()
        self.view_bar = QTabBar()
        self.view_bar.setMovable(True)
        self.view_bar.setExpanding(False)
        for view in self.views.views:
            self.view_bar.addTab(view["name"])
        self.view_bar.setCurrentIndex(next(i for i, view in enumerate(self.views.views) if view["id"] == self.views.active_id))
        self.view_bar.currentChanged.connect(self._switch_view)
        self.view_bar.tabMoved.connect(self._move_view)
        view_row.addWidget(self.view_bar, 1)
        for label, handler in (("+", self._add_view), ("Переименовать", self._rename_view), ("Удалить", self._remove_view),
                               ("Экспорт", self._export_views), ("Импорт", self._import_views)):
            button = QPushButton(label)
            button.clicked.connect(handler)
            view_row.addWidget(button)
        layout.addLayout(view_row)
        controls = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск Deployment / Namespace / Pod")
        self.search.textChanged.connect(self.render_matrix)
        controls.addWidget(self.search)
        self.status_filters = {}
        for status in ("Running", "Pending", "Failed", "Unknown"):
            checkbox = QCheckBox("Error" if status == "Failed" else status)
            checkbox.setChecked(self.preferences.get("filters", {}).get(status, True))
            checkbox.stateChanged.connect(self.render_matrix)
            controls.addWidget(checkbox)
            self.status_filters[status] = checkbox
        self.recent = QCheckBox("Недавние рестарты")
        self.recent.setChecked(self.preferences.get("recent", True))
        self.recent.stateChanged.connect(self.render_matrix)
        controls.addWidget(self.recent)
        self.problem_only = QCheckBox("Только проблемные")
        self.problem_only.stateChanged.connect(self.render_matrix)
        controls.addWidget(self.problem_only)
        self.hide_empty = QCheckBox("Hide empty")
        self.hide_empty.stateChanged.connect(self.render_matrix)
        controls.addWidget(self.hide_empty)
        scope = QPushButton("Фильтры ресурсов")
        scope.clicked.connect(self._select_scope)
        controls.addWidget(scope)
        refresh = QPushButton("Обновить")
        refresh.clicked.connect(self.refresh)
        controls.addWidget(refresh)
        layout.addLayout(controls)
        options = QHBoxLayout()
        options.addWidget(QLabel("Автообновление"))
        self.refresh_interval = QComboBox()
        for label, ms in (("Выкл", 0), ("5 сек", 5000), ("10 сек", 10000), ("30 сек", 30000), ("1 мин", 60000), ("5 мин", 300000)):
            self.refresh_interval.addItem(label, ms)
        self.refresh_interval.setCurrentIndex(max(0, self.refresh_interval.findData(self.preferences.get("refresh_ms", 10000))))
        self.refresh_interval.currentIndexChanged.connect(self._set_refresh_interval)
        options.addWidget(self.refresh_interval)
        options.addWidget(QLabel("Ширина Deployment"))
        self.deployment_width = QSpinBox()
        self.deployment_width.setRange(100, 500)
        self.deployment_width.setValue(self.preferences.get("deployment_width", 150))
        self.deployment_width.valueChanged.connect(self._set_matrix_dimensions)
        options.addWidget(self.deployment_width)
        options.addWidget(QLabel("Высота заголовка"))
        self.header_height = QSpinBox()
        self.header_height.setRange(40, 250)
        self.header_height.setValue(self.preferences.get("header_height", 80))
        self.header_height.valueChanged.connect(self._set_matrix_dimensions)
        options.addWidget(self.header_height)
        self.summary_label = QLabel("Total: 0")
        options.addWidget(self.summary_label)
        options.addStretch()
        layout.addLayout(options)
        self.matrix = QTableWidget()
        self.matrix.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.matrix.cellDoubleClicked.connect(self.open_matrix_cell)
        self.matrix.horizontalHeader().setSectionsMovable(True)
        self.matrix.verticalHeader().setSectionsMovable(True)
        self.matrix.horizontalHeader().sectionMoved.connect(self._column_moved)
        self.matrix.verticalHeader().sectionMoved.connect(self._row_moved)
        self.matrix.horizontalHeader().sectionResized.connect(self._column_resized)
        self.matrix.verticalHeader().sectionResized.connect(self._row_resized)
        layout.addWidget(self.matrix)
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
        self.render_matrix()

    def _switch_view(self, index):
        if 0 <= index < len(self.views.views):
            self.views.active_id = self.views.views[index]["id"]
            self.views.persist()
            self._save_preferences()
            self._load_view_controls()

    def _move_view(self, source, target):
        view = self.views.views.pop(source)
        self.views.views.insert(target, view)
        self.views.persist()
        self._save_preferences()

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
                count = self.views.import_data(json.loads(Path(path).read_text(encoding="utf-8")), self.store.clusters())
                for view in self.views.views[-count:]:
                    self.view_bar.addTab(view["name"])
                self.view_bar.setCurrentIndex(len(self.views.views) - count)
                self._save_preferences()
            except (OSError, ValueError, KeyError, TypeError) as exc:
                error(self, str(exc))

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
        if dialog.exec() == QDialog.DialogCode.Accepted:
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
        page = QWidget()
        self.tabs.addTab(page, title)
        layout = QVBoxLayout(page)
        controls = QHBoxLayout()
        cluster_box, namespace_box = QComboBox(), QComboBox()
        controls.addWidget(cluster_box)
        controls.addWidget(namespace_box)
        button = QPushButton("Обновить")
        controls.addWidget(button)
        layout.addLayout(controls)
        table = QTableWidget()
        table.setColumnCount(3)
        table.setHorizontalHeaderLabels(["Кластер", "Namespace", "Имя"])
        layout.addWidget(table)
        setattr(self, f"{kind}_controls", (cluster_box, namespace_box, table))
        cluster_box.currentIndexChanged.connect(lambda: self._load_namespaces_for(kind))
        namespace_box.currentIndexChanged.connect(lambda: self.load_resource_page(kind))
        button.clicked.connect(lambda: self.load_resource_page(kind))
        table.cellDoubleClicked.connect(lambda row, col: self.open_resource_row(kind, row))
        return page

    def _load_namespaces_for(self, kind):
        cluster_box, namespace_box, _ = getattr(self, f"{kind}_controls")
        cluster_id = cluster_box.currentData()
        namespace_box.clear()
        if not cluster_id:
            return
        self.background.submit(lambda: self.api.namespaces(cluster_id),
                               lambda values: namespace_box.addItems(values), lambda message: self.statusBar().showMessage(message))

    def load_resource_page(self, kind):
        cluster_box, namespace_box, table = getattr(self, f"{kind}_controls")
        cluster_id, namespace = cluster_box.currentData(), namespace_box.currentText()
        if not cluster_id or not namespace:
            return
        self.background.submit(lambda: self.api.list_resources(cluster_id, kind, namespace),
                               lambda values: self._render_resources(kind, values), lambda message: self.statusBar().showMessage(message))

    def _render_resources(self, kind, values):
        cluster_box, namespace_box, table = getattr(self, f"{kind}_controls")
        table.setRowCount(len(values))
        for row, value in enumerate(values):
            for col, text in enumerate((cluster_box.currentText(), namespace_box.currentText(), value.get("metadata", {}).get("name", ""))):
                table.setItem(row, col, item(text))
        table.resizeColumnsToContents()

    def open_resource_row(self, kind, row):
        cluster_box, namespace_box, table = getattr(self, f"{kind}_controls")
        name = table.item(row, 2).text()
        self.open_resource(cluster_box.currentData(), kind, namespace_box.currentText(), name)

    def _set_refresh_interval(self):
        ms = self.refresh_interval.currentData()
        self.timer.stop()
        if ms:
            self.timer.start(ms)
        self.preferences["refresh_ms"] = ms
        self._save_preferences()

    def _save_preferences(self):
        try:
            self.store.save_preferences(self.preferences)
        except OSError as exc:
            self.statusBar().showMessage(f"Настройки не сохранены: {exc}")
            if not getattr(self, "_storage_error_shown", False):
                self._storage_error_shown = True
                error(self, f"Нет прав записи в UserData; настройки не сохранены.\n{exc}")

    def _apply_theme(self):
        if self.preferences.get("theme", "dark") == "dark":
            self.setStyleSheet("QWidget { background: #151d2b; color: #e9eef7; } QLineEdit, QTextEdit, QTableWidget, QComboBox { background: #1d293b; border: 1px solid #34445a; } QPushButton { background: #26374e; border: 1px solid #49617d; border-radius: 4px; padding: 5px 10px; } QPushButton:hover { background: #34516e; } QHeaderView::section { background: #26374e; color: #e9eef7; padding: 5px; }")
        else:
            self.setStyleSheet("")

    def clusters_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Кластеры · Настройки")
        dialog.resize(780, 540)
        layout = QVBoxLayout(dialog)
        table = QTableWidget()
        table.setColumnCount(5)
        table.setHorizontalHeaderLabels(["Имя", "API server", "Пользователь", "Активен", "Вход"])
        layout.addWidget(table)
        def populate():
            clusters = self.store.clusters()
            table.setRowCount(len(clusters))
            for row, cluster in enumerate(clusters):
                for col, value in enumerate((cluster["name"], cluster["server"], cluster["username"], "Да" if cluster["enabled"] else "Нет", "Требуется пароль" if self.store.login_required(cluster) else "Активен")):
                    table.setItem(row, col, item(value))
            table.resizeColumnsToContents()
            return clusters
        clusters = populate()
        buttons = QHBoxLayout()
        def selected():
            row = table.currentRow()
            return clusters[row] if 0 <= row < len(clusters) else None
        def edit(cluster=None):
            nonlocal clusters
            if ClusterDialog(self, self.store, self.app_dir, cluster).exec() == QDialog.DialogCode.Accepted:
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
        journal = QPushButton("Журнал действий")
        def show_journal():
            try:
                records = self.store.recent_audit()
                content = "\n".join(f"{record.get('time', '')} · {record.get('action', '')} · {record.get('clusterId', '')} · {record.get('namespace', '')}/{record.get('name', '')}"
                                    for record in reversed(records)) or "Действий пока нет."
                TextDialog(dialog, "Журнал действий", content).exec()
            except Exception as exc:
                error(dialog, str(exc))
        journal.clicked.connect(show_journal)
        buttons.addWidget(journal)
        buttons.addStretch()
        layout.addLayout(buttons)
        theme = QCheckBox("Светлая тема")
        theme.setChecked(self.preferences.get("theme", "dark") == "light")
        def set_theme(checked):
            self.preferences["theme"] = "light" if checked else "dark"
            self._save_preferences()
            self._apply_theme()
        theme.toggled.connect(set_theme)
        layout.addWidget(theme)
        dialog.exec()

    def refresh(self):
        if self.tabs.currentIndex() == 0:
            self.refresh_matrix()
        elif self.tabs.currentIndex() == 1:
            self._refresh_resource_controls("quota")
        elif self.tabs.currentIndex() == 2:
            self._refresh_resource_controls("configmap")

    def _refresh_resource_controls(self, kind):
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
        def load():
            snapshot = {}
            failures = []
            for cluster in clusters:
                try:
                    namespaces = self.api.namespaces(cluster["id"])
                    data = {}
                    for namespace in namespaces:
                        try:
                            data[namespace] = {"deployments": self.api.deployments(cluster["id"], namespace),
                                               "pods": self.api.pods(cluster["id"], namespace),
                                               "replica_sets": self.api.replica_sets(cluster["id"], namespace)}
                        except Exception as exc:
                            failures.append(f"{cluster['name']}/{namespace}: {exc}")
                    snapshot[cluster["id"]] = {"name": cluster["name"], "namespaces": data}
                except Exception as exc:
                    failures.append(f"{cluster['name']}: {exc}")
            return snapshot, failures
        def done(result):
            self.loading_matrix = False
            self.snapshot, failures = result
            self.render_matrix()
            self.statusBar().showMessage("; ".join(failures[:3]) if failures else "Матрица обновлена")
        self.background.submit(load, done, lambda message: (setattr(self, "loading_matrix", False), error(self, message)))

    def render_matrix(self):
        if not hasattr(self, "snapshot"):
            return
        self._rendering_matrix = True
        self._save_view_controls()
        view = self.views.active
        columns = [(cluster_id, cluster["name"], namespace) for cluster_id, cluster in self.snapshot.items()
                   for namespace in cluster["namespaces"] if (view.get("selectedClusterIds") is None or cluster_id in view["selectedClusterIds"])
                   and (view.get("selectedNamespaces") is None or namespace in view["selectedNamespaces"])]
        order = view.get("namespaceOrder") or []
        columns.sort(key=lambda col: order.index(f"{col[0]}/{col[2]}") if f"{col[0]}/{col[2]}" in order else len(order))
        deployments = sorted({deployment.get("metadata", {}).get("name", "") for cluster_id, _, namespace in columns
                              for deployment in self.snapshot[cluster_id]["namespaces"][namespace]["deployments"]})
        if view.get("selectedDeployments") is not None:
            deployments = [name for name in deployments if name in view["selectedDeployments"]]
        deployment_order = view.get("deploymentOrder") or []
        deployments.sort(key=lambda name: deployment_order.index(name) if name in deployment_order else len(deployment_order))
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
        self.matrix.setHorizontalHeaderLabels(["Deployment"] + [f"{label}\n{ns}" for _, label, ns in columns])
        self.matrix_data.clear()
        visible_pod_count = 0
        for row, name in enumerate(deployments):
            self.matrix.setItem(row, 0, item(name))
            for col, (cluster_id, _, namespace) in enumerate(columns, 1):
                resources = self.snapshot[cluster_id]["namespaces"][namespace]
                deployment = next((d for d in resources["deployments"] if d.get("metadata", {}).get("name") == name), None)
                if not deployment:
                    continue
                deployment_uid = deployment.get("metadata", {}).get("uid")
                replica_set_to_deployment = {rs.get("metadata", {}).get("uid"): next((owner.get("uid") for owner in rs.get("metadata", {}).get("ownerReferences", []) if owner.get("controller") and owner.get("kind") == "Deployment"), None)
                                             for rs in resources["replica_sets"]}
                def belongs(pod):
                    owner = next((owner for owner in pod.get("metadata", {}).get("ownerReferences", []) if owner.get("controller")), {})
                    return (owner.get("uid") if owner.get("kind") == "Deployment" else replica_set_to_deployment.get(owner.get("uid"))) == deployment_uid
                pods = [pod for pod in resources["pods"] if belongs(pod)]
                filtered = []
                recent = False
                for pod in pods:
                    status = "Terminating" if pod.get("metadata", {}).get("deletionTimestamp") else pod.get("status", {}).get("phase", "Unknown")
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
                cell = item(f"{len(filtered)} Pod · {available}/{target}", color)
                cell.setToolTip("\n".join(p.get("metadata", {}).get("name", "") for p in filtered) or "Deployment без Pod")
                self.matrix.setItem(row, col, cell)
                self.matrix_data[(row, col)] = (cluster_id, namespace, name, deployment, filtered)
        if self.hide_empty.isChecked():
            for col in range(self.matrix.columnCount() - 1, 0, -1):
                if not any(self.matrix.item(row, col) and (self.matrix_data.get((row, col), (None, None, None, {}, []))[4] or self.matrix_data[(row, col)][3].get("spec", {}).get("replicas", 1) == 0) for row in range(self.matrix.rowCount())):
                    self.matrix.setColumnHidden(col, True)
                else:
                    self.matrix.setColumnHidden(col, False)
        else:
            for col in range(1, self.matrix.columnCount()):
                self.matrix.setColumnHidden(col, False)
        self.matrix.resizeColumnToContents(0)
        self.matrix.horizontalHeader().setMinimumSectionSize(100)
        self.summary_label.setText(f"Total: {visible_pod_count} · Deployment: {len(deployments)} · Namespace: {len(columns)}")
        self.matrix.setColumnWidth(0, self.preferences.get("deployment_width", 150))
        self.matrix.horizontalHeader().setFixedHeight(self.preferences.get("header_height", 80))
        for col, (cluster_id, _, namespace) in enumerate(columns, 1):
            width = view.get("columnWidths", {}).get(f"{cluster_id}/{namespace}")
            if width:
                self.matrix.setColumnWidth(col, width)
        for row, name in enumerate(deployments):
            height = view.get("rowHeights", {}).get(name)
            if height:
                self.matrix.setRowHeight(row, height)
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

    def open_matrix_cell(self, row, col):
        ref = self.matrix_data.get((row, col))
        if not ref:
            return
        cluster_id, namespace, name, deployment, pods = ref
        choices = ["Deployment"] + [pod.get("metadata", {}).get("name", "") for pod in pods]
        choice, accepted = QInputDialog.getItem(self, "Открыть ресурс", f"{namespace}/{name}", choices, 0, False)
        if accepted:
            self.open_resource(cluster_id, "deployment" if choice == "Deployment" else "pod", namespace, name if choice == "Deployment" else choice)

    def open_resource(self, cluster_id, kind, namespace, name):
        def done(resource):
            if kind == "quota":
                TextDialog(self, f"ResourceQuota {name}", yaml.safe_dump(resource, allow_unicode=True)).exec()
            else:
                ResourceDialog(self, self.api, self.background, cluster_id, kind, namespace, name, resource).exec()
                self.refresh()
        self.background.submit(lambda: self.api.resource(cluster_id, kind, namespace, name), done,
                               lambda message: error(self, message))

    def closeEvent(self, event):
        self.proxy.close()
        super().closeEvent(event)
