"""One explicit target and one decision for a resource mutation."""
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QDialog, QDialogButtonBox, QFrame, QLabel, QSizePolicy, QVBoxLayout

from .themes import write_background
from .diff_view import DiffView


class ResourceConfirmation(QDialog):
    def __init__(self, parent, title, target, message, details=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        self.resize(580, 220)
        layout = QVBoxLayout(self)
        controller = getattr(parent, "controller", parent)
        self.controller = controller
        self.setProperty("adminReview", bool(details))
        light = getattr(controller, "preferences", {}).get("theme") == "light"
        banner = QFrame()
        banner.setObjectName("operationTarget")
        banner.setStyleSheet(f"QFrame#operationTarget {{ background:{write_background(light)}; border-radius:4px; }}"
                            "QFrame#operationTarget QLabel { color:#FFFFFF; background:transparent; }")
        target_layout = QVBoxLayout(banner)
        target_layout.addWidget(QLabel("Изменения будут выполнены здесь:"))
        self.target_label = QLabel(target)
        self.target_label.setObjectName("operationTargetText")
        self.target_label.setTextFormat(Qt.TextFormat.PlainText)
        self.target_label.setWordWrap(True)
        self.target_label.setMinimumWidth(0)
        self.target_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.target_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.target_label.setStyleSheet("font-weight:700;")
        target_layout.addWidget(self.target_label)
        layout.addWidget(banner)
        description = QLabel(message)
        description.setTextFormat(Qt.TextFormat.PlainText)
        description.setWordWrap(True)
        layout.addWidget(description)
        if details:
            preview = DiffView(details, self)
            layout.addWidget(preview, 1)
            available = self.screen().availableGeometry()
            self.resize(min(1100, max(460, available.width()-100)), min(600, max(300, available.height()-100)))
        self._requires_access = bool(details) and hasattr(controller, "shell_actions")
        self.access_status = QLabel()
        self.access_status.setWordWrap(True)
        layout.addWidget(self.access_status)
        from PyQt5.QtWidgets import QPushButton
        self.renew = QPushButton("Продлить режим администратора")
        self.renew.clicked.connect(self._renew_access)
        self.renew.setVisible(self._requires_access)
        layout.addWidget(self.renew)
        self.access_timer = QTimer(self)
        self.access_timer.setInterval(200)
        self.access_timer.timeout.connect(self._update_access)
        self.access_timer.start()
        self._update_access()
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No)
        yes = self.buttons.button(QDialogButtonBox.StandardButton.Yes)
        no = self.buttons.button(QDialogButtonBox.StandardButton.No)
        yes.setText("Да")
        no.setText("Нет")
        yes.setAutoDefault(False)
        no.setDefault(True)
        no.setFocus()
        self.buttons.accepted.connect(self._accept_with_access)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _update_access(self):
        if not self._requires_access:
            self.access_status.hide()
            return
        readonly = self._access_expired()
        actions = self.controller.shell_actions
        remaining = actions.admin_time_remaining()
        self.access_status.setText("Доступ истёк. Продлите режим администратора для применения или отмените. Черновик сохранён." if readonly else
                                   'Редактирование YAML: таймер администратора приостановлен.' if actions.yaml_editing() else
                                   f"Режим администратора: {(remaining+999)//1000} сек")
        self.renew.setVisible(self._requires_access and not actions.yaml_editing())

    def _renew_access(self):
        if self._access_expired():
            self.controller.set_read_only(True)
            self.controller.shell_actions.toggle_mode()
        else:
            self.controller.shell_actions.sync_admin_timer(activity=True)
        self._update_access()

    def _accept_with_access(self):
        if self._requires_access and self._access_expired():
            self.controller.set_read_only(True)
            self._update_access()
            return
        self.accept()

    def _access_expired(self):
        return (self.controller.preferences.get("read_only", True)
                or self.controller.shell_actions.admin_time_remaining() <= 0)


def confirm_change(parent, title, target, message, details=None):
    dialog = ResourceConfirmation(parent, title, target, message, details)
    try:
        return dialog.exec_() == QDialog.DialogCode.Accepted
    finally:
        dialog.deleteLater()
