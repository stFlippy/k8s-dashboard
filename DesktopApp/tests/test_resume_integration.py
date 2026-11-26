"""Scoped physical evidence and own-renewal replans; no real cluster or secrets."""
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QPushButton, QWidget
import test_inventory_lifecycle as fixture
from quaber.background import PanelBackground
from quaber.monitoring import optional_read
from quaber.request_policy import credentials_revision
from quaber.shutdown import release_window


class ResumeIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.InventoryLifecycleTests.setUpClass()
        cls.app = fixture.InventoryLifecycleTests.app

    def setUp(self):
        fixture.InventoryLifecycleTests.setUp(self)
        self.window.monitoring.watchdog.stop()

    tearDown = fixture.InventoryLifecycleTests.tearDown
    wait = fixture.InventoryLifecycleTests.wait

    def start(self):
        monitor = self.window.monitoring
        monitor.sleep('inactivity')
        with patch.object(monitor.session, 'own_foreground', return_value=True), patch.object(monitor, 'resume'):
            monitor.reconnect()
        self.assertIsNotNone(monitor.attempt)
        return monitor

    def read(self, path, optional=False):
        work = lambda: self.api.request('c0', path)
        if optional: work = optional_read(work)
        done, failed = Mock(), Mock()
        self.window.background.submit(work, done, failed)
        self.wait(lambda: not self.window.background.running)
        return done, failed

    def test_cache_or_optional_identity_cannot_resume_but_core_can(self):
        monitor = self.start()
        self.read('/api/v1/namespaces/ns/pods/p', optional=True)
        self.assertEqual(monitor.policy.state, 'RECONNECTING')
        self.read('/api/v1/namespaces/other/deployments')
        # All namespaces are demanded on the default matrix.
        self.assertEqual(monitor.policy.state, 'ACTIVE')
        self.assertEqual(monitor.resume_results['c0'], 'Ready')

    def test_resume_button_releases_workspace_before_any_cluster_response(self):
        monitor = self.window.monitoring
        monitor.sleep('inactivity')
        self.assertTrue(monitor.overlay.isVisible())
        with patch.object(monitor.session, 'own_foreground', return_value=True), \
             patch.object(monitor, 'resume') as resume:
            QTest.mouseClick(monitor.button, Qt.LeftButton)
        resume.assert_called_once()
        self.assertEqual(monitor.policy.state, 'RECONNECTING')
        self.assertFalse(monitor.overlay.isVisible())
        self.assertEqual(monitor.resume_results, {'c0': 'Not requested'})
        self.assertTrue(self.window.preferences['read_only'])
        self.assertFalse(self.calls)

    def test_resume_new_plan_is_not_coalesced_behind_canceled_socket(self):
        from quaber.request_policy import RequestPolicy
        self.api.request_policy = RequestPolicy()
        entered, release = threading.Event(), threading.Event()
        old_done, old_failed, new_done, new_failed = Mock(), Mock(), Mock(), Mock()
        original = self.route
        def route(request):
            if not entered.is_set():
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('Test did not release the old socket')
            return original(request)
        self.api.opener.route = route
        background = self.window.background
        background.submit(lambda: self.api.request('c0', '/api/v1/namespaces'),
                          old_done, old_failed, key='matrix-read')
        try:
            self.wait(entered.is_set)
            monitor = self.start()
            background.submit(lambda: 'replacement plan',
                              new_done, new_failed, key='matrix-read')
            self.wait(lambda: new_done.called or new_failed.called)
            new_done.assert_called_once()
            new_failed.assert_not_called()
            self.assertFalse(release.is_set())
            self.assertEqual(monitor.resume_results['c0'], 'Not requested')
            self.assertLessEqual(self.api.request_policy.stats['peak'], 1)
        finally:
            release.set()
        self.wait(lambda: not background.running)
        old_done.assert_not_called()
        old_failed.assert_not_called()
        self.assertEqual(new_done.call_count, 1)

    def test_navigation_during_resume_does_not_pause_the_workspace_again(self):
        monitor = self.start()
        self.window.views.active['selectedNamespaces'] = ['other']
        with patch.object(monitor.session, 'own_foreground', return_value=True):
            monitor.tick()
        self.assertEqual(monitor.policy.state, 'ACTIVE')
        self.assertIsNone(monitor.attempt)
        self.assertFalse(monitor.overlay.isVisible())
        self.assertEqual(monitor.resume_results, {'c0': 'Not requested'})

    def test_statusbar_never_gains_a_connections_button_during_resume(self):
        monitor = self.window.monitoring
        statusbar = self.window.statusBar()
        self.assertEqual(statusbar.findChildren(QPushButton), [])
        now = [monitor.policy.clock()]
        with patch.object(monitor.policy, 'clock', side_effect=lambda: now[0]), \
             patch.object(monitor.session, 'own_foreground', return_value=True):
            self.start()
            self.assertEqual(statusbar.findChildren(QPushButton), [])
            self.api.opener.route = lambda req: (401, {}, {'message': 'expired'})
            self.read('/api/v1/namespaces')
            self.assertEqual(monitor.resume_results['c0'], 'Sign-in required')
            self.assertEqual(statusbar.findChildren(QPushButton), [])
            now[0] += 31
            monitor.tick()
            self.assertEqual(monitor.policy.state, 'ACTIVE')
            self.assertFalse(monitor.overlay.isVisible())
            self.assertEqual(monitor.resume_results['c0'], 'Sign-in required')
            self.assertEqual(statusbar.findChildren(QPushButton), [])
            self.api.opener.route = self.route
            self.store.value = dict(self.store.value, encryptedKubeconfig='signed-in-config')
            self.start()
            self.read('/api/v1/namespaces')
            self.assertEqual(monitor.policy.state, 'ACTIVE')
            self.assertEqual(monitor.resume_results['c0'], 'Ready')
            self.assertEqual(statusbar.findChildren(QPushButton), [])

    def test_different_selected_namespace_is_not_evidence(self):
        self.window.views.active['selectedNamespaces'] = ['ns']
        monitor = self.start()
        self.read('/api/v1/namespaces/other/deployments')
        self.assertEqual(monitor.policy.state, 'RECONNECTING')
        self.read('/api/v1/namespaces/ns/deployments')
        self.assertEqual(monitor.policy.state, 'ACTIVE')

    def test_cache_hit_never_proves_resume(self):
        from concurrent.futures import ThreadPoolExecutor
        path = '/apis/apps/v1/namespaces/ns/deployments'
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(self.api.snapshot_list, 'c0', path).result(timeout=5)
        calls = len(self.api.opener.calls)
        monitor = self.start()
        self.window.background.submit(lambda: self.api.snapshot_list('c0', path), Mock(), Mock())
        self.wait(lambda: not self.window.background.running)
        self.assertEqual(len(self.api.opener.calls), calls)
        self.assertEqual(monitor.policy.state, 'RECONNECTING')

    def test_late_old_scope_after_success_does_not_sleep_new_navigation(self):
        from quaber.resume import PhysicalRead
        monitor = self.start()
        attempt = monitor.attempt
        self.read('/api/v1/namespaces/ns/deployments')
        self.assertEqual(monitor.policy.state, 'ACTIVE')
        self.window.views.active['selectedNamespaces'] = ['other']
        monitor._observe_physical(PhysicalRead(attempt.attempt_id, attempt.epoch, 'c0',
            credentials_revision(self.store.value), '/api/v1/namespaces/ns/pods', 200, False))
        self.assertEqual(monitor.policy.state, 'ACTIVE')
        self.assertIsNone(monitor.token().attempt_id)
        self.read('/api/v1/namespaces/other/deployments')
        self.assertEqual(monitor.policy.state, 'ACTIVE')

    def test_empty_demand_with_enabled_connections_completes_without_fake_ready(self):
        self.window.views.active['selectedClusterIds'] = []
        self.window.monitoring.sleep('inactivity')
        with patch.object(self.window.monitoring.session, 'own_foreground', return_value=True):
            self.window.monitoring.reconnect()
        self.assertEqual(self.window.monitoring.policy.state, 'ACTIVE')
        self.assertEqual(self.window.monitoring.resume_results, {'c0': 'Not requested'})
        self.assertIsNone(self.window.monitoring.attempt)

    def test_scoped_forbidden_means_limited_access_and_does_not_lock_navigation(self):
        monitor = self.start()
        self.api.opener.route = lambda req: (403, {}, {'message': 'synthetic deny'})
        _, failed = self.read('/api/v1/namespaces/ns/deployments')
        self.assertEqual(failed.call_count, 1)
        self.assertEqual(monitor.policy.state, 'ACTIVE')
        self.assertEqual(monitor.resume_results['c0'], 'Access limited')

    def test_panel_own_renewal_rebinds_read_once_preserving_draft_and_lock(self):
        monitor = self.start()
        owner = QWidget(self.window)
        self.addCleanup(release_window, owner)
        owner.api, owner.controller, owner.cluster_id = self.api, self.window, 'c0'
        owner._opened_credentials = credentials_revision(self.store.value)
        owner.draft = 'local unsaved text'
        panel = PanelBackground(self.window.background, owner)
        first = [True]
        def route(request):
            if first[0]:
                first[0] = False
                return 401, {}, {'message': 'expired'}
            return self.route(request)
        self.api.opener.route = route
        done, failed, final = Mock(), Mock(), Mock()
        with patch('quaber.authorization.locate_kubectl', return_value=Path('synthetic.exe')), \
             patch('quaber.authorization.login', return_value='renewed-config') as login:
            panel.submit(lambda: self.api.request('c0','/api/v1/namespaces'), done, failed, finalized=final)
            self.wait(lambda: done.called or failed.called)
        done.assert_called_once()
        failed.assert_not_called()
        final.assert_called_once_with('ACCEPTED')
        login.assert_called_once()
        self.assertEqual(owner._opened_credentials, credentials_revision(self.store.value))
        self.assertEqual(owner.draft, 'local unsaved text')
        self.assertTrue(self.window.preferences['read_only'])
        self.assertEqual(monitor.resume_results['c0'], 'Ready')

    def test_external_edit_is_not_attested_and_does_not_replan_panel(self):
        owner = QWidget(self.window)
        self.addCleanup(release_window, owner)
        owner.api, owner.controller, owner.cluster_id = self.api, self.window, 'c0'
        owner._opened_credentials = credentials_revision(self.store.value)
        panel = PanelBackground(self.window.background, owner)
        def route(req):
            value = self.route(req)
            self.store.value = dict(self.store.value, username='other-account')
            return value
        self.api.opener.route = route
        done, failed, cancelled, final = Mock(), Mock(), Mock(), Mock()
        panel.submit(lambda: self.api.request('c0','/api/v1/namespaces'), done, failed,
                     cancelled=cancelled, finalized=final)
        self.wait(lambda: not self.window.background.running)
        done.assert_not_called()
        failed.assert_not_called()
        cancelled.assert_called_once()
        final.assert_called_once_with('CANCELLED')
        self.assertEqual(len(self.api.opener.calls), 1)

    def test_rejected_renewed_panel_credentials_do_not_loop(self):
        owner = QWidget(self.window)
        self.addCleanup(release_window, owner)
        owner.api, owner.controller, owner.cluster_id = self.api, self.window, 'c0'
        owner._opened_credentials = credentials_revision(self.store.value)
        panel = PanelBackground(self.window.background, owner)
        self.api.opener.route = lambda req: (401, {}, {'message': 'rejected'})
        done, failed, final = Mock(), Mock(), Mock()
        with patch('quaber.authorization.locate_kubectl', return_value=Path('synthetic.exe')), \
             patch('quaber.authorization.login', return_value='renewed-config') as login:
            panel.submit(lambda: self.api.request('c0','/api/v1/namespaces'), done, failed, finalized=final)
            self.wait(lambda: failed.called)
        done.assert_not_called()
        failed.assert_called_once()
        final.assert_called_once_with('FAILED')
        login.assert_called_once()
        self.assertEqual(len(self.api.opener.calls), 2)
