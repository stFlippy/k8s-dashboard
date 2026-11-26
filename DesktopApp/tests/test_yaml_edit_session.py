"""User-level YAML search, paused access, cancellation and server reloads."""
import copy
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QDialog
from quaber.confirmations import ResourceConfirmation
from quaber.shutdown import release_window
from quaber.capabilities import AccessOutcome
from test_ux import ShellFixture


class YamlEditSessionTests(ShellFixture, unittest.TestCase):
    def editing(self):
        panel = self.open_panel('deployment')
        self.ready_yaml(panel)
        self.window.set_read_only(False)
        self.wait()
        self.assertFalse(panel.yaml_editor.editor.isReadOnly())
        return panel

    def change_replicas(self, panel):
        editor = panel.yaml_editor.editor
        cursor = editor.document().find('replicas: 1')
        self.assertFalse(cursor.isNull())
        editor.setTextCursor(cursor)
        editor.setFocus()
        QTest.keyClicks(editor, 'replicas: 3')
        self.assertTrue(editor.document().isModified())

    def test_search_keeps_editing_with_stale_positive_rights_and_pauses_timeout(self):
        panel = self.editing()
        access = self.window.admin_access
        signature, stamp, rights = access.action_cache['c']
        access.action_cache['c'] = (signature, time.monotonic()-35, rights)
        panel.update_write_controls()
        self.assertFalse(access.allows('yaml'))
        self.assertFalse(panel.yaml_editor.editor.isReadOnly())
        panel.yaml_editor.show_search()
        panel.yaml_editor.search.setText('replicas: 1')
        panel.yaml_editor.find_match()
        panel.yaml_editor.editor.setFocus()
        QTest.keyClicks(panel.yaml_editor.editor, 'replicas: 3')
        self.assertIn('replicas: 3', panel.yaml_editor.toPlainText())
        self.assertTrue(panel.yaml_editor.editor.document().isModified())
        actions = self.window.shell_actions
        self.assertFalse(actions.idle_timer.isActive())
        actions._expire_admin()  # A timeout queued before entering YAML cannot revoke it.
        self.assertFalse(self.window.preferences['read_only'])

    def test_idle_timer_resumes_after_leaving_yaml(self):
        panel = self.editing()
        actions = self.window.shell_actions
        remaining = actions.admin_time_remaining()
        QTest.qWait(40)
        self.assertEqual(actions.admin_time_remaining(), remaining)
        panel.tabs.setCurrentIndex(0)
        self.assertTrue(actions.idle_timer.isActive())
        self.assertGreater(actions.idle_timer.remainingTime(), 0)
        self.assertLessEqual(actions.idle_timer.remainingTime(), remaining)

    def test_manual_lock_discards_undo_and_loads_fresh_server_version(self):
        panel = self.editing()
        self.change_replicas(panel)
        self.deployment['metadata']['resourceVersion'] = '2'
        self.deployment['spec']['replicas'] = 7
        self.window.set_read_only(True)
        document = panel.yaml_editor.editor.document()
        self.assertFalse(document.isModified())
        self.assertFalse(document.isUndoAvailable())
        self.assertNotIn('replicas: 3', panel.yaml_editor.toPlainText())
        self.wait()
        self.assertEqual(panel.version, '2')
        self.assertIn('replicas: 7', panel.yaml_editor.toPlainText())
        self.assertTrue(panel.yaml_editor.editor.isReadOnly())
        self.assertFalse(self.window.shell_actions.mode.isChecked())
        self.assertFalse(panel._yaml_reset_pending)

    def test_declining_save_revokes_mode_and_discards_draft(self):
        panel = self.editing()
        self.change_replicas(panel)
        with patch('quaber.ui.confirm', return_value=False):
            panel.save_yaml()
        self.wait()
        self.api.replace.assert_not_called()
        self.assertTrue(self.window.preferences['read_only'])
        self.assertFalse(self.window.shell_actions.mode.isChecked())
        self.assertTrue(panel.yaml_editor.editor.isReadOnly())
        self.assertFalse(panel.yaml_editor.editor.document().isModified())
        self.assertNotIn('replicas: 3', panel.yaml_editor.toPlainText())
        self.window.set_read_only(False)
        self.assertFalse(panel.yaml_editor.editor.isReadOnly())

    def test_reload_failure_does_not_resurrect_discarded_draft_and_retry_works(self):
        panel = self.editing()
        self.change_replicas(panel)
        self.api.resource.side_effect = ConnectionError('synthetic offline')
        self.window.set_read_only(True)
        self.wait()
        self.assertNotIn('replicas: 3', panel.yaml_editor.toPlainText())
        self.assertFalse(panel.yaml_editor.editor.document().isModified())
        self.assertTrue(panel._yaml_reset_pending)
        self.assertTrue(panel.yaml_reload.isEnabled())
        self.assertIn('Правки отменены', panel.yaml_notice_text.text())
        self.api.resource.side_effect = lambda *args: self.deployment
        panel.reload_yaml()
        self.wait()
        self.assertFalse(panel._yaml_reset_pending)
        self.assertTrue(panel._yaml_ready)

    def test_revoked_yaml_rights_discard_draft_even_when_scale_is_still_allowed(self):
        panel = self.editing()
        self.change_replicas(panel)
        with patch('quaber.admin_access.review_action', side_effect=lambda api, target:
                   SimpleNamespace(outcome=AccessOutcome.DENIED if target.action == 'yaml' else AccessOutcome.ALLOWED)):
            self.window.admin_access.check('c', force=True)
            self.wait()
        self.assertTrue(self.window.preferences['read_only'])
        self.assertFalse(panel.yaml_editor.editor.document().isModified())
        self.assertNotIn('replicas: 3', panel.yaml_editor.toPlainText())

    def test_confirmation_accepts_paused_admin_timer(self):
        panel = self.editing()
        self.change_replicas(panel)
        dialog = ResourceConfirmation(panel, 'Save', 'deployment/ns/app', 'Apply?', '--- old\n+++ new')
        self.addCleanup(release_window, dialog)
        self.assertFalse(dialog._access_expired())
        self.assertIn('приостановлен', dialog.access_status.text())
        dialog._accept_with_access()
        self.assertEqual(dialog.result(), QDialog.Accepted)
        self.assertFalse(self.window.preferences['read_only'])

    def test_successful_save_adopts_response_without_reloading_old_snapshot(self):
        panel = self.editing()
        self.change_replicas(panel)
        fresh = copy.deepcopy(self.deployment)
        fresh['metadata']['resourceVersion'] = '2'
        fresh['spec']['replicas'] = 3
        self.api.replace.return_value = fresh
        self.api.resource.reset_mock()
        with patch('quaber.ui.confirm', return_value=True), patch('quaber.ui.QMessageBox.information'):
            panel.save_yaml()
            self.wait()
        self.assertEqual(panel.version, '2')
        self.assertIn('replicas: 3', panel.yaml_editor.toPlainText())
        self.assertFalse(panel.yaml_editor.editor.document().isModified())
        self.assertFalse(getattr(panel, '_yaml_reset_pending', False))
        self.assertTrue(self.window.preferences['read_only'])
        self.api.resource.assert_not_called()

    def test_confirmation_from_before_access_loss_cannot_apply_discarded_yaml(self):
        panel = self.editing()
        self.change_replicas(panel)
        def confirm_after_revoke(*args, **kwargs):
            self.window.set_read_only(True)
            self.wait()
            self.window.set_read_only(False)
            return True
        with patch('quaber.ui.confirm', side_effect=confirm_after_revoke):
            panel.save_yaml()
        self.wait()
        self.api.replace.assert_not_called()
        self.assertTrue(self.window.preferences['read_only'])
        self.assertNotIn('replicas: 3', panel.yaml_editor.toPlainText())
