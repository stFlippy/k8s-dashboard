"""Administrator-only local access, bound to the active editable card."""
from .tooltips import show_tooltip
import time
from PyQt5.QtCore import QObject, QTimer
from .polling import polling_allowed, mark_poll_timer
from .capabilities import ActionTarget, AccessOutcome, review_action


class AdminAccess(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.cache, self.busy, self.intent = {}, {}, {}
        self.action_cache = {}
        self.generation = 0
        self.last_target = None
        self.updating = False
        self.timer = QTimer(self)
        self.timer.setInterval(30000)
        self.timer.timeout.connect(self.update)
        self.timer.start()
        mark_poll_timer(self.timer)

    def target(self):
        panel = getattr(self.window, "_resource_panel", None)
        if (not panel or not panel.isVisible() or panel.kind not in ("deployment", "pod", "configmap")
                or getattr(panel, "_navigation_pending", False) or not panel._matches_opened_target()):
            return None
        return (*panel._opened_identity, id(panel), getattr(self.window, "_resource_request", 0),
                getattr(panel, "_access_context_generation", 0)) if any(button.isVisible() for button in panel.write_controls) else None

    def signature(self, cid):
        cluster = self.window.api.cluster(cid)
        target = self.target()
        return (id(self.window.api), cluster.get("server"), cluster.get("username"),
                cluster.get("encryptedKubeconfig"), cluster.get("enabled"), tuple(target[:5]) if target else None)

    def allows(self, action):
        target = self.target()
        if target is None: return False
        cached = self.action_cache.get(target[0])
        return bool(cached and cached[0] == self.signature(target[0]) and time.monotonic()-cached[1] < 30
                    and cached[2].get(action) is True)

    def may_edit_yaml(self):
        """A stale positive review permits local typing, never server dispatch."""
        target = self.target()
        if target is None:
            return False
        cached = self.action_cache.get(target[0])
        return bool(cached and cached[0] == self.signature(target[0]) and cached[2].get('yaml') is True)

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
        if not polling_allowed(self):
            return
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
        target = self.target()
        if target is None or target[0] != cid:
            self.busy.pop(cid,None)
            return
        kind, namespace, name = target[1:4]
        actions = {'deployment': ('yaml','resources','scale','restart'),
                   'configmap': ('yaml',), 'pod': ('yaml','delete','exec')}.get(kind, ())
        panel = getattr(self.window, '_resource_panel', None)
        eviction = bool(panel) and any(o.get('controller') for o in panel.resource.get('metadata',{}).get('ownerReferences',[]))
        targets = tuple(ActionTarget(cid,kind,namespace,name,action, action=='delete' and eviction) for action in actions)
        def read():
            try:
                decisions = {t.action: review_action(api,t).outcome is AccessOutcome.ALLOWED for t in targets}
                allowed = any(decisions.values())
                return allowed, "" if allowed else "Действия не разрешены для этой цели.", decisions
            except Exception as exc:
                return None, f"Не удалось подтвердить права: {exc}", {}
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
            allowed, detail = result[:2]
            decisions = result[2] if len(result)>2 else {}
            self.cache[cid] = (signature, time.monotonic(), allowed, detail)
            self.action_cache[cid] = (signature,time.monotonic(),decisions)
            target = self.target()
            current_panel = getattr(self.window, '_resource_panel', None)
            yaml_revoked = (current_panel is not None and decisions.get('yaml') is False
                            and current_panel.yaml_editor.editor.document().isModified())
            if (allowed is not True or yaml_revoked) and target and target[0] == cid and not self.window.preferences.get("read_only", True):
                self.window.set_read_only(True)
            intended = self.intent.pop(cid, None)
            if intended is not None and intended[0] == target and time.monotonic()-intended[1] <= 30:
                if allowed is True:
                    self.window.set_read_only(False)
                else:
                    mode = self.window.shell_actions.mode
                    show_tooltip(mode.mapToGlobal(mode.rect().center()), detail, mode)
            self.update()
            self.window.refresh_write_controls()
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
        self.action_cache.pop(cid,None)
        self.intent.pop(cid, None)
        self.update()
