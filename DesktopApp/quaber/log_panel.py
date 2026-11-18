"""Application journal page, independent of cluster Terminal output."""
from pathlib import Path
from PyQt5.QtCore import Qt, QTimer, pyqtSlot
from PyQt5.QtGui import QTextCursor, QTextDocument
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QCheckBox,
                            QPlainTextEdit, QApplication, QFileDialog, QMessageBox, QLineEdit)
from .themes import code_font


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
        layout.addWidget(bar)
        self.output = QPlainTextEdit()
        self.output.setObjectName("applicationLog")
        self.output.setReadOnly(True)
        self.output.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.output.setMaximumBlockCount(1000)
        self.output.setFont(code_font())
        layout.addWidget(self.output, 1)
        self.setMinimumHeight(100)

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
