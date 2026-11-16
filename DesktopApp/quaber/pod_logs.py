"""Pod log viewer: source-aware snapshots, independent polling and following."""
import re
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QEvent, QTimer
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QPlainTextEdit, QFileDialog, QMessageBox)
from .widgets import FullCheckBox, AppComboBox
from .themes import code_font
from .kubernetes import LogTooLarge
from .container_selection import preferred_container

TAIL_LINES = 1000
MAX_DISPLAY_LINES = 50000


class PodLogs(QWidget):
    def __init__(self, owner, containers):
        super().__init__(owner)
        self.setObjectName("podLogView")
        self.owner = owner
        self.loading = False
        self.again = False
        self.generation = 0
        self.source = None
        self.pending_text = None
        self.matches = []
        self.match_index = -1
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(2)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        row.addWidget(QLabel("Контейнер"))
        self.container = AppComboBox()
        self.container.addItems(containers)
        regular = [c.get("name", "") for c in getattr(owner, "resource", {}).get("spec", {}).get("containers", [])]
        preferred = preferred_container(regular or containers)
        if preferred in containers:
            self.container.setCurrentText(preferred)
        self.container.setMinimumWidth(100)
        self.container.setMaximumWidth(210)
        self.container.setToolTip("Контейнер для логов. Выбор не меняет контейнер для terminal.")
        row.addWidget(self.container)
        self.search = QLineEdit()
        self.search.setMaximumWidth(300)
        self.search.setMinimumWidth(130)
        self.search.setPlaceholderText("Поиск в логе…")
        self.search.setClearButtonEnabled(True)
        self.search.setToolTip("Enter / F3 — следующее совпадение; Shift+Enter / Shift+F3 — предыдущее.")
        row.addWidget(self.search, 1)
        self.count = QLabel("0/0")
        row.addWidget(self.count)
        for label, backwards in (("↑", True), ("↓", False)):
            button = QPushButton(label)
            button.setMaximumWidth(32)
            button.clicked.connect(lambda _, back=backwards: self.find_next(back))
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self.fragment_label = QLabel("Последние 1000 строк")
        row.addWidget(self.fragment_label)
        self.expanded = FullCheckBox("full log")
        self.expanded.setToolTip("Показать весь доступный лог. Если он больше 4 МиБ или 50000 строк, будет предложено скачивание.")
        row.addWidget(self.expanded)
        self.follow = FullCheckBox("autoscrolling")
        self.follow.setChecked(True)
        self.follow.setToolTip("Следовать за концом лога. Действия в тексте/поиске отключают прокрутку, но не получение новых данных.")
        row.addWidget(self.follow)
        self.previous = FullCheckBox("Предыдущий запуск")
        row.addWidget(self.previous)
        self.export_button = QPushButton("Сохранить log…")
        self.export_button.setEnabled(False)
        self.export_button.setToolTip("Сохранить показанный фрагмент лога.")
        self.export_button.clicked.connect(self.export)
        row.addWidget(self.export_button)
        row.addStretch()
        controls = QWidget()
        controls.setLayout(row)
        if hasattr(owner.controller, "_scroll_controls"):
            layout.addWidget(owner.controller._scroll_controls(controls))
        else:
            layout.addWidget(controls)
        self._full_prompt = False
        self._download_running = False
        self.error_label = QLabel()
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)
        self.download_status = QLabel()
        self.download_status.setTextFormat(Qt.TextFormat.PlainText)
        self.download_status.setWordWrap(True)
        self.download_status.hide()
        layout.addWidget(self.download_status)
        self.output = QPlainTextEdit()
        self.output.setObjectName("podLogs")
        self.output.setFont(code_font())
        self.output.setReadOnly(True)
        self.output.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.output.setMaximumBlockCount(MAX_DISPLAY_LINES)
        self.output.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.output.viewport().installEventFilter(self)
        self.output.installEventFilter(self)
        self.search.installEventFilter(self)
        self.output.verticalScrollBar().actionTriggered.connect(self.pause_follow)
        self.output.verticalScrollBar().sliderPressed.connect(self.pause_follow)
        self.output.horizontalScrollBar().actionTriggered.connect(self.pause_follow)
        self.output.horizontalScrollBar().sliderPressed.connect(self.pause_follow)
        layout.addWidget(self.output, 1)
        self.status = QLabel("Загрузка логов…")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(180)
        self.search_timer.timeout.connect(self.update_search)
        self.search.textChanged.connect(lambda _: self.search_timer.start())
        self.search.textEdited.connect(self.pause_follow)
        self.search.returnPressed.connect(self.find_next)
        self.follow.toggled.connect(lambda enabled: self.resume_follow() if enabled else None)
        self.container.currentTextChanged.connect(self.load)
        self.previous.toggled.connect(self.load)
        self.expanded.toggled.connect(self.load)
        self.timer = QTimer(self)
        self.timer.setInterval(5000)
        self.timer.timeout.connect(lambda: self.load() if self.isVisible() else None)
        self.timer.start()
        QTimer.singleShot(0, self.load)

    def pause_follow(self, *_):
        self.follow.setChecked(False)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.resume_follow)

    def eventFilter(self, watched, event):
        from PyQt6 import sip
        output = getattr(self, "output", None)
        if output is None or sip.isdeleted(output):
            return super().eventFilter(watched, event)
        if (watched is self.search and event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.find_next(True)
            return True
        if watched is self.search and event.type() == QEvent.Type.MouseButtonPress:
            self.pause_follow()
        if (watched is self.output.viewport() and event.type() in (QEvent.Type.Wheel, QEvent.Type.TouchBegin)) or (
                watched is self.output and event.type() == QEvent.Type.KeyPress):
            self.pause_follow()
        if (watched is self.output.viewport() and event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton):
            self.pause_follow()
        return super().eventFilter(watched, event)

    def resume_follow(self):
        blocked = self.follow.blockSignals(True)
        self.follow.setChecked(True)
        self.follow.blockSignals(blocked)
        if self.pending_text is not None:
            self.show_text(self.pending_text)
            self.pending_text = None
        self.output.verticalScrollBar().setValue(self.output.verticalScrollBar().maximum())

    def load(self, *_):
        if self._full_prompt:
            return
        wanted = (self.container.currentText(), self.previous.isChecked(), None if self.expanded.isChecked() else TAIL_LINES)
        if wanted != self.source:
            self.generation += 1
            self.source = wanted
            self.fragment_label.setText("Полный доступный лог" if wanted[2] is None else "Последние 1000 строк")
            self.pending_text = None
            self.output.clear()
            self.resume_follow()
            self.export_button.setEnabled(False)
            self.error_label.hide()
            self.status.setText(f"Загрузка: {wanted[0]} · {'предыдущий' if wanted[1] else 'текущий'} запуск…")
            self.count.setText("0/0")
            self.matches = []
        if self.loading:
            self.again = True
            return
        if not wanted[0]:
            self.status.setText("В Pod нет доступных контейнеров.")
            return
        self.loading, self.again = True, False
        generation = self.generation
        def finish():
            self.loading = False
            if self.again:
                QTimer.singleShot(0, self.load)
            return generation == self.generation
        def done(text):
            if not finish():
                return
            if isinstance(text, LogTooLarge):
                self.offer_download(wanted)
                return
            self.error_label.clear()
            self.error_label.hide()
            self.output.setToolTip("")
            # Keep the review position stable while still receiving newer snapshots.
            if not self.follow.isChecked() and self.output.toPlainText() and text != self.output.toPlainText():
                self.pending_text = text
                self.status.setText("Получен новый фрагмент. Текущий оставлен для чтения; включите autoscrolling для перехода к новым строкам.")
            else:
                self.pending_text = None
                self.show_text(text)
        def failed(message):
            if not finish():
                return
            self.error_label.setText(f"Логи недоступны: {str(message)[:500]}. Повтор через 5 сек.")
            self.error_label.show()
            if self.output.toPlainText():
                self.status.setText("Обновление не удалось. Показан последний успешный фрагмент этого источника.")
            else:
                self.status.setText("Нет загруженного фрагмента этого источника.")
        def read():
            try:
                text = self.owner.api.logs(self.owner.cluster_id, self.owner.namespace, self.owner.name, *wanted)
                if text.count("\n") + bool(text and not text.endswith("\n")) > MAX_DISPLAY_LINES:
                    return LogTooLarge("Слишком много строк для просмотра.")
                return text
            except LogTooLarge as exc:
                return exc
        self.owner.background.submit(read, done, failed)

    def offer_download(self, source):
        self._full_prompt = True
        blocked = self.expanded.blockSignals(True)
        self.expanded.setChecked(False)
        self.expanded.blockSignals(blocked)
        try:
            if self._download_running:
                return
            answer = QMessageBox.question(self, "Лог слишком большой",
                "Полный лог превышает объём просмотра (4 МиБ / 50000 строк). Скачать его в файл?\nВ окне будут показаны последние 1000 строк.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
            filename = f"{self.owner.name}-{source[0]}-{'previous' if source[1] else 'current'}-full.log"
            path, _ = QFileDialog.getSaveFileName(self, "Скачать полный доступный лог", filename, "Логи (*.log *.txt)")
            if not path:
                return
            self._download_running = True
            self.download_status.setText("Скачивание полного лога…")
            self.download_status.show()
            def done(count):
                self._download_running = False
                self.download_status.setText(f"Лог сохранён: {path} ({count} байт).")
                self.download_status.show()
            def failed(message):
                self._download_running = False
                self.download_status.setText("Не удалось скачать лог: " + str(message)[:500])
                self.download_status.show()
            self.owner.background.submit(lambda: self.owner.api.download_logs(self.owner.cluster_id, self.owner.namespace,
                self.owner.name, source[0], source[1], path), done, failed)
        finally:
            self._full_prompt = False
            self.load()


    def show_text(self, text):
        if self.output.toPlainText() != text:
            self.output.setPlainText(text)
            self.update_search(select=False)
        shown = self.output.toPlainText()
        self.export_button.setEnabled(bool(shown))
        source = self.source or (self.container.currentText(), self.previous.isChecked(), None)
        self.fragment_label.setText("Полный доступный лог" if source[2] is None else "Последние 1000 строк")
        self.status.setText(f"{source[0]} · {'предыдущий' if source[1] else 'текущий'} запуск · "
            f"обновлено {datetime.now():%H:%M:%S} · {len(shown.splitlines())} строк · "
            f"{len(shown.encode('utf-8'))/1024:.0f} КиБ · ограниченный фрагмент" if shown else "В этом фрагменте записей нет.")
        if self.follow.isChecked():
            self.output.verticalScrollBar().setValue(self.output.verticalScrollBar().maximum())

    def update_search(self, select=True):
        needle = self.search.text()
        self.matches, self.match_index = [], -1
        self.match_text = self.output.toPlainText()
        if needle:
            for match in re.finditer(re.escape(needle), self.match_text, re.IGNORECASE):
                self.matches.append(match.span())
                if len(self.matches) == 10000:
                    break
        self.count.setText(f"0/{len(self.matches)}{'+' if len(self.matches) == 10000 else ''}")
        if select and self.matches:
            self.find_next()

    def find_next(self, backwards=False):
        if self.search_timer.isActive():
            self.search_timer.stop()
            self.update_search(select=False)
        if not self.matches:
            return
        self.pause_follow()
        self.match_index = ((len(self.matches)-1 if backwards else 0) if self.match_index < 0 else
                            (self.match_index + (-1 if backwards else 1)) % len(self.matches))
        start, end = self.matches[self.match_index]
        cursor = self.output.textCursor()
        # QTextCursor positions count UTF-16 code units, including surrogate pairs.
        cursor.setPosition(len(self.match_text[:start].encode('utf-16-le')) // 2)
        cursor.setPosition(len(self.match_text[:end].encode('utf-16-le')) // 2, QTextCursor.MoveMode.KeepAnchor)
        self.output.setTextCursor(cursor)
        self.output.ensureCursorVisible()
        self.count.setText(f"{self.match_index+1}/{len(self.matches)}{'+' if len(self.matches) == 10000 else ''}")

    def export(self):
        source = self.source or (self.container.currentText(), self.previous.isChecked(), None)
        filename = f"{self.owner.name}-{source[0]}-{'previous' if source[1] else 'current'}-{datetime.now():%Y%m%d-%H%M%S}.log"
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить log", filename, "Логи (*.log *.txt)")
        if path:
            try:
                Path(path).write_text(self.output.toPlainText(), encoding="utf-8")
            except OSError as exc:
                QMessageBox.warning(self, "Не удалось сохранить логи", str(exc))
