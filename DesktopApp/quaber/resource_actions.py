"""Actions capture resource identity before a menu opens, never a mutable row."""
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox, QDialog, QVBoxLayout, QLabel, QScrollArea, QWidget, QDialogButtonBox

from .safety import check_identity
from .widgets import ElidedButton
from .confirmations import confirm_change


def choose_location(parent, entries):
    dialog = QDialog(parent)
    dialog.setWindowTitle("Расположение ресурса")
    layout = QVBoxLayout(dialog)
    layout.addWidget(QLabel("Выберите расположение"))
    body = QWidget()
    buttons = QVBoxLayout(body)
    selected = []
    for label, identity in entries:
        button = ElidedButton(label)
        cluster_name = getattr(parent, "snapshot", {}).get(identity[0], {}).get("name")
        if cluster_name:
            complete_target = f"{cluster_name} / {identity[2]} / {identity[3]}"
            button.setToolTip(complete_target)
            button.setAccessibleName(complete_target)
        button.clicked.connect(lambda checked=False, identity=tuple(identity): (selected.append(identity), dialog.accept()))
        buttons.addWidget(button)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setWidget(body)
    layout.addWidget(scroll)
    cancel = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
    cancel.rejected.connect(dialog.reject)
    layout.addWidget(cancel)
    dialog.resize(460, min(500, 125+44*len(entries)))
    try:
        return selected[0] if dialog.exec() == QDialog.DialogCode.Accepted and selected else None
    finally:
        dialog.deleteLater()


def confirm_pod_removal(parent, target, pod):
    owner = next((o for o in pod.get("metadata", {}).get("ownerReferences", []) if o.get("controller")), None)
    description = (f"Владелец: {owner.get('kind', '')} / {owner.get('name', '')}.\n"
                   "Удалить выбранный Pod с проверкой PDB? Возможен простой."
                   if owner else "Удалить Pod без контроллера? Автоматическое восстановление не гарантировано.")
    description += "\nДанные внутри контейнера могут быть потеряны."
    if confirm_change(parent, "Удалить Pod", f"Pod · {target}", description):
        return "restart" if owner else "delete"
    return None


class ResourceActions:
    def __init__(self, window):
        self.window = window

    def menu(self, identity):
        # A tuple owns its values even if an auto-refresh replaces every row.
        identity = tuple(identity)
        cid, kind, namespace, name, uid = identity
        menu = QMenu(self.window)
        menu.setToolTipsVisible(True)
        title = menu.addAction(f"{namespace} / {name}")
        title.setEnabled(False)
        tabs = (("Открыть обзор", "Обзор"), ("Открыть YAML", "YAML"))
        if kind == "pod":
            tabs = (("Открыть POD", "Обзор"), ("Логи", "Логи"), ("События", "События"),
                    ("terminal", "terminal"), ("Открыть YAML", "YAML"))
        elif kind == "deployment":
            tabs = (("Открыть обзор", "Обзор"), ("Экземпляры (Pods)", "Pods"),
                    ("События", "События"), ("Открыть YAML", "YAML"))
        for label, tab in tabs:
            menu.addAction(label, lambda checked=False, tab=tab:
                self.window.open_resource(cid, kind, namespace, name, tab, expected_uid=uid))
        menu.addSeparator()
        if kind != "pod":
            menu.addAction("Скопировать имя", lambda: QApplication.clipboard().setText(name))
        cluster = self.window.api.cluster(cid).get("name", cid)
        menu.addAction("Скопировать расположение", lambda:
            QApplication.clipboard().setText(f"{cluster} / {namespace} / {name}"))
        if kind == "pod":
            menu.addSeparator()
            action = menu.addAction("Удалить Pod…", lambda: self.delete_pod(identity))
            readonly = self.window.preferences.get("read_only", True)
            action.setEnabled(not readonly and bool(uid) and not self.window._pod_operations and self.window._write_operation is None
                              and getattr(self.window, "_replica_draft_owner", None) is None)
            action.setToolTip("Для изменений включите режим администратора внизу слева." if readonly else
                "Удалить один выбранный Pod. Управляемые Pod — с проверкой бюджета доступности (PDB).")
        return menu

    def show(self, identity, position):
        if not identity:
            return
        menu = self.menu(identity)
        try:
            menu.exec(position)
        finally:
            menu.deleteLater()

    def bind_table(self, table, identity_for_entry, controls):
        """Mouse and keyboard menus share the same immutable target actions."""
        def current_identity():
            return identity_for_entry(table.currentItem()) if table.currentItem() else None

        def show_current():
            identity = current_identity()
            if identity:
                self.show(identity, table.viewport().mapToGlobal(table.visualItemRect(table.currentItem()).center()))

        def show_point(point):
            entry = table.itemAt(point)
            if entry is not None:
                table.setCurrentItem(entry)
                self.show(identity_for_entry(entry), table.viewport().mapToGlobal(point))

        def open_current():
            identity = current_identity()
            if identity:
                cid, kind, namespace, name, uid = identity
                self.window.open_resource(cid, kind, namespace, name, expected_uid=uid)

        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(show_point)
        table.setToolTip("Двойной клик или Enter — открыть. ПКМ или Shift+F10 — действия со строкой.")
        for label, keys, callback in (("Открыть", ("Return", "Enter"), open_current),
                                      ("Действия", ("Shift+F10", "Menu"), show_current)):
            action = QAction(label, table)
            action.setShortcuts([QKeySequence(key) for key in keys])
            action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
            action.triggered.connect(callback)
            table.addAction(action)

    def delete_pod(self, identity):
        identity = tuple(identity)
        window = self.window
        cid, kind, namespace, name, uid = identity
        if kind != "pod" or not uid or identity in window._pod_operations:
            return
        if window.preferences.get("read_only", True):
            window.statusBar().showMessage("Для изменений включите режим администратора внизу слева.", 5000)
            return
        if (window._write_operation is not None or window._pod_operations or getattr(window, "_replica_draft_owner", None) is not None
                or any(panel.pending_operation for panel in getattr(window, "_resource_stack", []))):
            window.statusBar().showMessage("Дождитесь завершения текущей операции.", 5000)
            return
        window._pod_operations.add(identity)
        window.statusBar().showMessage(f"Проверяется Pod {namespace}/{name}…")
        finished = False

        def finish():
            nonlocal finished
            if finished:
                return False
            finished = True
            window._pod_operations.discard(identity)
            return True

        def failed(message):
            if not finish():
                return
            QMessageBox.warning(window, "Операция с Pod не выполнена", str(message))

        def ready(pod):
            if finished:
                return
            try:
                check_identity(pod, uid)
            except RuntimeError as exc:
                failed(str(exc))
                return
            if window.preferences.get("read_only", True):
                finish()
                window.statusBar().showMessage("Удаление отменено: режим администратора выключен.", 5000)
                return
            target = f"{window.api.cluster(cid).get('name', cid)} / {namespace} / {name}"
            mode = confirm_pod_removal(window, target, pod)
            if not mode:
                finish()
                window.statusBar().showMessage("Удаление отменено.", 3000)
                return
            lease = window.begin_write_operation()
            if lease is None:
                finish()
                window.statusBar().showMessage("Удаление отменено: изменения заблокированы или выполняется другая операция.", 5000)
                return

            def execute():
                if window.preferences.get("read_only", True):
                    raise PermissionError("Удаление отменено: режим администратора выключен.")
                window.validate_admin_access(cid)
                if window.preferences.get("read_only", True):
                    raise PermissionError("Удаление отменено: режим администратора выключен.")
                # The existing API re-reads identity and ownership and never falls
                # back from an Eviction failure to direct DELETE.
                return window.api.delete_pod(cid, namespace, name, uid, mode)

            def done(_):
                if not finish():
                    return
                window.finish_write_operation(lease)
                window.api.cache.invalidate()
                window.statusBar().showMessage(f"Удаление {namespace}/{name} принято.", 10000)
                for panel in getattr(window, "_resource_stack", []):
                    if (panel.kind, panel.cluster_id, panel.namespace) == ("deployment", cid, namespace):
                        panel.reload_deployment_pods()
                window.refresh()

            def operation_failed(message):
                window.finish_write_operation(lease)
                failed(f"{message}\nПроверьте результат перед повтором.")
            if window.background.submit(execute, done, operation_failed, priority=True) is False:
                operation_failed("Не удалось поставить удаление в очередь.")

        if window.background.submit(lambda: window.api.resource(cid, "pod", namespace, name), ready, failed) is False:
            failed("Не удалось поставить проверку Pod в очередь.")
