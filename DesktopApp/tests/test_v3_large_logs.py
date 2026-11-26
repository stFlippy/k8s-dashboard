import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QWidget
from quaber.background import Background
from quaber.pod_logs import PodLogs


class LargeLogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.owner = QWidget()
        self.owner.controller = QWidget(self.owner)
        self.owner.background = Background(self.owner)
        self.owner.cluster_id, self.owner.namespace, self.owner.name = 'c', 'ns', 'pod'
        self.server_log = 'complete server current log\n'
        def download(cid, namespace, pod, container, previous, destination, *, cancel=None):
            self.assertEqual((cid, namespace, pod, container, previous), ('c', 'ns', 'pod', 'app', False))
            return Path(destination).write_bytes(self.server_log.encode('utf-8'))
        self.owner.api = SimpleNamespace(download_logs=download)
        self.view = PodLogs(self.owner, ['app'], defer_initial=True)
        self.view.source = ('app', False, None)
        self.view.timer.stop()
        self.owner.resize(800, 600)
        self.view.resize(800, 600)
        self.owner.show()
        self.app.processEvents()

    def tearDown(self):
        self.view.cancel_preparation()
        self.wait(lambda: not self.owner.background.running)
        self.owner.background.stop()
        self.owner.deleteLater()
        self.app.processEvents()

    def wait(self, condition):
        deadline = time.monotonic()+15
        while not condition() and time.monotonic() < deadline: QTest.qWait(2)
        self.assertTrue(condition())

    def text(self, tag, n=10000):
        return '\n'.join(f'{i:05} 😀 {tag} synthetic line duplicate payload' for i in range(n))

    def test_large_reset_is_atomic_export_old_then_latest_revision_wins(self):
        view = self.view
        view.show_text('old complete\n')
        a, b = self.text('obsolete'), self.text('latest')
        with patch.object(view.output, 'setPlainText', wraps=view.output.setPlainText) as reset:
            view.show_text(a)
            self.assertTrue(view._building)
            self.assertEqual(view.output.toPlainText(), 'old complete\n')
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp)/'old.log'
                with patch('quaber.pod_logs.QFileDialog.getSaveFileName', return_value=(str(path), '')):
                    view.export()
                self.wait(lambda: not view._download_running)
                self.assertEqual(path.read_text(), self.server_log)
            view.show_text(b)
            self.wait(lambda: view._display_text == b)
            reset.assert_not_called()
        self.assertEqual(view.output.toPlainText(), b)
        self.assertEqual(view._display_stats[0], 10000)
        self.assertFalse(view._building)

    def test_pause_during_preparation_keeps_old_and_resume_adopts_whole(self):
        view = self.view
        view.show_text('read position\n')
        text = self.text('pending')
        view.show_text(text)
        view.pause_follow()
        self.wait(lambda: not view._building)
        self.assertEqual(view._display_text, 'read position\n')
        self.assertEqual(view.output.toPlainText(), 'read position\n')
        self.assertEqual(view.pending_text, text)
        view.resume_follow()
        self.wait(lambda: view._display_text == text)
        self.assertEqual(view.output.toPlainText(), text)

    def test_unicode_search_append_unchanged_and_cancelled_old_callback(self):
        view = self.view
        raw = self.text('target').replace('\n', '\r\n')
        expected = raw.replace('\r\n', '\n')
        view.search.setText('target')
        view.show_text(raw)
        self.wait(lambda: view._display_text == expected)
        self.assertEqual(len(view.matches), 10000)
        view.find_next()
        self.assertEqual(view.output.textCursor().selectedText(), 'target')
        view.resume_follow()
        with patch.object(view.output, 'setDocument', wraps=view.output.setDocument) as document, \
             patch.object(view.output, 'setPlainText', wraps=view.output.setPlainText) as reset:
            suffix = '\n😀 suffix'
            view.show_text(expected+suffix)
            self.wait(lambda: view._display_text == expected+suffix)
            document.assert_not_called()
            reset.assert_not_called()
            with patch.object(view, '_prepare_large', wraps=view._prepare_large) as prepare:
                view.show_text(expected+suffix)
                prepare.assert_not_called()
        view.show_text(self.text('canceled'))
        view.cancel_preparation()
        self.wait(lambda: not self.owner.background.running)
        self.assertEqual(view._display_text, expected+suffix)

    def test_small_revision_wins_over_large_build_and_hidden_view_cancels(self):
        view = self.view
        view.show_text(self.text('obsolete'))
        view.show_text('latest small')
        self.wait(lambda: not self.owner.background.running)
        self.assertEqual(view.output.toPlainText(), 'latest small')
        view.show_text(self.text('hidden'))
        view.hide()
        self.wait(lambda: not self.owner.background.running)
        self.assertEqual(view._display_text, 'latest small')

    def test_new_source_keeps_large_old_fragment_and_export_identity_until_atomic_accept(self):
        view = self.view
        old = self.text('current')
        view.show_text(old)
        self.wait(lambda: view._display_text == old)
        view.source = ('app', True, None)
        with patch.object(view.output, 'setPlainText', wraps=view.output.setPlainText) as reset:
            view.show_text('previous small')
            self.assertEqual(view.output.toPlainText(), old)
            self.assertEqual(view._display_source, ('app', False, None))
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp)/'old.log'
                with patch('quaber.pod_logs.QFileDialog.getSaveFileName', return_value=(str(path), '')) as dialog:
                    view.export()
                self.wait(lambda: not view._download_running)
                self.assertEqual(path.read_text(encoding='utf-8'), self.server_log)
                self.assertIn('pod-app-current-', dialog.call_args.args[2])
            self.wait(lambda: view._display_text == 'previous small')
            reset.assert_not_called()
        self.assertEqual(view.output.toPlainText(), 'previous small')
        self.assertEqual(view._display_source, ('app', True, None))
