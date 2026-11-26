import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PyQt5.QtWidgets import QApplication, QDialog, QDialogButtonBox, QCheckBox, QLineEdit
from quaber.storage import UserStore
from quaber.confirmations import ResourceConfirmation
from quaber.diff_view import DiffView
from test_ux import ShellFixture, Deferred


class AtomicWriteTests(unittest.TestCase):
    def test_transient_replacement_lock_is_retried_without_losing_existing_data(self):
        import os
        replace = os.replace
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'clusters.json'
            target.write_bytes(b'old')
            calls = []
            def locked(source, destination):
                calls.append(source)
                if len(calls) < 3:
                    self.assertEqual(target.read_bytes(), b'old')
                    raise PermissionError('locked')
                replace(source, destination)
            with patch('quaber.storage.os.replace', side_effect=locked), patch('quaber.storage.time.sleep'):
                UserStore._atomic_bytes(target, b'new')
            self.assertEqual(target.read_bytes(), b'new')
            self.assertEqual(len(list(Path(directory).iterdir())), 1)

    def test_permanent_denial_preserves_original_and_reports_local_save_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'clusters.json'
            target.write_bytes(b'old')
            with patch('quaber.storage.os.replace', side_effect=PermissionError('denied')), patch('quaber.storage.time.sleep'):
                with self.assertRaisesRegex(PermissionError, 'Не удалось сохранить clusters.json'):
                    UserStore._atomic_bytes(target, b'new')
            self.assertEqual(target.read_bytes(), b'old')
            self.assertEqual(len(list(Path(directory).iterdir())), 1)


class ResourceReviewTests(ShellFixture, unittest.TestCase):
    def test_scale_cancel_locks_and_apply_commits_without_waiting(self):
        panel = self.open_panel('deployment')
        self.window.set_read_only(False)
        panel.scale_by(1)
        self.assertIn('1 → 2', panel.scale_apply.text())
        self.assertFalse(panel.scale_delay_timer.isActive())
        self.api.scale.assert_not_called()
        panel.scale_cancel.click()
        self.assertTrue(self.window.preferences['read_only'])
        self.assertIsNone(panel._scale_snapshot)
        self.api.scale.assert_not_called()
        panel.background.background = deferred = Deferred()
        self.window.set_read_only(False)
        panel.scale_by(1)
        panel.scale_apply.click()
        self.assertEqual(len(deferred.jobs), 1)
        self.assertFalse(panel.scale_delay_timer.isActive())
        updated = copy.deepcopy(panel.resource)
        updated['spec']['replicas'] = 2
        self.api.scale.return_value = updated
        deferred.finish(execute=True)
        self.assertTrue(self.window.preferences['read_only'])

    def test_review_remains_open_when_access_is_revoked_and_discards_yaml_draft(self):
        panel = self.open_panel('deployment')
        self.ready_yaml(panel)
        self.window.set_read_only(False)
        panel.yaml_editor.editor.insertPlainText('# draft\n')
        draft = panel.yaml_editor.toPlainText()
        diff = '--- a\n+++ b\n@@ -1 +1 @@\n-before\n+after'
        dialog = ResourceConfirmation(panel, 'Save', panel.operation_label(), 'Apply?', diff)
        self.addCleanup(dialog.deleteLater)
        dialog.show(); self.app.processEvents()
        self.assertTrue(dialog.findChild(DiffView).isVisible())
        self.assertEqual(dialog.findChildren(QCheckBox), [])
        self.window.set_read_only(True)
        dialog.buttons.button(QDialogButtonBox.StandardButton.Yes).click()
        self.assertTrue(dialog.isVisible())
        self.assertIn('Продлите', dialog.access_status.text())
        self.assertNotEqual(panel.yaml_editor.toPlainText(), draft)
        self.assertFalse(panel.yaml_editor.editor.document().isModified())
        self.assertFalse(panel.yaml_editor.editor.document().isUndoAvailable())
        dialog.reject()

    def test_journal_search_wraps_and_keyboard_search_focuses_field(self):
        self.window.navigate_workspace(self.window.tabs.indexOf(self.window.log_panel))
        page = self.window.log_panel
        page.clear()
        page.append('INFO one\nERROR first\nINFO two\nERROR second')
        page.search_field.setText('ERROR')
        self.assertEqual(page.output.textCursor().selectedText(), 'ERROR')
        first = page.output.textCursor().selectionStart()
        self.window.shell_actions.find(False)
        self.assertGreater(page.output.textCursor().selectionStart(), first)
        page.find_next()
        self.assertEqual(page.output.textCursor().selectionStart(), first)
        page.search_field.setText('missing')
        self.assertEqual(page.search_status.text(), 'Не найдено')

    def test_resources_are_bound_to_open_deployment_only_on_metrics_and_ram_is_mib(self):
        self.deployment['spec']['template'] = {'spec': {'containers': [
            {'name': 'istio-proxy'}, {'name': 'app', 'resources': {'limits': {'memory': '64Mi'}}}]}}
        panel = self.open_panel('deployment')
        self.window.set_read_only(False)
        self.assertFalse(panel.resources_button.isEnabled())
        index = next(i for i in range(panel.tabs.count()) if panel.tabs.tabText(i) == 'Метрики')
        panel.tabs.setCurrentIndex(index)
        self.wait()
        self.window.set_read_only(False)
        self.assertTrue(panel.resources_button.isEnabled())
        panel._deployment_metrics_display({'pods': [{'pod': 'app-pod', 'containers': [
            {'name': 'app', 'memory': {'usageBytes': 3731456}}]}]})
        self.assertEqual(panel.deployment_metrics_table.item(0, 3).text(), '3.56')
        self.assertTrue(panel.deployment_metrics_table.isVisible())
        self.assertIsNotNone(panel.deployment_graph)
        seen = []
        def inspect(dialog):
            seen.append(dialog.windowTitle())
            self.assertIn('64Mi', [field.text() for field in dialog.findChildren(QLineEdit)])
            return QDialog.DialogCode.Rejected
        with patch('quaber.ui.run_dialog', side_effect=inspect), patch('quaber.ui.QInputDialog.getItem') as popup:
            panel.edit_resources()
        self.assertEqual(seen, ['CPU / RAM — app'])
        popup.assert_not_called()
