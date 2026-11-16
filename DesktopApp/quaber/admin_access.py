"""Administrator-only local access, bound to the active editable card."""
import time
from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtWidgets import QToolTip


class AdminAccess(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.cache, self.busy, self.intent = {}, {}, {}
        self.generation = 0
        self.last_target = None
        self.updating = False
        self.timer = QTimer(self)
        self.timer.setInterval(30000)
        self.timer.timeout.connect(self.update)
        self.timer.start()

    def target(self):
        panel = getattr(self.window, "_resource_panel", None)
        if (not panel or not panel.isVisible() or panel.kind not in ("deployment", "pod", "configmap")
                or getattr(panel, "_navigation_pending", False) or not panel._matches_opened_target()):
            return None
        return (*panel._opened_identity, id(panel), getattr(self.window, "_resource_request", 0),
                getattr(panel, "_access_context_generation", 0)) if any(button.isVisible() for button in panel.write_controls) else None

    def signature(self, cid):
        cluster = self.window.api.cluster(cid)
        return (id(self.window.api), cluster.get("server"), cluster.get("username"),
                cluster.get("encryptedKubeconfig"), cluster.get("enabled"))

    def update(self):
        if self.updating or self.window._closing or not hasattr(self.window, "shell_actions"):
            return
        self.updating = True
        try:
            mode = self.window.shell_actions.mode
            target = self.target()
            previous, self.last_target = self.last_target, target
            if target is None:
                if previous is not None and not self.window.preferences.get("read_only", True):
                    self.window.set_read_only(True)
                mode.setEnabled(False)
                mode.setToolTip("В этом разделе нет доступных изменений.")
                return
            cid = target[0]
            try:
                signature = self.signature(cid)
            except Exception as exc:
                mode.setEnabled(False)
                mode.setToolTip(f"Не удалось подтвердить права администратора: {exc}")
                return
            cached = self.cache.get(cid)
            if not cached or cached[0] != signature or time.monotonic()-cached[1] >= 30:
                mode.setEnabled(not self.window.preferences.get("read_only", True))
                mode.setToolTip("Проверяются права администратора…")
                if cid not in self.busy:
                    self.check(cid)
                return
            allowed, detail = cached[2:]
            mode.setEnabled(not self.window.preferences.get("read_only", True) or
                            (allowed is True and self.window._write_operation is None))
            if allowed is not True:
                mode.setToolTip(detail)
            elif self.window._write_operation is not None and self.window.preferences.get("read_only", True):
                mode.setToolTip("Дождитесь завершения текущей операции.")
            else:
                mode.setToolTip(self.window.shell_actions.mode_tooltip())
        finally:
            self.updating = False

    def check(self, cid, *, force=False):
        if self.window._closing or (cid in self.busy and not force):
            return
        try:
            signature = self.signature(cid)
        except Exception:
            self.update()
            return
        self.generation += 1
        request = self.generation
        self.busy[cid] = request
        api = self.window.api
        def read():
            try:
                allowed = api.is_cluster_administrator(cid)
                if type(allowed) is not bool:
                    raise ValueError("Кластер не вернул результат проверки прав.")
                return allowed, "" if allowed else "Режим администратора недоступен из-за ограничения прав Kubernetes."
            except Exception as exc:
                return None, f"Не удалось подтвердить права администратора: {exc}"
        def done(result):
            if self.busy.get(cid) != request or self.window._closing:
                return
            self.busy.pop(cid, None)
            try:
                current = self.signature(cid)
            except Exception:
                current = None
            if signature != current:
                self.update()
                return
            allowed, detail = result
            self.cache[cid] = (signature, time.monotonic(), allowed, detail)
            target = self.target()
            if allowed is not True and target and target[0] == cid and not self.window.preferences.get("read_only", True):
                self.window.set_read_only(True)
            intended = self.intent.pop(cid, None)
            if intended is not None and intended[0] == target and time.monotonic()-intended[1] <= 30:
                if allowed is True:
                    self.window.set_read_only(False)
                else:
                    mode = self.window.shell_actions.mode
                    QToolTip.showText(mode.mapToGlobal(mode.rect().center()), detail, mode)
            self.update()
        if self.window.background.submit(read, done, lambda message: done((None, message)),
                                         key=("administrator-access", cid), priority=cid in self.intent) is False:
            done((None, "Не удалось выполнить проверку прав администратора."))

    def activate(self, target):
        if target is None or target != self.target():
            self.update()
            return
        self.intent[target[0]] = (target, time.monotonic())
        self.window.shell_actions.mode.setEnabled(False)
        self.check(target[0], force=True)

    def invalidate(self, cid):
        self.cache.pop(cid, None)
        self.intent.pop(cid, None)
        self.update()
