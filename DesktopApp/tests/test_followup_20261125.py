"""User-visible regressions: YAML discard and complete server log export."""
from pathlib import Path
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch
from PyQt5.QtWidgets import QMessageBox
from test_ux import ShellFixture
from quaber.request_policy import RequestPolicy
from quaber.matrix_controller import MatrixController


class FollowupTests(ShellFixture, unittest.TestCase):
    def test_hidden_resource_lists_and_quotas_do_not_submit_reads(self):
        window = self.window
        with patch.object(window.background, 'submit') as submit:
            window.load_quotas()
            window._load_namespaces_for('configmap')
            window.load_resource_page('configmap')
            submit.assert_not_called()
        window.tabs.setCurrentWidget(window.configmaps_page)
        old_cancel = window._workspace_cancel
        with patch.object(window.background, 'submit', return_value=True) as submit:
            window.open_resource('c', 'deployment', 'ns', 'app')
            self.assertTrue(old_cancel.is_set())
            window.load_resource_page('configmap')
            self.assertEqual(submit.call_count, 1)
            submit.call_args.kwargs['finalized']('CANCELLED')

    def test_opening_a_card_cancels_matrix_before_the_card_request_finishes(self):
        window = self.window
        canceled = window._matrix_cancel = threading.Event()
        window.loading_matrix = True
        window.matrix_paint_timer.start(1000)
        with patch.object(window.background, 'submit', return_value=True) as submit:
            window.open_resource('c', 'deployment', 'ns', 'app')
            self.assertTrue(canceled.is_set())
            self.assertFalse(window.loading_matrix)
            self.assertFalse(window.matrix_paint_timer.isActive())
            MatrixController(window).refresh()
            self.assertEqual(submit.call_count, 1)
            submit.call_args.kwargs['finalized']('CANCELLED')

    def test_discard_yaml_revokes_admin_but_cancel_keeps_the_draft(self):
        panel = self.open_panel('deployment')
        document = panel.yaml_editor.editor.document()
        self.window.set_read_only(False)
        document.setModified(True)
        with patch('quaber.ui.QMessageBox.warning', return_value=QMessageBox.Cancel):
            self.assertFalse(panel.can_leave())
        self.assertFalse(self.window.preferences['read_only'])
        self.assertTrue(document.isModified())
        with patch('quaber.ui.QMessageBox.warning', return_value=QMessageBox.Discard):
            self.assertTrue(panel.can_leave())
        self.assertTrue(self.window.preferences['read_only'])
        self.assertTrue(panel.yaml_editor.editor.isReadOnly())

    def test_save_fetches_complete_server_log_without_the_preview_limit(self):
        panel = self.open_panel('pod')
        view = panel.log_view
        full = b'first server line\n' + b'x' * (5 * 1024 * 1024) + b'\nlast server line\n'
        view._display_text = 'last server line\n'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'full.log'
            def download(cid, namespace, pod, container, previous, destination, *, cancel=None):
                self.assertFalse(previous)
                self.assertFalse(cancel.is_set())
                return Path(destination).write_bytes(full)
            self.api.download_logs.side_effect = download
            with patch('quaber.pod_logs.QFileDialog.getSaveFileName', return_value=(str(path), '')):
                view.export()
            self.wait()
            self.assertEqual(path.read_bytes(), full)
            self.api.download_logs.assert_called_once()
            self.assertFalse(view._download_running)
            self.assertEqual(view._display_text, 'last server line\n')


class IndependentClusterTests(unittest.TestCase):
    def test_six_clusters_dispatch_in_parallel_with_independent_budgets(self):
        policy = RequestPolicy()
        barrier = threading.Barrier(7, timeout=3)
        def request(index):
            with policy.slot(f'cluster-{index}', lambda: None, heavy=True, long_download=True):
                barrier.wait()
        with ThreadPoolExecutor(max_workers=6) as workers:
            tasks = [workers.submit(request, index) for index in range(6)]
            barrier.wait()
            for task in tasks:
                task.result(timeout=3)
        self.assertEqual(policy.diagnostics()['peak'], 6)
        self.assertEqual(policy.inflight, 0)
