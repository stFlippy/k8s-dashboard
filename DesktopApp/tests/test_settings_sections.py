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
    def test_https_and_full_thread_range_persist_and_apply_with_debug_hidden_after_restart(self):
        from quaber.ui import MainWindow
        from quaber.shutdown import release_window
        self.window.api, _, _ = fake_api(cluster_count=1)
        self.api = self.window.api
        def inspect(dialog):
            dialog.show()
            self.app.processEvents()
            QTest.keyClicks(dialog, '1994')
            spinner = dialog.findChild(QSpinBox, 'clusterIoThreads')
            self.assertEqual((spinner.minimum(), spinner.maximum()), (0,50))
            spinner.setValue(50)
            spinner.setValue(0)
            rate = dialog.findChild(QSpinBox, 'clusterRequestRate')
            self.assertEqual(rate.value(), 2)
            rate.setValue(10)
            self.assertEqual(self.window.api.request_policy.limits.cluster_rate, 10)
            combo = dialog.findChild(QComboBox, 'apiTransport')
            self.assertEqual(combo.currentData(), 'proxy')
            combo.setCurrentIndex(combo.findData('https'))
            self.assertEqual(self.window.api.transport_mode, 'https')
            dialog.accept()
            return QDialog.Accepted
        with patch('quaber.ui.run_dialog', side_effect=inspect): self.window.clusters_dialog()
        QTest.qWait(250)
        self.assertEqual(self.window.store.preferences()['api_transport'], 'https')
        self.assertEqual(self.window.store.preferences()['cluster_io_threads'], 0)
        self.assertEqual(self.window.store.preferences()['cluster_request_rate'], 10)
        with patch.object(MainWindow, 'refresh'), patch.object(MainWindow, '_prompt_expired_login'), patch.object(MainWindow, '_load_initial_matrix'):
            reopened = MainWindow(self.window.store, self.window.app_dir)
        try:
            self.assertEqual(reopened.api.transport_mode, 'https')
            self.assertEqual(configured_threads(reopened.api), 0)
            self.assertEqual(reopened.api.request_policy.limits.cluster_rate, 10)
            self.assertEqual(reopened.api.request_policy.endpoint_budget.rate, 10)
            self.assertFalse(getattr(reopened, '_debug_tools_unlocked', False))
        finally:
            reopened.close()
            reopened._cleanup_thread.join(5)
            release_window(reopened)
            self.app.processEvents()

    def test_sections_unlock_only_for_current_session_and_code_is_not_saved_as_path(self):
        self.window.api, _, _ = fake_api(cluster_count=1)
        self.api = self.window.api
        calls = []
        def inspect(dialog):
            dialog.show()
            self.app.processEvents()
            self.assertEqual([dialog.sections.item(i).text() for i in range(3)], ['Общее', 'Учётные записи', 'Подключения'])
            if not calls:
                self.assertEqual(dialog.sections.count(), 3)
                self.assertFalse(self.window.store.preferences().get('debug_tools_unlocked', False))
                client = next(w for w in dialog.findChildren(QLineEdit) if w.placeholderText() == 'Автоматически из PATH')
                client.setFocus()
                QTest.keyClicks(client, '1994')
                self.assertEqual(client.text(), '')
                self.assertEqual(dialog.sections.count(), 4)
                dialog.sections.setCurrentRow(3)
                spinner = dialog.findChild(QSpinBox, 'clusterIoThreads')
                self.assertEqual(spinner.value(), 1)
                spinner.setValue(2)
                self.assertEqual(configured_threads(self.api), 2)
                self.assertEqual(self.api.request_policy.cluster_concurrency, 2)
            else:
                self.assertEqual(dialog.sections.count(), 4)
                self.assertEqual(dialog.findChild(QSpinBox, 'clusterIoThreads').value(), 2)
            calls.append(True)
            dialog.accept()
            return QDialog.Accepted
        with patch('quaber.ui.run_dialog', side_effect=inspect):
            self.window.clusters_dialog()
            self.window.clusters_dialog()
        QTest.qWait(250)
        self.assertEqual(self.window.store.preferences()['cluster_io_threads'], 2)
        self.assertNotIn('debug_tools_unlocked', self.window.store.preferences())

    def test_saved_threads_are_applied_on_startup_while_debug_remains_hidden(self):
        from quaber.ui import MainWindow
        from quaber.shutdown import release_window
        self.window.store.save_preferences(dict(self.window.store.preferences(), cluster_io_threads=3))
        with patch.object(MainWindow, 'refresh'), patch.object(MainWindow, '_prompt_expired_login'), patch.object(MainWindow, '_load_initial_matrix'):
            reopened = MainWindow(self.window.store, self.window.app_dir)
        try:
            self.assertEqual(configured_threads(reopened.api), 3)
            self.assertEqual(reopened.api.request_policy.cluster_concurrency, 3)
            self.assertFalse(getattr(reopened, '_debug_tools_unlocked', False))
        finally:
            reopened.close()
            reopened._cleanup_thread.join(5)
            release_window(reopened)
            self.app.processEvents()

    def test_invalid_saved_thread_count_falls_back_to_one(self):
        from quaber.views import validate_preferences
        for value in (-1, 51, True, '2', None):
            self.assertEqual(validate_preferences({'cluster_io_threads': value})['cluster_io_threads'], 1)

    def test_background_sequence_unlocks_but_wrong_sequence_does_not(self):
        self.window.api, _, _ = fake_api(cluster_count=1)
        def inspect(dialog):
            dialog.show()
            self.app.processEvents()
            QTest.keyClicks(dialog, '1949')
            self.assertEqual(dialog.sections.count(), 3)
            QTest.keyClicks(dialog, '1994')
            self.assertEqual(dialog.sections.count(), 4)
            dialog.accept()
            return QDialog.Accepted
        with patch('quaber.ui.run_dialog', side_effect=inspect): self.window.clusters_dialog()


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
