"""Server YAML reloads and irreversible local draft cancellation on access loss."""
import copy
from .safety import check_identity
from .yaml_codec import dump_resource
from .request_policy import manual_read


class YamlReloadController:
    def __init__(self, view):
        self.view = view
        self.operation = None

    def discard(self):
        view = self.view
        self.operation = None
        view._yaml_generation += 1
        view._yaml_loading = False
        view._yaml_reset_pending = True
        snapshot = copy.deepcopy(getattr(view, 'live_resource', view.resource))
        view.yaml_base_resource = snapshot
        view.version = snapshot.get('metadata', {}).get('resourceVersion', '')
        # setPlainText clears both the draft and its undo history immediately.
        view._accept_yaml_text(dump_resource(snapshot))
        view._yaml_ready = False
        view.yaml_notice_text.setText('Правки отменены. Загрузка YAML с сервера…')
        view.yaml_notice.show()
        view.update_write_controls()
        self.reload(discard=True)

    def reload(self, *, discard=False):
        from .ui import confirm
        view = self.view
        discard = discard or getattr(view, '_yaml_reset_pending', False)
        if view.pending_operation or getattr(view, '_navigation_pending', False) or self.operation is not None:
            return
        document = view.yaml_editor.editor.document()
        if document.isModified() and not discard:
            if not confirm(view, 'Reload YAML', 'Отбросить локальные изменения?'):
                return
            if hasattr(view.controller, 'set_read_only'):
                view.controller.set_read_only(True)
                if getattr(view, '_yaml_reset_pending', False):
                    return
        if discard:
            from .polling import polling_allowed
            if (not view.isVisible() or view.tabs.currentWidget() is not view.yaml_editor
                    or not view.background.active or not polling_allowed(view)):
                view.yaml_notice_text.setText('Правки отменены. YAML будет загружен при возобновлении просмотра.')
                view._highlight_yaml_reload(True)
                return
        revision = document.revision()
        uid = view.resource.get('metadata', {}).get('uid')
        epoch = view._resource_epoch
        view._yaml_generation += 1
        generation = view._yaml_generation
        operation = self.operation = object()
        view._yaml_loading = True
        view.yaml_reload.setEnabled(False)
        view.yaml_notice_text.setText('Загрузка YAML с сервера…')
        view.yaml_notice.show()
        def current():
            return self.operation is operation and view._resource_epoch == epoch and view._yaml_generation == generation
        def failed(message):
            if not current():
                return
            view.yaml_notice_text.setText('Правки отменены. Не удалось обновить YAML; нажмите «Обновить».' if discard else
                                         'Не удалось обновить YAML. Черновик сохранён.')
            view.yaml_notice.setToolTip(message)
            view.yaml_notice.show()
            view._highlight_yaml_reload(True)
        def done(resource):
            if not current():
                return
            try:
                check_identity(resource, uid)
            except RuntimeError as exc:
                failed(str(exc))
                return
            view._adopt_live(resource)
            if document.revision() != revision:
                view.yaml_notice_text.setText('Черновик изменён во время загрузки. Нажмите «Обновить» повторно.')
                view.yaml_notice.show()
                return
            view.resource = copy.deepcopy(resource)
            view.yaml_base_resource = copy.deepcopy(resource)
            view.version = resource.get('metadata', {}).get('resourceVersion', '')
            view._yaml_ready = True
            view._yaml_reset_pending = False
            view._accept_yaml_text(dump_resource(resource))
            view.yaml_notice.hide()
            view._highlight_yaml_reload(False)
        def finalized(outcome):
            if self.operation is not operation:
                return
            self.operation = None
            view._yaml_loading = False
            if outcome == 'CANCELLED':
                view.yaml_notice_text.setText('Правки отменены. Загрузка YAML приостановлена.' if discard else
                                             'Загрузка YAML приостановлена. Черновик сохранён.')
                view.yaml_notice.show()
                view._highlight_yaml_reload(True)
            view.update_write_controls()
        with manual_read():
            admitted = view.background.submit(
                lambda: view.api.resource(view.cluster_id, view.kind, view.namespace, view.name),
                done, failed, key='yaml-reload', finalized=finalized)
        if admitted is False:
            failed('Не удалось начать чтение YAML.')
            finalized('CANCELLED')
