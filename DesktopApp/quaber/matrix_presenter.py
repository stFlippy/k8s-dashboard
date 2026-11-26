"""Feature controller extracted from the desktop shell."""
from __future__ import annotations


class MatrixPresenter:
    def __init__(self, view):
        self.view = view

    def render(self):
        _ui_view = self.view
        from .ui import QColor, QFontMetrics, QRect, Qt, WORKLOAD_ORDER, datetime, empty_workload, html, index_scope, item, math, pod_status, recent_activity_deadline, timezone
        if not hasattr(_ui_view, 'snapshot'):
            return
        _ui_view._rendering_matrix = True
        now = datetime.now(timezone.utc)
        next_expiry = None
        if _ui_view.matrix.property('deployMode') != _ui_view.deployment_mode.active:
            _ui_view.matrix.setProperty('deployMode', _ui_view.deployment_mode.active)
            _ui_view.matrix.viewport().update()
        _ui_view._save_view_controls()
        view = _ui_view.views.active
        enabled = {c['id'] for c in _ui_view.store.clusters() if c.get('enabled')}
        snapshot = {cid: data for cid, data in _ui_view.snapshot.items() if cid in enabled}
        from .resource_index import scope_revision, rebind_scope
        previous_indexes = getattr(_ui_view, '_scope_index_cache', {})
        next_indexes = {}
        all_indexes = {}
        for cid, cluster in snapshot.items():
            for ns, scope in cluster['namespaces'].items():
                key = (cid, ns)
                revision = scope_revision(scope)
                cached = previous_indexes.get(key)
                index = rebind_scope(scope, cached[1]) if cached and cached[0] == revision else index_scope(scope)
                next_indexes[key] = (revision, index)
                all_indexes[key] = index
        _ui_view._scope_index_cache = next_indexes
        if _ui_view.views.observe_scopes({'ClusterIds': enabled, 'Namespaces': {ns for cluster in snapshot.values() for ns in cluster['namespaces']}, 'Deployments': {name for entries, _ in all_indexes.values() for name in entries}}):
            _ui_view._save_preferences()
        columns = [(cluster_id, cluster['name'], namespace) for cluster_id, cluster in snapshot.items() for namespace in cluster['namespaces'] if (view.get('selectedClusterIds') is None or cluster_id in view['selectedClusterIds']) and (view.get('selectedNamespaces') is None or namespace in view['selectedNamespaces'])]
        order = view.get('namespaceOrder') or []
        positions = {key: position for position, key in enumerate(order)}
        columns.sort(key=lambda col: positions.get(f'{col[0]}/{col[2]}', len(order)))
        scope_indexes = {(cid, ns): all_indexes[cid, ns] for cid, _, ns in columns}
        row_kinds = {name: resource.get('kind', 'Deployment') for entries, _ in scope_indexes.values() for name, resource in entries.items()}
        ranks = {kind: index for index, kind in enumerate(WORKLOAD_ORDER)}
        deployments = sorted(row_kinds, key=lambda name: (ranks.get(row_kinds[name], 99), name))
        if view.get('selectedDeployments') is not None:
            deployments = [name for name in deployments if name in view['selectedDeployments']]
        deployment_order = view.get('deploymentOrder') or []
        positions = {key: position for position, key in enumerate(deployment_order)}
        deployments.sort(key=lambda name: (ranks.get(row_kinds[name], 99), positions.get(name, len(deployment_order))))
        term = _ui_view.search.text().casefold().strip()
        if term:
            deployments = [name for name in deployments if term in name.casefold()]
        selected_key = None
        selected = _ui_view.matrix.currentIndex()
        old_names = getattr(_ui_view, '_row_names', [])
        old_columns = getattr(_ui_view, '_columns', [])
        if selected.isValid() and selected.row() < len(old_names):
            old_scope = old_columns[selected.column() - 1] if 0 < selected.column() <= len(old_columns) else None
            selected_key = (old_names[selected.row()], (old_scope[0], old_scope[2]) if old_scope else None)
        scroll = (_ui_view.matrix.horizontalScrollBar().value(), _ui_view.matrix.verticalScrollBar().value())
        _ui_view._row_names = deployments
        _ui_view._columns = columns
        structure = (tuple(deployments), tuple(((cid, ns) for cid, _, ns in columns)), tuple((row_kinds[name] for name in deployments)))
        structural = structure != getattr(_ui_view, '_matrix_structure', None) or _ui_view.matrix.rowCount() != len(deployments) or _ui_view.matrix.columnCount() != len(columns) + 1 or any((_ui_view.matrix.horizontalHeaderItem(c) is None for c in range(len(columns) + 1)))
        if structural:
            _ui_view.matrix.setUpdatesEnabled(False)
            _ui_view.matrix._set_hover((-1, -1))
            _ui_view.matrix.clear()
            _ui_view.matrix.setColumnCount(len(columns) + 1)
            _ui_view.matrix.setRowCount(len(deployments))
            _ui_view._restore_header_identity(_ui_view.matrix.horizontalHeader())
            _ui_view._restore_header_identity(_ui_view.matrix.verticalHeader())
            _ui_view.matrix._height_groups = set()
            _ui_view.matrix.group_rows = set()
            for row in range(len(deployments)):
                _ui_view.matrix.setRowHeight(row, 30)
            _ui_view._matrix_structure = structure
        _ui_view.matrix.row_kinds = [row_kinds[name] for name in deployments]
        show_clusters = _ui_view.preferences.get('show_cluster_names', True)
        if structural:
            _ui_view.matrix.setHorizontalHeaderLabels([''] + [f'{label}\n{ns}' if show_clusters else ns for _, label, ns in columns])
        for col, (cluster_id, label, namespace) in enumerate(columns, 1):
            from .widgets import CLUSTER_ID_ROLE
            _ui_view.matrix.model().setHeaderData(col, Qt.Orientation.Horizontal, cluster_id, CLUSTER_ID_ROLE)
            scope = _ui_view.snapshot[cluster_id]['namespaces'][namespace]
            stale = scope.get('_stale', False)
            failed_types = scope.get('_type_errors', {})
            _ui_view.matrix.horizontalHeaderItem(col).setText(f'{label}\n{namespace}' if show_clusters else namespace)
            if stale or failed_types:
                _ui_view.matrix.horizontalHeaderItem(col).setText(f'{label}\n⚠ {namespace}' if show_clusters else f'⚠ {namespace}')
            _ui_view.matrix.horizontalHeaderItem(col).setToolTip(f"{label} / {namespace}\nПоследний успех: {scope.get('_last_success', 'неизвестно')}" + ('\nДополнительные типы ещё загружаются.' if scope.get('_loading') else '') + ('\nУСТАРЕЛО: ' + scope.get('_error', 'ошибка обновления') if stale else '') + ('\nНе прочитаны типы: ' + ', '.join(failed_types) if failed_types else ''))
        _ui_view.matrix_data.clear()
        previous_visuals = _ui_view.matrix._cell_visuals
        if hasattr(_ui_view, 'schedules'):
            _ui_view.schedules.begin()
        _ui_view.matrix._cell_visuals = {}
        _ui_view.matrix._cell_keys.clear()
        visible_pod_count = 0
        row_heights = [30] * len(deployments)
        from collections import Counter
        from .matrix_counters import PARTITIONS, classify_pod, scope_summary, workload_uid
        totals = Counter({status: 0 for status in PARTITIONS})
        workload_counts, batch_failed, batch_counts = (Counter(), Counter(), Counter())
        unavailable, stale_counts, issues = (set(), False, 0)
        for cid, _, ns in columns:
            summary = scope_summary(_ui_view.snapshot[cid]['namespaces'][ns], scope_indexes[cid, ns], set(deployments), now)
            totals.update(summary['pods'])
            workload_counts.update(summary['workloads'])
            batch_failed.update(summary['batch_failed'])
            batch_counts.update(summary['batch'])
            unavailable.update(summary['unavailable'])
            stale_counts |= summary['stale']
            issues += summary['issues']
        total_pods = sum(totals.values())
        _ui_view.matrix_counts = {'pods': dict(totals), 'batch': dict(batch_counts), 'workloads': dict(workload_counts), 'issues': issues, 'stale': stale_counts, 'unavailable': sorted(unavailable)}
        for row, name in enumerate(deployments):
            label = name.split('/', 1)[1] if row_kinds[name] != 'Deployment' and '/' in name else name
            if _ui_view.matrix.item(row, 0) is None:
                _ui_view.matrix.setItem(row, 0, item(label))
            elif _ui_view.matrix.item(row, 0).text() != label:
                _ui_view.matrix.item(row, 0).setText(label)
            for col, (cluster_id, _, namespace) in enumerate(columns, 1):
                resources = _ui_view.snapshot[cluster_id]['namespaces'][namespace]
                deployments_index, pods_index = scope_indexes[cluster_id, namespace]
                deployment = deployments_index.get(name)
                if not deployment:
                    if _ui_view.matrix.item(row, col) is not None:
                        _ui_view.matrix.takeItem(row, col)
                    continue
                deployment_uid = workload_uid(deployment)
                pods = pods_index.get(deployment_uid, [])
                filtered = []
                for pod in pods:
                    status = classify_pod(pod, now)
                    deadline = recent_activity_deadline(pod, now)
                    if deadline is not None and (next_expiry is None or deadline < next_expiry):
                        next_expiry = deadline
                    filter_status = 'Running' if status == 'Recent' else status
                    allowed = _ui_view.status_filters[filter_status].isChecked() if filter_status in _ui_view.status_filters else True
                    if allowed:
                        filtered.append(pod)
                visible_pod_count += len(filtered)
                from .matrix_presentation import PodVisual
                from .widgets import COLORS
                visuals = []
                stale = bool(resources.get('_stale')) or deployment.get('kind', 'Deployment').lower() in resources.get('_type_errors', {})
                for pod in filtered:
                    meta = pod.get('metadata', {})
                    status = pod_status(pod)
                    recent = recent_activity_deadline(pod, now) is not None and status == 'Running'
                    restarts = sum((c.get('restartCount', 0) for c in pod.get('status', {}).get('containerStatuses', [])))
                    visuals.append(PodVisual((cluster_id, 'pod', namespace, meta.get('name', ''), meta.get('uid')), status, COLORS.get(status, COLORS['Unknown']), recent, stale, f"Name: {meta.get('name', '')}\nStatus: {status}\nRestarts: {restarts}\nCreated: {meta.get('creationTimestamp', '—')}"))
                key = (cluster_id, namespace, name)
                if deployment.get('kind') == 'CronJob' and hasattr(_ui_view, 'schedules'):
                    visuals = [_ui_view.schedules.visual(cluster_id, namespace, deployment, resources)]
                _ui_view.matrix._cell_keys[row, col] = key
                values = tuple(visuals)
                _ui_view.matrix._cell_visuals[key] = previous_visuals[key] if previous_visuals.get(key) == values else values
                if not structural and previous_visuals.get(key) != values:
                    _ui_view.matrix.viewport().update(_ui_view.matrix.visualRect(_ui_view.matrix.model().index(row, col)))
                text, missing = empty_workload(deployment, pods)
                hidden_summary = ''
                if pods and (not filtered):
                    text = f'Скрыто: {len(pods)}'
                    states = {}
                    for pod in pods:
                        state = pod_status(pod)
                        states[state] = states.get(state, 0) + 1
                    hidden_summary = 'Скрыто фильтрами состояния: ' + ', '.join((f'{state}: {count}' for state, count in states.items())) + '. Включите соответствующий фильтр над таблицей.\n'
                from .matrix_presentation import condition_tooltip
                tooltip = hidden_summary + condition_tooltip(deployment, text or 'Наведите на Pod для сведений')
                from PyQt5.QtGui import QBrush
                roles = {Qt.ItemDataRole.DisplayRole: text, Qt.ItemDataRole.UserRole: key, Qt.ItemDataRole.ToolTipRole: tooltip, Qt.ItemDataRole.ForegroundRole: QBrush(QColor('#E56B6E')) if missing else None}
                old = _ui_view.matrix.item(row, col)
                if old is None:
                    old = item(text)
                    _ui_view.matrix.setItem(row, col, old)
                for role, value in roles.items():
                    if old.data(role) != value:
                        old.setData(role, value)
                _ui_view.matrix_data[row, col] = (cluster_id, namespace, name, deployment, filtered)
                width = 48
                text_height = _ui_view.matrix.fontMetrics().boundingRect(QRect(0, 0, max(20, width - 8), 1000), Qt.TextFlag.TextWrapAnywhere, text).height() + 12 if text else 30
                row_heights[row] = max(row_heights[row], 70 if missing else text_height, 14 + (len(visuals) + 1) // 2 * 19)
        for col in range(1, _ui_view.matrix.columnCount()):
            _ui_view.matrix.setColumnHidden(col, False)
        if structural:
            _ui_view.matrix.resizeColumnToContents(0)
        _ui_view.matrix.horizontalHeader().setMinimumSectionSize(36)
        for status, checkbox in _ui_view.status_filters.items():
            checkbox.setText(f"{('Error' if status == 'Failed' else status)}  {totals[status]}")
        pod_unavailable = stale_counts or any((str(kind).lower() in ('pod', 'pods') for kind in unavailable))
        no_scope_data = not columns and bool(getattr(_ui_view, '_matrix_failures', ()))
        if pod_unavailable and (not total_pods) or no_scope_data:
            _ui_view.summary_label.setText('Unavailable · Total Pods')
        else:
            suffix = ' · stale' if stale_counts else ' · partial' if pod_unavailable else ''
        _ui_view.summary_label.setToolTip(f'Основные Pod в выбранном scope: {total_pods}. Batch Pod отдельно: {sum(batch_counts.values())}.')
        _ui_view.recent_label.setText(f"● Recently started {totals['Recent']}")
        _ui_view.extra_counters.setText(' · '.join((f'{key} {totals[key]}' for key in ('Completed', 'Terminating', 'Other') if totals[key])))
        _ui_view.extra_counters.setVisible(bool(_ui_view.extra_counters.text()))
        from .themes import theme
        colors = theme(_ui_view.preferences.get('theme') == 'light')
        accent = colors['accent']
        def number(value):
            return f'<span style="color:{accent}">{value}</span>'
        if not (pod_unavailable and not total_pods or no_scope_data):
            _ui_view.summary_label.setText(number(total_pods) + '&nbsp;&nbsp;Total Pods' + suffix)
        labels = {'Deployment': 'Deployments', 'StatefulSet': 'StatefulSets', 'DaemonSet': 'DaemonSets', 'ReplicaSet': 'ReplicaSets', 'ReplicationController': 'ReplicationControllers', 'CronJob': 'CronJobs', 'Job': 'Jobs', 'Pod': 'Standalone Pods'}
        parts = []
        for kind, label in labels.items():
            if workload_counts[kind]:
                failed = f' · {number(batch_failed[kind])} failed' if batch_failed[kind] else ''
                parts.append(f'{label} {number(workload_counts[kind])}{failed}')
        if issues:
            parts.append(f'Workload issues {number(issues)}')
        if batch_counts:
            parts.append(f"Batch Pods {number(sum(batch_counts.values()))} · failed {number(batch_counts['Failed'])}")
        if unavailable:
            parts.append('Unavailable: ' + html.escape(', '.join(sorted(unavailable))))
        if stale_counts:
            parts.append('stale')
        _ui_view.workload_counters.setText('(' + ' · '.join(parts) + ')' if parts else '')
        _ui_view.workload_counters.setToolTip('Дочерние Job включены в CronJobs. Batch Pods: ' + ', '.join((f'{k} {v}' for k, v in batch_counts.items() if v)))
        _ui_view.workload_counters.setVisible(bool(parts))
        if hasattr(_ui_view, 'schedules'):
            _ui_view.schedules.end()
            _ui_view.schedules.flush()
        for key, (title, button) in _ui_view.scope_buttons.items():
            selected = view.get(key)
            label = 'Все' if selected is None else str(len(selected))
            if key == 'selectedClusterIds':
                selected = {cid for column, (cid, _, ns) in enumerate(columns, 1)
                            if not _ui_view.matrix.isColumnHidden(column)}
                button.setToolTip(f'Показано кластеров в матрице: {len(selected)}. Включено в настройках: {len(enabled)}.')
                label = str(len(selected))
            elif key == 'selectedNamespaces':
                button.setToolTip(f'Столбцов namespace: {len(columns)}.')
            elif key == 'selectedDeployments' and selected is not None:
                available = set(row_kinds)
                label = 'Все' if available and available.issubset(selected) else str(len(available.intersection(selected)))
                button.setToolTip('Выбор нагрузок для этой вкладки. Число учитывает только текущие доступные нагрузки.')
            button.setText(f"{title}: {label} ▾")
        _ui_view.matrix.setColumnWidth(0, _ui_view.preferences.get('deployment_width', 150))
        name_font = _ui_view.matrix.font()
        name_font.setBold(True)
        name_metrics = QFontMetrics(name_font)
        for row in range(_ui_view.matrix.rowCount()):
            label = _ui_view.matrix.item(row, 0)
            if label:
                height = name_metrics.boundingRect(QRect(0, 0, max(20, _ui_view.matrix.columnWidth(0) - 18), 10000), Qt.TextFlag.TextWordWrap, label.text()).height() + 8
                row_heights[row] = max(row_heights[row], height)
            height = row_heights[row] + _ui_view.matrix.group_offset(row)
            if _ui_view.matrix.rowHeight(row) != height:
                _ui_view.matrix.setRowHeight(row, height)
        _ui_view.matrix.horizontalHeader().setFixedHeight(_ui_view.preferences.get('header_height', 160))
        for col, (cluster_id, _, namespace) in enumerate(columns, 1):
            _ui_view.matrix.setColumnWidth(col, 48)
        _ui_view.matrix.sync_frozen()
        if structural:
            _ui_view.matrix.clearSelection()
            _ui_view.matrix.setCurrentIndex(_ui_view.matrix.model().index(-1, -1))
            if selected_key and selected_key[0] in deployments:
                scopes = [(cid, ns) for cid, _, ns in columns]
                if selected_key[1] is None or selected_key[1] in scopes:
                    column = 0 if selected_key[1] is None else scopes.index(selected_key[1]) + 1
                    _ui_view.matrix.setCurrentCell(deployments.index(selected_key[0]), column)
            _ui_view.matrix.horizontalScrollBar().setValue(scroll[0])
            _ui_view.matrix.verticalScrollBar().setValue(scroll[1])
        if next_expiry is None:
            _ui_view.recent_age_timer.stop()
        else:
            delay = (next_expiry - datetime.now(timezone.utc)).total_seconds()
            _ui_view.recent_age_timer.start(max(1, math.ceil(delay * 1000)))
        _ui_view._rendering_matrix = False
