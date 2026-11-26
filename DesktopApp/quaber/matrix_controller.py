"""Feature controller extracted from the desktop shell."""
from __future__ import annotations


class MatrixController:
    def __init__(self, view):
        self.view = view

    def refresh(self):
        _ui_view = self.view
        from .ui import ThreadPoolExecutor, bind_read, copy, datetime, error, item, polling_allowed, threading
        clusters = [item for item in _ui_view.store.clusters() if item['enabled']]
        if not clusters:
            _ui_view._reset_matrix_state()
            _ui_view.statusBar().showMessage('Добавьте кластер в настройках.')
            return
        if not polling_allowed(_ui_view):
            return
        if (getattr(_ui_view, '_resource_panel', None) is not None
                or getattr(_ui_view, '_resource_opening_request', None) is not None):
            _ui_view._matrix_visual_dirty = True
            return
        if _ui_view.tabs.currentWidget() is not _ui_view.matrix_page:
            _ui_view._matrix_visual_dirty = True
            return
        if any(_ui_view.views.active.get(key) == [] for key in ('selectedClusterIds', 'selectedNamespaces')):
            _ui_view._matrix_requested_scope = _ui_view._view_network_scope()
            _ui_view.render_matrix()
            _ui_view.statusBar().showMessage('Выберите кластеры и namespace в фильтрах над матрицей.')
            return
        if getattr(_ui_view, 'loading_matrix', False):
            from .request_policy import request_intent
            _ui_view._matrix_manual_pending = getattr(_ui_view, '_matrix_manual_pending', False) or request_intent.get().priority
            return
        _ui_view.loading_matrix = True
        _ui_view._matrix_generation += 1
        generation = _ui_view._matrix_generation
        from .fetch_plan import FetchPlan
        plan = FetchPlan.capture(clusters, _ui_view.views.active, generation)
        _ui_view._matrix_requested_scope = _ui_view._view_network_scope()
        _ui_view._matrix_cancel = matrix_cancel = threading.Event()
        initial = not any((cluster.get('namespaces') for cluster in _ui_view.snapshot.values()))
        _ui_view.statusBar().showMessage('Обновление матрицы…')
        previous = {cid: dict(cluster, namespaces=dict(cluster.get('namespaces', {}))) for cid, cluster in _ui_view.snapshot.items()}
        progress_signal = _ui_view.matrix_scope_ready

        def plan_is_current():
            try:
                current = FetchPlan.capture([c for c in _ui_view.store.clusters() if c.get('enabled')], _ui_view.views.active, generation)
            except Exception:
                _ui_view.loading_matrix = False
                _ui_view.statusBar().showMessage('Не удалось прочитать текущие подключения; данные устарели.', 5000)
                return False
            if (current.clusters, current.namespaces) == (plan.clusters, plan.namespaces):
                return True
            for cluster in _ui_view.snapshot.values():
                for scope in cluster.get('namespaces', {}).values():
                    scope['_stale'] = True
            _ui_view._matrix_scope_changed()
            return False

        def load():
            from .fetch_plan import collect_cluster
            from .monitoring import MonitoringCancelled
            snapshot, failures = ({}, [])

            def cluster_data(cluster):
                try:
                    value, problems = collect_cluster(_ui_view.api, plan, cluster, previous.get(cluster.identifier, {}).get('namespaces', {}), progress=progress_signal.emit)
                except MonitoringCancelled:
                    raise
                except Exception as exc:
                    scopes = copy.deepcopy(previous.get(cluster.identifier, {}).get('namespaces', {}))
                    if plan.namespaces is not None:
                        scopes = {ns: value for ns, value in scopes.items() if ns in plan.namespaces}
                    for scope in scopes.values():
                        scope['_stale'] = True
                        scope['_error'] = str(exc)
                    value, problems = ({'name': cluster.label, 'namespaces': scopes}, [f'{cluster.label}: {exc}'])
                return (cluster.identifier, value, problems)
            with ThreadPoolExecutor(max_workers=max(1, min(32, len(plan.clusters)))) as executor:
                for identifier, value, problems in executor.map(bind_read(cluster_data), plan.clusters):
                    snapshot[identifier] = value
                    failures.extend(problems)
            return (snapshot, failures)

        def done(result):
            if _ui_view._closing or generation != _ui_view._matrix_generation:
                return
            if not plan_is_current():
                return
            _ui_view.loading_matrix = False
            _ui_view.matrix_paint_timer.stop()
            _ui_view.snapshot, failures = result
            for message in failures:
                from .app_logging import log_problem
                log_problem('matrix', message)
            _ui_view._matrix_failures = tuple(failures)
            dates = [scope.get('_last_success') for cluster in _ui_view.snapshot.values() for scope in cluster.get('namespaces', {}).values()]
            observed = min(dates) if dates and all((isinstance(value, str) for value in dates)) else None
            stamp = datetime.fromisoformat(observed).astimezone().strftime('%H:%M:%S') if observed else '—'
            _ui_view.last_updated.setText((f'Не обновлено областей/типов: {len(failures)} · ' if failures else 'Последний снимок: ') + stamp)
            details = '\n'.join(failures[:20])
            if len(failures) > 20:
                details += f'\nЕщё ошибок: {len(failures) - 20}'
            _ui_view.last_updated.setToolTip((details + '\n' if failures else '') + f"Самый ранний успешный снимок выбранных областей: {observed or 'неизвестно'}.\n" + 'Повторное отображение кэша не меняет время снимка.')
            _ui_view.render_matrix()
            _ui_view.statusBar().showMessage('; '.join(failures[:3]) if failures else 'Матрица обновлена')
            if getattr(_ui_view, '_matrix_manual_pending', False):
                _ui_view.matrix_return_timer.start(0)

        def failed(message):
            if _ui_view._closing or generation != _ui_view._matrix_generation:
                return
            if not plan_is_current():
                return
            _ui_view.loading_matrix = False
            error(_ui_view, message)
            if getattr(_ui_view, '_matrix_manual_pending', False):
                _ui_view.matrix_return_timer.start(0)
        from .monitoring import cancellable_read

        def cancelled():
            if _ui_view._closing or generation != _ui_view._matrix_generation:
                return
            if not plan_is_current():
                return
            _ui_view.loading_matrix = False
            if getattr(_ui_view, '_matrix_manual_pending', False):
                _ui_view.matrix_return_timer.start(0)
        if _ui_view.background.submit(cancellable_read(load, matrix_cancel, _ui_view.monitoring.token()), done, failed, key='matrix-read', cancelled=cancelled) is False:
            _ui_view.loading_matrix = False
            _ui_view.statusBar().showMessage('Обновление отложено: очередь чтения занята.', 5000)
