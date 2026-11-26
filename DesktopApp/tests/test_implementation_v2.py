"""Targeted v2 regressions; isolated synthetic data and physical local exports."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from quaber.background import Background
from quaber.request_policy import manual_read, request_intent
from quaber.resume import core_read, ResumeAttempt
from quaber.monitoring import MonitoringController, MonitoringState


class RegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_REG12_REG13_pending_manual_intent_and_priority_survive_poll(self):
        background = Background()
        seen = []
        from test_v3_requests import fake_api
        api, clock, clusters = fake_api()
        values, served = [], []
        def route(request):
            served.append(len(served)+1)
            return 200, {}, {'items': [{'value': served[-1]}]}
        api.opener.route = route
        def plan():
            seen.append(request_intent.get())
            values.append(api.snapshot_list('c0', '/api/v1/namespaces/ns/pods')[0]['value'])
        with patch.object(background.pool, 'start') as start:
            background.submit(plan, key='scope')
            first = next(iter(background.running))
            first.function()
            with manual_read():
                background.submit(plan, key='scope')
            for _ in range(100): background.submit(plan, key='scope')
            self.assertEqual(len(background._pending), 1)
            background._done(first, None)
            pending = next(iter(background.running))
            pending.function()
            self.assertTrue(seen[-1].fresh)
            self.assertEqual(seen[-1].purpose, 'manual')
            self.assertEqual(start.call_args.args[1], 10)
            self.assertEqual(values, [1, 2])
            self.assertEqual(len(api.opener.calls), 2)
            background._done(pending, None)
        background.stop()

    def test_REG16_stats_never_core(self):
        for path in ('/api/v1/nodes/n/proxy/stats/summary', '/api/v1/namespaces/ns/pods/p/log', '/api/v1/namespaces/ns/events'):
            self.assertFalse(core_read(path))
        self.assertTrue(core_read('/apis/apps/v1/namespaces/ns/deployments'))

    def test_GRID_visual_neighbours_move_hide_restore(self):
        from PyQt5.QtWidgets import QTableWidget
        from quaber.widgets import CLUSTER_ID_ROLE, visual_cluster_boundary
        table = QTableWidget(1, 5)
        table.setHorizontalHeaderLabels(['', 'a1', 'a2', 'b1', 'b2'])
        header = table.horizontalHeader()
        for logical, cluster in enumerate(('frozen', 'A', 'A', 'B', 'B')):
            table.model().setHeaderData(logical, Qt.Horizontal, cluster, CLUSTER_ID_ROLE)
        self.assertFalse(visual_cluster_boundary(header, 1))
        self.assertTrue(visual_cluster_boundary(header, 2))
        header.moveSection(3, 2)
        self.assertTrue(visual_cluster_boundary(header, 1))
        self.assertTrue(visual_cluster_boundary(header, 3))
        table.hideColumn(3)
        self.assertFalse(visual_cluster_boundary(header, 1))
        saved = header.saveState()
        header.moveSection(4, 1)
        header.restoreState(saved)
        self.assertFalse(visual_cluster_boundary(header, 1))
        self.assertFalse(visual_cluster_boundary(header, 4))
        table.deleteLater()

    def test_REG19_callback_at_deadline_cannot_claim_connection_ready(self):
        policy = MonitoringState(clock=lambda: 30)
        policy.sleep('inactivity')
        policy.reconnect()
        owner = SimpleNamespace(policy=policy, attempt=ResumeAttempt.start(policy.epoch, [], 0),
                                valid=policy.allows, changed=Mock(), window=Mock(), resume_results={})
        owner._finish_resume_wait = lambda **kwargs: MonitoringController._finish_resume_wait(owner, **kwargs)
        from dataclasses import replace
        token = replace(policy.token(), attempt_id=owner.attempt.attempt_id)
        MonitoringController.observe_read(owner, token)
        self.assertIsNone(owner.attempt)
        self.assertEqual(policy.state, 'ACTIVE')
        self.assertEqual(owner.resume_results, {})

    def test_REG22_cancel_not_failed_and_finalizes_once(self):
        background = Background()
        failed, cancel, final = Mock(), Mock(), Mock()
        with patch.object(background.pool, 'start'):
            background.submit(lambda: None, failed=failed, cancelled=cancel, finalized=final)
            task = next(iter(background.running))
            background._cancelled(task)
        failed.assert_not_called()
        cancel.assert_called_once_with()
        final.assert_called_once_with('CANCELLED')
        self.assertFalse(background.running)
        self.assertFalse(background._keys)
        background.stop()

    def test_REG24_cleanup_exception_is_not_hidden(self):
        background = Background()
        with patch.object(background.pool, 'start'):
            background.submit(lambda: None, finalized=Mock(side_effect=ValueError('cleanup failure')))
            task = next(iter(background.running))
            with self.assertRaisesRegex(ValueError, 'cleanup failure'):
                background._done(task, None)
        self.assertFalse(background.running)
        background.stop()


class ExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from test_perf_matrix import MatrixTests
        MatrixTests.setUp(self)

    def tearDown(self):
        from test_perf_matrix import MatrixTests
        MatrixTests.tearDown(self)

    def test_REG08_csv_xlsx_menu_keyboard_export_survives_sleep(self):
        from quaber.inventory import CSV_FIELDS
        for index, suffix in enumerate(('csv', 'xlsx')):
            page = self.window.dumps_page
            snapshot = dict(cluster_name='Test', server='https://synthetic.invalid', namespace_count=1,
                            records=[], errors=[], captured_at='2026-11-26T00:00:00+00:00')
            page.selected_clusters = lambda: [{'id':'c'}]
            page.service.get_for_cluster = lambda cluster: snapshot
            path = Path(self.temp.name)/('export.'+suffix)
            page.export_button.setEnabled(True)
            jobs = []
            def submit(fn, done, failed, **options):
                jobs.append((fn, done, failed, options))
                return True
            menu = page.export_button.menu()
            with patch('quaber.inventory_pages.QFileDialog.getSaveFileName', return_value=(str(path), '')), \
                 patch.object(self.window.background, 'submit', side_effect=submit):
                menu.popup(page.mapToGlobal(page.rect().center()))
                menu.setActiveAction(menu.actions()[index])
                QTest.keyClick(menu, Qt.Key_Return)
            self.assertEqual(len(jobs), 1)
            fn, done, failed, options = jobs[0]
            self.assertIs(options['monitor'], False)
            self.window.monitoring.sleep('inactivity')
            done(fn())
            options['finalized']('ACCEPTED')
            self.assertTrue(path.is_file())
            self.assertFalse(page.exporting)
            self.assertTrue(page.refresh_button.isEnabled())
