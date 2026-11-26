"""Real Qt delivery/worker/physical fake reads; never touches saved UserData."""
import copy
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from PyQt5.QtTest import QTest
from helpers_matrix import MatrixFixture as MatrixTests
from test_v3_requests import fake_api
from quaber.authorization import ClusterAuthorization


class InventoryLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        MatrixTests.setUpClass()
        cls.app = MatrixTests.app

    def setUp(self):
        MatrixTests.setUp(self)
        api, clock, clusters = fake_api()
        cluster = dict(id='c0',name='Synthetic',enabled=True,server='https://synthetic.invalid',
                       username='synthetic',authMode='password',encryptedKubeconfig='old-config')
        class Store:
            root = self.window.store.root
            value = cluster
            def clusters(store): return [copy.deepcopy(store.value)]
            def preferences(store): return {'refresh_ms':0,'read_only':True,'kubectl_path':''}
            def save_preferences(store,prefs): pass
            def login_required(store,c): return False
            def saved_password(store,c): return 'synthetic-password'
            def update_logins(store,values,expected):
                if expected['c0'] != store.value: raise RuntimeError('record changed')
                store.value = dict(store.value,encryptedKubeconfig=values['c0'],updatedAt='renewed')
        self.window.store = self.store = Store()
        api.cluster = lambda cid: self.store.clusters()[0]
        api.store = self.store
        api.monitoring = self.window.monitoring
        api.proxy.close = Mock()
        api.authorization = ClusterAuthorization(self.store,api.proxy)
        self.window.api = self.api = api
        self.window._prompt_expired_login = Mock()
        self.window._load_initial_matrix = Mock()
        self.window.refresh = Mock()
        self.service = self.window.inventory
        self.calls = []
        def route(request):
            self.calls.append(request.selector)
            if request.selector.split('?')[0] == '/api/v1/namespaces':
                return 200,{},dict(items=[dict(metadata={'name':'ns'})])
            if '/deployments' in request.selector:
                return 200,{},dict(items=[dict(metadata={'name':'app','namespace':'ns'},spec={'replicas':1},status={'replicas':1})])
            return 200,{},dict(items=[])
        self.route = api.opener.route = route
        self.window.show()
        self.app.processEvents()

    def tearDown(self): MatrixTests.tearDown(self)

    def wait(self,condition):
        until = time.monotonic()+4
        while not condition() and time.monotonic()<until: QTest.qWait(5)
        self.assertTrue(condition())

    def open_dumps(self):
        self.window.tabs.setCurrentWidget(self.window.dumps_page)
        self.app.processEvents()

    def test_physical_success_loads_rows_and_clears_operation(self):
        self.open_dumps()
        self.wait(lambda:self.service.get('c0') is not None and not self.service.loading)
        self.assertEqual(self.window.dumps_page.table.rowCount(),1)
        self.assertEqual(self.service.state('c0'),'READY')
        self.assertFalse(self.service.operations)
        self.assertEqual(len(self.calls),3)

    def test_rejected_admission_has_no_orphan_and_allows_explicit_retry(self):
        with patch.object(self.window.background,'submit',return_value=False):
            self.open_dumps()
        self.assertEqual(self.service.state('c0'),'REJECTED')
        self.assertFalse(self.service.operations)
        self.assertFalse(self.service.loading)
        self.assertFalse(self.window.dumps_page.waiting())
        self.assertIn('inflight=—',self.window.dumps_page.status.toolTip())
        self.service.refresh({'c0'},force=True)
        self.wait(lambda:self.service.state('c0')=='READY')
        self.assertEqual(self.window.dumps_page.table.rowCount(),1)

    def test_orphan_loading_projection_is_recoverable_and_diagnosed(self):
        self.service.loading['c0'] = 123
        with self.assertLogs('quaber.inventory',level='ERROR') as logs:
            self.assertEqual(self.service.state('c0'),'REJECTED')
        self.assertIn('LIFECYCLE_DEFECT',''.join(logs.output))
        self.assertFalse(self.service.loading)

    def test_cancelled_read_releases_own_loading_and_retry_is_possible(self):
        from quaber.monitoring import MonitoringCancelled
        self.api.opener.route = Mock(side_effect=MonitoringCancelled('synthetic local cancel'))
        self.open_dumps()
        self.wait(lambda:not self.window.background.running and not self.service.loading)
        self.assertEqual(self.service.state('c0'),'CANCELLED')
        self.assertFalse(self.window.dumps_page.waiting())
        self.api.opener.route = self.route
        self.service.refresh({'c0'})
        # Deployment rows are deliberately published before a slow CronJob
        # read. A partial snapshot is not terminal success for this test.
        self.wait(lambda:self.service.get('c0') is not None and self.service.state('c0')=='READY')
        self.assertEqual(self.service.state('c0'),'READY')

    def test_own_401_renewal_replans_once_with_new_binding(self):
        first = [True]
        def route(request):
            if first[0]:
                first[0] = False
                return 401,{},dict(message='synthetic expired')
            return self.route(request)
        self.api.opener.route = route
        with patch('quaber.authorization.locate_kubectl',return_value=Path('synthetic.exe')), \
             patch('quaber.authorization.login',return_value='renewed-config') as login:
            self.open_dumps()
            self.wait(lambda:self.service.get('c0') is not None and not self.service.loading)
        login.assert_called_once()
        self.assertEqual(self.service.generations['c0'],2)
        self.assertEqual(self.service.state('c0'),'READY')
        self.assertEqual(self.window.dumps_page.table.rowCount(),1)
        self.assertTrue(self.window.preferences['read_only'])

    def test_retired_manual_pending_cannot_block_new_poll_generation(self):
        from quaber.request_policy import manual_read
        start = self.window.background.pool.start
        with patch.object(self.window.background.pool,'start'):
            self.open_dumps()
            running = next(iter(self.window.background.running))
            with manual_read(): self.service.refresh({'c0'},force=True)
            self.assertEqual(len(self.window.background._pending),1)
            self.service.set_demanded(False)
            self.service.set_demanded(True)
            self.service.refresh({'c0'})
            self.assertEqual(len(self.window.background._pending),1)
            self.assertEqual(self.service.generations['c0'],3)
        from quaber.background import TaskRunner
        start(TaskRunner(running))
        self.wait(lambda: self.service.get('c0') is not None and not self.window.background.running)
        self.assertEqual(self.service.state('c0'),'READY')
        self.assertFalse(self.service.loading)
        self.assertFalse(self.window.background._pending)

    def test_deployments_visible_while_cron_read_is_still_running(self):
        import threading
        gate, entered = threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        def route(request):
            if '/cronjobs' in request.selector:
                entered.set()
                gate.wait(3)
            return self.route(request)
        self.api.opener.route = route
        self.open_dumps()
        self.wait(lambda:entered.is_set() and self.window.dumps_page.table.rowCount()==1)
        self.assertEqual(self.service.state('c0'),'LOADING')
        self.assertTrue(self.service.get('c0')['partial'])
        self.assertFalse(self.window.dumps_page.export_button.isEnabled())
        gate.set()
        self.wait(lambda:not self.service.loading)
        self.assertFalse(self.service.get('c0').get('partial',False))
        self.assertEqual(len(self.calls),3)

    def test_rejected_renewed_credentials_do_not_loop_login_or_replan(self):
        self.api.opener.route = lambda request:(401,{},dict(message='synthetic rejected credentials'))
        with patch('quaber.authorization.locate_kubectl',return_value=Path('synthetic.exe')), \
             patch('quaber.authorization.login',return_value='renewed-config') as login:
            self.open_dumps()
            self.wait(lambda:self.service.state('c0') == 'FAILED' and not self.window.background.running)
        login.assert_called_once()
        self.assertEqual(self.service.generations['c0'],2)
        self.assertFalse(self.service.loading)
        self.assertIn('Sign-in required',' '.join(self.service.get('c0')['errors']))

    def test_external_subject_change_cancels_without_automatic_rebinding(self):
        def route(request):
            value = self.route(request)
            self.store.value = dict(self.store.value,username='external-subject')
            return value
        self.api.opener.route = route
        self.open_dumps()
        self.wait(lambda:not self.window.background.running and not self.service.loading)
        self.assertEqual(self.service.generations['c0'],1)
        self.assertEqual(self.service.state('c0'),'CANCELLED')
        self.assertIsNone(self.service.get('c0'))
        self.assertEqual(len(self.api.opener.calls),1)
        self.assertTrue(self.window.preferences['read_only'])

    def test_scoped_fallback_publishes_deployments_once_before_slow_cron(self):
        import threading
        gate, entered = threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        def route(request):
            path = request.selector.split('?')[0]
            if '/cronjobs' in path:
                entered.set()
                gate.wait(3)
            if path in ('/apis/apps/v1/deployments','/apis/batch/v1/cronjobs'):
                return 403,{},dict(message='synthetic aggregate forbidden')
            return self.route(request)
        self.api.opener.route = route
        self.open_dumps()
        self.wait(lambda:entered.is_set() and self.window.dumps_page.table.rowCount()==1)
        self.assertEqual(self.service.state('c0'),'LOADING')
        gate.set()
        self.wait(lambda:not self.service.loading)
        self.assertEqual(self.service.state('c0'),'READY')
        paths = [call['path'].split('?')[0] for call in self.api.opener.calls]
        self.assertEqual(paths.count('/apis/apps/v1/namespaces/ns/deployments'),1)
        self.assertEqual(len(paths),5)

    def test_shared_page_demand_ignores_late_hide_and_hidden_refresh(self):
        self.open_dumps()
        self.wait(lambda:self.service.state('c0') == 'READY')
        dumps, versions = self.window.dumps_page, self.window.versions_page
        self.window.tabs.setCurrentWidget(versions)
        self.app.processEvents()
        self.assertTrue(self.service.demanded)
        self.assertIs(self.service._demand_owner,versions)
        self.service.set_demanded(False,owner=dumps)
        self.assertTrue(self.service.demanded)
        self.window.tabs.setCurrentWidget(self.window.matrix_page)
        self.app.processEvents()
        self.assertFalse(self.service.demanded)
        count = len(self.api.opener.calls)
        self.service.refresh({'c0'},force=True)
        self.app.processEvents()
        self.assertEqual(len(self.api.opener.calls),count)

    def test_healthy_cluster_rows_survive_other_cluster_forbidden(self):
        from urllib.parse import urlsplit
        other = dict(self.store.value,id='c1',name='Forbidden fixture')
        self.store.clusters = lambda:[copy.deepcopy(self.store.value),copy.deepcopy(other)]
        self.api.cluster = lambda cid:next(c for c in self.store.clusters() if c['id']==cid)
        def route(request):
            if urlsplit(request.full_url).hostname.startswith('c1.'):
                return 403,{},dict(message='synthetic namespace forbidden')
            return self.route(request)
        self.api.opener.route = route
        self.open_dumps()
        self.wait(lambda:not self.service.loading and self.service.state('c0')=='READY')
        self.assertEqual(self.service.state('c1'),'FAILED')
        self.assertEqual(self.window.dumps_page.table.rowCount(),1)
        self.assertFalse(self.window.dumps_page.export_button.isEnabled())
        self.assertFalse(self.window.dumps_page.waiting())
        self.assertEqual(self.window.monitoring.policy.state,'ACTIVE')
