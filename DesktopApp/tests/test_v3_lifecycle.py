import copy
import json
import time
import unittest
from types import SimpleNamespace
from urllib.parse import urlsplit
from PyQt5.QtCore import QCoreApplication, QEvent, QTimer
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
from unittest.mock import patch
from quaber.ui import ResourceDialog
from test_v3_requests import fake_api
import test_perf_matrix as fixture


class LifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])
    def setUp(self): fixture.MatrixTests.setUp(self)
    def tearDown(self): fixture.MatrixTests.tearDown(self)

    def wait(self, predicate):
        until = time.monotonic()+10
        while not predicate() and time.monotonic() < until: QTest.qWait(2)
        self.assertTrue(predicate())

    def drain(self):
        self.wait(lambda: not self.window.background.running and not self.window.background._pending)
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def test_thirty_real_deployment_pod_back_cycles_with_logs_metrics_yaml_and_sleep(self):
        from quaber.metrics import ThrottleSamples
        w = self.window
        api, clock, clusters = fake_api(cluster_count=1)
        api.throttle_samples = ThrottleSamples()
        deployment = {'kind': 'Deployment', 'apiVersion': 'apps/v1',
            'metadata': {'name': 'app', 'namespace': 'ns', 'uid': 'd', 'resourceVersion': '1'},
            'spec': {'replicas': 1, 'template': {'spec': {'containers': [{'name': 'app'}]}}},
            'status': {'replicas': 1, 'readyReplicas': 1, 'availableReplicas': 1}}
        pod = {'kind': 'Pod', 'apiVersion': 'v1', 'metadata': {'name': 'pod', 'namespace': 'ns',
            'uid': 'p', 'resourceVersion': '1', 'ownerReferences': [{'controller': True, 'kind': 'Deployment', 'uid': 'd'}]},
            'spec': {'containers': [{'name': 'app'}]}, 'status': {'phase': 'Running'}}
        def route(req):
            path = urlsplit(req.full_url).path
            if path.endswith('/deployments/app'): value = deployment
            elif path.endswith('/pods/pod'): value = pod
            elif path.endswith('/log'): return 200, {}, b'synthetic log\n'
            elif path == '/api/v1/namespaces': value = {'items': [{'metadata': {'name': 'ns'}}]}
            elif path.startswith('/apis/metrics.k8s.io/'):
                value = {'items': [{'metadata': {'name': 'pod'}, 'containers': [{'name': 'app', 'usage': {'cpu': '1m'}}]}]}
            elif path.endswith('/pods'): value = {'items': [pod]}
            elif path.endswith('/deployments'): value = {'items': [deployment]}
            elif path.endswith('selfsubjectaccessreviews'): value = {'status': {'allowed': False}}
            else: value = {'items': []}
            return 200, {}, copy.deepcopy(value)
        api.opener.route = route
        api.monitoring = w.monitoring
        w.api = api
        w.store.clusters = lambda: copy.deepcopy(clusters)
        w.store.login_required = lambda cluster: False
        w._resource_panel = None
        w.views.active['selectedClusterIds'] = ['c0']
        w.views.active['selectedNamespaces'] = ['ns']
        w.show()
        self.app.processEvents()
        samples = []
        with patch.object(w.monitoring.session, 'own_foreground', return_value=True):
            for cycle in range(30):
                w.open_resource('c0', 'deployment', 'ns', 'app', expected_uid='d')
                self.wait(lambda: w._resource_panel is not None and w._resource_panel.kind == 'deployment')
                self.drain()
                parent = w._resource_panel
                w.open_resource('c0', 'pod', 'ns', 'pod', expected_uid='p')
                self.wait(lambda: w._resource_panel is not parent and w._resource_panel.kind == 'pod')
                panel = w._resource_panel
                logs = next(i for i in range(panel.tabs.count()) if panel.tabs.tabText(i) == 'Логи')
                panel.tabs.setCurrentIndex(logs)
                self.drain()  # real logs GET and page construction
                metrics = next(i for i in range(panel.tabs.count()) if panel.tabs.tabText(i) == 'Метрики')
                panel.tabs.setCurrentIndex(metrics)
                self.drain()
                yaml = next(i for i in range(panel.tabs.count()) if panel.tabs.tabText(i) == 'YAML')
                panel.tabs.setCurrentIndex(yaml)
                if cycle % 5 == 0:
                    w.monitoring.sleep('lock')
                    attempts = len(api.opener.calls)
                    self.drain()
                    self.assertEqual(len(api.opener.calls), attempts)
                    w.monitoring.reconnect()
                    self.drain()
                    self.assertEqual(w.monitoring.policy.state, 'ACTIVE')
                    self.assertTrue(w.preferences['read_only'])
                self.drain()
                w.go_back()
                self.assertIs(w._resource_panel, parent)
                w.go_back()
                self.assertIsNone(w._resource_panel)
                self.app.processEvents()
                self.drain()
                samples.append({'cycle': cycle+1, 'timers': len(w.findChildren(QTimer)),
                    'cards': len(w.findChildren(ResourceDialog)), 'jobs': len(w.background.running),
                    'pending': len(w.background._pending), 'cache_entries': len(api.cache.entries)})
                self.assertEqual(samples[-1]['cards'], 0)
                self.assertEqual(samples[-1]['jobs'], 0)
                self.assertLessEqual(samples[-1]['cache_entries'], 128)
            self.assertLessEqual(max(s['timers'] for s in samples[-10:]), max(s['timers'] for s in samples[:10]))
        self.assertTrue(any('/log?' in c['path'] for c in api.opener.calls))
        self.assertTrue(any('/metrics.k8s.io/' in c['path'] for c in api.opener.calls))
        self.assertTrue(all(c['method'] == 'GET' or 'selfsubjectaccessreviews' in c['path'] for c in api.opener.calls))
        print(json.dumps({'lifecycle': samples, 'physical_attempts': len(api.opener.calls), 'scope': 'synthetic/offscreen; foreground predicate controlled; no real WTS'}))
