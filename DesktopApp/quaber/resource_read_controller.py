"""Card read orchestration and explicit suspension of its read owners."""
from .read_lifecycle import suspend_children


class ResourceReadController:
    def __init__(self, view):
        self.view = view

    def suspend(self):
        view = self.view
        view.background.cancel_reads()
        view._navigation_pending = False
        view._cancel_scale_draft()
        view._resource_epoch += 1
        view._yaml_generation += 1
        view._yaml_loading = view._object_loading = view._pods_loading = view._pod_metrics_loading = False
        yaml = getattr(view, '_yaml_reload_controller', None)
        if yaml is not None:
            yaml.operation = None
        suspend_children(view)

    def refresh_object(self, *, notify_errors=True):
        _ui_view = self.view
        from .ui import check_identity, error, polling_allowed
        from .request_policy import request_intent
        manual = request_intent.get().purpose == 'manual'
        if not polling_allowed(_ui_view):
            return
        if _ui_view.kind == 'deployment':
            _ui_view.reload_deployment_pods()
            return
        if _ui_view.pending_operation or getattr(_ui_view, '_object_loading', False):
            return
        _ui_view._object_loading = True
        epoch = _ui_view._resource_epoch

        def done(resource):
            _ui_view._object_loading = False
            if epoch != _ui_view._resource_epoch:
                return
            try:
                check_identity(resource, _ui_view.resource.get('metadata', {}).get('uid'))
                changed = _ui_view._adopt_live(resource)
                if manual and not changed:
                    _ui_view._card_refreshed()
            except RuntimeError as exc:
                failed(str(exc))

        def failed(message):
            _ui_view._object_loading = False
            _ui_view.yaml_notice_text.setText('Не удалось проверить YAML на сервере.')
            _ui_view.yaml_reload.setEnabled(False)
            _ui_view.yaml_notice.setToolTip(message)
            _ui_view.yaml_notice.show()
            if notify_errors:
                error(_ui_view, message)
        _ui_view.background.submit(lambda: _ui_view.api.resource(_ui_view.cluster_id, _ui_view.kind, _ui_view.namespace, _ui_view.name), done, failed, finalized=_ui_view._read_finalizer('_object_loading', epoch))

    def reload_deployment_pods(self):
        _ui_view = self.view
        from .ui import Qt, ResourceNotFoundError, batch_table, item, pod_status, poll_interval, polling_allowed, resource_age, restart_poll_timer, time
        from .request_policy import request_intent
        manual = request_intent.get().purpose == 'manual'
        if not polling_allowed(_ui_view):
            return
        if _ui_view._pods_loading or _ui_view.pending_operation:
            return
        _ui_view.deployment_period.setText(f'Автообновление: {poll_interval(_ui_view.deployment_timer) / 1000:g} сек')
        _ui_view._pods_loading = True
        epoch = _ui_view._resource_epoch
        fresh = poll_interval(_ui_view.deployment_timer) == 1000
        deployment_uid = _ui_view.resource.get('metadata', {}).get('uid')

        def load():
            current = _ui_view.api.resource(_ui_view.cluster_id, 'deployment', _ui_view.namespace, _ui_view.name)
            if current.get('metadata', {}).get('uid') != deployment_uid:
                raise ResourceNotFoundError('Deployment был заменён. Откройте его заново из матрицы.')
            pods = _ui_view.api.pods(_ui_view.cluster_id, _ui_view.namespace, fresh=True) if fresh else _ui_view.api.pods(_ui_view.cluster_id, _ui_view.namespace)
            sets = _ui_view.api.replica_sets(_ui_view.cluster_id, _ui_view.namespace, fresh=True) if fresh else _ui_view.api.replica_sets(_ui_view.cluster_id, _ui_view.namespace)
            owned = {rs.get('metadata', {}).get('uid') for rs in sets if any((o.get('uid') == deployment_uid and o.get('controller') for o in rs.get('metadata', {}).get('ownerReferences', [])))}
            owned.discard(None)
            pods = [p for p in pods if deployment_uid and any((o.get('controller') and (o.get('uid') in owned or o.get('uid') == deployment_uid) for o in p.get('metadata', {}).get('ownerReferences', [])))]
            return (current, pods, sets, _ui_view.api.scaling_policy(_ui_view.cluster_id, _ui_view.namespace, _ui_view.name))

        @batch_table(lambda *_: _ui_view.pod_table, identity_column=1)
        def done(result):
            _ui_view._pods_loading = False
            if epoch != _ui_view._resource_epoch:
                restart_poll_timer(_ui_view.deployment_timer, _ui_view._rollout_cadence.interval(True))
                return
            current, pods, sets, policy = result
            if isinstance(policy, dict) and policy.get('known'):
                _ui_view._hpa_controllers = policy.get('controllers', [])
            _ui_view._displayed_pods = pods
            _ui_view._displayed_sets = sets
            changed = _ui_view._adopt_live(current)
            if manual and not changed:
                _ui_view._card_refreshed()
            _ui_view.pod_table.set_empty_text('Pod отсутствуют.')
            ready = sum((pod_status(p) == 'Running' for p in pods))
            moving = ready != current.get('spec', {}).get('replicas', 1) or _ui_view._rollout_in_progress or any((p.get('metadata', {}).get('deletionTimestamp') for p in pods))
            restart_poll_timer(_ui_view.deployment_timer, _ui_view._rollout_cadence.interval(moving))
            _ui_view.deployment_period.setText(f'Автообновление: {poll_interval(_ui_view.deployment_timer) / 1000:g} сек')
            _ui_view.update_write_controls()
            if not pods:
                _ui_view._pod_metrics = {}
                if hasattr(_ui_view, '_deployment_metrics_display'):
                    _ui_view._deployment_metrics_display({'pods': []})
            table = _ui_view.pod_table
            selected = table.item(table.currentRow(), 1)
            selected_identity = selected.data(Qt.ItemDataRole.UserRole) if selected else None
            table.setRowCount(len(pods))
            for row, pod in enumerate(pods):
                meta, status = (pod.get('metadata', {}), pod.get('status', {}))
                identity = (meta.get('name', ''), meta.get('uid', ''))
                values = (pod_status(pod), identity[0], resource_age(meta.get('creationTimestamp')), status.get('podIP', '—'), sum((c.get('restartCount', 0) for c in status.get('containerStatuses', []))), '—', '—')
                # Metrics have a separate cadence; avoid flashing dashes each poll.
                for col, value in enumerate(values[:5]):
                    cell = item(value)
                    if col == 1:
                        cell.setData(Qt.ItemDataRole.UserRole, identity)
                    elif col == 2:
                        cell.setToolTip(meta.get('creationTimestamp', ''))
                    table.setItem(row, col, cell)
                if identity == selected_identity:
                    table.selectRow(row)
            table.resizeColumnsToContents()
            table.setColumnWidth(0, max(92, table.columnWidth(0)))
            table.apply_filter()
            _ui_view._apply_pod_metrics()
            import time
            demand = table.isVisible() or _ui_view.tabs.tabText(_ui_view.tabs.currentIndex()) == 'Метрики'
            if not _ui_view._pod_metrics_loading and pods and demand and (time.monotonic() - getattr(_ui_view, '_pod_metrics_started', -10) >= 5):
                _ui_view._pod_metrics_started = time.monotonic()
                _ui_view._pod_metrics_loading = True

                def metrics():
                    return _ui_view.api.metrics_many(_ui_view.cluster_id, _ui_view.namespace, pods)

                def show_metrics(samples):
                    _ui_view._pod_metrics_loading = False
                    if epoch != _ui_view._resource_epoch:
                        return
                    _ui_view._pod_metrics = samples
                    _ui_view._apply_pod_metrics()
                    if hasattr(_ui_view, '_deployment_metrics_display'):
                        _ui_view._deployment_metrics_display({'pods': [dict(value, pod=identity[0]) for identity, value in samples.items()]})

                def metrics_failed(message):
                    _ui_view._pod_metrics_loading = False
                _ui_view.background.submit(metrics, show_metrics, metrics_failed, demand='tab', finalized=_ui_view._read_finalizer('_pod_metrics_loading', epoch))

        def failed(message):
            _ui_view._pods_loading = False
            if epoch != _ui_view._resource_epoch:
                return
            _ui_view.deployment_notice.setText(message)
            _ui_view.deployment_notice.show()
        _ui_view.background.submit(load, done, failed, finalized=_ui_view._read_finalizer('_pods_loading', epoch))
