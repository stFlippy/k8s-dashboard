"""Window actions shared by the toolbar, keyboard and mouse navigation."""
from PyQt5.QtCore import QObject, QEvent, Qt, QTimer
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import QAction, QApplication, QWidget, QPushButton, QMessageBox, QVBoxLayout, QCheckBox
from .widgets import DisabledHintButton


class ShellActions(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        footer = QWidget(window.sidebar)
        footer.setObjectName("sidebarFooter")
        layout = QVBoxLayout(footer)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.mode = DisabledHintButton("Режим администратора")
        self.mode.setCheckable(True)
        self.mode.setObjectName("adminMode")
        self.mode.toggled.connect(self.toggle_mode)
        layout.addWidget(self.mode)
        help_button = QPushButton("help")
        self.help_button = help_button
        help_button.setObjectName("helpButton")
        help_button.setToolTip("Справка и горячие клавиши · F1")
        help_button.clicked.connect(self.help)
        layout.addWidget(help_button)
        window.sidebar.layout().addWidget(footer)
        for text, shortcut, callback in (
                ("Назад", "Alt+Left", window.go_back),
                ("Обновить экран", "F5", self.refresh),
                ("Найти", "Ctrl+F", self.search),
                ("Сохранить текущую вкладку", "Ctrl+S", self.save),
                ("Следующее совпадение", "F3", lambda: self.find(False)),
                ("Предыдущее совпадение", "Shift+F3", lambda: self.find(True)),
                ("Заблокировать изменения", "Ctrl+Shift+L", lambda: window.set_read_only(True)),
                ("Клавиши", "F1", self.help)):
            action = QAction(text, window)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(callback)
            window.addAction(action)
        self.app = QApplication.instance()
        self.app.installEventFilter(self)
        self.idle_timer = QTimer(self)
        self.idle_timer.setSingleShot(True)
        self.idle_timer.setInterval(60000)
        self.idle_timer.timeout.connect(lambda: window.set_read_only(True))
        self.update_mode()

    def panel(self):
        return getattr(self.window, "_resource_panel", None)

    def tab_name(self, panel):
        return panel.tabs.tabText(panel.tabs.currentIndex()) if panel else ""

    def update_mode(self):
        readonly = self.window.preferences.get("read_only", True)
        if readonly:
            self.idle_timer.stop()
        else:
            self.idle_timer.start()
        blocked = self.mode.blockSignals(True)
        self.mode.setChecked(not readonly)
        self.mode.blockSignals(blocked)
        self.mode.setToolTip(self.mode_tooltip())
        self.window.sidebar.setProperty("writeEnabled", not readonly)
        self.window._update_brand()
        self.window._apply_sidebar()
        self.window.refresh_write_controls()

    def mode_tooltip(self):
        return ("Включить режим администратора: одно изменение или одна минута Terminal. Потребуется пример; права определяет Kubernetes."
                if self.window.preferences.get("read_only", True) else
                "Выключить режим администратора и отменить неотправленные изменения. Запущенные операции продолжаются.")

    def toggle_mode(self, *_):
        if not self.window.preferences.get("read_only", True):
            self.window.set_read_only(True)
            return
        if self.window._write_operation is not None:
            self.window.statusBar().showMessage("Дождитесь завершения операции.", 5000)
            self.update_mode()
            return
        from .ui import confirm
        target = self.window.admin_access.target()
        if target is None:
            self.update_mode()
            return
        if confirm(self.window, "Разрешить изменения?",
                   "Разрешить изменения?", arithmetic=True):
            self.update_mode()
            self.window.admin_access.activate(target)
        else:
            self.update_mode()

    def refresh(self):
        panel = self.panel()
        if not panel:
            page = self.window.tabs.currentWidget()
            if page in (self.window.dumps_page, self.window.versions_page):
                page.refresh_data(force=True)
            else:
                self.window.refresh()
        elif self.tab_name(panel) == "Логи":
            panel.load_logs()
        elif callable(getattr(panel.tabs.currentWidget(), "refresh_data", None)):
            panel.tabs.currentWidget().refresh_data()
        elif self.tab_name(panel) == "Pods":
            panel.reload_deployment_pods()
        else:
            panel.refresh_object()

    def search(self):
        panel = self.panel()
        if panel and self.tab_name(panel) == "Логи":
            field = panel.log_search
        elif panel and self.tab_name(panel) == "YAML":
            panel.yaml_editor.show_search()
            return
        elif not panel and self.window.tabs.currentIndex() == 0:
            field = self.window.search
        else:
            field = getattr(panel.tabs.currentWidget() if panel else self.window.tabs.currentWidget(), "search_field", None)
        if field is not None:
            field.setFocus()
            field.selectAll()
        else:
            self.window.statusBar().showMessage("На этом экране поиск пока недоступен.", 4000)

    def find(self, backwards):
        panel = self.panel()
        if panel and self.tab_name(panel) == "Логи":
            panel.log_view.find_next(backwards)
        elif panel and self.tab_name(panel) == "YAML":
            panel.yaml_editor.find_match(backwards)
        elif not panel and self.window.tabs.currentWidget() is self.window.log_panel:
            self.window.log_panel.find_next(backwards)

    def save(self):
        panel = self.panel()
        if panel and self.tab_name(panel) == "YAML":
            panel.save_yaml()
        elif panel and self.tab_name(panel) == "Логи":
            panel.export_logs()

    def help(self):
        QMessageBox.information(self.window, "Клавиши и основные понятия",
            "Alt+← / кнопка мыши Назад — вернуться\nF5 — обновить текущий экран\n"
            "Ctrl+F — поиск; F3 / Shift+F3 — следующее / предыдущее совпадение\n"
            "Ctrl+S — применить YAML с проверкой или сохранить логи\n"
            "Ctrl+Shift+L — заблокировать изменения\nCtrl+J — журнал\n"
            "Enter / двойной клик — открыть строку; Shift+F10 — её меню\n\n"
            "Кластер — окружение. Namespace — область внутри кластера.\n"
            "Deployment задаёт шаблон и число копий приложения. Pod — экземпляр с контейнерами.\n"
            "«Удалить подключение» удаляет только локальную запись, не кластер.")

    def eventFilter(self, watched, event):
        if (isinstance(watched, QWidget) and (watched is self.window or self.window.isAncestorOf(watched))
                and event.type() in (QEvent.Type.KeyPress, QEvent.Type.MouseButtonPress,
                                     QEvent.Type.MouseMove, QEvent.Type.Wheel, QEvent.Type.TouchBegin)
                and not self.window.preferences.get("read_only", True)):
            modal = QApplication.activeModalWidget()
            if modal is None or not modal.property("adminReview"):
                self.idle_timer.start()
        if (event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease)
                and event.button() == Qt.MouseButton.BackButton and isinstance(watched, QWidget)
                and (watched is self.window or self.window.isAncestorOf(watched))
                and QApplication.activeModalWidget() is None):
            if event.type() == QEvent.Type.MouseButtonPress:
                self.window.go_back()
            return True
        return super().eventFilter(watched, event)
