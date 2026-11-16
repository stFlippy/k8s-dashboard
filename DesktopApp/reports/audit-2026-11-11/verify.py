"""Offline audit: synthetic data only; no Addons, kubectl or cluster connections.

Run with DesktopApp/venv/Scripts/python.exe audit-2026-11-11/verify.py.
Writes measurements and screenshots next to this script, uses temporary UserData.
"""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import copy
import cProfile
import io
import json
from pathlib import Path
import platform
import pstats
import statistics
import sys
import tempfile
import time
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from PyQt6.QtCore import QTimer, QT_VERSION_STR, PYQT_VERSION_STR
from PyQt6.QtWidgets import QApplication, QMessageBox, QDialog, QLabel
from PyQt6.QtTest import QTest
from quaber.ui import MainWindow, ResourceDialog
from quaber.storage import UserStore
from quaber.kubernetes import Kubernetes
from quaber.views import MatrixViews, validate_preferences
from quaber.metrics import ThrottleSamples
from quaber.quota_card import QuotaCard


def dataset(rows, scopes, replicas=2):
    snapshot = {}
    for scope in range(scopes):
        cid = f'cluster-{scope // 5}'
        ns = f'team-{scope % 5}'
        cluster = snapshot.setdefault(cid, {'name': f'PROD-{scope // 5}', 'namespaces': {}})
        data = {'deployments': [], 'pods': [], 'replica_sets': [], '_stale': False}
        cluster['namespaces'][ns] = data
        for row in range(rows):
            name, uid = f'application-{row:03}', f'deploy-{scope}-{row}'
            dep = {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': {
                'name': name, 'namespace': ns, 'uid': uid, 'resourceVersion': '1'},
                'spec': {'replicas': replicas, 'strategy': {'type': 'RollingUpdate'},
                         'template': {'spec': {'containers': [{'name': 'app', 'image': 'example:1',
                            'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'},
                                          'limits': {'cpu': '1', 'memory': '512Mi'}}}]}}},
                'status': {'availableReplicas': replicas, 'readyReplicas': replicas, 'updatedReplicas': replicas}}
            data['deployments'].append(dep)
            rsid = f'rs-{scope}-{row}'
            data['replica_sets'].append({'metadata': {'uid': rsid, 'ownerReferences': [
                {'kind': 'Deployment', 'uid': uid, 'controller': True}]}})
            for number in range(replicas):
                data['pods'].append({'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {
                    'name': f'{name}-abc-{number}', 'namespace': ns, 'uid': f'pod-{scope}-{row}-{number}',
                    'resourceVersion': '1', 'creationTimestamp': '2026-11-11T08:00:00Z',
                    'ownerReferences': [{'kind': 'ReplicaSet', 'uid': rsid, 'controller': True}]},
                    'spec': {'nodeName': 'worker-1', 'containers': copy.deepcopy(dep['spec']['template']['spec']['containers'])},
                    'status': {'phase': 'Running', 'podIP': '10.1.1.20',
                               'containerStatuses': [{'name': 'app', 'ready': True, 'restartCount': 0}]}})
    return snapshot


class OfflineAPI:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.metrics_calls = 0
        self.stopped = False
    def cluster(self, cid):
        return {'id': cid, 'name': self.snapshot[cid]['name'], 'enabled': True}
    def namespaces(self, cid):
        return list(self.snapshot[cid]['namespaces'])
    def deployments(self, cid, ns):
        return self.snapshot[cid]['namespaces'][ns]['deployments']
    def pods(self, cid, ns):
        return self.snapshot[cid]['namespaces'][ns]['pods']
    def replica_sets(self, cid, ns):
        return self.snapshot[cid]['namespaces'][ns]['replica_sets']
    def resource(self, cid, kind, ns, name):
        values = self.deployments(cid, ns) if kind == 'deployment' else self.pods(cid, ns)
        return copy.deepcopy(next(v for v in values if v['metadata']['name'] == name))
    def events(self, *_):
        return [{'type': 'Warning', 'reason': 'BackOff', 'message': 'Synthetic event for audit', 'count': 3}]
    def logs(self, *_):
        return '\n'.join(f'2026-11-11T10:00:{n:02}Z INFO request completed' for n in range(40))
    def metrics(self, *_):
        self.metrics_calls += 1
        return {'available': True, 'source': 'fixture', 'containers': [{'name': 'app',
            'cpu': {'usageCores': .25, 'limitCores': 1, 'requestCores': .1},
            'memory': {'usageBytes': 268435456, 'limitBytes': 536870912, 'requestBytes': 134217728}}]}
    def list_resources(self, cid, kind, ns):
        if kind == 'quota':
            return [{'metadata': {'name': 'team-quota'}, 'status': {
                'used': {'requests.cpu': '3800m', 'requests.memory': '12Gi', 'pods': '24'},
                'hard': {'requests.cpu': '4', 'requests.memory': '16Gi', 'pods': '30'}}}] if (cid, ns) == ('cluster-0', 'team-0') else []
        return [{'metadata': {'name': 'service-config', 'uid': 'cm-1'}}]


def wait(predicate):
    end = time.monotonic()+5
    while not predicate() and time.monotonic() < end:
        QTest.qWait(10)
    if not predicate():
        raise RuntimeError('Offline UI callback timeout')


app = QApplication.instance() or QApplication([])
app.setStyle('Fusion')
results = {'environment': {'python': sys.version, 'platform': platform.platform(),
                          'Qt': QT_VERSION_STR, 'PyQt': PYQT_VERSION_STR, 'mode': 'offscreen'}}
ui_only = '--ui-only' in sys.argv
if ui_only and (OUT/'results.json').exists():
    results = json.loads((OUT/'results.json').read_text(encoding='utf-8'))
with tempfile.TemporaryDirectory(prefix='quaber-offline-audit-') as folder:
    store = UserStore(Path(folder))
    with patch('quaber.ui.locate_kubectl', return_value=Path('unused.exe')), \
         patch.object(MainWindow, 'refresh'), patch.object(MainWindow, '_prompt_expired_login'), \
         patch.object(Kubernetes, 'request', side_effect=AssertionError('Network prohibited in audit')):
        window = MainWindow(store, ROOT)
        window.timer.stop()
        window.auth_timer.stop()
        window.resize(1366, 768)
        window.show()
        QTest.qWait(30)
        timings = []
        for rows, scopes, replicas in ([] if ui_only else [(50, 20, 2), (200, 50, 2), (500, 80, 1)]):
            window.snapshot = dataset(rows, scopes, replicas)
            samples = []
            for _ in range(3):
                start = time.perf_counter()
                window.render_matrix()
                samples.append(round((time.perf_counter()-start)*1000, 2))
                app.processEvents()
            timings.append({'rows': rows, 'scopes': scopes, 'pods': rows*scopes*replicas,
                            'render_ms': samples, 'median_ms': statistics.median(samples)})
        if not ui_only:
            results['render'] = timings
            profiler = cProfile.Profile()
            profiler.runcall(window.render_matrix)
            stream = io.StringIO()
            pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats('cumulative').print_stats(22)
            (OUT/'profile.txt').write_text(stream.getvalue(), encoding='utf-8')
        window.snapshot = dataset(1, 1, 1)
        broken_scope = window.snapshot['cluster-0']['namespaces']['team-0']
        broken_scope['pods'] = []
        broken_scope['deployments'][0]['status'] = {'availableReplicas': 0, 'readyReplicas': 0}
        window.render_matrix()
        results['unavailable_deployment_without_pods'] = {'column_hidden': window.matrix.isColumnHidden(1),
                                                        'counter': window.summary_label.text()}
        window.snapshot = dataset(12, 10, 3)
        scope = window.snapshot['cluster-0']['namespaces']['team-0']
        stray = copy.deepcopy(scope['pods'][0])
        stray['metadata'].update(name='database-0', uid='stateful-pod', ownerReferences=[
            {'kind': 'StatefulSet', 'uid': 'stateful', 'controller': True}])
        stray['status'] = {'phase': 'Pending'}
        scope['pods'].append(stray)
        scope['pods'][1]['status']['containerStatuses'][0].update(ready=False, state={'waiting': {'reason': 'CrashLoopBackOff'}})
        window.render_matrix()
        app.processEvents()
        window.grab().save(str(OUT/'matrix-dark.png'))
        visible = [p['metadata']['name'] for ref in window.matrix_data.values() for p in ref[4]]
        results['stateful_pod_visible'] = 'database-0' in visible
        window._select_theme('light')
        app.processEvents()
        window.grab().save(str(OUT/'matrix-light.png'))
        window._select_theme('dark')
        fake = OfflineAPI(window.snapshot)
        window.api = fake
        store.clusters = lambda: [fake.cluster(cid) for cid in fake.snapshot]
        window.open_resource('cluster-0', 'deployment', 'team-0', 'application-000')
        wait(lambda: getattr(window, '_resource_panel', None) is not None)
        parent = window._resource_panel
        wait(lambda: not window.background.running)
        for timer in parent.findChildren(QTimer):
            timer.stop()
        results['deployment_metrics_tab_rows'] = next(parent.tabs.widget(i).rowCount() for i in range(parent.tabs.count()) if parent.tabs.tabText(i) == 'Метрики')
        parent.tabs.setCurrentIndex(next(i for i in range(parent.tabs.count()) if parent.tabs.tabText(i) == 'Pods'))
        app.processEvents()
        window.grab().save(str(OUT/'deployment.png'))
        def capture_operation(dialog, filename):
            dialog.show()
            app.processEvents()
            dialog.grab().save(str(OUT/filename))
            dialog.reject()
            dialog.deleteLater()
            return QDialog.DialogCode.Rejected
        with patch('quaber.ui.run_dialog', side_effect=lambda d: capture_operation(d, 'scale-dialog.png')):
            parent._scale_dialog('HPA: synthetic check')
        with patch('quaber.ui.QInputDialog.getItem', return_value=('app', True)), \
             patch('quaber.ui.run_dialog', side_effect=lambda d: capture_operation(d, 'resources-dialog.png')):
            parent.edit_resources()
        window.open_resource('cluster-0', 'pod', 'team-0', 'application-000-abc-0', initial_tab='Логи')
        wait(lambda: len(window._resource_stack) == 2)
        child = window._resource_panel
        wait(lambda: not window.background.running)
        for timer in child.findChildren(QTimer):
            timer.stop()
        app.processEvents()
        window.grab().save(str(OUT/'pod-logs.png'))
        for tab, filename in [('Консоль Pod', 'pod-console.png'), ('YAML', 'pod-yaml.png'),
                              ('Метрики', 'pod-metrics.png'), ('События', 'pod-events.png')]:
            child.tabs.setCurrentIndex(next(i for i in range(child.tabs.count()) if child.tabs.tabText(i) == tab))
            QTest.qWait(40)
            wait(lambda: not window.background.running)
            window.grab().save(str(OUT/filename))
        child.close()
        app.processEvents()
        window.preferences['read_only'] = False
        mutation_probe = Mock()
        parent._mutate(mutation_probe, lambda _: None)
        QTest.qWait(30)
        results['after_child_close'] = {'navigation_pending': parent._navigation_pending,
                                       'mutation_started': mutation_probe.called}
        parent._navigation_pending = False
        parent.tabs.setCurrentIndex(0)
        old_summary = parent.tabs.widget(0).toHtml()
        newer = copy.deepcopy(parent.resource)
        newer['metadata']['resourceVersion'] = '2'
        newer['spec']['replicas'] = 7
        with patch.object(QMessageBox, 'information'):
            parent._updated(newer)
        results['details_unchanged_after_update'] = old_summary == parent.tabs.widget(0).toHtml()
        parent.close()
        app.processEvents()
        window.tabs.setCurrentIndex(1)
        window.load_quotas()
        wait(lambda: not window._loading_quotas)
        app.processEvents()
        window.grab().save(str(OUT/'quotas.png'))
        window.tabs.setCurrentIndex(2)
        window._refresh_resource_controls('configmap')
        wait(lambda: not window.background.running)
        app.processEvents()
        window.grab().save(str(OUT/'configmaps.png'))
        window.tabs.setCurrentIndex(3)
        app.processEvents()
        window.grab().save(str(OUT/'journal.png'))
        def capture_dialog(dialog):
            dialog.show()
            app.processEvents()
            dialog.grab().save(str(OUT/'settings.png'))
            dialog.reject()
            dialog.deleteLater()
            return QDialog.DialogCode.Rejected
        # The settings list needs only display metadata, never credentials.
        store.clusters = lambda: [dict(fake.cluster(cid), server='https://example.invalid', username='demo', loginTime='2099-01-01T00:00:00+00:00') for cid in fake.snapshot]
        store.login_required = lambda _: False
        with patch('quaber.ui.run_dialog', side_effect=capture_dialog):
            window.clusters_dialog()
        window.tabs.setCurrentIndex(0)
        window.resize(900, 600)
        app.processEvents()
        window.grab().save(str(OUT/'matrix-small.png'))
        results['small_window_actual_size'] = [window.width(), window.height()]
        def visual_rows():
            header = window.matrix.verticalHeader()
            return [window.matrix.item(header.logicalIndex(i), 0).text() for i in range(4)]
        window.matrix.verticalHeader().moveSection(0, 2)
        before_refresh = visual_rows()
        window.render_matrix()
        results['row_order_after_refresh'] = {'before': before_refresh, 'after': visual_rows()}
        window.close()
        window._cleanup_thread.join(5)
        window.deleteLater()
        app.processEvents()

prefs = {'views': [{'id': 'default', 'name': 'Main', 'columnWidths': {'c/n': 99}, 'rowHeights': {'app': 75}}]}
results['geometry_after_validation'] = validate_preferences(prefs)['views'][0]
samples = ThrottleSamples()
def counters(periods, throttled):
    return '\n'.join(f'container_cpu_cfs_{k}{{namespace="team",pod="pod",container="app"}} {v}'
                     for k, v in [('periods_total', periods), ('throttled_periods_total', throttled)])
samples.parse(counters(100, 10), 'c', 'team', 'pod')
results['throttle_fresh'] = samples.parse(counters(200, 60), 'c', 'team', 'pod')
results['throttle_same_cached_sample'] = samples.parse(counters(200, 60), 'c', 'team', 'pod')
(OUT/'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(results, ensure_ascii=False, indent=2))
