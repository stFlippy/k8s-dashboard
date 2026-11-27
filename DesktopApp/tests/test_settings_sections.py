"""Sectioned settings, secret session controls and configurable network workers."""
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QDialog, QLineEdit, QSpinBox, QComboBox
from quaber.cluster_workers import ClusterWorkers, configure_cluster_threads, configured_threads
from quaber.request_policy import RequestPolicy, LIMITS
from quaber.matrix_presentation import condition_tooltip
from test_ux import ShellFixture
from test_v3_requests import fake_api


class SettingsTests(ShellFixture, unittest.TestCase):
    def test_unified_setting_is_general_persistent_and_transport_remains_hidden(self):
        self.window.api,_,_=fake_api(cluster_count=1)
        self.api=self.window.api
        def inspect(dialog):
            dialog.show();self.app.processEvents()
            self.assertEqual(dialog.sections.count(),3)
            spin=dialog.findChild(QSpinBox,'clusterParallelism')
            self.assertIsNotNone(spin)
            self.assertEqual((spin.minimum(),spin.maximum()),(1,50))
            spin.setValue(4)
            self.assertEqual(configured_threads(self.api),4)
            self.assertEqual(self.api.request_policy.limits.cluster_rate,4)
            QTest.keyClicks(dialog,'1994')
            self.assertEqual(dialog.sections.count(),4)
            self.assertIsNone(dialog.findChild(QSpinBox,'clusterIoThreads'))
            self.assertIsNotNone(dialog.findChild(QComboBox,'apiTransport'))
            dialog.accept();return QDialog.Accepted
        with patch('quaber.ui.run_dialog',side_effect=inspect):self.window.clusters_dialog()
        QTest.qWait(250)
        self.assertEqual(self.window.store.preferences()['cluster_parallelism'],4)
        from quaber.ui import MainWindow
        from quaber.shutdown import release_window
        with patch.object(MainWindow,'refresh'),patch.object(MainWindow,'_prompt_expired_login'),patch.object(MainWindow,'_load_initial_matrix'):
            reopened=MainWindow(self.window.store,self.window.app_dir)
        try:
            self.assertEqual(configured_threads(reopened.api),4)
            self.assertEqual(reopened.api.request_policy.limits.cluster_rate,4)
            self.assertFalse(getattr(reopened,'_debug_tools_unlocked',False))
        finally:
            reopened.close();reopened._cleanup_thread.join(5);release_window(reopened);self.app.processEvents()


class WorkerTests(unittest.TestCase):
    def test_debug_configuration_allows_two_requests_without_changing_rate(self):
        api, _, _ = fake_api(cluster_count=1)
        self.addCleanup(api.close)
        api.request_policy = RequestPolicy(limits=replace(LIMITS, cluster_rate=1000, cluster_burst=10))
        configure_cluster_threads(api, 2)
        self.assertEqual(api.request_policy.limits.cluster_rate, 1000)
        barrier = threading.Barrier(2)
        identities = set()
        def route(request):
            identities.add(threading.get_ident())
            barrier.wait(timeout=2)
            return 200, {}, {'items': []}
        api.opener.route = route
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(api.request, 'c0', '/api/v1/pods') for _ in range(2)]
            for future in futures: future.result(timeout=3)
        self.assertEqual(len(identities), 2)
        self.assertEqual(api.request_policy.stats['peak'], 2)

    def test_resize_waits_for_old_work_without_canceling_or_overlapping_replacement_pool(self):
        workers = ClusterWorkers()
        self.addCleanup(workers.close)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        with ThreadPoolExecutor(max_workers=2) as callers:
            first = callers.submit(workers.run, 'c', lambda: (entered.set(), release.wait(2)))
            self.assertTrue(entered.wait(1))
            workers.configure(2)
            second = callers.submit(workers.run, 'c', lambda: 'next')
            time.sleep(.03)
            self.assertFalse(second.done())
            release.set()
            first.result(timeout=2)
            self.assertEqual(second.result(timeout=2), 'next')

    def test_conditions_without_reason_have_readable_status_instead_of_empty_colons(self):
        resource = dict(status=dict(conditions=[dict(type='Ready', status='True'), {}]))
        self.assertEqual(condition_tooltip(resource, 'fallback'), 'Ready · True')
        self.assertEqual(condition_tooltip(dict(status=dict(conditions=[{}, {}])), 'fallback'), 'fallback')
