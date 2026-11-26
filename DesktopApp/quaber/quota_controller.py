"""ResourceQuota read orchestration, independent of page construction."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from PyQt5.QtWidgets import QLabel
from .collection_reader import CollectionReader
from .polling import polling_allowed
from .quota_card import QuotaCard
from .monitoring import MonitoringCancelled


class QuotaController:
    def __init__(self, view):
        self.view = view
        self.operation = None

    def load(self):
        view = self.view
        if self.operation is not None or not polling_allowed(view) or not view._workspace_read_active('quota'):
            return
        operation = self.operation = object()
        view._loading_quotas = True
        generation = getattr(view, '_quota_generation', 0)
        if not view.quota_layout.count():
            view.quota_status.setText('Загрузка квот ресурсов…')
        view.quota_clusters.update_text()
        selected = view.quota_clusters.ids()
        clusters = [c for c in view.store.clusters() if c['enabled'] and c['id'] in selected]
        delivered = [False]
        def load_cluster(cluster):
            result = []
            reader = CollectionReader(view.api)
            cid = cluster['id']
            from .request_policy import expected_credentials, credentials_revision
            try:
                with expected_credentials({cid: credentials_revision(cluster)}):
                    plan = reader.plan(cid)
                    collection = reader.read(cid, 'quota', plan)
                    result.extend((cluster['name'], ns, collection.groups.get(ns, []),
                                   str(collection.errors[ns]) if ns in collection.errors else '')
                                  for ns in plan.namespaces)
            except MonitoringCancelled:
                raise
            except Exception as exc:
                result.append((cluster['name'], '', [], str(exc)))
            return result
        def load():
            if not clusters:
                return []
            context = copy_context()
            with ThreadPoolExecutor(max_workers=min(32, len(clusters))) as pool:
                futures = [pool.submit(context.copy().run, load_cluster, cluster) for cluster in clusters]
                return [row for future in futures for row in future.result()]
        def current():
            return (view._workspace_read_active('quota') and self.operation is operation
                    and generation == getattr(view, '_quota_generation', 0))
        def display(groups):
            if not current():
                return
            delivered[0] = True
            from copy import deepcopy
            revision = (selected, [(cluster, namespace, failure,
                         [(q.get('metadata', {}).get('name'), q.get('status', {})) for q in quotas])
                         for cluster, namespace, quotas, failure in groups])
            if revision != getattr(view, '_quota_render_revision', None):
                view._clear_quota_cards()
                for cluster, namespace, quotas, failure in groups:
                    if failure:
                        label = QLabel(failure)
                        label.setWordWrap(True)
                        view.quota_layout.addWidget(label)
                    for quota in quotas:
                        view.quota_layout.addWidget(QuotaCard(cluster, namespace, quota))
                if not view.quota_layout.count():
                    view.quota_layout.addWidget(QLabel('Квоты ресурсов не найдены.' if selected else 'Выберите кластеры.'))
                view.quota_layout.addStretch()
                view._quota_render_revision = deepcopy(revision)
            failures = sum(bool(failure) for _, _, _, failure in groups)
            view.quota_status.setText(('Квоты загружены.' if selected else 'Выберите кластеры.')
                                     + (f' Ошибок: {failures}.' if failures else ''))
        def failed(message):
            if current():
                delivered[0] = True
                view.quota_status.setText('Не удалось загрузить квоты. Нажмите F5 для повтора.')
                view.statusBar().showMessage(message)
        def finalized(outcome):
            if self.operation is not operation:
                return
            self.operation = None
            view._loading_quotas = False
            if generation != getattr(view, '_quota_generation', 0):
                self.load()
            elif not delivered[0]:
                view.quota_status.setText('Чтение квот приостановлено. Возобновите мониторинг и нажмите F5.')
        if view.background.submit(view._workspace_read(load), display, failed, label='Квоты ресурсов', finalized=finalized) is False:
            failed('Не удалось начать чтение квот.')
            finalized('CANCELLED')
