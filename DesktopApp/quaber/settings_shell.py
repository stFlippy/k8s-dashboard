"""Sectioned settings and session-only diagnostic controls."""
from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QPushButton, QStackedWidget, QVBoxLayout, QWidget)


class SettingsDialog(QDialog):
    def __init__(self, controller):
        super().__init__(controller)
        self.controller = controller
        self.code = ''
        self.code_field = None
        self.setWindowTitle('Настройки')
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.resize(1140, 720)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        body = QHBoxLayout()
        body.setSpacing(0)
        self.sections = QListWidget()
        self.sections.setObjectName('settingsSections')
        self.sections.setFixedWidth(224)
        self.sections.setStyleSheet('QListWidget#settingsSections {border:0; border-right:1px solid palette(mid); padding-top:14px;} QListWidget#settingsSections::item {padding:10px 18px;}')
        body.addWidget(self.sections)
        right = QWidget()
        right_box = QVBoxLayout(right)
        right_box.setContentsMargins(24, 20, 24, 20)
        right_box.setSpacing(20)
        self.heading = QLabel()
        font = self.heading.font()
        font.setBold(True)
        font.setPointSize(font.pointSize()+2)
        self.heading.setFont(font)
        right_box.addWidget(self.heading)
        self.pages = QStackedWidget()
        right_box.addWidget(self.pages, 1)
        body.addWidget(right, 1)
        layout.addLayout(body, 1)
        footer = QHBoxLayout()
        footer.setContentsMargins(16, 10, 16, 10)
        footer.addStretch()
        close = QPushButton('Закрыть')
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        layout.addLayout(footer)
        self.sections.currentRowChanged.connect(self.select_section)
        QApplication.instance().installEventFilter(self)
        self.finished.connect(lambda _: QApplication.instance().removeEventFilter(self))

    def add_section(self, title, page):
        self.pages.addWidget(page)
        self.sections.addItem(title)
        if self.sections.currentRow() < 0: self.sections.setCurrentRow(0)

    def select_section(self, row):
        if row < 0: return
        self.pages.setCurrentIndex(row)
        self.heading.setText(self.sections.item(row).text())

    def eventFilter(self, watched, event):
        if (event.type() != QEvent.KeyPress or not isinstance(watched, QWidget)
                or watched.window() is not self or getattr(self.controller, '_debug_tools_unlocked', False)):
            return False
        if event.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            self.code = ''
            return False
        field = watched if isinstance(watched, QLineEdit) else None
        # Never intercept credentials in child dialogs or text already entered.
        if field and (field.echoMode() != QLineEdit.Normal or
                      (field.text() and (field is not self.code_field or field.text() != self.code))):
            self.code = ''
            return False
        if field is not self.code_field:
            self.code = ''
        self.code_field = field
        candidate = self.code + event.text()
        self.code = candidate if '1994'.startswith(candidate) else ('1' if event.text() == '1' else '')
        if self.code == '1994':
            if field:
                field.clear()
            self.controller._debug_tools_unlocked = True
            self.reveal_debug()
            self.code = ''
            return True
        return field is None and bool(event.text() and event.text().isdigit())
