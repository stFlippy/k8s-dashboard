"""Shared secret cleanup for credential dialogs."""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QDialog


class SecretDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

    def _clear_secrets(self):
        fields = [self.password] if hasattr(self, 'password') else self.passwords
        for field in fields:
            field.clear()

    def closeEvent(self, event):
        if self.busy:
            event.ignore()
            return
        self._clear_secrets()
        super().closeEvent(event)

    def reject(self):
        if not self.busy:
            self._clear_secrets()
            super().reject()
