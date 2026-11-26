"""Application journal page, independent of cluster Terminal output."""
from pathlib import Path
import json
from PyQt5.QtCore import QTimer, pyqtSlot
from PyQt5.QtGui import QTextCursor, QTextDocument, QPalette, QColor
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QCheckBox,
                            QPlainTextEdit, QApplication, QFileDialog, QMessageBox, QLineEdit)
from .themes import code_font, theme


class LogPanel(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("applicationLogPage")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        bar = QWidget()
        bar.setObjectName("logTitlebar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 3, 8, 3)
        row.addWidget(QLabel("ЖУРНАЛ ПРИЛОЖЕНИЯ"))
        self.search_field = QLineEdit()
        self.search_field.setObjectName('applicationLogSearch')
        self.search_field.setPlaceholderText("Поиск в журнале…")
        self.search_field.setClearButtonEnabled(True)
        self.search_field.textChanged.connect(lambda: self.find_next(restart=True))
        self.search_field.returnPressed.connect(self.find_next)
        row.addWidget(self.search_field)
        for text, backwards in (("↑", True), ("↓", False)):
            find = QPushButton(text)
            find.setFixedWidth(28)
            find.clicked.connect(lambda checked=False, backwards=backwards: self.find_next(backwards))
            row.addWidget(find)
        self.search_status = QLabel()
        row.addWidget(self.search_status)
        row.addStretch()
        self.auto_scroll = QCheckBox("Автопрокрутка")
        self.auto_scroll.setChecked(True)
        row.addWidget(self.auto_scroll)
        button = QPushButton("Сохранить…")
        button.clicked.connect(self.export)
        row.addWidget(button)
        diagnostics = QPushButton('Диагностика…')
        diagnostics.setToolTip('Сохранить локальные счётчики запросов и кэша без обращения к кластерам.')
        diagnostics.clicked.connect(self.export_diagnostics)
        row.addWidget(diagnostics)
        layout.addWidget(bar)
        self.output = QPlainTextEdit()
        self.output.setObjectName("applicationLog")
        self.output.setReadOnly(True)
        self.output.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.output.setMaximumBlockCount(1000)
        self.output.setFont(code_font())
        layout.addWidget(self.output, 1)
        self.setMinimumHeight(100)
        self.apply_theme(getattr(parent, 'preferences', {}).get('theme') == 'light')

    def apply_theme(self, light):
        palette = self.search_field.palette()
        color = QColor(theme(light)['muted'])
        for group in (QPalette.Active, QPalette.Inactive, QPalette.Disabled):
            palette.setColor(group, QPalette.PlaceholderText, color)
        self.search_field.setPalette(palette)

    def export_diagnostics(self):
        window = self.window()
        api = getattr(window, 'api', None)
        policy = getattr(api, 'request_policy', None)
        data = {'requests': policy.diagnostics() if policy is not None else {}}
        cache = getattr(api, 'cache', None)
        if cache is not None:
            with cache.lock:
                data['cache'] = dict(cache.stats, entries=len(cache.entries), active=len(cache.active),
                                     bytes=sum(cache.weights.values()))
        controller = getattr(window, 'monitoring', None)
        if controller is not None:
            with controller.policy.lock:
                data['monitoring'] = {'state': controller.policy.state, 'epoch': controller.policy.epoch,
                    'reasons': sorted(controller.policy.reasons), 'idle_minutes': controller.policy.minutes}
        path, _ = QFileDialog.getSaveFileName(self, 'Сохранить диагностику', 'quaber-diagnostics.json', 'JSON (*.json)')
        if path:
            try:
                Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            except OSError as exc:
                QMessageBox.warning(self, 'Диагностика', str(exc))

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._scroll_to_end)

    def _scroll_to_end(self):
        self.output.verticalScrollBar().setValue(self.output.verticalScrollBar().maximum())

    def find_next(self, backwards=False, *, restart=False):
        text = self.search_field.text()
        if not text:
            self.search_status.clear()
            return
        self.auto_scroll.setChecked(False)
        flags = QTextDocument.FindFlag.FindBackward if backwards else QTextDocument.FindFlag(0)
        if restart:
            cursor = self.output.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            self.output.setTextCursor(cursor)
        found = self.output.find(text, flags)
        if not found:
            cursor = self.output.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End if backwards else QTextCursor.MoveOperation.Start)
            self.output.setTextCursor(cursor)
            found = self.output.find(text, flags)
        self.search_status.setText("" if found else "Не найдено")

    @pyqtSlot(str)
    def append(self, message):
        scroll = self.output.verticalScrollBar()
        previous = scroll.value()
        follow = self.auto_scroll.isChecked() and previous >= scroll.maximum()-2
        self.output.appendPlainText(message)
        scroll.setValue(scroll.maximum() if follow else previous)

    def clear(self):
        self.output.clear()

    def copy(self):
        QApplication.clipboard().setText(self.output.toPlainText())

    def export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить журнал", "quaber.log", "Log (*.log);;Text (*.txt)")
        if path:
            try:
                Path(path).write_text(self.output.toPlainText(), encoding="utf-8")
            except OSError:
                QMessageBox.warning(self, "Quaber", "Не удалось сохранить журнал в выбранный файл.")
